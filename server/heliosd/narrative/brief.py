"""Morning brief generation: deterministic signals in, validated narrative out."""

from __future__ import annotations

import json
import uuid
from datetime import date

from heliosd.narrative import templates
from heliosd.narrative.lmstudio import LMStudio, NARRATIVE_SCHEMA, SYSTEM_GUARDRAILS
from heliosd.narrative.validator import validate_text
from heliosd.signals.markers import signals_for, verdict as make_verdict
from heliosd.store import db


def generate_brief(conn, lm: LMStudio | None, day: date, owner_name: str,
                   temperature: float = 0.2) -> dict:
    signals = signals_for(conn, day)
    v = make_verdict(signals)
    flags = signals[0]["context_flags"] if signals else []
    actions = templates.rule_based_actions(signals, flags)
    payload = {"date": str(day), "verdict": v, "signals": [
        {k: s[k] for k in ("metric", "state", "value", "unit", "baseline_median",
                           "delta_pct", "device_key", "grade", "why")}
        for s in signals], "context_flags": flags, "rule_actions": actions}

    narrative, model_used, validated = None, "template", False
    if lm and lm.available():
        prompt = (
            "Write the morning brief JSON for this data. narrative: 3 to 6 sentences, "
            "plain language, cite the device for each number you use. actions: rephrase "
            "the provided rule_actions faithfully, do not invent new ones. flags: copy "
            "context_flags.\n\nDATA:\n" + json.dumps(payload, default=str))
        for attempt, temp in enumerate((temperature, 0.0, 0.0)):
            try:
                out = lm.structured(
                    [{"role": "system", "content": SYSTEM_GUARDRAILS},
                     {"role": "user", "content": prompt}],
                    NARRATIVE_SCHEMA, temperature=temp,
                    model=lm.primary if attempt < 2 else lm.fallback)
                errors = validate_text(out.get("narrative", ""), payload)
                for a in out.get("actions", []):
                    errors += validate_text(a.get("text", ""), payload)
                if not errors:
                    narrative = out["narrative"]
                    actions = out["actions"] or actions
                    model_used, validated = (lm.primary if attempt < 2 else lm.fallback), True
                    break
                prompt += f"\n\nVALIDATION ERRORS to fix: {errors}"
            except Exception:
                break

    if narrative is None:
        narrative = templates.fallback_narrative(day, v, signals)

    db.execute(conn, "INSERT OR REPLACE INTO narratives (date, narrative, model, validated) VALUES (?, ?, ?, ?)",
               [day, narrative, model_used, validated])
    for a in actions:
        db.execute(conn, """INSERT OR IGNORE INTO actions (action_id, date, text, category, created_by)
                            VALUES (?, ?, ?, ?, ?)""",
                   [f"{day}:{uuid.uuid4().hex[:8]}", day, a["text"], a.get("category", "general"),
                    "llm" if validated else "engine"])
    return {"date": str(day), "greeting": _greeting(owner_name), "verdict": v,
            "narrative": narrative, "signals": signals, "actions": actions,
            "context_flags": flags, "model": model_used, "validated": validated}


def _greeting(name: str) -> str:
    from datetime import datetime
    h = datetime.now().hour
    part = "morning" if h < 12 else ("afternoon" if h < 17 else "evening")
    return f"Good {part}, {name}."
