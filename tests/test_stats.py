"""research_record/stats.py: `rr stats`, and the beat/shortfall direction split (SCOPE.md 5.2)."""

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


def test_missed_direction_reads_beat_and_shortfall_from_the_numbers(record_data) -> None:
    beat = ResearchRecord(**{**record_data(status="missed"), "outcome": {**record_data()["outcome"], "reported_value": 6_500.0}})
    shortfall = ResearchRecord(**record_data(status="missed"))  # fixture default: reported 4800, range [5000, 6000]
    assert stats.missed_direction(beat) == "beat"
    assert stats.missed_direction(shortfall) == "shortfall"


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
    shortfall = _record(record_data)  # reported 4800, below the 5000 low end
    result = stats.compute([beat, shortfall])
    assert result["missed_by_direction"] == {"beat": 1, "shortfall": 1}


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


# --- run -------------------------------------------------------------------


def test_run_prints_the_direction_split(tmp_path, record_data) -> None:
    path = tmp_path / "release.jsonl"
    write_jsonl(path, [record_data()])
    text = stats.run(path)
    assert "rows: 1" in text and "missed by direction" in text and "'shortfall': 1" in text


def test_run_reports_invalid_rows_without_raising(tmp_path, record_data) -> None:
    path = tmp_path / "release.jsonl"
    path.write_text("not json\n" + json.dumps(record_data(), default=str) + "\n", encoding="utf-8")
    text = stats.run(path)
    assert "rows: 1" in text and "1 row(s) did not parse" in text


def test_run_on_a_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        stats.run(Path("/nonexistent/release.jsonl"))
