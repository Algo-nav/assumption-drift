"""higher_is_better per metric (pipeline/config.yaml) and the direction it gives a record."""

from __future__ import annotations

import pytest

from pipeline import common
from research_record import polarity, stats
from research_record.schema import ResearchRecord

HIGHER = ["revenue", "gross margin GAAP", "gross margin non-GAAP", "operating margin GAAP", "operating margin non-GAAP",
          "operating income", "EPS GAAP", "EPS non-GAAP", "comparable sales", "free cash flow", "other income and expense"]
LOWER = ["operating expenses GAAP", "operating expenses non-GAAP", "tax rate", "total expenses", "capital expenditures"]


def test_every_metric_has_a_polarity_and_nothing_else_does() -> None:
    config = common.load_config()
    assert set(config["higher_is_better"]) == set(config["metrics"])


def test_polarity_matches_the_agreed_table() -> None:
    table = polarity.load()
    assert all(table[m] is True for m in HIGHER)
    assert all(table[m] is False for m in LOWER)
    assert len(table) == len(HIGHER) + len(LOWER)


def test_an_unknown_metric_is_an_error_not_a_default() -> None:
    with pytest.raises(KeyError, match="no higher_is_better entry"):
        polarity.higher_is_better("gross profit")


def _cost_record(record_data, reported):
    data = record_data()
    data["assumption"]["metric"] = "operating expenses GAAP"
    data["outcome"]["reported_value"] = reported
    return ResearchRecord.model_validate(data)


def test_a_cost_above_its_range_is_worse_and_below_is_better(record_data) -> None:
    above, below = _cost_record(record_data, 6_500.0), _cost_record(record_data, 4_500.0)  # range [5000, 6000]
    assert polarity.record_direction(above) == "worse"
    assert polarity.record_direction(below) == "better"
    assert stats.compute([above, below])["missed_by_direction"] == {"better": 1, "worse": 1}
