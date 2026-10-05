"""Shared nightly sleep-stage helper (single-source plan v2, Phase 1a item 7).

Eligibility is not arbitration (checkpoint A, point 28). The eligibility view
says which rows MAY be read; it does not say which device owns a night. Before
this module three readers (sleep_report, weekly_review, doctor_report) each
summed stage minutes across every device that wrote the night, so a night
recorded by Whoop and the Watch counted twice, and sleep_report carried its own
hard-coded device order. Now one helper picks ONE device per night:

1. the device that owns the night's canonical sleep_duration daily value, when
   it has stage data for that night (for Whoop: the API record in whoop_cache
   first, then its HealthKit stage copy);
2. else the first device in the sleep_analysis priority list with stage rows.

Stage rows come from eligible_samples only (registered, usable, not excluded,
time-valid). A night is the reporting date of the segment end, exactly as every
reader bucketed before; the main-sleep episode builder of Phase 4 replaces that.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from heliosd.store import db
from heliosd.trust.policy import MetricPolicy

ASLEEP_STAGES = ("asleep", "core", "deep", "rem")
STAGED = ("core", "deep", "rem")


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
    out = {"device": "whoop", "source": "whoop_api", "staged": True,
           "deep_min": round(st.get("total_slow_wave_sleep_time_milli", 0) / 60000),
           "rem_min": round(st.get("total_rem_sleep_time_milli", 0) / 60000),
           "light_min": round(st.get("total_light_sleep_time_milli", 0) / 60000),
           "awake_min": round(st.get("total_awake_time_milli", 0) / 60000),
           "in_bed_h": None, "efficiency_pct": None,
           "fell_asleep": _wall(p.get("start"), zone), "woke": _wall(p.get("end"), zone)}
    in_bed_ms = st.get("total_in_bed_time_milli") or 0
    if in_bed_ms:
        out["in_bed_h"] = round(in_bed_ms / 3.6e6, 2)
    eff = sc.get("sleep_efficiency_percentage")
    if eff is None and in_bed_ms and asleep_h:
        eff = asleep_h / (in_bed_ms / 3.6e6) * 100
    if eff is not None:
        out["efficiency_pct"] = round(float(eff), 1)
    return out


def _from_stage_rows(device: str, st: dict, asleep_h: float | None) -> dict | None:
    """st: {stage: {"minutes", "s", "e"}} for one device and night. None when
    the device wrote only in_bed or awake rows (not a sleep record)."""
    staged = any(k in st for k in STAGED)
    if not staged and "asleep" not in st:
        return None

    def m(key: str) -> int:
        return round(float(st[key]["minutes"])) if key in st else 0

    out = {"device": device, "source": "healthkit", "staged": staged,
           "deep_min": m("deep"), "rem_min": m("rem"),
           "light_min": m("core") + (0 if staged else m("asleep")),
           "awake_min": m("awake"), "in_bed_h": None, "efficiency_pct": None,
           "fell_asleep": None, "woke": None}
    if "in_bed" in st:
        in_bed_h = float(st["in_bed"]["minutes"]) / 60.0
        out["in_bed_h"] = round(in_bed_h, 2)
        if asleep_h and in_bed_h > 0:
            out["efficiency_pct"] = round(asleep_h / in_bed_h * 100, 1)
    sleep_rows = [st[k] for k in ASLEEP_STAGES if k in st]
    if sleep_rows:
        out["fell_asleep"] = min(x["s"] for x in sleep_rows)
        out["woke"] = max(x["e"] for x in sleep_rows)
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
    reporting-zone wall times or None)."""
    owners = night_owners(conn, start, end)
    cache = {r["date"]: r["payload"] for r in db.fetchdicts(conn,
        "SELECT date, payload FROM whoop_cache WHERE kind = 'sleep' AND date BETWEEN ? AND ?", [start, end])}
    per: dict[date, dict[str, dict]] = {}
    for r in db.fetchdicts(conn, """
        SELECT CAST(end_ts AS DATE) AS d, device_key, text_value AS stage,
               SUM(value) AS minutes, MIN(start_ts) AS s, MAX(end_ts) AS e
        FROM eligible_samples
        WHERE metric = 'sleep_analysis' AND CAST(end_ts AS DATE) BETWEEN ? AND ?
        GROUP BY 1, 2, 3""", [start, end]):
        per.setdefault(r["d"], {}).setdefault(r["device_key"], {})[r["stage"]] = r
    priority = policy.priority("sleep_analysis")
    out: dict[date, dict] = {}
    for night in sorted(set(per) | set(cache) | set(owners)):
        owner, asleep_h = owners.get(night, (None, None))
        candidates = ([owner] if owner else []) + [d for d in priority if d != owner]
        for dev in candidates:
            rec = None
            if dev == "whoop" and night in cache:
                rec = _from_whoop_payload(cache[night], asleep_h, policy.zone)
            if rec is None and dev in per.get(night, {}):
                rec = _from_stage_rows(dev, per[night][dev], asleep_h)
            if rec is not None:
                out[night] = rec
                break
    return out
