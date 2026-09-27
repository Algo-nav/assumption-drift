"""pipeline/06_publish.py: the release (SCOPE.md section 5.3)."""

from __future__ import annotations

import csv as csv_module
import importlib
import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from pipeline import common
from research_record.schema import ResearchRecord

publish = importlib.import_module("pipeline.06_publish")
review = importlib.import_module("pipeline.05_review")

COMPANY = common.Company("Example Corp", "EXMP", "0000000123", fiscal_year_end_month=1)
OTHER = common.Company("Other Corp", "OTHR", "0000000456", fiscal_year_end_month=1)


def csv_row(record_data, **overrides) -> dict[str, str]:
    data = record_data(**{k: v for k, v in overrides.items() if k not in ("approved", "empty_block")})
    flat: dict[str, str] = {}

    def walk(value, prefix=""):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, f"{prefix}{k}.")
        else:
            flat[prefix[:-1]] = "" if value is None else str(value)

    walk(data)
    flat.update(approved="true", hand_verified="false", reviewer_note="", conflict="false", empty_block="false",
                aid_verify="", aid_verify_reason="", aid_verify_class="")
    flat["approved"] = overrides.get("approved", "true")
    flat["empty_block"] = overrides.get("empty_block", "false")
    return flat


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({k for row in rows for k in row})
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv_module.DictWriter(fh, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / "review").mkdir()
    monkeypatch.setattr(publish, "REVIEW_DIR", tmp_path / "review")
    return tmp_path


def seed(world, company, rows):
    write_csv(world / "review" / f"{company.cik}.csv", rows)


# --- approved_rows / build_records ----------------------------------------------


def test_approved_rows_keeps_only_approved_and_drops_empty_block(world, record_data) -> None:
    seed(world, COMPANY, [
        csv_row(record_data, approved="true"),
        csv_row(record_data, record_id="01HZY8Q9XMR3T7VBN2CDEFGH2K", approved="false"),
        csv_row(record_data, record_id="01HZY8Q9XMR3T7VBN2CDEFGH3K", approved="true", empty_block="true"),
    ])
    rows = publish.approved_rows(COMPANY, world / "review")
    assert [r["record_id"] for r in rows] == [record_data()["record_id"]]


def test_approved_rows_of_a_company_with_no_file_is_empty(world) -> None:
    assert publish.approved_rows(COMPANY, world / "review") == []


def test_build_records_parses_every_approved_row_across_companies(world, record_data) -> None:
    seed(world, COMPANY, [csv_row(record_data)])
    seed(world, OTHER, [csv_row(record_data, record_id="01HZY8Q9XMR3T7VBN2CDEFGH2K", company="Other Corp")])
    records, invalid = publish.build_records([COMPANY, OTHER], world / "review")
    assert {r.record_id for r in records} == {record_data()["record_id"], "01HZY8Q9XMR3T7VBN2CDEFGH2K"}
    assert invalid == 0


def test_resolve_status_recomputes_from_the_numbers_when_the_row_is_still_open(record_data) -> None:
    """A review-queue row is always status="open"; an approved row's release status is the rubric's
    own answer from its numbers instead."""
    record = ResearchRecord(**{**record_data(), "status": "open"})  # 4800 reported, [5000, 6000] guided
    resolved = publish.resolve_status(record, {})
    assert resolved.status == "missed"


def test_resolve_status_reads_withdrawn_off_the_withdrawal_note(record_data) -> None:
    record = ResearchRecord(**{**record_data(), "status": "open"})
    resolved = publish.resolve_status(record, {"aid_withdrawal_note": "filed 2024-03-01, before the period closed"})
    assert resolved.status == "withdrawn"


def test_resolve_status_leaves_a_row_unchanged_when_it_already_matches(record_data) -> None:
    record = ResearchRecord(**record_data())  # already "missed", matching its own numbers
    assert publish.resolve_status(record, {}) == record


def test_build_records_gives_approved_rows_their_real_status(world, record_data) -> None:
    row = csv_row(record_data, status="open")  # what a freshly-approved row actually looks like
    seed(world, COMPANY, [row])
    records, invalid = publish.build_records([COMPANY], world / "review")
    assert invalid == 0 and records[0].status == "missed"


