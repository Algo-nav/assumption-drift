"""pipeline/config.yaml: the values SCOPE fixes, and the ones Navneet still has to supply."""

from __future__ import annotations

import pytest

from pipeline import common


@pytest.fixture(scope="module")
def config():
    return common.load_config()


def test_edgar_block_matches_scope(config) -> None:
    edgar = config["edgar"]
    assert edgar["user_agent"] == "Navneet navn07588@gmail.com"
    assert edgar["max_rps"] == 8
    assert edgar["filing_types"] == ["8-K", "10-K", "10-Q"]
    # date_to was moved from SCOPE's 2025-12-31 to 2026-09-20 at Navneet's request, to take in 2026 filings.
    assert (edgar["date_from"], edgar["date_to"]) == ("2019-01-01", "2026-09-20")


def test_the_three_pilot_companies_have_ten_digit_ciks(config) -> None:
    listed = common.companies(config)
    assert [(c.ticker, c.cik) for c in listed] == [
        ("NVDA", "0001045810"),
        ("TGT", "0000027419"),
        ("CRM", "0001108524"),
    ]
    assert all(len(c.cik) == 10 and c.cik.isdigit() for c in listed)


def test_only_filters_by_ticker(config) -> None:
    assert [c.ticker for c in common.companies(config, ["tgt"])] == ["TGT"]


def test_llm_block_keeps_scopes_values_and_adds_prices(config) -> None:
    llm = config["llm"]
    assert (llm["model"], llm["budget_usd"]) == ("claude-haiku-4-5", 10)
    # Added for the cost gate: Haiku 4.5 list prices per million tokens, and the Batches discount.
    assert (llm["price_input_per_mtok"], llm["price_output_per_mtok"], llm["batch_discount"]) == (1.00, 5.00, 0.5)


def test_hf_block_is_untouched(config) -> None:
    assert config["hf"] == {"user": "{HF_USER}", "dataset": "assumption-drift"}  # supplied before Phase 3


def test_a_config_with_another_user_agent_is_refused(tmp_path) -> None:
    bad = tmp_path / "config.yaml"
    bad.write_text('edgar: {user_agent: "Somebody else@example.com"}\n')
    with pytest.raises(ValueError, match="user_agent"):
        common.load_config(bad)


def test_every_company_has_a_fiscal_year_end_month_and_targets_years_are_named_for_the_year_they_start(config) -> None:
    listed = {c.ticker: c for c in common.companies(config)}
    assert all(isinstance(c.fiscal_year_end_month, int) and 1 <= c.fiscal_year_end_month <= 12 for c in listed.values())
    # From each company's 10-K: NVIDIA "fiscal year ended January 27, 2019", Salesforce "January 31, 2019", Target "Fiscal 2022 will end January 28, 2023".
    assert {t: (c.fiscal_year_end_month, c.fiscal_year_named_for) for t, c in listed.items()} == {"NVDA": (1, "end"), "TGT": (1, "start"), "CRM": (1, "end")}
