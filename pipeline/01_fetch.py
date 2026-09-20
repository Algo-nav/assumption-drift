"""Phase 1, step 1: EDGAR fetch and local cache.

    python -m pipeline.01_fetch [--config PATH] [--company TICKER ...] [--list-only]

Discovery uses the browse-edgar Atom feed (one company and form type at a time)
and each filing's index page. EDGAR full-text search was tried and set aside: in a
probe it listed only the main 8-K document, with no exhibit rows.

What gets cached, one document per filing:
    8-K    the earnings release exhibit, typed EX-99.1 or EX-99 (Target files EX-99)
    10-K   the primary document
    10-Q   the primary document

Cache layout: data/raw/{cik}/{accession}.html plus {accession}.meta.json holding
fetched_at, final URL, HTTP status and sha256. Only a 200 response is ever
written, and `final_url` in a meta file is the URL that returned it. Use that one,
not `requested_url`, when a row needs a source URL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import requests

from pipeline.common import CONFIG_PATH, Company, companies, load_config, raw_paths, read_meta
from research_record.schema import ALLOWED_HOSTS

#: SEC allows 10 per second per IP and blocks the address for about ten minutes
#: past that. This project never goes above 8, whatever the config says.
HARD_CEILING_RPS = 8

SMOKE_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000320193&type=10-K&output=atom"
BROWSE_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form}"
    "&dateb={dateb}&owner=include&count=100&start={start}&output=atom"
)
FEED_PAGE = 100
#: Earnings release exhibit types, most specific first.
EXHIBIT_TYPES = ("EX-99.1", "EX-99")

_ATOM = {"a": "http://www.w3.org/2005/Atom"}
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_ARCHIVE_PATH = re.compile(r"^/Archives/edgar/data/\d+/\d{18}/[^/?#]+$")


class EdgarError(Exception):
    """A fetch that failed. The run can carry on with the next document."""


class Blocked(EdgarError):
    """The SEC answered 403 or 429. Stop the whole run: pushing on extends the block."""


# --- rate limit ------------------------------------------------------------


class TokenBucket:
    """Token bucket. `acquire` blocks until a token is free.

    The default burst of 1 is deliberate. The SEC counts per second, so a
    bucket that starts full at 8 could pass 8 at once and 8 more as it refills
    inside the same second. With burst 1, requests are spaced by 1/rate.
    """

    def __init__(
        self,
        rate: float,
        burst: int = 1,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rate = float(rate)
        self.capacity = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._tokens = self.capacity
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1.0 - 1e-9:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            self._sleep(wait)


# --- http ------------------------------------------------------------------


@dataclass(frozen=True)
class Fetched:
    url: str
    final_url: str
    status: int
    content: bytes
    content_type: str | None
    fetched_at: datetime
    redirects: tuple[str, ...]


class EdgarClient:
    """Every SEC request goes through here: declared User-Agent, host allowlist, rate limit."""

    def __init__(
        self,
        user_agent: str,
        max_rps: float,
        *,
        session: Any = None,
        limiter: TokenBucket | None = None,
        retries: int = 3,
        max_redirects: int = 3,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not user_agent.strip():
            raise ValueError("a User-Agent is required by the SEC")
        if not 0 < max_rps <= HARD_CEILING_RPS:
            raise ValueError(f"max_rps must be in (0, {HARD_CEILING_RPS}], got {max_rps}")
        self.user_agent = user_agent
        self.session = session if session is not None else requests.Session()
        self.limiter = limiter if limiter is not None else TokenBucket(max_rps)
        self.retries = retries
        self.max_redirects = max_redirects
        self.timeout = timeout
        self._sleep = sleep
        self._utcnow = utcnow
        self.request_count = 0

    def headers(self, host: str, *, declare_user_agent: bool = True) -> dict[str, str | None]:
        return {
            "User-Agent": self.user_agent if declare_user_agent else None,
            "Accept-Encoding": "gzip, deflate",
            "Host": host,
        }

    @staticmethod
    def _check(url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
            raise EdgarError(f"refusing {url}: only https on {sorted(ALLOWED_HOSTS)} is allowed")
        return parts.hostname

    def _request(self, url: str, host: str, declare_user_agent: bool) -> Any:
        last: Exception | str = "no attempt made"
        for attempt in range(self.retries + 1):
            self.limiter.acquire()
            self.request_count += 1
            try:
                resp = self.session.get(
                    url,
                    headers=self.headers(host, declare_user_agent=declare_user_agent),
                    timeout=self.timeout,
                    allow_redirects=False,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last = exc
            else:
                if resp.status_code in (403, 429) and declare_user_agent:
                    raise Blocked(
                        f"HTTP {resp.status_code} from {url}. Stop and check the User-Agent and the "
                        "request rate; carrying on extends the block."
                    )
                if resp.status_code < 500:
                    return resp
                last = f"HTTP {resp.status_code}"
            if attempt < self.retries:
                self._sleep(2.0**attempt)
        raise EdgarError(f"giving up on {url}: {last}")

    def get(self, url: str, *, declare_user_agent: bool = True) -> Fetched:
        """GET with redirects followed by hand, so every hop is checked and rate limited."""
        current = url
        chain: list[str] = []
        for _ in range(self.max_redirects + 1):
            host = self._check(current)
            resp = self._request(current, host, declare_user_agent)
            if resp.status_code in _REDIRECTS:
                location = resp.headers.get("Location")
                if not location:
                    raise EdgarError(f"redirect from {current} with no Location")
                current = urljoin(current, location)
                chain.append(current)
                continue
            return Fetched(
                url=url,
                final_url=current,
                status=resp.status_code,
                content=resp.content,
                content_type=resp.headers.get("Content-Type"),
                fetched_at=self._utcnow(),
                redirects=tuple(chain),
            )
        raise EdgarError(f"too many redirects for {url}")


def smoke_test(client: EdgarClient, *, check_rejection: bool = False) -> None:
    """The SEC must accept our header, and (optionally) must refuse a request without one."""
    accepted = client.get(SMOKE_URL)
    if accepted.status != 200:
        raise EdgarError(f"smoke test: expected 200 with the User-Agent, got {accepted.status}")
    if check_rejection:
        refused = client.get(SMOKE_URL, declare_user_agent=False)
        if refused.status != 403:
            raise EdgarError(f"smoke test: expected 403 without a User-Agent, got {refused.status}")


# --- discovery -------------------------------------------------------------


@dataclass(frozen=True)
class Filing:
    accession: str
    form: str
    filed_at: date
    items: tuple[str, ...]
    index_url: str


def parse_feed(content: bytes) -> list[Filing]:
    """Filings in one Atom page. Takes bytes: the feed declares ISO-8859-1."""
    filings: list[Filing] = []
    for entry in ET.fromstring(content).findall("a:entry", _ATOM):
        body = entry.find("a:content", _ATOM)
        if body is None:
            continue

        def text(tag: str) -> str:
            element = body.find(f"a:{tag}", _ATOM)
            return (element.text or "").strip() if element is not None else ""

        accession, form, filed, href = (
            text("accession-number"), text("filing-type"), text("filing-date"), text("filing-href"),
        )
        if not (accession and form and filed and href):
            continue
        filings.append(
            Filing(
                accession=accession,
                form=form,
                filed_at=date.fromisoformat(filed),
                # Written like "items 1.01, 2.03and7.01", so pull the numbers out.
                items=tuple(re.findall(r"\d\.\d{2}", text("items-desc"))),
                index_url=href,
            )
        )
    return filings


def discover(client: EdgarClient, company: Company, config: dict[str, Any]) -> list[Filing]:
    edgar = config["edgar"]
    date_from = date.fromisoformat(edgar["date_from"])
    date_to = date.fromisoformat(edgar["date_to"])
    wanted = set(edgar["filing_types"])
    dateb = (date_to + timedelta(days=1)).strftime("%Y%m%d")

    found: dict[str, Filing] = {}
    for form in edgar["filing_types"]:
        start = 0
        while True:
            got = client.get(BROWSE_URL.format(cik=company.cik, form=form, dateb=dateb, start=start))
            if got.status != 200:
                raise EdgarError(f"{company.ticker} {form}: feed returned HTTP {got.status}")
            page = parse_feed(got.content)
            for filing in page:
                # The feed matches "10-K" as a prefix, so 10-K/A and 8-K/A arrive too.
                if filing.form in wanted and date_from <= filing.filed_at <= date_to:
                    found[filing.accession] = filing
            if len(page) < FEED_PAGE or min(f.filed_at for f in page) < date_from:
                break
            start += FEED_PAGE
    return sorted(found.values(), key=lambda f: (f.filed_at, f.accession))


@dataclass(frozen=True)
class IndexRow:
    type: str
    path: str  # /Archives/edgar/data/{cik}/{accession without dashes}/{file}


def _archive_path(href: str) -> str | None:
    parts = urlsplit(href)
    if parts.netloc and parts.netloc not in ALLOWED_HOSTS:
        return None
    path = parts.path
    if path == "/ix" and parts.query.startswith("doc="):  # inline XBRL viewer link
        path = parts.query[len("doc=") :].split("&")[0]
    return path if _ARCHIVE_PATH.match(path) else None


class _IndexParser(HTMLParser):
    """Rows of the 'Document Format Files' table: Seq, Description, Document, Type, Size."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[IndexRow] = []
        self._cells: list[str] | None = None
        self._cell: list[str] | None = None
        self._href: str | None = None
        self._row_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._cells, self._row_href = [], None
        elif tag == "td" and self._cells is not None:
            self._cell = []
        elif tag == "a" and self._cell is not None:
            self._href = dict(attrs).get("href")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._cells is not None and self._cell is not None:
            if len(self._cells) == 2 and self._href:  # the Document column
                self._row_href = self._href
            self._cells.append(" ".join("".join(self._cell).split()))
            self._cell, self._href = None, None
        elif tag == "tr" and self._cells is not None:
            if len(self._cells) >= 4 and self._cells[0].isdigit() and self._row_href:
                path = _archive_path(self._row_href)
                if path:
                    self.rows.append(IndexRow(type=self._cells[3], path=path))
            self._cells = None


