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


# Units whose medians read as whole numbers in the why text (T14): rates in
# beats or breaths per minute and energy in kcal; sums (steps, energy) too.
_INT_UNITS = frozenset({"count/min", "kcal", "bpm"})


def fmt_median(policy: MetricPolicy, metric: str, med: float) -> str:
    """A step count median reads "4,362", a resting HR "70"; the rest keep one
    decimal ("49.6" ms, "6.9" h, "30.5" bmi). Fix program A21 (T14)."""
    if policy.agg(metric) == "sum" or policy.unit(metric) in _INT_UNITS:
        return f"{med:,.0f}"
    return f"{med:.1f}"


def _state_for(policy: MetricPolicy, metric: str, value: float, base: dict) -> tuple[str, str]:
    med, mad = base["median"], base["mad"]
    k = policy.mad_k
    direction = policy.direction(metric)
    band = max(mad * k, abs(med) * 0.02)
    m = policy.get(metric)
    med_s = fmt_median(policy, metric, med)
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
            return "favorable", f"below your median ({med_s})"
        return ("flag" if value > med + band else "neutral"), f"above your median ({med_s})"
    if direction == "higher":
        if value >= med:
            return "favorable", f"at or above your median ({med_s})"
        return ("flag" if value < med - band else "neutral"), f"below your median ({med_s})"
    if direction == "band":
        if abs(value - med) <= band:
            return "favorable", f"near your baseline ({med_s})"
        return "flag", f"{value - med:+.1f} off your baseline ({med_s})"
    return "neutral", "informational"


def compute_signals(conn, policy: MetricPolicy, day: date) -> int:
    flags = ctx.context_flags(conn, day)
    written = 0
    daily_metrics = [m for m in policy.metrics if policy.daily(m)]
    # Signals of metrics that are no longer daily metrics are not kept.
    db.execute(conn, "DELETE FROM signals WHERE date = ? AND metric NOT IN (SELECT unnest(?))", [day, daily_metrics])
    for metric in daily_metrics:
        dv = db.fetchdicts(conn, """
            SELECT value, unit, device_key, confidence, grade FROM daily_values
            WHERE metric = ? AND date = ?""", [metric, day])
        base = get_baseline(conn, metric, day, policy.default_window)
        if not dv or dv[0]["value"] is None:
            # No canonical value for this date any more: the signal goes too.
            db.execute(conn, "DELETE FROM signals WHERE date = ? AND metric = ?", [day, metric])
            continue
        v = dv[0]
        owner = owner_device(policy, metric)
        fallback = owner is not None and v["device_key"] != owner
        med, mad = (base["median"], base["mad"]) if base else (None, None)
        if fallback:
            # Fix program A6 (audit T5): a value from a non-owner device is shown
            # and labelled, never judged against the mixed-device baseline (a
            # same-device baseline is Wave 2, baseline_scope).
            state, why, delta = "fallback", (f"from {v['device_key']} standing in for {owner}, "
                                             "not compared to your baseline"), None
        elif not base:
            state, why, delta = "insufficient", "not enough history for a baseline yet", None
        else:
            state, why = _state_for(policy, metric, v["value"], base)
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


def owner_device(policy: MetricPolicy, metric: str) -> str | None:
    """The metric's owner device: the head of its priority list (None when the
    list is empty)."""
    prio = policy.priority(metric)
    return prio[0] if prio else None


def signals_for(conn, day: date, policy: MetricPolicy | None = None) -> list[dict]:
    """The day's signals in display order. Every row carries `fallback` (the
    value comes from a device other than the metric's owner) and
    `owner_device`; with a policy both come from the priority lists, without
    one the boolean is read from the stored state (A6)."""
    rows = db.fetchdicts(conn, "SELECT * FROM signals WHERE date = ?", [day])
    order = {m: i for i, m in enumerate(TODAY_MARKERS)}
    rows.sort(key=lambda r: order.get(r["metric"], 99))
    for r in rows:
        r["context_flags"] = json.loads(r["context_flags"] or "[]")
        if policy is not None:
            owner = owner_device(policy, r["metric"])
            r["owner_device"] = owner
            r["fallback"] = bool(owner is not None and r["device_key"] != owner)
        else:
            r["owner_device"] = None
            r["fallback"] = r["state"] == "fallback"
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
