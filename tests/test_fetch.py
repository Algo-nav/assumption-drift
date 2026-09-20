"""01_fetch.py, offline. A fake session stands in for the SEC, so nothing here touches the network."""

from __future__ import annotations

import hashlib
import importlib
import json
from datetime import date

import pytest
import requests
from requests.structures import CaseInsensitiveDict

fetch = importlib.import_module("pipeline.01_fetch")
common = importlib.import_module("pipeline.common")

UA = "Navneet navn07588@gmail.com"


# --- fakes -----------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"", headers: dict | None = None) -> None:
        self.status_code = status
        self.content = body
        self.headers = CaseInsensitiveDict(headers or {})


class FakeSession:
    """Serves a URL -> response (or list of responses, or an exception) map and logs every call."""

    def __init__(self, routes: dict) -> None:
        self.routes = {url: (list(r) if isinstance(r, list) else r) for url, r in routes.items()}
        self.calls: list[tuple[str, dict, bool]] = []

    def get(self, url, headers=None, timeout=None, allow_redirects=None):
        self.calls.append((url, dict(headers or {}), allow_redirects))
        if url not in self.routes:
            raise AssertionError(f"unexpected request: {url}")
        route = self.routes[url]
        if isinstance(route, list):
            route = route.pop(0)
        if isinstance(route, Exception):
            raise route
        return route

    def urls(self) -> list[str]:
        return [c[0] for c in self.calls]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make_client(routes: dict, **kwargs) -> tuple[fetch.EdgarClient, FakeSession, Clock]:
    clock = Clock()
    session = FakeSession(routes)
    limiter = fetch.TokenBucket(8, clock=clock, sleep=clock.sleep)
    client = fetch.EdgarClient(UA, 8, session=session, limiter=limiter, sleep=clock.sleep, **kwargs)
    return client, session, clock


# --- rate limit ------------------------------------------------------------


def test_token_bucket_never_passes_more_than_eight_in_any_second() -> None:
    clock = Clock()
    bucket = fetch.TokenBucket(8, clock=clock, sleep=clock.sleep)
    grants = []
    for _ in range(60):
        bucket.acquire()
        grants.append(clock.now)
    # Nine grants in a row must span at least a full second.
    assert all(grants[i + 8] - grants[i] >= 1.0 - 1e-6 for i in range(len(grants) - 8))


def test_token_bucket_first_request_is_immediate() -> None:
    clock = Clock()
    fetch.TokenBucket(8, clock=clock, sleep=clock.sleep).acquire()
    assert clock.slept == []


def test_token_bucket_does_not_bank_a_burst_while_idle() -> None:
    clock = Clock()
    bucket = fetch.TokenBucket(8, clock=clock, sleep=clock.sleep)
    bucket.acquire()
    clock.now += 60.0  # a long idle stretch
    bucket.acquire()
    bucket.acquire()
    assert sum(clock.slept) == pytest.approx(1 / 8)  # the second of the two still waits


def test_client_refuses_a_rate_above_the_ceiling_and_an_empty_user_agent() -> None:
    with pytest.raises(ValueError):
        fetch.EdgarClient(UA, 9)
    with pytest.raises(ValueError):
        fetch.EdgarClient(UA, 0)
    with pytest.raises(ValueError):
        fetch.EdgarClient("  ", 8)


# --- http ------------------------------------------------------------------


def test_every_request_carries_the_declared_headers() -> None:
    www, efts = "https://www.sec.gov/a", "https://efts.sec.gov/LATEST/search-index?q=x"
    client, session, _ = make_client({www: FakeResponse(200, b"x"), efts: FakeResponse(200, b"y")})
    client.get(www)
    client.get(efts)
    assert session.calls[0][1] == {"User-Agent": UA, "Accept-Encoding": "gzip, deflate", "Host": "www.sec.gov"}
    assert session.calls[1][1] == {"User-Agent": UA, "Accept-Encoding": "gzip, deflate", "Host": "efts.sec.gov"}
    assert all(call[2] is False for call in session.calls)  # redirects are followed by hand