def parse_index(content: bytes | str) -> list[IndexRow]:
    parser = _IndexParser()
    parser.feed(content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content)
    parser.close()
    return parser.rows


def pick_document(form: str, rows: list[IndexRow]) -> IndexRow | None:
    """8-K: the earnings release exhibit. 10-K and 10-Q: the primary document."""
    if form == "8-K":
        for wanted in EXHIBIT_TYPES:
            for row in rows:
                if row.type == wanted:
                    return row
        return None
    return next((row for row in rows if row.type == form), None)


# --- cache -----------------------------------------------------------------


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def is_cached(company: Company, accession: str) -> bool:
    """A cached document needs its sidecar, a 200, and bytes that still match the hash."""
    html_path, meta_path = raw_paths(company.cik, accession)
    meta = read_meta(meta_path)
    if not meta or meta.get("http_status") != 200 or not html_path.exists():
        return False
    return hashlib.sha256(html_path.read_bytes()).hexdigest() == meta.get("content_sha256")


def cache_document(
    client: EdgarClient, company: Company, filing: Filing, row: IndexRow
) -> Fetched | None:
    """Fetch one document and cache it. Returns None, writing nothing, unless it was a 200."""
    got = client.get("https://www.sec.gov" + row.path)
    if got.status != 200:
        return None
    html_path, meta_path = raw_paths(company.cik, filing.accession)
    _write_atomic(html_path, got.content)
    meta = {
        "cik": company.cik,
        "accession": filing.accession,
        "filing_type": filing.form,
        "doc_type": row.type,
        "filed_at": filing.filed_at.isoformat(),
        "items": list(filing.items),
        "requested_url": got.url,
        "final_url": got.final_url,
        "http_status": got.status,
        "content_type": got.content_type,
        "bytes": len(got.content),
        "content_sha256": hashlib.sha256(got.content).hexdigest(),
        "fetched_at": got.fetched_at.isoformat(timespec="seconds"),
    }
    _write_atomic(meta_path, (json.dumps(meta, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
    return got


# --- run -------------------------------------------------------------------


def fetch_company(
    client: EdgarClient, company: Company, config: dict[str, Any], *, list_only: bool = False
) -> Counter:
    stats: Counter = Counter()
    filings = discover(client, company, config)
    stats["filings"] = len(filings)
    for filing in filings:
        label = f"{company.ticker:<5} {filing.form:<5} {filing.filed_at} {filing.accession}"
        if is_cached(company, filing.accession):
            stats["cached"] += 1
            continue
        index = client.get(filing.index_url)
        if index.status != 200:
            stats["failed"] += 1
            print(f"{label}  index HTTP {index.status}, skipped", file=sys.stderr)
            continue
        row = pick_document(filing.form, parse_index(index.content))
        if row is None:
            stats["no_exhibit" if filing.form == "8-K" else "failed"] += 1
            continue
        if list_only:
            stats["would_fetch"] += 1
            print(f"{label}  {row.type:<8} {row.path}")
            continue
        try:
            got = cache_document(client, company, filing, row)
        except Blocked:
            raise
        except EdgarError as exc:
            stats["failed"] += 1
            print(f"{label}  {exc}", file=sys.stderr)
            continue
        if got is None:
            stats["failed"] += 1
            print(f"{label}  {row.type} non-200, not cached", file=sys.stderr)
            continue
        stats["fetched"] += 1
        print(f"{label}  {row.type:<8} {len(got.content):>9,} B")
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch EDGAR filings into data/raw/.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--company", action="append", metavar="TICKER", help="limit to this ticker; repeatable")
    parser.add_argument("--list-only", action="store_true", help="discover and print, download nothing")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    targets = companies(config, args.company)
    if not targets:
        print("No companies to fetch. Check `companies:` in config.yaml.", file=sys.stderr)
        return 2

    edgar = config["edgar"]
    client = EdgarClient(edgar["user_agent"], edgar["max_rps"])
    started = time.monotonic()
    try:
        smoke_test(client)
        totals: Counter = Counter()
        for company in targets:
            print(f"== {company.ticker} {company.name} ({company.cik})")
            stats = fetch_company(client, company, config, list_only=args.list_only)
            totals.update(stats)
            print("   " + "  ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    except Blocked as exc:
        print(f"STOPPED: {exc}", file=sys.stderr)
        return 3
    except EdgarError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - started
    print(f"done: {client.request_count} requests in {elapsed:.0f}s ({client.request_count / max(elapsed, 1e-9):.1f}/s)  "
          + "  ".join(f"{k}={v}" for k, v in sorted(totals.items())))
    return 1 if totals["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
