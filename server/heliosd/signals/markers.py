"""Marker states: favorable / neutral / flag / insufficient, per metric,
against the owner's own baseline, plus in_progress (a running total of the
reporting today, owner decision D7, or an open Whoop cycle's strain so far,
Wave 2 B7) and fallback (a stand-in device's value, fix program A6). No
composite score exists anywhere."""

from __future__ import annotations

import json
from datetime import date, datetime

from heliosd.ingest.normalize import reporting_today
from heliosd.signals import context as ctx
from heliosd.signals.baselines import detail_in_progress, get_baseline
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy

# Metrics surfaced as recovery signals on the Today screen, in display order.
TODAY_MARKERS = ["recovery_score", "hrv_rmssd", "resting_hr", "sleep_duration",
                 "respiratory_rate", "hrv_sdnn", "spo2", "wrist_temp", "strain", "steps"]
CORE_MARKERS = TODAY_MARKERS[:4]
# States that carry a judgement against the baseline. in_progress, fallback
# and insufficient rows are facts on the screen, never judged.
JUDGED = ("favorable", "neutral", "flag")
IN_PROGRESS_WHY = "so far today, the day is not complete"
# An open Whoop cycle (B7): its strain keeps growing until the next sleep
# onset, whatever the reporting day says.
CYCLE_OPEN_WHY = "cycle still open, strain so far"
# The verdict while last night's Whoop recovery has not arrived (fix program
# A5, audit T6). The web shows this exact text.
WAITING_FOR_WHOOP = "Waiting for Whoop's recovery for last night."
MISSING_WHOOP = "Whoop's recovery for this night is missing."


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


def in_progress_why(policy: MetricPolicy, metric: str, day: date, today: date,
                    detail=None, device_key: str | None = None) -> str | None:
    """Why a day's value can still change, or None when it is final: an open
    Whoop cycle (its daily value's detail says in_progress, B7), on whatever
    day it files, else a running total of the reporting today (D7) from the
    device the value comes from (B5: Whoop's cloud resting HR today is final,
    Apple's is so far)."""
    if detail_in_progress(detail):
        return CYCLE_OPEN_WHY
    if day == today and policy.running_total(metric, device_key):
        return IN_PROGRESS_WHY
    return None


def _judge(policy: MetricPolicy, metric: str, value: float, device_key: str | None,
           base: dict | None, in_progress: str | None = None) -> tuple[str, str, float | None]:
    """(state, why, delta_pct) for one daily value; the one rule shared by
    compute_signals and the read-time presentation in signals_for.
    Precedence: in_progress (`in_progress` is the reason from in_progress_why:
    the number will still change, the most important fact), fallback (fix
    program A6, audit T5: a stand-in device's value is shown and labelled,
    never judged against the mixed-device baseline; a same-device baseline is
    Wave 2, baseline_scope), insufficient (no baseline), then the judged
    states."""
    if in_progress:
        return "in_progress", in_progress, None
    owner = owner_device(policy, metric)
    if owner is not None and device_key != owner:
        return "fallback", f"from {device_key} standing in for {owner}, not compared to your baseline", None
    if not base:
        return "insufficient", "not enough history for a baseline yet", None
    state, why = _state_for(policy, metric, value, base)
    med = base["median"]
    return state, why, (round((value - med) / med * 100, 1) if med else None)


def _stored_base(row: dict) -> dict | None:
    """The baseline a stored signal row was judged against."""
    if row.get("baseline_median") is None:
        return None
    return {"median": row["baseline_median"], "mad": row.get("baseline_mad") or 0.0}


