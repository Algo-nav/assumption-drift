"""02_candidates.py, offline."""

from __future__ import annotations

import hashlib
import importlib
import json

import pytest

from research_record.text import TEXT_VERSION, html_to_text

cand = importlib.import_module("pipeline.02_candidates")
common = importlib.import_module("pipeline.common")

CFG = common.load_config()["candidates"]
SETTINGS = cand.Settings.from_config(CFG)
META = {"cik": "0000000123", "accession": "0000000123-24-000001", "filing_type": "8-K", "filed_at": "2024-02-01"}


def is_candidate(sentence: str) -> bool:
    return cand.is_candidate(sentence, SETTINGS.forward, SETTINGS.number)


def build(*lines: str, form: str = "8-K"):
    """One <p> per line, so each line is its own line of extracted text."""
    html = "<html><body>" + "".join(f"<p>{line}</p>" for line in lines) + "</body></html>"
    rows, dropped = cand.candidates_for_document({**META, "filing_type": form}, html.encode(), SETTINGS)
    return rows, dropped


# --- the sentence rule -----------------------------------------------------


@pytest.mark.parametrize(
    "sentence",
    [
        "Revenue is expected to be $108.0 billion, plus or minus 2%.",  # NVIDIA
        "Full-year Adjusted EPS is now expected to be approximately $7.00 to $8.00.",  # Target
        "Raises full year FY26 revenue guidance to $41.45 billion to $41.55 billion, up 9% - 10% Y/Y",  # Salesforce
        "We anticipate capital expenditures of 500 million in fiscal 2026.",
        "Our long-term targets include operating margin of 30 percent.",
        "The outlook calls for 120 basis points of expansion.",
        "We forecast opening 25 stores.",
        "For the full year fiscal 2027, NVIDIA expects tax rates to be between 16.0% and 18.0%.",
        "Guidance of $2.50 per share.",
    ],
)
def test_forward_term_plus_number_with_unit_is_a_candidate(sentence: str) -> None:
    assert is_candidate(sentence)


@pytest.mark.parametrize(
    "sentence",
    [
        "The Company expects strong growth next year.",  # forward term, no number
        "Strong growth continued into a low-single digit decline.",  # neither
        "Revenue was $5.0 billion, up 12%.",  # number with unit, no forward term
        "We expect to complete the work in fiscal 2026.",  # a bare year is not a number with a unit
        "We expect 3 new offices.",  # a bare count is not a number with a unit
        "The results were unexpected, at $5 billion.",  # 'expect' inside another word
        "Target Corporation reported sales of $25.2 billion.",  # the company's own name is not a forward term
        "Target\u2019s revenue rose 5%.",
    ],
)
def test_anything_else_is_not(sentence: str) -> None:
    assert not is_candidate(sentence)


def test_a_lower_case_target_still_counts_but_the_company_name_does_not() -> None:
    assert is_candidate("Our target is $5 billion of savings.")
    assert not is_candidate("Target has $5 billion of savings.")


# --- one document: the basics ----------------------------------------------

HTML = (
    "<html><body><p>Outlook</p><p>NVIDIA\u2019s outlook for the third quarter of fiscal 2027 is as follows:</p>"
    "<p>\u2022 Revenue is expected to be $108.0 billion, plus or minus 2%. NVIDIA is not assuming any China revenue.</p>"
    "<p>Revenue was $96.2 billion last quarter.</p></body></html>"
)


def test_offsets_index_back_into_the_extracted_text() -> None:
    rows, dropped = cand.candidates_for_document(META, HTML.encode(), SETTINGS)
    text = html_to_text(HTML.encode())
    assert dropped == 0
    assert rows and all(text[r["char_start"] : r["char_end"]] == r["sentence"] for r in rows)
    assert all(r["text_version"] == TEXT_VERSION for r in rows)
    assert "Revenue is expected to be $108.0 billion, plus or minus 2%." in [r["sentence"] for r in rows]


def test_a_row_carries_every_field() -> None:
    (row, *_), _ = cand.candidates_for_document(META, HTML.encode(), SETTINGS)
    assert set(row) == {
        "cik", "accession", "filing_type", "filed_at", "char_start", "char_end", "sentence", "text_version",
        "capture_method", "heading", "lead_in", "context_before", "context_after",
    }
    assert (row["accession"], row["filing_type"], row["filed_at"]) == ("0000000123-24-000001", "8-K", "2024-02-01")


