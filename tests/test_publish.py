"""pipeline/06_publish.py: the release (SCOPE.md section 5.3)."""

from __future__ import annotations

import re

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


def render(result, **overrides):
    kwargs = dict(dataset="assumption-drift", generated_at=date(2026, 9, 27), hf_user="Nav772",
                  filing_date_from=date(2019, 1, 1), filing_date_to=date(2026, 9, 20))
    kwargs.update(overrides)
    return publish.render_card(result, **kwargs)


def test_render_card_has_every_section_in_order(sample_result) -> None:
    text = render(sample_result)
    sections = ["## What this is", "## How a row is built", "## The resolution rubric", "## Provenance guarantee",
                "## Known limitations", "## How to cite", "## Licence"]
    positions = [text.index(s) for s in sections]
    assert positions == sorted(positions)


def test_render_card_starts_with_hugging_face_yaml_frontmatter(sample_result) -> None:
    text = render(sample_result)
    assert text.startswith("---\n")
    frontmatter = text[4 : text.index("\n---\n", 4)]
    assert "license: cc-by-4.0" in frontmatter
    assert "- en" in frontmatter
    for tag in ("finance", "sec-filings", "guidance", "provenance"):
        assert f"- {tag}" in frontmatter
    assert "- n<1K" in frontmatter
    assert "pretty_name: assumption-drift" in frontmatter


def test_render_card_names_the_companies_and_filing_range_in_the_first_paragraph(sample_result) -> None:
    text = render(sample_result, filing_date_from=date(2019, 1, 1), filing_date_to=date(2026, 9, 20))
    first_paragraph = text[text.index("## What this is") : text.index("## How a row is built")]
    assert sample_result["company_table"][0]["company"] in first_paragraph
    assert "January 2019" in first_paragraph and "September 2026" in first_paragraph


def test_render_card_defines_acknowledged_under_the_rubric(sample_result) -> None:
    text = render(sample_result)
    rubric_section = text[text.index("## The resolution rubric") : text.index("As of")]
    assert "Acknowledged means" in rubric_section
    flat = " ".join(rubric_section.split())
    assert "better than guided" in flat and "worse than guided" in flat
    assert "Lower is better for operating expenses, tax rate, total expenses and capital expenditures" in flat
    assert not re.search(r"\bbeats?\b|shortfalls?", text, re.IGNORECASE)


def test_render_card_states_every_limitation_plainly(sample_result) -> None:
    text = render(sample_result)
    limitations = " ".join(text[text.index("## Known limitations") : text.index("## How to cite")].split())
    for phrase in [
        "three companies", "pilot",
        "does not claim to capture every guidance statement",
        "recorded as missed", "once per metric",
        "one-sided floor", "not measured",
        "8-K, 10-K and 10-Q", "earnings call",
        "multi-column table", "unresolved",
    ]:
        assert phrase in limitations, f"missing: {phrase!r}"


def test_render_card_states_the_human_review_guarantee_once_in_provenance_only(sample_result) -> None:
    text = render(sample_result)
    how_built = text[text.index("## How a row is built") : text.index("## The resolution rubric")]
    provenance = text[text.index("## Provenance guarantee") : text.index("## Known limitations")]
    limitations = text[text.index("## Known limitations") : text.index("## How to cite")]
    assert "checked by a person" in provenance and "ten percent" in provenance and "hand" in provenance
    assert "checked by a person" not in how_built and "hand-verified" not in how_built.lower()
    assert "human-reviewed" not in limitations.lower() and "hand-verified" not in limitations.lower()


def test_render_card_has_no_em_dash_and_no_buy_side_and_no_product_mention(sample_result) -> None:
    text = render(sample_result)
    assert "—" not in text
    assert "buy-side" not in text.lower()
    for banned in ("anthropic", "claude", "scnd order"):
        assert banned not in text.lower()


def test_render_card_embeds_live_numbers_from_stats(sample_result) -> None:
    text = render(sample_result)
    assert f"{sample_result['rows']:,} rows" in text
    assert "figures/falsifiable_vs_acknowledged.png" in text


def test_render_card_says_the_hash_is_of_the_extracted_text(sample_result) -> None:
    text = render(sample_result)
    assert "sha256 of the extracted text" in text


def test_render_card_links_all_four_figures(sample_result) -> None:
    from pipeline import figures

    text = render(sample_result)
    for name in figures.FIGURE_FILES:
        assert f"figures/{name}" in text


def test_render_card_cite_block_has_the_right_shape(sample_result) -> None:
    text = render(sample_result, hf_user="Nav772", generated_at=date(2026, 9, 27))
    assert "Navneet (2026). assumption-drift. https://huggingface.co/datasets/Nav772/assumption-drift. Accessed 2026-09-27." in text


