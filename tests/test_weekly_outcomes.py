"""04_outcomes for the weekly refresh: the empty-overwrite guard, --accessions, --from-review, --sync and --max-usd."""

from __future__ import annotations

import dataclasses
import importlib
import json
from datetime import date

import pytest

import fakes
from pipeline import common, llm, manifest as mf
from research_record.schema import ResearchRecord
from test_outcomes import COMPANY, config_path, draft, read_outcomes, respond, seed_drafts, world  # noqa: F401  (fixtures)

outcomes = importlib.import_module("pipeline.04_outcomes")
review = importlib.import_module("pipeline.05_review")

RELEASE, LATER = "0000000123-24-000012", "0000000123-24-000013"  # the Q4 results release; the next one, which says "below our guidance"
CONFERENCE = "0000000123-24-000011"


def uid(n: int) -> str:
    """A record id the schema accepts (a ULID)."""
    return f"01{'0' * 22}{n:02d}"


D1, D2, D3, D9, NEW = uid(1), uid(2), uid(3), uid(9), uid(4)
OPEN, NOREL, NOTRUN, DEAD, MISS, HIT, OLDMISS, ACKED, FLAG, WDRN = (uid(i) for i in range(11, 21))


def outcomes_file(world):
    return world / "outcomes" / f"{COMPANY.cik}.jsonl"


def seed_outcomes(world, *rows):
    (world / "outcomes").mkdir(exist_ok=True)
    outcomes_file(world).write_text("".join(json.dumps(r) + "\n" for r in rows))


def as_of(monkeypatch, date_text: str) -> None:
    """The run date 04 uses as `as_of` and as `today`."""
    monkeypatch.setattr(outcomes, "_today", lambda: date.fromisoformat(date_text))


def scope(world, *accessions):
    path = world / "new_filings.json"
    path.write_text(json.dumps({"run_date": "2024-09-01", "complete": True, "companies": [COMPANY.cik], "filings": [
        {"cik": COMPANY.cik, "accession": a, "form": "8-K", "filing_date": "2024-05-01", "status": "fetched", "path": f"data/raw/{a}.html"} for a in accessions]}))
    return str(path)


def assumption(record_data):
    a = ResearchRecord(**record_data()).assumption.model_dump(mode="json")
    return {**a, "unit": "USD billions", "target_low": 5.0, "target_high": 6.0, "target_period": "Q4 FY2024",
            "text": "Revenue is expected to be $5.0 billion to $6.0 billion."}


def outcome_of(record_data, value, reported_at):
    o = ResearchRecord(**record_data()).outcome.model_dump(mode="json")
    return {"reported_value": value, "reported_at": reported_at, "evidence": {**o["evidence"], "filed_at": reported_at}}


def review_row(record_data, rid, *, outcome=None, reason="period not closed", **columns):
    d = {"draft_id": rid, "cik": COMPANY.cik, "ticker": "EXMP", "company": COMPANY.name, "assumption": assumption(record_data)}
    row = review.build_row(d, {"draft_id": rid, "outcome": outcome, "outcome_reason": None if outcome else reason, "acknowledgement": None}, date(2026, 9, 20))
    row.update(columns)
    return row


def write_review(world, monkeypatch, *rows):
    (world / "review").mkdir(exist_ok=True)
    review.write_csv(world / "review" / f"{COMPANY.cik}.csv", list(rows))
    monkeypatch.setattr(outcomes, "REVIEW_DIR", world / "review")


# --- the guard -----------------------------------------------------------------------------------------------------------


