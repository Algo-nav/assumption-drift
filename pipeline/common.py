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

    def period_end(self, year: int, quarter: int | None = None) -> date | None:
        """The last day of the month a fiscal period ends in, or None if the fiscal calendar is unknown."""
        if self.fiscal_year_end_month is None:
            return None
        return period_end(self.fiscal_year_end_month, self.fiscal_year_named_for, year, quarter)


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
    return Company(name=entry["name"], ticker=entry["ticker"].upper(), cik=str(entry["cik"]).zfill(10),
                   fiscal_year_end_month=month, fiscal_year_named_for=named_for)


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
