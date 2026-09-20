"""Shape constraints on the record models (SCOPE.md section 2.1)."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from research_record.schema import Assumption, Evidence, ResearchRecord


def test_a_full_record_parses(record_data) -> None:
    record = ResearchRecord(**record_data())
    assert record.ticker == "EXMP"
    assert record.status == "missed"
    assert record.outcome is not None
    assert record.outcome.reported_value == 4_800.0


def test_a_record_without_an_outcome_parses(record_data) -> None:
    record = ResearchRecord(
        **record_data(
            outcome=None,
            status="unresolved",
            days_to_falsifiable=None,
        )
    )
    assert record.outcome is None


def test_the_schema_is_closed(record_data) -> None:
    # No confidence scores, no cross record links, no extra judgment fields.
    with pytest.raises(ValidationError):
        ResearchRecord(**record_data(confidence=0.9))


# --- evidence --------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.sec.gov/Archives/edgar/data/123/000000012324000001/example-8k.htm",
        "https://efts.sec.gov/LATEST/search-index?q=guidance",
    ],
)
def test_sec_hosts_are_accepted(evidence_data, url: str) -> None:
    assert Evidence(**evidence_data(source_url=url)).source_url.host in {
        "www.sec.gov",
        "efts.sec.gov",
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/filing.htm",
        "https://sec.gov.evil.example/filing.htm",
        "https://ir.examplecorp.com/press-release.htm",
    ],
)
def test_non_sec_hosts_are_rejected(evidence_data, url: str) -> None:
    with pytest.raises(ValidationError):
        Evidence(**evidence_data(source_url=url))


@pytest.mark.parametrize(
    "accession",
    ["0000000123-24-00001", "000000123-24-000001", "0000000123-2024-000001", "not-an-accession"],
)
def test_malformed_accession_numbers_are_rejected(evidence_data, accession: str) -> None:
    with pytest.raises(ValidationError):
        Evidence(**evidence_data(accession_number=accession))


def test_excerpt_is_capped_at_400_characters(evidence_data) -> None:
    Evidence(**evidence_data(excerpt="x" * 400))
    with pytest.raises(ValidationError):
        Evidence(**evidence_data(excerpt="x" * 401))


def test_excerpt_must_not_be_empty(evidence_data) -> None:
    with pytest.raises(ValidationError):
        Evidence(**evidence_data(excerpt=""))


def test_content_hash_must_be_sha256(evidence_data) -> None:
    Evidence(**evidence_data(content_sha256="A" * 64))  # normalised to lower case
    with pytest.raises(ValidationError):
        Evidence(**evidence_data(content_sha256="abc123"))
    with pytest.raises(ValidationError):
        Evidence(**evidence_data(content_sha256="z" * 64))


def test_content_hash_is_stored_lower_case(evidence_data) -> None:
    assert Evidence(**evidence_data(content_sha256="A" * 64)).content_sha256 == "a" * 64


def test_filing_type_is_stored_upper_case(evidence_data) -> None:
    assert Evidence(**evidence_data(filing_type="10-k")).filing_type == "10-K"


# --- assumption ------------------------------------------------------------


def test_target_range_must_be_ordered(record_data) -> None:
    payload = record_data()["assumption"]
    payload.update(target_low=6_000.0, target_high=5_000.0)
    with pytest.raises(ValidationError):
        Assumption(**payload)


def test_point_guidance_is_a_valid_range(record_data) -> None:
    payload = record_data()["assumption"]
    payload.update(target_low=2.50, target_high=2.50)
    assert Assumption(**payload).target_low == 2.50


def test_one_sided_targets_parse(record_data) -> None:
    payload = record_data()["assumption"]
    payload.update(target_low=5_000.0, target_high=None)
    assert Assumption(**payload).target_high is None


def test_nan_is_not_a_target(record_data) -> None:
    payload = record_data()["assumption"]
    payload.update(target_low=float("nan"), target_high=float("inf"))
    with pytest.raises(ValidationError):
        Assumption(**payload)


# --- identifiers -----------------------------------------------------------


def test_cik_is_padded_to_ten_digits(record_data) -> None:
    assert ResearchRecord(**record_data(cik="320193")).cik == "0000320193"


def test_cik_must_be_digits(record_data) -> None:
    with pytest.raises(ValidationError):
        ResearchRecord(**record_data(cik="CIK0000320193"))


def test_record_id_must_be_a_ulid(record_data) -> None:
    with pytest.raises(ValidationError):
        ResearchRecord(**record_data(record_id="not-a-ulid"))
    with pytest.raises(ValidationError):
        # I, L, O and U are not in Crockford base32.
        ResearchRecord(**record_data(record_id="01HZY8Q9XMR3T7VBN2CDEFGHJU"))


def test_record_id_is_stored_upper_case(record_data) -> None:
    lower = "01hzy8q9xmr3t7vbn2cdefghjk"
    assert ResearchRecord(**record_data(record_id=lower)).record_id == lower.upper()


@pytest.mark.parametrize("ticker", ["EXMP", "BRK.B", "A"])
def test_tickers_parse(record_data, ticker: str) -> None:
    assert ResearchRecord(**record_data(ticker=ticker)).ticker == ticker


def test_ticker_is_stored_upper_case(record_data) -> None:
    assert ResearchRecord(**record_data(ticker="exmp")).ticker == "EXMP"


def test_status_is_closed(record_data) -> None:
    with pytest.raises(ValidationError):
        ResearchRecord(**record_data(status="probably missed"))


# --- acknowledgement -------------------------------------------------------


def test_acknowledgement_date_needs_evidence(record_data, evidence_data) -> None:
    with pytest.raises(ValidationError):
        ResearchRecord(**record_data(acknowledged_at=date(2025, 5, 1), days_to_acknowledged=87))


def test_acknowledgement_evidence_needs_a_date(record_data, evidence_data) -> None:
    with pytest.raises(ValidationError):
        ResearchRecord(**record_data(acknowledgement_evidence=evidence_data()))


def test_an_acknowledged_miss_parses(record_data, evidence_data) -> None:
    record = ResearchRecord(
        **record_data(
            acknowledged_at=date(2025, 5, 1),
            acknowledgement_evidence=evidence_data(
                accession_number="0000000123-25-000009",
                filing_type="10-Q",
                filed_at=date(2025, 5, 1),
                content_sha256="c" * 64,
                excerpt="Full year 2024 revenue came in below the range we guided to in February.",
            ),
            days_to_acknowledged=87,
        )
    )
    assert record.acknowledged_at == date(2025, 5, 1)
    assert record.acknowledgement_evidence is not None
