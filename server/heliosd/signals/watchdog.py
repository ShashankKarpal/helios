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


CORROBORATION_NOTE = ("Informational: this device is not the primary for the metric and "
                      "the primary is current, so the daily value is unaffected. Its "
                      "corroboration has lapsed; the fix text says how to revive it.")
WHOOP_CLOUD_FIX = ("The Whoop cloud puller has not stored a recovery for two days. If the "
                   "last error mentions the token or 401, re-authorize once at "
                   "/whoop/login; otherwise check network and POST /api/whoop/pull.")
SOURCE_FIX = ("This informational feed has stopped updating. It is a file another app "
              "writes; check that app is running and still pointed at the same path.")


def _fix_for(primary_dk: str, bridge_delivering: bool) -> str:
    if not bridge_delivering:
        return BRIDGE_FIX
    if primary_dk.startswith("zepp") and "scale" not in primary_dk:
        return ZEPP_FIX
    if primary_dk == "whoop":
        return WHOOP_FIX
    if primary_dk.startswith("apple_watch"):
        return WATCH_FIX
    return WRITER_FIX


def _age_hours(now: datetime, then: datetime) -> float:
    return (now - then).total_seconds() / 3600


def _source_last_seen(spec: dict) -> datetime | None:
    """Last activity of an external file feed: the timestamp in its last JSONL
    line when it has one (ISO 8601, Z or offset), else the file mtime. Never
    raises; an unreadable feed reads as never seen."""
    import json
    import os
    from datetime import timezone
    path = os.path.expanduser(str(spec.get("path") or ""))
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", errors="replace").strip().splitlines()
        for line in reversed(tail):
            try:
                ts = json.loads(line).get(spec.get("ts_field", "ts"))
                if ts:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    if dt.tzinfo is not None:
                        dt = dt.astimezone().replace(tzinfo=None)
                    return dt
            except (ValueError, AttributeError, json.JSONDecodeError):
                continue
        return datetime.fromtimestamp(os.path.getmtime(path))
    except OSError:
        return None


def check_sources(policy: MetricPolicy, now: datetime | None = None) -> list[dict]:
    """Informational file feeds declared under `sources:` in the metric policy
    (normally only in the HELIOS_HOME overlay). Same 2x/4x cadence rule as
    metrics, never notified, reported so `freshness` shows them."""
    now = now or datetime.now()
    out: list[dict] = []
    for spec in getattr(policy, "sources", []) or []:
        key = str(spec.get("key") or "").strip()
        if not key:
            continue
        cadence = float(spec.get("cadence_hours", 24))
        last = _source_last_seen(spec)
        if last is None:
            status, age = "silent", None
        else:
            age = _age_hours(now, last)
            if age <= 2 * cadence:
                continue
            status = "silent" if age > 4 * cadence else "stale"
        out.append({"metric": "*", "device_key": key, "last_seen": str(last) if last else None,
                    "age_hours": round(age, 1) if age is not None else None,
                    "status": status, "tier": "informational", "notify": False,
                    "fix": str(spec.get("fix") or SOURCE_FIX)})
    return out


def whoop_cloud_status(conn, now: datetime, enabled: bool, last_error: str | None) -> dict | None:
    """One row when the Whoop cloud puller is behind or its last run failed.
    Before this, a rejected refresh token only reached the log (audit B9)."""
    if not enabled:
        return None
    rows = db.fetchall(conn, "SELECT MAX(date) FROM whoop_cache WHERE kind = 'recovery'")
    last_date = rows[0][0] if rows and rows[0][0] else None
    behind = last_date is None or (now.date() - last_date).days > 2
    if not behind and not last_error:
        return None
    fix = WHOOP_CLOUD_FIX
    if last_error:
        fix += f" Last error: {last_error}"
    return {"metric": "*", "device_key": "whoop_cloud",
            "last_seen": str(last_date) if last_date else None,
            "age_hours": round(_age_hours(now, datetime.combine(last_date, datetime.min.time())), 1) if last_date else None,
            "status": "silent" if behind else "error", "fix": fix}


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
          registry=None, whoop: dict | None = None) -> list[dict]:
    """Sync report, worst first. Entries carry `status` (silent | stale | error
    | corroboration_decayed), and informational ones carry `tier:
    "informational"` and `notify: False`: they are listed for the freshness
    surfaces but never posted as a macOS notification. `whoop` is
    {"enabled": bool, "last_error": str | None} from the daemon, optional."""
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
            # The metric is healthy. Corroboration tier (audit B3): a lower
            # ranked device that has gone quiet for 4x its cadence is reported
            # as informational, never notified. Before this, the Whoop-via-
            # HealthKit copy of heart rate died for weeks with zero visibility
            # because the primary stayed fresh.
            for dk, ls in present[1:]:
                age = _age_hours(now, ls)
                if age > 4 * cadence:
                    report.append({"metric": metric, "device_key": dk, "last_seen": str(ls),
                                   "age_hours": round(age, 1),
                                   "status": "corroboration_decayed",
                                   "tier": "informational", "notify": False,
                                   "fix": _fix_for(dk, bridge_delivering) + " " + CORROBORATION_NOTE})
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
    if whoop:
        w = whoop_cloud_status(conn, now, bool(whoop.get("enabled")), whoop.get("last_error"))
        if w:
            report.append(w)
    report.extend(check_sources(policy, now))
    # Worst first: the bridge itself, then error and silent before stale, then
    # informational rows last, then by metric. The hourly loop notifies the
    # first row whose notify flag is not False; in policy-file order that was
    # an arbitrary stale metric while the bridge entry sat last (audit 2026-09-02).
    rank = {"error": 0, "silent": 0, "stale": 1, "corroboration_decayed": 5}
    report.sort(key=lambda e: (e["device_key"] != "bridge", e.get("tier") == "informational",
                               rank.get(e["status"], 9), e["metric"]))
    return report


def notifiable(report: list[dict]) -> dict | None:
    """The entry the hourly loop may post as a notification: worst-first order,
    skipping informational rows."""
    for e in report:
        if e.get("notify") is not False:
            return e
    return None


def notify_macos(title: str, message: str) -> None:
    """Local macOS notification via osascript. No-op off macOS."""
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{message}" with title "{title}"'],
                       capture_output=True, timeout=5)
    except Exception:
        pass
