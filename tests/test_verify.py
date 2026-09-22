"""03b_verify.py: a second machine opinion on the review queue, against synthetic rows and a fake client."""

from __future__ import annotations

import importlib
import json

import pytest
import yaml

import fakes
from pipeline import common, llm

verify = importlib.import_module("pipeline.03b_verify")
review = importlib.import_module("pipeline.05_review")

COMPANY = common.Company("Example Corp", "EXMP", "0000000123", fiscal_year_end_month=1)
OTHER = common.Company("Other Corp", "OTHR", "0000000456", fiscal_year_end_month=1)


def row(record_id="R1", metric="revenue", low="5.0", high="6.0", unit="USD billions", period="Q4 FY2026",
        excerpt="Revenue is expected to be $5.0 billion to $6.0 billion.", capture_method="sentence",
        empty_block="false", company=COMPANY, **overrides) -> dict[str, str]:
    base = {c: "" for c in review.COLUMNS}
    base.update({
        "record_id": record_id, "company": company.name, "ticker": company.ticker, "cik": company.cik,
        "assumption.metric": metric, "assumption.target_low": low, "assumption.target_high": high,
        "assumption.unit": unit, "assumption.target_period": period, "assumption.evidence.excerpt": excerpt,
        "approved": "false", "hand_verified": "false", "reviewer_note": "", "conflict": "false",
        "empty_block": empty_block, "aid_capture_method": capture_method,
    })
    base.update(overrides)
    return base


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / "review").mkdir()
    monkeypatch.setattr(verify, "REVIEW_DIR", tmp_path / "review")
    monkeypatch.setattr(llm, "BATCH_DIR", tmp_path / "batches")
    return tmp_path


def seed(world, company, rows):
    review.write_csv(world / "review" / f"{company.cik}.csv", rows)


def read_csv(world, company):
    return review.read_csv(world / "review" / f"{company.cik}.csv")


# --- which rows get a request -------------------------------------------------


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({}, True),
        ({"empty_block": "true"}, False),
        ({"assumption.metric": ""}, False),
        ({"assumption.evidence.excerpt": ""}, False),
    ],
)
def test_is_verifiable(changes, expected) -> None:
    assert verify.is_verifiable(row(**changes)) is expected


def test_custom_id_is_the_record_id_prefixed() -> None:
    assert verify.custom_id(row(record_id="01HZY8Q9XMR3T7VBN2CDEFGHJK")) == "v-01HZY8Q9XMR3T7VBN2CDEFGHJK"


# --- the fiscal calendar fact (fix 6) ------------------------------------------


def test_fiscal_note_states_the_calendar_and_which_year_the_label_names() -> None:
    ends = common.Company("Example Corp", "EXMP", "0000000123", fiscal_year_end_month=1, fiscal_year_named_for="end")
    note = verify.fiscal_note(ends)
    assert "Example Corp" in note and "month 1" in note and "ENDS in" in note
    starts = common.Company("Target Corporation", "TGT", "0000027419", fiscal_year_end_month=1, fiscal_year_named_for="start")
    assert "STARTS in" in verify.fiscal_note(starts)


def test_fiscal_note_says_so_when_the_calendar_is_not_known() -> None:
    unknown = common.Company("Unknown Co", "UNK", "0000000000")
    assert "not known" in verify.fiscal_note(unknown)


# --- the prompt: what it carries (fixes 5, 6, 7) -------------------------------


def test_the_prompt_carries_the_metric_numbers_unit_period_excerpt_and_fiscal_calendar() -> None:
    user = verify.build_prompt(row(), COMPANY)
    assert "revenue" in user and "$5 billion to $6 billion" in user and "USD billions" in user
    assert "Q4 FY2026" in user and "Revenue is expected to be $5.0 billion to $6.0 billion." in user
    assert "fiscal year ends in month 1" in user
    # nothing about the row's identity or how it was captured leaks into what the model sees
    for leak in (COMPANY.ticker, COMPANY.cik, "sentence"):
        assert leak not in user


def test_the_prompt_carries_heading_lead_in_and_table_header_only_when_the_row_has_them() -> None:
    bare = verify.build_prompt(row(), COMPANY)
    assert "Section heading:" not in bare and "Lead-in line:" not in bare and "Table header:" not in bare
    full = verify.build_prompt(
        row(aid_heading="Updated Q4 Fiscal 2019 Guidance", aid_lead_in="Our outlook is as follows:",
            aid_table_header="Q1 2027 | Full Year 2027"),
        COMPANY,
    )
    assert "Section heading: Updated Q4 Fiscal 2019 Guidance" in full
    assert "Lead-in line: Our outlook is as follows:" in full
    assert "Table header: Q1 2027 | Full Year 2027" in full


