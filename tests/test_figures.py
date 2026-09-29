"""pipeline/figures.py: the four static charts under card/figures/ (SCOPE.md section 5.5)."""

from __future__ import annotations

import importlib
from datetime import date, datetime, timezone

import pytest

from research_record.schema import ResearchRecord

figures = importlib.import_module("pipeline.figures")


def evidence(**overrides):
    e = {
        "source_url": "https://www.sec.gov/Archives/edgar/data/123/x.htm",
        "accession_number": "0000000123-24-000001", "filing_type": "8-K",
        "filed_at": date(2024, 2, 1), "fetched_at": datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
        "content_sha256": "a" * 64, "excerpt": "x" * 10,
    }
    e.update(overrides)
    return e


DEFAULT_ID = "01HZY8Q9XMR3T7VBN2CDEFGH0K"


def outcome(reported_value, **overrides):
    base = {"reported_value": reported_value, "reported_at": date(2025, 2, 3), "evidence": evidence(content_sha256="b" * 64)}
    base.update(overrides)
    return base


def rec(record_id=DEFAULT_ID, **overrides):
    base = dict(
        record_id=record_id, company="Example Corp", ticker="EXMP", cik="0000000123",
        claim="c", assumption={
            "text": "t", "metric": "revenue", "target_low": 5000.0, "target_high": 6000.0,
            "unit": "USD millions", "target_period": "FY2024", "stated_at": date(2024, 2, 1), "evidence": evidence(),
        },
        outcome=outcome(4800.0),
        invalidation_condition="x", status="missed", acknowledged_at=None, acknowledgement_evidence=None,
        days_to_falsifiable=368, days_to_acknowledged=None, last_reviewed_at=date(2026, 9, 20), reviewer="navneet",
    )
    base.update(overrides)
    return ResearchRecord(**base)


# --- fiscal_year -----------------------------------------------------------------


@pytest.mark.parametrize("period, year", [("FY2024", 2024), ("Q3 FY2024", 2024), ("Q3 2024", 2024), ("no year here", None)])
def test_fiscal_year(period, year) -> None:
    assert figures.fiscal_year(period) == year


# --- direction, nearest_edge, miss_magnitude -----------------------------------


def test_direction_is_none_without_an_outcome() -> None:
    assert figures.direction(rec(outcome=None, status="unresolved", days_to_falsifiable=None)) is None


def test_direction_better_and_worse() -> None:
    assert figures.direction(rec(outcome=outcome(6_500.0))) == "better"
    assert figures.direction(rec()) == "worse"


def test_nearest_edge_is_the_point_value_for_point_guidance() -> None:
    point = rec(assumption={**rec().assumption.model_dump(mode="json"), "target_low": 5.0, "target_high": 5.0})
    assert figures.nearest_edge(point) == 5.0


def test_nearest_edge_is_the_high_end_on_a_better_and_the_low_end_on_a_worse() -> None:
    assert figures.nearest_edge(rec(outcome=outcome(6_500.0))) == 6_000.0
    assert figures.nearest_edge(rec()) == 5_000.0


def test_miss_magnitude_is_none_unless_missed_with_an_outcome() -> None:
    assert figures.miss_magnitude(rec(outcome=None, status="unresolved", days_to_falsifiable=None)) is None
    met = rec(status="met", outcome=outcome(5_500.0))
    assert figures.miss_magnitude(met) is None


def test_miss_magnitude_of_a_worse_is_negative() -> None:
    # reported 4800 vs low end 5000: (4800 - 5000) / 5000 = -0.04
    assert figures.miss_magnitude(rec()) == pytest.approx(-0.04)


def test_miss_magnitude_of_a_better_is_positive() -> None:
    beat = rec(outcome=outcome(6_600.0))
    # (6600 - 6000) / 6000 = 0.1
    assert figures.miss_magnitude(beat) == pytest.approx(0.1)


# --- acknowledgement_bucket -----------------------------------------------------


def test_acknowledgement_bucket_is_none_unless_missed() -> None:
    met = rec(status="met", outcome=outcome(5_500.0))
    assert figures.acknowledgement_bucket(met) is None


def test_acknowledgement_bucket_is_the_same_filing_when_days_to_acknowledged_is_zero() -> None:
    r = rec(acknowledged_at="2025-02-03", acknowledgement_evidence=evidence(content_sha256="c" * 64), days_to_acknowledged=0)
    assert figures.acknowledgement_bucket(r) == "acknowledged in the same filing"


def test_acknowledgement_bucket_is_later_when_days_to_acknowledged_is_positive() -> None:
    r = rec(acknowledged_at="2025-02-10", acknowledgement_evidence=evidence(content_sha256="c" * 64), days_to_acknowledged=7)
    assert figures.acknowledgement_bucket(r) == "acknowledged later"


def test_acknowledgement_bucket_is_never_without_an_acknowledgement() -> None:
    assert figures.acknowledgement_bucket(rec()) == "never acknowledged"


def test_ack_categories_lists_all_three_in_display_order() -> None:
    assert figures.ACK_CATEGORIES == (
        "acknowledged in the same filing", "acknowledged later", "never acknowledged",
    )


# --- acknowledgement_summary, for the stat tile ---------------------------------


