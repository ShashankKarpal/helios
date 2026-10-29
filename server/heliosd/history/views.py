"""Read-only views over the Phase 2 history tables (rank 5, 2026-10-29).

The web app's History tab shows the workout sessions and the ECG recordings
the Apple Health export import wrote (history/importer.py). Display only: no
score, no comparison, no advice, no ECG interpretation; the ECG
classification is Apple's own text as recorded. Every function here runs
SELECTs only.

The live Bridge keeps newer workouts and ECGs as raw_records rows
(ingest/bridge_v2.py) and builds no session from them until the R1 matching
fix: the Bridge, Whoop and Zepp copies of one workout would list twice. So
the lists count those newer raw records (newer_records) and never list them.

A route leaves the server only as an outline: integer points on a 0 to 1000
grid with the north up, its aspect kept. No latitude or longitude is sent, so
none can reach a screen or a log.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from heliosd.store import db

ARCHIVE_ORIGIN = "apple_health_export"
OUTLINE_MAX_POINTS = 500
OUTLINE_GRID = 1000
EVENTS_MAX = 200
METADATA_MAX = 100
WORKOUT_ID = re.compile(r"^wk:[A-Za-z0-9:_.-]{1,120}$")
_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


class BadCursor(ValueError):
    """A cursor this module did not write: the route answers 422."""


def _iso_utc(v: datetime | None) -> str | None:
    return None if v is None else v.replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def _local(v: datetime | None, zone_name: str | None, fallback: ZoneInfo) -> tuple[str | None, str]:
    """The UTC instant as wall time in the session's own zone when it names a valid one, else the
    reporting zone; returns (ISO with offset, the zone used)."""
    zone = fallback
    if zone_name:
        try:
            zone = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError):
            zone = fallback
    if v is None:
        return None, zone.key
    return v.replace(tzinfo=timezone.utc).astimezone(zone).isoformat(timespec="seconds"), zone.key


def activity_label(activity_type: str | None) -> str:
    """HKWorkoutActivityTypeTraditionalStrengthTraining -> "Traditional strength training".
    Mechanical: the prefix stripped and the words split; an acronym stays upper case."""
    t = (activity_type or "").removeprefix("HKWorkoutActivityType")
    words = _WORDS.findall(t)
    if not words:
        return "Workout"
    out = [w if w.isupper() and len(w) > 1 else w.lower() for w in words]
    out[0] = out[0] if out[0].isupper() else out[0].capitalize()
    return " ".join(out)


def encode_cursor(start: datetime | None, key: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([_iso_utc(start), key]).encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime | None, str]:
    try:
        v = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (binascii.Error, UnicodeDecodeError, ValueError) as e:
        raise BadCursor("unreadable cursor") from e
    if not (isinstance(v, list) and len(v) == 2 and isinstance(v[1], str) and (v[0] is None or isinstance(v[0], str))):
        raise BadCursor("unreadable cursor")
    try:
        start = None if v[0] is None else datetime.fromisoformat(v[0].removesuffix("Z"))
    except ValueError as e:
        raise BadCursor("unreadable cursor") from e
    return start, v[1]


def _keyset(col: str, key: str, cursor: str | None) -> tuple[str, list]:
    """The WHERE part after a cursor, for ORDER BY col DESC NULLS LAST, key DESC."""
    if not cursor:
        return "TRUE", []
    start, last = decode_cursor(cursor)
    if start is None:
        return f"({col} IS NULL AND {key} < ?)", [last]
    return f"({col} < ? OR ({col} = ? AND {key} < ?) OR {col} IS NULL)", [start, start, last]


def _labels(registry, device_key: str | None, family: str) -> str:
    return registry.label(device_key) if device_key else family


def _families(conn, ids: list[str], registry) -> dict[str, list[str]]:
    """Per session, the readable labels of the devices that recorded it: the anchor first."""
    if not ids:
        return {}
    rows = db.fetchdicts(conn, """
        SELECT workout_id, device_key, family FROM workout_sources
        WHERE workout_id IN (SELECT unnest(?))
        ORDER BY workout_id, CASE role WHEN 'anchor' THEN 0 WHEN 'member' THEN 1 ELSE 2 END, source_id""", [ids])
    out: dict[str, list[str]] = {}
    for r in rows:
        labels = out.setdefault(r["workout_id"], [])
        lab = _labels(registry, r["device_key"], r["family"])
        if lab not in labels:
            labels.append(lab)
    return out


def _archive(conn, table: str, col: str) -> dict:
    lo, hi = db.fetchall(conn, f"SELECT MIN({col}), MAX({col}) FROM {table}")[0]
    return {"origin": ARCHIVE_ORIGIN, f"oldest_{col}": _iso_utc(lo), f"newest_{col}": _iso_utc(hi)}


def _newer_records(conn, element: str, newest: datetime | None) -> int:
    """Raw records of one class (the live Bridge's) that start after the archive's newest. With no
    archive every one of them is newer."""
    if newest is None:
        return int(db.fetchall(conn, "SELECT COUNT(*) FROM raw_records WHERE element = ?", [element])[0][0])
    return int(db.fetchall(conn, "SELECT COUNT(*) FROM raw_records WHERE element = ? AND start_utc > ?",
                           [element, newest])[0][0])


def _row(r: dict, zone: ZoneInfo) -> dict:
    local, used = _local(r["start_utc"], r.get("tz_name"), zone)
    return {"workout_id": r["workout_id"], "status": r["status"], "n_sources": r["n_sources"],
            "activity_type": r["activity_type"], "activity_label": activity_label(r["activity_type"]),
            "activity_family": r["activity_family"], "start_utc": _iso_utc(r["start_utc"]),
            "end_utc": _iso_utc(r["end_utc"]), "start_local": local, "tz": used, "duration_s": r["duration_s"],
            "distance": r["distance"], "distance_unit": r["distance_unit"], "energy_kcal": r["energy_kcal"],
            "energy_unit": r["energy_unit"], "hr_avg": r["hr_avg"], "hr_unit": r["hr_unit"],
            "has_route": r["route_id"] is not None}


def list_workouts(conn, zone: ZoneInfo, registry, limit: int, cursor: str | None) -> dict:
    where, params = _keyset("start_utc", "workout_id", cursor)
    rows = db.fetchdicts(conn, f"""
        SELECT * FROM workouts WHERE {where}
        ORDER BY start_utc DESC NULLS LAST, workout_id DESC LIMIT ?""", params + [limit + 1])
    more = len(rows) > limit
    rows = rows[:limit]
    fams = _families(conn, [r["workout_id"] for r in rows], registry)
    out = []
    for r in rows:
        row = _row(r, zone)
        row["sources"] = fams.get(r["workout_id"], [])
        out.append(row)
    total, newest = db.fetchall(conn, "SELECT COUNT(*), MAX(start_utc) FROM workouts")[0]
    return {"reporting_zone": zone.key, "total": int(total),
            "archive": _archive(conn, "workouts", "start_utc"),
            "newer_records": _newer_records(conn, "Workout", newest),
            "workouts": out,
            "next_cursor": encode_cursor(rows[-1]["start_utc"], rows[-1]["workout_id"]) if more and rows else None}


def route_outline(conn, route_id: str) -> dict | None:
    """The route as integer points on a 0 to 1000 grid (north up, aspect kept), at most 500 of
    them picked evenly by sequence with the first and last kept. No coordinate is returned."""
    pts = db.fetchall(conn, "SELECT lat, lon FROM route_points WHERE route_id = ? AND lat IS NOT NULL "
                           "AND lon IS NOT NULL ORDER BY seq", [route_id])
    if not pts:
        return None
    n = len(pts)
    if n > OUTLINE_MAX_POINTS:
        pick = sorted({round(i * (n - 1) / (OUTLINE_MAX_POINTS - 1)) for i in range(OUTLINE_MAX_POINTS)})
        pts = [pts[i] for i in pick]
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    k = math.cos(math.radians(sum(lats) / len(lats)))
    lat_hi, lon_lo = max(lats), min(lons)
    xs = [(lon - lon_lo) * k for lon in lons]
    ys = [lat_hi - lat for lat in lats]
    span = max(max(xs), max(ys))
    scale = OUTLINE_GRID / span if span > 0 else 0.0
    outline = [[round(x * scale), round(y * scale)] for x, y in zip(xs, ys)]
    return {"n_points_total": n, "outline": outline,
            "width": round(max(xs) * scale), "height": round(max(ys) * scale)}


def _source(conn, registry, s: dict) -> dict:
    sid = s["source_id"]
    stats = db.fetchdicts(conn, """
        SELECT hk_type, stat_sum AS sum, stat_avg AS avg, stat_min AS min, stat_max AS max, unit
        FROM workout_statistics WHERE source_id = ? ORDER BY ord""", [sid])
    events = db.fetchdicts(conn, "SELECT event_type, date_utc, duration, duration_unit FROM workout_events "
                                 "WHERE source_id = ? ORDER BY ord", [sid])
    meta = [(k, v) for k, v in db.fetchall(conn, "SELECT meta_key, meta_value FROM workout_metadata "
                                                 "WHERE source_id = ? ORDER BY ord", [sid])
            if "sync" not in (k or "").lower()]
    return {"source_id": sid, "role": s["role"], "link_rule": s["link_rule"], "family": s["family"],
            "label": _labels(registry, s["device_key"], s["family"]), "source_name": s["source_name"],
            "source_version": s["source_version"], "activity_type": s["activity_type"],
            "activity_label": activity_label(s["activity_type"]), "start_utc": _iso_utc(s["start_utc"]),
            "end_utc": _iso_utc(s["end_utc"]), "duration_s": s["duration_s"],
            "total_distance_raw": s["total_distance_raw"], "total_distance_unit": s["total_distance_unit"],
            "total_energy_raw": s["total_energy_raw"], "total_energy_unit": s["total_energy_unit"],
            "statistics": stats,
            "events": {"total": len(events), "counts": dict(Counter(e["event_type"] for e in events)),
                       "items": [{**e, "date_utc": _iso_utc(e["date_utc"])} for e in events[:EVENTS_MAX]]},
            "metadata": {"total": len(meta), "items": [[k, v] for k, v in meta[:METADATA_MAX]]}}


FIELD_SOURCE_COLS = {"type": "type_source", "timing": "timing_source", "tz": "tz_source",
                     "distance": "distance_source", "energy": "energy_source", "hr": "hr_source",
                     "strain": "strain_source"}


def get_workout(conn, zone: ZoneInfo, registry, workout_id: str) -> dict | None:
    if not WORKOUT_ID.match(workout_id):
        return None
    rows = db.fetchdicts(conn, "SELECT * FROM workouts WHERE workout_id = ?", [workout_id])
    if not rows:
        return None
    w = rows[0]
    srcs = db.fetchdicts(conn, """
        SELECT * FROM workout_sources WHERE workout_id = ?
        ORDER BY CASE role WHEN 'anchor' THEN 0 WHEN 'member' THEN 1 ELSE 2 END, source_id""", [workout_id])
    by_id = {s["source_id"]: s for s in srcs}
    field_sources = {}
    for field, col in FIELD_SOURCE_COLS.items():
        s = by_id.get(w[col]) if w[col] else None
        field_sources[field] = None if s is None else {
            "source_id": s["source_id"], "label": _labels(registry, s["device_key"], s["family"]),
            "source_name": s["source_name"]}
    workout = _row(w, zone)
    workout.update(hr_min=w["hr_min"], hr_max=w["hr_max"], distance_type=w["distance_type"], strain=w["strain"],
                   rule_version=w["rule_version"])
    route = None
    if w["route_id"]:
        link = next(iter(db.fetchall(conn, "SELECT link_status FROM workout_routes WHERE route_ref_id = ?",
                                     [w["route_source"]])), None)
        shape = route_outline(conn, w["route_id"])
        if shape is not None:
            route = {"link_status": link[0] if link else None, "n_points": shape["n_points_total"],
                     "outline": shape["outline"], "width": shape["width"], "height": shape["height"]}
    return {"reporting_zone": zone.key, "workout": workout, "field_sources": field_sources,
            "sources": [_source(conn, registry, s) for s in srcs], "route": route}


# ---------- ECG ----------

ECG_ID = re.compile(r"^ecg:\d{8}T\d{6}Z$")
WAVEFORM_MAX_SAMPLES = 100_000
CLASSIFICATION_NOTE = ("The classification is device-reported: Apple's ECG app's own words, as recorded on the "
                       "watch. Helios does not interpret ECGs.")


class WaveformTooLong(ValueError):
    """More samples than a screen draws: the route answers 422 (a 30 s ECG is about 15,400)."""


def _ecg_local(v: datetime | None, offset_min: int | None, zone: ZoneInfo) -> str | None:
    """The recording instant as the export printed it (its own offset), else in the reporting zone."""
    if v is None:
        return None
    utc = v.replace(tzinfo=timezone.utc)
    tz = timezone(timedelta(minutes=offset_min)) if offset_min is not None else zone
    return utc.astimezone(tz).isoformat(timespec="seconds")


def list_ecgs(conn, zone: ZoneInfo, limit: int, cursor: str | None) -> dict:
    where, params = _keyset("e.recorded_utc", "e.ecg_id", cursor)
    rows = db.fetchdicts(conn, f"""
        SELECT e.ecg_id, e.recorded_utc, e.print_offset_min, e.classification, e.symptoms, e.device,
               e.software_version, e.sample_rate_hz, e.lead, e.unit, e.n_samples,
               EXISTS (SELECT 1 FROM ecg_waveforms w WHERE w.ecg_id = e.ecg_id) AS has_waveform
        FROM ecgs e WHERE {where}
        ORDER BY e.recorded_utc DESC NULLS LAST, e.ecg_id DESC LIMIT ?""", params + [limit + 1])
    more = len(rows) > limit
    rows = rows[:limit]
    out = [{"ecg_id": r["ecg_id"], "recorded_utc": _iso_utc(r["recorded_utc"]),
            "recorded_local": _ecg_local(r["recorded_utc"], r["print_offset_min"], zone),
            "classification": r["classification"], "symptoms": r["symptoms"] or None, "device": r["device"],
            "software_version": r["software_version"], "sample_rate_hz": r["sample_rate_hz"], "lead": r["lead"],
            "unit": r["unit"], "n_samples": r["n_samples"],
            "duration_s": (round(r["n_samples"] / r["sample_rate_hz"], 1)
                           if r["n_samples"] and r["sample_rate_hz"] else None),
            "has_waveform": bool(r["has_waveform"])} for r in rows]
    total, newest = db.fetchall(conn, "SELECT COUNT(*), MAX(recorded_utc) FROM ecgs")[0]
    return {"reporting_zone": zone.key, "total": int(total), "archive": _archive(conn, "ecgs", "recorded_utc"),
            "newer_records": _newer_records(conn, "Electrocardiogram", newest),
            "classification_note": CLASSIFICATION_NOTE, "ecgs": out,
            "next_cursor": encode_cursor(rows[-1]["recorded_utc"], rows[-1]["ecg_id"]) if more and rows else None}


def ecg_waveform(conn, ecg_id: str) -> dict | None:
    """One recording's voltages at full rate, rounded to whole microvolts for drawing (the store
    keeps the recorded values). The recording's own lead first. None when unknown or missing."""
    if not ECG_ID.match(ecg_id):
        return None
    rows = db.fetchdicts(conn, """
        SELECT w.lead, w.unit, w.sample_rate_hz, len(w.microvolts) AS n FROM ecg_waveforms w
        JOIN ecgs e USING (ecg_id) WHERE w.ecg_id = ?
        ORDER BY (w.lead = e.lead) DESC, w.lead LIMIT 1""", [ecg_id])
    if not rows:
        return None
    r = rows[0]
    if (r["n"] or 0) > WAVEFORM_MAX_SAMPLES:
        raise WaveformTooLong(f"{r['n']} samples; at most {WAVEFORM_MAX_SAMPLES} are drawn")
    # Rounded in the store's engine: one list comes back, not a Python loop over doubles.
    mv = db.fetchall(conn, "SELECT list_transform(microvolts, v -> CAST(round(v) AS INTEGER)) FROM ecg_waveforms "
                           "WHERE ecg_id = ? AND lead = ?", [ecg_id, r["lead"]])[0][0]
    return {"ecg_id": ecg_id, "lead": r["lead"], "unit": r["unit"], "sample_rate_hz": r["sample_rate_hz"],
            "n_samples": len(mv), "microvolts": mv}