def test_the_system_prompt_states_the_pm_rule_and_the_named_equivalences() -> None:
    assert "[P-X, P+X]" in verify.VERIFY_SYSTEM
    assert "$21B" in verify.VERIFY_SYSTEM and "$21.0B" in verify.VERIFY_SYSTEM
    assert "non-GAAP" in verify.VERIFY_SYSTEM and "Adjusted" in verify.VERIFY_SYSTEM
    assert "decline of 3 to 5 percent" in verify.VERIFY_SYSTEM and "-5% to -3%" in verify.VERIFY_SYSTEM


def test_build_requests_covers_every_company_and_skips_what_is_not_verifiable(world) -> None:
    seed(world, COMPANY, [row("R1"), row("R2", empty_block="true")])
    seed(world, OTHER, [row("R3", company=OTHER)])
    requests, index = verify.build_requests([COMPANY, OTHER])
    assert {r.custom_id for r in requests} == {"v-R1", "v-R3"}
    assert index["v-R1"] == (COMPANY, "R1") and index["v-R3"] == (OTHER, "R3")


def test_no_review_file_means_no_requests(world) -> None:
    requests, index = verify.build_requests([COMPANY])
    assert requests == [] and index == {}


# --- turning model output into a verdict, pure (fix 8: aid_verify_class) ------


def result(cid, ok=True, verified=True, reason="matches", klass="other") -> llm.Result:
    text = json.dumps({"verified": verified, "reason": reason, "class": klass}) if ok else None
    return llm.Result(cid, "succeeded" if ok else "errored", text, 10, 10, None if ok else "overloaded_error", "b")


def test_derive_verdicts_reads_yes_and_no_with_a_class_only_on_no() -> None:
    index = {"v-R1": (COMPANY, "R1"), "v-R2": (COMPANY, "R2")}
    results = {
        "v-R1": result("v-R1", verified=True, reason="the excerpt says it", klass="other"),
        "v-R2": result("v-R2", verified=False, reason="wrong metric", klass="wrong_metric"),
    }
    verdicts = verify.derive_verdicts(index, results)
    assert verdicts == {"R1": ("yes", "the excerpt says it", ""), "R2": ("no", "wrong metric", "wrong_metric")}


def test_derive_verdicts_skips_missing_failed_and_unparseable_answers() -> None:
    index = {"v-R1": (COMPANY, "R1"), "v-R2": (COMPANY, "R2"), "v-R3": (COMPANY, "R3")}
    results = {"v-R1": result("v-R1", ok=False), "v-R2": llm.Result("v-R2", "succeeded", "not json", 1, 1, None, "b")}
    # v-R3 has no result at all: never sent, or the batch has not answered it yet
    assert verify.derive_verdicts(index, results) == {}


def test_an_answer_missing_the_class_field_is_skipped() -> None:
    index = {"v-R1": (COMPANY, "R1")}
    bad = llm.Result("v-R1", "succeeded", json.dumps({"verified": False, "reason": "x"}), 1, 1, None, "b")
    assert verify.derive_verdicts(index, {"v-R1": bad}) == {}


def test_an_invalid_class_value_falls_back_to_blank() -> None:
    index = {"v-R1": (COMPANY, "R1")}
    bad = llm.Result("v-R1", "succeeded", json.dumps({"verified": False, "reason": "x", "class": "not_a_real_class"}), 1, 1, None, "b")
    assert verify.derive_verdicts(index, {"v-R1": bad})["R1"] == ("no", "x", "")


def test_a_long_reason_is_cut_to_the_limit() -> None:
    index = {"v-R1": (COMPANY, "R1")}
    results = {"v-R1": result("v-R1", reason="x" * 500)}
    assert len(verify.derive_verdicts(index, results)["R1"][1]) == verify.REASON_LIMIT


# --- writing back: preserve edits, then sort ----------------------------------


def test_apply_verdicts_only_changes_the_three_verify_columns() -> None:
    r = row(approved="true", reviewer_note="checked on EDGAR", **{"assumption.target_high": "6100.0"})
    updated = verify.apply_verdicts([r], {"R1": ("no", "wrong period", "wrong_period")})[0]
    assert (updated["aid_verify"], updated["aid_verify_reason"], updated["aid_verify_class"]) == ("no", "wrong period", "wrong_period")
    for key, value in r.items():
        if key not in ("aid_verify", "aid_verify_reason", "aid_verify_class"):
            assert updated[key] == value


def test_a_row_with_no_verdict_keeps_what_it_already_had() -> None:
    r = row(aid_verify="yes", aid_verify_reason="checked last time", aid_verify_class="")
    assert verify.apply_verdicts([r], {})[0] == r