def test_an_empty_result_is_not_written_over_a_non_empty_outcomes_file(world, config_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    row = {"draft_id": D9, "outcome": None, "outcome_reason": "period not closed", "acknowledgement": None, "withdrawal": None}
    seed_outcomes(world, row)
    before = outcomes_file(world).read_bytes()  # a fresh checkout: tracked outcomes, no data/drafts/ at all
    assert outcomes.main(["--config", str(config_path), "--submit"]) == 6
    err = capsys.readouterr().err
    assert "REFUSED" in err and "--force" in err and "--from-review" in err
    assert outcomes_file(world).read_bytes() == before


def test_force_lets_an_empty_result_through(world, config_path, monkeypatch) -> None:
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    seed_outcomes(world, {"draft_id": D9, "outcome": None, "outcome_reason": "x", "acknowledgement": None, "withdrawal": None})
    assert outcomes.main(["--config", str(config_path), "--submit", "--force"]) == 0
    assert outcomes_file(world).read_text() == ""


def test_the_guard_does_not_get_in_the_way_of_an_empty_file_a_dry_run_or_a_merge(world, config_path, monkeypatch) -> None:
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    assert outcomes.main(["--config", str(config_path), "--submit"]) == 0  # nothing on disk to protect
    seed_outcomes(world, {"draft_id": D9, "outcome": None, "outcome_reason": "x", "acknowledgement": None, "withdrawal": None})
    before = outcomes_file(world).read_bytes()
    assert outcomes.main(["--config", str(config_path)]) == 0 and outcomes_file(world).read_bytes() == before  # dry run
    ids = world / "ids.txt"
    ids.write_text(f"{D1}\n")
    assert outcomes.main(["--config", str(config_path), "--submit", "--only", str(ids)]) == 0  # --only merges, so D9 stays
    assert [r["draft_id"] for r in read_outcomes(world)] == [D9]


# --- the document store --------------------------------------------------------------------------------------------------


def test_a_scoped_store_sees_only_the_listed_filings(world) -> None:
    assert len(outcomes.DocStore(COMPANY).metas) == 4
    scoped = outcomes.DocStore(COMPANY, {RELEASE, LATER})
    assert [m["accession"] for m in scoped.metas] == [RELEASE, LATER] and [m["accession"] for m in scoped.all_metas] == [RELEASE, LATER]
    assert outcomes.DocStore(COMPANY, set()).metas == []


# --- --from-review -------------------------------------------------------------------------------------------------------


def test_from_review_picks_the_unresolved_rows_the_open_ack_windows_and_the_open_periods(record_data) -> None:
    missed = outcome_of(record_data, 4.8, "2024-05-01")
    hit = outcome_of(record_data, 5.5, "2024-05-01")
    rows = [
        review_row(record_data, OPEN, reason="period not closed"),
        review_row(record_data, NOREL, reason="no later 8-K release for that period found"),
        review_row(record_data, NOTRUN, reason="outcome search not run"),
        review_row(record_data, DEAD, reason="the matching release has no line naming the metric with a number"),
        review_row(record_data, MISS, outcome=missed),
        review_row(record_data, HIT, outcome=hit),
        review_row(record_data, OLDMISS, outcome=outcome_of(record_data, 4.8, "2023-01-02")),  # 400 days from it ended long before as_of
        review_row(record_data, ACKED, outcome=missed, **{"acknowledged_at": "2024-06-01"}),
        review_row(record_data, FLAG, empty_block="true"),
    ]
    fiscal = dataclasses.replace(COMPANY, fiscal_year_end_month=4)  # a fiscal calendar, to date the periods
    picks, known = outcomes.select_from_review(fiscal, rows, as_of=date(2024, 9, 1), ack_days=400)
    assert [s["draft_id"] for s in picks["outcome"]] == [OPEN, NOREL, NOTRUN]
    assert [s["draft_id"] for s in picks["ack"]] == [OPEN, NOREL, NOTRUN, MISS]
    assert known == {MISS: {"reported_value": 4.8, "reported_at": "2024-05-01"}}
    # Q4 FY2024 of a fiscal year ending in April closes 2024-04-30: no row has an open period on 2024-09-01, and all do on 2024-04-01
    assert picks["withdrawal"] == []
    early, _ = outcomes.select_from_review(fiscal, rows[:1], as_of=date(2024, 4, 1), ack_days=400)
    assert [s["draft_id"] for s in early["withdrawal"]] == [OPEN]
    withdrawn, _ = outcomes.select_from_review(fiscal, [review_row(record_data, WDRN, aid_withdrawal_note="filed earlier")], as_of=date(2024, 4, 1), ack_days=400)
    assert withdrawn["withdrawal"] == []


def test_from_review_searches_only_the_new_filings_merges_rows_and_logs_the_spend(world, config_path, monkeypatch, record_data) -> None:
    fiscal = ResearchRecord(**record_data()).model_dump(mode="json")["outcome"]["evidence"]  # noqa: F841
    write_review(world, monkeypatch,
                 review_row(record_data, D1),  # unresolved: the Q4 release is in the new filings
                 review_row(record_data, D2, outcome=outcome_of(record_data, 5.5, "2024-05-01")),  # a hit: nobody looks at it again
                 review_row(record_data, D3, outcome=outcome_of(record_data, 4.8, "2024-05-01")))  # a miss with no acknowledgement
    untouched_d2 = {"draft_id": D2, "outcome": outcome_of(record_data, 5.5, "2024-05-01"), "outcome_reason": None, "acknowledgement": None, "withdrawal": None}
    d3_before = {"draft_id": D3, "outcome": outcome_of(record_data, 4.8, "2024-05-01"), "outcome_reason": None, "acknowledgement": None, "withdrawal": None}
    seed_outcomes(world, untouched_d2, d3_before)
    as_of(monkeypatch, "2024-09-01")
    client = fakes.FakeAnthropic(respond=respond)
    monkeypatch.setattr(llm, "make_client", lambda: client)
    code = outcomes.main(["--config", str(config_path), "--from-review", "--accessions", scope(world, RELEASE, LATER), "--sync", "--max-usd", "1", "--submit"])
    assert code == 0
    rows = {r["draft_id"]: r for r in read_outcomes(world)}
    assert set(rows) == {D1, D2, D3}
    assert rows[D2] == untouched_d2  # not selected, not rewritten
    assert rows[D1]["outcome"]["reported_value"] == 4.8 and rows[D1]["outcome_reason"] is None
    assert rows[D1]["acknowledgement"]["acknowledged_at"] == "2024-08-01"
    assert rows[D3]["outcome"] == d3_before["outcome"] and rows[D3]["acknowledgement"]["acknowledged_at"] == "2024-08-01"
    # exactly the requests that were wanted, one by one
    assert set(llm.RawArchive(outcomes.STEP_OUTCOMES).load()) == {f"o-{D1}"}
    assert set(llm.RawArchive(outcomes.STEP_ACK).load()) == {f"k-{D1}", f"k-{D3}"}
    assert client.messages.batches.created == []
    lines = [json.loads(l) for l in (world / "spend_ledger.jsonl").read_text().splitlines()]
    assert [(l["stage"], l["requests"]) for l in lines] == [("04_outcomes", 1), ("04_acknowledgements", 2)]


def test_from_review_never_takes_away_what_a_row_had(world, config_path, monkeypatch, record_data) -> None:
    write_review(world, monkeypatch, review_row(record_data, D3, outcome=outcome_of(record_data, 4.8, "2024-05-01")))
    d3 = {"draft_id": D3, "outcome": outcome_of(record_data, 4.8, "2024-05-01"), "outcome_reason": None, "acknowledgement": None, "withdrawal": None}
    seed_outcomes(world, d3)
    as_of(monkeypatch, "2024-09-01")
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    # the only new filing is the conference-call notice: no line in it admits anything
    assert outcomes.main(["--config", str(config_path), "--from-review", "--accessions", scope(world, CONFERENCE), "--sync", "--submit"]) == 0
    assert read_outcomes(world) == [d3]


def test_from_review_records_why_an_unresolved_row_is_still_unresolved(world, config_path, monkeypatch, record_data) -> None:
    write_review(world, monkeypatch, review_row(record_data, D1, reason="outcome search not run"))
    seed_outcomes(world, {"draft_id": D9, "outcome": None, "outcome_reason": "x", "acknowledgement": None, "withdrawal": None})
    as_of(monkeypatch, "2024-09-01")
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    assert outcomes.main(["--config", str(config_path), "--from-review", "--accessions", scope(world, CONFERENCE), "--sync", "--submit"]) == 0
    rows = {r["draft_id"]: r for r in read_outcomes(world)}
    assert rows[D1]["outcome"] is None and rows[D1]["outcome_reason"] == "no later 8-K release for that period found"
    assert rows[D9]["outcome_reason"] == "x"


def test_from_review_adds_the_new_drafts_of_the_listed_filings_only(world, config_path, monkeypatch, record_data) -> None:
    write_review(world, monkeypatch)  # an empty queue
    new = draft("NEW")
    new["assumption"]["evidence"] = {"accession_number": CONFERENCE}
    old = draft("OLD")
    old["assumption"]["evidence"] = {"accession_number": "0000000123-23-000001"}
    seed_drafts(world, new, old)
    as_of(monkeypatch, "2024-09-01")
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    assert outcomes.main(["--config", str(config_path), "--from-review", "--accessions", scope(world, CONFERENCE), "--sync", "--submit"]) == 0
    assert [r["draft_id"] for r in read_outcomes(world)] == ["NEW"]


def test_as_of_is_the_run_date_and_the_manifest_is_not_read(world, config_path, monkeypatch, record_data) -> None:
    """processed_through is far in the past here and absent in the second run; neither changes what 04 decides."""
    write_review(world, monkeypatch, review_row(record_data, D1, reason="outcome search not run"))
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    mf.save_manifest(mf.Manifest(date(2020, 1, 1), {}), common.MANIFEST_PATH)  # before the period closed: would say "period not closed"
    as_of(monkeypatch, "2024-09-01")  # the run date: after it closed
    assert outcomes.main(["--config", str(config_path), "--from-review", "--accessions", scope(world, CONFERENCE), "--sync", "--submit"]) == 0
    assert read_outcomes(world)[0]["outcome_reason"] == "no later 8-K release for that period found"
    common.MANIFEST_PATH.unlink()
    outcomes_file(world).unlink()
    assert outcomes.main(["--config", str(config_path), "--from-review", "--accessions", scope(world, CONFERENCE), "--sync", "--submit"]) == 0


def test_without_a_patched_clock_as_of_is_really_today() -> None:
    assert outcomes._today() == date.today()


def test_max_usd_stops_a_stage_before_it_sends(world, config_path, monkeypatch, record_data) -> None:
    write_review(world, monkeypatch, review_row(record_data, D1))
    as_of(monkeypatch, "2024-09-01")
    client = fakes.FakeAnthropic(respond=respond)
    monkeypatch.setattr(llm, "make_client", lambda: client)
    assert outcomes.main(["--config", str(config_path), "--from-review", "--accessions", scope(world, RELEASE, LATER), "--sync",
                          "--max-usd", "0.0000001", "--submit"]) == 3
    assert client.messages.create_calls == [] and not outcomes_file(world).exists()


def test_as_of_moves_with_the_run_date(world, config_path, monkeypatch, record_data) -> None:
    """A row whose period closes between two dates is "period not closed" before one and "no later release" after it."""
    write_review(world, monkeypatch, review_row(record_data, D1, reason="outcome search not run"))
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    reasons = {}
    for day in ("2024-04-01", "2024-09-01"):
        as_of(monkeypatch, day)
        outcomes_file(world).unlink(missing_ok=True)
        assert outcomes.main(["--config", str(config_path), "--from-review", "--accessions", scope(world, CONFERENCE), "--sync", "--submit"]) == 0
        reasons[day] = read_outcomes(world)[0]["outcome_reason"]
    assert reasons == {"2024-04-01": "period not closed", "2024-09-01": "no later 8-K release for that period found"}
