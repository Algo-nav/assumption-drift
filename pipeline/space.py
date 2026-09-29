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


def limitations_from_card(card_text: str) -> list[str]:
    """The bullets under "## Known limitations" of the rendered dataset card, each joined onto one line.
    Taken from the card's own text, so the page repeats the card word for word."""
    start = card_text.index("## Known limitations") + len("## Known limitations")
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


def guided_line(record: ResearchRecord) -> str:
    a, o = record.assumption, record.outcome
    low, high = a.target_low, a.target_high
    if low is not None and high is not None:
        guided = _number(low, a.unit) if low == high else f"{_number(low, a.unit)} to {_number(high, a.unit)}"
    elif low is not None:
        guided = f"at least {_number(low, a.unit)}"
    else:
        guided = f"at most {_number(high, a.unit)}"  # type: ignore[arg-type]
    reported = _number(o.reported_value, a.unit) if o and o.reported_value is not None else "nothing"
    return f"guided {guided}, reported {reported}"


def _link(evidence: Evidence, label: str) -> str:
    return f'<a href="{escape(str(evidence.source_url), quote=True)}">{escape(label)}</a>'


def _card(record: ResearchRecord, kind: str) -> str:
    a, o = record.assumption, record.outcome
    assert o is not None
    if record.acknowledgement_evidence is not None and record.acknowledged_at is not None:
        ack = (
            f'<p class="label">Acknowledged {record.acknowledged_at.isoformat()}</p>'
            f'<blockquote>{escape(record.acknowledgement_evidence.excerpt)}</blockquote>'
        )
    else:
        ack = '<p class="never">Never referred to again.</p>'
    return f"""\
<article class="card {kind}">
<h4>{escape(a.metric)}, {escape(a.target_period)}</h4>
<p class="label">Guidance, {a.stated_at.isoformat()}</p>
<blockquote>{escape(a.evidence.excerpt)}</blockquote>
<p class="label">Outcome, {o.reported_at.isoformat()}</p>
<blockquote>{escape(o.evidence.excerpt)}</blockquote>
<p class="line">{escape(guided_line(record))}</p>
<p class="links">{_link(a.evidence, "Guidance filing on EDGAR")} · {_link(o.evidence, "Outcome filing on EDGAR")}</p>
{ack}
</article>"""


def _kind(record: ResearchRecord) -> str | None:
    if record.status != "missed" or record.outcome is None or record.outcome.reported_value is None:
        return None
    return polarity.record_direction(record)


def _company_section(name: str, records: list[ResearchRecord], index: int) -> str:
    newest_first = sorted(records, key=lambda r: (r.assumption.stated_at, r.record_id), reverse=True)
    worse = [r for r in newest_first if _kind(r) == "worse"]
    better = [r for r in newest_first if _kind(r) == "better"]
    parts = [f'<section class="company" id="company-{index}" data-company="{escape(name, quote=True)}">', f"<h2>{escape(name)}</h2>"]
    parts.append(f"<h3>Worse than guided ({len(worse):,})</h3>")
    parts += [_card(r, "worse") for r in worse] or ["<p>No rows worse than guided in the release.</p>"]
    if better:
        parts.append(f'<details class="better-section"><summary>Better than guided ({len(better):,})</summary>')
        parts += [_card(r, "better") for r in better]
        parts.append("</details>")
    parts.append("</section>")
    return "\n".join(parts)


