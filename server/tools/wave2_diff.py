#!/usr/bin/env python
"""Wave 2 diff tool (fix program design, section 4): what the rebuild changed,
old copy against new copy, for the owner's plain-English diff table.

    cd server && HELIOS_HOME=<stage folder> <venv>/bin/python tools/wave2_diff.py <old.duckdb> <new.duckdb> \
        --today YYYY-MM-DD --out DIR

Both copies are attached READ_ONLY in one scratch DuckDB process; nothing is
written to either. Writes <out>/diff.json and <out>/diff.md:
- per metric: days changed, added, removed, the median and largest change,
  grades changed; the owner device before and after, with date ranges;
- the latest owner baselines old and new (the default window), and the new
  per-device baselines of sleep_duration for apple_watch_ultra;
- signal states and the days flagged travel_or_shifted_schedule over the last
  30 days;
- the last 7 complete nights before --today: Helios old and new (value,
  device, grade, every device's value), Whoop's own numbers from its stored
  records (asleep = light + SWS + REM, in bed, efficiency, respiratory rate,
  the four sleep_needed parts, recovery score, resting HR, rMSSD, the cycle's
  strain) and the independent SQL episode oracle per device (design section
  4: the union of the asleep stages of the longest episode ending that day,
  the way Apple Health counts);
- the episode oracle against the new per-device sleep values for every night;
- the counts the owner needs for Q1, Q3 and Q4 (design section 5).
The policy (priority lists, day bases, sync paths) comes from HELIOS_HOME,
like the rebuild. The Q counts are estimates by the policy's day basis, from
the new copy's eligible rows. Aggregates and the last 7 nights only.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from heliosd.config import helios_home, load_settings  # noqa: E402
from heliosd.ingest.normalize import reporting_today  # noqa: E402
from heliosd.trust.policy import MetricPolicy  # noqa: E402

NIGHTS = 7
SIGNAL_DAYS = 30
ORACLE_MIN_H = 3.0            # the main-episode minimum (Q3 default)
ORACLE_ALT_MIN_H = 2.0        # the alternative the owner is asked about (Q3)
ORACLE_TOL_H = 1 / 60 + 0.005  # one minute, plus the stored values' rounding to 0.01 h
LEGACY = "apple_watch_6_legacy"

ORACLE_SQL = """
WITH r AS (SELECT device_key AS dk, start_ts AS s, end_ts AS e, text_value AS tv FROM n.eligible_samples
           WHERE metric = 'sleep_analysis' AND text_value IN ('asleep', 'core', 'deep', 'rem', 'awake')),
