"""pipeline/space.py: the static Space (SCOPE.md section 5.6)."""

from __future__ import annotations

import importlib
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
    assert "guided $5,000 million to $6,000 million, reported $4,800 million" in html
    assert "Acknowledged 2025-03-01" in html and "We came in below our range." in html
    assert "Never referred to again." in html
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
