"""pipeline/space.py: the static Space (SCOPE.md section 5.6)."""

from __future__ import annotations

import importlib
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from research_record import polarity, stats
from research_record.schema import ResearchRecord

figures = importlib.import_module("pipeline.figures")
space = importlib.import_module("pipeline.space")

REPO = Path(__file__).resolve().parent.parent
ALLOWED = ("sec.gov", "huggingface.co", "github.com")
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def make(record_data, record_id, *, company="Example Corporation", reported=4_800.0, ack=False, stated=date(2024, 2, 1)):
    data = record_data(record_id=record_id, company=company, status="missed")
    data["assumption"]["stated_at"] = stated
    data["outcome"]["reported_value"] = reported
    if ack:
        data["acknowledged_at"] = date(2025, 3, 1)
        data["acknowledgement_evidence"] = dict(data["outcome"]["evidence"], excerpt="We came in below our range.")
    return ResearchRecord.model_validate(data)


def page(records):
    return space.render_page(
        records, stats.compute(records), figures.acknowledgement_summary(records),
        strip_plot_png=PNG, limitations=["First limit.", "Second limit."], hf_user="Nav772", generated_at=date(2026, 9, 29),
    )


def sample(record_data):
    return [
        make(record_data, "01HZY8Q9XMR3T7VBN2CDEFGH01", ack=True, stated=date(2024, 2, 1)),
        make(record_data, "01HZY8Q9XMR3T7VBN2CDEFGH02", stated=date(2025, 2, 1)),
        make(record_data, "01HZY8Q9XMR3T7VBN2CDEFGH03", reported=6_500.0),  # a beat
        make(record_data, "01HZY8Q9XMR3T7VBN2CDEFGH04", company="Other Inc", reported=1.0),
    ]


def test_one_card_per_worse_row(record_data) -> None:
    records = sample(record_data)
    html = page(records)
    worse = [r for r in records if polarity.record_direction(r) == "worse"]
    assert html.count('class="card worse"') == len(worse) == 3
    assert html.count('class="card better"') == 1


def test_better_rows_sit_in_a_collapsed_section(record_data) -> None:
    html = page(sample(record_data))
    better = html[html.index('<details class="better-section">') : html.index("</details>")]
    assert 'class="card better"' in better and "open" not in better.split(">")[0]


def test_worse_rows_are_newest_first(record_data) -> None:
    html = page(sample(record_data))
    assert html.index("Guidance, 2025-02-01") < html.index("Guidance, 2024-02-01")


def test_card_content(record_data) -> None:
    html = page(sample(record_data))
    assert "guided $5,000 million to $6,000 million, reported $4,800 million" in re.sub(r"<[^>]+>", "", html)
    assert '<span class="rep">$4,800 million</span>' in html
    assert "Acknowledged 2025-03-01" in html and "We came in below our range." in html
    assert "No later filing acknowledged the gap." in html
    assert html.count("Guidance filing on EDGAR") == 4 and html.count("Outcome filing on EDGAR") == 4


def test_only_allowed_hosts(record_data) -> None:
    html = page(sample(record_data))
    urls = space.external_urls(html)
    assert urls
    for url in urls:
        host = urlparse(url).hostname or ""
        assert any(host == a or host.endswith("." + a) for a in ALLOWED), url
    assert "data:image/png;base64," in html


def test_no_forms_no_external_assets(record_data) -> None:
    html = page(sample(record_data))
    for tag in ("<form", "<link", "src=\"http", "@import"):
        assert tag not in html
    assert "<script src" not in html


def test_register(record_data) -> None:
    html = page(sample(record_data)).lower()
    assert "—" not in html and "buy-side" not in html


def test_limitations_and_links_present(record_data) -> None:
    html = page(sample(record_data))
    assert "First limit." in html and "Second limit." in html
    assert "https://huggingface.co/datasets/Nav772/assumption-drift" in html
    assert "https://github.com/Algo-nav/assumption-drift" in html


def test_limitations_from_card_joins_wrapped_bullets() -> None:
    card = "## Known limitations\n\n- one wrapped\n  across lines.\n- two.\n\n## How to cite\n"
    assert space.limitations_from_card(card) == ["one wrapped across lines.", "two."]


def test_release_page_has_one_card_per_worse_row(tmp_path) -> None:
    records, _ = stats.load_records(REPO / "data" / "release" / "assumption_drift.jsonl")
    card = (REPO / "card" / "README.md").read_text(encoding="utf-8")
    plot = REPO / "card" / "figures" / figures.FIGURE_FILES[0]
    index = space.write_space(
        records, stats.compute(records), figures.acknowledgement_summary(records),
        strip_plot=plot, card_text=card, hf_user="Nav772", generated_at=date(2026, 9, 29), space_dir=tmp_path,
    )
    html = index.read_text(encoding="utf-8")
    assert html.count('class="card worse"') == figures.acknowledgement_summary(records)["worse"]
    for url in space.external_urls(html):
        assert any((urlparse(url).hostname or "").endswith(a) for a in ALLOWED), url
    assert "sdk: static" in (tmp_path / "README.md").read_text()


