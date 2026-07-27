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


def canon_value(value: Any) -> str:
    """Canonical string form of a sample value for hashing.

    The Bridge sends full-precision doubles ("0.0757658928...") while the Apple
    Health XML export prints rounded decimals ("0.075766") for the very same
    sample, and JSON/XML float round-trips differ in trailing digits. Hashing
    raw str(value) therefore produced different ids for identical samples.
    Rounding to 6 significant decimals before hashing makes both paths agree;
    genuinely different readings never differ only past the 6th decimal."""
    if value is None:
        return "None"
    try:
        s = f"{float(value):.6f}".rstrip("0").rstrip(".")
        return s if s not in ("", "-0") else "0"
    except (TypeError, ValueError):
        return str(value)


def sample_id_for(hk_type: str, start: str, end: str | None, source: str, value: Any,
                  text: str | None = None) -> str:
    """Always content-based, so Bridge backfill and any bulk import (Apple
    Health XML export, legacy DB) dedupe against each other. HealthKit UUIDs
    are kept separately for deletions.

    ch2 canonical form (2026-07-25): timestamps are whole seconds (the XML
    export has no sub-second precision, the Bridge does), values are rounded
    via canon_value, and text_value participates so two category samples
    sharing a span (in_bed vs asleep over the same night) never collide.
    Existing rows were migrated to ch2 ids by tools/import_health_export.py."""
    raw = f"{hk_type}|{start}|{end}|{source}|{canon_value(value)}|{text or ''}"
    return "ch2:" + hashlib.sha1(raw.encode()).hexdigest()


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
    # Canonical whole-second timestamps, for storage AND hashing. The Bridge
    # sends fractional seconds; the Apple Health XML export does not. Keeping
    # sub-second precision made identical samples hash differently (and sleep
    # durations computed from them differ), breaking dedup between the two
    # paths. Sub-second precision carries no analytical value here.
    start = start.replace(microsecond=0)
    end = end.replace(microsecond=0) if end is not None else start

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
        "sample_id": sample_id_for(hk_type, str(start), str(end), source_name, value,
                                   text_value if isinstance(text_value, str) else None),
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
