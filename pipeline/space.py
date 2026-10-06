"""The static Space (SCOPE.md section 5.6): one self-contained space/index.html, rendered from release data.

Called from pipeline/06_publish.py, never run on its own. Reads nothing but the records and figure it is
handed. The page takes no input beyond a company selector, calls no model, has no form, and makes no
request to any host: the strip plot is embedded as base64 and every other URL is a plain link.
"""

from __future__ import annotations

import base64
import re
from datetime import date
from html import escape
from pathlib import Path
from typing import Any

from pipeline import figures
from research_record import polarity
from research_record.schema import Evidence, ResearchRecord

SPACE_TITLE = "assumption-drift"
DATASET_URL = "https://huggingface.co/datasets/{hf_user}/assumption-drift"
REPO_URL = "https://github.com/Algo-nav/assumption-drift"

_BILLION_MILLION = {"USD billions": ("$", " billion"), "USD millions": ("$", " million")}


def limitations_from_card(card_text: str, heading: str = "Known limitations") -> list[str]:
    """The bullets under a "## <heading>" section of the rendered dataset card, each joined onto one line.
    Taken from the card's own text, so the page repeats the card word for word."""
    start = card_text.index(f"## {heading}") + len(f"## {heading}")
    end = card_text.index("\n## ", start)
    bullets: list[str] = []
    for line in card_text[start:end].splitlines():
        if line.startswith("- "):
            bullets.append(line[2:].strip())
        elif line.strip() and bullets:
            bullets[-1] += " " + line.strip()
    return bullets


def _number(value: float, unit: str) -> str:
    if unit in _BILLION_MILLION:
        prefix, suffix = _BILLION_MILLION[unit]
        return f"{prefix}{value:,.3f}".rstrip("0").rstrip(".") + suffix
    if unit == "percent":
        return f"{value:g}%"
    if unit == "USD per share":
        return f"${value:g}"
    return f"{value:g} {unit}"


def guided_parts(record: ResearchRecord) -> tuple[str, str]:
    """The guided range and the reported value as two strings, for "guided X, reported Y"."""
    a, o = record.assumption, record.outcome
    low, high = a.target_low, a.target_high
    if low is not None and high is not None:
        guided = _number(low, a.unit) if low == high else f"{_number(low, a.unit)} to {_number(high, a.unit)}"
    elif low is not None:
        guided = f"at least {_number(low, a.unit)}"
    else:
        guided = f"at most {_number(high, a.unit)}"  # type: ignore[arg-type]
    reported = _number(o.reported_value, a.unit) if o and o.reported_value is not None else "nothing"
    return guided, reported


def guided_line(record: ResearchRecord) -> str:
    guided, reported = guided_parts(record)
    return f"guided {guided}, reported {reported}"


_NUMBER_TOKEN = re.compile(r"^\(?[-$]?[\d,.]+\)?%?$")
_TABLE_FILLER = {"up", "down", "pts", "pt", "%", "--", "n/a", "nm"}


def is_table_line(excerpt: str) -> bool:
    """True for a row lifted from a multi-column table ("Operating expenses $1,624 $1,028 $970 Up 58% Up 67%"):
    at least three numbers, and no words beyond a short row label and the Up/Down/pts filler."""
    numbers, words = 0, 0
    for token in excerpt.split():
        if _NUMBER_TOKEN.match(token):
            numbers += 1
        elif token.lower() not in _TABLE_FILLER:
            words += 1
    return numbers >= 3 and words <= 4


def _link(evidence: Evidence, label: str) -> str:
    return f'<a href="{escape(str(evidence.source_url), quote=True)}">{escape(label)}</a>'