def test_headline_is_the_stat_tile_sentence_and_sections_are_named_by_direction(record_data) -> None:
    records = sample(record_data)
    html = page(records)
    summary = figures.acknowledgement_summary(records)
    assert figures.headline_sentence(summary) in html
    assert "3 worse than guided. 1 acknowledged." in html
    assert "Worse than guided (2)" in html and "Better than guided (1)" in html
    assert "shortfall" not in html.lower() and "beat" not in html.lower()


def _style_and_script(html: str) -> str:
    return "".join(re.findall(r"<(?:style|script)>(.*?)</(?:style|script)>", html, flags=re.S))


def test_no_left_border_uses_the_accent(record_data) -> None:
    html = page(sample(record_data))
    accent = re.search(r"--accent:(#[0-9A-Fa-f]{6})", html).group(1).lower()
    declarations = re.findall(r"border-left[a-z-]*:[^;}]*", html)
    assert declarations
    for declaration in declarations:
        assert accent not in declaration.lower() and "var(--accent)" not in declaration, declaration


def test_styles_and_script_make_no_external_reference(record_data) -> None:
    html = page(sample(record_data))
    assert space.external_urls(_style_and_script(html)) == set()
    assert "url(" not in _style_and_script(html)
    for tag in ('src="http', "src='http", "srcset", "<iframe", "<object", "<embed"):
        assert tag not in html


def test_palette_is_the_five_report_colours(record_data) -> None:
    css = _style_and_script(page(sample(record_data)))
    found = {c.upper() for c in re.findall(r"#[0-9A-Fa-f]{3,6}\b", css)}
    assert found <= {"#FBFAF7", "#1A1A1A", "#5B5B5B", "#D9D6CE", "#3B4CCA", "#FFF"}, found
    assert "box-shadow" not in css and "border-radius" not in css


def test_table_line_is_shown_as_evidence(record_data) -> None:
    assert space.is_table_line("Operating expenses $1,624 $1,028 $970 Up 58% Up 67%")
    assert space.is_table_line("Gross margin 43.5 % 65.5 % 64.8 % Down 22.0 pts Down 21.3 pts")
    assert not space.is_table_line("Fiscal 2020 GAAP earnings per share was $0.15, and non-GAAP diluted earnings per share was $2.99.")
    records = sample(record_data)
    data = records[1].model_dump()
    data["outcome"]["evidence"]["excerpt"] = "Revenue $6,704 $8,288 $6,507 Down 19% Up 3%"
    records[1] = ResearchRecord.model_validate(data)
    html = page(records)
    assert html.count("table line as filed") == 1
    assert "<pre>Revenue $6,704 $8,288 $6,507 Down 19% Up 3%</pre>" in html


def test_report_structure(record_data) -> None:
    html = page(sample(record_data))
    assert html.index("<h1>") < html.index('<table class="summary">') < html.index("<figure>") < html.index('<select id="company-select"')
    assert html.count("<select") == 1 and " worse)</option>" in html and '<ol class="limits">' in html
    assert "4 guidance statements, 2 companies, filings Feb 2024 to Feb 2025, generated 2026-09-29" in html
    assert html.count('class="total"') == 1 and "@media print" in html and "max-width:600px" in html
    assert html.count("<section class=\"company\"") == 2


def test_summary_table_has_six_columns_on_desktop_and_a_phone_rule(record_data) -> None:
    html = page(sample(record_data))
    table = html[html.index('<table class="summary">') : html.index("</table>")]
    head = table[: table.index("</thead>")]
    assert len(re.findall(r"<th[ >]", head)) - 1 == 6  # the empty corner cell aside
    for row in re.findall(r"<tr[^>]*>.*?</tr>", table[table.index("<tbody>") :], flags=re.S):
        assert row.count("<td") == 6
    for short in ("Stmts", "Met", "Better", "Worse", "Worse ack&#x27;d"):
        assert f'<span class="short">{short}</span>' in head
    assert 'title="Better than guided"' in head and 'title="Worse acknowledged"' in head
    mobile = html[html.index("@media (max-width:600px)") :]
    mobile = mobile[: mobile.index("@media print")]
    assert ".col-resolved{display:none}" in mobile
    assert "table-layout:fixed" in mobile and "width:100%" in mobile and "font-size:15px" in mobile
    assert "overflow-x:auto" in mobile
