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

import random
import re
from datetime import date
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # headless: this runs from a script and from tests, never a display

import matplotlib.pyplot as plt
import matplotlib.ticker as mtick

from research_record import rubric
from research_record.schema import ResearchRecord

__all__ = [
    "GREY_DARK", "GREY_MID", "GREY_LIGHT", "ACCENT_MISSED", "FIGURE_FILES", "ACK_CATEGORIES",
    "fiscal_year", "direction", "nearest_edge", "miss_magnitude", "acknowledgement_bucket",
    "acknowledgement_summary", "write_all",
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


#: The three buckets a missed row's acknowledgement falls into, in display order (top to bottom).
ACK_CATEGORIES = ("acknowledged in the same filing", "acknowledged later", "never acknowledged")


def acknowledgement_bucket(record: ResearchRecord) -> str | None:
    """Which of `ACK_CATEGORIES` a missed row falls into: the same filing that reported the outcome
    also acknowledged it (`days_to_acknowledged == 0`), a later one did (`> 0`), or none ever did.
    None for a row that is not missed."""
    if record.status != "missed":
        return None
    if record.acknowledged_at is None:
        return "never acknowledged"
    return "acknowledged in the same filing" if (record.days_to_acknowledged or 0) == 0 else "acknowledged later"


def _footer(fig, dataset: str, rows: int, generated_at: date) -> None:
    unit = "row" if rows == 1 else "rows"
    fig.text(
        0.01, 0.01, f"{dataset} · {rows:,} {unit} · generated {generated_at.isoformat()} · Source: SEC EDGAR",
        ha="left", va="bottom", fontsize=7, color=GREY_MID,
    )


# --- 1. when a missed row was acknowledged: a categorical strip plot, jittered ------------------


def _falsifiable_vs_acknowledged(records: list[ResearchRecord], dataset: str, generated_at: date) -> plt.Figure:
    missed = [r for r in records if r.status == "missed"]
    rng = random.Random(0)  # fixed seed: the jitter is reproducible for the same release data
    buckets: dict[str, list[int]] = {c: [] for c in ACK_CATEGORIES}
    for r in missed:
        bucket = acknowledgement_bucket(r)
        if bucket is not None and r.days_to_falsifiable is not None:
            buckets[bucket].append(r.days_to_falsifiable)

    fig, ax = plt.subplots(figsize=(7, 4.5))
    for i, category in enumerate(ACK_CATEGORIES):
        xs = buckets[category]
        if not xs:
            continue
        ys = [i + rng.uniform(-0.15, 0.15) for _ in xs]
        color = ACCENT_MISSED if category == "never acknowledged" else GREY_DARK
        ax.scatter(xs, ys, color=color, alpha=0.7, s=28, zorder=3)
    ax.set_yticks(range(len(ACK_CATEGORIES)))
    ax.set_yticklabels(ACK_CATEGORIES)
    ax.invert_yaxis()
    ax.set_xlabel("days to falsifiable (guidance to the filing that reported it)")
    ax.set_title("Missed rows: when the gap was acknowledged")
    ax.grid(True, axis="x", color=GREY_LIGHT, linewidth=0.5, alpha=0.6)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    _footer(fig, dataset, len(missed), generated_at)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    return fig


# --- 2. shortfalls vs beats, acknowledged or not: a stat tile -----------------------------------


def acknowledgement_summary(records: list[ResearchRecord]) -> dict[str, Any]:
    """Missed rows split into shortfalls and beats, and how many of each were ever acknowledged:
    `{"shortfalls", "shortfalls_acknowledged", "beats", "beats_acknowledged", "acknowledging_companies"}`.
    The last, sorted, is which companies had at least one acknowledged beat; empty if none did."""
    missed = [r for r in records if r.status == "missed"]
    shortfalls = [r for r in missed if direction(r) == "shortfall"]
    beats = [r for r in missed if direction(r) == "beat"]
    beats_acknowledged = [r for r in beats if r.acknowledged_at is not None]
    return {
        "shortfalls": len(shortfalls),
        "shortfalls_acknowledged": sum(1 for r in shortfalls if r.acknowledged_at is not None),
        "beats": len(beats),
        "beats_acknowledged": len(beats_acknowledged),
        "acknowledging_companies": sorted({r.company for r in beats_acknowledged}),
    }


def _join_names(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" and {names[-1]}"


def _acknowledgement_stat_tile(records: list[ResearchRecord], dataset: str, generated_at: date) -> plt.Figure:
    summary = acknowledgement_summary(records)
    headline = f"{summary['shortfalls']:,} shortfalls. {summary['shortfalls_acknowledged']:,} acknowledged."
    subline = f"Beats: {summary['beats_acknowledged']:,} of {summary['beats']:,} acknowledged"
    if summary["acknowledging_companies"]:
        subline += f", by {_join_names(summary['acknowledging_companies'])}"

    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.axis("off")  # plain background: no axes, no gridlines, just the two lines and the footer
    ax.text(0.5, 0.58, headline, ha="center", va="center", fontsize=24, fontweight="bold",
            color=ACCENT_MISSED, transform=ax.transAxes, wrap=True)
    ax.text(0.5, 0.38, subline, ha="center", va="center", fontsize=13, color=GREY_DARK, transform=ax.transAxes, wrap=True)
    _footer(fig, dataset, sum(1 for r in records if r.status == "missed"), generated_at)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
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
    ax.text(0, ax.get_ylim()[1], "edge of guided range", ha="center", va="top", fontsize=8, color=GREY_DARK)
    ax.xaxis.set_major_formatter(mtick.PercentFormatter(xmax=1))
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
    _acknowledgement_stat_tile,
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
