"""Deterministic fallbacks: the morning brief must never fail to render,
and suggested actions are rule-based first, LLM-phrased second."""

from __future__ import annotations

from datetime import date


_DEVICE_NAMES = {"whoop": "Whoop", "apple_watch_ultra": "Apple Watch Ultra"}

# Metrics that only earn a sentence when off baseline; mirrors the LLM prompt.
_EXCEPTION_METRICS = ("respiratory_rate", "spo2", "wrist_temp", "strain", "hrv_sdnn")


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


def fallback_narrative(day: date, verdict: str, signals: list[dict]) -> str:
    """Instant, deterministic narrative in the same shape the model writes:
    verdict, recovery cluster, sleep, steps, then flagged-only extras."""
    by = {s["metric"]: s for s in signals
          if s["state"] != "insufficient" and s["value"] is not None}
    parts = [verdict]

    bits = []
    rec = by.get("recovery_score")
    if rec:
        bits.append(f"recovery is {_n(rec['value'])}% on {device_name(rec['device_key'])}")
    hrv = by.get("hrv_rmssd")
    if hrv:
        bits.append(f"HRV is {_n(hrv['value'])} ms ({hrv['why']})")
    rhr = by.get("resting_hr")
    if rhr:
        bits.append(f"resting heart rate is {_n(rhr['value'])} bpm "
                    f"on {device_name(rhr['device_key'])}")
    if bits:
        s = "; ".join(bits)
        parts.append(s[0].upper() + s[1:] + ".")

    sd = by.get("sleep_duration")
    if sd:
        base = (f", against a median of {hours_to_hm(sd['baseline_median'])}"
                if sd.get("baseline_median") is not None else "")
        parts.append(f"You slept {hours_to_hm(sd['value'])} "
                     f"on {device_name(sd['device_key'])}{base}.")

    st = by.get("steps")
    if st:
        base = (f", median {int(st['baseline_median'])}"
                if st.get("baseline_median") is not None else "")
        parts.append(f"Steps: {int(st['value'])} on {device_name(st['device_key'])}{base}.")

    for m in _EXCEPTION_METRICS:
        s = by.get(m)
        if s and s["state"] == "flag":
            parts.append(f"Worth watching: {m.replace('_', ' ')} is "
                         f"{_n(s['value'])} {s['unit'] or ''} ({s['why']}, "
                         f"{device_name(s['device_key'])}).")

    return " ".join(parts)


def rule_based_actions(signals: list[dict], flags: list[str]) -> list[dict]:
    """Up to 3 concrete actions from deterministic rules; the LLM may rephrase
    but never invent. Each has a category the PWA can deep-link."""
    by = {s["metric"]: s for s in signals}
    out: list[dict] = []

    rec = by.get("recovery_score")
    if rec and rec["state"] == "flag":
        out.append({"text": "Recovery is in the red. Keep strain low today: mobility or an easy walk only.",
                    "category": "training"})
    elif rec and rec["state"] == "favorable":
        out.append({"text": "Recovery is green. Good day for your harder session if one is planned.",
                    "category": "training"})

    sd = by.get("sleep_duration")
    if sd and sd["state"] in ("flag", "neutral") and sd["value"] is not None and sd["value"] < 7:
        out.append({"text": "Sleep ran short. Set a wind-down alert 45 minutes before your usual bedtime tonight.",
                    "category": "sleep"})
    if "late_night" in flags:
        out.append({"text": "Late night detected. Screens off and Sleep Focus on by 23:30 tonight.",
                    "category": "sleep"})
    if "heat" in flags:
        out.append({"text": "Heat season: front-load water before noon and keep outdoor efforts early.",
                    "category": "hydration"})
    if "travel_or_shifted_schedule" in flags:
        out.append({"text": "Schedule shift detected. Anchor tomorrow with morning daylight and a fixed wake time.",
                    "category": "circadian"})

    hrv = by.get("hrv_rmssd")
    if hrv and hrv["state"] == "flag" and not any(a["category"] == "training" for a in out):
        out.append({"text": "HRV is well below baseline. Trade intensity for Zone 2 or rest today.",
                    "category": "training"})

    steps = by.get("steps")
    if steps and steps["value"] is not None and steps["value"] < 4000 and len(out) < 3:
        out.append({"text": "Steps are behind. Block a 20-minute walk after your next call.",
                    "category": "movement"})

    if not out:
        out.append({"text": "All signals steady. Keep the routine that got you here.",
                    "category": "general"})
    return out[:3]
