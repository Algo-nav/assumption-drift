"""Which way is good for each metric (`higher_is_better` in pipeline/config.yaml), and a record's own
direction computed with it. `rubric.direction` is pure and takes the polarity as an argument; this is the
one place that knows where it is written down."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from research_record import rubric
from research_record.schema import ResearchRecord

__all__ = ["CONFIG_PATH", "load", "higher_is_better", "record_direction"]

CONFIG_PATH = Path(__file__).resolve().parent.parent / "pipeline" / "config.yaml"


@lru_cache(maxsize=None)
def load(path: Path = CONFIG_PATH) -> dict[str, bool]:
    """`{metric: higher_is_better}` from a config file."""
    with open(path, encoding="utf-8") as fh:
        return dict(yaml.safe_load(fh)["higher_is_better"])


def higher_is_better(metric: str, table: dict[str, bool] | None = None) -> bool:
    table = load() if table is None else table
    try:
        return table[metric]
    except KeyError:
        raise KeyError(f"no higher_is_better entry for metric {metric!r} in pipeline/config.yaml") from None


def record_direction(record: ResearchRecord, table: dict[str, bool] | None = None) -> rubric.Direction | None:
    """"better" or "worse" for a record's own numbers, or None when there is no outcome or the reported
    value is inside the range."""
    if record.outcome is None:
        return None
    a = record.assumption
    return rubric.direction(a.target_low, a.target_high, record.outcome.reported_value, higher_is_better(a.metric, table))
