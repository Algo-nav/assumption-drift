"""Edge cases for the resolution rubric (SCOPE.md section 2.2)."""

from __future__ import annotations

from datetime import date

import pytest

from research_record import rubric
from research_record.schema import ResearchRecord


# --- boundary values -------------------------------------------------------


@pytest.mark.parametrize(
    "reported, expected",
    [
        (4_900.0, "missed"),   # just under the low end
        (5_000.0, "met"),      # exactly the low end, inclusive
        (5_500.0, "met"),
        (6_000.0, "met"),      # exactly the high end, inclusive
        (6_000.01, "missed"),  # just over the high end
    ],
)
def test_range_endpoints_are_inclusive(reported: float, expected: str) -> None:
    assert rubric.resolve(5_000.0, 6_000.0, reported) == expected


def test_float_representation_does_not_push_a_value_off_the_endpoint() -> None:
    # 1.1 + 2.2 == 3.3000000000000003, which is outside [1.1, 3.3] by raw comparison.
    assert rubric.resolve(1.1, 3.3, 1.1 + 2.2) == "met"


def test_negative_range_resolves_the_same_way() -> None:
    # Loss per share guidance of -0.50 to -0.40.
    assert rubric.resolve(-0.50, -0.40, -0.45) == "met"
    assert rubric.resolve(-0.50, -0.40, -0.51) == "missed"
    assert rubric.resolve(-0.50, -0.40, -0.39) == "missed"


# --- point guidance --------------------------------------------------------


def test_point_guidance_is_detected() -> None:
    assert rubric.is_point_guidance(2.50, 2.50) is True
    assert rubric.is_point_guidance(2.40, 2.50) is False
    assert rubric.is_point_guidance(2.50, None) is False
    assert rubric.is_point_guidance(None, None) is False


@pytest.mark.parametrize(
    "reported, expected",
    [
        (100.0, "met"),     # exact
        (100.5, "met"),     # exactly 0.5 percent above
        (99.5, "met"),      # exactly 0.5 percent below
        (100.51, "missed"),
        (99.49, "missed"),
    ],
)
def test_point_guidance_tolerance_is_half_a_percent(reported: float, expected: str) -> None:
    assert rubric.resolve(100.0, 100.0, reported) == expected


def test_point_tolerance_scales_with_the_value() -> None:
    # 0.5 percent of 2.50 is 0.0125.
    assert rubric.resolve(2.50, 2.50, 2.5124) == "met"
    assert rubric.resolve(2.50, 2.50, 2.52) == "missed"


def test_point_guidance_of_zero_requires_an_exact_match() -> None:
    assert rubric.resolve(0.0, 0.0, 0.0) == "met"
    assert rubric.resolve(0.0, 0.0, 0.01) == "missed"


def test_negative_point_guidance_uses_the_absolute_value_for_tolerance() -> None:
    # 0.5 percent of -2.00 is 0.01 either side.
    assert rubric.resolve(-2.00, -2.00, -2.01) == "met"
    assert rubric.resolve(-2.00, -2.00, -2.02) == "missed"


def test_a_range_gets_no_tolerance() -> None:
    # The half percent is for point guidance only. A range says what it says.
    assert rubric.resolve(5_000.0, 6_000.0, 6_010.0) == "missed"


# --- one sided targets -----------------------------------------------------


def test_at_least_guidance_resolves_on_the_low_end() -> None:
    assert rubric.resolve(5_000.0, None, 5_000.0) == "met"
    assert rubric.resolve(5_000.0, None, 9_000.0) == "met"
    assert rubric.resolve(5_000.0, None, 4_999.0) == "missed"


def test_no_more_than_guidance_resolves_on_the_high_end() -> None:
    assert rubric.resolve(None, 6_000.0, 6_000.0) == "met"
    assert rubric.resolve(None, 6_000.0, 10.0) == "met"
    assert rubric.resolve(None, 6_000.0, 6_001.0) == "missed"


# --- withdrawn -------------------------------------------------------------


def test_withdrawn_before_close_is_a_withdrawal() -> None:
    assert rubric.withdrawn_before_close(date(2020, 4, 30), date(2020, 12, 31)) is True


def test_withdrawn_on_the_last_day_of_the_period_still_counts() -> None:
    assert rubric.withdrawn_before_close(date(2020, 12, 31), date(2020, 12, 31)) is True


def test_withdrawn_after_the_period_closed_is_not_a_withdrawal() -> None:
    assert rubric.withdrawn_before_close(date(2021, 1, 1), date(2020, 12, 31)) is False


def test_withdrawn_before_close_needs_both_dates() -> None:
    assert rubric.withdrawn_before_close(None, date(2020, 12, 31)) is False
    assert rubric.withdrawn_before_close(date(2020, 4, 30), None) is False


def test_withdrawn_outranks_the_numbers() -> None:
    # The company pulled the guidance, so the later report does not resolve it.
    assert rubric.resolve(5_000.0, 6_000.0, 4_000.0, withdrawn=True) == "withdrawn"
    assert rubric.resolve(5_000.0, 6_000.0, 5_500.0, withdrawn=True) == "withdrawn"