def test_acknowledgement_summary_counts_worse_and_better_separately() -> None:
    worse = rec("01HZY8Q9XMR3T7VBN2CDEFGH1K")  # default: reported 4800 vs [5000, 6000], a worse
    beat = rec("01HZY8Q9XMR3T7VBN2CDEFGH2K", outcome=outcome(6_500.0))
    summary = figures.acknowledgement_summary([worse, beat])
    assert (summary["worse"], summary["better"]) == (1, 1)


def test_acknowledgement_summary_counts_acknowledged_worse_and_better() -> None:
    ack_worse = rec("01HZY8Q9XMR3T7VBN2CDEFGH1K", acknowledged_at="2025-02-10",
                         acknowledgement_evidence=evidence(content_sha256="c" * 64), days_to_acknowledged=7)
    bare_worse = rec("01HZY8Q9XMR3T7VBN2CDEFGH2K")
    ack_better = rec("01HZY8Q9XMR3T7VBN2CDEFGH3K", outcome=outcome(6_500.0), acknowledged_at="2025-02-03",
                    acknowledgement_evidence=evidence(content_sha256="d" * 64), days_to_acknowledged=0)
    bare_better = rec("01HZY8Q9XMR3T7VBN2CDEFGH4K", outcome=outcome(6_600.0))
    summary = figures.acknowledgement_summary([ack_worse, bare_worse, ack_better, bare_better])
    assert (summary["worse"], summary["worse_acknowledged"]) == (2, 1)
    assert (summary["better"], summary["better_acknowledged"]) == (2, 1)


def test_acknowledgement_summary_names_only_companies_with_an_acknowledged_better() -> None:
    acknowledged_better = rec("01HZY8Q9XMR3T7VBN2CDEFGH1K", company="Acme Corp", outcome=outcome(6_500.0),
                             acknowledged_at="2025-02-03", acknowledgement_evidence=evidence(content_sha256="c" * 64), days_to_acknowledged=0)
    bare_better_other_company = rec("01HZY8Q9XMR3T7VBN2CDEFGH2K", company="Zeta Corp", outcome=outcome(6_600.0))
    acknowledged_worse_third_company = rec(  # acknowledged, but a worse, not a beat: must not be named
        "01HZY8Q9XMR3T7VBN2CDEFGH3K", company="Beta Corp", acknowledged_at="2025-02-10",
        acknowledgement_evidence=evidence(content_sha256="d" * 64), days_to_acknowledged=7,
    )
    summary = figures.acknowledgement_summary([acknowledged_better, bare_better_other_company, acknowledged_worse_third_company])
    assert summary["acknowledging_companies"] == ["Acme Corp"]


def test_acknowledgement_summary_on_no_missed_rows() -> None:
    met = rec(status="met", outcome=outcome(5_500.0))
    summary = figures.acknowledgement_summary([met])
    assert summary == {"worse": 0, "worse_acknowledged": 0, "better": 0, "better_acknowledged": 0, "acknowledging_companies": []}


# --- write_all: all four files exist and are non-empty, live data or none at all ----


def test_write_all_produces_all_four_files_with_no_records(tmp_path) -> None:
    paths = figures.write_all([], tmp_path / "figures", dataset="assumption-drift", generated_at=date(2026, 9, 27))
    assert len(paths) == len(figures.FIGURE_FILES) == 4
    for path in paths:
        assert path.exists() and path.stat().st_size > 0


def test_write_all_produces_all_four_files_with_records(tmp_path) -> None:
    records = [
        rec("01HZY8Q9XMR3T7VBN2CDEFGH1K"),  # missed worse, never acknowledged
        rec("01HZY8Q9XMR3T7VBN2CDEFGH2K", company="Beta Corp", status="met", outcome=outcome(5_500.0)),
        rec("01HZY8Q9XMR3T7VBN2CDEFGH3K", company="Gamma Corp", status="withdrawn", outcome=None, days_to_falsifiable=None),
        rec("01HZY8Q9XMR3T7VBN2CDEFGH4K", company="Beta Corp", outcome=outcome(6_500.0),
            acknowledged_at="2025-02-10", acknowledgement_evidence=evidence(content_sha256="d" * 64), days_to_acknowledged=7),
    ]
    out = tmp_path / "figures"
    paths = figures.write_all(records, out, dataset="assumption-drift", generated_at=date(2026, 9, 27))
    assert {p.name for p in paths} == set(figures.FIGURE_FILES)
    for filename in figures.FIGURE_FILES:
        path = out / filename
        assert path.exists() and path.stat().st_size > 0


def test_write_all_uses_no_red_amber_or_green() -> None:
    forbidden = {"red", "green", "amber", "orange", "yellow"}
    for name in ("GREY_DARK", "GREY_MID", "GREY_LIGHT", "ACCENT_MISSED"):
        value = getattr(figures, name).lower()
        assert not any(word in value for word in forbidden)
    # the accent is a blue-violet, not a warm colour: its red channel is not the dominant one
    r, g, b = (int(figures.ACCENT_MISSED[i : i + 2], 16) for i in (1, 3, 5))
    assert b >= r


def test_figures_module_source_has_no_em_dash() -> None:
    import pathlib

    source = pathlib.Path(figures.__file__).read_text(encoding="utf-8")
    assert "—" not in source