def test_build_records_skips_an_invalid_row_and_counts_it(world, record_data, capsys) -> None:
    bad = csv_row(record_data)
    bad["status"] = "not-a-status"
    seed(world, COMPANY, [bad])
    records, invalid = publish.build_records([COMPANY], world / "review")
    assert records == [] and invalid == 1
    assert bad["record_id"] in capsys.readouterr().err


def test_build_records_is_sorted_by_company_then_stated_at_then_record_id(world, record_data) -> None:
    late = csv_row(record_data, record_id="01HZY8Q9XMR3T7VBN2CDEFGH2K", company="Beta Corp")
    late["assumption.stated_at"] = "2025-01-01"
    early = csv_row(record_data, record_id="01HZY8Q9XMR3T7VBN2CDEFGH3K", company="Alpha Corp")
    seed(world, COMPANY, [late, early])
    records, _ = publish.build_records([COMPANY], world / "review")
    assert [r.company for r in records] == ["Alpha Corp", "Beta Corp"]


# --- write_release: parquet and jsonl --------------------------------------------


def test_write_release_writes_both_files_with_the_right_row_count(tmp_path, record_data) -> None:
    records = [ResearchRecord(**record_data())]
    parquet_path, jsonl_path = publish.write_release(records, tmp_path / "release")
    assert parquet_path.name == "assumption_drift.parquet" and parquet_path.exists()
    assert jsonl_path.name == "assumption_drift.jsonl"
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["record_id"] == record_data()["record_id"]

    import pyarrow.parquet as pq

    table = pq.read_table(parquet_path)
    assert table.num_rows == 1
    assert "record_id" in table.column_names and "assumption.target_low" in table.column_names


def test_write_release_with_no_records_still_writes_both_files(tmp_path) -> None:
    parquet_path, jsonl_path = publish.write_release([], tmp_path / "release")
    assert parquet_path.exists() and jsonl_path.exists()
    assert jsonl_path.read_text(encoding="utf-8") == ""

    import pyarrow.parquet as pq

    assert pq.read_table(parquet_path).num_rows == 0


def test_to_flat_row_has_every_column_even_when_outcome_is_absent(record_data) -> None:
    record = ResearchRecord(**{**record_data(outcome=None, status="unresolved", days_to_falsifiable=None)})
    row = publish.to_flat_row(record, review.schema_columns())
    assert row["outcome.reported_value"] is None
    assert row["assumption.metric"] == "revenue"


# --- render_card -------------------------------------------------------------------


@pytest.fixture
def sample_result(record_data):
    from research_record import stats

    return stats.compute([ResearchRecord(**record_data())])


def test_render_card_has_every_section_in_order(sample_result) -> None:
    text = publish.render_card(sample_result, dataset="assumption-drift", generated_at=date(2026, 9, 27))
    sections = ["## What this is", "## How a row is built", "## The resolution rubric", "## Provenance guarantee",
                "## Known limitations", "## How to cite", "## Licence"]
    positions = [text.index(s) for s in sections]
    assert positions == sorted(positions)


def test_render_card_states_every_limitation_plainly(sample_result) -> None:
    text = publish.render_card(sample_result, dataset="assumption-drift", generated_at=date(2026, 9, 27))
    limitations = text[text.index("## Known limitations") : text.index("## How to cite")]
    for phrase in [
        "three companies", "pilot",
        "does not claim to capture every guidance statement",
        "recorded as missed",
        "one-sided floor", "not measured",
        "8-K, 10-K and 10-Q", "earnings call",
        "multi-column table", "unresolved",
        "human-reviewed", "ten percent", "hand-verified",
    ]:
        assert phrase in limitations, f"missing: {phrase!r}"


def test_render_card_has_no_em_dash_and_no_buy_side_and_no_product_mention(sample_result) -> None:
    text = publish.render_card(sample_result, dataset="assumption-drift", generated_at=date(2026, 9, 27))
    assert "—" not in text
    assert "buy-side" not in text.lower()
    for banned in ("anthropic", "claude", "scnd order"):
        assert banned not in text.lower()