@pytest.mark.parametrize(
    "url",
    ["https://example.com/x", "http://www.sec.gov/x", "https://sec.gov.evil.example/x", "https://data.sec.gov/x"],
)
def test_hosts_and_schemes_outside_the_allowlist_are_refused_before_any_request(url: str) -> None:
    client, session, _ = make_client({})
    with pytest.raises(fetch.EdgarError):
        client.get(url)
    assert session.calls == []


def test_redirects_are_followed_by_hand_and_each_hop_is_counted() -> None:
    a, b = "https://www.sec.gov/old", "https://www.sec.gov/new"
    client, session, _ = make_client(
        {a: FakeResponse(301, headers={"Location": "/new"}), b: FakeResponse(200, b"ok", {"Content-Type": "text/html"})}
    )
    got = client.get(a)
    assert (got.status, got.final_url, got.redirects, got.content_type) == (200, b, (b,), "text/html")
    assert client.request_count == 2


def test_a_redirect_off_the_allowlist_is_refused() -> None:
    a = "https://www.sec.gov/old"
    client, session, _ = make_client({a: FakeResponse(302, headers={"Location": "https://example.com/x"})})
    with pytest.raises(fetch.EdgarError):
        client.get(a)
    assert session.urls() == [a]


def test_a_redirect_loop_gives_up() -> None:
    a = "https://www.sec.gov/loop"
    client, _, _ = make_client({a: [FakeResponse(302, headers={"Location": a})] * 10})
    with pytest.raises(fetch.EdgarError, match="too many redirects"):
        client.get(a)


@pytest.mark.parametrize("status", [403, 429])
def test_a_403_or_429_stops_the_run_and_is_not_retried(status: int) -> None:
    url = "https://www.sec.gov/a"
    client, session, clock = make_client({url: [FakeResponse(status), FakeResponse(200)]})
    with pytest.raises(fetch.Blocked):
        client.get(url)
    assert len(session.calls) == 1
    assert clock.slept == []


def test_server_errors_are_retried_with_backoff_then_succeed() -> None:
    url = "https://www.sec.gov/a"
    client, session, clock = make_client({url: [FakeResponse(503), FakeResponse(500), FakeResponse(200, b"ok")]})
    assert client.get(url).content == b"ok"
    assert len(session.calls) == 3
    backoffs = [s for s in clock.slept if s >= 1.0]
    assert backoffs == [1.0, 2.0]


def test_connection_errors_are_retried_and_then_give_up() -> None:
    url = "https://www.sec.gov/a"
    client, session, _ = make_client({url: [requests.ConnectionError("reset")] * 10}, retries=2)
    with pytest.raises(fetch.EdgarError, match="giving up"):
        client.get(url)
    assert len(session.calls) == 3


def test_404_is_returned_not_raised() -> None:
    url = "https://www.sec.gov/missing"
    client, _, _ = make_client({url: FakeResponse(404, b"nope")})
    assert client.get(url).status == 404


# --- smoke test ------------------------------------------------------------


def _smoke_session(without_ua_status: int) -> dict:
    class Session(FakeSession):
        def get(self, url, headers=None, timeout=None, allow_redirects=None):
            self.calls.append((url, dict(headers or {}), allow_redirects))
            return FakeResponse(200 if headers["User-Agent"] else without_ua_status, b"<feed/>")

    return Session({})


def test_smoke_test_passes_when_the_sec_behaves_as_documented() -> None:
    clock = Clock()
    session = _smoke_session(403)
    client = fetch.EdgarClient(UA, 8, session=session, limiter=fetch.TokenBucket(8, clock=clock, sleep=clock.sleep))
    fetch.smoke_test(client, check_rejection=True)
    assert [c[1]["User-Agent"] for c in session.calls] == [UA, None]  # the second call omits it entirely


def test_smoke_test_fails_if_a_request_without_a_user_agent_is_accepted() -> None:
    clock = Clock()
    client = fetch.EdgarClient(
        UA, 8, session=_smoke_session(200), limiter=fetch.TokenBucket(8, clock=clock, sleep=clock.sleep)
    )
    with pytest.raises(fetch.EdgarError, match="expected 403"):
        fetch.smoke_test(client, check_rejection=True)


