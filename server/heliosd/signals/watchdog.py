"""Sync watchdog: detects silently dead metric streams (the 2026-04-29 problem)
and a silent Bridge, and says exactly how to fix them."""

from __future__ import annotations

import subprocess
from datetime import datetime

from heliosd.store import db
from heliosd.trust.policy import MetricPolicy

ZEPP_FIX = ("Zepp > Profile > Add accounts > Health: toggle all metrics off and on. "
            "iPhone Health app > Profile > Apps > Zepp: enable all write categories. "
            "Force-quit Zepp, reopen, confirm fresh samples.")
BRIDGE_FIX = ("Open Helios Bridge on the iPhone and tap Sync Now. If it will not launch, "
              "re-sign from Xcode (check the profile expiry in the Bridge status screen).")


def check(conn, policy: MetricPolicy, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    report: list[dict] = []
    for metric, m in policy.metrics.items():
        cadence = policy.cadence_hours(metric)
        rows = db.fetchall(conn, """
            SELECT device_key, MAX(COALESCE(end_ts, start_ts)) FROM samples
            WHERE metric = ? GROUP BY device_key""", [metric])
        for device_key, last_seen in rows:
            if last_seen is None:
                continue
            age_h = (now - last_seen).total_seconds() / 3600
            status = "ok"
            if age_h > 4 * cadence:
                status = "silent"
            elif age_h > 2 * cadence:
                status = "stale"
            if status != "ok":
                fix = ZEPP_FIX if device_key == "zepp_helio" else BRIDGE_FIX
                report.append({"metric": metric, "device_key": device_key,
                               "last_seen": str(last_seen), "age_hours": round(age_h, 1),
                               "status": status, "fix": fix})
    last_batch = db.fetchall(conn, "SELECT MAX(received_at) FROM sync_log WHERE sync_path = 'bridge'")
    if last_batch and last_batch[0][0]:
        age_h = (now - last_batch[0][0]).total_seconds() / 3600
        if age_h > 12:
            report.append({"metric": "*", "device_key": "bridge", "last_seen": str(last_batch[0][0]),
                           "age_hours": round(age_h, 1), "status": "silent", "fix": BRIDGE_FIX})
    return report


def notify_macos(title: str, message: str) -> None:
    """Local macOS notification via osascript. No-op off macOS."""
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{message}" with title "{title}"'],
                       capture_output=True, timeout=5)
    except Exception:
        pass
