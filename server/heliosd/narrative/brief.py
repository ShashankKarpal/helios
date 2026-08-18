"""Morning brief generation: deterministic signals in, validated narrative out.

Two paths share this module:
  - Fast path (allow_llm=False): what /api/today calls. Returns the deterministic
    numbers plus a validated narrative if one is already cached, otherwise an
    instant template narrative. It NEVER calls the model, so the Today screen
    always renders in well under a second.
  - Slow path (allow_llm=True, force=True): what the background task calls. Runs
    the local model to write and validate a richer narrative, then caches it so
    the next fast-path read serves it. This is the only path that can take many
    seconds, and it never blocks a request.
"""

from __future__ import annotations

import json
from datetime import date, datetime

from heliosd.narrative import templates
from heliosd.narrative.lmstudio import LMStudio, NARRATIVE_SCHEMA, SYSTEM_GUARDRAILS
from heliosd.narrative.validator import validate_text
from heliosd.signals.markers import signals_for, verdict as make_verdict
from heliosd.store import db


def generate_brief(conn, lm: LMStudio | None, day: date, owner_name: str,
                   temperature: float = 0.2, force: bool = False,
                   allow_llm: bool = True) -> dict:
    signals = signals_for(conn, day)
    v = make_verdict(signals)
    flags = signals[0]["context_flags"] if signals else []
    rule_actions = templates.rule_based_actions(signals, flags)

    stored = None
    if not force:
        rows = db.fetchdicts(
            conn, "SELECT narrative, model, validated FROM narratives WHERE date = ?", [day])
        stored = rows[0] if rows else None

    llm_ready = bool(lm and lm.available())

    # Reuse a cached narrative when we cannot, or need not, produce a better one:
    # a validated local-AI narrative is always reused; an unvalidated template is
    # reused only when the model is unavailable to upgrade it. When the model IS
    # available, an unvalidated template falls through so the fast path can report
    # "generating" and the background task can replace it.
    if stored and (stored["validated"] or not llm_ready):
        status = "ready" if stored["validated"] else "template"
        return _result(day, owner_name, v, stored["narrative"], signals,
                       _read_actions(conn, day, rule_actions), flags,
                       stored["model"], stored["validated"], status)

    # Fast path: the caller forbids the model (used by /api/today). Show the
    # cached template if present, otherwise write an instant one, and report
    # whether a richer one is on its way ("generating") or final ("template").
    if not allow_llm:
        status = "generating" if llm_ready else "template"
        if not stored:
            narrative = templates.fallback_narrative(day, v, signals)
            db.execute(conn, "INSERT OR REPLACE INTO narratives (date, narrative, model, validated) "
                             "VALUES (?, ?, ?, ?)", [day, narrative, "template", False])
            _persist_actions(conn, day, rule_actions, False)
            model = "template"
        else:
            narrative, model = stored["narrative"], stored["model"]
        return _result(day, owner_name, v, narrative, signals,
                       _read_actions(conn, day, rule_actions), flags,
                       model, False, status)

    # Slow path (background task): full validated model generation.
    actions = rule_actions
    sig_rows = []
    for s in signals:
        row = {k: s[k] for k in ("metric", "state", "value", "unit", "baseline_median",
                                 "delta_pct", "device_key", "grade", "why")}
        # Durations in hours also get an hours-and-minutes rendering. The
        # validator only allows numbers present in this payload, so "7 hours
        # 13 minutes" is only speakable if we compute it here as data.
        if row.get("unit") == "h":
            row["value_hm"] = templates.hours_to_hm(row["value"])
            row["baseline_hm"] = templates.hours_to_hm(row["baseline_median"])
        sig_rows.append(row)
    payload = {"date": str(day), "verdict": v, "signals": sig_rows,
               "context_flags": flags, "rule_actions": actions}

    narrative, model_used, validated = None, "template", False
    if lm and lm.available():
        prompt = (
            "Write the morning brief JSON for this data. narrative: 4 to 6 sentences, "
            "70 to 85 words total (a 25 second read), in plain, warm language that reads "
            "like a knowledgeable friend's summary, not a data dump. Sentence 1: the "
            "overall verdict in plain words, using the verdict field, with no numbers. "
            "Always cover, grouped as one story each: the recovery cluster "
            "(recovery_score, hrv_rmssd and resting_hr together), sleep_duration, and "
            "steps. Mention respiratory_rate, spo2, wrist_temp, strain or hrv_sdnn ONLY "
            "if their state is flag; if favorable or neutral, leave them out entirely. "
            "Cite the device for each number you use. Write sleep durations exactly as "
            "given in the value_hm field (hours and minutes), never as a decimal. Number "
            "style: at most 2 decimals, never a trailing .0, write bpm not count/min, "
            "write percentages like 36%. Final sentence: the single most useful thing to "
            "do today, drawn from rule_actions. actions: rephrase the provided "
            "rule_actions faithfully, do not invent new ones. flags: copy context_flags."
            "\n\nDATA:\n" + json.dumps(payload, default=str))
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

    db.execute(conn, "INSERT OR REPLACE INTO narratives (date, narrative, model, validated) "
                     "VALUES (?, ?, ?, ?)", [day, narrative, model_used, validated])
    _persist_actions(conn, day, actions, validated)
    return _result(day, owner_name, v, narrative, signals,
                   _read_actions(conn, day, rule_actions), flags,
                   model_used, validated, "ready" if validated else "template")


def _persist_actions(conn, day: date, actions: list[dict], validated: bool) -> None:
    """Replace today's still-suggested actions with the fresh set (deterministic
    ids, so no duplicates). Anything the owner already adopted or dismissed is
    left untouched."""
    db.execute(conn, "DELETE FROM actions WHERE date = ? AND status = 'suggested'", [day])
    for i, a in enumerate(actions):
        db.execute(conn, """INSERT OR REPLACE INTO actions (action_id, date, text, category, created_by)
                            VALUES (?, ?, ?, ?, ?)""",
                   [f"{day}:{i}", day, a["text"], a.get("category", "general"),
                    "llm" if validated else "engine"])


def _read_actions(conn, day: date, fallback: list[dict]) -> list[dict]:
    acts = db.fetchdicts(conn, """
        SELECT action_id, text, category, status FROM actions
        WHERE date = ? ORDER BY action_id""", [day])
    return acts or fallback


def _result(day: date, owner_name: str, v: str, narrative: str, signals: list[dict],
            actions: list[dict], flags: list[str], model: str | None,
            validated: bool, status: str) -> dict:
    return {"date": str(day), "greeting": _greeting(owner_name), "verdict": v,
            "narrative": narrative, "signals": signals, "actions": actions,
            "context_flags": flags, "model": model, "validated": validated,
            "narrative_status": status}


def _greeting(name: str) -> str:
    h = datetime.now().hour
    part = "morning" if h < 12 else ("afternoon" if h < 17 else "evening")
    return f"Good {part}, {name}."
