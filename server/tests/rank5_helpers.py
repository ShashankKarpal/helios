"""Shared helpers for the rank 5 tests (2026-10-29): a store filled with
synthetic Phase 2 rows (workouts, their sources, statistics, events, metadata,
routes, ECGs and waveforms) and Bridge raw records, and a signed-in test
client over it. Every value is made up; the route points sit near 0,0 with
decimals no test fixture elsewhere uses, so a test can prove they never leave
the server as numbers."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from heliosd import main
from heliosd.config import Settings
from heliosd.main import create_app
from heliosd.store import db

TOKEN = "test-token-rank5-0123456789"
BASE = "https://testserver"
AUTH = {"X-Helios-Token": TOKEN}
T0 = datetime(2026, 1, 1, 4, 0, 0)      # UTC; 08:00 in the fixture's Asia/Dubai zone
# Distinctive synthetic coordinates: the route tests search the response for them.
LAT0, LON0 = 0.0123457, 0.0234561


def client_for(tmp_path: Path, monkeypatch, db_path: Path | None = None, extra: dict | None = None) -> TestClient:
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text("<!doctype html><div id=root></div>\n", encoding="utf-8")
    monkeypatch.setenv("HELIOS_WEB_DIST", str(dist))
    monkeypatch.setattr(main, "SESSION_FAIL_DELAY_S", 0.0)
    raw = {"server": {"ingest_token": TOKEN},
           "storage": {"db_path": str(db_path or tmp_path / "h.duckdb")},
           "notifications": {"macos_alerts": False},
           # a dead port: no test may reach the Mac's real LM Studio
           "llm": {"base_url": "http://127.0.0.1:9/v1", "timeout_seconds": 1}}
    for section, values in (extra or {}).items():
        raw.setdefault(section, {}).update(values)
    return TestClient(create_app(Settings(raw=raw)), base_url=BASE)


def _insert(conn, table: str, row: dict) -> None:
    cols = ", ".join(row)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))


def source_row(source_id: str, workout_id: str, role: str, device_key: str, family: str, start: datetime,
               minutes: float, activity: str, ordinal: int) -> dict:
    return {"source_id": source_id, "workout_id": workout_id, "role": role,
            "link_rule": "anchor" if role == "anchor" else "candidate_v1", "family": family,
            "activity_type": activity, "activity_family": "walking", "duration_s": minutes * 60,
            "duration_raw": str(minutes), "duration_unit": "min", "source_name": f"Synthetic {family} app",
            "source_version": "1.0", "device": "Synthetic device string", "device_key": device_key,
            "start_utc": start, "end_utc": start + timedelta(minutes=minutes), "tz_name": "Asia/Dubai",
            "sync_identifier": "syn-sync", "sync_version": 1, "match_detail": "{}",
            "independence": "unknown", "file_sha": "syn-sha", "ordinal": ordinal, "run_id": "syn-run"}


def add_workout(conn, n: int, start: datetime, *, status: str = "single", with_whoop: bool = False,
                route_points: int = 0, hr: bool = True, distance: bool = True, minutes: float = 42.0,
                activity: str = "HKWorkoutActivityTypeWalking") -> str:
    """One session, its anchor (Apple) and optionally a Whoop member, with statistics,
    events and metadata on the anchor and a linked route when route_points > 0."""
    anchor = f"xw:{n:024x}:{n}"
    wid = f"wk:{anchor}"
    _insert(conn, "workout_sources", source_row(anchor, wid, "anchor", "apple_watch_ultra", "apple",
                                               start, minutes, activity, n))
    if with_whoop:
        _insert(conn, "workout_sources", source_row(f"xw:{n:024x}:w{n}", wid, "member", "whoop", "whoop",
                                                   start + timedelta(minutes=1), minutes - 1, activity, n + 100000))
    route_id = None
    if route_points:
        route_id = f"rt:{n:024x}"
        _insert(conn, "workout_routes", {"route_ref_id": f"{anchor}:route:0", "source_id": anchor, "ord": 0,
                                         "file_ref": "/workout-routes/synthetic.gpx", "route_id": route_id,
                                         "link_status": "linked", "start_utc": start})
        for seq in range(route_points):
            a = 2 * math.pi * seq / route_points
            _insert(conn, "route_points", {"route_id": route_id, "seq": seq, "time_utc": start + timedelta(seconds=seq),
                                           "lat": LAT0 + 0.003 * math.sin(a), "lon": LON0 + 0.005 * math.cos(a)})
    _insert(conn, "workouts", {
        "workout_id": wid, "status": status, "n_sources": 2 if with_whoop else 1,
        "activity_type": activity, "activity_family": "walking", "type_source": anchor,
        "start_utc": start, "end_utc": start + timedelta(minutes=minutes), "duration_s": minutes * 60,
        "timing_source": anchor, "tz_name": "Asia/Dubai", "tz_source": anchor,
        "distance": 3.125 if distance else None, "distance_unit": "km" if distance else None,
        "distance_type": "HKQuantityTypeIdentifierDistanceWalkingRunning" if distance else None,
        "distance_source": anchor if distance else None,
        "energy_kcal": 186.5, "energy_unit": "kcal", "energy_source": anchor,
        "hr_avg": 118.5 if hr else None, "hr_min": 92.0 if hr else None, "hr_max": 141.0 if hr else None,
        "hr_unit": "count/min" if hr else None, "hr_source": anchor if hr else None,
        "strain": 9.375 if with_whoop else None, "strain_source": f"xw:{n:024x}:w{n}" if with_whoop else None,
        "route_id": route_id, "route_source": f"{anchor}:route:0" if route_id else None,
        "rule_version": "v1", "run_id": "syn-run"})
    _insert(conn, "workout_statistics", {"source_id": anchor, "ord": 0,
                                         "hk_type": "HKQuantityTypeIdentifierActiveEnergyBurned",
                                         "start_utc": start, "end_utc": start + timedelta(minutes=minutes),
                                         "stat_sum": 186.5, "unit": "kcal"})
    for i, kind in enumerate(["HKWorkoutEventTypeSegment"] * 3 + ["HKWorkoutEventTypePause"]):
        _insert(conn, "workout_events", {"source_id": anchor, "ord": i, "event_type": kind,
                                         "date_utc": start + timedelta(minutes=i), "duration": 1.0,
                                         "duration_unit": "min", "metadata": None})
    for i, (k, v) in enumerate([("HKIndoorWorkout", "0"), ("HKMetadataKeySyncIdentifier", "syn-sync"),
                                ("HKMetadataKeySyncVersion", "1"), ("HKIndoorWorkout", "0")]):
        _insert(conn, "workout_metadata", {"source_id": anchor, "ord": i, "meta_key": k, "meta_value": v})
    return wid


def add_raw(conn, element: str, n: int, start: datetime) -> None:
    """A Bridge raw record (element Workout or Electrocardiogram), as ingest/bridge_v2.py keys it."""
    _insert(conn, "raw_records", {"record_id": f"hk:syn-{element}-{n}", "element": element,
                                  "hk_type": "HKWorkoutTypeIdentifier", "start_utc": start,
                                  "file_sha": "bridge", "ordinal": n})


# A made-up periodic shape in microvolts (a sum of sines), not a heart recording.
WAVE_SQL = "round(400 * sin(2 * pi() * i / 400) + 60 * sin(2 * pi() * i / 37) + 0.437, 3)::DOUBLE"


def add_ecg(conn, recorded: datetime, *, classification: str = "Sinus Rhythm", symptoms: str | None = None,
            rate: float = 512.0, n_samples: int | None = None, waveform: bool = True, offset_min: int = 240) -> str:
    ecg_id = "ecg:" + recorded.strftime("%Y%m%dT%H%M%SZ")
    n = n_samples if n_samples is not None else int(round(rate * 30))
    _insert(conn, "ecgs", {"ecg_id": ecg_id, "recorded_raw": "synthetic", "recorded_utc": recorded,
                           "print_offset_min": offset_min, "classification": classification, "symptoms": symptoms,
                           "software_version": "2.0", "device": "Watch7,1", "sample_rate_hz": rate,
                           "sample_rate_raw": f"{rate} hertz", "lead": "Lead I", "unit": "µV", "n_samples": n,
                           "voltage_sha": "syn", "export_label": "Synthetic-Export", "rel_path": "x.csv",
                           "file_sha": "syn", "run_id": "syn-run"})
    if waveform:
        # Built inside DuckDB: binding 15,000 Python floats one by one costs seconds per ECG.
        conn.execute("INSERT INTO ecg_waveforms (ecg_id, lead, unit, sample_rate_hz, n_samples, microvolts) "
                     f"SELECT ?, ?, ?, ?, ?, list_transform(range(?), i -> {WAVE_SQL})",
                     [ecg_id, "Lead I", "µV", rate, n, n])
    return ecg_id


def new_store(tmp_path: Path, fill) -> Path:
    path = tmp_path / "h.duckdb"
    conn = db.connect(path)
    try:
        fill(conn)
    finally:
        conn.close()
    return path
