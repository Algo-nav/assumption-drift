"""data/manifest.json (pipeline/manifest.py) and 01_fetch --since-manifest. No network: discover is stubbed and the SEC is a fake session."""

from __future__ import annotations

import importlib
import json
from datetime import date, timedelta

import pytest
import yaml

from pipeline import common, manifest as mf
from test_fetch import (ACC_8K_EX99, COMPANY, FakeResponse, index_html, index_url, make_client, raw_dir)  # noqa: F401  (raw_dir is a fixture)

fetch = importlib.import_module("pipeline.01_fetch")

A, B, C = "0000000123-26-000001", "0000000123-26-000002", "0000000123-26-000003"
CIK = COMPANY.cik
OTHER = "0000000456"


def state(d: str, *accessions: str) -> mf.CompanyState:
    return mf.CompanyState(date.fromisoformat(d), tuple(sorted(accessions)))


def manifest(**companies: mf.CompanyState) -> mf.Manifest:
    return mf.Manifest(date(2026, 9, 20), {CIK if k == "a" else OTHER: v for k, v in companies.items()})


def filing(accession: str, filed: str, status: str = "fetched", cik: str = CIK, form: str = "8-K") -> dict:
    return {"cik": cik, "accession": accession, "form": form, "filing_date": filed, "status": status,
            "path": f"data/raw/{cik}/{accession}.html" if status in ("fetched", "cached") else None}


# --- the file --------------------------------------------------------------------------------------------------------------


def test_save_then_load_round_trips_and_the_file_is_sorted_and_human_readable(tmp_path) -> None:
    path = tmp_path / "manifest.json"
    original = manifest(b=state("2026-08-01", "x-2", "x-1"), a=state("2026-08-19", A))
    mf.save_manifest(original, path)
    text = path.read_text()
    assert text.index(CIK) < text.index(OTHER) and text.endswith("\n")
    assert json.loads(text)["companies"][OTHER]["accessions_on_last_date"] == ["x-1", "x-2"]
    assert mf.load_manifest(path) == mf.Manifest(date(2026, 9, 20), {CIK: state("2026-08-19", A), OTHER: state("2026-08-01", "x-1", "x-2")})


def test_a_missing_or_broken_manifest_is_a_manifest_error_that_says_how_to_make_one(tmp_path) -> None:
    with pytest.raises(mf.ManifestError, match="bootstrap"):
        mf.load_manifest(tmp_path / "nope.json")
    (tmp_path / "bad.json").write_text("{not json")
    with pytest.raises(mf.ManifestError, match="not valid JSON"):
        mf.load_manifest(tmp_path / "bad.json")
    (tmp_path / "odd.json").write_text(json.dumps({"processed_through": "2026-09-20", "companies": {CIK: {"last_filing_date": "soon"}}}))
    with pytest.raises(mf.ManifestError, match="malformed"):
        mf.load_manifest(tmp_path / "odd.json")


def test_the_default_path_is_read_at_call_time(tmp_path, monkeypatch) -> None:
    path = tmp_path / "elsewhere.json"
    mf.save_manifest(manifest(a=state("2026-08-19", A)), path)
    monkeypatch.setattr(common, "MANIFEST_PATH", path)
    assert mf.load_manifest().companies[CIK].last_filing_date == date(2026, 8, 19)


def test_bootstrap_takes_each_companys_newest_cached_filing_of_any_form_and_every_accession_on_that_date(tmp_path) -> None:
    raw = tmp_path / "raw"
    for cik, accession, filed, form, status in [
        (CIK, A, "2026-08-19", "8-K", 200), (CIK, B, "2026-08-26", "10-Q", 200), (CIK, C, "2026-08-26", "8-K", 200),
        (CIK, "0000000123-26-000009", "2026-09-10", "8-K", 404),  # not a cached filing: ignored
        (OTHER, "0000000456-26-000001", "2026-05-01", "10-K", 200),
    ]:
        (raw / cik).mkdir(parents=True, exist_ok=True)
        (raw / cik / f"{accession}.meta.json").write_text(json.dumps({"accession": accession, "filed_at": filed, "filing_type": form, "http_status": status}))
    boot = mf.bootstrap(raw, [CIK, OTHER], date(2026, 9, 20))
    assert boot.processed_through == date(2026, 9, 20)
    assert boot.companies[CIK] == state("2026-08-26", B, C)
    assert boot.companies[OTHER] == state("2026-05-01", "0000000456-26-000001")
    with pytest.raises(mf.ManifestError, match="nothing to bootstrap"):
        mf.bootstrap(raw, ["0000009999"], date(2026, 9, 20))


def test_the_real_manifest_covers_the_fourteen_companies_that_have_review_csvs() -> None:
    real = mf.load_manifest(common.REPO_ROOT / "data" / "manifest.json")
    with_review = {p.stem for p in (common.REPO_ROOT / "data" / "review").glob("*.csv")}
    assert set(real.companies) == with_review and len(real.companies) == 14
    assert all(s.accessions_on_last_date for s in real.companies.values())


