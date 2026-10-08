"""Deterministic fallbacks: the morning brief must never fail to render,
and suggested actions are rule-based first, LLM-phrased second."""

from __future__ import annotations

from datetime import date


_DEVICE_NAMES = {"whoop": "Whoop", "apple_watch_ultra": "Apple Watch Ultra"}

# Metrics that only earn a sentence when off baseline; mirrors the LLM prompt.
_EXCEPTION_METRICS = ("respiratory_rate", "spo2", "wrist_temp", "strain", "hrv_sdnn")
# States that carry a judgement against the baseline (signals.markers.JUDGED).
_JUDGED = ("favorable", "neutral", "flag")
_CORE = ("recovery_score", "hrv_rmssd", "resting_hr", "sleep_duration")


def device_name(key: str | None) -> str:
    return _DEVICE_NAMES.get(key or "", (key or "").replace("_", " ").title())


def hours_to_hm(x: float) -> str:
    """7.21 -> '7 hours 13 minutes'; 8.0 -> '8 hours'."""
    h = int(x)
    m = int(round((x - h) * 60))
    if m == 60:
        h, m = h + 1, 0
    return f"{h} hours {m} minutes" if m else f"{h} hours"


def _n(x) -> str:
    """72.0 -> '72'; 41.216 -> '41.22'."""
    return f"{round(float(x), 2):g}"


def _so_far(s: dict) -> bool:
    """A running total of the reporting today (owner decision D7)."""
    return s.get("state") == "in_progress"


def _is_fallback(s: dict) -> bool:
    """A value from a device other than the metric's owner (A6)."""
    return bool(s.get("fallback")) or s.get("state") == "fallback"


def _stand_in(s: dict) -> str:
    """', standing in for Whoop (not compared to your baseline)' for a fallback
    row, '' otherwise."""
    if not _is_fallback(s):
        return ""
    owner = s.get("owner_device")
    who = f" for {device_name(owner)}" if owner else ""
    return f", standing in{who} (not compared to your baseline)"


def fallback_narrative(day: date, verdict: str, signals: list[dict]) -> str:
    """Instant, deterministic narrative in the same shape the model writes:
    verdict, recovery cluster, sleep, steps, then flagged-only extras."""
    by = {s["metric"]: s for s in signals
          if s["state"] != "insufficient" and s["value"] is not None}
    parts = [verdict]

    bits = []
    rec = by.get("recovery_score")
    if rec:
        bits.append(f"recovery is {_n(rec['value'])}% on {device_name(rec['device_key'])}{_stand_in(rec)}")
    hrv = by.get("hrv_rmssd")
    if hrv:
        bits.append(f"HRV is {_n(hrv['value'])} ms on {device_name(hrv['device_key'])}"
                    + (_stand_in(hrv) if _is_fallback(hrv) else f" ({hrv['why']})"))
    rhr = by.get("resting_hr")
    if rhr:
        so_far = " so far today" if _so_far(rhr) else ""
        bits.append(f"resting heart rate{so_far} is {_n(rhr['value'])} bpm "
                    f"on {device_name(rhr['device_key'])}{_stand_in(rhr)}")
    if bits:
        s = "; ".join(bits)
        parts.append(s[0].upper() + s[1:] + ".")
    # A5: a missing recovery is the verdict sentence itself ("Waiting for
    # Whoop's recovery for last night."); HRV missing on its own is said here.
    present = {s["metric"] for s in signals if s.get("value") is not None}
    if "recovery_score" in present and "hrv_rmssd" not in present:
        parts.append("Last night's Whoop HRV is not in yet.")

    sd = by.get("sleep_duration")
    if sd:
        base = (f", against a median of {hours_to_hm(sd['baseline_median'])}"
                if sd.get("baseline_median") is not None and not _is_fallback(sd) else "")
        parts.append(f"You slept {hours_to_hm(sd['value'])} "
                     f"on {device_name(sd['device_key'])}{_stand_in(sd)}{base}.")

    st = by.get("steps")
    if st and _so_far(st):
        # D7: a running total is a fact for a day in progress, never compared.
        parts.append(f"Steps so far today: {int(st['value']):,} on "
                     f"{device_name(st['device_key'])}{_stand_in(st)}.")
    elif st:
        base = (f", median {int(round(st['baseline_median'])):,}"
                if st.get("baseline_median") is not None and not _is_fallback(st) else "")
        parts.append(f"Steps: {int(st['value']):,} on {device_name(st['device_key'])}{_stand_in(st)}{base}.")

    for m in _EXCEPTION_METRICS:
        s = by.get(m)
        if s and s["state"] == "flag":
            parts.append(f"Worth watching: {m.replace('_', ' ')} is "
                         f"{_n(s['value'])} {s['unit'] or ''} ({s['why']}, "
                         f"{device_name(s['device_key'])}).")

    return " ".join(parts)