def test_smoke_test_fails_if_the_declared_user_agent_is_refused() -> None:
    client, _, _ = make_client({fetch.SMOKE_URL: FakeResponse(200, b"")})
    client.session.routes[fetch.SMOKE_URL] = FakeResponse(500)
    with pytest.raises(fetch.EdgarError):
        fetch.smoke_test(client)


# --- discovery parsing -----------------------------------------------------


def atom(entries: list[tuple[str, str, str, str, str]]) -> bytes:
    """(accession, form, filed, items_desc, index href) -> an ISO-8859-1 Atom page like the SEC's."""
    body = "".join(
        f'<entry><title>{form} - Caf\u00e9</title><content type="text/xml">'
        f"<accession-number>{acc}</accession-number><filing-date>{filed}</filing-date>"
        f"<filing-href>{href}</filing-href><filing-type>{form}</filing-type>"
        f"<items-desc>{items}</items-desc></content></entry>"
        for acc, form, filed, items, href in entries
    )
    xml = f'<?xml version="1.0" encoding="ISO-8859-1" ?>\n<feed xmlns="http://www.w3.org/2005/Atom">{body}</feed>'
    return xml.encode("iso-8859-1")


def index_url(acc: str, cik: str = "123") -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{acc}-index.htm"


def index_html(rows: list[tuple[int, str, str, str]]) -> bytes:
    """(seq, type, file name, href) -> a filing index page."""
    trs = "".join(
        f'<tr><td>{seq}</td><td>{typ}</td><td><a href="{href}">{name}</a> iXBRL</td><td>{typ}</td><td>9 KB</td></tr>'
        for seq, typ, name, href in rows
    )
    return (
        '<html><body><table summary="Document Format Files"><tr><th>Seq</th><th>Description</th>'
        f"<th>Document</th><th>Type</th><th>Size</th></tr>{trs}</table></body></html>"
    ).encode()


def test_parse_feed_reads_iso_8859_1_and_pulls_items_out_of_messy_text() -> None:
    page = atom([("0000000123-24-000001", "8-K", "2024-02-01", "items 1.01, 2.03and7.01", index_url("0000000123-24-000001"))])
    (filing,) = fetch.parse_feed(page)
    assert filing.accession == "0000000123-24-000001"
    assert filing.filed_at == date(2024, 2, 1)
    assert filing.items == ("1.01", "2.03", "7.01")


def test_parse_index_reads_plain_and_inline_xbrl_links() -> None:
    base = "/Archives/edgar/data/123/000000012324000003"
    rows = fetch.parse_index(
        index_html(
            [
                (1, "10-K", "x-20231231.htm", f"/ix?doc={base}/x-20231231.htm"),
                (2, "EX-99.1", "ex991.htm", f"{base}/ex991.htm"),
                (3, "GRAPHIC", "logo.jpg", f"{base}/logo.jpg"),
                (4, "EX-101.SCH", "x.xsd", "https://example.com/elsewhere/x.xsd"),  # not an archive path
            ]
        )
    )
    assert [(r.type, r.path.rsplit("/", 1)[1]) for r in rows] == [
        ("10-K", "x-20231231.htm"),
        ("EX-99.1", "ex991.htm"),
        ("GRAPHIC", "logo.jpg"),
    ]


def test_pick_document_takes_the_earnings_exhibit_for_8k_and_the_primary_for_the_rest() -> None:
    R = fetch.IndexRow
    both = [R("8-K", "/Archives/edgar/data/1/000000000000000001/main.htm"),
            R("EX-99", "/Archives/edgar/data/1/000000000000000001/a.htm"),
            R("EX-99.1", "/Archives/edgar/data/1/000000000000000001/b.htm")]
    assert fetch.pick_document("8-K", both).path.endswith("b.htm")  # EX-99.1 preferred
    assert fetch.pick_document("8-K", both[:2]).path.endswith("a.htm")  # Target files plain EX-99
    assert fetch.pick_document("8-K", both[:1]) is None  # main document only: not an earnings release
    assert fetch.pick_document("10-K", [R("10-K", "/Archives/edgar/data/1/000000000000000001/k.htm")]).path.endswith("k.htm")
    assert fetch.pick_document("10-Q", [R("10-K", "/Archives/edgar/data/1/000000000000000001/k.htm")]) is None


