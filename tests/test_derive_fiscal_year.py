"""pipeline/worker.py: fiscal_year_end_month and fiscal_year_named_for from the SEC's company facts.

tests/fixtures/companyfacts/CIK##########.json is the SEC's companyfacts answer for each company in config.yaml, cut
down to the entries the deriver reads (form 10-K, fp FY, of the concepts it tries; the fields end, fy, fp, form, accn,
filed). The first test derives both fields from each and compares them with what config.yaml has by hand. A company
that fails there is a finding about the SEC's data or the rule, and is not excused here. No test reaches a network.
"""

from __future__ import annotations

import importlib
import json
from datetime import date
from pathlib import Path

import pytest

from pipeline import common, worker

FIXTURES = Path(__file__).parent / "fixtures" / "companyfacts"
COMPANIES = common.companies(common.load_config())


def fixture(cik: str) -> dict:
    return json.loads((FIXTURES / f"CIK{cik}.json").read_text(encoding="utf-8"))


def facts(*years: tuple[int, str], concept: str = "Revenues", form: str = "10-K", fp: str = "FY") -> dict:
    """A company facts answer with one filing per (fy, end)."""
    entries = [{"end": end, "fy": fy, "fp": fp, "form": form, "accn": f"0000000123-{n:02d}-000001", "filed": end}
               for n, (fy, end) in enumerate(years)]
    return {"cik": 123, "facts": {"us-gaap": {concept: {"units": {"USD": entries}}}}}


SEC = {"name": "Example Corporation", "tickers": ["EXMP"], "fiscalYearEnd": "1231"}
DECEMBER = facts((2023, "2023-12-31"), (2024, "2024-12-31"))


# --- the companies set up by hand ---------------------------------------------------------------------------------------


def test_every_company_in_config_has_a_company_facts_fixture() -> None:
    assert sorted(p.name for p in FIXTURES.glob("CIK*.json")) == sorted(f"CIK{c.cik}.json" for c in COMPANIES)
    assert all(p.stat().st_size < 20_000 for p in FIXTURES.glob("CIK*.json"))


@pytest.mark.parametrize("company", COMPANIES, ids=lambda c: c.ticker)
def test_the_derived_fiscal_year_is_the_one_set_by_hand_in_config(company) -> None:
    fiscal = worker.derive_fiscal_year(fixture(company.cik))
    votes = {v: sum(f.named_for == v for f in fiscal.facts if f.end >= worker.NAMING_CUTOFF) for v in ("end", "start")}
    assert (fiscal.end_month, fiscal.named_for) == (company.fiscal_year_end_month, company.fiscal_year_named_for), \
        f"{company.ticker}: {fiscal.concept}, {len(fiscal.facts)} 10-Ks, {votes}"


# --- the rule -----------------------------------------------------------------------------------------------------------


def test_a_10k_counts_once_by_the_year_it_reports_not_the_years_it_repeats() -> None:
    # two 10-Ks for years ending late January; each also repeats the two years before it under its own fy
    entries = []
    for n, fy in enumerate((2024, 2025)):
        for back in range(3):
            entries.append({"end": f"{fy - back}-01-28", "fy": fy, "fp": "FY", "form": "10-K", "accn": f"a-{n}", "filed": f"{fy}-03-01"})
    fiscal = worker.derive_fiscal_year({"facts": {"us-gaap": {"Revenues": {"units": {"USD": entries}}}}})
    assert (fiscal.end_month, fiscal.named_for, len(fiscal.facts)) == (1, "end", 2)
    assert [(f.fy, f.end) for f in fiscal.facts] == [(2024, date(2024, 1, 28)), (2025, date(2025, 1, 28))]


def test_a_year_named_for_the_year_it_starts_in() -> None:
    fiscal = worker.derive_fiscal_year(facts((2022, "2023-01-28"), (2023, "2024-02-03"), (2024, "2025-02-01")))
    assert (fiscal.end_month, fiscal.named_for) == (1, "start")  # Feb. 3 and Feb. 1 are January, a week earlier


def test_the_majority_decides_and_an_even_split_is_refused() -> None:
    assert worker.derive_fiscal_year(facts((2023, "2024-01-31"), (2025, "2025-01-31"), (2026, "2026-01-31"))).named_for == "end"
    with pytest.raises(worker.NeedsSetup, match="split evenly"):
        worker.derive_fiscal_year(facts((2023, "2024-01-31"), (2025, "2025-01-31")))
    with pytest.raises(worker.NeedsSetup, match="do not agree on the month"):
        worker.derive_fiscal_year(facts((2023, "2023-06-30"), (2024, "2024-09-30")))


