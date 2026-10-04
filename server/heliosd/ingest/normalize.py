"""Normalize incoming samples (Bridge payloads, future export rows) into store rows.

Single-source Phase 1a time and identity model:
- Every instant is parsed to UTC. `start_utc` and `end_utc` are stored as naive
  UTC wall values (tzinfo stripped at the binding boundary: an aware datetime
  binds as TIMESTAMPTZ and DuckDB would shift it by the session zone).
- `start_ts` and `end_ts` are the same instants rendered in the REPORTING zone
  (policy reporting_timezone, else the owner's timezone, else UTC). They are
  display and day-bucketing values only; local time is never part of a key.
- Identity is native: `hk:<uuid>` for Bridge rows. The ch3 content hash is kept
  as lineage only and never decides whether a row exists.
- The metric comes only from the policy's HealthKit mapping. A payload cannot
  name its own metric (the old raw["metric"] bypass is closed), and an unknown
  sleep category is stored with its raw string and quality unknown_category,
  never coerced to "asleep".
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

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
KNOWN_STAGES = set(SLEEP_STAGE_MAP.values())
ASLEEP_STAGES = {"asleep", "core", "deep", "rem"}

# Unit rules, versioned so a replay or a migration can tell an applied rule
# from a raw value. HealthKit delivers percent quantities as fractions (0..1).
FRAC_TO_PCT = "frac_to_pct_v1"
FRACTION_METRICS = {"spo2", "body_fat_pct"}

TIME_SOURCE_BY_PATH = {"bridge": "bridge_utc", "health_export": "export_offset",
                       "whoop_live": "whoop_api"}


def canon_value(value: Any) -> str:
    """Canonical string form of a sample value for hashing (6 decimals)."""
    if value is None:
        return "None"
    try:
        s = f"{float(value):.6f}".rstrip("0").rstrip(".")
        return s if s not in ("", "-0") else "0"
    except (TypeError, ValueError):
        return str(value)


def sample_id_for(hk_type: str, start: str, end: str | None, source: str, value: Any,
                  text: str | None = None) -> str:
    """Legacy ch2 content id (local-time strings). Kept for the alias and
    migration tooling of Phase 1b; new rows never use it as identity."""
    raw = f"{hk_type}|{start}|{end}|{source}|{canon_value(value)}|{text or ''}"
    return "ch2:" + hashlib.sha1(raw.encode()).hexdigest()


def content_hash_for(hk_type: str, start_utc: datetime, end_utc: datetime | None, source: str,
                     value: Any, text: str | None = None) -> str:
    """ch3 lineage hash over UTC fields. Zone independent, so the same sample
    hashes the same wherever the Mac was. Lineage only."""
    raw = f"{hk_type}|{start_utc.isoformat()}|{end_utc.isoformat() if end_utc else ''}|{source}|{canon_value(value)}|{text or ''}"
    return "ch3:" + hashlib.sha1(raw.encode()).hexdigest()


def reporting_zone(name: str | None) -> ZoneInfo:
    return ZoneInfo(name or "UTC")


def parse_utc(v: str | datetime | None, zone: ZoneInfo) -> datetime | None:
    """Parse an ISO8601 string or datetime into an AWARE UTC datetime.
    Aware input converts; naive input is read as wall time in the reporting
    zone (test fixtures and manual logs), never as the Mac's zone."""
    if v is None:
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        s = str(v).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone)
    return dt.astimezone(timezone.utc)


