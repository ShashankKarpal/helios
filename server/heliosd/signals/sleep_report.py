"""Nightly sleep analysis: asleep vs in-bed, efficiency, stage architecture,
fell-asleep/woke window, and week-over-week comparisons. Deterministic math
only; the LLM never computes here.

Sources, in trust order:
- asleep hours per night: daily_values (already trust-arbitrated, Whoop first).
- stage architecture: the shared nightly helper (signals/sleep_stages), which
  picks ONE device per night: the night's sleep_duration owner (Whoop's API
  record from whoop_cache, else its stage rows), then the sleep_analysis
  priority list. Rows come from the eligibility view, never raw samples.
  in_bed is time in bed, never counted as sleep.

Every date and clock time is in the policy's reporting zone; the Mac's own
clock is never consulted (single-source plan v2, Phase 1a).
"""

from __future__ import annotations

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

    # 1. Canonical nightly asleep hours (trust-arbitrated, never blended).
    nights: dict = {}
    for r in db.fetchdicts(conn, """
        SELECT date, value, device_key, grade FROM daily_values
        WHERE metric = 'sleep_duration' AND date >= ? ORDER BY date""", [start_d]):
        nights[r["date"]] = {"date": str(r["date"]), "asleep_h": r["value"],
                             "device": r["device_key"], "grade": r["grade"]}

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
    same_wd = (nights.get(today - timedelta(days=7)) or {}).get("asleep_h")

    return {"nights": ordered, "summary": {
        "last_night": ordered[-1] if ordered else None,
        "avg_7d": avg(last7),
        "avg_prev_7d": avg(prev7),
        "same_weekday_last_week": same_wd,
        "median": all_vals[len(all_vals) // 2] if all_vals else None,
        "efficiency_avg_7d": avg(eff7),
    }}