# --- discovery and cache, end to end ---------------------------------------

COMPANY = common.Company(name="Example Corp", ticker="EXMP", cik="0000000123")
CONFIG = {
    "edgar": {
        "filing_types": ["8-K", "10-K", "10-Q"],
        "date_from": "2019-01-01",
        "date_to": "2025-12-31",
    }
}
ACC_8K_EX99, ACC_8K_BARE, ACC_10K, ACC_8KA, ACC_OLD = (
    "0000000123-24-000001", "0000000123-24-000002", "0000000123-24-000003",
    "0000000123-24-000004", "0000000123-18-000001",
)


def sec_routes(doc_bodies: dict[str, FakeResponse] | None = None) -> dict:
    def feed(form: str, entries: list) -> tuple[str, FakeResponse]:
        url = fetch.BROWSE_URL.format(cik=COMPANY.cik, form=form, dateb="20260101", start=0)
        return url, FakeResponse(200, atom(entries))

    base = "/Archives/edgar/data/123"
    routes = dict(
        [
            feed("8-K", [
                (ACC_8K_EX99, "8-K", "2024-02-01", "items 2.02 and 9.01", index_url(ACC_8K_EX99)),
                (ACC_8K_BARE, "8-K", "2024-03-01", "item 5.02", index_url(ACC_8K_BARE)),
                (ACC_8KA, "8-K/A", "2024-05-01", "item 2.02", index_url(ACC_8KA)),  # amendment: not wanted
                (ACC_OLD, "8-K", "2018-12-01", "item 2.02", index_url(ACC_OLD)),  # before date_from
            ]),
            feed("10-K", [(ACC_10K, "10-K", "2024-04-01", "", index_url(ACC_10K))]),
            feed("10-Q", []),
        ]
    )
    routes[index_url(ACC_8K_EX99)] = FakeResponse(200, index_html([
        (1, "8-K", "main.htm", f"/ix?doc={base}/{ACC_8K_EX99.replace('-', '')}/main.htm"),
        (2, "EX-99", "release.htm", f"{base}/{ACC_8K_EX99.replace('-', '')}/release.htm"),
    ]))
    routes[index_url(ACC_8K_BARE)] = FakeResponse(200, index_html([
        (1, "8-K", "main.htm", f"/ix?doc={base}/{ACC_8K_BARE.replace('-', '')}/main.htm"),
    ]))
    routes[index_url(ACC_10K)] = FakeResponse(200, index_html([
        (1, "10-K", "annual.htm", f"/ix?doc={base}/{ACC_10K.replace('-', '')}/annual.htm"),
    ]))
    routes[f"https://www.sec.gov{base}/{ACC_8K_EX99.replace('-', '')}/release.htm"] = FakeResponse(
        200, b"<html><body><p>The Company expects revenue of $5.0 billion.</p></body></html>", {"Content-Type": "text/html"}
    )
    routes[f"https://www.sec.gov{base}/{ACC_10K.replace('-', '')}/annual.htm"] = FakeResponse(
        200, b"<html><body><p>Annual report.</p></body></html>"
    )
    routes.update(doc_bodies or {})
    return routes


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "RAW_DIR", tmp_path / "raw")
    return tmp_path / "raw"


def test_discover_keeps_only_the_wanted_forms_inside_the_date_range() -> None:
    client, _, _ = make_client(sec_routes())
    found = fetch.discover(client, COMPANY, CONFIG)
    assert [(f.accession, f.form) for f in found] == [
        (ACC_8K_EX99, "8-K"),
        (ACC_8K_BARE, "8-K"),
        (ACC_10K, "10-K"),
    ]


