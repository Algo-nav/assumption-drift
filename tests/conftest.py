"""Shared fixtures.

The record built here is synthetic. It is shaped like a real row but it names
no real company and points at no real document, so nothing in the test suite
can be mistaken for a published row.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64

_BASE_URL = "https://www.sec.gov/Archives/edgar/data/123/000000012324000001"


def _evidence(**overrides: Any) -> dict[str, Any]:
    evidence = {
        "source_url": f"{_BASE_URL}/example-8k.htm",
        "accession_number": "0000000123-24-000001",
        "filing_type": "8-K",
        "filed_at": date(2024, 2, 1),
        "fetched_at": datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
        "content_sha256": SHA_A,
        "excerpt": "The Company expects full year 2024 revenue of $5.0 billion to $6.0 billion.",
    }
    evidence.update(overrides)
    return evidence


@pytest.fixture
def evidence_data():
    return _evidence


@pytest.fixture
def record_data():
    """Returns a factory for a valid ResearchRecord payload, with overrides applied at the top level."""

    def _record(**overrides: Any) -> dict[str, Any]:
        record = {
            "record_id": "01HZY8Q9XMR3T7VBN2CDEFGHJK",
            "company": "Example Corporation",
            "ticker": "EXMP",
            "cik": "0000000123",
            "claim": "Example Corporation expects FY2024 revenue between 5.0 and 6.0 billion USD.",
            "assumption": {
                "text": "We expect full year 2024 revenue of $5.0 billion to $6.0 billion.",
                "metric": "revenue",
                "target_low": 5_000.0,
                "target_high": 6_000.0,
                "unit": "USD millions",
                "target_period": "FY2024",
                "stated_at": date(2024, 2, 1),
                "evidence": _evidence(),
            },
            "outcome": {
                "reported_value": 4_800.0,
                "reported_at": date(2025, 2, 3),
                "evidence": _evidence(
                    source_url=f"{_BASE_URL}/example-10k.htm",
                    accession_number="0000000123-25-000004",
                    filing_type="10-K",
                    filed_at=date(2025, 2, 3),
                    content_sha256=SHA_B,
                    excerpt="Full year 2024 revenue was $4.8 billion.",
                ),
            },
            "invalidation_condition": "reported value falls outside [5000.0, 6000.0]",
            "status": "missed",
            "acknowledged_at": None,
            "acknowledgement_evidence": None,
            "days_to_falsifiable": 368,
            "days_to_acknowledged": None,
            "last_reviewed_at": date(2026, 9, 20),
            "reviewer": "navneet",
        }
        record.update(overrides)
        return record

    return _record
