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
        "capture_method", "heading", "lead_in", "context_before", "context_after", "block_lines", "block_end", "table_header",
    }
    assert (row["accession"], row["filing_type"], row["filed_at"]) == ("0000000123-24-000001", "8-K", "2024-02-01")


def test_a_matching_segment_over_the_length_cap_is_dropped_and_counted() -> None:
    rows, dropped = build("We expect " + "revenue growth, " * 100 + "of $5 billion.")
    assert rows == [] and dropped == 1


# --- fix 1: a section is one block, never a bare value line ----------------
#
# (line, blank lines before it), copied from real releases. The gaps are what the layout looked like.

NVIDIA_2019 = [  # 0001045810-19-000004: the table behind the pilot's wrong metrics and false ranges
    ("Updated Q4 Fiscal 2019 Guidance", 1),
    ("Revenue", 3), ("$2.70 billion, plus or minus 2%", 1), ("$2.20 billion, plus or minus 2%", 1),
    ("Gross margin - GAAP", 3), ("Gross margin - non-GAAP", 1), ("62.3%, plus or minus 50 bps", 1),
    ("62.5%, plus or minus 50 bps", 1), ("55.0%, plus or minus 100 bps", 1), ("56.0%, plus or minus 100 bps", 1),
    ("Operating expenses - GAAP", 3), ("Operating expenses - non-GAAP", 1), ("$915 million", 1), ("$755 million", 1),
    ("$915 million", 1), ("$755 million", 1),
    ("GAAP and non-GAAP other income and expense", 3), ("$21 million", 1), ("$25 million", 1),
    ("GAAP and non-GAAP tax rate, excluding discrete items", 3), ("8%, plus or minus 1%", 1), ("6%, plus or minus 1%", 1),
    ("This update is an estimate, based on information available to management as of the date of this press release.", 8),
    ("Shareholder Letter", 4), ("A shareholder letter from NVIDIA founder and CEO Jensen Huang is available here.", 1),
]
NVIDIA_2025 = [  # 0001045810-25-000228: the outlook is followed by "Highlights" with only one blank line before it
    ("Outlook", 6), ("NVIDIA\u2019s outlook for the fourth quarter of fiscal 2026 is as follows:", 1),
    ("\u2022Revenue is expected to be $65.0 billion, plus or minus 2%.", 1),
    ("\u2022GAAP and non-GAAP tax rates are expected to be 17.0%, plus or minus 1%, excluding any discrete items.", 1),
    ("Highlights", 1), ("Data Center", 1), ("\u2022Third-quarter revenue was a record $51.2 billion, up 25% from the previous quarter.", 1),
]
TARGET_2025 = [  # 0000027419-25-000123
    ("Guidance", 4),
    ("For the fourth quarter of 2025, the Company is maintaining its expectation of a low-single digit decline in sales.", 1),
    ("Full-year GAAP EPS is now expected to be approximately $7.70 to $8.70.", 1),
    ("Operating Results", 4), ("Net Sales of $25.3 billion in the third quarter were 1.5 percent lower than last year.", 1),
]
SALESFORCE_2025 = [  # 0001108524-25-000234: a table whose own rows have gaps of 0 to 6
    ("Q4 FY26 Guidance", 22), ("GAAP", 2), ("Non-GAAP(1)", 0), ("Revenue", 3), ("$11.13 - $11.23 billion", 1),
    ("Revenue growth(2)", 3), ("11% - 12%", 1), ("Diluted net income per share", 3), ("$1.47 - $1.49 $3.02 - $3.04", 0),
    ("Full Year FY26 Guidance", 11), ("GAAP", 1), ("Non-GAAP(1)", 0), ("Revenue", 3), ("$41.45 - $41.55 billion", 1),
    ("Revenue growth(2)", 6), ("9% - 10%", 1), ("Diluted net income per share", 2), ("$5.85 - $5.87 $11.33 - $11.37", 1),
]


def blocks_of(rows):
    lines, gaps = [r[0] for r in rows], [r[1] for r in rows]
    return lines, cand.find_blocks(lines, gaps, SETTINGS)


def test_the_pilots_bad_table_is_one_block_that_stops_at_the_disclaimer() -> None:
    lines, (block,) = blocks_of(NVIDIA_2019)
    assert lines[block.heading] == "Updated Q4 Fiscal 2019 Guidance"
    assert (block.end - block.start, block.reason) == (21, "gap")  # the 8-line gap before "This update is an estimate"
    assert lines[block.start] == "Revenue" and lines[block.end - 1] == "6%, plus or minus 1%"


def test_a_block_stops_at_an_end_heading_even_with_no_gap_before_it() -> None:
    lines, (block,) = blocks_of(NVIDIA_2025)
    assert (block.end - block.start, block.reason) == (3, "end-heading")
    assert lines[block.end] == "Highlights"


def test_a_block_stops_at_the_next_section_heading() -> None:
    lines, (block,) = blocks_of(TARGET_2025)
    assert block.reason == "end-heading" and lines[block.end] == "Operating Results" and block.end - block.start == 2