def to_utc_naive(dt: datetime | None) -> datetime | None:
    """UTC wall value with tzinfo stripped, whole seconds. The only shape that
    is ever bound into start_utc/end_utc."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


def to_wall(dt: datetime | None, zone: ZoneInfo) -> datetime | None:
    """Reporting-zone wall time, naive, whole seconds (start_ts/end_ts)."""
    if dt is None:
        return None
    return dt.astimezone(zone).replace(tzinfo=None, microsecond=0)


def reporting_today(zone: ZoneInfo, now: datetime | None = None) -> date:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(zone).date()


def apply_unit_rule(metric: str, value: float | None, unit_rule: str | None) -> tuple[float | None, str | None, str | None]:
    """(value, unit_rule, quality). Fractions become percent exactly once: a
    row that already carries the stamp is never scaled again, and a stamped
    row that still looks like a fraction is flagged, not re-scaled."""
    if metric not in FRACTION_METRICS or value is None:
        return value, unit_rule, None
    if unit_rule == FRAC_TO_PCT:
        if value <= 1.5:
            return value, unit_rule, "unit_mismatch"
        return value, unit_rule, None
    if value <= 1.5:
        return value * 100.0, FRAC_TO_PCT, None
    return value, None, None


def normalize_sample(raw: dict, policy: MetricPolicy, registry: SourceRegistry,
                     sync_path: str, batch_id: str | None = None) -> dict | None:
    """One incoming sample dict -> store row dict, or None when the type is
    unregistered or the source is ignored (drop mode). Callers count the skips
    per hk_type; nothing is coerced into a different metric."""
    hk_type = raw.get("hk_type") or raw.get("type") or ""
    metric = policy.hk_to_metric.get(hk_type)
    if not metric:
        return None
    source_name = raw.get("source_name") or raw.get("source") or "unknown"
    device_key = registry.resolve(source_name)
    if device_key is None:
        return None
    zone = policy.zone
    start_utc = parse_utc(raw.get("start") or raw.get("start_ts"), zone)
    if start_utc is None:
        return None
    end_utc = parse_utc(raw.get("end") or raw.get("end_ts"), zone) or start_utc
    start_utc = start_utc.replace(microsecond=0)
    end_utc = end_utc.replace(microsecond=0)
    quality = None
    if end_utc < start_utc:
        quality = "bad_time"

    value, text_value = raw.get("value"), raw.get("text_value")
    if metric == "sleep_analysis":
        stage = SLEEP_STAGE_MAP.get(str(value)) or SLEEP_STAGE_MAP.get(str(text_value))
        if stage is None:
            cand = text_value if isinstance(text_value, str) else (value if isinstance(value, str) else None)
            if cand in KNOWN_STAGES:
                stage = cand
        if stage is None:
            # Unknown category: keep the raw string, mark unusable. Never "asleep".
            text_value = str(text_value if text_value is not None else value)
            quality = quality or "unknown_category"
        else:
            text_value = stage
        value = (end_utc - start_utc).total_seconds() / 60.0  # minutes
    else:
        try:
            value = float(value) if value is not None else None
        except (TypeError, ValueError):
            text_value, value = str(value), None
    unit_rule = None
    value, unit_rule, q = apply_unit_rule(metric, value, raw.get("unit_rule"))
    quality = quality or q

    text_out = text_value if isinstance(text_value, str) else None
    uuid = raw.get("uuid")
    sample_id = raw.get("sample_id") or (f"hk:{uuid}" if uuid else None)
    chash = content_hash_for(hk_type, start_utc, end_utc, source_name, value, text_out)
    if sample_id is None:
        sample_id = chash
    offset = raw.get("offset_min")
    if offset is None:
        src = raw.get("start") or raw.get("start_ts")
        if isinstance(src, datetime) and src.tzinfo is not None and src.utcoffset() != timedelta(0):
            offset = int(src.utcoffset().total_seconds() // 60)

    return {
        "sample_id": sample_id,
        "hk_uuid": uuid,
        "metric": metric,
        "hk_type": hk_type or None,
        "value": value,
        "text_value": text_out,
        "unit": raw.get("unit") or policy.unit(metric),
        "start_ts": to_wall(start_utc, zone),
        "end_ts": to_wall(end_utc, zone),
        "source_name": source_name,
        "device_key": device_key,
        "sync_path": sync_path,
        "start_utc": to_utc_naive(start_utc),
        "end_utc": to_utc_naive(end_utc),
        "src_offset_min": offset,
        "time_source": raw.get("time_source") or TIME_SOURCE_BY_PATH.get(sync_path, sync_path),
        "content_hash": chash,
        "unit_rule": unit_rule,
        "score_state": raw.get("score_state"),
        "quality": quality,
        "batch_id": batch_id,
        "sync_identifier": raw.get("sync_identifier"),
        "sync_version": raw.get("sync_version"),
        "writer_id": raw.get("writer_id"),
    }