def _card(record: ResearchRecord, kind: str) -> str:
    # The class stays "card" (SCOPE 5.6 and tests count them); the styling is a ruled row, not a box.
    a, o = record.assumption, record.outcome
    assert o is not None
    guided, reported = guided_parts(record)
    if record.acknowledgement_evidence is not None and record.acknowledged_at is not None:
        ack = (
            f'<p class="ack">Acknowledged {record.acknowledged_at.isoformat()}: '
            f'&ldquo;{escape(record.acknowledgement_evidence.excerpt)}&rdquo;</p>'
        )
    else:
        ack = '<p class="ack">No later filing acknowledged the gap.</p>'
    if is_table_line(o.evidence.excerpt):
        outcome = (
            '<div class="raw"><span class="rawlabel">table line as filed</span>'
            f'<pre>{escape(o.evidence.excerpt)}</pre></div>'
        )
    else:
        outcome = f"<blockquote>{escape(o.evidence.excerpt)}</blockquote>"
    return f"""\
<article class="card {kind}">
<div class="top">
<h4>{escape(a.metric)}, {escape(a.target_period)}</h4>
<p class="gap">guided {escape(guided)}, reported <span class="rep">{escape(reported)}</span></p>
</div>
<p class="label">Guidance, {a.stated_at.isoformat()}</p>
<blockquote>{escape(a.evidence.excerpt)}</blockquote>
<p class="label">Outcome, {o.reported_at.isoformat()}</p>
{outcome}
<p class="links">{_link(a.evidence, "Guidance filing on EDGAR")} <span class="sep">/</span> {_link(o.evidence, "Outcome filing on EDGAR")}</p>
{ack}
</article>"""


def _kind(record: ResearchRecord) -> str | None:
    if record.status != "missed" or record.outcome is None or record.outcome.reported_value is None:
        return None
    return polarity.record_direction(record)


def short_name(company: str) -> str:
    """"NVIDIA Corporation" -> "NVIDIA", "Salesforce, Inc." -> "Salesforce"."""
    return re.sub(r"[,.]?\s+(?:Corporation|Corp\.?|Incorporated|Inc\.?|Company|Co\.?)$", "", company).strip() or company


def _company_section(name: str, records: list[ResearchRecord], index: int) -> str:
    newest_first = sorted(records, key=lambda r: (r.assumption.stated_at, r.record_id), reverse=True)
    worse = [r for r in newest_first if _kind(r) == "worse"]
    better = [r for r in newest_first if _kind(r) == "better"]
    parts = [f'<section class="company" id="company-{index}" data-company="{escape(name, quote=True)}">', f"<h2>{escape(name)}</h2>"]
    parts.append(f'<h3 class="smallcaps">Worse than guided ({len(worse):,}), newest first</h3>')
    parts += [_card(r, "worse") for r in worse] or ["<p>No rows worse than guided in the release.</p>"]
    if better:
        parts.append(f'<details class="better-section"><summary class="smallcaps">Better than guided ({len(better):,})</summary>')
        parts += [_card(r, "better") for r in better]
        parts.append("</details>")
    parts.append("</section>")
    return "\n".join(parts)


# (full header, phone header, class). Resolved is dropped on phones: it is met plus missed, said in the small print.
_SUMMARY_COLUMNS = [
    ("Statements", "Stmts", ""),
    ("Resolved", "Resolved", "col-resolved"),
    ("Met", "Met", ""),
    ("Better than guided", "Better", ""),
    ("Worse than guided", "Worse", ""),
    ("Worse acknowledged", "Worse ack'd", ""),
]


def _summary_row(label: str, records: list[ResearchRecord], *, total: bool = False) -> str:
    ack = figures.acknowledgement_summary(records)
    met = sum(1 for r in records if r.status == "met")
    missed = sum(1 for r in records if r.status == "missed")
    cells = [len(records), met + missed, met, ack["better"], ack["worse"], ack["worse_acknowledged"]]
    tds = "".join(
        f'<td{f" class=\"{c}\"" if c else ""}>{v:,}</td>' for v, (_, _, c) in zip(cells, _SUMMARY_COLUMNS)
    )
    return f'<tr{" class=\"total\"" if total else ""}><th scope="row">{escape(label)}</th>{tds}</tr>'