def test_a_matching_segment_over_the_length_cap_is_dropped_and_counted() -> None:
    rows, dropped = build("We expect " + "revenue growth, " * 100 + "of $5 billion.")
    assert rows == [] and dropped == 1


# --- capture_method and the section rule -----------------------------------

TABLE = [
    "Q4 FY26 Guidance", "GAAP", "Revenue", "$11.13 - $11.23 billion", "Revenue growth", "11% - 12%",
    "Includes approximately 3pts Informatica contribution", "A closing remark without figures.",
]


def test_number_lines_under_a_guidance_heading_are_captured_as_section() -> None:
    rows, _ = build(*TABLE)
    assert [(r["sentence"], r["capture_method"]) for r in rows] == [
        ("$11.13 - $11.23 billion", "section"),
        ("11% - 12%", "section"),
        ("Includes approximately 3pts Informatica contribution", "section"),
    ]
    assert {r["heading"] for r in rows} == {"Q4 FY26 Guidance"}


def test_a_section_row_keeps_its_label_in_the_context() -> None:
    first = build(*TABLE)[0][0]
    assert first["context_before"] == ["GAAP", "Revenue"]
    assert first["context_after"] == ["Revenue growth", "11% - 12%"]


def test_the_window_is_fifteen_lines() -> None:
    lines = ["Outlook"] + ["filler"] * 14 + ["$5 billion", "$6 billion"]  # heading at 0, so lines 15 and 16
    rows, _ = build(*lines)
    assert [r["sentence"] for r in rows] == ["$5 billion"]


def test_section_capture_is_for_8k_exhibits_only() -> None:
    assert build(*TABLE, form="10-K")[0] == []
    assert build(*TABLE, form="10-Q")[0] == []


def test_a_line_that_qualifies_as_a_sentence_stays_a_sentence_and_is_not_duplicated() -> None:
    rows, _ = build("Outlook", "Revenue is expected to be $108.0 billion, plus or minus 2%.", "$5 billion")
    assert [(r["sentence"], r["capture_method"]) for r in rows] == [
        ("Revenue is expected to be $108.0 billion, plus or minus 2%.", "sentence"),
        ("$5 billion", "section"),
    ]
    assert rows[0]["heading"] == "Outlook"


def test_every_10k_and_10q_candidate_is_a_sentence_with_no_heading() -> None:
    rows, _ = build("Outlook", "We expect capital expenditures of $5 billion.", form="10-K")
    assert [(r["capture_method"], r["heading"]) for r in rows] == [("sentence", None)]


@pytest.mark.parametrize(
    "line",
    ["Outlook", "Financial Outlook", "Q4 FY26 Guidance", "Guidance 3", "RECONCILIATION OF GAAP TO NON-GAAP OUTLOOK", "Adjusted EPS guidance"],
)
def test_short_lines_that_name_guidance_or_outlook_are_headings(line: str) -> None:
    assert cand.is_heading(line, SETTINGS)


@pytest.mark.parametrize(
    "line",
    [
        "NVIDIA\u2019s outlook for the third quarter of fiscal 2027 is as follows:",  # a lead-in: too many words
        "Salesforce's guidance includes GAAP and non-GAAP financial measures.",  # reads as a sentence
        "Management will provide further commentary around these guidance assumptions on its call",  # too long
        "Highlights",  # no keyword
        "",
    ],
)
def test_sentences_and_lead_ins_are_not_headings(line: str) -> None:
    assert not cand.is_heading(line, SETTINGS)


# --- lead-in ---------------------------------------------------------------


def test_the_lead_in_is_the_nearest_preceding_line_ending_in_a_colon() -> None:
    rows, _ = build(
        "Contact:", "Outlook", "NVIDIA outlook for Q3 is as follows:",
        "Revenue is expected to be $5 billion.", "Margins are expected to be 74%.",
    )
    assert [r["lead_in"] for r in rows] == ["NVIDIA outlook for Q3 is as follows:"] * 2
    assert build("First:", "Second:", "Revenue is expected to be $5 billion.")[0][0]["lead_in"] == "Second:"


def test_a_candidates_own_line_is_never_its_lead_in() -> None:
    assert build("Revenue is expected to be $5 billion, as follows:")[0][0]["lead_in"] is None


