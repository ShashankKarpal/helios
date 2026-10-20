"""Shared helpers for the Phase 2 tests: the synthetic export folder copied to a temp
directory with a fresh sha256 manifest, a July-style twin of it (the same recordings
printed in another zone and renamed), and a store holding the samples the registered
records should link to. Synthetic values only."""

from __future__ import annotations

import hashlib
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from heliosd.store import db
from heliosd.trust.policy import MetricPolicy

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "phase2" / "june"
EXPORT_DAY = "2026-05-06"


def write_manifest(folder: Path, name: str = "MANIFEST-test.sha256") -> Path:
    lines = []
    for p in sorted(folder.rglob("*")):
        if p.is_file() and not p.name.startswith(".") and "sha256" not in p.name:
            lines.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(folder).as_posix()}")
    out = folder / name
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def june(tmp_path: Path, name: str = "Synthetic-Export-2026-05-06") -> Path:
    dest = tmp_path / name
    shutil.copytree(FIXTURE, dest)
    write_manifest(dest)
    return dest


def _shift_print(s: str, minutes: int, offset: str) -> str:
    wall = datetime.fromisoformat(s[:19]) + timedelta(minutes=minutes)
    return wall.strftime("%Y-%m-%d %H:%M:%S") + " " + offset


def july_twin(june_folder: Path, tmp_path: Path, name: str = "Synthetic-Export-2026-06-06") -> Path:
    """Side files only, as the SSD's July copy: every ECG printed at +0530 (same instant), every
    route under a new local-time file name, the same attachment."""
    dest = tmp_path / name
    (dest / "electrocardiograms").mkdir(parents=True)
    (dest / "workout-routes").mkdir()
    shutil.copytree(june_folder / "attachments", dest / "attachments")
    for p in sorted((june_folder / "electrocardiograms").glob("*.csv")):
        text = p.read_text(encoding="utf-8")
        m = re.search(r"^Recorded Date,(.+)$", text, re.M)
        new = _shift_print(m.group(1), 90, "+0530")
        (dest / "electrocardiograms" / ("ecg_" + new[:10] + "_b.csv")).write_text(
            text.replace(m.group(0), "Recorded Date," + new), encoding="utf-8")
    for i, p in enumerate(sorted((june_folder / "workout-routes").glob("*.gpx"))):
        shutil.copy(p, dest / "workout-routes" / f"route_renamed_{i}.gpx")
    write_manifest(dest, "_manifest.sha256")
    return dest


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s)


SAMPLE_SQL = ("INSERT INTO samples (sample_id, hk_uuid, metric, hk_type, value, text_value, unit, start_ts, end_ts, "
              "start_utc, end_utc, source_name, device_key, sync_path, time_source, quality) "
              "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")


def _row(sid, metric, hk_type, value, unit, start, end, source, device, path="bridge", text=None, quality=None):
    s, e = utc(start), utc(end)
    return [sid, sid[3:] if sid.startswith("hk:") else None, metric, hk_type, value, text, unit,
            s + timedelta(hours=4), e + timedelta(hours=4), s, e, source, device, path,
            "bridge_utc" if path == "bridge" else "export_linked_v1", quality]