def rule_based_actions(signals: list[dict], flags: list[str]) -> list[dict]:
    """Up to 3 concrete actions from deterministic rules; the LLM may rephrase
    but never invent. Each has a category the PWA can deep-link and a stable
    `key` naming the rule (fix program A2): the stored action id is built from
    the date, the category and this key, never from the wording or the
    position, so an adopted or dismissed status stays on the action the owner
    resolved when the list is regenerated in another order."""
    by = {s["metric"]: s for s in signals}
    out: list[dict] = []

    rec = by.get("recovery_score")
    if rec and rec["state"] == "flag":
        out.append({"text": "Recovery is in the red. Keep strain low today: mobility or an easy walk only.",
                    "category": "training", "key": "recovery_red"})
    elif rec and rec["state"] == "favorable":
        out.append({"text": "Recovery is green. Good day for your harder session if one is planned.",
                    "category": "training", "key": "recovery_green"})

    sd = by.get("sleep_duration")
    if sd and sd["state"] in ("flag", "neutral") and sd["value"] is not None and sd["value"] < 7:
        out.append({"text": "Sleep ran short. Set a wind-down alert 45 minutes before your usual bedtime tonight.",
                    "category": "sleep", "key": "sleep_short"})
    if "late_night" in flags:
        out.append({"text": "Late night detected. Screens off and Sleep Focus on by 23:30 tonight.",
                    "category": "sleep", "key": "late_night"})
    if "heat" in flags:
        out.append({"text": "Heat season: front-load water before noon and keep outdoor efforts early.",
                    "category": "hydration", "key": "heat"})
    if "travel_or_shifted_schedule" in flags:
        out.append({"text": "Schedule shift detected. Anchor tomorrow with morning daylight and a fixed wake time.",
                    "category": "circadian", "key": "schedule_shift"})

    hrv = by.get("hrv_rmssd")
    if hrv and hrv["state"] == "flag" and not any(a["category"] == "training" for a in out):
        out.append({"text": "HRV is well below baseline. Trade intensity for Zone 2 or rest today.",
                    "category": "training", "key": "hrv_low"})

    steps = by.get("steps")
    # Only a judged (closed) day of steps can be "behind" (D7: 114 steps at
    # 06:40 is not a shortfall).
    if (steps and steps["state"] in _JUDGED and steps["value"] is not None
            and steps["value"] < 4000 and len(out) < 3):
        out.append({"text": "Steps are behind. Block a 20-minute walk after your next call.",
                    "category": "movement", "key": "steps_behind"})

    if not out:
        # "Steady" needs judged evidence (Codex A point 11): with nothing judged
        # yet (the night not in, every total still in progress) say so instead.
        if any(s["metric"] in _CORE and s["state"] in _JUDGED for s in signals):
            out.append({"text": "All signals steady. Keep the routine that got you here.",
                        "category": "general", "key": "steady"})
        else:
            out.append({"text": "Nothing to act on yet. Check back once last night's data is in.",
                        "category": "general", "key": "nothing_yet"})
    return out[:3]
