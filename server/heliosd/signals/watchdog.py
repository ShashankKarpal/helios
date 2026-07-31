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

Blame attribution (added 2026-07-24): a stale metric has two very different
causes and the fix text must not confuse them.
- If the Bridge is DELIVERING (recent sync_log batches), the pipeline is fine
  and the stall is upstream: the writer app (Whoop, Zepp, Watch) has stopped
  writing into Apple Health, so the phone has nothing to ship. Telling the
  owner to "open Helios Bridge and tap Sync Now" was wrong and eroded trust.
- If the Bridge itself is silent, the fix is about reachability (Mac asleep,
  different network), not about the writer apps.
Additionally, when Whoop cloud data is current, heart-rate-family alerts note
that recovery/HRV/RHR shown by Helios remain live via the cloud overlay.
"""

from __future__ import annotations

import subprocess
from datetime import datetime

from heliosd.store import db
from heliosd.trust.policy import MetricPolicy

# Metrics that only have data when the owner opts in (manual logging, occasional
# devices). Silence here is expected, not a fault, so the watchdog skips them.
OPTIONAL_METRICS = {"dietary_energy"}

# Metrics whose headline values stay live through the Whoop cloud overlay even
# when the HealthKit stream is behind. Names match config/metric_policy.yaml.
# hrv_sdnn is deliberately absent: that is the Watch's number, not Whoop's.
WHOOP_CLOUD_METRICS = {"heart_rate", "resting_hr", "hrv_rmssd",
                       "respiratory_rate", "recovery_score", "strain"}

ZEPP_FIX = ("Amazfit/Zepp writes to Apple Health only when the band syncs to the "
            "Zepp app over Bluetooth, so its samples always lag. Open the Zepp app "
            "and pull to refresh to force a sync. This is a corroboration source, so "
            "lag here does not affect the primary number for this metric.")
WHOOP_FIX = ("The Whoop app has stopped writing to Apple Health (the band itself keeps "
             "recording and Whoop cloud stays current). Open the Whoop app on the iPhone "
             "so it syncs and backfills Health; the bridge then ships it automatically. "
             "The bridge is fine, do not touch it.")
WATCH_FIX = ("No recent samples from the Apple Watch. Wear it (and unlock it once) so it "
             "writes to Health; the bridge ships new samples automatically.")
WRITER_FIX = ("The source app for this metric has stopped writing to Apple Health. Open "
              "that app on the iPhone so it syncs; the bridge ships new samples "
              "automatically. The bridge itself is delivering fine.")
BRIDGE_FIX = ("The phone has not delivered batches recently. Usually the Mac was asleep "
              "or the phone was on a different network; batches queue safely on the phone "
              "and drain on reconnect, so keep the Mac awake while on power (or move "
              "heliosd to the always-on relay). If both are awake on the same network and "
              "this persists, open Helios Bridge once and check its Mac link row.")
CLOUD_COVER_NOTE = (" Whoop cloud is current, so recovery, HRV, and resting HR shown by "
                    "Helios remain live via the overlay; only the raw HealthKit stream "
                    "is behind.")


def _bridge_age_hours(conn, now: datetime) -> float | None:
    rows = db.fetchall(conn, "SELECT MAX(received_at) FROM sync_log WHERE sync_path = 'bridge'")
    if rows and rows[0][0]:
        return (now - rows[0][0]).total_seconds() / 3600
    return None


def _whoop_cloud_fresh(conn, now: datetime) -> bool:
    """True when the Whoop cloud cache has recovery data for today or yesterday."""
    try:
        rows = db.fetchall(conn, "SELECT MAX(date) FROM whoop_cache WHERE kind = 'recovery'")
        if rows and rows[0][0]:
            return (now.date() - rows[0][0]).days <= 1
    except Exception:
        pass
    return False


def _fix_for(primary_dk: str, bridge_delivering: bool) -> str:
    if not bridge_delivering:
        return BRIDGE_FIX
    if primary_dk == "zepp_helio":
        return ZEPP_FIX
    if primary_dk == "whoop":
        return WHOOP_FIX
    if primary_dk.startswith("apple_watch"):
        return WATCH_FIX
    return WRITER_FIX


def _snoozed(m: dict, now: datetime) -> bool:
    """True while the metric's snooze_until (config/metric_policy.yaml) has not
    passed. Lets the owner mute an expected silence (for example travel away
    from the scale) without marking the metric permanently optional. YAML may
    hand us a date, a datetime, or a string; accept all three."""
    s = m.get("snooze_until")
    if not s:
        return False
    if isinstance(s, datetime):
        s = s.date()
    elif isinstance(s, str):
        try:
            s = datetime.strptime(s.strip(), "%Y-%m-%d").date()
        except ValueError:
            return False
    return now.date() <= s


def check(conn, policy: MetricPolicy, now: datetime | None = None,
          registry=None) -> list[dict]:
    if registry is None:
        from heliosd.trust.registry import SourceRegistry
        registry = SourceRegistry()
    now = now or datetime.now()
    report: list[dict] = []

    bridge_age = _bridge_age_hours(conn, now)
    # Delivering means batches landed within the last 3 hours: generous enough
    # for a Mac that sleeps between hourly background wakes, strict enough to
    # catch a genuinely dead pipeline.
    bridge_delivering = bridge_age is not None and bridge_age <= 3
    cloud_fresh = _whoop_cloud_fresh(conn, now)

    for metric, m in policy.metrics.items():
        if m.get("optional") or metric in OPTIONAL_METRICS:
            continue
        if _snoozed(m, now):
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
        fix = _fix_for(primary_dk, bridge_delivering)
        cloud_cover = (cloud_fresh and primary_dk == "whoop"
                       and metric in WHOOP_CLOUD_METRICS)
        if cloud_cover:
            fix += CLOUD_COVER_NOTE
        entry = {"metric": metric, "device_key": primary_dk,
                 "last_seen": str(primary_ls),
                 "age_hours": round((now - primary_ls).total_seconds() / 3600, 1),
                 "status": status, "fix": fix}
        if cloud_cover:
            entry["cloud_cover"] = True
        report.append(entry)

    if bridge_age is not None and bridge_age > 12:
        last_batch = db.fetchall(conn, "SELECT MAX(received_at) FROM sync_log WHERE sync_path = 'bridge'")
        report.append({"metric": "*", "device_key": "bridge",
                       "last_seen": str(last_batch[0][0]),
                       "age_hours": round(bridge_age, 1),
                       "status": "silent", "fix": BRIDGE_FIX})
    return report


def notify_macos(title: str, message: str) -> None:
    """Local macOS notification via osascript. No-op off macOS."""
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{message}" with title "{title}"'],
                       capture_output=True, timeout=5)
    except Exception:
        pass