def _summary_table(records: list[ResearchRecord], names: list[str]) -> str:
    ths = "".join(
        f'<th{f" class=\"{c}\"" if c else ""} title="{escape(full, quote=True)}">'
        f'<span class="full">{escape(full)}</span><span class="short">{escape(short)}</span></th>'
        for full, short, c in _SUMMARY_COLUMNS
    )
    rows = [_summary_row(short_name(n), [r for r in records if r.company == n]) for n in names]
    rows.append(_summary_row("Total", records, total=True))
    return (
        f'<div class="tablewrap"><table class="summary"><thead><tr><th></th>{ths}</tr></thead>\n<tbody>\n'
        + "\n".join(rows)
        + "\n</tbody></table></div>\n"
        '<p class="note">Resolved is met plus missed. Withdrawn and unresolved rows count under statements only.</p>'
    )


CSS = """\
:root{--paper:#FBFAF7;--ink:#1A1A1A;--mute:#5B5B5B;--rule:#D9D6CE;--accent:#3B4CCA;
--serif:Georgia,"Times New Roman",serif;--sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 var(--sans);font-variant-numeric:tabular-nums}
main{max-width:880px;margin:0 auto;padding:56px 24px 48px}
a{color:var(--ink);text-decoration-color:var(--rule);text-underline-offset:3px}
h1,h2,h3,h4{font-family:var(--serif);font-weight:400;margin:0}
.smallcaps{font-variant:small-caps;letter-spacing:.06em;color:var(--mute)}
.masthead .label-top{font-variant:small-caps;letter-spacing:.14em;color:var(--mute);margin:0 0 12px}
h1{font-size:40px;line-height:1.15;font-weight:700}
.dateline{color:var(--mute);margin:16px 0 24px}
hr,.rule{border:0;border-top:1px solid var(--rule);margin:0}
table.summary{width:100%;border-collapse:collapse;margin:32px 0 8px}
table.summary th,table.summary td{padding:8px 0 8px 12px;text-align:right;border-bottom:1px solid var(--rule);font-weight:400;vertical-align:bottom}
table.summary thead th{font-size:13px;color:var(--mute);border-bottom-color:var(--ink)}
table.summary th:first-child{text-align:left;padding-left:0}
table.summary tbody th{font-family:var(--serif);font-size:17px}
table.summary tr.total th,table.summary tr.total td{font-weight:700;border-bottom:0}
.short{display:none}
.note{color:var(--mute);font-size:13px;margin:4px 0 0}
figure{margin:40px 0 0}
img.plot{display:block;width:100%;height:auto}
figcaption{color:var(--mute);font-size:14px;margin-top:8px}
.picker{display:none;align-items:baseline;gap:12px;margin:48px 0 0}
html.js .picker{display:flex}
.picker label{font-family:var(--serif);font-size:18px}
.picker select{font:inherit;font-family:var(--serif);font-size:18px;color:inherit;background:none;border:0;border-bottom:1px solid var(--ink);
-webkit-appearance:none;appearance:none;padding:0 20px 4px 0;cursor:pointer;
background-image:linear-gradient(45deg,transparent 50%,currentColor 50%),linear-gradient(135deg,currentColor 50%,transparent 50%);
background-position:calc(100% - 8px) 55%,calc(100% - 3px) 55%;background-size:5px 5px,5px 5px;background-repeat:no-repeat}
html.js section.company{display:none}
html.js section.company.active{display:block}
section.company{margin-top:32px}
h2{font-size:26px;font-weight:700;margin-bottom:24px}
h3.smallcaps{font-size:15px;margin:0 0 4px}
summary.smallcaps{font-size:15px;cursor:pointer;padding:8px 0}
details.better-section{margin-top:32px}
.card{border-top:1px solid var(--rule);padding:20px 0}
.top{display:flex;justify-content:space-between;align-items:baseline;gap:24px}
h4{font-size:18px;font-weight:700}
.gap{margin:0;text-align:right;white-space:nowrap}
.rep{color:var(--ink)}
.card.worse .rep{color:var(--accent);font-weight:700}
.label{margin:14px 0 4px;color:var(--mute);font-size:13px}
blockquote{margin:0 0 0 16px;padding-left:16px;border-left:1px solid var(--rule);overflow-wrap:anywhere}
.raw{margin:0 0 0 16px;padding-left:16px;border-left:1px solid var(--rule)}
.rawlabel{display:block;color:var(--mute);font-size:12px;font-variant:small-caps;letter-spacing:.06em}
pre{margin:2px 0 0;font:13px/1.45 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere}
.links{margin:14px 0 0;font-size:14px}
.sep{color:var(--rule);padding:0 6px}
.ack{margin:8px 0 0;font-style:italic;color:var(--mute);font-size:15px}
h2.limits-head{font-size:15px;font-weight:400;margin:64px 0 12px}
ol.limits{padding-left:24px;margin:0}
ol.limits li{margin-bottom:8px}
footer{margin-top:48px;padding-top:16px;border-top:1px solid var(--rule);color:var(--mute);font-size:14px}
footer a{color:var(--mute)}
@media (max-width:600px){
main{padding:32px 16px}
h1{font-size:30px}
.top{display:block}
.gap{text-align:left;white-space:normal;margin-top:4px}
.tablewrap{overflow-x:auto;background:
linear-gradient(to right,var(--paper) 30%,rgba(251,250,247,0)) left center/40px 100% no-repeat local,
linear-gradient(to left,var(--paper) 30%,rgba(251,250,247,0)) right center/40px 100% no-repeat local,
linear-gradient(to right,rgba(217,214,206,.9),rgba(217,214,206,0)) left center/14px 100% no-repeat scroll,
linear-gradient(to left,rgba(217,214,206,.9),rgba(217,214,206,0)) right center/14px 100% no-repeat scroll}
table.summary{table-layout:fixed;width:100%;font-size:15px}
table.summary th,table.summary td{padding-left:4px}
table.summary tbody th{font-size:15px;overflow-wrap:anywhere}
table.summary thead th{font-size:12px;line-height:1.25}
table.summary th:first-child{width:27%}
.col-resolved{display:none}
.full{display:none}
.short{display:inline}
.picker{display:none}
html.js .picker{display:block}
.picker select{width:100%;margin-top:4px}
}
@media print{
body{background:#fff}
html.js .picker{display:none!important}
html.js section.company{display:block}
.card{break-inside:avoid}
}
"""

