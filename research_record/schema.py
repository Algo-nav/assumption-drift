"""Pydantic models for a written down research conclusion.

The field set is fixed by SCOPE.md section 2.1. Nothing is added here without
being told to: no cross record links, no contradiction fields, no confidence
scores, no free text judgment field beyond `claim`.

Constraints in this module are shape constraints only: formats, bounds, and
consistency that lives inside a single model. Cross model date ordering and
anything that needs the cached document on disk belongs to `rr validate`
(SCOPE.md section 5.1), not here.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

__all__ = [
    "ALLOWED_HOSTS",
    "Status",
    "STATUSES",
    "Evidence",
    "Assumption",
    "Outcome",
    "ResearchRecord",
]

#: Only documents fetched from these hosts may back a row.
ALLOWED_HOSTS: frozenset[str] = frozenset({"www.sec.gov", "efts.sec.gov"})

Status = Literal["open", "met", "missed", "withdrawn", "unresolved"]

#: Same values as `Status`, available at runtime for iteration and tests.
STATUSES: tuple[str, ...] = ("open", "met", "missed", "withdrawn", "unresolved")

# 0000000000-00-000000
AccessionNumber = Annotated[str, Field(pattern=r"^\d{10}-\d{2}-\d{6}$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
# Canonical ULID: 26 characters of Crockford base32, first character 0-7.
Ulid = Annotated[str, Field(pattern=r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")]
NonEmpty = Annotated[str, Field(min_length=1)]
#: A target or a reported value. NaN and infinity are not numbers a company files.
Number = Annotated[float, Field(allow_inf_nan=False)]
Excerpt = Annotated[str, Field(min_length=1, max_length=400)]


class _Base(BaseModel):
    """Shared configuration. `extra="forbid"` is what keeps the schema fence."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Evidence(_Base):
    """One fetched SEC document and the exact words taken from it."""

    source_url: HttpUrl
    accession_number: AccessionNumber
    filing_type: NonEmpty  # 10-K, 10-Q, 8-K
    filed_at: date
    fetched_at: datetime
    content_sha256: Sha256  # sha256 of the extracted text (research_record.text.html_to_text), not the raw HTML
    excerpt: Excerpt  # the exact sentence(s), max 400 chars

    @field_validator("source_url")
    @classmethod
    def _host_is_sec(cls, url: HttpUrl) -> HttpUrl:
        if url.host not in ALLOWED_HOSTS:
            allowed = ", ".join(sorted(ALLOWED_HOSTS))
            raise ValueError(f"source_url host must be one of {allowed}, got {url.host!r}")
        return url

    @field_validator("filing_type")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("content_sha256", mode="before")
    @classmethod
    def _lower_hash(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class Assumption(_Base):
    """A numeric forward guidance statement, as the company stated it."""

    text: NonEmpty
    metric: NonEmpty  # e.g. "revenue", "non-GAAP EPS", "gross margin"
    target_low: Number | None
    target_high: Number | None
    unit: NonEmpty  # "USD", "USD millions", "percent", "units"
    target_period: NonEmpty  # "FY2024", "Q3 2024"
    stated_at: date
    evidence: Evidence

    @model_validator(mode="after")
    def _range_is_ordered(self) -> "Assumption":
        low, high = self.target_low, self.target_high
        if low is not None and high is not None and low > high:
            raise ValueError(f"target_low ({low}) must not exceed target_high ({high})")
        return self


class Outcome(_Base):
    """What the company later reported for that metric and period."""

    reported_value: Number | None
    reported_at: date
    evidence: Evidence


class ResearchRecord(_Base):
    """One guidance statement, its outcome, and whether the gap was acknowledged."""

    record_id: Ulid
    company: NonEmpty
    ticker: Annotated[str, Field(pattern=r"^[A-Z0-9.\-]{1,10}$")]
    cik: Annotated[str, Field(pattern=r"^\d{10}$")]
    claim: NonEmpty  # plain restatement of the guidance
    assumption: Assumption
    outcome: Outcome | None
    invalidation_condition: NonEmpty  # "reported value falls outside [low, high]"
    status: Status
    acknowledged_at: date | None  # first later filing that references the miss
    acknowledgement_evidence: Evidence | None
    days_to_falsifiable: int | None
    days_to_acknowledged: int | None
    last_reviewed_at: date
    reviewer: NonEmpty  # "navneet"

    @field_validator("record_id", mode="before")
    @classmethod
    def _upper_ulid(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("ticker", mode="before")
    @classmethod
    def _upper_ticker(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("cik", mode="before")
    @classmethod
    def _pad_cik(cls, value: object) -> object:
        """EDGAR writes CIKs zero padded to ten digits. Accept 320193, store 0000320193."""
        if isinstance(value, int):
            value = str(value)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.isdigit():
                return stripped.zfill(10)
        return value

    @model_validator(mode="after")
    def _acknowledgement_is_paired(self) -> "ResearchRecord":
        """An acknowledgement date without a filing behind it is not provenance."""
        has_date = self.acknowledged_at is not None
        has_evidence = self.acknowledgement_evidence is not None
        if has_date != has_evidence:
            raise ValueError(
                "acknowledged_at and acknowledgement_evidence must both be set or both be null"
            )
        return self
