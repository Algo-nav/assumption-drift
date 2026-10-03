"""Config loading and cache paths shared by the pipeline scripts."""

from __future__ import annotations

import calendar
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "pipeline" / "config.yaml"
RAW_DIR = REPO_ROOT / "data" / "raw"
CANDIDATES_DIR = REPO_ROOT / "data" / "candidates"
DRAFTS_DIR = REPO_ROOT / "data" / "drafts"
OUTCOMES_DIR = REPO_ROOT / "data" / "outcomes"
REVIEW_DIR = REPO_ROOT / "data" / "review"
RELEASE_DIR = REPO_ROOT / "data" / "release"
CARD_DIR = REPO_ROOT / "card"
FIGURES_DIR = CARD_DIR / "figures"

#: The only User-Agent this project may send to the SEC. Config must match it.
EXPECTED_USER_AGENT = "Navneet navn07588@gmail.com"


@dataclass(frozen=True)
class Company:
    name: str
    ticker: str
    cik: str  # ten digits, zero padded
    #: The month (1 to 12) the fiscal year ends in. None means the company's fiscal calendar is not known,
    #: and anything that needs to date a period ("Q4 FY21") leaves it undated.
    fiscal_year_end_month: int | None = None
    #: "end": FY2026 is the year that ENDS in 2026 (NVIDIA, Salesforce). "start": it is the year that STARTS
    #: in 2026 (Target's "fiscal 2022" ends January 28, 2023). One month cannot tell these apart.
    fiscal_year_named_for: str = "end"
    #: What a parenthesised figure on a TAX RATE line means for this filer. False (the default): it is a rate that is
    #: unusually large or worth calling out, so it is read as positive unless the evidence says negative, benefit or
    #: loss. True: this filer prints a rate that is a benefit or unusually low in parentheses, so the sign the model
    #: read is kept. Only the tax rate is ever affected; every other rate keeps the one global rule.
    tax_rate_parens_negative: bool = False
    #: False keeps the company out of the release, the figures, the Space and the card's table (06_publish): it is still
    #: fetched, drafted and reviewed like any other. Set for a company that issues no numeric guidance the metric
    #: list covers, and named on the card for that reason.
    publish: bool = True

    def fiscal_year_reported(self, quarter: int | None, filed_at: date) -> int | None:
        """The fiscal year a release filed on `filed_at` reports for a headline that names `quarter` (None: the fiscal
        year's own results, which ride with the fourth quarter) and no year. See `_fiscal_year_reported`."""
        return _fiscal_year_reported(self, 4 if quarter is None else quarter, filed_at)

    def period_end(self, year: int, quarter: int | None = None) -> date | None:
        """The last day of the month a fiscal period ends in, or None if the fiscal calendar is unknown."""
        if self.fiscal_year_end_month is None:
            return None
        return period_end(self.fiscal_year_end_month, self.fiscal_year_named_for, year, quarter)


def _fiscal_year_reported(company: "Company", quarter: int, filed_at: date, max_days: int = 150) -> int | None:
    """The fiscal year whose `quarter` ended most recently before `filed_at`, if it ended within `max_days` of it.
    A release headed "Fourth Quarter" with no year, filed 2020-02-26 by a company whose fiscal 2019 ended January 31,
    2020, reports fiscal 2019. None when the fiscal calendar is unknown or nothing ended that recently."""
    if company.fiscal_year_end_month is None:
        return None
    best: tuple[date, int] | None = None
    for year in range(filed_at.year - 2, filed_at.year + 2):
        end = company.period_end(year, quarter)
        if end is not None and end < filed_at and (best is None or end > best[0]):
            best = (end, year)
    return best[1] if best and (filed_at - best[0]).days <= max_days else None


def period_end(month: int, named_for: str, year: int, quarter: int | None = None) -> date:
    """The last day of the month in which fiscal `year` (or its `quarter`) ends, for a fiscal year that ends
    in `month`. Month granularity: a fiscal year that ends on the Sunday nearest January 31 counts as January."""
    end_year = year if named_for == "end" or month == 12 else year + 1
    index = end_year * 12 + (month - 1)  # months since year 0, counting from the fiscal year's last month
    if quarter is not None:
        index -= (4 - quarter) * 3
    y, m = divmod(index, 12)
    return date(y, m + 1, calendar.monthrange(y, m + 1)[1])


def load_config(path: Path | str = CONFIG_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    if config["edgar"]["user_agent"] != EXPECTED_USER_AGENT:
        raise ValueError(
            f"edgar.user_agent must be {EXPECTED_USER_AGENT!r}, got {config['edgar']['user_agent']!r}"
        )
    return config


def _company(entry: dict[str, Any]) -> Company:
    month = entry.get("fiscal_year_end_month")
    named_for = entry.get("fiscal_year_named_for", "end")
    if month is not None and not (isinstance(month, int) and not isinstance(month, bool) and 1 <= month <= 12):
        raise ValueError(f"{entry.get('ticker')}: fiscal_year_end_month must be a month from 1 to 12, got {month!r}")
    if named_for not in ("end", "start"):
        raise ValueError(f"{entry.get('ticker')}: fiscal_year_named_for must be 'end' or 'start', got {named_for!r}")
    parens = entry.get("tax_rate_parens_negative", False)
    if not isinstance(parens, bool):
        raise ValueError(f"{entry.get('ticker')}: tax_rate_parens_negative must be true or false, got {parens!r}")
    publish = entry.get("publish", True)
    if not isinstance(publish, bool):
        raise ValueError(f"{entry.get('ticker')}: publish must be true or false, got {publish!r}")
    return Company(name=entry["name"], ticker=entry["ticker"].upper(), cik=str(entry["cik"]).zfill(10),
                   fiscal_year_end_month=month, fiscal_year_named_for=named_for, tax_rate_parens_negative=parens, publish=publish)


def companies(config: dict[str, Any], only: list[str] | None = None) -> list[Company]:
    found = [_company(c) for c in config.get("companies") or []]
    if only:
        wanted = {t.upper() for t in only}
        found = [c for c in found if c.ticker in wanted]
    return found


def raw_paths(cik: str, accession: str) -> tuple[Path, Path]:
    """Cached document and its sidecar: data/raw/{cik}/{accession}.html and .meta.json."""
    folder = RAW_DIR / cik
    return folder / f"{accession}.html", folder / f"{accession}.meta.json"


def read_meta(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def stable_ulid(key: str, when: date) -> str:
    """A valid ULID that is the same every time for the same key.

    48 bits of millisecond timestamp from `when`, then 80 bits taken from a hash of
    the key. Review CSVs are edited by hand, so a re-run has to land on the same
    record_id for the same draft instead of minting a new one.
    """
    millis = int(datetime(when.year, when.month, when.day, tzinfo=timezone.utc).timestamp() * 1000)
    value = (millis << 80) | int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:10], "big")
    return "".join(_CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))
