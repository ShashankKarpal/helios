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
from datetime import datetime, timedelta, timezone

from heliosd.ingest.normalize import to_wall
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy

# Metrics that only have data when the owner opts in (manual logging, occasional
# devices). Silence here is expected, not a fault, so the watchdog skips them.
OPTIONAL_METRICS = {"dietary_energy"}

# Metrics whose headline values stay live through the Whoop cloud overlay even
# when the HealthKit stream is behind. Names match config/metric_policy.yaml.
# hrv_sdnn is deliberately absent: that is the Watch's number, not Whoop's.
# resting_hr is absent since 2026-10-08 (audit P7): Apple owns the all-day
# resting HR and Whoop's sleep RHR is its own metric, resting_hr_sleep.
WHOOP_CLOUD_METRICS = {"heart_rate", "hrv_rmssd",
                       "respiratory_rate", "recovery_score", "strain"}
# Daily metrics that only the Whoop API supplies: one value per night, so a
# missed day shows at 1.5 x cadence instead of the 2 x the sampled streams get
# (audit P11: 52 h passed before a missing night of recovery or HRV was stale).
WHOOP_DAILY_METRICS = {"hrv_rmssd", "recovery_score", "strain", "respiratory_rate",
                       "sleep_need", "sleep_duration"}
# Whoop scores the night within an hour or so of waking; after this reporting
# hour a missing recovery or sleep for today is a fault, before it the puller
# is still polling (fix program A3 polls every 15 min from 05:00 to 10:00).
WHOOP_SCORING_HOUR = 10
WHOOP_POLL_START_HOUR = 5
# The puller runs at least hourly (A3); a pull older than this is stuck.
WHOOP_PULL_STUCK_HOURS = 1.5

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
CLOUD_COVER_NOTE = (" Whoop cloud is current, so recovery and HRV (rMSSD) shown by Helios "
                    "remain live via the overlay; only the raw HealthKit stream is behind.")


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
        # `notify: true` promotes a feed to a real alert (used for the nightly
        # backup marker: a backup that stops is not informational).
        alert = bool(spec.get("notify"))
        out.append({"metric": "*", "device_key": key, "last_seen": str(last) if last else None,
                    "age_hours": round(age, 1) if age is not None else None,
                    "status": status, "tier": "primary" if alert else "informational",
                    "notify": alert, "fix": str(spec.get("fix") or SOURCE_FIX)})
    return out


def _as_utc_aware(v: datetime | str | None) -> datetime | None:
    if v is None:
        return None
    try:
        dt = v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _last_pull(conn, zone, last_ok_pull_at: datetime | str | None = None) -> tuple[datetime | None, str | None]:
    """(reporting-zone wall time, ISO with offset) of the newest Whoop pull:
    the newer of MAX(whoop_records.fetched_at) (naive UTC) and the daemon's
    last successful pull (aware; an unchanged record is skipped by
    apply_record, so fetched_at only moves when something changed), else the
    cache stamp of a pre-1a store (naive wall time), else None."""
    rows = db.fetchall(conn, "SELECT MAX(fetched_at) FROM whoop_records")
    stored = rows[0][0].replace(tzinfo=timezone.utc) if rows and rows[0][0] else None
    candidates = [t for t in (stored, _as_utc_aware(last_ok_pull_at)) if t is not None]
    if candidates:
        aware = max(candidates)
        return to_wall(aware, zone), aware.astimezone(zone).isoformat(timespec="seconds")
    rows = db.fetchall(conn, "SELECT MAX(fetched_at) FROM whoop_cache")
    if rows and rows[0][0]:
        wall = rows[0][0].replace(microsecond=0)
        return wall, wall.replace(tzinfo=zone).isoformat(timespec="seconds")
    return None, None


def _whoop_present_today(conn, today, zone) -> set[str]:
    """Which of {recovery, sleep} Whoop has SCORED for the reporting today:
    native records projected the way the cache files them (recovery by
    created_at, the night by its end; naps never count), plus a pre-1a cache
    row for the date."""
    from heliosd.ingest.whoop import projection_date
    present: set[str] = set()
    for kind, nap, state, s, e, c in db.fetchall(conn, """
            SELECT kind, nap, score_state, start_utc, end_utc, created_at FROM whoop_records
            WHERE kind IN ('recovery', 'sleep') AND COALESCE(end_utc, created_at, start_utc) >= ?""",
            [datetime.combine(today, datetime.min.time()) - timedelta(days=2)]):
        if state != "SCORED" or (kind == "sleep" and nap):
            continue
        if projection_date(kind, s, e, c, zone) == today:
            present.add(kind)
    for (kind,) in db.fetchall(conn, "SELECT kind FROM whoop_cache WHERE date = ? AND kind IN ('recovery', 'sleep')", [today]):
        present.add(kind)
    return present