def compute_signals(conn, policy: MetricPolicy, day: date, today: date | None = None,
                    now: datetime | None = None) -> int:
    """Signals of one date. `today` is the reporting today of the pass (the
    recompute passes its own, so one pass has one clock); without it the
    reporting zone's clock decides. A running total of the reporting today
    (D7) and an open Whoop cycle (B7) are in_progress: no delta, no flag, no
    grade."""
    today = today or reporting_today(policy.zone, now)
    flags = ctx.context_flags(conn, day)
    written = 0
    daily_metrics = [m for m in policy.metrics if policy.daily(m)]
    # Signals of metrics that are no longer daily metrics are not kept.
    db.execute(conn, "DELETE FROM signals WHERE date = ? AND metric NOT IN (SELECT unnest(?))", [day, daily_metrics])
    for metric in daily_metrics:
        dv = db.fetchdicts(conn, """
            SELECT value, unit, device_key, confidence, grade, detail FROM daily_values
            WHERE metric = ? AND date = ?""", [metric, day])
        base = get_baseline(conn, metric, day, policy.default_window)
        if not dv or dv[0]["value"] is None:
            # No canonical value for this date any more: the signal goes too.
            db.execute(conn, "DELETE FROM signals WHERE date = ? AND metric = ?", [day, metric])
            continue
        v = dv[0]
        med, mad = (base["median"], base["mad"]) if base else (None, None)
        in_progress = in_progress_why(policy, metric, day, today, v["detail"], v["device_key"])
        state, why, delta = _judge(policy, metric, v["value"], v["device_key"], base, in_progress)
        conf_, grade_ = (None, None) if in_progress else (v["confidence"], v["grade"])
        db.execute(conn, """
            INSERT OR REPLACE INTO signals
              (date, metric, state, value, unit, baseline_median, baseline_mad, delta_pct,
               device_key, confidence, grade, context_flags, why)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [day, metric, state, v["value"], v["unit"], med, mad, delta,
             v["device_key"], conf_, grade_, json.dumps(flags), why])
        written += 1
    return written


def owner_device(policy: MetricPolicy, metric: str) -> str | None:
    """The metric's owner device: the head of its priority list (None when the
    list is empty)."""
    prio = policy.priority(metric)
    return prio[0] if prio else None


def signals_for(conn, day: date, policy: MetricPolicy | None = None,
                today: date | None = None) -> list[dict]:
    """The day's signals in display order. With a policy every row carries
    `owner_device` and the boolean `fallback` (the value comes from a device
    other than the metric's owner), taken from the priority lists and
    independent of the state, and every row is presented against the
    reporting today (`today`, else the policy's clock) from its stored
    baseline when its stored state disagrees: a running total of the
    reporting today is in_progress even if an older pass judged it (Codex A
    point 3), a closed day still marked in progress is judged (point 2) unless
    its daily value is an open Whoop cycle (B7), a judged stand-in is a
    fallback and a fallback whose device is now the owner is judged (point
    16). Without a policy rows are returned as stored and provenance is
    unknown: owner_device and fallback are None, never guessed from the
    state."""
    rows = db.fetchdicts(conn, "SELECT s.*, d.detail AS daily_detail FROM signals s LEFT JOIN daily_values d "
                               "ON d.date = s.date AND d.metric = s.metric WHERE s.date = ?", [day])
    order = {m: i for i, m in enumerate(TODAY_MARKERS)}
    rows.sort(key=lambda r: order.get(r["metric"], 99))
    if policy is not None:
        today = today or reporting_today(policy.zone)
    for r in rows:
        r["context_flags"] = json.loads(r["context_flags"] or "[]")
        detail = r.pop("daily_detail")
        if policy is None:
            r["owner_device"] = r["fallback"] = None
            continue
        owner = owner_device(policy, r["metric"])
        fb = bool(owner is not None and r["device_key"] != owner)
        r["owner_device"], r["fallback"] = owner, fb
        in_progress = in_progress_why(policy, r["metric"], day, today, detail, r["device_key"])
        stale = bool(in_progress) != (r["state"] == "in_progress") or (
            not in_progress and fb != (r["state"] == "fallback"))
        if r["value"] is not None and stale:
            r["state"], r["why"], r["delta_pct"] = _judge(policy, r["metric"], r["value"],
                                                          r["device_key"], _stored_base(r), in_progress)
        if r["state"] == "in_progress":
            r["confidence"] = r["grade"] = None
    return rows


def awaiting(signals: list[dict]) -> list[str]:
    """Core markers whose owner value has not arrived for the day: no row, or
    only a stand-in device's value (fix program A5; Codex A point 10). In
    display order, [] when all four are in. A value still in progress or
    without a baseline has arrived; it is simply not judged."""
    have = {s["metric"] for s in signals
            if s.get("value") is not None and not (s.get("fallback") or s.get("state") == "fallback")}
    return [m for m in CORE_MARKERS if m not in have]


def verdict(signals: list[dict], is_today: bool = True) -> str:
    # Recovery is the verdict's anchor: while Whoop's recovery for the night
    # has not arrived, say so instead of judging what happens to be present
    # (A5, audit T6: "Mostly steady" with no recovery data). One predicate for
    # the verdict, the template and the model gate (Codex A point 8); the
    # waiting sentence only for the reporting today (point 9).
    if "recovery_score" in awaiting(signals):
        return WAITING_FOR_WHOOP if is_today else MISSING_WHOOP
    # Only judged rows count: a value still in progress, a stand-in device's
    # value and a value with no baseline are facts, never evidence (A4, A6).
    core = [s for s in signals if s["metric"] in CORE_MARKERS and s["state"] in JUDGED]
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
