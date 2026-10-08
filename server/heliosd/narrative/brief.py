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

Generations (checkpoint A point 26, checkpoint B point 8): every recompute of a
date bumps derived_generation and deletes the cached narrative. This module
reads the generation BEFORE the signals, serves a cached narrative only when
its stored generation is the current one, and publishes narrative plus actions
in ONE transaction that re-reads the generation, so text written against inputs
that moved is never cached and never served.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime

from heliosd.narrative import templates
from heliosd.narrative.lmstudio import LMStudio, NARRATIVE_SCHEMA, SYSTEM_GUARDRAILS
from heliosd.narrative.validator import validate_text
from heliosd.signals.markers import signals_for, verdict as make_verdict
from heliosd.store import db
from heliosd.signals.recompute import generation_of


def generate_brief(conn, lm: LMStudio | None, day: date, owner_name: str,
                   temperature: float = 0.2, force: bool = False,
                   allow_llm: bool = True, policy=None) -> dict:
    # The generation first: a recompute between this read and the signals read
    # moves the generation, and the publish check below then refuses the text.
    gen_at_start = generation_of(conn, day)
    # The policy (when the caller has one) labels fallback devices on every row (A6).
    signals = signals_for(conn, day, policy)
    v = make_verdict(signals)
    flags = signals[0]["context_flags"] if signals else []
    rule_actions = templates.rule_based_actions(signals, flags)

    stored = None
    if not force:
        rows = db.fetchdicts(
            conn, "SELECT narrative, model, validated, generation FROM narratives WHERE date = ?", [day])
        # A cached narrative written against another generation is stale and is
        # never served, whatever its validation flag.
        if rows and int(rows[0]["generation"] or 0) == gen_at_start:
            stored = rows[0]

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
            if not _publish(conn, day, narrative, "template", False, gen_at_start, rule_actions):
                status = "generating"      # inputs moved under us: nothing cached, the next read retries
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
        row["fallback"] = bool(s.get("fallback"))
        # Durations in hours also get an hours-and-minutes rendering. The
        # validator only allows numbers present in this payload, so "7 hours
        # 13 minutes" is only speakable if we compute it here as data.
        if row.get("unit") == "h":
            row["value_hm"] = templates.hours_to_hm(row["value"])
            # A night with no baseline yet (insufficient) has no median to render
            # (Codex A point 15: hours_to_hm(None) raised before the model loop).
            if row.get("baseline_median") is not None:
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
            "A row with fallback true comes from a stand-in device while the usual one has "
            "no value: cite it as standing in and never compare it to the median or call it "
            "high or low. "
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
                paired = pair_llm_actions(rule_actions, out.get("actions") or [])
                for a in paired:
                    errors += validate_text(a.get("text", ""), payload)
                if not errors:
                    narrative = out["narrative"]
                    actions = paired
                    model_used, validated = (lm.primary if attempt < 2 else lm.fallback), True
                    break
                prompt += f"\n\nVALIDATION ERRORS to fix: {errors}"
            except Exception:
                break

    if narrative is None:
        narrative = templates.fallback_narrative(day, v, signals)

    if _publish(conn, day, narrative, model_used, validated, gen_at_start, actions):
        status = "ready" if validated else "template"
    else:
        # Inputs changed under us: publish nothing; the next read regenerates.
        status = "generating"
    return _result(day, owner_name, v, narrative, signals,
                   _read_actions(conn, day, rule_actions), flags,
                   model_used, validated, status)


def _publish(conn, day: date, narrative: str, model: str, validated: bool,
             generation: int, actions: list[dict]) -> bool:
    """Cache the narrative and replace today's still-suggested actions in one
    transaction, only if the date's generation is still the one the inputs
    were read under. Returns False (nothing written) otherwise."""
    with db.transaction(conn) as c:
        cur = c.execute("SELECT generation FROM derived_generation WHERE date = ?", [day]).fetchone()
        if int(cur[0] if cur else 0) != generation:
            return False
        c.execute("INSERT OR REPLACE INTO narratives (date, narrative, model, validated, generation) "
                  "VALUES (?, ?, ?, ?, ?)", [day, narrative, model, validated, generation])
        _persist_actions(c, day, actions, validated)
    return True


# Fix program A2 (audit T3, 2026-10-08). Action ids used to be positional
# (<date>:<i>) and the persist step ran INSERT OR REPLACE over rows it had not
# deleted (the resolved ones): DuckDB kept the status and replaced the text, so
# an adopted or dismissed status moved onto whatever rule sat at that position
# after the next regeneration. Ids are now <date>:<category>:<rule key>, a
# resolved row is never rewritten, and the model may reword but never
# re-identify an action.
_POSITIONAL_ID = re.compile(r"^\d{4}-\d{2}-\d{2}:\d+$")


def action_key(a: dict) -> str:
    """The rule key, or a deterministic slug of the text for an action that has
    none (never the position)."""
    key = str(a.get("key") or "").strip()
    if key:
        return key
    return re.sub(r"[^a-z0-9]+", "_", str(a.get("text", "")).lower()).strip("_")[:40] or "action"


def action_id(day: date, a: dict) -> str:
    return f"{day}:{a.get('category') or 'general'}:{action_key(a)}"


