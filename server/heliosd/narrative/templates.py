"""Deterministic fallbacks: the morning brief must never fail to render,
and suggested actions are rule-based first, LLM-phrased second."""

from __future__ import annotations

from datetime import date


def fallback_narrative(day: date, verdict: str, signals: list[dict]) -> str:
    parts = [verdict]
    for s in signals[:4]:
        if s["state"] == "insufficient" or s["value"] is None:
            continue
        parts.append(f"{s['metric'].replace('_', ' ').title()}: {s['value']:g} {s['unit'] or ''} "
                     f"({s['why']}, {s['device_key']}, grade {s['grade']}).")
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
