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
    space/index.html                        the static Space (pipeline/space.py, SCOPE 5.6)
    card/README.md                          the dataset card (SCOPE 5.4), with live numbers from
                                             research_record.stats.compute()

Dry run is the default. `--dry-run` is accepted explicitly too, as a no-op: it is already what
happens without `--live`. `--live` additionally uploads `data/release/` and `card/` to
`hf://datasets/{hf.user}/assumption-drift`, and `space/` to `hf://spaces/{hf.user}/assumption-drift`, via `huggingface_hub`, reading `hf.user` from
`config.yaml`. Refused, before anything at all is written, if `hf.user` is still the `"{HF_USER}"`
placeholder: a live run that fails to upload is worse than one that never tried.

Reads   data/review/{cik}.csv
Writes  data/release/assumption_drift.parquet, .jsonl; card/README.md; card/figures/*.png
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError

from pipeline import figures, space
from pipeline.common import CARD_DIR, CONFIG_PATH, FIGURES_DIR, RELEASE_DIR, REPO_ROOT, REVIEW_DIR, Company, companies, load_config
from research_record import polarity, rubric, stats
from research_record.schema import ResearchRecord
from research_record.validate import unflatten_csv_row

review = importlib.import_module("pipeline.05_review")

DATASET_NAME = "assumption-drift"
HF_PLACEHOLDER = "{HF_USER}"
PARQUET_NAME = "assumption_drift.parquet"
JSONL_NAME = "assumption_drift.jsonl"
SPACE_DIR = REPO_ROOT / "space"
SPACE_NAME = "assumption-drift"


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


def _families(metrics: Any) -> list[str]:
    """Metric names without their GAAP / non-GAAP suffix, each once, in config order."""
    names = [re.sub(r" (?:GAAP|non-GAAP)$", "", m) for m in metrics]
    return list(dict.fromkeys(names))


def _join_names_plain(names: list[str]) -> str:
    return ", ".join(names[:-1]) + f" and {names[-1]}" if len(names) > 1 else "".join(names)


def _join_names(names: list[str]) -> str:
    if not names:
        return "no companies yet"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" and {names[-1]}"


def _frontmatter(dataset: str) -> str:
    return f"""\
---
license: cc-by-4.0
language:
- en
tags:
- finance
- sec-filings
- guidance
- provenance
size_categories:
- n<1K
pretty_name: {dataset}
---
"""


def render_card(
    result: dict[str, Any], *, dataset: str, generated_at: date, hf_user: str, filing_date_from: date, filing_date_to: date
) -> str:
    """The dataset card (SCOPE.md 5.4), sections in order: what this is, how a row is built, the
    resolution rubric, provenance guarantee, known limitations, how to cite, licence, behind a Hugging
    Face YAML frontmatter block. Every number in it comes from `result`
    (`research_record.stats.compute()`'s own dict): nothing here is typed by hand and drifts from the
    release. The human-review guarantee is stated once, under Provenance, and nowhere else."""
    by_status = result["by_status"]
    better = result["missed_by_direction"].get("better", 0)
    worse = result["missed_by_direction"].get("worse", 0)
    table = polarity.load()
    higher = _join_names_plain(_families(m for m, up in table.items() if up))
    lower = _join_names_plain(_families(m for m, up in table.items() if not up))
    companies_named = _join_names([row["company"] for row in result["company_table"]])
    filing_range = f"{filing_date_from:%B %Y} and {filing_date_to:%B %Y}"

    return _frontmatter(dataset) + f"""\
# {dataset}

## What this is

{companies_named} made numeric forward guidance statements in their own SEC filings, filed between
{filing_range}. This dataset pairs each one with what the company later reported for that same metric
and period, and records whether the company ever acknowledged the gap when the guidance was missed.
It is a problem statement in data form, not a model and not a demonstration of one.

## How a row is built

A row starts as a sentence or a table in an 8-K, 10-K or 10-Q filing that names a metric, a number or
range, and a period. A later filing reporting the same metric for the same period supplies the outcome,
when one exists. Every candidate row was drafted by a language model reading the filing text.

## The resolution rubric

A row resolves mechanically from its own numbers, once the period has closed and an outcome exists:

- **met**: the reported value falls inside the guided range, endpoints included (a single point value
  is met within half a percent of itself)
- **missed**: the reported value falls outside the guided range, on the good side of it (better than
  guided) or the bad side (worse than guided). Which side is good depends on the metric. Higher is better
  for {higher}. Lower is better for {lower}
- **withdrawn**: the company explicitly withdrew or suspended the guidance in a later filing, before
  the period closed
- **unresolved**: the period has not closed yet, or no later filing reports the metric

Acknowledged means a later 8-K, 10-K or 10-Q states the gap in so many words, naming which side of its
guidance the result fell on: for example "below", "short of", "did not meet", "higher than expected",
"exceeded" or "above the high end".

As of {generated_at.isoformat()}, the release holds **{result['rows']:,} rows**: {", ".join(f"{v:,} {k}" for k, v in by_status.items())}.
Of the misses, {better:,} were better than guided and {worse:,} worse than guided; median days from the guidance to the
filing that made it checkable was {result['median_days_to_falsifiable']}, and
{_pct(result['missed_never_acknowledged_share'])} of misses were never acknowledged in a later filing.

By company:

{_company_table_markdown(result["company_table"])}

![Missed rows: when the gap was acknowledged](figures/{figures.FIGURE_FILES[0]})
![Worse and better than guided, acknowledged or not](figures/{figures.FIGURE_FILES[1]})
![Resolution by fiscal year](figures/{figures.FIGURE_FILES[2]})
![Miss magnitude](figures/{figures.FIGURE_FILES[3]})

## Provenance guarantee

Every `source_url` in this dataset returned a 200 from `www.sec.gov` or `efts.sec.gov`; no row was
built from a URL that was not actually fetched. Every excerpt is the exact text taken from that
filing. Its provenance hash is the sha256 of the extracted text, not of the raw page: SEC serves a
per-request script tag inside the raw HTML that differs on every fetch of the same document, so only
the extracted text is stable enough to hash and check again later. Every row was independently
checked by a person against the cached filing before it was approved, and at least ten percent of the
approved rows for each company were separately re-found on EDGAR by hand, their URL compared to the
one the pipeline used.

## Known limitations

- This covers three companies. It is a pilot, not a survey of the market.
- The pipeline does not claim to capture every guidance statement a company ever made; it captures
  the ones its patterns and its model caught.
- A row reported better than guided is recorded as missed, the same as one reported worse, because the
  guided range was wrong either way. Which side it fell on is reported separately, not folded into the
  status. Whether a side is good or bad is fixed once per metric, the same for every company: operating
  expenses and the tax rate are treated as lower-is-better, everything else as higher-is-better.
- A one-sided floor ("at least X") resolves as met on any reported value above the floor. There is no
  ceiling to measure against, so a floor's better-than-guided rows are not measured; only values below
  it can be missed.
- The search for an acknowledgement covers 8-K, 10-K and 10-Q filing text only. A company that only
  addressed a miss on an earnings call, and never wrote it into a filing, is not found: that text is
  not in EDGAR.
- An outcome that would come from a multi-column table whose header could not be confidently matched
  to a period is held back as unresolved rather than guessed at.

## How to cite

If you use this dataset, please cite it as:

    Navneet ({generated_at.year}). {dataset}. https://huggingface.co/datasets/{hf_user}/{dataset}. Accessed {generated_at.isoformat()}.

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


def upload_space(hf_user: str, *, space_dir: Path = SPACE_DIR) -> None:
    from huggingface_hub import HfApi

    repo_id = f"{hf_user}/{SPACE_NAME}"
    api = HfApi()
    api.create_repo(repo_id=repo_id, repo_type="space", space_sdk="static", exist_ok=True)
    api.upload_folder(repo_id=repo_id, repo_type="space", folder_path=str(space_dir), path_in_repo=".")


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
    card_text = render_card(
        result, dataset=DATASET_NAME, generated_at=generated_at, hf_user=hf_user,
        filing_date_from=date.fromisoformat(config["edgar"]["date_from"]),
        filing_date_to=date.fromisoformat(config["edgar"]["date_to"]),
    )
    card_path.write_text(card_text, encoding="utf-8")
    print(f"  wrote {card_path}")

    index_path = space.write_space(
        records, result, figures.acknowledgement_summary(records),
        strip_plot=FIGURES_DIR / figures.FIGURE_FILES[0], card_text=card_text, hf_user=hf_user,
        generated_at=generated_at, space_dir=SPACE_DIR,
    )
    print(f"  wrote {index_path}")

    if args.live:
        upload_to_hub(hf_user, release_dir=RELEASE_DIR, card_dir=CARD_DIR)
        print(f"  uploaded to hf://datasets/{hf_user}/{DATASET_NAME}")
        upload_space(hf_user, space_dir=SPACE_DIR)
        print(f"  uploaded to hf://spaces/{hf_user}/{SPACE_NAME}")
    else:
        print("dry run: nothing was uploaded. Add --live (with hf.user set) to publish to Hugging Face.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
