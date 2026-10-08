"""Nightly sleep analysis: asleep vs in-bed, efficiency, stage architecture,
fell-asleep/woke window, and week-over-week comparisons. Deterministic math
only; the LLM never computes here.

Sources, in trust order:
- asleep hours per night: daily_values (already trust-arbitrated, Whoop first),
  each night filed on its wake date by the main-sleep episode builder
  (signals/episodes.py). `per_device` lists the night's value and every other
  device's value of the same night (its corroboration), so an Apple night of
  6 h 47 m is shown beside the Whoop value instead of a midnight-cut 5 h 20 m.
  `fallback` says the night's value is not the owner device's (for example
  Whoop's Apple Health copy on a night with no Whoop API record, fix program
  B2), the label the Today screen carries too (A6).
- stage architecture: the shared nightly helper (signals/sleep_stages), which
  picks ONE device per night: the night's sleep_duration owner (Whoop's API
  record from whoop_cache, else its stage rows), then the sleep_analysis
  priority list. Rows come from the eligibility view, never raw samples.
  in_bed is time in bed, never counted as sleep.

Every date and clock time is in the policy's reporting zone; the Mac's own
clock is never consulted (single-source plan v2, Phase 1a).
"""

from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta

from heliosd.ingest.normalize import reporting_today
from heliosd.signals.sleep_stages import nightly_stages
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy

ASLEEP_STAGES = ("asleep", "core", "deep", "rem")


def _hhmm(dt: datetime | None) -> str | None:
    """HH:MM of a naive reporting-zone wall time."""
    return dt.strftime("%H:%M") if dt is not None else None


def build_sleep_report(conn, days: int = 31, policy: MetricPolicy | None = None,
                       today: date | None = None) -> dict:
    policy = policy or MetricPolicy()
    today = today or reporting_today(policy.zone)
    start_d = today - timedelta(days=days)

    # 1. Canonical nightly asleep hours (trust-arbitrated, never blended),
    #    with every device's own value of the night beside it.
    order = policy.priority("sleep_duration")
    owner = order[0] if order else None

    def rank(k: str) -> tuple:
        return (order.index(k) if k in order else len(order), k)

    nights: dict = {}
    for r in db.fetchdicts(conn, """
        SELECT date, value, device_key, grade, corroboration FROM daily_values
        WHERE metric = 'sleep_duration' AND date >= ? ORDER BY date""", [start_d]):
        others = json.loads(r["corroboration"]) if r["corroboration"] else {}
        per_device = [{"device": r["device_key"], "asleep_h": r["value"]}] + [
            {"device": k, "asleep_h": others[k]} for k in sorted(others, key=rank)]
        nights[r["date"]] = {"date": str(r["date"]), "asleep_h": r["value"],
                             "device": r["device_key"], "grade": r["grade"], "per_device": per_device,
                             "fallback": owner is not None and r["device_key"] != owner}

    # 2. Stage architecture: one arbitrated device per night.
    for d, st in nightly_stages(conn, policy, start_d, today).items():
        n = nights.get(d)
        if n is None:
            continue
        n["stages"] = {"deep_min": st["deep_min"], "rem_min": st["rem_min"],
                       "light_min": st["light_min"], "awake_min": st["awake_min"]}
        n["stage_source"] = st["device"]
        if st["in_bed_h"] is not None:
            n["in_bed_h"] = st["in_bed_h"]
        if st["efficiency_pct"] is not None:
            n["efficiency_pct"] = st["efficiency_pct"]
        n["fell_asleep"] = _hhmm(st["fell_asleep"])
        n["woke"] = _hhmm(st["woke"])
        n["window"] = st.get("window")
        n["in_bed_start"] = _hhmm(st.get("in_bed_start"))
        n["in_bed_end"] = _hhmm(st.get("in_bed_end"))

    # 3. Comparisons, all from canonical values.
    ordered = [nights[k] for k in sorted(nights)]

    def window_vals(a: date, b: date) -> list[float]:
        return [x["asleep_h"] for k, x in nights.items()
                if a < k <= b and x.get("asleep_h") is not None]

    def avg(vals: list[float]):
        return round(sum(vals) / len(vals), 2) if vals else None

    last7 = window_vals(today - timedelta(days=7), today)
    prev7 = window_vals(today - timedelta(days=14), today - timedelta(days=7))
    all_vals = sorted(v for v in (x.get("asleep_h") for x in ordered) if v is not None)
    eff7 = [x["efficiency_pct"] for k, x in nights.items()
            if today - timedelta(days=7) < k <= today and x.get("efficiency_pct")]
    # "Same day last week" is the night one week before the LAST night with a
    # value, not one week before today: when last night is not in yet the
    # comparison still pairs like with like (audit S9).
    last_date = max(nights) if nights else None
    same_wd_date = last_date - timedelta(days=7) if last_date else None
    same_wd = (nights.get(same_wd_date) or {}).get("asleep_h") if same_wd_date else None

    # Every average carries the number of nights it rests on (audit S7): a
    # "7-night" figure built from five nights says so.
    return {"nights": ordered, "summary": {
        "last_night": ordered[-1] if ordered else None,
        "window_nights": 7,
        "avg_7d": avg(last7), "n_7d": len(last7),
        "avg_prev_7d": avg(prev7), "n_prev_7d": len(prev7),
        "same_weekday_last_week": same_wd,
        "same_weekday_last_week_date": str(same_wd_date) if same_wd is not None else None,
        "median": round(statistics.median(all_vals), 3) if all_vals else None,
        "median_n": len(all_vals),
        "efficiency_avg_7d": avg(eff7), "efficiency_n_7d": len(eff7),
    }}