SCRIPT = """\
(function(){
document.documentElement.className+=' js';
var sel=document.getElementById('company-select');
var secs=document.querySelectorAll('section.company');
function show(i){
for(var k=0;k<secs.length;k++){secs[k].className='company'+(k===i?' active':'');}
sel.selectedIndex=i;}
function fromHash(){
var h=location.hash.replace('#','');
for(var k=0;k<secs.length;k++){if(secs[k].id===h)return k;}
return 0;}
sel.addEventListener('change',function(){show(sel.selectedIndex);
history.replaceState(null,'','#'+secs[sel.selectedIndex].id);});
window.addEventListener('hashchange',function(){show(fromHash());});
show(fromHash());
})();
"""


def _month(d: date) -> str:
    return d.strftime("%b %Y")


def render_page(
    records: list[ResearchRecord], result: dict[str, Any], summary: dict[str, Any], *,
    strip_plot_png: bytes, limitations: list[str], hf_user: str, generated_at: date,
    filings_from: date | None = None, filings_to: date | None = None, coverage_notes: list[str] | None = None,
) -> str:
    """The whole page as one string. `result` is `stats.compute()`, `summary` is
    `figures.acknowledgement_summary()`, both already computed from `records`. The filing range in the
    masthead is the configured one when given, else the earliest and latest filing in the records."""
    names = sorted({r.company for r in records})
    if filings_from is None or filings_to is None:
        filed = [e.filed_at for r in records for e in (r.assumption.evidence, r.outcome.evidence if r.outcome else None) if e]
        filings_from = filings_from or min(filed, default=generated_at)
        filings_to = filings_to or max(filed, default=generated_at)
    options = "\n".join(
        f'<option value="company-{i}"{" selected" if i == 0 else ""}>'
        f'{escape(short_name(n))} ({sum(1 for r in records if r.company == n and _kind(r) == "worse"):,} worse)</option>'
        for i, n in enumerate(names)
    )
    sections = "\n".join(_company_section(n, [r for r in records if r.company == n], i) for i, n in enumerate(names))
    encoded = base64.b64encode(strip_plot_png).decode("ascii")
    limit_html = "\n".join(f"<li>{escape(item)}</li>" for item in limitations)
    coverage_html = ""
    if coverage_notes:
        items = "\n".join(f"<li>{escape(item)}</li>" for item in coverage_notes)
        coverage_html = f'<h2 class="limits-head smallcaps">Coverage notes</h2>\n<ol class="limits">\n{items}\n</ol>\n'
    dataset_url = DATASET_URL.format(hf_user=hf_user)
    dateline = (
        f"{len(records):,} guidance statements, {len(names)} companies, filings "
        f"{_month(filings_from)} to {_month(filings_to)}, generated {generated_at.isoformat()}"
    )
    return f"""\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{SPACE_TITLE}</title>
<style>
{CSS}</style>
</head>
<body>
<main>
<header class="masthead">
<p class="label-top">{SPACE_TITLE}</p>
<h1>{escape(figures.headline_sentence(summary))}</h1>
<p class="dateline">{escape(dateline)}</p>
</header>
<hr>
{_summary_table(records, names)}
<figure>
<img class="plot" alt="Missed rows: days from guidance to a checkable outcome, against days to acknowledgement" src="data:image/png;base64,{encoded}">
<figcaption>Each mark is a missed row: days from guidance to a checkable outcome, against days to acknowledgement.</figcaption>
</figure>
<div class="picker"><label for="company-select">Company</label>
<select id="company-select">
{options}
</select></div>
{sections}
<h2 class="limits-head smallcaps">Known limitations</h2>
<ol class="limits">
{limit_html}
</ol>
{coverage_html}<footer>
<a href="{escape(dataset_url, quote=True)}">Dataset</a> / <a href="{REPO_URL}">Repository</a> / Source: SEC EDGAR / Data CC-BY-4.0, code MIT
</footer>
</main>
<script>
{SCRIPT}</script>
</body>
</html>
"""


