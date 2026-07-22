"""Marker states: favorable / neutral / flag / insufficient, per metric,
against the owner's own baseline. No composite score exists anywhere."""

from __future__ import annotations

import json
from datetime import date

from heliosd.signals import context as ctx
from heliosd.signals.baselines import get_baseline
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy

# Metrics surfaced as recovery signals on the Today screen, in display order.
TODAY_MARKERS = ["recovery_score", "hrv_rmssd", "resting_hr", "sleep_duration",
                 "respiratory_rate", "hrv_sdnn", "spo2", "wrist_temp", "strain", "steps"]


def _state_for(policy: MetricPolicy, metric: str, value: float, base: dict) -> tuple[str, str]:
    med, mad = base["median"], base["mad"]
    k = policy.mad_k
    direction = policy.direction(metric)
    band = max(mad * k, abs(med) * 0.02)
    m = policy.get(metric)
    if "zones" in m:  # e.g. whoop recovery
        z = m["zones"]
        if value >= z["green"][0]:
            return "favorable", f"in the green zone ({value:.0f}%)"
        if value >= z["yellow"][0]:
            return "neutral", f"in the yellow zone ({value:.0f}%)"
        return "flag", f"in the red zone ({value:.0f}%)"
    # explicit owner rules
    rule = m.get("flag_rule", "")
    if rule.startswith("abs_above_30d_avg") and value >= med + float(rule.split(">=")[1]):
        return "flag", f"{value - med:+.0f} above your 30-day baseline"
    if rule.startswith("below_30d_baseline_pct") and med and (med - value) / med * 100 >= float(rule.split(">=")[1]):
        return "flag", f"{(med - value) / med * 100:.0f}% below your 30-day baseline"
    if rule.startswith("below_hours") and value < float(rule.split()[1]):
        return "flag", f"under {rule.split()[1]}h"
    if direction == "lower":
        if value <= med:
            return "favorable", f"below your median ({med:.0f})"
        return ("flag" if value > med + band else "neutral"), f"above your median ({med:.0f})"
    if direction == "higher":
        if value >= med:
            return "favorable", f"at or above your median ({med:.1f})"
        return ("flag" if value < med - band else "neutral"), f"below your median ({med:.1f})"
    if direction == "band":
        if abs(value - med) <= band:
            return "favorable", f"near your baseline ({med:.1f})"
        return "flag", f"{value - med:+.1f} off your baseline ({med:.1f})"
    return "neutral", "informational"


def compute_signals(conn, policy: MetricPolicy, day: date) -> int:
    flags = ctx.context_flags(conn, day)
    written = 0
    for metric in policy.metrics:
        if metric == "sleep_analysis":
            continue
        dv = db.fetchdicts(conn, """
            SELECT value, unit, device_key, confidence, grade FROM daily_values
            WHERE metric = ? AND date = ?""", [metric, day])
        base = get_baseline(conn, metric, day, policy.default_window)
        if not dv or dv[0]["value"] is None:
            continue
        v = dv[0]
        if not base:
            state, why = "insufficient", "not enough history for a baseline yet"
            med = mad = delta = None
        else:
            state, why = _state_for(policy, metric, v["value"], base)
            med, mad = base["median"], base["mad"]
            delta = round((v["value"] - med) / med * 100, 1) if med else None
        db.execute(conn, """
            INSERT OR REPLACE INTO signals
              (date, metric, state, value, unit, baseline_median, baseline_mad, delta_pct,
               device_key, confidence, grade, context_flags, why)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [day, metric, state, v["value"], v["unit"], med, mad, delta,
             v["device_key"], v["confidence"], v["grade"], json.dumps(flags), why])
        written += 1
    return written


def signals_for(conn, day: date) -> list[dict]:
    rows = db.fetchdicts(conn, "SELECT * FROM signals WHERE date = ?", [day])
    order = {m: i for i, m in enumerate(TODAY_MARKERS)}
    rows.sort(key=lambda r: order.get(r["metric"], 99))
    for r in rows:
        r["context_flags"] = json.loads(r["context_flags"] or "[]")
    return rows


def verdict(signals: list[dict]) -> str:
    core = [s for s in signals if s["metric"] in TODAY_MARKERS[:4] and s["state"] != "insufficient"]
    if not core:
        return "Not enough data yet. Wear your devices tonight and check back."
    n_fav = sum(1 for s in core if s["state"] == "favorable")
    n_flag = sum(1 for s in core if s["state"] == "flag")
    if n_flag == 0 and n_fav >= max(1, len(core) - 1):
        return "Recovery signals lean favorable."
    if n_flag >= 2:
        return "Several signals are off baseline. Take it easy today."
    if n_flag == 1:
        return "Mostly steady, one signal is off baseline."
    return "Signals are mixed but steady."
