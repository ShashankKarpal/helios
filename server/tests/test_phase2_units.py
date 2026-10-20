"""The unit alias table knows the spelling an Apple Health export prints for VO2 max
(mL/min·kg, seen in the June 2026 export): such a record is the policy unit, not a unit
mismatch, so it links to the Bridge row that holds it. Synthetic values only."""

from __future__ import annotations

from datetime import datetime, timezone

from heliosd.ingest.normalize import UNIT_ALIASES, canonical_unit, normalize_sample
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry


def test_the_export_spelling_of_the_vo2_max_unit_is_the_policy_unit():
    assert canonical_unit("mL/min·kg") == "mL/min/kg" == UNIT_ALIASES["mL/kg·min"]
    row = normalize_sample({"hk_type": "HKQuantityTypeIdentifierVO2Max", "source_name": "Synthetic Ultra 1",
                            "unit": "mL/min·kg", "value": "41.5", "start": datetime(2026, 5, 1, 6, 0, tzinfo=timezone.utc)},
                           MetricPolicy(), SourceRegistry(), "health_export")
    assert (row["metric"], row["unit"], row["quality"], row["value"]) == ("vo2max", "mL/min/kg", None, 41.5)


def test_an_unknown_spelling_is_still_a_unit_mismatch():
    row = normalize_sample({"hk_type": "HKQuantityTypeIdentifierVO2Max", "source_name": "Synthetic Ultra 1",
                            "unit": "ml/min/lb", "value": "41.5", "start": datetime(2026, 5, 1, 6, 0, tzinfo=timezone.utc)},
                           MetricPolicy(), SourceRegistry(), "health_export")
    assert (row["unit"], row["quality"]) == ("ml/min/lb", "unit_mismatch")