def store(tmp_path: Path, name: str = "copy.duckdb") -> Path:
    """A store with the policy registry and the samples the fixture's registered records meet:
    twins for two heart-rate records, one of the two identical step records, the SDNN record, the
    Apple core sleep, an earlier export's heart-rate row, one energy row within the tolerance and
    one outside it; a row at the third heart-rate record's instant under another unit label; an
    earlier export's copy of a Bridge row with its alias; a tombstone at the Whoop deep sleep's
    instant; a heart-rate row after the fixture's records that the export does not hold; daily
    values beside them."""
    path = tmp_path / name
    conn = db.connect(path)
    MetricPolicy().sync_registry(conn)
    hr, st = "HKQuantityTypeIdentifierHeartRate", "HKQuantityTypeIdentifierStepCount"
    rows = [
        _row("hk:syn-hr-1", "heart_rate", hr, 71.25, "count/min", "2026-05-01 06:05:00", "2026-05-01 06:05:00",
             "Synthetic Ultra 1", "apple_watch_ultra"),
        _row("hk:syn-hr-2", "heart_rate", hr, 74.5, "count/min", "2026-05-01 06:10:00", "2026-05-01 06:10:00",
             "Synthetic Ultra 1", "apple_watch_ultra"),
        _row("hk:syn-steps-1", "steps", st, 121.0, "count", "2026-05-01 06:00:00", "2026-05-01 06:05:00",
             "Synthetic Ultra 1", "apple_watch_ultra"),
        _row("hk:syn-sdnn-1", "hrv_sdnn", "HKQuantityTypeIdentifierHeartRateVariabilitySDNN", 45.125, "ms",
             "2026-05-01 03:00:00", "2026-05-01 03:01:00", "Synthetic Ultra 1", "apple_watch_ultra"),
        _row("hk:syn-sleep-1", "sleep_analysis", "HKCategoryTypeIdentifierSleepAnalysis", 60.0, "min",
             "2026-04-30 21:00:00", "2026-04-30 22:00:00", "Synthetic Ultra 1", "apple_watch_ultra", text="core"),
        _row("ch2:syn-export-hr", "heart_rate", hr, 66.5, "count/min", "2026-05-02 04:00:00", "2026-05-02 04:00:00",
             "Synthetic Ultra 1", "apple_watch_ultra", path="health_export"),
        _row("hk:syn-energy-1", "active_energy", "HKQuantityTypeIdentifierActiveEnergyBurned", 3.52, "kcal",
             "2026-05-01 06:00:00", "2026-05-01 06:01:00", "Synthetic Ultra 1", "apple_watch_ultra"),
        _row("hk:syn-energy-2", "active_energy", "HKQuantityTypeIdentifierActiveEnergyBurned", 6.0, "kcal",
             "2026-05-01 06:01:00", "2026-05-01 06:02:00", "Synthetic Ultra 1", "apple_watch_ultra"),
        _row("hk:syn-hr-late", "heart_rate", hr, 70.0, "count/min", "2026-05-05 08:00:00", "2026-05-05 08:00:00",
             "Synthetic Ultra 1", "apple_watch_ultra"),
        # at the third heart-rate record's instant, but under a unit label the store never normalised
        _row("hk:syn-hr-3-label", "heart_rate", hr, 79.75, "beats/min", "2026-05-01 06:15:00", "2026-05-01 06:15:00",
             "Synthetic Ultra 1", "apple_watch_ultra", quality="unit_mismatch"),
        # an earlier export's copy of the first heart-rate Bridge row, linked to it (the Wave 2 relink)
        _row("ch2:syn-export-dup-hr1", "heart_rate", hr, 71.25, "count/min", "2026-05-01 06:05:00", "2026-05-01 06:05:00",
             "Synthetic Ultra 1", "apple_watch_ultra", path="health_export", quality="export_duplicate"),
    ]
    conn.executemany(SAMPLE_SQL, rows)
    conn.execute("INSERT INTO sample_aliases (old_id, new_id, reason) VALUES "
                 "('ch2:syn-export-dup-hr1', 'hk:syn-hr-1', 'export_link_v2')")
    conn.execute("INSERT INTO tombstones (tomb_id, hk_uuid, metric, start_utc, reason) VALUES "
                 "('hk:syn-deleted', 'syn-deleted', 'sleep_analysis', '2026-04-30 22:00:00', 'bridge_deleted')")
    conn.execute("INSERT INTO daily_values (date, metric, value, unit, device_key, n_samples) VALUES "
                 "('2026-05-01', 'steps', 121, 'count', 'apple_watch_ultra', 1), "
                 "('2026-05-01', 'heart_rate', 72.875, 'count/min', 'apple_watch_ultra', 2)")
    conn.close()
    return path