def test_render_card_embeds_live_numbers_from_stats(sample_result) -> None:
    text = publish.render_card(sample_result, dataset="assumption-drift", generated_at=date(2026, 9, 27))
    assert f"{sample_result['rows']:,} rows" in text
    assert "figures/falsifiable_vs_acknowledged.png" in text


def test_render_card_links_all_four_figures(sample_result) -> None:
    from pipeline import figures

    text = publish.render_card(sample_result, dataset="assumption-drift", generated_at=date(2026, 9, 27))
    for name in figures.FIGURE_FILES:
        assert f"figures/{name}" in text


# --- main, end to end ------------------------------------------------------------


@pytest.fixture
def config_path(world):
    cfg = common.load_config()
    cfg["companies"] = [{"name": COMPANY.name, "ticker": COMPANY.ticker, "cik": COMPANY.cik, "fiscal_year_end_month": 1}]
    path = world / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


@pytest.fixture(autouse=True)
def isolated_dirs(world, monkeypatch):
    monkeypatch.setattr(publish, "RELEASE_DIR", world / "release")
    monkeypatch.setattr(publish, "CARD_DIR", world / "card")
    monkeypatch.setattr(publish, "FIGURES_DIR", world / "card" / "figures")


def test_main_dry_run_writes_everything_but_does_not_upload(world, config_path, record_data, monkeypatch) -> None:
    seed(world, COMPANY, [csv_row(record_data)])
    called = []
    monkeypatch.setattr(publish, "upload_to_hub", lambda *a, **k: called.append((a, k)))
    assert publish.main(["--config", str(config_path), "--dry-run"]) == 0
    assert (world / "release" / "assumption_drift.parquet").exists()
    assert (world / "release" / "assumption_drift.jsonl").exists()
    assert (world / "card" / "README.md").exists()
    for name in importlib.import_module("pipeline.figures").FIGURE_FILES:
        assert (world / "card" / "figures" / name).exists()
    assert called == []


def test_main_without_any_flag_also_does_not_upload(world, config_path, record_data, monkeypatch) -> None:
    seed(world, COMPANY, [csv_row(record_data)])
    called = []
    monkeypatch.setattr(publish, "upload_to_hub", lambda *a, **k: called.append((a, k)))
    assert publish.main(["--config", str(config_path)]) == 0
    assert called == []


def test_main_live_refuses_when_hf_user_is_still_the_placeholder(world, config_path, record_data, monkeypatch, capsys) -> None:
    seed(world, COMPANY, [csv_row(record_data)])
    monkeypatch.setattr(publish, "upload_to_hub", lambda *a, **k: pytest.fail("must not upload"))
    assert publish.main(["--config", str(config_path), "--live"]) == 2
    assert "placeholder" in capsys.readouterr().err
    assert not (world / "release").exists()  # refused before writing anything


def test_main_live_uploads_when_hf_user_is_set(world, config_path, record_data, monkeypatch) -> None:
    seed(world, COMPANY, [csv_row(record_data)])
    cfg = yaml.safe_load(config_path.read_text())
    cfg["hf"]["user"] = "navneet"
    config_path.write_text(yaml.safe_dump(cfg))
    called = []
    monkeypatch.setattr(publish, "upload_to_hub", lambda *a, **k: called.append((a, k)))
    assert publish.main(["--config", str(config_path), "--live"]) == 0
    assert len(called) == 1 and called[0][0] == ("navneet",)


def test_main_prints_how_many_rows_and_how_many_were_invalid(world, config_path, record_data, monkeypatch, capsys) -> None:
    bad = csv_row(record_data)
    bad["status"] = "not-a-status"
    seed(world, COMPANY, [bad])
    monkeypatch.setattr(publish, "upload_to_hub", lambda *a, **k: None)
    assert publish.main(["--config", str(config_path)]) == 0
    out = capsys.readouterr().out
    assert "0 approved row(s)" in out and "1 left out as invalid" in out