def test_sort_rows_puts_no_first_then_section_then_the_rest_and_keeps_ties_in_order() -> None:
    rest = row("R1", capture_method="sentence", aid_verify="yes")
    section = row("R2", capture_method="section", aid_verify="yes")
    no_sentence = row("R3", capture_method="sentence", aid_verify="no")
    no_section = row("R4", capture_method="section", aid_verify="no")
    rest2 = row("R5", capture_method="sentence", aid_verify="")  # unchecked: neither no nor section
    ordered = verify.sort_rows([rest, section, no_sentence, no_section, rest2])
    assert [r["record_id"] for r in ordered] == ["R3", "R4", "R2", "R1", "R5"]


# --- main, end to end ------------------------------------------------------


@pytest.fixture
def config_path(world):
    cfg = common.load_config()
    cfg["companies"] = [{"name": COMPANY.name, "ticker": COMPANY.ticker, "cik": COMPANY.cik, "fiscal_year_end_month": 1}]
    path = world / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def respond(params: dict) -> str:
    user = params["messages"][0]["content"]
    verified = "BAD-EXCERPT" not in user
    return json.dumps({
        "verified": verified,
        "reason": "matches" if verified else "names a different metric",
        "class": "other" if verified else "wrong_metric",
    })


def test_a_dry_run_only_counts_tokens_and_writes_nothing(world, config_path, monkeypatch, capsys) -> None:
    seed(world, COMPANY, [row("R1"), row("R2")])
    client = fakes.FakeAnthropic(respond=respond)
    monkeypatch.setattr(llm, "make_client", lambda: client)
    assert verify.main(["--config", str(config_path)]) == 0
    assert client.messages.create_calls == [] and client.messages.batches.created == []
    on_disk = read_csv(world, COMPANY)
    assert all(r["aid_verify"] == "" for r in on_disk)  # untouched
    out = capsys.readouterr().out
    assert "nothing was written to data/review/" in out and "2 rows to check" in out


def test_submit_writes_verdicts_and_preserves_every_human_edit(world, config_path, monkeypatch) -> None:
    good = row("R1", excerpt="Revenue is expected to be $5.0 billion to $6.0 billion.")
    bad = row("R2", excerpt="BAD-EXCERPT: gross margin is expected to be 75%.", approved="true", reviewer_note="looks right to me",
              **{"assumption.target_high": "6100.0"})
    seed(world, COMPANY, [good, bad])
    client = fakes.FakeAnthropic(respond=respond)
    monkeypatch.setattr(llm, "make_client", lambda: client)
    assert verify.main(["--config", str(config_path), "--submit"]) == 0
    on_disk = {r["record_id"]: r for r in read_csv(world, COMPANY)}
    assert (on_disk["R1"]["aid_verify"], on_disk["R2"]["aid_verify"]) == ("yes", "no")
    assert on_disk["R2"]["aid_verify_reason"] == "names a different metric"
    # the human's approval, note and hand-corrected field are exactly as they were
    assert (on_disk["R2"]["approved"], on_disk["R2"]["reviewer_note"], on_disk["R2"]["assumption.target_high"]) == ("true", "looks right to me", "6100.0")
    steps = {e["step"] for e in llm.Ledger().entries()}
    assert steps == {"03b_verify"}  # the same ledger every step uses


def test_a_no_verdict_carries_its_class_and_a_yes_verdict_leaves_it_blank(world, config_path, monkeypatch) -> None:
    seed(world, COMPANY, [row("R1"), row("R2", excerpt="BAD-EXCERPT: gross margin is expected to be 75%.")])
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    assert verify.main(["--config", str(config_path), "--submit"]) == 0
    on_disk = {r["record_id"]: r for r in read_csv(world, COMPANY)}
    assert (on_disk["R1"]["aid_verify"], on_disk["R1"]["aid_verify_class"]) == ("yes", "")
    assert (on_disk["R2"]["aid_verify"], on_disk["R2"]["aid_verify_class"]) == ("no", "wrong_metric")


def test_the_rows_come_back_sorted_no_first(world, config_path, monkeypatch) -> None:
    seed(world, COMPANY, [
        row("R1", capture_method="sentence"),
        row("R2", capture_method="section", excerpt="BAD-EXCERPT here"),
        row("R3", capture_method="section"),
    ])
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    verify.main(["--config", str(config_path), "--submit"])
    on_disk = read_csv(world, COMPANY)
    assert [r["record_id"] for r in on_disk] == ["R2", "R3", "R1"]


