"""Nightly sleep analysis: asleep vs in-bed, efficiency, stage architecture,
fell-asleep/woke window, and week-over-week comparisons. Deterministic math
only; the LLM never computes here.

Sources, in trust order:
- asleep hours per night: daily_values (already trust-arbitrated, Whoop first).
- stage architecture: Whoop's stage_summary from whoop_cache (Whoop owns sleep),
  falling back to HealthKit stage samples (Apple Watch, then Zepp) for nights
  Whoop did not cover. in_bed is time in bed, never counted as sleep.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from heliosd.store import db

ASLEEP_STAGES = ("asleep", "core", "deep", "rem")


def _hhmm(dt) -> str | None:
    """HH:MM in the Mac's local timezone. Accepts datetimes (already local)
    or Whoop's UTC ISO strings."""
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = (datetime.fromisoformat(dt.replace("Z", "+00:00"))
              .astimezone().replace(tzinfo=None))
    return dt.strftime("%H:%M")


def build_sleep_report(conn, days: int = 31) -> dict:
    start_d = date.today() - timedelta(days=days)

    # 1. Canonical nightly asleep hours (trust-arbitrated, never blended).
    nights: dict = {}
    for r in db.fetchdicts(conn, """
        SELECT date, value, device_key, grade FROM daily_values
        WHERE metric = 'sleep_duration' AND date >= ? ORDER BY date""", [start_d]):
        nights[r["date"]] = {"date": str(r["date"]), "asleep_h": r["value"],
                             "device": r["device_key"], "grade": r["grade"]}

    # 2. Stage architecture from Whoop's cache (sleep owner).
    for r in db.fetchdicts(conn, """
        SELECT date, payload FROM whoop_cache WHERE kind = 'sleep' AND date >= ?""",
        [start_d]):
        n = nights.get(r["date"])
        if n is None:
            continue
        p = json.loads(r["payload"])
        sc = p.get("score") or {}
        st = sc.get("stage_summary") or {}
        if not st:
            continue
        n["stages"] = {
            "deep_min": round(st.get("total_slow_wave_sleep_time_milli", 0) / 60000),
            "rem_min": round(st.get("total_rem_sleep_time_milli", 0) / 60000),
            "light_min": round(st.get("total_light_sleep_time_milli", 0) / 60000),
            "awake_min": round(st.get("total_awake_time_milli", 0) / 60000),
        }
        n["stage_source"] = "whoop"
        in_bed_ms = st.get("total_in_bed_time_milli") or 0
        if in_bed_ms:
            n["in_bed_h"] = round(in_bed_ms / 3.6e6, 2)
        eff = sc.get("sleep_efficiency_percentage")
        if eff is None and in_bed_ms and n.get("asleep_h"):
            eff = n["asleep_h"] / (in_bed_ms / 3.6e6) * 100
        if eff is not None:
            n["efficiency_pct"] = round(float(eff), 1)
        n["fell_asleep"] = _hhmm(p.get("start"))
        n["woke"] = _hhmm(p.get("end"))

    # 3. HealthKit stage fallback for nights Whoop missed: Apple, then Zepp.
    rows = db.fetchdicts(conn, """
        SELECT CAST(end_ts AS DATE) AS d, device_key, text_value AS stage,
               SUM(value) AS minutes, MIN(start_ts) AS s, MAX(end_ts) AS e
        FROM samples WHERE metric = 'sleep_analysis' AND CAST(end_ts AS DATE) >= ?
        GROUP BY 1, 2, 3""", [start_d])
    per: dict = {}
    for r in rows:
        per.setdefault(r["d"], {}).setdefault(r["device_key"], {})[r["stage"]] = r
    for d, devs in per.items():
        n = nights.get(d)
        if n is None or n.get("stages"):
            continue
        for dev in ("apple_watch_ultra", "zepp_helio", "whoop"):
            st = devs.get(dev)
            if not st:
                continue
            staged = any(k in st for k in ("core", "deep", "rem"))
            if not staged and "asleep" not in st:
                continue  # only in_bed/awake: not a sleep record

            def m(key: str) -> int:
                return round(float(st[key]["minutes"])) if key in st else 0

            n["stages"] = {"deep_min": m("deep"), "rem_min": m("rem"),
                           "light_min": m("core") + (0 if staged else m("asleep")),
                           "awake_min": m("awake")}
            n["stage_source"] = dev
            if "in_bed" in st:
                in_bed_h = float(st["in_bed"]["minutes"]) / 60.0
                n["in_bed_h"] = round(in_bed_h, 2)
                if n.get("asleep_h") and in_bed_h > 0:
                    n["efficiency_pct"] = round(n["asleep_h"] / in_bed_h * 100, 1)
            sleep_rows = [st[k] for k in ASLEEP_STAGES if k in st]
            if sleep_rows:
                n["fell_asleep"] = _hhmm(min(x["s"] for x in sleep_rows))
                n["woke"] = _hhmm(max(x["e"] for x in sleep_rows))
            break

    # 4. Comparisons, all from canonical values.
    ordered = [nights[k] for k in sorted(nights)]
    today = date.today()

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