# --- advancing -------------------------------------------------------------------------------------------------------------


def run(filings, *, complete=True, covered=None, run_date="2026-09-27", **companies):
    before = manifest(**companies)
    payload = {"complete": complete, "companies": covered if covered is not None else sorted(before.companies), "filings": filings}
    return mf.advance(before, payload, run_date=date.fromisoformat(run_date))


def test_a_company_whose_filings_all_fetched_advances_to_the_newest_and_the_run_date_is_recorded() -> None:
    after, report = run([filing(B, "2026-09-02"), filing(C, "2026-09-02", status="no_exhibit")], a=state("2026-08-19", A))
    assert after.companies[CIK] == state("2026-09-02", B, C)
    assert report[CIK] == "advanced" and after.processed_through == date(2026, 9, 27)


def test_a_second_filing_on_the_last_date_is_added_to_the_ones_already_known_for_it() -> None:
    after, _ = run([filing(B, "2026-08-19")], a=state("2026-08-19", A))
    assert after.companies[CIK] == state("2026-08-19", A, B)


def test_a_failed_filing_holds_that_company_and_the_date_the_run_covers() -> None:
    after, report = run([filing(B, "2026-09-02", status="failed"), filing(C, "2026-09-03")], a=state("2026-08-19", A))
    assert after.companies[CIK] == state("2026-08-19", A)  # even the clean filing after the failed one does not move it
    assert report[CIK].startswith("held") and after.processed_through == date(2026, 9, 20)


def test_one_failure_does_not_hold_the_others_but_does_hold_processed_through() -> None:
    after, report = run([filing(B, "2026-09-02", status="failed"), filing("0000000456-26-000002", "2026-09-04", cik=OTHER)],
                        a=state("2026-08-19", A), b=state("2026-08-01", "0000000456-26-000001"))
    assert report[OTHER] == "advanced" and after.companies[OTHER].last_filing_date == date(2026, 9, 4)
    assert after.companies[CIK].last_filing_date == date(2026, 8, 19) and after.processed_through == date(2026, 9, 20)


def test_an_incomplete_run_advances_nothing() -> None:
    after, report = run([filing(B, "2026-09-02")], complete=False, a=state("2026-08-19", A))
    assert after == manifest(a=state("2026-08-19", A)) and report[CIK].startswith("held")


def test_a_company_the_run_did_not_cover_is_left_alone_and_holds_processed_through() -> None:
    after, report = run([filing(B, "2026-09-02")], covered=[CIK], a=state("2026-08-19", A), b=state("2026-08-01", "x"))
    assert report[OTHER] == "not covered" and after.processed_through == date(2026, 9, 20)


def test_a_clean_run_with_nothing_new_still_moves_processed_through_and_never_backwards() -> None:
    after, report = run([], a=state("2026-08-19", A))
    assert report[CIK] == "unchanged" and after.processed_through == date(2026, 9, 27)
    earlier, _ = run([], run_date="2026-09-01", a=state("2026-08-19", A))
    assert earlier.processed_through == date(2026, 9, 20)


def test_the_advance_command_rewrites_the_manifest(tmp_path, capsys) -> None:
    path = tmp_path / "m.json"
    mf.save_manifest(manifest(a=state("2026-08-19", A)), path)
    filings = tmp_path / "new_filings.json"
    filings.write_text(json.dumps({"run_date": "2026-09-27", "complete": True, "companies": [CIK], "filings": [filing(B, "2026-09-02")]}))
    assert mf.main(["advance", "--filings", str(filings), "--manifest", str(path)]) == 0
    assert mf.load_manifest(path).companies[CIK] == state("2026-09-02", B)
    assert "processed through 2026-09-27" in capsys.readouterr().out
    assert mf.main(["advance", "--filings", str(tmp_path / "missing.json"), "--manifest", str(path)]) == 2


# --- 01_fetch --since-manifest ---------------------------------------------------------------------------------------------


@pytest.fixture
def weekly(tmp_path, monkeypatch, raw_dir):
    cfg = common.load_config()
    cfg["companies"] = [{"name": COMPANY.name, "ticker": COMPANY.ticker, "cik": CIK},
                        {"name": "Unlisted Corp", "ticker": "UNLS", "cik": "0000000789"}]
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(cfg))
    mf.save_manifest(mf.Manifest(date(2026, 9, 20), {CIK: state("2026-08-19", A)}), common.MANIFEST_PATH)
    monkeypatch.setattr(fetch, "smoke_test", lambda client: None)
    monkeypatch.setattr(common, "NEW_FILINGS_PATH", tmp_path / "new_filings.json")
    base = "/Archives/edgar/data/123"
    routes = {
        index_url(B): FakeResponse(200, index_html([(1, "8-K", "m.htm", f"{base}/{B.replace('-', '')}/m.htm"),
                                                    (2, "EX-99.1", "r.htm", f"{base}/{B.replace('-', '')}/r.htm")])),
        f"https://www.sec.gov{base}/{B.replace('-', '')}/r.htm": FakeResponse(200, b"<html><body><p>Revenue of $5.0 billion.</p></body></html>"),
        index_url(C): FakeResponse(200, index_html([(1, "8-K", "m.htm", f"{base}/{C.replace('-', '')}/m.htm")])),  # no exhibit
    }
    client, session, _ = make_client(routes)
    monkeypatch.setattr(fetch, "EdgarClient", lambda user_agent, rps: client)
    return config, session