def test_the_lead_in_lookback_is_fifteen_lines() -> None:
    assert build("Lead:", *["x"] * 14, "Revenue is expected to be $5 billion.")[0][0]["lead_in"] == "Lead:"
    assert build("Lead:", *["x"] * 15, "Revenue is expected to be $5 billion.")[0][0]["lead_in"] is None


def test_a_colon_line_that_is_too_long_is_not_a_lead_in_and_the_search_goes_on() -> None:
    long_line = "y" * SETTINGS.lead_in_max_chars + " and more:"
    row = build("Short lead:", long_line, "Revenue is expected to be $5 billion.")[0][0]
    assert row["lead_in"] == "Short lead:"


# --- surrounding sentences -------------------------------------------------


def test_context_is_two_sentences_either_side_and_shorter_at_the_edges() -> None:
    rows, _ = build("Alpha one.", "Beta two.", "Gamma three.", "Revenue is expected to be $5 billion.",
                    "Delta four.", "Epsilon five.", "Zeta six.")
    assert rows[0]["context_before"] == ["Beta two.", "Gamma three."]
    assert rows[0]["context_after"] == ["Delta four.", "Epsilon five."]
    first = build("Revenue is expected to be $5 billion.", "Next one.")[0][0]
    assert (first["context_before"], first["context_after"]) == ([], ["Next one."])


def test_context_strings_are_cut_to_the_limit() -> None:
    limit = SETTINGS.context_max_chars
    row = build("z" * 500, "Revenue is expected to be $5 billion.")[0][0]
    assert len(row["context_before"][0]) == limit + 3 and row["context_before"][0].endswith("...")


# --- a whole company -------------------------------------------------------


@pytest.fixture
def cache(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    monkeypatch.setattr(common, "RAW_DIR", raw)
    monkeypatch.setattr(cand, "RAW_DIR", raw)
    folder = raw / "0000000123"
    folder.mkdir(parents=True)

    def add(accession: str, filed: str, form: str, body: str, *, status: int = 200, tamper: bool = False) -> None:
        content = body.encode()
        meta = {**META, "accession": accession, "filed_at": filed, "filing_type": form, "http_status": status,
                "content_sha256": hashlib.sha256(content).hexdigest()}
        (folder / f"{accession}.meta.json").write_text(json.dumps(meta))
        (folder / f"{accession}.html").write_bytes(b"tampered" if tamper else content)

    add("0000000123-24-000002", "2024-05-01", "10-Q", "<p>We expect capex of $200 million.</p>")
    add("0000000123-24-000001", "2024-02-01", "8-K", HTML)
    add("0000000123-24-000003", "2024-06-01", "8-K", "<p>Board change announced.</p>")
    add("0000000123-24-000004", "2024-07-01", "8-K", "<p>We expect $1 billion.</p>", status=404)
    add("0000000123-24-000005", "2024-08-01", "10-K", "<p>We expect $9 billion.</p>", tamper=True)
    return folder


def test_build_company_writes_sorted_rows_and_skips_bad_documents(cache, tmp_path, capsys) -> None:
    company = common.Company("Example", "EXMP", "0000000123")
    out_dir = tmp_path / "candidates"
    stats = cand.build_company(company, CFG, out_dir)
    rows = [json.loads(line) for line in (out_dir / "0000000123.jsonl").read_text().splitlines()]

    accessions = [r["accession"] for r in rows]
    assert accessions == sorted(accessions)  # by filing date, then position
    assert set(accessions) == {"0000000123-24-000001", "0000000123-24-000002"}
    assert all(r["capture_method"] in {"sentence", "section"} for r in rows)
    assert stats["documents"] == 3 and stats["candidates"] == len(rows)
    assert stats["hash_mismatch"] == 1  # the tampered 10-K, never read as a filing
    assert stats["silent_8k_documents"] == 1  # the board change: an 8-K with no candidate
    assert "0000000123-24-000003" in capsys.readouterr().out
    assert "0000000123-24-000004" not in accessions  # non-200 sidecar ignored


def test_build_company_is_deterministic(cache, tmp_path) -> None:
    company = common.Company("Example", "EXMP", "0000000123")
    cand.build_company(company, CFG, tmp_path / "a")
    cand.build_company(company, CFG, tmp_path / "b")
    assert (tmp_path / "a" / "0000000123.jsonl").read_bytes() == (tmp_path / "b" / "0000000123.jsonl").read_bytes()