def test_a_row_withdrawn_after_the_period_closed_resolves_on_the_numbers() -> None:
    withdrawn = rubric.withdrawn_before_close(date(2021, 2, 1), date(2020, 12, 31))
    assert rubric.resolve(5_000.0, 6_000.0, 4_000.0, withdrawn=withdrawn) == "missed"


# --- unresolved and open ---------------------------------------------------


def test_period_not_closed_is_unresolved() -> None:
    assert rubric.resolve(5_000.0, 6_000.0, None, period_closed=False) == "unresolved"


def test_no_later_filing_reports_the_metric_is_unresolved() -> None:
    assert rubric.resolve(5_000.0, 6_000.0, None) == "unresolved"


def test_no_numeric_target_is_unresolved() -> None:
    # "strong growth" is not a row, but if one reaches the rubric it does not resolve.
    assert rubric.resolve(None, None, 5_500.0) == "unresolved"


def test_a_reported_value_before_the_period_closes_does_not_resolve() -> None:
    assert rubric.resolve(5_000.0, 6_000.0, 5_500.0, period_closed=False) == "unresolved"


def test_an_unreviewed_row_is_open_whatever_the_numbers_say() -> None:
    assert rubric.resolve(5_000.0, 6_000.0, 5_500.0, reviewed=False) == "open"
    assert rubric.resolve(5_000.0, 6_000.0, 4_000.0, reviewed=False) == "open"
    assert rubric.resolve(5_000.0, 6_000.0, 4_000.0, reviewed=False, withdrawn=True) == "open"


def test_in_range_returns_none_when_there_is_nothing_to_compare() -> None:
    assert rubric.in_range(5_000.0, 6_000.0, None) is None
    assert rubric.in_range(None, None, 5_500.0) is None
    assert rubric.in_range(None, None, None) is None


# --- day counts ------------------------------------------------------------


def test_days_to_falsifiable_counts_to_the_reporting_date() -> None:
    # Guidance given at the Q1 release, FY reported the following February.
    stated = date(2023, 4, 27)
    reported = date(2024, 2, 1)
    assert rubric.days_to_falsifiable(stated, reported) == 280


def test_outcome_after_the_period_but_before_the_filing() -> None:
    # The period closed on 2023-12-31 but nobody could check the number until
    # the 10-K landed on 2024-02-01. The count runs to the filing, not the close.
    stated = date(2023, 4, 27)
    period_end = date(2023, 12, 31)
    reported = date(2024, 2, 1)
    days = rubric.days_to_falsifiable(stated, reported)
    assert days == 280
    assert days > (period_end - stated).days


def test_days_to_falsifiable_is_none_without_an_outcome() -> None:
    assert rubric.days_to_falsifiable(date(2023, 4, 27), None) is None


def test_days_to_acknowledged_counts_from_the_report() -> None:
    assert rubric.days_to_acknowledged(date(2024, 2, 1), date(2024, 5, 2)) == 91


def test_acknowledged_in_the_same_filing_is_zero_days() -> None:
    assert rubric.days_to_acknowledged(date(2024, 2, 1), date(2024, 2, 1)) == 0


def test_days_to_acknowledged_is_none_when_never_acknowledged() -> None:
    assert rubric.days_to_acknowledged(date(2024, 2, 1), None) is None
    assert rubric.days_to_acknowledged(None, None) is None


# --- over a whole record ---------------------------------------------------


def test_resolve_record_recomputes_a_miss(record_data) -> None:
    record = ResearchRecord(**record_data())
    assert rubric.resolve_record(record) == "missed" == record.status


def test_resolve_record_recomputes_a_met_row(record_data) -> None:
    payload = record_data(status="met")
    payload["outcome"]["reported_value"] = 5_500.0
    record = ResearchRecord(**payload)
    assert rubric.resolve_record(record) == "met"


def test_resolve_record_catches_a_status_that_does_not_match_the_numbers(record_data) -> None:
    # Stored as met, but 4800 is below the 5000 low end. This is the check
    # `rr validate` runs in Phase 3.
    record = ResearchRecord(**record_data(status="met"))
    assert rubric.resolve_record(record) != record.status


def test_resolve_record_reads_open_and_withdrawn_off_the_stored_status(record_data) -> None:
    for status in ("open", "withdrawn"):
        record = ResearchRecord(**record_data(status=status))
        assert rubric.resolve_record(record) == status


def test_resolve_record_without_an_outcome_is_unresolved(record_data) -> None:
    record = ResearchRecord(
        **record_data(outcome=None, status="unresolved", days_to_falsifiable=None)
    )
    assert rubric.resolve_record(record) == "unresolved"


def test_record_day_counts_agree_with_the_rubric(record_data) -> None:
    record = ResearchRecord(**record_data())
    assert record.outcome is not None
    assert record.days_to_falsifiable == rubric.days_to_falsifiable(
        record.assumption.stated_at, record.outcome.reported_at
    )
    assert record.days_to_acknowledged == rubric.days_to_acknowledged(
        record.outcome.reported_at, record.acknowledged_at
    )
