#!/usr/bin/env python
"""Wave 3 diff tool (design note docs/briefs/build-2026-10-09/wave3/design.md,
sections 4.4, 4.5 and 5 C5): what the Whoop back-pull and the second rebuild
changed, old copy against new copy, for the owner's plain-English diff table.

    cd server && HELIOS_HOME=<stage folder> <venv>/bin/python tools/wave3_diff.py <old.duckdb> <new.duckdb> \
        --today YYYY-MM-DD --out DIR [--reference-nights LIST] [--recent-from YYYY-MM-DD]

LIST is dates and inclusive ranges, comma separated: FIRST:LAST,DAY.

Both copies are attached READ_ONLY in one scratch DuckDB process; nothing is
written to either. wave2_diff.py supplies the per-metric and owner-device
tables, the latest owner baselines, the episode oracle and Whoop's own numbers
per night. This tool adds:
- whoop_records per kind and per month of the reporting zone, old and new, the
  first and last record of each kind, the months under 25 cycles in the new
  copy, and the records present in both whose updated_at moved (revised by
  Whoop);
- Whoop's Apple Health copy against the API, per night with both in the new
  copy: the longest Whoop episode of the oracle against the API record's
  light + SWS + REM, the nights within 3 minutes and those over 30;
- recovery, rMSSD, strain and sleep need: days added (typical and largest
  value), changed, switched and removed;
- sleep, resting HR and breathing rate: the owner device switches, with the
  typical and largest change;
- the reference nights, old and new per metric with the device, beside
  Whoop's own numbers for the night;
- the recent window: every daily value dated on or after --recent-from
  (default: the oldest Whoop record of the old copy, in the reporting zone)
  that moved, and every latest baseline (each window of the policy, owner and
  per device) that moved;
- whole-table counts of the rows that differ in daily_values, baselines,
  device_baselines, signals and whoop_records.
"empty" is true when nothing differs anywhere: a plain copy against its own
rebuild must give that (design 4.4 check 10). The policy and the zone come
from HELIOS_HOME, as for wave2_diff.py. Writes <out>/diff.json and
<out>/summary.md (dated values included: the folder is git-excluded) and
prints one JSON line of counts.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import wave2_diff as w2  # noqa: E402
from heliosd.config import helios_home, load_settings  # noqa: E402
from heliosd.ingest.normalize import reporting_today  # noqa: E402
from heliosd.ingest.whoop import _asleep_ms  # noqa: E402
from heliosd.trust.policy import MetricPolicy  # noqa: E402

WHOOP_ONLY = ("recovery_score", "hrv_rmssd", "strain", "sleep_need")
SWITCHED = ("sleep_duration", "resting_hr", "respiratory_rate")
REFERENCE_METRICS = ("sleep_duration", "recovery_score", "hrv_rmssd", "resting_hr", "respiratory_rate", "strain", "sleep_need")
WITHIN_MIN = 3.0               # design 4.4 check 7: the copy and the API agree
OVER_MIN = 30.0                # nights listed (the S6 overlap nights are expected there)
FULL_MONTH_CYCLES = 25         # design 4.4 check 1: a month with fewer cycles is listed
LIST_CAP = 500                 # rows kept per list in diff.json; the counts are always complete
DV_FIELDS = ("value", "device_key", "grade", "n_samples", "confidence", "corroboration", "detail")
TABLES = {                     # whole-table comparison: key columns, compared columns
    "daily_values": (("date", "metric"), DV_FIELDS),
    "baselines": (("date", "metric", "window_days"), ("median", "mad", "n_days")),
    "device_baselines": (("date", "metric", "window_days", "device_key"), ("median", "mad", "n_days")),
    "signals": (("date", "metric"), ("state", "value", "unit", "baseline_median", "baseline_mad", "delta_pct", "device_key",
                                     "confidence", "grade", "context_flags", "why")),
    "whoop_records": (("record_key",), ("kind", "start_utc", "end_utc", "score_state", "nap", "updated_at", "payload")),
}


def parse_dates(spec: str | None) -> list[date]:
    """'A:B,C' -> every date from A to B (inclusive) and C, sorted, once each."""
    out: set[date] = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            a, b = (date.fromisoformat(x.strip()) for x in part.split(":", 1))
            if b < a:
                raise ValueError(f"reversed range {part!r}")
            out.update(a + timedelta(days=i) for i in range((b - a).days + 1))
        else:
            out.add(date.fromisoformat(part))
    return sorted(out)


def _months(lo: date, hi: date) -> list[str]:
    out, (y, m) = [], (lo.year, lo.month)
    while (y, m) <= (hi.year, hi.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _both(con, table: str, column: str | None = None) -> bool:
    return w2._has(con, "o", table, column) and w2._has(con, "n", table, column)


def oldest_record_date(con, side: str, zone) -> date | None:
    """The reporting date of the oldest Whoop record (its start; a recovery's creation)."""
    if not w2._has(con, side, "whoop_records"):
        return None
    return w2._wake_date(con.execute(f"SELECT MIN(COALESCE(start_utc, created_at)) FROM {side}.whoop_records").fetchone()[0],
                         zone)


def records(con, zone) -> dict:
    """whoop_records per kind and per month (reporting zone), old and new."""
    stamps: dict[str, dict[str, list]] = {"old": defaultdict(list), "new": defaultdict(list)}
    for side, name in (("o", "old"), ("n", "new")):
        if w2._has(con, side, "whoop_records"):
            for kind, t in con.execute(f"SELECT kind, COALESCE(start_utc, created_at) FROM {side}.whoop_records").fetchall():
                stamps[name][kind].append(w2._wake_date(t, zone))
    by_month: dict[tuple[str, str], dict] = defaultdict(lambda: {"old": 0, "new": 0})
    kinds: dict[str, dict] = {}
    for name, per_kind in stamps.items():
        for kind, ds in per_kind.items():
            for d in ds:
                by_month[(f"{d:%Y-%m}" if d else "undated", kind)][name] += 1
            dated = [d for d in ds if d]
            k = kinds.setdefault(kind, {"old": 0, "new": 0, "old_first": None, "old_last": None, "new_first": None, "new_last": None})
            k[name] = len(ds)
            k[f"{name}_first"] = str(min(dated)) if dated else None
            k[f"{name}_last"] = str(max(dated)) if dated else None
    rows = [{"month": m, "kind": kind, **v} for (m, kind), v in sorted(by_month.items())]
    cycles = [d for d in stamps["new"].get("cycle", []) if d]
    per = Counter(f"{d:%Y-%m}" for d in cycles)
    under = [{"month": m, "cycles": per.get(m, 0)} for m in (_months(min(cycles), max(cycles)) if cycles else [])
             if per.get(m, 0) < FULL_MONTH_CYCLES]
    revised = []
    if _both(con, "whoop_records"):
        revised = w2._dicts(con, """SELECT a.record_key, a.kind, a.updated_at AS old_updated_at, b.updated_at AS new_updated_at
            FROM o.whoop_records a JOIN n.whoop_records b USING (record_key)
            WHERE a.updated_at IS DISTINCT FROM b.updated_at ORDER BY 1""")
    return {"kinds": dict(sorted(kinds.items())), "by_month": rows,
            "months_changed": sum(1 for r in rows if r["old"] != r["new"]),
            "new_months_under_25_cycles": under, "revised_by_whoop": len(revised), "revised": revised[:LIST_CAP]}


def copy_vs_api(con, zone, orc: dict) -> dict:
    """Whoop's Apple Health copy (the oracle's longest Whoop episode) against
    the API record of the same wake date (the scored main sleep, light + SWS +
    REM), on every night the new copy holds both."""
    api: dict[date, float] = {}
    if w2._has(con, "n", "whoop_records"):
        for end_utc, nap, state, payload in con.execute(
                "SELECT end_utc, nap, score_state, payload FROM n.whoop_records WHERE kind = 'sleep'").fetchall():
            if nap or end_utc is None or state != "SCORED":
                continue
            d = w2._wake_date(end_utc, zone)
            api[d] = max(api.get(d, 0.0), _asleep_ms(payload) / 3.6e6)
    copy = {d: h for (dk, d), h in orc.items() if dk == "whoop"}
    nights = [{"wake_date": str(d), "copy_h": w2._r(copy[d]), "api_h": w2._r(api[d]), "diff_min": round((copy[d] - api[d]) * 60, 1)}
              for d in sorted(set(api) & set(copy))]
    within = sum(1 for n in nights if abs(n["diff_min"]) <= WITHIN_MIN)
    over = [n for n in nights if abs(n["diff_min"]) > OVER_MIN]
    return {"api_nights": len(api), "copy_nights": len(copy), "nights_with_both": len(nights), "within_3_min": within,
            "within_3_min_pct": round(100 * within / len(nights), 1) if nights else None,
            "median_abs_diff_min": round(statistics.median(abs(n["diff_min"]) for n in nights), 1) if nights else None,
            "over_30_min": len(over), "over_30_min_nights": over[:LIST_CAP]}


def added_and_changed(con, metrics=WHOOP_ONLY) -> list[dict]:
    """Per metric: the days added (typical and largest value), changed, switched and removed."""
    add = "ov IS NULL AND nv IS NOT NULL"
    rows = {r["metric"]: r for r in w2._dicts(con, f"""{w2._JOIN}
        SELECT metric, COUNT(*) FILTER (WHERE {add}) AS days_added, MIN(d) FILTER (WHERE {add}) AS first_added,
               MAX(d) FILTER (WHERE {add}) AS last_added, ROUND(MEDIAN(nv) FILTER (WHERE {add}), 3) AS typical_added,
               ROUND(MIN(nv) FILTER (WHERE {add}), 3) AS lowest_added, ROUND(MAX(nv) FILTER (WHERE {add}), 3) AS largest_added,
               COUNT(*) FILTER (WHERE ov IS NOT NULL AND nv IS NOT NULL AND (ov <> nv OR od IS DISTINCT FROM nd)) AS days_changed,
               COUNT(*) FILTER (WHERE ov IS NOT NULL AND nv IS NOT NULL AND od IS DISTINCT FROM nd) AS days_switched,
               ROUND(MEDIAN(abs(nv - ov)) FILTER (WHERE ov <> nv), 3) AS typical_change,
               ROUND(MAX(abs(nv - ov)) FILTER (WHERE ov <> nv), 3) AS largest_change,
               COUNT(*) FILTER (WHERE ov IS NOT NULL AND nv IS NULL) AS days_removed,
               COUNT(*) FILTER (WHERE ov IS NOT NULL) AS days_old, COUNT(*) FILTER (WHERE nv IS NOT NULL) AS days_new
        FROM j WHERE metric IN (SELECT unnest(?)) GROUP BY 1""", [list(metrics)])}
    zero = {"days_added": 0, "first_added": None, "last_added": None, "typical_added": None, "lowest_added": None,
            "largest_added": None, "days_changed": 0, "days_switched": 0, "typical_change": None, "largest_change": None,
            "days_removed": 0, "days_old": 0, "days_new": 0}
    return [rows.get(m) or {"metric": m, **zero} for m in metrics]


def owner_switches(con, metrics=SWITCHED) -> list[dict]:
    """Which device owns the day, before and after, with the size of the change
    (old_device None: a day added; new_device None: a day removed)."""
    return w2._dicts(con, f"""{w2._JOIN}
        SELECT metric, od AS old_device, nd AS new_device, COUNT(*) AS days, MIN(d) AS first, MAX(d) AS last,
               ROUND(MEDIAN(nv - ov), 3) AS median_signed_change, ROUND(MEDIAN(abs(nv - ov)), 3) AS typical_change,
               ROUND(MAX(abs(nv - ov)), 3) AS largest_change
        FROM j WHERE metric IN (SELECT unnest(?)) AND od IS DISTINCT FROM nd
        GROUP BY 1, 2, 3 ORDER BY 1, 4 DESC, 2, 3""", [list(metrics)])


def reference_nights(con, zone, dates: list[date]) -> list[dict]:
    if not dates:
        return []
    vals: dict[tuple, dict] = {}
    for side, name in (("o", "old"), ("n", "new")):
        for d, m, v, dk, g in con.execute(
                f"SELECT date, metric, value, device_key, grade FROM {side}.daily_values "
                "WHERE date IN (SELECT unnest(?)) AND metric IN (SELECT unnest(?))", [dates, list(REFERENCE_METRICS)]).fetchall():
            vals[(d, m, name)] = {"value": w2._r(v), "device": dk, "grade": g}
    wh = w2.whoop_nights(con, zone, dates) if w2._has(con, "n", "whoop_records") else {}
    return [{"date": str(d), "metrics": {m: {"old": vals.get((d, m, "old")), "new": vals.get((d, m, "new"))} for m in REFERENCE_METRICS},
             "whoop": wh.get(d)} for d in dates]


def recent_daily_values(con, recent_from: date) -> dict:
    """Every daily value dated on or after recent_from that differs (any
    compared field), or exists on one side only."""
    cols = [c for c in DV_FIELDS if _both(con, "daily_values", c)]
    sel = ", ".join(f"a.{c} AS o_{c}, b.{c} AS n_{c}" for c in cols)
    cond = " OR ".join(["a.date IS NULL", "b.date IS NULL"] + [f"a.{c} IS DISTINCT FROM b.{c}" for c in cols])
    rows = con.execute(f"""SELECT COALESCE(a.date, b.date) AS d, COALESCE(a.metric, b.metric) AS metric,
               a.date IS NOT NULL AS in_old, b.date IS NOT NULL AS in_new, {sel}
        FROM (SELECT * FROM o.daily_values WHERE date >= ?) a FULL OUTER JOIN (SELECT * FROM n.daily_values WHERE date >= ?) b
          ON a.date = b.date AND a.metric = b.metric
        WHERE {cond} ORDER BY 1, 2""", [recent_from, recent_from]).fetchall()
    moved = []
    for d, metric, in_old, in_new, *v in rows:
        old, new = dict(zip(cols, v[0::2])), dict(zip(cols, v[1::2]))
        brief = lambda x, present: ({"value": w2._r(x["value"]), "device": x["device_key"], "grade": x["grade"]}   # noqa: E731
                                    if present else None)
        moved.append({"date": str(d), "metric": metric, "fields": [c for c in cols if old[c] != new[c]] if in_old and in_new else
                      (["added"] if in_new else ["removed"]), "old": brief(old, in_old), "new": brief(new, in_new)})
    return {"from": str(recent_from), "moved": len(moved), "rows": moved[:LIST_CAP],
            "by_metric": dict(sorted(Counter(r["metric"] for r in moved).items()))}


def latest_baselines_moved(con, today: date, windows: list[int]) -> dict:
    """Per table: the latest baseline of every (metric, window[, device]) on or
    before today, old against new, listed when anything differs."""
    out: dict[str, list] = {}
    for table, keys in (("baselines", ("metric", "window_days")), ("device_baselines", ("metric", "window_days", "device_key"))):
        if not _both(con, table):
            continue
        part = ", ".join(keys)
        latest = (f"SELECT * FROM {{}}.{table} WHERE date <= ? AND window_days IN (SELECT unnest(?)) "
                  f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY date DESC) = 1")
        out[table] = w2._dicts(con, f"""WITH ob AS ({latest.format('o')}), nb AS ({latest.format('n')})
            SELECT {", ".join(f"COALESCE(ob.{k}, nb.{k}) AS {k}" for k in keys)},
                   ob.date AS old_date, ROUND(ob.median, 3) AS old_median, ROUND(ob.mad, 3) AS old_mad, ob.n_days AS old_n_days,
                   nb.date AS new_date, ROUND(nb.median, 3) AS new_median, ROUND(nb.mad, 3) AS new_mad, nb.n_days AS new_n_days
            FROM ob FULL OUTER JOIN nb ON {" AND ".join(f"ob.{k} = nb.{k}" for k in keys)}
            WHERE ob.date IS DISTINCT FROM nb.date OR ob.median IS DISTINCT FROM nb.median OR ob.mad IS DISTINCT FROM nb.mad
               OR ob.n_days IS DISTINCT FROM nb.n_days
            ORDER BY {part}""", [today, windows, today, windows])
    return out


def whole_tables(con) -> dict:
    """Rows that differ per table over every date (one side only, or any compared column)."""
    out: dict[str, dict | None] = {}
    for table, (keys, fields) in TABLES.items():
        if not _both(con, table):
            out[table] = None
            continue
        cols = [c for c in fields if _both(con, table, c)]
        on = " AND ".join(f"a.{k} = b.{k}" for k in keys)
        cond = " OR ".join([f"a.{keys[0]} IS NULL", f"b.{keys[0]} IS NULL"] + [f"a.{c} IS DISTINCT FROM b.{c}" for c in cols])
        span = "MIN(COALESCE(a.date, b.date)), MAX(COALESCE(a.date, b.date))" if "date" in keys else "NULL, NULL"
        n, only_old, only_new, lo, hi = con.execute(
            f"SELECT COUNT(*), COUNT(*) FILTER (WHERE b.{keys[0]} IS NULL), COUNT(*) FILTER (WHERE a.{keys[0]} IS NULL), {span} "
            f"FROM o.{table} a FULL OUTER JOIN n.{table} b ON {on} WHERE {cond}").fetchone()
        out[table] = {"rows_differing": n, "only_old": only_old, "only_new": only_new,
                      "first": str(lo) if lo is not None else None, "last": str(hi) if hi is not None else None}
    return out


def is_empty(R: dict) -> bool:
    return (all(r["days_changed"] == 0 and r["days_added"] == 0 and r["days_removed"] == 0 and r["grades_changed"] == 0
                for r in R["per_metric"])
            and not R["owner_device_changes"] and R["records"]["months_changed"] == 0 and R["records"]["revised_by_whoop"] == 0
            and R["recent_window"]["daily_values"]["moved"] == 0
            and not any(R["recent_window"]["latest_baselines_moved"].values())
            and all(v is None or v["rows_differing"] == 0 for v in R["whole_tables"].values()))


def run(old, new, today: date, out, policy: MetricPolicy | None = None, reference: list[date] | None = None,
        recent_from: date | None = None) -> dict:
    policy = policy or MetricPolicy(default_tz=load_settings().timezone)
    con = duckdb.connect()
    try:
        for alias, path in (("o", old), ("n", new)):
            con.execute(f"ATTACH '{str(Path(path)).replace(chr(39), chr(39) * 2)}' AS {alias} (READ_ONLY)")
        zone = policy.zone
        recent_from = recent_from or oldest_record_date(con, "o", zone) or oldest_record_date(con, "n", zone) or today
        orc = w2.oracle(con)
        R = {"old": str(old), "new": str(new), "today": str(today), "zone": str(zone), "policy_home": str(helios_home()),
             "generated": datetime.now().isoformat(timespec="seconds"),
             "records": records(con, zone), "copy_vs_api": copy_vs_api(con, zone, orc),
             "whoop_only_metrics": added_and_changed(con), "owner_switches": owner_switches(con),
             "reference_nights": reference_nights(con, zone, reference or []),
             "recent_window": {"daily_values": recent_daily_values(con, recent_from),
                               "latest_baselines_moved": latest_baselines_moved(con, today, policy.windows),
                               "windows": policy.windows},
             "per_metric": w2.per_metric(con), "owner_device_changes": w2.owner_device_changes(con),
             "baselines_default_window": w2.baselines(con, policy, today), "episode_oracle_check": w2.oracle_check(con, orc),
             "whole_tables": whole_tables(con)}
    finally:
        con.close()
    R["empty"] = is_empty(R)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "diff.json").write_text(json.dumps(R, indent=2, default=str), encoding="utf-8")
    (out / "summary.md").write_text(render_markdown(R), encoding="utf-8")
    return R