# --- main, end to end ------------------------------------------------------------


@pytest.fixture
def config_path(world):
    cfg = common.load_config()
    cfg["llm"]["sync_below"] = 0  # these tests are about the batch path; the synchronous rule has its own tests in test_llm.py
    cfg["companies"] = [{"name": COMPANY.name, "ticker": COMPANY.ticker, "cik": COMPANY.cik, "fiscal_year_end_month": 1}]
    path = world / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


space_calls: list = []


@pytest.fixture(autouse=True)
def isolated_dirs(world, monkeypatch):
    space_calls.clear()
    monkeypatch.setattr(publish, "RELEASE_DIR", world / "release")
    monkeypatch.setattr(publish, "CARD_DIR", world / "card")
    monkeypatch.setattr(publish, "FIGURES_DIR", world / "card" / "figures")
    monkeypatch.setattr(publish, "SPACE_DIR", world / "space")
    monkeypatch.setattr(publish, "upload_space", lambda *a, **k: space_calls.append((a, k)))


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
    cfg = yaml.safe_load(config_path.read_text())
    cfg["hf"]["user"] = "{HF_USER}"
    config_path.write_text(yaml.safe_dump(cfg))
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
    assert len(space_calls) == 1 and space_calls[0][0] == ("navneet",)
    assert (world / "space" / "index.html").exists()


def test_main_prints_how_many_rows_and_how_many_were_invalid(world, config_path, record_data, monkeypatch, capsys) -> None:
    bad = csv_row(record_data)
    bad["status"] = "not-a-status"
    seed(world, COMPANY, [bad])
    monkeypatch.setattr(publish, "upload_to_hub", lambda *a, **k: None)
    assert publish.main(["--config", str(config_path)]) == 0
    out = capsys.readouterr().out
    assert "0 approved row(s)" in out and "1 left out as invalid" in out


# --- publish: false keeps a company out of everything published, and the card says why ----------------------


def test_the_card_names_unpublished_companies_in_one_sentence(sample_result) -> None:
    text = render(sample_result, unpublished=["Apple Inc.", "Alphabet Inc.", "JPMorgan Chase & Co."])
    sentence = "Apple Inc., Alphabet Inc. and JPMorgan Chase & Co. were in the company list but issued no numeric guidance the metric list covers, so they have no rows here."
    assert text.count(sentence) == 1
    assert "—" not in sentence  # the card's register: no em-dashes
    assert "issued no numeric guidance" not in render(sample_result)  # nothing is said when nobody is left out
    assert "Costco Wholesale Corporation was in the company list but issued no numeric guidance the metric list covers, so it has no rows here." in render(sample_result, unpublished=["Costco Wholesale Corporation"])


def test_config_reads_publish_per_company_and_defaults_it_on() -> None:
    base = {"name": "A", "ticker": "a", "cik": "1"}
    assert common._company(base).publish is True and common._company({**base, "publish": False}).publish is False
    with pytest.raises(ValueError, match="publish"):
        common._company({**base, "publish": "no"})
    listed = {c.ticker: c.publish for c in common.companies(common.load_config())}
    assert [t for t, p in listed.items() if not p] == ["AAPL", "GOOGL", "COST", "JPM", "MU"]


def test_an_unpublished_company_is_in_no_release_file_figure_space_or_card_table(world, config_path, record_data, monkeypatch) -> None:
    seed(world, COMPANY, [csv_row(record_data)])
    seed(world, OTHER, [csv_row(record_data, record_id="01ARZ3NDEKTSV4RRFFQ69G5FAW", company="Other Corp", ticker="OTHR", cik=OTHER.cik)])
    cfg = yaml.safe_load(config_path.read_text())
    cfg["companies"].append({"name": OTHER.name, "ticker": OTHER.ticker, "cik": OTHER.cik, "fiscal_year_end_month": 1, "publish": False})
    config_path.write_text(yaml.safe_dump(cfg))
    assert publish.main(["--config", str(config_path)]) == 0
    release = (world / "release" / "assumption_drift.jsonl").read_text()
    assert COMPANY.name in release and "Other Corp" not in release
    card = (world / "card" / "README.md").read_text()
    assert "Other Corp was in the company list but issued no numeric guidance" in card
    assert "Other Corp" not in (world / "space" / "index.html").read_text().replace("Other Corp was in the company list", "")
    # --company cannot bring it back
    assert publish.main(["--config", str(config_path), "--company", "OTHR"]) == 0
    assert "Other Corp" not in (world / "release" / "assumption_drift.jsonl").read_text()
