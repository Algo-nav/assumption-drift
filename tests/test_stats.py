"""research_record/stats.py: `rr stats`, and the beat/worse direction split (SCOPE.md 5.2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_record import stats
from research_record.schema import ResearchRecord


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, default=str) + "\n" for r in records), encoding="utf-8")


# --- load_records ------------------------------------------------------------


def test_load_records_parses_a_jsonl_file(tmp_path, record_data) -> None:
    path = tmp_path / "r.jsonl"
    write_jsonl(path, [record_data()])
    records, invalid = stats.load_records(path)
    assert len(records) == 1 and isinstance(records[0], ResearchRecord) and invalid == 0


def test_load_records_skips_blank_lines(tmp_path, record_data) -> None:
    path = tmp_path / "r.jsonl"
    path.write_text(json.dumps(record_data(), default=str) + "\n\n\n", encoding="utf-8")
    records, invalid = stats.load_records(path)
    assert len(records) == 1 and invalid == 0


def test_load_records_counts_invalid_lines_without_raising(tmp_path, record_data) -> None:
    path = tmp_path / "r.jsonl"
    bad_json = json.dumps(record_data(), default=str)[:-1]  # truncated: not valid JSON
    bad_record = json.dumps({**record_data(), "status": "not-a-status"}, default=str)  # valid JSON, invalid record
    path.write_text(bad_json + "\n" + bad_record + "\n" + json.dumps(record_data(), default=str) + "\n", encoding="utf-8")
    records, invalid = stats.load_records(path)
    assert len(records) == 1 and invalid == 2


# --- missed_direction ---------------------------------------------------------


def test_missed_direction_reads_better_and_worse_from_the_numbers(record_data) -> None:
    beat = ResearchRecord(**{**record_data(status="missed"), "outcome": {**record_data()["outcome"], "reported_value": 6_500.0}})
    worse = ResearchRecord(**record_data(status="missed"))  # fixture default: reported 4800, range [5000, 6000]
    assert stats.missed_direction(beat) == "better"
    assert stats.missed_direction(worse) == "worse"


def test_missed_direction_is_none_without_an_outcome(record_data) -> None:
    record = ResearchRecord(**record_data(outcome=None, status="unresolved", days_to_falsifiable=None))
    assert stats.missed_direction(record) is None


# --- compute -------------------------------------------------------------------


def _record(record_data, **overrides):
    payload = record_data(**overrides)
    return ResearchRecord(**payload)


def test_compute_counts_rows_and_status(record_data) -> None:
    met = _record(record_data, status="met", outcome={**record_data()["outcome"], "reported_value": 5_500.0})
    missed = _record(record_data)  # status "missed" by default
    result = stats.compute([met, missed])
    assert result["rows"] == 2 and result["by_status"] == {"met": 1, "missed": 1}


def test_compute_splits_missed_rows_by_direction(record_data) -> None:
    beat = _record(record_data, status="missed", outcome={**record_data()["outcome"], "reported_value": 6_500.0})
    worse = _record(record_data)  # reported 4800, below the 5000 low end
    result = stats.compute([beat, worse])
    assert result["missed_by_direction"] == {"better": 1, "worse": 1}


def test_compute_never_acknowledged_share(record_data) -> None:
    acknowledged = _record(
        record_data,
        acknowledged_at="2025-02-04",
        acknowledgement_evidence=record_data()["outcome"]["evidence"],
        days_to_acknowledged=1,
    )
    unacknowledged = _record(record_data)
    result = stats.compute([acknowledged, unacknowledged])
    assert result["missed"] == 2 and result["missed_never_acknowledged"] == 1
    assert result["missed_never_acknowledged_share"] == pytest.approx(0.5)


def test_compute_median_days_to_falsifiable_ignores_unresolved_rows(record_data) -> None:
    a = _record(record_data, days_to_falsifiable=100)
    b = _record(record_data, days_to_falsifiable=300)
    unresolved = _record(record_data, outcome=None, status="unresolved", days_to_falsifiable=None)
    result = stats.compute([a, b, unresolved])
    assert result["median_days_to_falsifiable"] == 200


def test_compute_median_days_to_falsifiable_is_none_with_nothing_resolved(record_data) -> None:
    unresolved = _record(record_data, outcome=None, status="unresolved", days_to_falsifiable=None)
    assert stats.compute([unresolved])["median_days_to_falsifiable"] is None


def test_compute_per_company_breakdown(record_data) -> None:
    a = _record(record_data, company="Alpha Corp")
    b = _record(record_data, company="Beta Corp", status="met", outcome={**record_data()["outcome"], "reported_value": 5_500.0})
    result = stats.compute([a, b])
    assert result["by_company"] == {"Alpha Corp": {"missed": 1}, "Beta Corp": {"met": 1}}


def test_compute_on_no_records() -> None:
    result = stats.compute([])
    assert result["rows"] == 0 and result["missed"] == 0
    assert result["median_days_to_falsifiable"] is None and result["missed_never_acknowledged_share"] is None


# --- floors, counted apart from beat/worse ---------------------------------


def test_is_floor_is_a_lower_bound_with_no_ceiling(record_data) -> None:
    floor = _record(record_data, assumption={**record_data()["assumption"], "target_high": None})
    ranged = _record(record_data)
    ceiling = _record(record_data, assumption={**record_data()["assumption"], "target_low": None})
    assert stats.is_floor(floor) and not stats.is_floor(ranged) and not stats.is_floor(ceiling)


def test_compute_counts_missed_floors_separately_from_the_direction_split(record_data) -> None:
    floor_worse = _record(record_data, assumption={**record_data()["assumption"], "target_high": None})
    ranged_worse = _record(record_data)
    result = stats.compute([floor_worse, ranged_worse])
    assert result["missed_floors"] == 1
    assert result["missed_by_direction"] == {"worse": 2}  # a floor is still a worse, just also flagged


# --- median days to falsifiable, and acknowledgement rate, by direction --------


def test_compute_median_days_to_falsifiable_by_direction(record_data) -> None:
    beat = _record(record_data, days_to_falsifiable=100, outcome={**record_data()["outcome"], "reported_value": 6_500.0})
    worse_a = _record(record_data, days_to_falsifiable=200)
    worse_b = _record(record_data, days_to_falsifiable=400)
    result = stats.compute([beat, worse_a, worse_b])
    assert result["median_days_to_falsifiable_by_direction"] == {"better": 100, "worse": 300}


def test_compute_median_days_to_falsifiable_by_direction_is_none_with_nothing_in_it(record_data) -> None:
    worse = _record(record_data)
    result = stats.compute([worse])
    assert result["median_days_to_falsifiable_by_direction"]["better"] is None


def test_compute_acknowledged_rate_by_direction(record_data) -> None:
    acknowledged_better = _record(
        record_data, outcome={**record_data()["outcome"], "reported_value": 6_500.0},
        acknowledged_at="2025-02-04", acknowledgement_evidence=record_data()["outcome"]["evidence"], days_to_acknowledged=1,
    )
    unacknowledged_better = _record(record_data, outcome={**record_data()["outcome"], "reported_value": 6_500.0})
    unacknowledged_worse = _record(record_data)
    result = stats.compute([acknowledged_better, unacknowledged_better, unacknowledged_worse])
    assert result["acknowledged_rate_by_direction"] == {"better": pytest.approx(0.5), "worse": 0.0}


def test_compute_acknowledged_rate_by_direction_is_none_with_nothing_in_it(record_data) -> None:
    worse = _record(record_data)
    result = stats.compute([worse])
    assert result["acknowledged_rate_by_direction"]["better"] is None


# --- the per-company table -------------------------------------------------------


def test_compute_company_table_has_rows_status_counts_and_never_ack_share(record_data) -> None:
    a = _record(record_data, company="Alpha Corp")  # missed, never acknowledged
    b = _record(record_data, company="Beta Corp", status="met", outcome={**record_data()["outcome"], "reported_value": 5_500.0})
    result = stats.compute([a, b])
    table = {row["company"]: row for row in result["company_table"]}
    assert table["Alpha Corp"] == {"company": "Alpha Corp", "rows": 1, "by_status": {"missed": 1}, "missed_never_acknowledged_share": 1.0}
    assert table["Beta Corp"] == {"company": "Beta Corp", "rows": 1, "by_status": {"met": 1}, "missed_never_acknowledged_share": None}


def test_format_stats_renders_a_company_table_with_a_header(record_data) -> None:
    result = stats.compute([_record(record_data, company="Alpha Corp")])
    text = stats.format_stats(result)
    assert "company table:" in text
    assert "Alpha Corp" in text
    lines = [line for line in text.splitlines() if "company" in line and "rows" in line]
    assert lines  # a header row with both column names


# --- run -------------------------------------------------------------------


def test_run_prints_the_direction_split(tmp_path, record_data) -> None:
    path = tmp_path / "release.jsonl"
    write_jsonl(path, [record_data()])
    text = stats.run(path)
    assert "rows: 1" in text and "missed by direction" in text and "'worse': 1" in text


def test_run_reports_invalid_rows_without_raising(tmp_path, record_data) -> None:
    path = tmp_path / "release.jsonl"
    path.write_text("not json\n" + json.dumps(record_data(), default=str) + "\n", encoding="utf-8")
    text = stats.run(path)
    assert "rows: 1" in text and "1 row(s) did not parse" in text


def test_run_on_a_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        stats.run(Path("/nonexistent/release.jsonl"))


def test_run_as_json_prints_computes_own_dict(tmp_path, record_data) -> None:
    path = tmp_path / "release.jsonl"
    write_jsonl(path, [record_data()])
    parsed = json.loads(stats.run(path, as_json=True))
    assert parsed["rows"] == 1 and parsed["missed_by_direction"] == {"worse": 1} and parsed["invalid"] == 0


def test_run_as_json_counts_invalid_rows_too(tmp_path, record_data) -> None:
    path = tmp_path / "release.jsonl"
    path.write_text("not json\n" + json.dumps(record_data(), default=str) + "\n", encoding="utf-8")
    parsed = json.loads(stats.run(path, as_json=True))
    assert parsed["rows"] == 1 and parsed["invalid"] == 1