def render_markdown(R: dict) -> str:
    T = w2._table
    rec, cva, rw = R["records"], R["copy_vs_api"], R["recent_window"]
    L = [f"# Wave 3 back-pull: what changed (today {R['today']}, zone {R['zone']})", "",
         f"Old copy: {R['old']}. New copy: {R['new']}. Policy from {R['policy_home']}.", "",
         "Nothing differs anywhere (empty diff)." if R["empty"] else "The copies differ: see below.", "",
         "## Whoop records per kind", ""]
    L += T([{"kind": k, **v} for k, v in rec["kinds"].items()], ["kind", "old", "new", "old_first", "old_last", "new_first", "new_last"])
    months = sorted({r["month"] for r in rec["by_month"]})
    cell = {(r["month"], r["kind"]): r for r in rec["by_month"]}
    kinds = sorted(rec["kinds"])
    L += ["", "## Whoop records per month (old / new)", ""]
    L += T([{"month": m, **{k: f"{cell.get((m, k), {}).get('old', 0)} / {cell.get((m, k), {}).get('new', 0)}" for k in kinds}}
            for m in months], ["month"] + kinds)
    L += ["", f"Months under {FULL_MONTH_CYCLES} cycles in the new copy: "
              + (", ".join(f"{u['month']} ({u['cycles']})" for u in rec["new_months_under_25_cycles"]) or "none") + ".",
          f"Records in both copies that Whoop revised (updated_at moved): {rec['revised_by_whoop']}.", ""]
    L += T(rec["revised"][:60], ["record_key", "kind", "old_updated_at", "new_updated_at"])
    L += ["", "## Whoop's Apple Health copy against the API (new copy)", "",
          f"Nights with both: {cva['nights_with_both']} (API nights {cva['api_nights']}, copy nights {cva['copy_nights']}). "
          f"Within {WITHIN_MIN:g} minutes: {cva['within_3_min']} ({cva['within_3_min_pct']}%). "
          f"Median difference {cva['median_abs_diff_min']} min. Over {OVER_MIN:g} minutes: {cva['over_30_min']}.", ""]
    L += T(cva["over_30_min_nights"][:60], ["wake_date", "copy_h", "api_h", "diff_min"])
    L += ["", "## Recovery, rMSSD, strain and sleep need", ""]
    L += T(R["whoop_only_metrics"], ["metric", "days_old", "days_new", "days_added", "first_added", "last_added", "typical_added",
                                     "lowest_added", "largest_added", "days_changed", "days_switched", "typical_change",
                                     "largest_change", "days_removed"])
    L += ["", "## Sleep, resting HR and breathing rate: which device owns the day", ""]
    L += T(R["owner_switches"], ["metric", "old_device", "new_device", "days", "first", "last", "median_signed_change",
                                 "typical_change", "largest_change"])
    if R["reference_nights"]:
        L += ["", "## Reference nights (old -> new; Whoop's own record of the night)", ""]
        rows = []
        for n in R["reference_nights"]:
            def fmt(x):
                return f"{x['value']} {x['device']}" if x else "-"
            w = n["whoop"] or {}
            rows.append({"date": n["date"], **{m: f"{fmt(v['old'])} -> {fmt(v['new'])}" for m, v in n["metrics"].items()},
                         "Whoop asleep, recovery, RHR, rMSSD, RR, strain": ", ".join(
                             str(w.get(k)) for k in ("asleep_h", "recovery_score", "resting_hr", "rmssd_ms", "respiratory_rate",
                                                     "strain")) if w else "-"})
        L += T(rows, list(rows[0]))
    dv = rw["daily_values"]
    L += ["", f"## Recent window (from {dv['from']}): daily values that moved", "",
          f"{dv['moved']} rows; by metric: {dv['by_metric'] or 'none'}.", ""]
    L += T([{"date": r["date"], "metric": r["metric"], "fields": ", ".join(r["fields"]),
             "old": f"{r['old']['value']} {r['old']['device']} {r['old']['grade']}" if r["old"] else "-",
             "new": f"{r['new']['value']} {r['new']['device']} {r['new']['grade']}" if r["new"] else "-"} for r in dv["rows"][:80]],
           ["date", "metric", "fields", "old", "new"])
    for table, rows in rw["latest_baselines_moved"].items():
        keys = ["metric", "window_days"] + (["device_key"] if table == "device_baselines" else [])
        L += ["", f"Latest {table} that moved (windows {rw['windows']}): {len(rows)}.", ""]
        L += T(rows, keys + ["old_date", "old_median", "old_mad", "old_n_days", "new_date", "new_median", "new_mad", "new_n_days"])
    L += ["", "## Daily values per metric (all dates)", ""]
    L += T(R["per_metric"], ["metric", "days_changed", "days_added", "days_removed", "median_change", "max_change", "grades_changed"])
    b = R["baselines_default_window"]
    L += ["", f"## Latest owner baselines ({b['window_days']} days)", ""]
    L += T(b["owner"], ["metric", "old_date", "old_median", "old_n_days", "new_date", "new_median", "new_n_days"])
    oc = R["episode_oracle_check"]
    L += ["", "## Episode oracle against the stored per-device nights (new)", "",
          f"Compared {oc['compared']}: {oc['within_1_min']} within 1 minute, {oc['off']} off, {oc['not_stored']} not stored.", ""]
    L += ["## Whole tables: rows that differ", ""]
    L += T([{"table": t, **(v or {"rows_differing": "not in both"})} for t, v in R["whole_tables"].items()],
           ["table", "rows_differing", "only_old", "only_new", "first", "last"])
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Wave 3: old copy against new copy after the Whoop back-pull, for the owner's diff table")
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--today", default=None, help="the reporting today, YYYY-MM-DD (default: today in the policy zone)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--reference-nights", default=None, help="dates and inclusive ranges, comma separated: FIRST:LAST,DAY")
    ap.add_argument("--recent-from", default=None,
                    help="YYYY-MM-DD (default: the oldest Whoop record of the old copy, in the reporting zone)")
    args = ap.parse_args(argv)
    policy = MetricPolicy(default_tz=load_settings().timezone)
    today = date.fromisoformat(args.today) if args.today else reporting_today(policy.zone)
    R = run(args.old, args.new, today, args.out, policy=policy, reference=parse_dates(args.reference_nights),
            recent_from=date.fromisoformat(args.recent_from) if args.recent_from else None)
    print(json.dumps({"empty": R["empty"], "records_new": {k: v["new"] for k, v in R["records"]["kinds"].items()},
                      "copy_vs_api": [R["copy_vs_api"]["nights_with_both"], R["copy_vs_api"]["within_3_min"]],
                      "recent_moved": R["recent_window"]["daily_values"]["moved"],
                      "whole_tables": {t: (v or {}).get("rows_differing") for t, v in R["whole_tables"].items()},
                      "out": args.out}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