def test_only_10ks_ending_in_2015_or_later_vote_on_the_name() -> None:
    old = [(2009, "2010-01-31"), (2010, "2011-01-31"), (2011, "2012-01-31"), (2012, "2013-01-31"), (2013, "2014-01-31")]
    fiscal = worker.derive_fiscal_year(facts(*old, (2015, "2015-01-31"), (2016, "2016-01-31")))
    assert (fiscal.end_month, fiscal.named_for, len(fiscal.facts)) == (1, "end", 7)  # five say start and do not count
    assert worker.NAMING_CUTOFF == date(2015, 1, 1)
    assert worker.derive_fiscal_year(facts((2014, "2014-12-31"), (2015, "2015-01-01"), (2016, "2016-01-02"), concept="Assets")).end_month == 12
    for too_few in [facts(*old), facts(*old, (2015, "2015-01-31")), facts((2014, "2014-06-30"), (2015, "2015-06-30"))]:
        with pytest.raises(worker.NeedsSetup, match="end on or after 2015-01-01; 2 are needed"):
            worker.derive_fiscal_year(too_few)
    with pytest.raises(worker.NeedsSetup, match="split evenly"):
        worker.derive_fiscal_year(facts(*old, (2015, "2016-01-31"), (2017, "2017-01-31")))
    # a December year holds no vote, so it needs no 10-K from the cutoff on
    assert worker.derive_fiscal_year(facts((2012, "2012-12-31"), (2013, "2013-12-31"))).named_for == "end"


def test_fewer_than_two_10k_facts_is_refused() -> None:
    for answer in [facts((2024, "2024-06-30")), facts(), {}, {"facts": {}},
                   facts((2023, "2023-06-30"), (2024, "2024-06-30"), form="10-Q"), facts((2023, "2023-06-30"), (2024, "2024-06-30"), fp="Q4"),
                   facts((2023, "2023-06-30"), (2024, "2024-06-30"), form="10-K/A"), facts((2023, "2023-06-30"), (2024, "2024-06-30"), concept="NetIncomeLoss")]:
        with pytest.raises(worker.NeedsSetup, match="2 are needed"):
            worker.derive_fiscal_year(answer)


