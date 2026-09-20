"""research_record.text: HTML to text and sentence spans. Offsets stored by 02 depend on this staying put."""

from __future__ import annotations

import pytest

from research_record.text import TEXT_VERSION, html_to_text, html_to_text_and_gaps, sentence_spans


def sentences(text: str) -> list[str]:
    return [text[s:e] for s, e in sentence_spans(text)]


# --- html_to_text ----------------------------------------------------------


def test_text_version_is_pinned() -> None:
    # Bump this on purpose, together with any change that moves offsets.
    assert TEXT_VERSION == 1


def test_scripts_styles_titles_and_hidden_xbrl_are_dropped() -> None:
    html = (
        "<html><head><title>TITLE</title><style>p{color:red}</style></head><body>"
        '<div style="display: none"><ix:header><ix:hidden>HIDDEN</ix:hidden></ix:header></div>'
        "<p>Visible.</p><script>var s = 'SCRIPT';</script><noscript>NOSCRIPT</noscript></body></html>"
    )
    assert html_to_text(html) == "Visible."


def test_a_hidden_block_ends_at_its_own_closing_tag_even_when_nested() -> None:
    html = '<div style="display:none"><div>inner</div>still hidden</div><p>after</p>'
    assert html_to_text(html) == "after"


def test_blocks_rows_and_breaks_end_a_line_and_cells_share_one() -> None:
    html = "<p>One</p><div>Two<br>Three</div><table><tr><td>Revenue</td><td>$5.0 billion</td></tr><tr><td>Margin</td><td>75%</td></tr></table>"
    assert html_to_text(html) == "One\nTwo\nThree\nRevenue $5.0 billion\nMargin 75%"


def test_whitespace_is_collapsed_and_blank_lines_dropped_but_nothing_else_is_rewritten() -> None:
    html = "<p>  A&nbsp;&nbsp;  B\u200b  </p><p> </p><p>\u201cQuoted\u201d \u2013 it\u2019s $1.5&#160;billion</p>"
    assert html_to_text(html) == "A B\n\u201cQuoted\u201d \u2013 it\u2019s $1.5 billion"


@pytest.mark.parametrize("raw", ["caf\u00e9 revenue".encode("utf-8"), "caf\u00e9 revenue".encode("cp1252")])
def test_bytes_decode_as_utf8_or_fall_back_to_cp1252(raw: bytes) -> None:
    assert html_to_text(b"<p>" + raw + b"</p>") == "caf\u00e9 revenue"


def test_a_byte_order_mark_is_ignored() -> None:
    assert html_to_text(b"\xef\xbb\xbf<p>text</p>") == "text"


def test_output_is_deterministic() -> None:
    html = "<div><p>A. B.</p><table><tr><td>x</td></tr></table></div>" * 20
    assert html_to_text(html) == html_to_text(html)


# --- sentence_spans --------------------------------------------------------


def test_spans_index_back_into_the_text() -> None:
    text = "First one. Second one!\nThird line here."
    for start, end in sentence_spans(text):
        assert text[start:end] == text[start:end].strip() and start < end
    assert sentences(text) == ["First one.", "Second one!", "Third line here."]


def test_a_line_break_always_ends_a_sentence() -> None:
    assert sentences("Outlook\nRevenue is expected to be $5 billion\nGross margin 75%") == [
        "Outlook",
        "Revenue is expected to be $5 billion",
        "Gross margin 75%",
    ]


@pytest.mark.parametrize(
    "text, first",
    [
        ("Acme Inc. expects U.S. sales of $5.5 billion. It expects more.", "Acme Inc. expects U.S. sales of $5.5 billion."),
        ("We expect revenue of approx. $5 billion. Margins should hold.", "We expect revenue of approx. $5 billion."),
        ("Guidance, e.g. revenue, is unchanged. See the table.", "Guidance, e.g. revenue, is unchanged."),
        ("Mr. Smith said revenue would be $1.5 billion. He was right.", "Mr. Smith said revenue would be $1.5 billion."),
    ],
)
def test_abbreviations_and_decimals_do_not_end_a_sentence(text: str, first: str) -> None:
    found = sentences(text)
    assert len(found) == 2
    assert found[0] == first


def test_a_lowercase_word_after_a_period_does_not_start_a_new_sentence() -> None:
    # "bn" is not in the abbreviation list, so only the lowercase rule keeps this together.
    assert sentences("Revenue was 5.0 bn. lower than plan.") == ["Revenue was 5.0 bn. lower than plan."]
    assert sentences("Revenue was $5.0 billion vs. $4.0 billion last year.") == [
        "Revenue was $5.0 billion vs. $4.0 billion last year."
    ]


def test_closing_quotes_stay_with_their_sentence() -> None:
    assert sentences("He said \u201cwe expect growth.\u201d Then he left.") == [
        "He said \u201cwe expect growth.\u201d",
        "Then he left.",
    ]


def test_leading_bullet_glyphs_sit_outside_the_span() -> None:
    text = "\u2022 Revenue is expected to be $108.0 billion, plus or minus 2%. NVIDIA is not assuming any China revenue."
    assert sentences(text) == [
        "Revenue is expected to be $108.0 billion, plus or minus 2%.",
        "NVIDIA is not assuming any China revenue.",
    ]
    assert sentences("- Revenue up 5%") == ["Revenue up 5%"]
    assert sentences("-5% growth is expected") == ["-5% growth is expected"]  # a minus sign is not a bullet


def test_empty_and_blank_input() -> None:
    assert sentence_spans("") == []
    assert sentence_spans("\n\n") == []


# --- blank-line gaps -------------------------------------------------------


def test_the_text_with_gaps_is_character_for_character_the_text_without() -> None:
    html = "<p>Outlook</p><div><div><p>A. B.</p></div></div><table><tr><td>x</td></tr></table><p></p><p>&nbsp;</p><p>tail</p>"
    text, gaps = html_to_text_and_gaps(html)
    assert text == html_to_text(html) and len(gaps) == len(text.split("\n"))


def test_a_gap_counts_the_blank_lines_that_were_collapsed_before_a_line() -> None:
    plain = html_to_text_and_gaps("<p>A</p><p>B</p>")[1]
    spaced = html_to_text_and_gaps("<p>A</p><p>&nbsp;</p><p>&nbsp;</p><p>B</p>")[1]
    assert plain == [1, 1] and spaced[0] == 1
    assert spaced[1] > plain[1]  # the empty paragraphs between A and B show up as a bigger gap before B


def test_every_line_has_a_gap_and_the_gaps_are_never_negative() -> None:
    _, gaps = html_to_text_and_gaps("<div>one</div><div><div>two</div></div><p>three</p>")
    assert len(gaps) == 3 and all(g >= 0 for g in gaps)