def whoop_cloud_status(conn, now: datetime, enabled: bool, last_error: str | None, zone=None,
                       last_error_at: datetime | str | None = None,
                       last_ok_pull_at: datetime | str | None = None) -> dict | None:
    """One row describing the Whoop cloud puller whenever it needs attention,
    with the last pull time in every case. `now` is the reporting-zone wall
    clock. Audit P11: the old row appeared only when the newest cached
    recovery was more than two days old, so a stuck puller, a missing night
    and a token failure that happened before a restart were all invisible.

    - error: the last pull or token refresh failed (persisted across restarts).
    - silent: never pulled anything.
    - stale (alarm): it is past WHOOP_SCORING_HOUR and today's SCORED recovery
      or sleep is still missing, named in missing_today.
    - stale (informational): the last pull is older than WHOOP_PULL_STUCK_HOURS.
    - waiting (informational): inside the morning polling window and the
      night is not scored yet; nothing is wrong.
    - None: today's night is in and the puller ran recently."""
    if not enabled:
        return None
    zone = zone or timezone.utc
    today = now.date()
    pull_wall, pull_iso = _last_pull(conn, zone, last_ok_pull_at)
    present = _whoop_present_today(conn, today, zone)
    missing = sorted({"recovery", "sleep"} - present)
    newest = db.fetchall(conn, "SELECT MAX(date) FROM whoop_cache WHERE kind = 'recovery'")
    base: dict = {"metric": "*", "device_key": "whoop_cloud", "last_pull_at": pull_iso,
                  "last_seen": str(newest[0][0]) if newest and newest[0][0] else None,
                  "age_hours": round((now - pull_wall).total_seconds() / 3600, 1) if pull_wall else None,
                  "missing_today": missing}
    if last_error:
        when = f" at {last_error_at}" if last_error_at else ""
        return base | {"status": "error", "fix": WHOOP_CLOUD_FIX + f" Last error{when}: {last_error}"}
    if pull_wall is None:
        return base | {"status": "silent",
                       "fix": "The Whoop cloud puller has never stored a record. Authorize once at "
                              "/whoop/login, then POST /api/whoop/pull."}
    age_h = (now - pull_wall).total_seconds() / 3600
    if missing and now.hour >= WHOOP_SCORING_HOUR:
        return base | {"status": "stale",
                       "fix": (f"Whoop's {' and '.join(missing)} for {today} is not pulled yet although it is past "
                               f"{WHOOP_SCORING_HOUR:02d}:00 (last pull {age_h:.1f} h ago). If the Whoop app shows the "
                               f"night scored, POST /api/whoop/pull and read the puller's log lines; if the last "
                               f"error mentions the token or 401, re-authorize once at /whoop/login.")}
    if age_h > WHOOP_PULL_STUCK_HOURS:
        return base | {"status": "stale", "tier": "informational", "notify": False,
                       "fix": (f"The Whoop puller's last pull was {age_h:.1f} h ago; it runs at least hourly while "
                               f"heliosd is up, so either the daemon just restarted or the loop is stuck. Today's "
                               f"records are {'all in' if not missing else 'missing: ' + ', '.join(missing)}.")}
    if missing and WHOOP_POLL_START_HOUR <= now.hour < WHOOP_SCORING_HOUR:
        return base | {"status": "waiting", "tier": "informational", "notify": False,
                       "fix": (f"Waiting for Whoop to score last night ({' and '.join(missing)} not in yet); the "
                               f"puller is polling every 15 minutes until {WHOOP_SCORING_HOUR:02d}:00. Nothing to do.")}
    return None


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
    # The reporting-zone wall clock, never the Mac's own zone (audit P8); a
    # caller's naive `now` is read as that wall clock, as every sample row is.
    now = now or to_wall(datetime.now(timezone.utc), policy.zone)
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
        # Raw reader on purpose: the watchdog measures DELIVERY, so a stream
        # whose rows are excluded, unscored or flagged still counts as
        # delivering. Analysis reads the eligibility view instead.
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
        # One value per night from the Whoop API: a missed night is late at
        # 1.5 x cadence; sampled streams keep the 2 x allowance (audit P11).
        late_factor = 1.5 if (metric in WHOOP_DAILY_METRICS and priority and priority[0] == "whoop") else 2.0
        if freshest_age <= late_factor * cadence:
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
        w = whoop_cloud_status(conn, now, bool(whoop.get("enabled")), whoop.get("last_error"),
                               policy.zone, whoop.get("last_error_at"), whoop.get("last_ok_pull_at"))
        if w:
            report.append(w)
    report.extend(check_sources(policy, now))
    # Worst first: the bridge itself, then error and silent before stale, then
    # informational rows last, then by metric. The hourly loop notifies the
    # first row whose notify flag is not False; in policy-file order that was
    # an arbitrary stale metric while the bridge entry sat last (audit 2026-09-02).
    rank = {"error": 0, "silent": 0, "stale": 1, "corroboration_decayed": 5, "waiting": 6}
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