o1 AS (SELECT *, MAX(e) OVER (PARTITION BY dk ORDER BY s, e ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS pm FROM r),
g1 AS (SELECT *, SUM(CASE WHEN pm IS NULL OR s > pm + INTERVAL 60 MINUTE THEN 1 ELSE 0 END)
                 OVER (PARTITION BY dk ORDER BY s, e ROWS UNBOUNDED PRECEDING) AS ep FROM o1),
a1 AS (SELECT * FROM g1 WHERE tv <> 'awake'),
o2 AS (SELECT *, MAX(e) OVER (PARTITION BY dk, ep ORDER BY s, e ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS pm2 FROM a1),
g2 AS (SELECT *, SUM(CASE WHEN pm2 IS NULL OR s > pm2 THEN 1 ELSE 0 END)
                 OVER (PARTITION BY dk, ep ORDER BY s, e ROWS UNBOUNDED PRECEDING) AS isl FROM o2),
u AS (SELECT dk, ep, isl, MIN(s) AS us, MAX(e) AS ue FROM g2 GROUP BY 1, 2, 3),
epi AS (SELECT dk, ep, MAX(ue) AS ee, SUM(epoch(ue) - epoch(us)) / 3600.0 AS union_h FROM u GROUP BY 1, 2)
SELECT CAST(ee AS DATE) AS wake_d, dk, MAX(union_h) AS best_h FROM epi GROUP BY 1, 2"""


def _r(x, n=3):
    return None if x is None else round(float(x), n)


def _has(con, catalog: str, table: str, column: str | None = None) -> bool:
    if column is None:
        return con.execute("SELECT COUNT(*) FROM duckdb_tables() WHERE database_name = ? AND table_name = ?",
                           [catalog, table]).fetchone()[0] > 0
    return con.execute("SELECT COUNT(*) FROM duckdb_columns() WHERE database_name = ? AND table_name = ? AND column_name = ?",
                       [catalog, table, column]).fetchone()[0] > 0


def _dicts(con, sql: str, params=None) -> list[dict]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [{k: (str(v) if isinstance(v, (date, datetime)) else v) for k, v in zip(cols, row)} for row in cur.fetchall()]


def day_sql(policy: MetricPolicy, metric: str) -> str:
    """The day a row files under, by the metric's day basis (estimate: points of
    a sleep_end metric take their own date; the episode builder may move a few)."""
    basis = policy.day_basis(metric)
    if basis == "interval_midpoint":
        return "CAST(start_ts + to_microseconds(CAST((epoch_us(end_ts) - epoch_us(start_ts)) / 2 AS BIGINT)) AS DATE)"
    if basis in ("sleep_end", "whoop_cycle"):
        return "CAST(COALESCE(end_ts, start_ts) AS DATE)"
    return "CAST(start_ts AS DATE)"


_JOIN = """WITH j AS (SELECT COALESCE(a.date, b.date) AS d, COALESCE(a.metric, b.metric) AS metric, a.value AS ov, b.value AS nv,
                        a.device_key AS od, b.device_key AS nd, a.grade AS og, b.grade AS ng
                 FROM o.daily_values a FULL OUTER JOIN n.daily_values b ON a.date = b.date AND a.metric = b.metric)"""


def per_metric(con) -> list[dict]:
    return _dicts(con, f"""{_JOIN}
        SELECT metric, COUNT(*) FILTER (WHERE ov IS DISTINCT FROM nv) AS days_changed,
               COUNT(*) FILTER (WHERE ov IS NULL AND nv IS NOT NULL) AS days_added,
               COUNT(*) FILTER (WHERE ov IS NOT NULL AND nv IS NULL) AS days_removed,
               ROUND(MEDIAN(abs(nv - ov)) FILTER (WHERE ov <> nv), 3) AS median_change,
               ROUND(MAX(abs(nv - ov)), 3) AS max_change,
               COUNT(*) FILTER (WHERE og IS DISTINCT FROM ng) AS grades_changed
        FROM j GROUP BY 1 ORDER BY 1""")


def owner_device_changes(con) -> list[dict]:
    return _dicts(con, f"""{_JOIN}
        SELECT metric, od AS old_device, nd AS new_device, COUNT(*) AS days, MIN(d) AS first, MAX(d) AS last
        FROM j WHERE od IS DISTINCT FROM nd GROUP BY 1, 2, 3 ORDER BY 1, 4 DESC, 2, 3""")


def baselines(con, policy: MetricPolicy, today: date) -> dict:
    w = policy.default_window
    rows = _dicts(con, """
        WITH ob AS (SELECT * FROM o.baselines WHERE window_days = ? AND date <= ? QUALIFY ROW_NUMBER() OVER (PARTITION BY metric ORDER BY date DESC) = 1),
             nb AS (SELECT * FROM n.baselines WHERE window_days = ? AND date <= ? QUALIFY ROW_NUMBER() OVER (PARTITION BY metric ORDER BY date DESC) = 1)
        SELECT COALESCE(ob.metric, nb.metric) AS metric, ob.date AS old_date, ROUND(ob.median, 3) AS old_median, ob.n_days AS old_n_days,
               nb.date AS new_date, ROUND(nb.median, 3) AS new_median, nb.n_days AS new_n_days
        FROM ob FULL OUTER JOIN nb ON nb.metric = ob.metric ORDER BY 1""", [w, today, w, today])
    dev = []
    if _has(con, "n", "device_baselines"):
        dev = _dicts(con, """SELECT date, window_days, ROUND(median, 3) AS median, ROUND(mad, 3) AS mad, n_days FROM n.device_baselines
            WHERE metric = 'sleep_duration' AND device_key = 'apple_watch_ultra' AND date = (SELECT MAX(date) FROM n.device_baselines
              WHERE metric = 'sleep_duration' AND device_key = 'apple_watch_ultra' AND date <= ?) ORDER BY window_days""", [today])
    return {"window_days": w, "owner": rows, "device_sleep_duration_apple_watch_ultra": dev}


def signals_window(con, today: date) -> dict:
    lo = today - timedelta(days=SIGNAL_DAYS - 1)
    out: dict = {"from": str(lo), "to": str(today), "states": {}, "travel_days": {}}
    for side in ("o", "n"):
        for state, cnt in con.execute(f"SELECT state, COUNT(*) FROM {side}.signals WHERE date BETWEEN ? AND ? GROUP BY 1",
                                      [lo, today]).fetchall():
            out["states"].setdefault(state, {"old": 0, "new": 0})["old" if side == "o" else "new"] = cnt
        days = [str(r[0]) for r in con.execute(
            f"SELECT DISTINCT date FROM {side}.signals WHERE date BETWEEN ? AND ? AND context_flags LIKE '%travel_or_shifted_schedule%' "
            "ORDER BY 1", [lo, today]).fetchall()]
        out["travel_days"]["old" if side == "o" else "new"] = days
    out["states"] = dict(sorted(out["states"].items()))
    return out


def _wake_date(utc: datetime | None, zone) -> date | None:
    return utc.replace(tzinfo=timezone.utc).astimezone(zone).date() if utc is not None else None


def whoop_nights(con, zone, dates: list[date]) -> dict[date, dict]:
    """Whoop's own numbers per wake date, from the new copy's stored records."""
    recs = defaultdict(list)
    for kind, native, sleep_id, cycle_id, end_utc, created, nap, payload in con.execute(
            "SELECT kind, native_id, sleep_id, cycle_id, end_utc, created_at, nap, payload FROM n.whoop_records").fetchall():
        recs[kind].append((native, sleep_id, cycle_id, end_utc, created, nap, json.loads(payload or "{}")))
    out: dict[date, dict] = {}
    for d in dates:
        sleeps = [r for r in recs["sleep"] if not r[5] and _wake_date(r[3], zone) == d]
        if not sleeps:
            continue

        def ms(sc, *path):
            v = sc
            for p in path:
                v = (v or {}).get(p)
            return v
        hrs = lambda v: None if v is None else round(v / 3.6e6, 3)   # noqa: E731
        native, *_rest, p = max(sleeps, key=lambda r: sum((ms(r[6], "score", "stage_summary", k) or 0) for k in (
            "total_light_sleep_time_milli", "total_slow_wave_sleep_time_milli", "total_rem_sleep_time_milli")))
        st = ms(p, "score", "stage_summary") or {}
        need = ms(p, "score", "sleep_needed") or {}
        night = {"asleep_h": hrs(sum(st.get(k) or 0 for k in ("total_light_sleep_time_milli", "total_slow_wave_sleep_time_milli",
                                                               "total_rem_sleep_time_milli"))),
                 "in_bed_h": hrs(st.get("total_in_bed_time_milli")),
                 "efficiency_pct": _r(ms(p, "score", "sleep_efficiency_percentage"), 1),
                 "respiratory_rate": _r(ms(p, "score", "respiratory_rate"), 2),
                 "sleep_needed_h": {k: hrs(need.get(f"{k}_milli")) for k in ("baseline", "need_from_sleep_debt",
                                                                              "need_from_recent_strain", "need_from_recent_nap")}}
        rec = next((r for r in recs["recovery"] if r[1] == native), None) or next(
            (r for r in recs["recovery"] if _wake_date(r[4], zone) == d), None)
        if rec:
            night.update({"recovery_score": ms(rec[6], "score", "recovery_score"),
                          "resting_hr": ms(rec[6], "score", "resting_heart_rate"),
                          "rmssd_ms": _r(ms(rec[6], "score", "hrv_rmssd_milli"), 1)})
            cyc = next((r for r in recs["cycle"] if r[0] == rec[2]), None)
            if cyc:
                night["strain"] = _r(ms(cyc[6], "score", "strain"), 2)
                night["strain_in_progress"] = cyc[3] is None
        out[d] = night
    return out


def _per_device(row) -> dict:
    if not row:
        return {}
    value, device, _grade, corr = row[:4]
    out = {k: _r(v) for k, v in json.loads(corr or "{}").items()}
    out[device] = _r(value)
    return dict(sorted(out.items()))


def oracle(con) -> dict[tuple[str, date], float]:
    """(device, wake date) -> hours asleep in the longest episode (any length)."""
    return {(dk, d): float(h) for d, dk, h in con.execute(ORACLE_SQL).fetchall()}


def _label(device: str) -> str:
    """Whoop's sleep stages in the store are its Apple Health copy, never the Whoop API night."""
    return "whoop:healthkit" if device == "whoop" else device


def _stored_key(per_device: dict, device: str) -> str | None:
    """The key a device's stage-built night is stored under: Whoop's stages are its HealthKit copy."""
    if device == "whoop":
        return "whoop:healthkit" if "whoop:healthkit" in per_device else None
    return device if device in per_device else None


def nights(con, policy: MetricPolicy, today: date, orc: dict) -> list[dict]:
    dates = [today - timedelta(days=i) for i in range(NIGHTS, 0, -1)]
    detail = _has(con, "n", "daily_values", "detail")
    wh = whoop_nights(con, policy.zone, dates)
    out = []
    for d in dates:
        old = con.execute("SELECT value, device_key, grade, corroboration FROM o.daily_values WHERE metric = 'sleep_duration' "
                          "AND date = ?", [d]).fetchone()
        new = con.execute("SELECT value, device_key, grade, corroboration" + (", detail" if detail else "") +
                          " FROM n.daily_values WHERE metric = 'sleep_duration' AND date = ?", [d]).fetchone()
        out.append({"wake_date": str(d),
                    "old": {"value": _r(old[0]), "device": old[1], "grade": old[2]} if old else None,
                    "new": {"value": _r(new[0]), "device": new[1], "grade": new[2],
                            **({"detail": json.loads(new[4]) if new[4] else None} if detail else {})} if new else None,
                    "per_device_old": _per_device(old), "per_device_new": _per_device(new),
                    "oracle_h": {_label(dk): _r(h) for (dk, wd), h in sorted(orc.items()) if wd == d and h >= ORACLE_MIN_H},
                    "whoop": wh.get(d)})
    return out


def oracle_check(con, orc: dict) -> dict:
    """Every night the oracle sees (longest episode at least 3 h) against the
    device's stored nightly value in the new copy."""
    stored = {d: _per_device(r[1:]) for r in con.execute(
        "SELECT date, value, device_key, grade, corroboration FROM n.daily_values WHERE metric = 'sleep_duration'").fetchall()
        for d in [r[0]]}
    res = {"compared": 0, "within_1_min": 0, "off": 0, "not_stored": 0, "off_nights": []}
    for (dk, d), h in sorted(orc.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        if h < ORACLE_MIN_H:
            continue
        key = _stored_key(stored.get(d, {}), dk)
        if key is None:
            res["not_stored"] += 1
            continue
        res["compared"] += 1
        if abs(stored[d][key] - h) <= ORACLE_TOL_H:
            res["within_1_min"] += 1
        else:
            res["off"] += 1
            if len(res["off_nights"]) < 30:
                res["off_nights"].append({"wake_date": str(d), "device": key, "oracle_h": _r(h), "stored_h": stored[d][key]})
    return res


def _owner_of(con, metric: str) -> dict[date, str]:
    return dict(con.execute("SELECT date, device_key FROM n.daily_values WHERE metric = ?", [metric]).fetchall())


def q1(con, policy: MetricPolicy, today: date) -> dict:
    """Days that would change if Whoop's HealthKit copy were the labelled
    fallback right after the Whoop API value for respiratory rate and resting HR."""
    out = {}
    for metric in ("respiratory_rate", "resting_hr"):
        paths = policy.sync_paths(metric).get("whoop")
        if not paths:
            out[metric] = {"applies": False, "reason": "the policy counts every Whoop row of this metric already"}
            continue
        days = [r[0] for r in con.execute(
            f"SELECT DISTINCT {day_sql(policy, metric)} AS d FROM n.eligible_samples WHERE metric = ? AND device_key = 'whoop' "
            "AND sync_path NOT IN (SELECT unnest(?)) AND d <= ? ORDER BY 1", [metric, paths, today]).fetchall()]
        owner = _owner_of(con, metric)
        change = [d for d in days if owner.get(d) not in ("whoop", "whoop:healthkit")]
        by = defaultdict(int)
        for d in change:
            by[owner.get(d) or "blank"] += 1
        out[metric] = {"applies": True, "days_with_a_whoop_healthkit_value": len(days), "days_that_would_change": len(change),
                       "now_from": dict(sorted(by.items())), "first": str(min(change)) if change else None,
                       "last": str(max(change)) if change else None}
    return out


def q3(con, policy: MetricPolicy, orc: dict) -> dict:
    """Nights that would gain a value if a main episode needed 2 h asleep instead of 3 h."""
    prio = policy.priority("sleep_duration")
    owner = _owner_of(con, "sleep_duration")
    per: dict[str, dict] = {}
    for (dk, d), h in sorted(orc.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        if not ORACLE_ALT_MIN_H <= h < ORACLE_MIN_H:
            continue
        key = "whoop:healthkit" if dk == "whoop" and "whoop:healthkit" in prio else dk
        c = per.setdefault(_label(dk), {"nights_gaining_a_value": 0, "blank_days_filled": 0, "owner_days_changed": 0,
                                "corroboration_only": 0, "first": str(d), "last": str(d)})
        c["nights_gaining_a_value"] += 1
        c["last"] = str(d)
        cur = owner.get(d)
        if cur is None:
            c["blank_days_filled"] += 1
        elif key in prio and (cur not in prio or prio.index(key) < prio.index(cur)):
            c["owner_days_changed"] += 1
        else:
            c["corroboration_only"] += 1
    return {"min_hours_now": ORACLE_MIN_H, "min_hours_asked": ORACLE_ALT_MIN_H, "by_device": dict(sorted(per.items()))}


def q4(con, policy: MetricPolicy, today: date) -> dict:
    """Days that would change if apple_watch_6_legacy were added last to resting HR and all-day HR."""
    out = {}
    for metric in ("resting_hr", "heart_rate"):
        if LEGACY in policy.priority(metric):
            out[metric] = {"applies": False, "reason": f"{LEGACY} is already in the list"}
            continue
        days = [r[0] for r in con.execute(
            f"SELECT DISTINCT {day_sql(policy, metric)} AS d FROM n.eligible_samples WHERE metric = ? AND device_key = ? "
            "AND d <= ? ORDER BY 1", [metric, LEGACY, today]).fetchall()]
        owner = _owner_of(con, metric)
        blank = [d for d in days if d not in owner]
        out[metric] = {"applies": True, "days_with_legacy_rows": len(days), "blank_days_filled": len(blank),
                       "corroboration_only": len(days) - len(blank), "first": str(min(blank)) if blank else None,
                       "last": str(max(blank)) if blank else None}
    return out


def run(old, new, today: date, out, policy: MetricPolicy | None = None) -> dict:
    policy = policy or MetricPolicy(default_tz=load_settings().timezone)
    con = duckdb.connect()
    try:
        for alias, path in (("o", old), ("n", new)):
            con.execute(f"ATTACH '{str(Path(path)).replace(chr(39), chr(39) * 2)}' AS {alias} (READ_ONLY)")
        orc = oracle(con)
        R = {"old": str(old), "new": str(new), "today": str(today), "policy_home": str(helios_home()),
             "generated": datetime.now().isoformat(timespec="seconds"),
             "per_metric": per_metric(con), "owner_device_changes": owner_device_changes(con),
             "baselines": baselines(con, policy, today), "signals_last_30_days": signals_window(con, today),
             "nights": nights(con, policy, today, orc), "episode_oracle_check": oracle_check(con, orc),
             "owner_questions": {"q1_whoop_healthkit_fallback": q1(con, policy, today), "q3_two_hour_minimum": q3(con, policy, orc),
                                 "q4_watch6_last": q4(con, policy, today)}}
    finally:
        con.close()
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "diff.json").write_text(json.dumps(R, indent=2, default=str), encoding="utf-8")
    (out / "diff.md").write_text(render_markdown(R), encoding="utf-8")
    return R


def _table(rows: list[dict], cols: list[str]) -> list[str]:
    if not rows:
        return ["(none)"]
    fmt = lambda v: "" if v is None else (f"{v:g}" if isinstance(v, float) else str(v))   # noqa: E731
    return ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)] + \
           ["| " + " | ".join(fmt(r.get(c)) for c in cols) + " |" for r in rows]


