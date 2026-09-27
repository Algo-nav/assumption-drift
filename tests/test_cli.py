"""research_record/cli.py: the `rr` command line."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from research_record import cli, reviewer


def make_csv(path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["record_id", "approved"], lineterminator="\n")
        writer.writeheader()
        writer.writerow({"record_id": "R1", "approved": "false"})


def test_review_calls_the_reviewer_with_the_given_path(tmp_path, monkeypatch) -> None:
    path = tmp_path / "R.csv"
    make_csv(path)
    seen: dict = {}
    monkeypatch.setattr(reviewer, "run", lambda p, **kw: seen.setdefault("path", p))
    assert cli.main(["review", str(path)]) == 0
    assert seen["path"] == path


def test_review_of_a_missing_file_reports_it_and_exits_1(tmp_path, capsys) -> None:
    missing = tmp_path / "nope.csv"
    assert cli.main(["review", str(missing)]) == 1
    assert "rr review" in capsys.readouterr().err


def test_review_of_a_csv_with_no_record_id_column_reports_it_and_exits_1(tmp_path, capsys) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("a,b\n1,2\n")
    assert cli.main(["review", str(path)]) == 1
    assert "record_id" in capsys.readouterr().err


def test_review_with_no_filter_flags_passes_none(tmp_path, monkeypatch) -> None:
    path = tmp_path / "R.csv"
    make_csv(path)
    seen: dict = {}
    monkeypatch.setattr(reviewer, "run", lambda p, **kw: seen.update(kw))
    assert cli.main(["review", str(path)]) == 0
    assert seen["filters"] is None


def test_review_passes_repeated_filter_flags_through_in_order(tmp_path, monkeypatch) -> None:
    path = tmp_path / "R.csv"
    make_csv(path)
    seen: dict = {}
    monkeypatch.setattr(reviewer, "run", lambda p, **kw: seen.update(kw))
    assert cli.main(["review", str(path), "--filter", "verify-no", "--filter", "no-note"]) == 0
    assert seen["filters"] == ["verify-no", "no-note"]


def test_review_with_an_unrecognised_filter_reports_it_and_exits_1(tmp_path, capsys) -> None:
    path = tmp_path / "R.csv"
    make_csv(path)
    assert cli.main(["review", str(path), "--filter", "bogus"]) == 1
    assert "bogus" in capsys.readouterr().err


def test_a_keyboard_interrupt_during_review_exits_130(tmp_path, monkeypatch) -> None:
    path = tmp_path / "R.csv"
    make_csv(path)

    def raise_interrupt(p, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(reviewer, "run", raise_interrupt)
    assert cli.main(["review", str(path)]) == 130


def test_no_command_is_an_error() -> None:
    with pytest.raises(SystemExit):
        cli.main([])


def test_review_actually_drives_the_reviewer_end_to_end(tmp_path, monkeypatch) -> None:
    path = tmp_path / "R.csv"
    make_csv(path)
    keys = iter(["y"])
    monkeypatch.setattr(reviewer, "default_read_key", lambda *a, **k: next(keys))
    assert cli.main(["review", str(path)]) == 0
    on_disk = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    assert on_disk[0]["approved"] == "true"


# --- stats -------------------------------------------------------------------


def test_stats_prints_summary_numbers(tmp_path, record_data, capsys) -> None:
    path = tmp_path / "release.jsonl"
    path.write_text(json.dumps(record_data(), default=str) + "\n", encoding="utf-8")
    assert cli.main(["stats", str(path)]) == 0
    out = capsys.readouterr().out
    assert "rows: 1" in out and "missed by direction" in out


def test_stats_of_a_missing_file_reports_it_and_exits_1(tmp_path, capsys) -> None:
    missing = tmp_path / "nope.jsonl"
    assert cli.main(["stats", str(missing)]) == 1
    assert "rr stats" in capsys.readouterr().err


def test_stats_json_prints_valid_json(tmp_path, record_data, capsys) -> None:
    path = tmp_path / "release.jsonl"
    path.write_text(json.dumps(record_data(), default=str) + "\n", encoding="utf-8")
    assert cli.main(["stats", str(path), "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["rows"] == 1


# --- validate ------------------------------------------------------------------


def test_validate_of_a_clean_jsonl_reports_ok_and_exits_0(tmp_path, record_data, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "raw" / "0000000123").mkdir(parents=True)
    html = tmp_path / "data" / "raw" / "0000000123" / "0000000123-24-000001.html"
    html.write_bytes(b"The Company expects full year 2024 revenue of $5.0 billion to $6.0 billion.")
    from research_record.validate import DEFAULT_RAW_DIR
    import hashlib
    data = record_data()
    data["assumption"]["evidence"]["content_sha256"] = hashlib.sha256(html.read_bytes()).hexdigest()
    data["outcome"] = None
    data["status"] = "unresolved"
    data["days_to_falsifiable"] = None
    path = tmp_path / "release.jsonl"
    path.write_text(json.dumps(data, default=str) + "\n", encoding="utf-8")
    assert cli.main(["validate", str(path.relative_to(tmp_path))]) == 0
    assert "all OK" in capsys.readouterr().out


def test_validate_reports_issues_and_exits_1(tmp_path, record_data, capsys) -> None:
    path = tmp_path / "release.jsonl"
    path.write_text(json.dumps(record_data(), default=str) + "\n", encoding="utf-8")  # cached doc absent
    assert cli.main(["validate", str(path)]) == 1
    err = capsys.readouterr().err
    assert record_data()["record_id"] in err and "issue(s)" in err


def test_validate_of_an_unsupported_extension_reports_it_and_exits_1(tmp_path, capsys) -> None:
    path = tmp_path / "release.txt"
    path.write_text("nothing", encoding="utf-8")
    assert cli.main(["validate", str(path)]) == 1
    assert "rr validate" in capsys.readouterr().err
