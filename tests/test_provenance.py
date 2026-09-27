"""Provenance.

The `network` tests make live requests and are deselected with `-m "not network"`:

- the SEC User-Agent smoke test from SCOPE 3.1: the SEC must accept the declared header and refuse a
  request without it.
- the SCOPE section 6 provenance check: 5 random rows from `data/release/assumption_drift.jsonl`,
  each one's `assumption.evidence.source_url` re-fetched live, its sha256 compared to the hash stored
  on the row, and its excerpt checked to still appear verbatim in the re-fetched text.

content_sha256 is a hash of the document's extracted text (research_record.text.html_to_text), not
its raw bytes: SEC injects a per-request script tag into the raw HTML, different on every fetch of
the same page, so a raw-byte hash could never match a later fetch. The extracted text is stable.

The cache test needs no network. It walks whatever is in data/raw/ and checks that every cached
document came from a 200 on an SEC host, its raw bytes still match raw_sha256 (the file on disk is
what was actually fetched), and its extracted text still matches content_sha256.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import random
from urllib.parse import urlsplit

import pytest

from pipeline import common
from research_record import stats
from research_record.schema import ALLOWED_HOSTS
from research_record.text import html_to_text

fetch = importlib.import_module("pipeline.01_fetch")


@pytest.fixture(scope="module")
def client():
    edgar = common.load_config()["edgar"]
    return fetch.EdgarClient(edgar["user_agent"], edgar["max_rps"])


def test_the_smoke_test_url_is_the_one_scope_names() -> None:
    assert fetch.SMOKE_URL == (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000320193&type=10-K&output=atom"
    )


@pytest.mark.network
def test_sec_accepts_the_declared_user_agent(client) -> None:
    got = client.get(fetch.SMOKE_URL)
    assert got.status == 200
    assert b"<feed" in got.content  # an Atom feed, not an error page


@pytest.mark.network
def test_sec_refuses_a_request_without_the_user_agent(client) -> None:
    got = client.get(fetch.SMOKE_URL, declare_user_agent=False)
    assert got.status == 403


def test_every_cached_document_came_from_a_200_on_an_sec_host_and_matches_its_hash() -> None:
    metas = sorted(common.RAW_DIR.glob("*/*.meta.json"))
    if not metas:
        pytest.skip("data/raw is empty (it is gitignored); run `python -m pipeline.01_fetch` first")

    problems: list[str] = []
    for meta_path in metas:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        accession = meta.get("accession", "?")
        html_path = meta_path.with_name(f"{accession}.html")
        if meta_path.name != f"{accession}.meta.json" or meta_path.parent.name != meta.get("cik"):
            problems.append(f"{meta_path}: sidecar is filed under the wrong cik or accession")
        if meta.get("http_status") != 200:
            problems.append(f"{accession}: http_status {meta.get('http_status')}")
        if urlsplit(meta.get("final_url", "")).hostname not in ALLOWED_HOSTS:
            problems.append(f"{accession}: final_url host not allowed: {meta.get('final_url')}")
        if not html_path.exists():
            problems.append(f"{accession}: no cached document")
        else:
            body = html_path.read_bytes()
            if hashlib.sha256(body).hexdigest() != meta.get("raw_sha256"):
                problems.append(f"{accession}: cached bytes do not match raw_sha256")
            if hashlib.sha256(html_to_text(body).encode("utf-8")).hexdigest() != meta.get("content_sha256"):
                problems.append(f"{accession}: extracted text does not match content_sha256")
    assert not problems, f"{len(problems)} problem(s):\n" + "\n".join(problems[:10])


@pytest.mark.network
def test_five_random_release_rows_match_a_live_refetch(client) -> None:
    """SCOPE section 6: 5 random release rows, re-fetched live, sha256 and excerpt checked against
    what is actually out there right now, not just against the cached copy."""
    release_path = common.REPO_ROOT / "data" / "release" / "assumption_drift.jsonl"
    if not release_path.exists():
        pytest.skip("data/release/assumption_drift.jsonl does not exist; run `python -m pipeline.06_publish` first")
    records, _ = stats.load_records(release_path)
    if not records:
        pytest.skip("data/release/assumption_drift.jsonl has no valid rows")

    sample = random.sample(records, min(5, len(records)))
    problems: list[str] = []
    for record in sample:
        evidence = record.assumption.evidence
        got = client.get(str(evidence.source_url))
        if got.status != 200:
            problems.append(f"{record.record_id}: refetch returned status {got.status}")
            continue
        live_text = html_to_text(got.content)
        if hashlib.sha256(live_text.encode("utf-8")).hexdigest() != evidence.content_sha256:
            problems.append(f"{record.record_id}: live sha256 does not match the stored content_sha256")
        if evidence.excerpt not in live_text:
            problems.append(f"{record.record_id}: excerpt no longer appears verbatim in the live document")
    assert not problems, f"{len(problems)} problem(s):\n" + "\n".join(problems)
