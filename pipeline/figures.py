"""Four static charts for `card/figures/` (SCOPE.md section 5.5), written by `pipeline/06_publish.py`
from release data. Matplotlib only, and never hand-edited: a figure changes because the release data
or this code changed, not because someone opened it in an editor.

Colour: neutral greys, plus one accent colour reserved for "missed". Never red, amber or green
anywhere in any figure: this is a dataset of gaps, not a scorecard, and stoplight colours read as a
verdict this project is not making.

Every figure carries one footer line in the reserved neutral grey: the dataset name, the row count
behind that figure, the generation date, and "Source: SEC EDGAR".
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: this runs from a script and from tests, never a display

import matplotlib.pyplot as plt

from research_record import rubric
from research_record.schema import ResearchRecord

__all__ = [
    "GREY_DARK", "GREY_MID", "GREY_LIGHT", "ACCENT_MISSED", "FIGURE_FILES",
    "fiscal_year", "direction", "nearest_edge", "miss_magnitude", "write_all",
]

GREY_DARK = "#3f3f46"
GREY_MID = "#8a8a92"
GREY_LIGHT = "#d4d4d8"
#: The one colour reserved for "missed" everywhere it appears. Never red, amber or green (SCOPE 5.5).
ACCENT_MISSED = "#5b6ee1"

FIGURE_FILES: tuple[str, ...] = (
    "falsifiable_vs_acknowledged.png",
    "acknowledgement_by_company.png",
    "resolution_by_fiscal_year.png",
    "miss_magnitude.png",
)

_YEAR = re.compile(r"(20\d\d)")


def fiscal_year(target_period: str) -> int | None:
    """The four digit year named in a period label ("FY2024", "Q3 2024"), quarter or full year alike.
    None if the label names no year (should not happen on a published row, but a chart never crashes
    over one bad label)."""
    match = _YEAR.search(target_period)
    return int(match.group(1)) if match else None


def direction(record: ResearchRecord) -> rubric.Direction | None:
    """"beat" or "shortfall" for a missed record's own numbers. Recomputed from the numbers, same as
    `research_record.stats.missed_direction`, kept local so this module's only dependencies are the
    schema and the rubric."""
    if record.outcome is None:
        return None
    return rubric.direction(record.assumption.target_low, record.assumption.target_high, record.outcome.reported_value)


def nearest_edge(record: ResearchRecord) -> float | None:
    """The target value a missed row's reported figure is measured against: the point value itself for
    point guidance, else whichever bound (`target_high` for a beat, `target_low` for a shortfall) the
    reported value actually landed outside of. None when there is nothing to measure against."""
    a = record.assumption
    if rubric.is_point_guidance(a.target_low, a.target_high):
        return a.target_low
    d = direction(record)
    if d == "beat":
        return a.target_high
    if d == "shortfall":
        return a.target_low
    return None


def miss_magnitude(record: ResearchRecord) -> float | None:
    """(reported_value - nearest_edge) / nearest_edge, for a missed row. None if the row is not missed,
    has no outcome, or its edge is zero (nothing to divide by)."""
    if record.status != "missed" or record.outcome is None or record.outcome.reported_value is None:
        return None
    edge = nearest_edge(record)
    if not edge:
        return None
    return (record.outcome.reported_value - edge) / edge


def _footer(fig, dataset: str, rows: int, generated_at: date) -> None:
    unit = "row" if rows == 1 else "rows"
    fig.text(
        0.01, 0.01, f"{dataset} · {rows:,} {unit} · generated {generated_at.isoformat()} · Source: SEC EDGAR",
        ha="left", va="bottom", fontsize=7, color=GREY_MID,
    )


# --- 1. falsifiable vs acknowledged, missed rows, never-acknowledged as a strip at the top ------


def _falsifiable_vs_acknowledged(records: list[ResearchRecord], dataset: str, generated_at: date) -> plt.Figure:
    missed = [r for r in records if r.status == "missed"]
    both = [(r.days_to_falsifiable, r.days_to_acknowledged) for r in missed
            if r.days_to_falsifiable is not None and r.days_to_acknowledged is not None]
    never = [r.days_to_falsifiable for r in missed if r.days_to_falsifiable is not None and r.acknowledged_at is None]

    fig, ax = plt.subplots(figsize=(7, 4.5))
    top_y = max([y for _, y in both] + [1]) * 4 if (both or never) else 10
    if both:
        xs, ys = zip(*both)
        ax.scatter(xs, [max(y, 1) for y in ys], color=ACCENT_MISSED, alpha=0.75, s=28, label="acknowledged", zorder=3)
    if never:
        ax.scatter(never, [top_y] * len(never), color=ACCENT_MISSED, marker="x", s=28, label="never acknowledged", zorder=3)
        ax.axhline(top_y * 0.6, color=GREY_LIGHT, linewidth=0.8, linestyle="--", zorder=1)
    ax.set_yscale("log")
    ax.set_xlabel("days to falsifiable (guidance to the filing that reported it)")
    ax.set_ylabel("days to acknowledged (log scale)")
    ax.set_title("Missed rows: how long before the gap was checkable, and acknowledged")
    ax.grid(True, which="both", axis="both", color=GREY_LIGHT, linewidth=0.5, alpha=0.6)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    if both or never:
        ax.legend(frameon=False, fontsize=8, loc="lower right")
    _footer(fig, dataset, len(missed), generated_at)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    return fig


# --- 2. share of shortfalls never acknowledged, per company, horizontal bars --------------------


def _acknowledgement_by_company(records: list[ResearchRecord], dataset: str, generated_at: date) -> plt.Figure:
    companies = sorted({r.company for r in records})
    shares: list[float] = []
    used: list[str] = []
    for company in companies:
        shortfalls = [r for r in records if r.company == company and r.status == "missed" and direction(r) == "shortfall"]
        if not shortfalls:
            continue
        never = sum(1 for r in shortfalls if r.acknowledged_at is None)
        used.append(company)
        shares.append(never / len(shortfalls))

    fig, ax = plt.subplots(figsize=(7, max(2.5, 0.5 * len(used) + 1)))
    y_pos = range(len(used))
    ax.barh(list(y_pos), shares, color=ACCENT_MISSED)
    ax.set_yticks(list(y_pos))
    ax.set_yticklabels(used)
    ax.invert_yaxis()
    ax.set_xlim(0, 1)
    ax.set_xlabel("share of shortfalls never acknowledged")
    ax.set_title("Acknowledgement of shortfalls, by company")
    ax.grid(True, axis="x", color=GREY_LIGHT, linewidth=0.5, alpha=0.6)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    total_shortfalls = sum(1 for r in records if r.status == "missed" and direction(r) == "shortfall")
    _footer(fig, dataset, total_shortfalls, generated_at)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    return fig


# --- 3. met / missed / withdrawn, stacked bars per fiscal year ----------------------------------


def _resolution_by_fiscal_year(records: list[ResearchRecord], dataset: str, generated_at: date) -> plt.Figure:
    statuses = ("met", "missed", "withdrawn")
    colors = {"met": GREY_LIGHT, "missed": ACCENT_MISSED, "withdrawn": GREY_DARK}
    resolved = [r for r in records if r.status in statuses]
    years = sorted({fiscal_year(r.assumption.target_period) for r in resolved} - {None})

    fig, ax = plt.subplots(figsize=(7, 4.5))
    bottom = [0] * len(years)
    for status in statuses:
        counts = [sum(1 for r in resolved if r.status == status and fiscal_year(r.assumption.target_period) == y) for y in years]
        ax.bar([str(y) for y in years], counts, bottom=bottom, color=colors[status], label=status)
        bottom = [b + c for b, c in zip(bottom, counts)]
    ax.set_ylabel("rows")
    ax.set_xlabel("fiscal year")
    ax.set_title("Resolution by fiscal year")
    ax.grid(True, axis="y", color=GREY_LIGHT, linewidth=0.5, alpha=0.6)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    if years:
        ax.legend(frameon=False, fontsize=8)
    _footer(fig, dataset, len(resolved), generated_at)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    return fig


# --- 4. histogram of miss magnitude, missed rows only -------------------------------------------


def _miss_magnitude_histogram(records: list[ResearchRecord], dataset: str, generated_at: date) -> plt.Figure:
    missed = [r for r in records if r.status == "missed"]
    values = [m for m in (miss_magnitude(r) for r in missed) if m is not None]

    fig, ax = plt.subplots(figsize=(7, 4.5))
    if values:
        # Point guidance near zero (a small GAAP loss guided, say) makes this ratio blow up for a
        # handful of rows without changing what happened to the rest. The view is clipped to the
        # 2nd-98th percentile so the bulk of the distribution stays readable; nothing is dropped from
        # the count, and how many rows fall outside the shown range is stated on the chart itself.
        if len(values) >= 10:
            lo, hi = sorted(values)[len(values) * 2 // 100], sorted(values)[-(len(values) * 2 // 100) - 1]
        else:
            lo, hi = min(values), max(values)
        pad = (hi - lo) * 0.1 or 0.5
        lo, hi = lo - pad, hi + pad
        ax.hist(values, bins=30, range=(lo, hi), color=ACCENT_MISSED, edgecolor="white", linewidth=0.5)
        outside = sum(1 for v in values if v < lo or v > hi)
        if outside:
            ax.text(0.99, 0.97, f"{outside} row(s) outside the shown range", transform=ax.transAxes,
                    ha="right", va="top", fontsize=8, color=GREY_MID)
    ax.axvline(0, color=GREY_DARK, linewidth=1)
    ax.set_xlabel("(reported - nearest target edge) / nearest target edge")
    ax.set_ylabel("rows")
    ax.set_title("Miss magnitude")
    ax.grid(True, axis="y", color=GREY_LIGHT, linewidth=0.5, alpha=0.6)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    _footer(fig, dataset, len(values), generated_at)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    return fig


_BUILDERS = (
    _falsifiable_vs_acknowledged,
    _acknowledgement_by_company,
    _resolution_by_fiscal_year,
    _miss_magnitude_histogram,
)


def write_all(records: list[ResearchRecord], out_dir: Path, *, dataset: str, generated_at: date) -> list[Path]:
    """Write all four figures to `out_dir`, in the order of `FIGURE_FILES`. Returns their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for filename, builder in zip(FIGURE_FILES, _BUILDERS):
        fig = builder(records, dataset, generated_at)
        path = out_dir / filename
        fig.savefig(path, dpi=150)
        plt.close(fig)
        paths.append(path)
    return paths