def render_markdown(R: dict) -> str:
    L = [f"# Wave 2 rebuild: what changed (today {R['today']})", "",
         f"Old copy: {R['old']}. New copy: {R['new']}. Policy from {R['policy_home']}.", "",
         "## Daily values per metric", ""]
    L += _table(R["per_metric"], ["metric", "days_changed", "days_added", "days_removed", "median_change", "max_change", "grades_changed"])
    L += ["", "## Which device owns the day, before and after", ""]
    L += _table(R["owner_device_changes"], ["metric", "old_device", "new_device", "days", "first", "last"])
    b = R["baselines"]
    L += ["", f"## Latest owner baselines ({b['window_days']} days)", ""]
    L += _table(b["owner"], ["metric", "old_date", "old_median", "old_n_days", "new_date", "new_median", "new_n_days"])
    L += ["", "Apple Watch Ultra's own sleep baseline (new):", ""]
    L += _table(b["device_sleep_duration_apple_watch_ultra"], ["date", "window_days", "median", "mad", "n_days"])
    s = R["signals_last_30_days"]
    L += ["", f"## Signals from {s['from']} to {s['to']}", ""]
    L += _table([{"state": k, **v} for k, v in s["states"].items()], ["state", "old", "new"])
    L += ["", f"Days flagged as a schedule shift: old {len(s['travel_days']['old'])} {s['travel_days']['old']}, "
              f"new {len(s['travel_days']['new'])} {s['travel_days']['new']}.", "",
          "## The last 7 nights (hours asleep)", ""]
    rows = []
    for n in R["nights"]:
        w = n["whoop"]
        rows.append({"wake date": n["wake_date"],
                     "Helios old": f"{n['old']['value']} {n['old']['device']} {n['old']['grade']}" if n["old"] else None,
                     "Helios new": f"{n['new']['value']} {n['new']['device']} {n['new']['grade']}" if n["new"] else None,
                     "devices old": ", ".join(f"{k} {v}" for k, v in n["per_device_old"].items()),
                     "devices new": ", ".join(f"{k} {v}" for k, v in n["per_device_new"].items()),
                     "oracle": ", ".join(f"{k} {v}" for k, v in n["oracle_h"].items()),
                     "Whoop asleep, in bed, eff.": f"{w.get('asleep_h')}, {w.get('in_bed_h')}, {w.get('efficiency_pct')}%" if w else None,
                     "Whoop RHR, RR, recovery, rMSSD, strain": ", ".join(str(w.get(k)) for k in (
                         "resting_hr", "respiratory_rate", "recovery_score", "rmssd_ms", "strain")) if w else None,
                     "Whoop need parts": ", ".join(f"{v}" for v in (w.get("sleep_needed_h") or {}).values()) if w else None})
    L += _table(rows, list(rows[0]) if rows else [])
    oc = R["episode_oracle_check"]
    L += ["", "## Episode oracle against the stored per-device nights (new)", "",
          f"Compared {oc['compared']}: {oc['within_1_min']} within 1 minute, {oc['off']} off, {oc['not_stored']} not stored.", ""]
    L += _table(oc["off_nights"], ["wake_date", "device", "oracle_h", "stored_h"])
    q = R["owner_questions"]
    L += ["", "## Owner questions", "", "Q1, Whoop's Apple Health copy as the labelled fallback:", ""]
    L += _table([{"metric": k, **v} for k, v in q["q1_whoop_healthkit_fallback"].items()],
                ["metric", "applies", "days_with_a_whoop_healthkit_value", "days_that_would_change", "now_from", "first", "last", "reason"])
    L += ["", "Q3, a 2 h main-episode minimum instead of 3 h (nights gaining a value):", ""]
    L += _table([{"device": k, **v} for k, v in q["q3_two_hour_minimum"]["by_device"].items()],
                ["device", "nights_gaining_a_value", "blank_days_filled", "owner_days_changed", "corroboration_only", "first", "last"])
    L += ["", "Q4, Watch 6 history added last to resting HR and all-day HR:", ""]
    L += _table([{"metric": k, **v} for k, v in q["q4_watch6_last"].items()],
                ["metric", "applies", "days_with_legacy_rows", "blank_days_filled", "corroboration_only", "first", "last", "reason"])
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Wave 2: old copy against new copy, for the owner's diff table")
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--today", default=None, help="the reporting today, YYYY-MM-DD (default: today in the policy zone)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    policy = MetricPolicy(default_tz=load_settings().timezone)
    today = date.fromisoformat(args.today) if args.today else reporting_today(policy.zone)
    R = run(args.old, args.new, today, args.out, policy=policy)
    print(json.dumps({"per_metric": len(R["per_metric"]), "nights": len(R["nights"]), "out": args.out}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