def pair_llm_actions(rules: list[dict], llm: list[dict]) -> list[dict]:
    """The model's wording for the rule actions, by position, taken only when
    the rule categories are pairwise distinct, the model returned exactly as
    many actions as the rules and every category matches in order; otherwise
    the rule texts stay. Keys and categories always come from the rules, so a
    reordered, extra or dropped model action can never change which action a
    status belongs to (Codex A point 7: two sleep rules share a category, and
    a swapped pair would pass a position-and-category check)."""
    out = [dict(a) for a in rules]
    cats = [str(a.get("category") or "") for a in rules]
    if len(llm) != len(rules) or len(set(cats)) != len(cats):
        return out
    texts = []
    for r, m in zip(rules, llm):
        text = str((m or {}).get("text") or "").strip()
        if not text or str((m or {}).get("category") or "") != str(r.get("category") or ""):
            return out
        texts.append(text)
    for a, text in zip(out, texts):
        a["text"] = text
    return out


def _match_legacy(row: dict, fresh: dict[str, dict]) -> str | None:
    """The fresh id a resolved positional row belongs to: the fresh action with
    exactly the same text, or None. A category match was rejected at Codex A
    (point 6): one old and one new action in a category does not make them the
    same rule (recovery_red versus recovery_green, sleep_short versus
    late_night), and a resolved row with model wording keeps its positional id
    until it ages out of the list rather than taking a guessed identity."""
    by_text = [aid for aid, a in fresh.items() if a["text"] == row["text"]]
    return by_text[0] if len(by_text) == 1 else None


def _persist_actions(c, day: date, actions: list[dict], validated: bool) -> None:
    """Upsert today's actions under stable ids. A row whose status is not
    'suggested' is never rewritten (text included: the owner keeps seeing the
    sentence they resolved) and never deleted; a suggested row takes the fresh
    text; suggested rows whose rule no longer fires are deleted; a positional
    row from before this rule is re-filed under the stable id when its text is
    exactly a current rule's text (its status, author and time kept), and
    otherwise stays as it is. Idempotent: positional ids are recognizable, so
    a second pass finds nothing left to migrate. No key is deleted and
    re-inserted inside one transaction (DuckDB checks unique constraints
    eagerly). `c` is the raw connection inside db.transaction."""
    created_by = "llm" if validated else "engine"
    fresh: dict[str, dict] = {}
    for a in actions:
        fresh[action_id(day, a)] = {"text": a["text"], "category": a.get("category") or "general", "key": action_key(a)}
    existing = {r[0]: {"text": r[1], "category": r[2], "status": r[3], "created_by": r[4], "created_at": r[5]}
                for r in c.execute("SELECT action_id, text, category, status, created_by, created_at "
                                   "FROM actions WHERE date = ?", [day]).fetchall()}
    # 1. Positional rows from before the stable ids.
    legacy = {aid: r for aid, r in existing.items() if _POSITIONAL_ID.match(aid)}
    for aid, row in legacy.items():
        if row["status"] == "suggested":
            c.execute("DELETE FROM actions WHERE action_id = ?", [aid])
            existing.pop(aid)
            continue
        target = _match_legacy(row, fresh)
        if target is None:
            continue                                  # stays as the record of what was resolved
        cur = existing.get(target)
        if cur is not None and cur["status"] != "suggested":
            continue                                  # two resolutions for one action: keep both rows
        if cur is not None:
            c.execute("UPDATE actions SET text = ?, status = ?, created_by = ?, created_at = ? WHERE action_id = ?",
                      [row["text"], row["status"], row["created_by"], row["created_at"], target])
        else:
            c.execute("INSERT INTO actions (action_id, date, text, category, status, created_by, created_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)",
                      [target, day, row["text"], fresh[target]["category"], row["status"], row["created_by"], row["created_at"]])
        c.execute("DELETE FROM actions WHERE action_id = ?", [aid])
        existing.pop(aid)
        existing[target] = dict(row, category=fresh[target]["category"])
    # 2. The fresh set: insert what is new, reword what is still suggested, leave the rest alone.
    for aid, a in fresh.items():
        cur = existing.get(aid)
        if cur is None:
            c.execute("INSERT INTO actions (action_id, date, text, category, created_by) VALUES (?, ?, ?, ?, ?)",
                      [aid, day, a["text"], a["category"], created_by])
        elif cur["status"] == "suggested" and (cur["text"], cur["category"], cur["created_by"]) != (a["text"], a["category"], created_by):
            c.execute("UPDATE actions SET text = ?, category = ?, created_by = ? WHERE action_id = ?",
                      [a["text"], a["category"], created_by, aid])
    # 3. Suggestions whose rule no longer fires.
    for aid, cur in existing.items():
        if aid not in fresh and cur["status"] == "suggested":
            c.execute("DELETE FROM actions WHERE action_id = ?", [aid])


def _read_actions(conn, day: date, fallback: list[dict]) -> list[dict]:
    """Today's stored actions in the rule order (the ids sort alphabetically by
    category, which is not the order the rules fire in), then any resolved row
    outside the current rules by its creation time."""
    acts = db.fetchdicts(conn, """
        SELECT action_id, text, category, status, created_at FROM actions
        WHERE date = ? ORDER BY created_at, action_id""", [day])
    if not acts:
        return fallback
    order = {action_id(day, a): i for i, a in enumerate(fallback)}
    acts.sort(key=lambda r: (order.get(r["action_id"], len(order)), str(r["created_at"]), r["action_id"]))
    for r in acts:
        r.pop("created_at", None)
    return acts


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