def test_a_rerun_sends_nothing_new(world, config_path, monkeypatch) -> None:
    seed(world, COMPANY, [row("R1")])
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    verify.main(["--config", str(config_path), "--submit"])
    again = fakes.FakeAnthropic(respond=respond)
    monkeypatch.setattr(llm, "make_client", lambda: again)
    assert verify.main(["--config", str(config_path), "--submit"]) == 0
    assert again.messages.create_calls == [] and again.messages.batches.created == []
    assert read_csv(world, COMPANY)[0]["aid_verify"] == "yes"


def test_hand_editing_the_excerpt_sends_that_row_again(world, config_path, monkeypatch) -> None:
    seed(world, COMPANY, [row("R1", excerpt="Revenue is expected to be $5.0 billion to $6.0 billion.")])
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    verify.main(["--config", str(config_path), "--submit"])
    assert read_csv(world, COMPANY)[0]["aid_verify"] == "yes"

    edited = read_csv(world, COMPANY)
    edited[0]["assumption.evidence.excerpt"] = "BAD-EXCERPT: this is not what it said"
    review.write_csv(world / "review" / f"{COMPANY.cik}.csv", edited)

    again = fakes.FakeAnthropic(respond=respond)
    monkeypatch.setattr(llm, "make_client", lambda: again)
    assert verify.main(["--config", str(config_path), "--submit"]) == 0
    assert len(again.messages.create_calls) == 1  # the canary: the edited row was sent again
    assert read_csv(world, COMPANY)[0]["aid_verify"] == "no"


def test_empty_block_rows_are_never_sent_and_stay_exactly_as_they_were(world, config_path, monkeypatch) -> None:
    flag = row("R1", empty_block="true", **{"assumption.metric": "", "assumption.evidence.excerpt": ""}, aid_flag_note="the model returned no items")
    seed(world, COMPANY, [flag])
    client = fakes.FakeAnthropic(respond=respond)
    monkeypatch.setattr(llm, "make_client", lambda: client)
    assert verify.main(["--config", str(config_path), "--submit"]) == 0
    assert client.messages.create_calls == [] and client.messages.batches.created == []
    on_disk = read_csv(world, COMPANY)[0]
    assert on_disk["aid_verify"] == "" and on_disk["aid_flag_note"] == "the model returned no items"


def test_a_pending_batch_writes_nothing_to_data_review_and_exits_5(world, config_path, monkeypatch, capsys) -> None:
    seed(world, COMPANY, [row("R1"), row("R2")])
    client = fakes.FakeAnthropic(respond=respond, batch_ready=False)
    monkeypatch.setattr(llm, "make_client", lambda: client)
    assert verify.main(["--config", str(config_path), "--submit", "--wait-minutes", "0"]) == 5
    assert all(r["aid_verify"] == "" for r in read_csv(world, COMPANY))
    err = capsys.readouterr().err
    assert "has not ended, so data/review/ was not rewritten" in err


def test_resuming_a_pending_batch_writes_every_verdict(world, config_path, monkeypatch) -> None:
    seed(world, COMPANY, [row("R1"), row("R2", excerpt="BAD-EXCERPT here")])
    client = fakes.FakeAnthropic(respond=respond, batch_ready=False)
    monkeypatch.setattr(llm, "make_client", lambda: client)
    assert verify.main(["--config", str(config_path), "--submit", "--wait-minutes", "0"]) == 5
    client.batch_ready = True
    assert verify.main(["--config", str(config_path), "--submit", "--wait-minutes", "0"]) == 0
    on_disk = {r["record_id"]: r for r in read_csv(world, COMPANY)}
    assert on_disk["R1"]["aid_verify"] == "yes" and on_disk["R2"]["aid_verify"] == "no"


def test_submit_without_credentials_stops_and_writes_nothing(world, config_path, monkeypatch, capsys) -> None:
    seed(world, COMPANY, [row("R1")])
    client = fakes.FakeAnthropic(authenticated=False)
    monkeypatch.setattr(llm, "make_client", lambda: client)
    assert verify.main(["--config", str(config_path), "--submit"]) == 3
    assert "no API credentials" in capsys.readouterr().err
    assert read_csv(world, COMPANY)[0]["aid_verify"] == ""


def test_a_dry_run_leaves_an_earlier_submit_byte_for_byte_alone(world, config_path, monkeypatch) -> None:
    seed(world, COMPANY, [row("R1")])
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    verify.main(["--config", str(config_path), "--submit"])
    before = (world / "review" / f"{COMPANY.cik}.csv").read_bytes()
    monkeypatch.setattr(verify, "VERIFY_SYSTEM", verify.VERIFY_SYSTEM + "\nBe careful.")  # the archived answer no longer answers this request
    assert verify.main(["--config", str(config_path)]) == 0
    assert (world / "review" / f"{COMPANY.cik}.csv").read_bytes() == before
