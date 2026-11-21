"""The "it is live" email for a company request, sent through the Resend HTTPS API.

    RESEND_API_KEY    the Resend key. Unset: nothing is sent, the email is written to the log instead
    NOTIFY_FROM       the sender (default "SCND Order <pilot@scndorder.com>")
    PILOT_BASE_URL    the site the link points at (default https://pilot.scndorder.com)
    NOTIFY_DRY_RUN    1: nothing is sent, the email is written to the log instead
    NOTIFY_MAX_PER_ADDRESS_PER_DAY   how many emails one address may get in a UTC day (default 5)

The body is plain text and has one shape. Every value in it is read from a database row: the company's name and ticker
from `companies`, the counts and filing dates from `statements` and `filings` (research_record.store.email_counts), the
day of the request from `company_requests.created_at`. Nothing a visitor typed is put in an email, because the address
on a request is not verified: whoever receives it may not be the person who asked.

The sentence saying the rows had no human review is itself a claim about rows. It is in the email when the company has
machine-tier rows (statements.review_tier = 'machine') and left out when every counted row was approved by a person.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import timezone
from typing import Any, Callable

import requests

RESEND_URL = "https://api.resend.com/emails"
DEFAULT_FROM = "SCND Order <pilot@scndorder.com>"
DEFAULT_BASE_URL = "https://pilot.scndorder.com"
DEFAULT_MAX_PER_ADDRESS_PER_DAY = 5

#: One address, nothing else: no spaces, no control characters, no commas, no angle brackets, no second address.
_ADDRESS = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")

MACHINE_TIER_NOTE = ("These rows were produced by the pipeline without a human review pass. Each row links to the filing it came "
                     "from; check the filing before relying on a row.")


class NotifyError(Exception):
    """The email was not sent. `permanent`: sending the same thing again would fail the same way."""

    def __init__(self, message: str, *, permanent: bool) -> None:
        super().__init__(message)
        self.permanent = permanent


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    sender: str
    base_url: str
    dry_run: bool
    max_per_address_per_day: int

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        api_key = (env.get("RESEND_API_KEY") or "").strip() or None
        raw = (env.get("NOTIFY_MAX_PER_ADDRESS_PER_DAY") or "").strip()
        try:
            limit = int(raw) if raw else DEFAULT_MAX_PER_ADDRESS_PER_DAY
        except ValueError as exc:
            raise ValueError(f"NOTIFY_MAX_PER_ADDRESS_PER_DAY must be a whole number, got {raw!r}") from exc
        return cls(api_key=api_key, sender=(env.get("NOTIFY_FROM") or "").strip() or DEFAULT_FROM,
                   base_url=((env.get("PILOT_BASE_URL") or "").strip() or DEFAULT_BASE_URL).rstrip("/"),
                   dry_run=(env.get("NOTIFY_DRY_RUN") or "").strip() == "1" or api_key is None,
                   max_per_address_per_day=limit)

    @property
    def why_dry(self) -> str:
        return "RESEND_API_KEY is not set" if self.api_key is None else "NOTIFY_DRY_RUN=1"


def valid_address(email: str) -> bool:
    return len(email) <= 254 and bool(_ADDRESS.match(email))


def masked(email: str) -> str:
    """The address as the log shows it: the first character and the domain."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def build_email(company: dict[str, Any], counts: dict[str, Any], request: dict[str, Any], base_url: str) -> tuple[str, str]:
    """(subject, body). `company` is the companies row, `counts` is store.email_counts for it, `request` the
    company_requests row. Raises ValueError when there is nothing to state: no counted statement, or no filing date."""
    if not counts["n_statements"] or counts["first_filing_date"] is None or counts["last_filing_date"] is None:
        raise ValueError(f"{company['ticker']} has no statement to tell anyone about")
    ticker, name = company["ticker"], company["name"]
    asked = request["created_at"].astimezone(timezone.utc).date().isoformat()
    paragraphs = [
        f"{name} ({ticker}) is built.",
        f"{counts['n_statements']} forward-looking statements from {counts['n_filings']} filings, "
        f"{counts['first_filing_date'].isoformat()} to {counts['last_filing_date'].isoformat()}.\n"
        f"{counts['n_resolved']} have a resolution; {counts['n_open']} are still open.",
        f"{base_url.rstrip('/')}/company/{ticker.lower()}",
    ]
    if counts["n_machine"]:
        paragraphs.append(MACHINE_TIER_NOTE)
    paragraphs.append(f"You asked for this company on {asked}. Reply to this email if something looks wrong.")
    return f"{ticker} is live on assumption-drift", "\n\n".join(paragraphs) + "\n"


def send(to: str, subject: str, body: str, *, settings: Settings, idempotency_key: str | None = None,
         post: Callable[..., Any] = requests.post) -> str:
    """Send one plain-text email through Resend and return its message id. One recipient, no HTML."""
    if not valid_address(to):
        raise NotifyError("the address is not one plain email address", permanent=True)
    if settings.api_key is None:
        raise NotifyError("RESEND_API_KEY is not set", permanent=True)
    headers = {"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key  # a retry of the same request is not a second email
    try:
        resp = post(RESEND_URL, headers=headers, json={"from": settings.sender, "to": [to], "subject": subject, "text": body}, timeout=30)
    except requests.RequestException as exc:
        raise NotifyError(f"Resend could not be reached ({type(exc).__name__})", permanent=False) from exc
    if resp.status_code >= 400:
        # 429 and 5xx may pass; any other 4xx is about this request (the address, the sender, the key) and will not.
        raise NotifyError(f"Resend answered HTTP {resp.status_code}", permanent=resp.status_code < 500 and resp.status_code != 429)
    try:
        message_id = resp.json()["id"]
    except (ValueError, KeyError, TypeError) as exc:
        raise NotifyError("Resend answered without a message id", permanent=False) from exc
    return str(message_id)
