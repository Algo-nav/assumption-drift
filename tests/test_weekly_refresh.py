"""pipeline/refresh.py: the review diff, the run summary, and the issue the weekly job opens. No network, no model."""

from __future__ import annotations

import json

from pipeline import refresh

COMPANIES = [{"cik": "0000000001", "ticker": "AAA"}, {"cik": "0000000002", "ticker": "BBB"}]


def row(**kw: str) -> dict[str, str]:
    base = {c: "" for c in refresh.SNAPSHOT_COLUMNS}
    return {**base, **kw}


def test_diff_finds_new_rows_flags_and_leaves_old_rows_alone() -> None:
    before = {"0000000001": {"r1": row()}}
    after = {"0000000001": {"r1": row(), "r2": row(), "f1": row(empty_block="true")}}
    d = refresh.diff_review(before, after)["0000000001"]
    assert d["new_ids"] == ["r2", "f1"] and d["new_flag_ids"] == ["f1"] and d["proposal_ids"] == []


def test_diff_counts_outcomes_acks_withdrawals_and_proposals() -> None:
    before = {"0000000001": {"a": row(), "b": row(), "c": row(), "d": row(change_pending_review="true")}}
    after = {"0000000001": {
        "a": row(**{"outcome.reported_value": "5"}, acknowledged_at="2026-10-01"),
        "b": row(ack_pending_review="true"),
        "c": row(change_pending_review="true", aid_proposed_change=json.dumps({"aid_withdrawal_note": "x", "outcome.reported_value": "7"})),
        "d": row(change_pending_review="true"),  # was pending already: not a new proposal
    }}
    d = refresh.diff_review(before, after)["0000000001"]
    assert (d["outcomes_attached"], d["acks_filled"], d["acks_proposed"]) == (1, 1, 1)
    assert (d["outcomes_proposed"], d["withdrawals_proposed"]) == (1, 1)
    assert d["proposal_ids"] == ["b", "c"]


def test_spend_this_run_counts_only_lines_after_the_start() -> None:
    ledger = [{"stage": "03_structure", "requests": 9, "usd": 1.0},
              {"stage": "03_structure", "requests": 2, "usd": 0.01}, {"stage": "04_outcomes", "requests": 3, "usd": 0.02},
              {"stage": "03_structure", "requests": 1, "usd": 0.005}]
    assert refresh.spend_this_run(ledger, 1) == {"03_structure": {"requests": 3, "usd": 0.015}, "04_outcomes": {"requests": 3, "usd": 0.02}}
    assert refresh.spend_this_run(ledger, 4) == {}


def test_count_candidates_only_in_the_new_accessions(tmp_path) -> None:
    (tmp_path / "0000000001.jsonl").write_text(
        "\n".join(json.dumps({"accession": a}) for a in ("new-1", "new-1", "old-1")) + "\n", encoding="utf-8")
    assert refresh.count_candidates(tmp_path, {"0000000001": {"new-1"}, "0000000002": {"x"}}) == {"0000000001": 2}


def summary(**over):
    diff = refresh.diff_review(
        {"0000000001": {"r1": row()}, "0000000002": {}},
        {"0000000001": {"r1": row(ack_pending_review="true"), "r2": row(), "f1": row(empty_block="true")}, "0000000002": {}})
    args = dict(
        run_date="2026-10-17", max_usd=1.0, companies=COMPANIES,
        filings=[{"cik": "0000000001", "form": "8-K"}, {"cik": "0000000001", "form": "10-Q"}, {"cik": "0000000002", "form": "8-K"}],
        candidates={"0000000001": 12}, stage_spend={"03_structure": {"requests": 4, "usd": 0.0123}, "04_outcomes": {"requests": 2, "usd": 0.004}},
        diff=diff, pending_published=17, spend_to_date=0.42, failures=[], dumps={"AAA": ["AAA_new.txt", "AAA_pending.txt"]})
    return refresh.build_summary(**{**args, **over})


def test_summary_per_company_totals_and_spend() -> None:
    s = summary()
    assert s["filings_total"] == 3 and s["filings_by_form"] == {"10-Q": 1, "8-K": 2}
    assert s["per_company"] == {
        "AAA": {"filings": 2, "candidates": 12, "new_rows": 1, "new_flags": 1, "proposals": 1},
        "BBB": {"filings": 1, "candidates": 0, "new_rows": 0, "new_flags": 0, "proposals": 0}}
    assert (s["new_rows"], s["new_flags"], s["proposals"]) == (1, 1, 1)
    assert s["acknowledgements"] == {"filled": 0, "proposed": 1}
    assert s["run_usd"] == 0.0163 and s["spend_to_date_usd"] == 0.42 and s["pending_published"] == 17


def test_summary_of_a_quiet_week_is_all_zeros_and_json_serialisable() -> None:
    s = summary(filings=[], candidates={}, stage_spend={}, diff={}, dumps={})
    assert (s["filings_total"], s["new_rows"], s["proposals"], s["run_usd"], s["stages"]) == (0, 0, 0, 0, {})
    json.dumps(s)
    assert refresh.issue_title(s) == "Refresh 2026-10-17: 0 new rows, 0 proposals, $0.00"


def test_issue_title_and_body() -> None:
    s = summary(failures=[{"stage": "03b_verify", "exit": 1}])
    assert refresh.issue_title(s) == "Refresh 2026-10-17: 1 new rows, 1 proposals, $0.02"
    body = refresh.render_issue_body(s, "refresh/2026-10-17")
    assert "`refresh/2026-10-17`" in body
    assert "| AAA | 2 | 12 | 1 | 1 | 1 |" in body
    assert "| 03_structure | 4 | $0.0123 |" in body
    assert "| Rows pending review (published companies) | 17 |" in body
    assert "- 03b_verify: exit 1" in body
    assert "rr review data/review/0000000001.csv --apply decisions_AAA_new.csv   # decisions for AAA_new.txt" in body
    assert "decisions_AAA_pending.csv" in body
    assert "BBB_new" not in body


def test_issue_body_without_dumps_or_failures_has_no_review_section() -> None:
    body = refresh.render_issue_body(summary(dumps={}), "refresh/2026-10-17")
    assert "**Review**" not in body and "**Failures**" not in body
