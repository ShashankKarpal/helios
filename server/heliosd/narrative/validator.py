"""Host-side validation: the model may only speak numbers that exist in its
input, and may never drift into diagnosis or dosing."""

from __future__ import annotations

import json
import re

BLOCKLIST = re.compile(
    r"\b(diagnos\w*|prescri\w*|dosage|dose of|mg of|take \d+ ?mg|disease|disorder|"
    r"syndrome|you (have|suffer)|medical emergency)\b", re.IGNORECASE)

_NUM = re.compile(r"\d+(?:[.,]\d+)?")

# How the narrative names each metric. A metric the brief holds back from the
# model (payload "not_for_narrative": a running total of the reporting today or
# a stand-in device's value, fix program A4 and A6) must not be mentioned at
# all: any mention is rejected, not only a judgement, so "Steps are
# disappointing" fails like "steps are below your median" (Codex A point 12).
# Resting heart rate and heart rate variability are not all-day heart rate,
# and a walk is not a step count (point 13). Unknown metrics: their key in words.
MENTION = {
    "steps": r"\bsteps?\b|\bstep count\b",
    "active_energy": r"\bactive (energy|calories)\b|\bcalories\b|\bkcal\b|\bmove ring\b",
    "basal_energy": r"\bbasal\b|\bresting (energy|calories)\b",
    "dietary_energy": r"\bdietary\b|\bcalories (eaten|consumed|logged)\b|\bfood (intake|log)\b",
    "heart_rate": r"(?<!resting )\bheart rate\b(?! variability)|\baverage HR\b|\bpulse\b",
    "resting_hr": r"\bresting (heart rate|HR|pulse)\b|\bRHR\b",
    "hrv_sdnn": r"\bSDNN\b",
    "hrv_rmssd": r"\bHRV\b|\bheart rate variability\b|\brMSSD\b",
    "recovery_score": r"\brecover\w*|\bready\b|\breadiness\b|\bprimed\b",
    "sleep_duration": r"\bslept\b|\basleep\b|\bhours of sleep\b|\bsleep (duration|time|total|ran|was)\b",
    "spo2": r"\bSpO2\b|\bblood oxygen\b|\boxygen saturation\b",
    "respiratory_rate": r"\brespiratory\b|\bbreathing rate\b|\bbreaths per minute\b",
    "wrist_temp": r"\bwrist temp\w*|\bskin temp\w*",
    "body_temp": r"\bbody temp\w*|\bskin temp\w*",
    "glucose": r"\bglucose\b|\bblood sugar\b",
    "strain": r"\bstrain\b",
}


def held_back(payload) -> list[str]:
    """Metrics the narrative must not mention. Only the brief's dict payload
    carries them; chat validates against a list of tool results and keeps
    the number and vocabulary checks alone (Codex A point 17)."""
    if not isinstance(payload, dict):
        return []
    return list(payload.get("not_for_narrative") or [])


def mention_pattern(metric: str) -> str:
    return MENTION.get(metric) or r"\b" + re.escape(metric.replace("_", " ")) + r"\b"


def _variants(x: float) -> set[str]:
    out = {f"{x:g}", f"{x:.0f}", f"{x:.1f}", f"{x:.2f}"}
    if x >= 1000:
        out.add(f"{x:,.0f}")
    return out


def allowed_numbers(payload) -> set[str]:
    """Every numeric literal reachable in the payload, in common formats."""
    found: set[str] = set()

    def walk(v):
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            found.update(_variants(float(v)))
        elif isinstance(v, str):
            for m in _NUM.finditer(v):
                try:
                    found.update(_variants(float(m.group().replace(",", ""))))
                except ValueError:
                    pass
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    walk(json.loads(json.dumps(payload, default=str)))
    return found


def validate_text(text: str, payload) -> list[str]:
    """Empty list = valid."""
    errors: list[str] = []
    allowed = allowed_numbers(payload)
    for m in _NUM.finditer(text):
        token = m.group().replace(",", "")
        # tolerate times like 07:30 and small counting words (1, 2, 3)
        if float(token) <= 3 or text[max(0, m.start() - 1):m.start()] == ":" or text[m.end():m.end() + 1] == ":":
            continue
        try:
            if not (_variants(float(token)) & allowed):
                errors.append(f"number {m.group()} not present in input data")
        except ValueError:
            pass
    if BLOCKLIST.search(text):
        errors.append(f"blocked vocabulary: {BLOCKLIST.search(text).group()}")
    for metric in held_back(payload):
        m = re.search(mention_pattern(metric), text, re.IGNORECASE)
        if m:
            errors.append(f"mentions {metric} ('{m.group()}'), which is held back from the narrative")
    return errors
