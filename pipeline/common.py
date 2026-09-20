"""Config loading and cache paths shared by the pipeline scripts."""

from __future__ import annotations

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


def load_config(path: Path | str = CONFIG_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    if config["edgar"]["user_agent"] != EXPECTED_USER_AGENT:
        raise ValueError(
            f"edgar.user_agent must be {EXPECTED_USER_AGENT!r}, got {config['edgar']['user_agent']!r}"
        )
    return config


def companies(config: dict[str, Any], only: list[str] | None = None) -> list[Company]:
    found = [
        Company(name=c["name"], ticker=c["ticker"].upper(), cik=str(c["cik"]).zfill(10))
        for c in config.get("companies") or []
    ]
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
