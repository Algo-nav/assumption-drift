"""Resolution rules as pure functions (SCOPE.md section 2.2).

Resolution is mechanical. Every function here takes values and returns a value.
None of them read files, call a model, or make a judgment call.

The rules:

- met         reported value falls inside the target range, endpoints included
- missed      reported value falls outside it
- withdrawn   the company withdrew or suspended the guidance before the period closed
- unresolved  the period has not closed, or no later filing reports the metric
- open        draft row, not yet reviewed

Two of those five, `open` and `withdrawn`, are facts about the row rather than
about the numbers, so `resolve` takes them as flags. The caller establishes
them: `open` from the review queue, `withdrawn` from a later filing, with
`withdrawn_before_close` available to date check it.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from research_record.schema import ResearchRecord, Status

__all__ = [
    "POINT_TOLERANCE",
    "is_point_guidance",
    "in_range",
    "direction",
    "resolve",
    "resolve_record",
    "withdrawn_before_close",
    "days_to_falsifiable",
    "days_to_acknowledged",
]

Direction = Literal["beat", "shortfall"]

#: Point guidance (low == high) is met within 0.5 percent of the stated value.
POINT_TOLERANCE = 0.005

#: Guards float representation at the endpoints of a range. Not a tolerance:
#: a range is inclusive, and this only keeps 3.3 from landing outside [1.1, 3.3].
_REL_EPS = 1e-9


def _slack(bound: float) -> float:
    return _REL_EPS * max(abs(bound), 1.0)


def is_point_guidance(target_low: float | None, target_high: float | None) -> bool:
    """True when the company named a single number rather than a range."""
    return target_low is not None and target_high is not None and target_low == target_high


def in_range(
    target_low: float | None,
    target_high: float | None,
    reported_value: float | None,
) -> bool | None:
    """Compare a reported value to a target.

    Returns True (inside), False (outside), or None when there is nothing to
    compare: no reported value, or no numeric target at either end.

    A one sided target resolves against the side that was given. "at least
    500" is met by 500 or more; "no more than 500" is met by 500 or less.
    """
    if reported_value is None:
        return None
    if target_low is None and target_high is None:
        return None

    if is_point_guidance(target_low, target_high):
        target = float(target_low)  # type: ignore[arg-type]
        tolerance = POINT_TOLERANCE * abs(target)
        return abs(reported_value - target) <= tolerance

    if target_low is not None and reported_value < target_low - _slack(target_low):
        return False
    if target_high is not None and reported_value > target_high + _slack(target_high):
        return False
    return True


def direction(
    target_low: float | None,
    target_high: float | None,
    reported_value: float | None,
) -> Direction | None:
    """Which side of the range a missed value landed on: "beat" above target_high, "shortfall" below
    target_low. None when the value is inside the range (or there is nothing to compare): it is not a
    property of a "missed" row alone, so a caller checks that separately if it needs to.

    Not stored on the record: the schema has no direction field, so callers compute this from the numbers
    each time, as `rr stats` does for its beat/shortfall split and `04_outcomes.py` does to pick which
    acknowledgement vocabulary to search with."""
    if in_range(target_low, target_high, reported_value) is not False:
        return None
    if target_high is not None and reported_value > target_high + _slack(target_high):  # type: ignore[operator]
        return "beat"
    if target_low is not None and reported_value < target_low - _slack(target_low):  # type: ignore[operator]
        return "shortfall"
    return None


def withdrawn_before_close(withdrawn_at: date | None, period_end: date | None) -> bool:
    """True when the guidance was pulled on or before the last day of the period.

    A withdrawal on the final day of the period still lands inside the period.
    A withdrawal after the period closed is not a withdrawal under this rubric:
    the number was already falsifiable by then, so the row resolves on the
    numbers instead.
    """
    if withdrawn_at is None or period_end is None:
        return False
    return withdrawn_at <= period_end


def resolve(
    target_low: float | None,
    target_high: float | None,
    reported_value: float | None,
    *,
    reviewed: bool = True,
    period_closed: bool = True,
    withdrawn: bool = False,
) -> Status:
    """Return the status for one row.

    Precedence, highest first: not reviewed, withdrawn, period still open or no
    reported value, then the comparison itself.
    """
    if not reviewed:
        return "open"
    if withdrawn:
        return "withdrawn"
    if not period_closed:
        return "unresolved"

    verdict = in_range(target_low, target_high, reported_value)
    if verdict is None:
        return "unresolved"
    return "met" if verdict else "missed"


def resolve_record(
    record: ResearchRecord,
    *,
    reviewed: bool | None = None,
    period_closed: bool = True,
    withdrawn: bool | None = None,
) -> Status:
    """Run `resolve` over the values carried by a record.

    `open` and `withdrawn` are not derivable from the numbers, so by default
    they are read off the record's own status and the rest is recomputed. That
    is what lets `rr validate` check a stored status against the values without
    the two flag states making the check circular.
    """
    if reviewed is None:
        reviewed = record.status != "open"
    if withdrawn is None:
        withdrawn = record.status == "withdrawn"

    reported_value = record.outcome.reported_value if record.outcome is not None else None
    return resolve(
        record.assumption.target_low,
        record.assumption.target_high,
        reported_value,
        reviewed=reviewed,
        period_closed=period_closed,
        withdrawn=withdrawn,
    )


def days_to_falsifiable(stated_at: date, reported_at: date | None) -> int | None:
    """Days from the guidance to the filing that reported the metric.

    Measured to the reporting date, not to the end of the period. The gap
    between those two is the point: the number is not checkable until someone
    files it.
    """
    if reported_at is None:
        return None
    return (reported_at - stated_at).days


def days_to_acknowledged(reported_at: date | None, acknowledged_at: date | None) -> int | None:
    """Days from the report to the first filing that references the gap.

    None when the company never acknowledged it, which is itself a reading.
    """
    if reported_at is None or acknowledged_at is None:
        return None
    return (acknowledged_at - reported_at).days