CSS = """\
:root{--ink:#27272a;--mute:#6b6b74;--line:#d4d4d8;--bg:#fafafa;--card:#fff;--accent:#5b6ee1}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:46rem;margin:0 auto;padding:1rem}
h1{font-size:1.5rem;margin:.5rem 0}
h2{font-size:1.25rem;margin:1.5rem 0 .25rem}
h3{font-size:1.05rem;margin:1.25rem 0 .5rem}
h4{font-size:1rem;margin:0 0 .5rem}
a{color:var(--accent)}
.lede{color:var(--mute);margin:.25rem 0 1rem}
.headline{font-size:1.6rem;font-weight:700;color:var(--accent);margin:.75rem 0 0}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));gap:.5rem;margin:1rem 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:.6rem .75rem}
.stat b{display:block;font-size:1.6rem;color:var(--accent)}
.stat span{color:var(--mute);font-size:.85rem}
img.plot{width:100%;height:auto;border:1px solid var(--line);border-radius:6px;background:#fff}
label{display:block;margin:1.25rem 0 .25rem;font-weight:600}
select{width:100%;font:inherit;padding:.5rem;border:1px solid var(--line);border-radius:6px;background:var(--card)}
.card{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:6px;padding:.75rem;margin:.75rem 0}
.card.better{border-left-color:var(--line)}
.label{margin:.5rem 0 .1rem;color:var(--mute);font-size:.85rem}
blockquote{margin:0;padding-left:.75rem;border-left:2px solid var(--line);overflow-wrap:anywhere}
.line{font-weight:600;margin:.6rem 0 .25rem}
.links{margin:.25rem 0;font-size:.9rem}
.never{margin:.5rem 0 0;font-style:italic}
details.better-section{margin:1rem 0}
summary{cursor:pointer;font-weight:600}
ul.limits{padding-left:1.1rem}
footer{margin:2rem 0 1rem;color:var(--mute);font-size:.9rem}
"""

SCRIPT = """\
(function(){
var sel=document.getElementById('company-select');
var secs=document.querySelectorAll('section.company');
function show(){for(var i=0;i<secs.length;i++){secs[i].hidden=(secs[i].id!==sel.value);}}
sel.addEventListener('change',show);
show();
})();
"""


def render_page(
    records: list[ResearchRecord], result: dict[str, Any], summary: dict[str, Any], *,
    strip_plot_png: bytes, limitations: list[str], hf_user: str, generated_at: date,
) -> str:
    """The whole page as one string. `result` is `stats.compute()`, `summary` is
    `figures.acknowledgement_summary()`, both already computed from `records`."""
    names = sorted({r.company for r in records})
    options = "\n".join(
        f'<option value="company-{i}">{escape(n)}</option>' for i, n in enumerate(names)
    )
    sections = "\n".join(_company_section(n, [r for r in records if r.company == n], i) for i, n in enumerate(names))
    never = result["missed_never_acknowledged_share"]
    tiles = [
        (f"{result['rows']:,}", "rows"),
        (f"{summary['better_acknowledged']:,} of {summary['better']:,}", "better than guided, acknowledged"),
        (f"{never:.0%}" if never is not None else "n/a", "of misses never acknowledged"),
    ]
    tile_html = "\n".join(f'<div class="stat"><b>{escape(v)}</b><span>{escape(k)}</span></div>' for v, k in tiles)
    encoded = base64.b64encode(strip_plot_png).decode("ascii")
    limit_html = "\n".join(f"<li>{escape(item)}</li>" for item in limitations)
    dataset_url = DATASET_URL.format(hf_user=hf_user)
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
<h1>{SPACE_TITLE}</h1>
<p class="lede">Numeric guidance from SEC filings, set beside what the company later reported. Generated {generated_at.isoformat()} from the release data.</p>
<p class="headline">{escape(figures.headline_sentence(summary))}</p>
<div class="stats">
{tile_html}
</div>
<img class="plot" alt="Missed rows: days from guidance to a checkable outcome, against days to acknowledgement" src="data:image/png;base64,{encoded}">
<label for="company-select">Company</label>
<select id="company-select">
{options}
</select>
{sections}
<h2>Known limitations</h2>
<ul class="limits">
{limit_html}
</ul>
<footer>
<a href="{escape(dataset_url, quote=True)}">Dataset</a> · <a href="{REPO_URL}">Repository</a> · Source: SEC EDGAR
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
) -> Path:
    """Write space/index.html and the Space's README.md (the YAML block Hugging Face reads to know the
    Space is static). Returns the index path."""
    space_dir.mkdir(parents=True, exist_ok=True)
    page = render_page(
        records, result, summary, strip_plot_png=strip_plot.read_bytes(),
        limitations=limitations_from_card(card_text), hf_user=hf_user, generated_at=generated_at,
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
