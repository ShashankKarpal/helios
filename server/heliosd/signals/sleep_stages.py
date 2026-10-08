"""Shared nightly sleep-stage helper (single-source plan v2, Phase 1a item 7).

Eligibility is not arbitration (checkpoint A, point 28). The eligibility view
says which rows MAY be read; it does not say which device owns a night. Before
this module three readers (sleep_report, weekly_review, doctor_report) each
summed stage minutes across every device that wrote the night, so a night
recorded by Whoop and the Watch counted twice, and sleep_report carried its own
hard-coded device order. Now one helper picks ONE device per night:

1. the device that owns the night's canonical sleep_duration daily value, when
   it has stage data for that night (for Whoop: the API record in whoop_cache
   first, then its HealthKit stage copy, labelled whoop:healthkit);
2. else the first device in the sleep_analysis priority list with stage rows.
In bed and efficiency are the owner device's own (fix program B2, audit S8):
from the Whoop API record, from Whoop's own in_bed rows for its HealthKit
episode, from Apple's in_bed rows when it wrote them; a stage record of another
device carries none, because its time in bed beside the owner's asleep time
would mix two devices.

Stage rows come from eligible_samples only (registered, usable, not excluded,
time-valid), through the main-sleep episode builder (signals/episodes.py, fix
program B1): a night is the device's main episode filed on its wake date, so a
night that began before midnight is one night, a nap is not part of it and an
overlapping near-duplicate row counts once. Before Wave 2 a night was the
reporting date of each segment's end, which cut every night at midnight.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from heliosd.signals import episodes
from heliosd.signals.episodes import Episode
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.schema import base_device


def _wall(iso: str | None, zone) -> datetime | None:
    """Whoop's UTC ISO string rendered as a naive reporting-zone wall time."""
    if not iso:
        return None
    dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(zone).replace(tzinfo=None, microsecond=0)


def _from_whoop_payload(payload: str, asleep_h: float | None, zone) -> dict | None:
    p = json.loads(payload)
    # Eligibility applies to cached records too: a nap or a record that is not
    # SCORED yields no stages (checkpoint B, point 6).
    if p.get("nap") or (p.get("score_state") or "SCORED") != "SCORED":
        return None
    sc = p.get("score") or {}
    st = sc.get("stage_summary") or {}
    if not st:
        return None
    # The API record carries the in-bed window only (no per-stage times), so
    # fell_asleep and woke ARE the in-bed edges here; window says so (audit S10).
    bed_s, bed_e = _wall(p.get("start"), zone), _wall(p.get("end"), zone)
    out = {"device": "whoop", "source": "whoop_api", "staged": True,
           "deep_min": round(st.get("total_slow_wave_sleep_time_milli", 0) / 60000),
           "rem_min": round(st.get("total_rem_sleep_time_milli", 0) / 60000),
           "light_min": round(st.get("total_light_sleep_time_milli", 0) / 60000),
           "awake_min": round(st.get("total_awake_time_milli", 0) / 60000),
           "in_bed_h": None, "efficiency_pct": None,
           "fell_asleep": bed_s, "woke": bed_e,
           "window": "in_bed", "in_bed_start": bed_s, "in_bed_end": bed_e}
    in_bed_ms = st.get("total_in_bed_time_milli") or 0
    if in_bed_ms:
        out["in_bed_h"] = round(in_bed_ms / 3.6e6, 2)
    eff = sc.get("sleep_efficiency_percentage")
    if eff is None and in_bed_ms and asleep_h:
        eff = asleep_h / (in_bed_ms / 3.6e6) * 100
    if eff is not None:
        out["efficiency_pct"] = round(float(eff), 1)
    return out


def _from_episode(ep: Episode) -> dict:
    """A stage record from one device's main episode, under the episode's key
    (whoop:healthkit for Whoop's HealthKit copy): its stage minutes (each
    stretch counted once, the most recently ingested row deciding the stage),
    its asleep window (first and last asleep instant), and its in-bed window
    and efficiency (its own asleep time over its own time in bed) when the
    device wrote in_bed rows (audit S10)."""
    staged = bool(ep.deep_min or ep.rem_min or ep.core_min)
    out = {"device": ep.device_key, "source": "healthkit", "staged": staged,
           "deep_min": round(ep.deep_min), "rem_min": round(ep.rem_min),
           # Unstaged 'asleep' time is the light figure of a device that writes no stages.
           "light_min": round(ep.core_min + (0 if staged else ep.asleep_plain_min)),
           "awake_min": round(ep.awake_min), "in_bed_h": None, "efficiency_pct": None,
           "fell_asleep": ep.start, "woke": ep.end,
           "window": "asleep", "in_bed_start": ep.in_bed_start, "in_bed_end": ep.in_bed_end}
    if ep.in_bed_h:
        out["in_bed_h"] = round(ep.in_bed_h, 2)
        out["efficiency_pct"] = round(ep.asleep_h / ep.in_bed_h * 100, 1)
    return out


def night_owners(conn, start: date, end: date) -> dict[date, tuple[str, float | None]]:
    """{night: (device_key, asleep_h)} from the canonical sleep_duration values."""
    return {r["date"]: (r["device_key"], r["value"]) for r in db.fetchdicts(conn, """
        SELECT date, device_key, value FROM daily_values
        WHERE metric = 'sleep_duration' AND date BETWEEN ? AND ?""", [start, end])}


def nightly_stages(conn, policy: MetricPolicy, start: date, end: date) -> dict[date, dict]:
    """One arbitrated stage record per night in [start, end] that has stage data.
    Keys: device, source (whoop_api | healthkit), staged, deep_min, rem_min,
    light_min, awake_min, in_bed_h, efficiency_pct, fell_asleep, woke (naive
    reporting-zone wall times or None), window ("asleep" when fell_asleep and
    woke are the first and last asleep stage, "in_bed" when they are the
    record's in-bed edges, as for the Whoop API record), in_bed_start and
    in_bed_end (the in-bed window when known, else None)."""
    owners = night_owners(conn, start, end)
    cache = {r["date"]: r["payload"] for r in db.fetchdicts(conn,
        "SELECT date, payload FROM whoop_cache WHERE kind = 'sleep' AND date BETWEEN ? AND ?", [start, end])}
    priority = policy.priority("sleep_analysis")
    # Whoop as a candidate is its API record first, then its HealthKit stage
    # copy (the key whoop:healthkit of the episode builder).
    keys = {episodes.episode_key(k) for k in priority} | {episodes.episode_key(o) for o, _ in owners.values()}
    eps = episodes.main_sleep_episodes(conn, policy, start, end, devices=sorted(keys))
    out: dict[date, dict] = {}
    for night in sorted(set(cache) | set(owners) | {d for _, d in eps}):
        owner, asleep_h = owners.get(night, (None, None))
        candidates = ([owner] if owner else []) + [d for d in priority if d != owner]
        for dev in candidates:
            rec = None
            if dev == "whoop" and night in cache:
                rec = _from_whoop_payload(cache[night], asleep_h, policy.zone)
            if rec is None and (episodes.episode_key(dev), night) in eps:
                rec = _from_episode(eps[(episodes.episode_key(dev), night)])
            if rec is not None:
                if owner is not None and base_device(rec["device"]) != base_device(owner):
                    rec.update(in_bed_h=None, efficiency_pct=None, in_bed_start=None, in_bed_end=None)
                out[night] = rec
                break
    return out
