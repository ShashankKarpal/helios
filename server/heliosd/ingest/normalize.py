"""Normalize incoming samples (Bridge payloads, backfill rows) into store rows."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry

SLEEP_STAGE_MAP = {
    "HKCategoryValueSleepAnalysisInBed": "in_bed",
    "HKCategoryValueSleepAnalysisAsleepUnspecified": "asleep",
    "HKCategoryValueSleepAnalysisAsleep": "asleep",
    "HKCategoryValueSleepAnalysisAsleepCore": "core",
    "HKCategoryValueSleepAnalysisAsleepDeep": "deep",
    "HKCategoryValueSleepAnalysisAsleepREM": "rem",
    "HKCategoryValueSleepAnalysisAwake": "awake",
}
ASLEEP_STAGES = {"asleep", "core", "deep", "rem"}


def sample_id_for(hk_type: str, start: str, end: str | None, source: str, value: Any) -> str:
    """Always content-based, so Bridge backfill and any legacy import dedupe
    against each other. HealthKit UUIDs are kept separately for deletions."""
    raw = f"{hk_type}|{start}|{end}|{source}|{value}"
    return "ch:" + hashlib.sha1(raw.encode()).hexdigest()


def parse_ts(v: str | datetime | None) -> datetime | None:
    """Parse an incoming ISO8601 timestamp into naive LOCAL wall time.

    The Bridge sends UTC ('...Z'). Storing UTC wall time unconverted shifted
    every evening event forward a day for owners east of UTC: a night ending
    05:06 local is 23:36Z the previous day, so sleep filed under the wrong
    date. All samples are stored in the Mac's local time, matching the Whoop
    puller and the signals engine's notion of 'today'."""
    if v is None:
        return v
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            return v.astimezone().replace(tzinfo=None)
        return v
    return (datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            .astimezone().replace(tzinfo=None))


def normalize_sample(raw: dict, policy: MetricPolicy, registry: SourceRegistry,
                     sync_path: str) -> dict | None:
    """One incoming sample dict -> store row dict, or None if ignored/unknown."""
    hk_type = raw.get("hk_type") or raw.get("type") or ""
    metric = raw.get("metric") or policy.hk_to_metric.get(hk_type)
    if not metric:
        return None
    source_name = raw.get("source_name") or raw.get("source") or "unknown"
    device_key = registry.resolve(source_name)
    if device_key is None:
        return None
    start = parse_ts(raw.get("start") or raw.get("start_ts"))
    end = parse_ts(raw.get("end") or raw.get("end_ts")) or start
    if start is None:
        return None

    value, text_value = raw.get("value"), raw.get("text_value")
    if metric == "sleep_analysis":
        stage = SLEEP_STAGE_MAP.get(str(value), None) or SLEEP_STAGE_MAP.get(str(text_value), None)
        if stage is None and isinstance(text_value, str):
            stage = text_value
        text_value = stage or "asleep"
        value = (end - start).total_seconds() / 60.0  # minutes
    else:
        try:
            value = float(value) if value is not None else None
        except (TypeError, ValueError):
            text_value, value = str(value), None
    # SpO2 arrives as fraction (0..1) from HealthKit; store as percent.
    if metric == "spo2" and value is not None and value <= 1.5:
        value *= 100.0

    return {
        "sample_id": sample_id_for(hk_type, str(start), str(end), source_name, value),
        "hk_uuid": raw.get("uuid"),
        "metric": metric,
        "hk_type": hk_type or None,
        "value": value,
        "text_value": text_value if isinstance(text_value, str) else None,
        "unit": raw.get("unit") or policy.unit(metric),
        "start_ts": start,
        "end_ts": end,
        "source_name": source_name,
        "device_key": device_key,
        "sync_path": sync_path,
    }
