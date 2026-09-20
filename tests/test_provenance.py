"""Provenance.

The two `network` tests are the SEC User-Agent smoke test from SCOPE 3.1: the SEC
must accept the declared header and refuse a request without it. They make two
live requests. Deselect them with `-m "not network"`.

The cache test needs no network. It walks whatever is in data/raw/ and checks that
every cached document came from a 200 on an SEC host and still matches its hash.
The "re-fetch 5 random release rows and compare hashes" test arrives in Phase 3,
with the release data it needs.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from urllib.parse import urlsplit

import pytest

from pipeline import common
from research_record.schema import ALLOWED_HOSTS

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
        elif hashlib.sha256(html_path.read_bytes()).hexdigest() != meta.get("content_sha256"):
            problems.append(f"{accession}: cached bytes do not match content_sha256")
    assert not problems, f"{len(problems)} problem(s):\n" + "\n".join(problems[:10])