def found(accession, form, filed):
    return fetch.Filing(accession, form, date.fromisoformat(filed), ("2.02",), index_url(accession))


def test_since_manifest_looks_from_the_last_filing_date_skips_known_accessions_and_writes_the_list(weekly, monkeypatch) -> None:
    config, _ = weekly
    seen = {}

    def discover(client, company, cfg, *, date_from=None, date_to=None, skip_accessions=frozenset()):
        seen.update(cik=company.cik, date_from=date_from, date_to=date_to, skip=set(skip_accessions))
        return [found(B, "8-K", "2026-09-02"), found(C, "8-K", "2026-09-03")]

    monkeypatch.setattr(fetch, "discover", discover)
    assert fetch.main(["--config", str(config), "--since-manifest"]) == 0
    assert seen == {"cik": CIK, "date_from": date(2026, 8, 19), "date_to": date.today(), "skip": {A}}  # UNLS is not in the manifest: never asked
    payload = json.loads(common.NEW_FILINGS_PATH.read_text())
    assert (payload["run_date"], payload["complete"], payload["companies"]) == (date.today().isoformat(), True, [CIK])
    by_acc = {f["accession"]: f for f in payload["filings"]}
    assert set(by_acc) == {B, C}
    assert by_acc[B] == {"cik": CIK, "accession": B, "form": "8-K", "filing_date": "2026-09-02", "status": "fetched", "path": by_acc[B]["path"]}
    assert by_acc[B]["path"].endswith(f"{CIK}/{B}.html")
    assert by_acc[C]["status"] == "no_exhibit" and by_acc[C]["path"] is None
    assert common.load_accession_scope(common.NEW_FILINGS_PATH) == {CIK: {B}}  # only a filing with a document is in scope
    # and the list is what advances the manifest
    after, _ = mf.advance(mf.load_manifest(), payload, run_date=date.today())
    assert after.companies[CIK] == state("2026-09-03", C)


def test_an_sec_block_exits_3_with_the_status_code_and_the_list_says_the_run_is_not_complete(weekly, monkeypatch, capsys) -> None:
    config, _ = weekly

    def discover(*args, **kwargs):
        raise fetch.Blocked("HTTP 429 from https://www.sec.gov/cgi-bin/browse-edgar. Stop.")

    monkeypatch.setattr(fetch, "discover", discover)
    assert fetch.main(["--config", str(config), "--since-manifest"]) == 3
    assert "429" in capsys.readouterr().err
    payload = json.loads(common.NEW_FILINGS_PATH.read_text())
    assert payload["complete"] is False and payload["companies"] == []
    after, report = mf.advance(mf.load_manifest(), payload, run_date=date.today())
    assert after == mf.load_manifest() and report[CIK].startswith("held")


def test_a_failed_document_is_listed_as_failed_and_the_exit_code_says_so(weekly, monkeypatch) -> None:
    config, session = weekly
    monkeypatch.setattr(fetch, "discover", lambda *a, **k: [found("0000000123-26-000077", "8-K", "2026-09-05")])
    session.routes[index_url("0000000123-26-000077")] = FakeResponse(404)
    assert fetch.main(["--config", str(config), "--since-manifest"]) == 1
    (entry,) = json.loads(common.NEW_FILINGS_PATH.read_text())["filings"]
    assert entry["status"] == "failed" and entry["path"] is None


def test_without_a_manifest_since_manifest_stops_before_asking_the_sec(weekly, monkeypatch, capsys) -> None:
    config, session = weekly
    common.MANIFEST_PATH.unlink()
    assert fetch.main(["--config", str(config), "--since-manifest"]) == 2
    assert "bootstrap" in capsys.readouterr().err and session.calls == []


def test_discover_takes_a_start_date_an_end_date_and_a_skip_list_and_defaults_the_end_to_today() -> None:
    from test_fetch import atom
    cfg = {"edgar": {"filing_types": ["8-K"], "date_from": "2019-01-01"}}  # no date_to in the config any more
    dateb = (date.today() + timedelta(days=1)).strftime("%Y%m%d")  # the feed is asked for filings before the day after date_to
    url = fetch.BROWSE_URL.format(cik=CIK, form="8-K", dateb=dateb, start=0)
    entries = [(A, "8-K", "2026-08-19", "item 2.02", index_url(A)), (B, "8-K", "2026-09-02", "item 2.02", index_url(B)),
               (C, "8-K", "2026-07-01", "item 2.02", index_url(C))]
    client, session, _ = make_client({url: FakeResponse(200, atom(entries))})
    got = fetch.discover(client, COMPANY, cfg, date_from=date(2026, 8, 19), skip_accessions={A})
    assert [f.accession for f in got] == [B]  # A is known, C is before the start date