def test_discover_pages_through_a_full_feed() -> None:
    first = [(f"0000000123-24-{i:06d}", "8-K", "2024-06-01", "item 2.02", index_url(f"0000000123-24-{i:06d}")) for i in range(1, 101)]
    second = [("0000000123-23-000001", "8-K", "2023-06-01", "item 2.02", index_url("0000000123-23-000001"))]
    url = lambda form, start: fetch.BROWSE_URL.format(cik=COMPANY.cik, form=form, dateb="20260101", start=start)
    client, session, _ = make_client({
        url("8-K", 0): FakeResponse(200, atom(first)),
        url("8-K", 100): FakeResponse(200, atom(second)),
    })
    found = fetch.discover(client, COMPANY, {"edgar": {**CONFIG["edgar"], "filing_types": ["8-K"]}})
    assert len(found) == 101
    assert session.urls() == [url("8-K", 0), url("8-K", 100)]


def test_fetch_company_caches_only_what_it_should_and_writes_a_sidecar(raw_dir) -> None:
    client, session, _ = make_client(sec_routes())
    stats = fetch.fetch_company(client, COMPANY, CONFIG)
    assert (stats["filings"], stats["fetched"], stats["no_exhibit"], stats["cached"]) == (3, 2, 1, 0)

    html_path, meta_path = common.raw_paths(COMPANY.cik, ACC_8K_EX99)
    assert html_path == raw_dir / "0000000123" / f"{ACC_8K_EX99}.html"
    body = html_path.read_bytes()
    meta = json.loads(meta_path.read_text())
    assert meta["content_sha256"] == hashlib.sha256(body).hexdigest()
    assert meta["http_status"] == 200
    assert meta["final_url"].startswith("https://www.sec.gov/Archives/edgar/data/123/")
    assert meta["final_url"].endswith("/release.htm")
    assert (meta["filing_type"], meta["doc_type"], meta["filed_at"], meta["items"]) == ("8-K", "EX-99", "2024-02-01", ["2.02", "9.01"])
    assert meta["fetched_at"].endswith("+00:00")
    # Nothing was cached for the bare 8-K, and no temp files are left behind.
    assert not common.raw_paths(COMPANY.cik, ACC_8K_BARE)[0].exists()
    assert not list(raw_dir.rglob("*.tmp"))


def test_a_rerun_skips_cached_documents(raw_dir) -> None:
    fetch.fetch_company(make_client(sec_routes())[0], COMPANY, CONFIG)
    client, session, _ = make_client(sec_routes())
    stats = fetch.fetch_company(client, COMPANY, CONFIG)
    assert (stats["cached"], stats["fetched"]) == (2, 0)
    assert not [u for u in session.urls() if u.endswith(("release.htm", "annual.htm"))]


def test_a_cached_file_that_no_longer_matches_its_hash_is_fetched_again(raw_dir) -> None:
    fetch.fetch_company(make_client(sec_routes())[0], COMPANY, CONFIG)
    common.raw_paths(COMPANY.cik, ACC_10K)[0].write_bytes(b"corrupted")
    stats = fetch.fetch_company(make_client(sec_routes())[0], COMPANY, CONFIG)
    assert (stats["cached"], stats["fetched"]) == (1, 1)


def test_a_non_200_document_is_not_cached(raw_dir) -> None:
    release = f"https://www.sec.gov/Archives/edgar/data/123/{ACC_8K_EX99.replace('-', '')}/release.htm"
    client, _, _ = make_client(sec_routes({release: FakeResponse(404, b"gone")}))
    stats = fetch.fetch_company(client, COMPANY, CONFIG)
    assert (stats["fetched"], stats["failed"]) == (1, 1)
    html_path, meta_path = common.raw_paths(COMPANY.cik, ACC_8K_EX99)
    assert not html_path.exists() and not meta_path.exists()


def test_list_only_downloads_no_documents(raw_dir) -> None:
    client, session, _ = make_client(sec_routes())
    stats = fetch.fetch_company(client, COMPANY, CONFIG, list_only=True)
    assert stats["would_fetch"] == 2
    assert not list(raw_dir.rglob("*")) if raw_dir.exists() else True
    assert not [u for u in session.urls() if u.endswith(("release.htm", "annual.htm"))]