def write_space(
    records: list[ResearchRecord], result: dict[str, Any], summary: dict[str, Any], *,
    strip_plot: Path, card_text: str, hf_user: str, generated_at: date, space_dir: Path,
    filings_from: date | None = None, filings_to: date | None = None,
) -> Path:
    """Write space/index.html and the Space's README.md (the YAML block Hugging Face reads to know the
    Space is static). Returns the index path."""
    space_dir.mkdir(parents=True, exist_ok=True)
    page = render_page(
        records, result, summary, strip_plot_png=strip_plot.read_bytes(),
        limitations=limitations_from_card(card_text), hf_user=hf_user, generated_at=generated_at,
        filings_from=filings_from, filings_to=filings_to,
        coverage_notes=limitations_from_card(card_text, "Coverage notes"),
    )
    (space_dir / "README.md").write_text(
        f"---\ntitle: {SPACE_TITLE}\nsdk: static\npinned: false\nlicense: cc-by-4.0\n---\n", encoding="utf-8"
    )
    index = space_dir / "index.html"
    index.write_text(page, encoding="utf-8")
    return index


_URL = re.compile(r"""(?:https?:)?//[^\s"'<>)]+""")


def external_urls(page: str) -> set[str]:
    """Every absolute or protocol-relative URL in `page`, the embedded base64 image aside."""
    return set(_URL.findall(re.sub(r"data:image/png;base64,[A-Za-z0-9+/=]+", "", page)))
