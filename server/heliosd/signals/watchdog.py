"""Sync watchdog: detects silently dead metric streams (the 2026-04-29 problem)
and a silent Bridge, and says exactly how to fix them.

It only alarms when a metric has genuinely gone stale, meaning EVERY source in
its priority list is late. If any source is still fresh, the daily value falls
back to it, so the number Helios shows is current and there is nothing to fix.
This is what stops a batch-syncing band (Amazfit writes to Apple Health only
when it syncs to its app, so it always lags) from raising an alert while the
wrist watch keeps heart rate fresh, even though the band outranks the watch for
that metric. Metrics flagged optional (for example dietary energy, which depends
on the owner logging food) are not watched at all.
"""

from __future__ import annotations

import subprocess
from datetime import datetime

from heliosd.store import db
from heliosd.trust.policy import MetricPolicy

# Metrics that only have data when the owner opts in (manual logging, occasional
# devices). Silence here is expected, not a fault, so the watchdog skips them.
OPTIONAL_METRICS = {"dietary_energy"}

ZEPP_FIX = ("Amazfit/Zepp writes to Apple Health only when the band syncs to the "
            "Zepp app over Bluetooth, so its samples always lag. Open the Zepp app "
            "and pull to refresh to force a sync. This is a corroboration source, so "
            "lag here does not affect the primary number for this metric.")
BRIDGE_FIX = ("Open Helios Bridge on the iPhone and tap Sync Now. If it will not launch, "
              "re-sign from Xcode (check the profile expiry in the Bridge status screen).")


def check(conn, policy: MetricPolicy, now: datetime | None = None,
          registry=None) -> list[dict]:
    if registry is None:
        from heliosd.trust.registry import SourceRegistry
        registry = SourceRegistry()
    now = now or datetime.now()
    report: list[dict] = []
    for metric, m in policy.metrics.items():
        if m.get("optional") or metric in OPTIONAL_METRICS:
            continue
        cadence = policy.cadence_hours(metric)
        priority = policy.priority(metric)
        rows = db.fetchall(conn, """
            SELECT device_key, MAX(COALESCE(end_ts, start_ts)) FROM samples
            WHERE metric = ? GROUP BY device_key""", [metric])
        seen = {dk: ls for dk, ls in rows if ls is not None}
        # Present sources for this metric, in priority order, excluding retired
        # devices. present[0] is the preferred one (what we would ideally use).
        present = [(dk, seen[dk]) for dk in priority
                   if dk in seen and dk not in registry.inactive]
        if not present:
            continue
        # The metric is only stale if EVERY source is late. If the freshest one
        # is current, the daily value falls back to it and nothing needs fixing,
        # no matter how far the preferred (higher-priority) device has lagged.
        freshest_age = min((now - ls).total_seconds() / 3600 for _, ls in present)
        if freshest_age <= 2 * cadence:
            continue
        status = "silent" if freshest_age > 4 * cadence else "stale"
        primary_dk, primary_ls = present[0]
        fix = ZEPP_FIX if primary_dk == "zepp_helio" else BRIDGE_FIX
        report.append({"metric": metric, "device_key": primary_dk,
                       "last_seen": str(primary_ls),
                       "age_hours": round((now - primary_ls).total_seconds() / 3600, 1),
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