def test_a_short_title_after_a_gap_starts_something_new_but_a_table_label_does_not() -> None:
    title = [("Outlook", 1), ("Revenue of $5 billion is expected.", 1), ("Capital Allocation", 4), ("We paid $2 billion.", 1)]
    assert blocks_of(title)[1][0].reason == "title"
    assert cand._is_value("$11.13 - $11.23 billion", SETTINGS) and cand._is_value("Approximately 15%", SETTINGS)
    assert not cand._is_value("We paid $2 billion.", SETTINGS) and not cand._is_value("(1) Subscription & support revenue excludes services.", SETTINGS)
    label = [("Outlook", 1), ("Revenue", 5), ("$5 billion", 1), ("Margin", 5), ("74%", 1)]  # labels are followed by a value
    (only,) = blocks_of(label)[1]
    assert only.end == 5 and only.reason == "eof"


def test_a_table_with_uneven_row_gaps_stays_in_one_block_per_heading() -> None:
    lines, blocks = blocks_of(SALESFORCE_2025)
    assert [(lines[b.heading], b.reason) for b in blocks] == [("Q4 FY26 Guidance", "heading"), ("Full Year FY26 Guidance", "eof")]
    full = blocks[1]
    assert "9% - 10%" in lines[full.start : full.end]  # the gap of 6 before "Revenue growth(2)" did not cut the table


def test_the_next_matching_heading_ends_a_block_and_a_block_is_capped_at_forty_lines() -> None:
    lines, blocks = blocks_of([("Outlook", 1)] + [("x", 1)] * 45)
    assert (blocks[0].end - blocks[0].start, blocks[0].reason) == (40, "max")


def spaced(n_empty: int) -> str:
    return "<p>&nbsp;</p>" * n_empty


def test_a_section_becomes_one_candidate_holding_every_line_and_no_bare_value_lines(world=None) -> None:
    body = "".join(f"<p>{line}</p>" for line, _ in NVIDIA_2019[:22])
    rows, _ = cand.candidates_for_document({**META, "filing_type": "8-K"}, f"<html><body>{body}</body></html>".encode(), SETTINGS)
    (block,) = [r for r in rows if r["capture_method"] == "section"]
    assert block["heading"] == "Updated Q4 Fiscal 2019 Guidance"
    assert block["sentence"].split("\n") == [line for line, _ in NVIDIA_2019[1:22]]
    assert block["block_lines"] == 21 and block["block_end"] in {"eof", "gap"}
    assert not {"$915 million", "$755 million"} & {r["sentence"] for r in rows}  # never sent alone


def test_offsets_of_a_block_index_back_into_the_text_including_its_newlines() -> None:
    body = "".join(f"<p>{line}</p>" for line, _ in NVIDIA_2019[:22])
    html = f"<html><body>{body}</body></html>".encode()
    rows, _ = cand.candidates_for_document({**META, "filing_type": "8-K"}, html, SETTINGS)
    text = html_to_text(html)
    block = next(r for r in rows if r["capture_method"] == "section")
    assert text[block["char_start"] : block["char_end"]] == block["sentence"] and "\n" in block["sentence"]


def test_a_blank_line_gap_in_the_layout_ends_a_block() -> None:
    html = ("<html><body><p>Outlook</p><p>Revenue is expected to be $5 billion.</p><p>$5 billion</p>"
            + spaced(8) + "<p>A disclaimer paragraph with no figures.</p></body></html>").encode()
    rows, _ = cand.candidates_for_document({**META, "filing_type": "8-K"}, html, SETTINGS)
    block = next(r for r in rows if r["capture_method"] == "section")
    assert block["block_end"] == "gap" and "disclaimer" not in block["sentence"]


def test_a_block_with_no_figure_in_it_is_not_a_candidate() -> None:
    rows, _ = build("Outlook", "Management will discuss the results.", "Nothing numeric here.")
    assert [r for r in rows if r["capture_method"] == "section"] == []


def test_section_capture_is_for_8k_exhibits_only() -> None:
    assert build(*[l for l, _ in NVIDIA_2019[:8]], form="10-K")[0] == []
    assert build(*[l for l, _ in NVIDIA_2019[:8]], form="10-Q")[0] == []


def test_sentences_inside_a_block_are_still_their_own_candidates_and_know_their_heading() -> None:
    rows, _ = build("Outlook", "Revenue is expected to be $108.0 billion, plus or minus 2%.", "$5 billion")
    sentence = next(r for r in rows if r["capture_method"] == "sentence")
    assert sentence["sentence"].startswith("Revenue is expected") and sentence["heading"] == "Outlook"
    assert sentence["block_lines"] is None and sentence["block_end"] is None
    assert any(r["capture_method"] == "section" for r in rows)


def test_a_block_carries_context_from_outside_it() -> None:
    rows, _ = build("Intro one.", "Intro two.", "Outlook", "Revenue is expected to be $5 billion.", "Highlights", "After one.", "After two.")
    block = next(r for r in rows if r["capture_method"] == "section")
    assert block["context_before"] == ["Intro one.", "Intro two."] and block["context_after"][0] == "Highlights"


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
    assert [r["lead_in"] for r in rows if r["capture_method"] == "sentence"] == ["NVIDIA outlook for Q3 is as follows:"] * 2
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
