"""data/manifest.json: how far the weekly refresh has read EDGAR.

    python -m pipeline.manifest bootstrap --processed-through YYYY-MM-DD [--company TICKER ...]
    python -m pipeline.manifest advance [--filings PATH] [--run-date YYYY-MM-DD]

The file (tracked) holds `processed_through`, the date the last complete, clean run covered, and for each company
`last_filing_date` (its newest filing of any wanted form that has been processed) and `accessions_on_last_date` (every
accession filed on that date, so a second filing on the same day is not fetched again when the next run starts at
`last_filing_date`, inclusive).

`bootstrap` builds it once from the cached sidecars in data/raw/. `advance` reads the `new_filings.json` that
`01_fetch --since-manifest` wrote and moves a company forward ONLY if every one of its new filings was fetched cleanly;
one failure holds that company where it was, so the filing is looked for again next time. `processed_through` moves only
when the run was complete (no SEC block) and every company in the manifest was covered and advanced.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from pipeline import common
from pipeline.common import NEW_FILINGS_PATH, RAW_DIR, REVIEW_DIR, companies, load_config, read_meta

#: Statuses of a filing in new_filings.json that count as handled. Anything else (failed, would_fetch) holds the company.
CLEAN_STATUSES = frozenset({"fetched", "cached", "no_exhibit"})


class ManifestError(Exception):
    """The manifest is missing or is not what this module writes."""


@dataclass(frozen=True)
class CompanyState:
    last_filing_date: date
    accessions_on_last_date: tuple[str, ...] = ()


@dataclass(frozen=True)
class Manifest:
    processed_through: date
    companies: dict[str, CompanyState] = field(default_factory=dict)


def load_manifest(path: Path | str | None = None) -> Manifest:
    path = Path(path) if path is not None else common.MANIFEST_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"{path}: no manifest. Build one with `python -m pipeline.manifest bootstrap`.") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path}: not valid JSON ({exc})") from exc
    try:
        states = {
            cik: CompanyState(date.fromisoformat(entry["last_filing_date"]), tuple(sorted(entry["accessions_on_last_date"])))
            for cik, entry in payload["companies"].items()
        }
        return Manifest(date.fromisoformat(payload["processed_through"]), states)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ManifestError(f"{path}: malformed manifest ({exc!r})") from exc


def save_manifest(manifest: Manifest, path: Path | str | None = None) -> None:
    path = Path(path) if path is not None else common.MANIFEST_PATH
    payload = {
        "processed_through": manifest.processed_through.isoformat(),
        "companies": {
            cik: {"last_filing_date": state.last_filing_date.isoformat(), "accessions_on_last_date": sorted(state.accessions_on_last_date)}
            for cik, state in sorted(manifest.companies.items())
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def bootstrap(raw_dir: Path, ciks: list[str], processed_through: date) -> Manifest:
    """A manifest from the cached sidecars: each company's newest filed_at over every cached filing, and the accessions on it."""
    states: dict[str, CompanyState] = {}
    for cik in ciks:
        metas = [m for m in (read_meta(p) for p in sorted((raw_dir / cik).glob("*.meta.json"))) if m and m.get("http_status") == 200]
        if not metas:
            raise ManifestError(f"{cik}: no cached filings under {raw_dir / cik}, nothing to bootstrap from")
        newest = max(m["filed_at"] for m in metas)
        states[cik] = CompanyState(date.fromisoformat(newest), tuple(sorted(m["accession"] for m in metas if m["filed_at"] == newest)))
    return Manifest(processed_through, states)


def advance(manifest: Manifest, new_filings: dict[str, Any], *, run_date: date) -> tuple[Manifest, dict[str, str]]:
    """(the manifest after this run, what happened to each company). `new_filings` is the parsed new_filings.json."""
    by_cik: dict[str, list[dict[str, Any]]] = {}
    for filing in new_filings.get("filings", []):
        by_cik.setdefault(filing["cik"], []).append(filing)
    covered = set(new_filings.get("companies") or by_cik)
    states = dict(manifest.companies)
    report: dict[str, str] = {}
    for cik, state in manifest.companies.items():
        if not new_filings.get("complete", False):
            report[cik] = "held: the run did not complete"
            continue
        if cik not in covered:
            report[cik] = "not covered"
            continue
        filings = by_cik.get(cik, [])
        if any(f.get("status") not in CLEAN_STATUSES for f in filings):
            report[cik] = "held: a new filing did not fetch cleanly"
            continue
        newest = max([state.last_filing_date, *(date.fromisoformat(f["filing_date"]) for f in filings)])
        known = set(state.accessions_on_last_date) if newest == state.last_filing_date else set()
        known |= {f["accession"] for f in filings if date.fromisoformat(f["filing_date"]) == newest}
        states[cik] = CompanyState(newest, tuple(sorted(known)))
        report[cik] = "advanced" if states[cik] != state else "unchanged"
    all_clean = all(report[cik] in ("advanced", "unchanged") for cik in manifest.companies)
    through = max(manifest.processed_through, run_date) if all_clean else manifest.processed_through
    return Manifest(through, states), report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build or advance data/manifest.json.")
    sub = parser.add_subparsers(dest="command", required=True)
    boot = sub.add_parser("bootstrap", help="build the manifest from the cached sidecars in data/raw/")
    boot.add_argument("--processed-through", required=True, metavar="YYYY-MM-DD")
    boot.add_argument("--company", action="append", metavar="TICKER", help="default: every company that has a review CSV")
    boot.add_argument("--manifest", type=Path, default=None, help="default data/manifest.json")
    adv = sub.add_parser("advance", help="move companies forward from a new_filings.json")
    adv.add_argument("--filings", type=Path, default=None, help="default data/_work/new_filings.json")
    adv.add_argument("--run-date", metavar="YYYY-MM-DD", help="default: the run_date recorded in the filings file")
    adv.add_argument("--manifest", type=Path, default=None, help="default data/manifest.json")
    args = parser.parse_args(argv)

    try:
        if args.command == "bootstrap":
            targets = companies(load_config(), args.company)
            ciks = [c.cik for c in targets if (REVIEW_DIR / f"{c.cik}.csv").exists()] if not args.company else [c.cik for c in targets]
            manifest = bootstrap(RAW_DIR, ciks, date.fromisoformat(args.processed_through))
            save_manifest(manifest, args.manifest)
            print(f"wrote {args.manifest or common.MANIFEST_PATH}: {len(manifest.companies)} companies, processed through {manifest.processed_through}")
            return 0
        payload = json.loads((args.filings or common.NEW_FILINGS_PATH).read_text(encoding="utf-8"))
        run_date = date.fromisoformat(args.run_date or payload["run_date"])
        after, report = advance(load_manifest(args.manifest), payload, run_date=run_date)
        save_manifest(after, args.manifest)
        for cik, what in sorted(report.items()):
            print(f"  {cik}: {what}")
        print(f"processed through {after.processed_through}")
        return 0
    except (ManifestError, FileNotFoundError, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
