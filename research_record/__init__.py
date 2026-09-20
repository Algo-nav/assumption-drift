"""research_record: a minimal schema for a written down research conclusion.

The schema says what a conclusion must carry to be checkable later: the claim,
the number, the period, the filing it came from, what was reported, and when
the gap was acknowledged. `rubric` resolves a row from those values.
"""

from research_record.schema import (
    ALLOWED_HOSTS,
    STATUSES,
    Assumption,
    Evidence,
    Outcome,
    ResearchRecord,
    Status,
)

__all__ = [
    "ALLOWED_HOSTS",
    "STATUSES",
    "Assumption",
    "Evidence",
    "Outcome",
    "ResearchRecord",
    "Status",
]

__version__ = "0.0.1"
