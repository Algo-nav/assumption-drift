"""Phase 3, step 6: the release (SCOPE.md section 5.3).

    python -m pipeline.06_publish [--config PATH] [--company TICKER ...] [--dry-run] [--live]

Reads every targeted company's `data/review/{cik}.csv`, keeps only `approved=true` rows (a row with
`empty_block=true` is a flag, never a record, and is never read), and parses each one as a
`ResearchRecord` (`research_record.validate.unflatten_csv_row`, the same CSV-to-record shape
`rr validate` uses). A row that fails to parse is skipped and counted, not raised: one bad row does
not block the release, and it is reported by record_id.

A review row's own `status` is always `"open"`; that is what the column means there ("not yet
reviewed"), and reviewing it never changes it. An approved row has been reviewed, so its release
status is instead computed fresh from its numbers (`research_record.rubric.resolve_record`, with
`reviewed=True` and `withdrawn` read off whether `aid_withdrawal_note` is set): `rr validate`'s status
check is exactly this same computation, run again from the other side.

Writes, every run, live or dry:

    data/release/assumption_drift.parquet   one row per record, flat columns (the schema's own dotted names)
    data/release/assumption_drift.jsonl     one ResearchRecord per line, nested
    card/figures/*.png                      the four charts (pipeline/figures.py, SCOPE 5.5)
    card/README.md                          the dataset card (SCOPE 5.4), with live numbers from
                                             research_record.stats.compute()

Dry run is the default. `--dry-run` is accepted explicitly too, as a no-op: it is already what
happens without `--live`. `--live` additionally uploads `data/release/` and `card/` to
`hf://datasets/{hf.user}/assumption-drift` via `huggingface_hub`, reading `hf.user` from
`config.yaml`. Refused, before anything at all is written, if `hf.user` is still the `"{HF_USER}"`
placeholder: a live run that fails to upload is worse than one that never tried.

Reads   data/review/{cik}.csv
Writes  data/release/assumption_drift.parquet, .jsonl; card/README.md; card/figures/*.png
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError

from pipeline import figures
from pipeline.common import CARD_DIR, CONFIG_PATH, FIGURES_DIR, RELEASE_DIR, REVIEW_DIR, Company, companies, load_config
from research_record import rubric, stats
from research_record.schema import ResearchRecord
from research_record.validate import unflatten_csv_row

review = importlib.import_module("pipeline.05_review")

DATASET_NAME = "assumption-drift"
HF_PLACEHOLDER = "{HF_USER}"
PARQUET_NAME = "assumption_drift.parquet"
JSONL_NAME = "assumption_drift.jsonl"


# --- gathering approved rows -----------------------------------------------------


def approved_rows(company: Company, review_dir: Path = REVIEW_DIR) -> list[dict[str, str]]:
    """A company's own review CSV, kept to the rows that may ever be published: approved, and not a
    flag row (`empty_block=true` is not a record at all)."""
    path = review_dir / f"{company.cik}.csv"
    if not path.exists():
        return []
    return [r for r in review.read_csv(path) if r.get("approved") == "true" and r.get("empty_block") != "true"]


def resolve_status(record: ResearchRecord, row: dict[str, str]) -> ResearchRecord:
    """A review-queue row's `status` is always "open": that column means "not yet reviewed", and
    reviewing never changes it (pipeline/05_review.py's own refresh deliberately leaves it alone). An
    approved row IS reviewed, so its release status is instead the rubric's own answer from its
    numbers, with `reviewed=True` and `withdrawn` read off `aid_withdrawal_note` (non-empty only when
    04_outcomes found a withdrawal dated before the period closed)."""
    withdrawn = bool(row.get("aid_withdrawal_note", "").strip())
    resolved = rubric.resolve_record(record, reviewed=True, withdrawn=withdrawn)
    return record if resolved == record.status else record.model_copy(update={"status": resolved})


def build_records(targets: list[Company], review_dir: Path = REVIEW_DIR) -> tuple[list[ResearchRecord], int]:
    """(records, invalid) across every targeted company's approved rows, sorted by company, then
    stated_at, then record_id, for a stable, readable release file."""
    records: list[ResearchRecord] = []
    invalid = 0
    for company in targets:
        for row in approved_rows(company, review_dir):
            try:
                record = ResearchRecord.model_validate(unflatten_csv_row(row))
                records.append(resolve_status(record, row))
            except (ValidationError, KeyError, ValueError) as exc:
                invalid += 1
                print(f"  {row.get('record_id', '?')}: not a valid record, left out of the release: {str(exc)[:160]}", file=sys.stderr)
    records.sort(key=lambda r: (r.company, r.assumption.stated_at, r.record_id))
    return records, invalid


# --- writing the release files ---------------------------------------------------


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, inner in value.items():
            out.update(_flatten(inner, f"{prefix}{key}."))
        return out
    return {prefix[:-1]: value}


def to_flat_row(record: ResearchRecord, columns: list[str]) -> dict[str, Any]:
    """`record` as one flat dict, in the schema's own dotted-column shape: every column present, even
    one an absent outcome or acknowledgement leaves null."""
    row: dict[str, Any] = dict.fromkeys(columns)
    row.update({c: v for c, v in _flatten(record.model_dump(mode="json")).items() if c in row})
    return row


def write_release(records: list[ResearchRecord], release_dir: Path = RELEASE_DIR) -> tuple[Path, Path]:
    """(parquet path, jsonl path). The parquet is flat (`to_flat_row`); the jsonl is one nested
    `ResearchRecord` per line, the same shape `rr stats` and `rr validate` already read."""
    release_dir.mkdir(parents=True, exist_ok=True)
    columns = review.schema_columns()
    table = (
        pa.Table.from_pylist([to_flat_row(r, columns) for r in records])
        if records
        else pa.table({c: pa.array([], type=pa.string()) for c in columns})
    )
    parquet_path = release_dir / PARQUET_NAME
    pq.write_table(table, parquet_path)

    jsonl_path = release_dir / JSONL_NAME
    jsonl_path.write_text("".join(json.dumps(r.model_dump(mode="json")) + "\n" for r in records), encoding="utf-8")
    return parquet_path, jsonl_path


# --- the dataset card -------------------------------------------------------------


def _pct(value: float | None) -> str:
    return f"{value:.0%}" if value is not None else "n/a"


def _company_table_markdown(company_table: list[dict[str, Any]]) -> str:
    if not company_table:
        return "_No rows yet._"
    columns = ["met", "missed", "withdrawn", "unresolved"]
    header = ["company", "rows", *columns, "never-acknowledged share"]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    for row in company_table:
        cells = [row["company"], str(row["rows"])]
        cells += [str(row["by_status"].get(c, 0)) for c in columns]
        cells.append(_pct(row["missed_never_acknowledged_share"]))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_card(result: dict[str, Any], *, dataset: str, generated_at: date) -> str:
    """The dataset card (SCOPE.md 5.4), sections in order: what this is, how a row is built, the
    resolution rubric, provenance guarantee, known limitations, how to cite, licence. Every number in
    it comes from `result` (`research_record.stats.compute()`'s own dict): nothing here is typed by
    hand and drifts from the release."""
    by_status = result["by_status"]
    beat = result["missed_by_direction"].get("beat", 0)
    shortfall = result["missed_by_direction"].get("shortfall", 0)

    return f"""\
# {dataset}

## What this is

This dataset pairs numeric forward guidance statements from public company SEC filings with what the
company later reported for that same metric and period. Each row also records whether the company
ever acknowledged the gap, if the guidance was missed. It is a problem statement in data form, not a
model and not a demonstration of one.

## How a row is built

A row starts as a sentence or a table in an 8-K, 10-K or 10-Q filing that names a metric, a number or
range, and a period. A later filing reporting the same metric for the same period supplies the outcome,
when one exists. Every candidate row was drafted by a language model reading the filing text, then
checked by a person against the cached filing before it was approved; nothing below is published
without that check.

## The resolution rubric

A row resolves mechanically from its own numbers, once the period has closed and an outcome exists:

- **met**: the reported value falls inside the guided range, endpoints included (a single point value
  is met within half a percent of itself)
- **missed**: the reported value falls outside the guided range, either above it (a beat: the company
  did better than it said) or below it (a shortfall: the company did worse)
- **withdrawn**: the company explicitly withdrew or suspended the guidance in a later filing, before
  the period closed
- **unresolved**: the period has not closed yet, or no later filing reports the metric

As of {generated_at.isoformat()}, the release holds **{result['rows']:,} rows**: {", ".join(f"{v:,} {k}" for k, v in by_status.items())}.
Of the misses, {beat:,} were a beat and {shortfall:,} a shortfall; median days from the guidance to the
filing that made it checkable was {result['median_days_to_falsifiable']}, and
{_pct(result['missed_never_acknowledged_share'])} of misses were never acknowledged in a later filing.

By company:

{_company_table_markdown(result["company_table"])}

![Missed rows: falsifiable vs acknowledged](figures/{figures.FIGURE_FILES[0]})
![Acknowledgement of shortfalls by company](figures/{figures.FIGURE_FILES[1]})
![Resolution by fiscal year](figures/{figures.FIGURE_FILES[2]})
![Miss magnitude](figures/{figures.FIGURE_FILES[3]})

## Provenance guarantee

Every `source_url` in this dataset returned a 200 from `www.sec.gov` or `efts.sec.gov`; no row was
built from a URL that was not actually fetched. Every excerpt is the exact text taken from that
filing, hashed at fetch time, and the hash is checked again before publication. Every row was
independently checked by a person against the cached filing, and at least ten percent of the approved
rows for each company were separately re-found on EDGAR by hand and their URL compared to the one the
pipeline used.

## Known limitations

- This covers three companies. It is a pilot, not a survey of the market.
- The pipeline does not claim to capture every guidance statement a company ever made; it captures
  the ones its patterns and its model caught.
- A beat is recorded as missed, the same as a shortfall, because the underlying assumption (the
  guided range) was wrong either way. Which direction it missed in is reported separately, not folded
  into the status.
- A one-sided floor ("at least X") resolves as met on any reported value above the floor. There is no
  ceiling to beat against, so a floor's beats are not measured; only shortfalls are.
- The search for an acknowledgement covers 8-K, 10-K and 10-Q filing text only. A company that only
  addressed a miss on an earnings call, and never wrote it into a filing, is not found: that text is
  not in EDGAR.
- An outcome that would come from a multi-column table whose header could not be confidently matched
  to a period is held back as unresolved rather than guessed at.
- Every row here was human-reviewed against the filing it cites, and at least ten percent of the
  approved rows per company were hand-verified on EDGAR by comparing the pipeline's URL to one
  independently found.

## How to cite

If you use this dataset, please cite it by name and the date it was accessed:

    {dataset}, accessed {generated_at.isoformat()}.

## Licence

The dataset is released under CC-BY-4.0. The code that built it is released separately under MIT.
"""


# --- upload, only with --live -----------------------------------------------------


def upload_to_hub(hf_user: str, *, release_dir: Path = RELEASE_DIR, card_dir: Path = CARD_DIR) -> None:
    from huggingface_hub import HfApi

    repo_id = f"{hf_user}/{DATASET_NAME}"
    api = HfApi()
    api.create_repo(repo_id=repo_id, repo_type="dataset", exist_ok=True)
    api.upload_folder(repo_id=repo_id, repo_type="dataset", folder_path=str(release_dir), path_in_repo="data")
    api.upload_folder(repo_id=repo_id, repo_type="dataset", folder_path=str(card_dir), path_in_repo=".")


# --- running -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the release: parquet, jsonl, figures, and the dataset card.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--company", action="append", metavar="TICKER")
    parser.add_argument("--dry-run", action="store_true", help="explicit, but this is already the default without --live")
    parser.add_argument("--live", action="store_true", help="also upload to hf://datasets/{hf.user}/assumption-drift")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    hf_user = config.get("hf", {}).get("user", HF_PLACEHOLDER)
    if args.live and hf_user == HF_PLACEHOLDER:
        print(f"STOPPED: hf.user in config.yaml is still the {HF_PLACEHOLDER!r} placeholder. Set it before running --live. "
              "Nothing was written.", file=sys.stderr)
        return 2

    targets = companies(config, args.company)
    records, invalid = build_records(targets, REVIEW_DIR)
    print(f"06_publish: {len(records):,} approved row(s) across {len(targets)} company(ies), {invalid:,} left out as invalid")

    parquet_path, jsonl_path = write_release(records, RELEASE_DIR)
    print(f"  wrote {parquet_path} and {jsonl_path}")

    generated_at = date.today()
    figure_paths = figures.write_all(records, FIGURES_DIR, dataset=DATASET_NAME, generated_at=generated_at)
    print(f"  wrote {len(figure_paths)} figure(s) under {FIGURES_DIR}")

    result = stats.compute(records)
    CARD_DIR.mkdir(parents=True, exist_ok=True)
    card_path = CARD_DIR / "README.md"
    card_path.write_text(render_card(result, dataset=DATASET_NAME, generated_at=generated_at), encoding="utf-8")
    print(f"  wrote {card_path}")

    if args.live:
        upload_to_hub(hf_user, release_dir=RELEASE_DIR, card_dir=CARD_DIR)
        print(f"  uploaded to hf://datasets/{hf_user}/{DATASET_NAME}")
    else:
        print("dry run: nothing was uploaded. Add --live (with hf.user set) to publish to Hugging Face.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