def test_the_concepts_are_tried_in_order_and_the_first_with_two_10ks_is_used() -> None:
    one, two, three = (facts((2024, "2024-06-30"), concept="Revenues"),
                       facts((2023, "2024-01-31"), (2024, "2025-01-31"), concept="RevenueFromContractWithCustomerExcludingAssessedTax"),
                       facts((2024, "2024-09-30"), (2025, "2025-09-30"), concept="Assets"))
    merged = {"facts": {"us-gaap": {**one["facts"]["us-gaap"], **two["facts"]["us-gaap"], **three["facts"]["us-gaap"]}}}
    fiscal = worker.derive_fiscal_year(merged)
    assert (fiscal.concept, fiscal.end_month, fiscal.named_for) == ("RevenueFromContractWithCustomerExcludingAssessedTax", 1, "start")
    del merged["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]
    assert worker.derive_fiscal_year(merged).concept == "Assets"
    assert worker.annual_facts(one)[0] == "Revenues"  # one fact is reported as what was found, and refused by the caller


def test_the_concept_is_chosen_by_10ks_from_2015_on() -> None:
    old = facts((2009, "2010-01-30"), (2010, "2011-01-29"), (2011, "2012-01-28"), (2014, "2015-01-31"), concept="Revenues")
    new = facts((2018, "2019-02-02"), (2019, "2020-02-01"), concept="RevenueFromContractWithCustomerExcludingAssessedTax")
    both = {"facts": {"us-gaap": {**old["facts"]["us-gaap"], **new["facts"]["us-gaap"]}}}
    fiscal = worker.derive_fiscal_year(both)  # Revenues has four 10-Ks, one of them from 2015 on: passed over
    assert (fiscal.concept, fiscal.end_month, fiscal.named_for, len(fiscal.facts)) == ("RevenueFromContractWithCustomerExcludingAssessedTax", 1, "start", 2)
    assert worker.annual_facts(old)[0] == "Revenues"  # nothing newer anywhere: the older concept is what there is
    with pytest.raises(worker.NeedsSetup, match="1 of the 4 10-K facts .Revenues. end on or after 2015-01-01"):
        worker.derive_fiscal_year(old)
    december = facts((2011, "2011-12-31"), (2012, "2012-12-31"), (2013, "2013-12-31"), concept="Revenues")
    assert worker.derive_fiscal_year(december).named_for == "end"  # a December year: no vote, no minimum


def test_a_december_year_is_named_for_its_end_whatever_fy_says() -> None:
    # a 52/53 week year that ends on January 2 is a December year; and fy one behind changes nothing in December
    assert worker.derive_fiscal_year(facts((2020, "2021-01-02"), (2021, "2022-01-01"))).end_month == 12
    fiscal = worker.derive_fiscal_year(facts((2022, "2023-12-31"), (2023, "2024-12-31")))
    assert (fiscal.end_month, fiscal.named_for) == (12, "end")


# --- with the submissions answer ----------------------------------------------------------------------------------------


def test_a_company_is_set_up_from_the_two_answers() -> None:
    assert worker.derive_company(SEC, DECEMBER) == worker.Derived("Example Corporation", "EXMP", 12, "end")
    assert worker.derive_company({**SEC, "fiscalYearEnd": "0102"}, DECEMBER).fiscal_year_end_month == 12  # a 52/53 week year: December
    assert worker.derive_company({**SEC, "tickers": ["EXMP", "EXMPB"]}, DECEMBER, "exmpb").ticker == "EXMPB"
    january = facts((2022, "2023-01-28"), (2023, "2024-02-03"))
    assert worker.derive_company({**SEC, "fiscalYearEnd": "0203"}, january) == worker.Derived("Example Corporation", "EXMP", 1, "start")
    september = facts((2024, "2024-09-28"), (2025, "2025-09-27"))
    assert worker.derive_company({**SEC, "fiscalYearEnd": "0927"}, september) == worker.Derived("Example Corporation", "EXMP", 9, "end")


def test_what_the_two_answers_do_not_settle_is_refused() -> None:
    for change, why in [({"fiscalYearEnd": None}, "fiscalYearEnd"), ({"fiscalYearEnd": "1340"}, "fiscalYearEnd"),
                        ({"tickers": []}, "no ticker"), ({"tickers": ["EXMP", "EXMPB"]}, "names none of them"), ({"name": ""}, "no name"),
                        ({"fiscalYearEnd": "0930"}, "month 12, the SEC submissions API says month 9")]:
        with pytest.raises(worker.NeedsSetup, match=why):
            worker.derive_company({**SEC, **change}, DECEMBER)
    with pytest.raises(worker.NeedsSetup, match="names none"):
        worker.derive_company({**SEC, "tickers": ["EXMP", "EXMPB"]}, DECEMBER, "OTHR")
    with pytest.raises(worker.NeedsSetup, match="2 are needed"):
        worker.derive_company(SEC, facts((2024, "2024-12-31")))


# --- asking the SEC -----------------------------------------------------------------------------------------------------


def test_the_sec_lookup_is_confined_to_the_two_apis() -> None:
    fetch = importlib.import_module("pipeline.01_fetch")
    client = worker.submissions_client()
    assert client.user_agent == common.EXPECTED_USER_AGENT
    assert client._check("https://data.sec.gov/submissions/CIK0000000123.json") == "data.sec.gov"
    assert client._check("https://data.sec.gov/api/xbrl/companyfacts/CIK0000000123.json") == "data.sec.gov"
    for url in ["https://data.sec.gov/api/xbrl/frames", "http://data.sec.gov/submissions/CIK0000000123.json", "https://example.com/submissions/x",
                "https://data.sec.gov/api/xbrl/companyconcept/CIK0000000123/us-gaap/Assets.json"]:
        with pytest.raises(fetch.EdgarError):
            client._check(url)


class _Client:
    def __init__(self, answers: dict[str, tuple[int, bytes]]) -> None:
        self.answers, self.asked = answers, []

    def get(self, url: str):
        from types import SimpleNamespace

        self.asked.append(url)
        status, content = self.answers[url]
        return SimpleNamespace(status=status, content=content)


def test_both_answers_are_fetched_with_the_one_client_and_a_missing_one_is_refused() -> None:
    cik = "0000000123"
    sub_url, facts_url = worker.SUBMISSIONS_URL.format(cik=cik), worker.FACTS_URL.format(cik=cik)
    assert facts_url == "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000123.json"
    client = _Client({sub_url: (200, json.dumps(SEC).encode()), facts_url: (200, json.dumps(DECEMBER).encode())})
    w = worker.Worker(db=None, caps={}, build=lambda *a: None, settings=object())
    w._edgar = client
    assert w._derive_from_sec(cik, None) == worker.Derived("Example Corporation", "EXMP", 12, "end")
    assert client.asked == [sub_url, facts_url]
    with pytest.raises(worker.NeedsSetup, match="no XBRL company facts"):
        worker.fetch_facts(cik, _Client({facts_url: (404, b"")}))
    with pytest.raises(importlib.import_module("pipeline.01_fetch").EdgarError):
        worker.fetch_facts(cik, _Client({facts_url: (500, b"")}))


def test_derive_prints_both_values_and_the_facts_they_came_from(monkeypatch, capsys) -> None:
    cik = COMPANIES[0].cik  # config.yaml's first company, from its fixture
    monkeypatch.setattr(worker, "submissions_client", lambda config=None: None)
    monkeypatch.setattr(worker, "fetch_submissions", lambda c, client: {"name": "Example Corporation", "tickers": [COMPANIES[0].ticker], "fiscalYearEnd": "0131"})
    monkeypatch.setattr(worker, "fetch_facts", lambda c, client: fixture(c))
    assert worker.main(["--derive", cik]) == 0
    out = capsys.readouterr().out
    fiscal = worker.derive_fiscal_year(fixture(cik))
    assert f"derived: fiscal_year_end_month={fiscal.end_month} fiscal_year_named_for={fiscal.named_for}" in out
    assert f"10-K facts (us-gaap {fiscal.concept}" in out and out.count(" accn=") == len(fiscal.facts)
    first = fiscal.facts[0]
    assert f"fy={first.fy} end={first.end.isoformat()} filed={first.filed} accn={first.accn} -> month {first.end_month}, named for {first.named_for}" in out
    assert "config.yaml has:" in out
