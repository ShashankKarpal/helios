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
import logging
import os
import re
import threading
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path

from heliosd.ingest.normalize import reporting_today
from heliosd.narrative import templates
from heliosd.narrative.glance import glance_file_for, record_glance
from heliosd.narrative.lmstudio import LMStudio, NARRATIVE_SCHEMA, SYSTEM_GUARDRAILS
from heliosd.narrative.validator import held_back as validator_held_back, mention_pattern, validate_text
from heliosd.signals.markers import awaiting as awaiting_markers, signals_for, verdict as make_verdict
from heliosd.store import db
from heliosd.signals.recompute import generation_of


# A deterministic narrative published on purpose for its generation: while
# last night's Whoop recovery is not in, or when the model's retries ran out.
# The fast path reuses it like a validated one, so no poll relaunches the model
# until a recompute moves the generation (Codex A point 14).
FINAL_TEMPLATE = "template:final"

log = logging.getLogger("heliosd")

# Rank 2 (2026-10-23): the last valid brief, kept outside the store in a small
# content-free JSON file beside it, so its age can be shown everywhere and a
# check that never opens DuckDB can read it. The narratives table is only a
# cache (every recompute of a date deletes its narrative), so it cannot say
# when a brief last existed. Valid ("complete"): published for the reporting
# today with last night's recovery in and at least one settled signal (not a
# running total of the day in progress, not a stand-in device), worded by the
# local model or by the template (the deterministic template is the required
# output; the model only rewords it). A brief published while the recovery is
# still awaited is recorded apart ("waiting"); it never counts as delivered.
STATUS_FILE_NAME = "last_valid_brief.json"
# Past this hour in the reporting zone, no valid brief for today is "overdue".
BRIEF_DUE_HOUR = 9
_STATUS_LOCK = threading.Lock()

# The recovery cluster the prompt asks the model to tell as one story.
RECOVERY_CLUSTER = ("recovery_score", "hrv_rmssd", "resting_hr")
# Mentioned only when flagged; never when held back.
FLAG_ONLY = ("respiratory_rate", "spo2", "wrist_temp", "strain", "hrv_sdnn")


def _error_kinds(errors: list[str]) -> list[str]:
    """Validation errors without the numbers in them, for the log (a health
    value never reaches a log line)."""
    kinds = set()
    for e in errors:
        if e.startswith("number "):
            kinds.add("a number not in the data")
        elif e.startswith("mentions "):
            kinds.add(e.split(" (")[0])
        elif e.startswith("blocked vocabulary"):
            kinds.add("blocked vocabulary")
        else:
            kinds.add("other")
    return sorted(kinds)


# Wave 4: how to steer clear of a held-back metric in plain words. The live
# model wrote "heart rate" on its own (all-day heart rate, held back) and used
# "strain" as an everyday word on a day whose strain was still in progress.
AVOID_HINTS = {
    "heart_rate": 'never write "heart rate" on its own ("resting heart rate" and '
                  '"heart rate variability" are fine)',
    "strain": 'never use the word "strain", not even as an everyday word (say "load" or "stress")',
    "steps": 'never use the words "step" or "steps"',
    "hrv_rmssd": 'never write "HRV", "heart rate variability" or "rMSSD"',
    "resting_hr": 'never write "resting heart rate", "resting HR" or "RHR"',
    "recovery_score": 'never write "recovery", "recovered", "ready" or "readiness"',
    "sleep_duration": 'never say how long the night was ("slept", "asleep", "hours of sleep")',
    "spo2": 'never write "SpO2", "blood oxygen" or "oxygen saturation"',
    "active_energy": 'never write "calories", "kcal", "active energy" or "move ring"',
    "respiratory_rate": 'never write "respiratory" or "breathing rate"',
}


def avoid_sentence(blocked: list[str]) -> str:
    """One prompt sentence naming what the narrative and actions must not say."""
    if not blocked:
        return ""
    parts = []
    for m in blocked:
        if m == "heart_rate" and ({"resting_hr", "hrv_rmssd"} & set(blocked)):
            parts.append('never write "heart rate" at all')
        else:
            parts.append(AVOID_HINTS.get(m, f'never mention {m.replace("_", " ")}'))
    return "Today, in the narrative and in the actions: " + "; ".join(parts) + ". "


def fix_instructions(errors: list[str]) -> str:
    """The validation errors as instructions the model can act on."""
    out = []
    for e in errors:
        if e.startswith("mentions ") and "('" in e:
            word = e.split("('", 1)[1].split("')", 1)[0]
            out.append(f'remove "{word}" and the idea it carries; say nothing about it')
        else:
            out.append(e)
    return "; ".join(dict.fromkeys(out))


def split_rule_actions(actions: list[dict], blocked: list[str]) -> tuple[list[dict], list[dict]]:
    """Wave 4 (2026-10-18). The rule actions the model may reword, and the
    ones that keep the engine's text because they name a metric the narrative
    must not mention today (a running total of the day in progress, a
    stand-in device, a value not in yet). The model never sees the second
    list, so it is never asked to say a word the validator then rejects."""
    speakable, fixed = [], []
    for a in actions:
        text = str(a.get("text") or "")
        if any(re.search(mention_pattern(m), text, re.IGNORECASE) for m in blocked):
            fixed.append(a)
        else:
            speakable.append(a)
    return speakable, fixed


def merge_actions(rules: list[dict], speakable_paired: list[dict], speakable_rules: list[dict]) -> list[dict]:
    """The rule order, with the model's paired wording for the speakable ones
    and the engine text for the rest."""
    def ident(a: dict) -> tuple:
        return (a.get("category"), a.get("key"), a.get("text"))
    by_rule = {ident(r): p for r, p in zip(speakable_rules, speakable_paired)}
    return [by_rule.get(ident(r), dict(r)) for r in rules]


def generate_brief(conn, lm: LMStudio | None, day: date, owner_name: str,
                   temperature: float = 0.2, force: bool = False,
                   allow_llm: bool = True, policy=None, today: date | None = None,
                   status_file: Path | None = None) -> dict:
    # The generation first: a recompute between this read and the signals read
    # moves the generation, and the publish check below then refuses the text.
    gen_at_start = generation_of(conn, day)
    # The reporting today the brief is read against: the caller's, else the
    # policy's clock; without a policy the brief day itself (the old assumption).
    if today is None:
        today = reporting_today(policy.zone) if policy is not None else day
    # The policy labels stand-in devices and presents every row against the
    # reporting today (A4, A6).
    signals = signals_for(conn, day, policy, today)
    # Core markers whose owner value has not arrived (A5); /api/today shows it.
    pending = awaiting_markers(signals)
    v = make_verdict(signals, is_today=(day == today))
    flags = signals[0]["context_flags"] if signals else []
    rule_actions = templates.rule_based_actions(signals, flags)

    def done(narrative: str, actions: list[dict], model: str | None,
             validated: bool, status: str) -> dict:
        out = _result(day, owner_name, v, narrative, signals, actions, flags,
                      model, validated, status)
        out["awaiting"] = pending
        return out

    def published(model: str, validated: bool, only_if_missing: bool = False) -> None:
        # Only today's brief is "the morning brief" (rank 2).
        if day == today:
            settled = sum(1 for s in signals
                          if s.get("state") not in ("in_progress", "fallback") and not s.get("fallback"))
            zone = policy.zone if policy is not None else None
            now = _utcnow()
            record_brief(status_file, day, model, bool(validated), settled, pending,
                         zone, now=now, only_if_missing=only_if_missing)
            # The phone's glance (rank 3), beside the status file, on the same clock.
            if status_file is not None:
                record_glance(glance_file_for(status_file), day, signals, v, pending,
                              brief_complete(settled, pending), zone, now=now, generation=gen_at_start)

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
    if stored and (stored["validated"] or not llm_ready or stored["model"] == FINAL_TEMPLATE):
        status = "ready" if stored["validated"] else "template"
        # A cached brief published before a restart, or while the status file
        # could not be written, is recorded now (rank 2 review).
        published(stored["model"], stored["validated"], only_if_missing=True)
        return done(stored["narrative"], _read_actions(conn, day, rule_actions),
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
            else:
                published("template", False)
            model = "template"
        else:
            narrative, model = stored["narrative"], stored["model"]
            published(model, False, only_if_missing=True)
        return done(narrative, _read_actions(conn, day, rule_actions), model, False, status)

    # Slow path (background task): full validated model generation.
    actions = rule_actions
    # Held back from the model (fix program A4 and A6): a running total of the
    # reporting today and a stand-in device's value. They stay on the screen as
    # "so far" and labelled rows; the model never sees them, and the validator
    # rejects any mention of them, so it cannot judge a partial day or a
    # stand-in (Codex A point 12: no judgement-word list to slip past).
    held_back = sorted({s["metric"] for s in signals
                        if s["state"] in ("in_progress", "fallback") or s.get("fallback")})
    sig_rows = []
    for s in signals:
        if s["metric"] in held_back:
            continue
        row = {k: s[k] for k in ("metric", "state", "value", "unit", "baseline_median",
                                 "delta_pct", "device_key", "grade", "why")}
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
    # Wave 4: the prompt asks only for what the validator allows (the old
    # prompt always asked for steps and for the rule actions in full, and on
    # a day in progress every try failed on "steps" or "strain").
    blocked = validator_held_back({"not_for_narrative": held_back, "awaiting": pending})
    speakable_rules, _fixed_rules = split_rule_actions(actions, blocked)
    payload = {"date": str(day), "verdict": v, "signals": sig_rows,
               "context_flags": flags, "rule_actions": speakable_rules,
               "not_for_narrative": held_back, "awaiting": pending}
    cluster = [m for m in RECOVERY_CLUSTER if m not in blocked]
    cover = []
    if len(cluster) > 1:
        cover.append("the recovery cluster (" + ", ".join(cluster) + " together)")
    else:
        cover += cluster
    cover += [m for m in ("sleep_duration", "steps") if m not in blocked]
    flag_only = [m for m in FLAG_ONLY if m not in blocked]
    never = avoid_sentence(blocked)

    narrative, model_used, validated = None, "template", False
    if "recovery_score" in pending:
        # A5: while last night's Whoop recovery is not in, the narrative is the
        # deterministic template, final for this generation; the model is never
        # asked to narrate a night that has not arrived (Codex A points 8, 12).
        narrative, model_used = templates.fallback_narrative(day, v, signals), FINAL_TEMPLATE
    elif lm and lm.available():
        prompt = (
            "Write the morning brief JSON for this data. narrative: 4 to 6 sentences, "
            "70 to 85 words total (a 25 second read), in plain, warm language that reads "
            "like a knowledgeable friend's summary, not a data dump. Sentence 1: the "
            "overall verdict in plain words, using the verdict field, with no numbers. "
            + ("Cover, grouped as one story each and only for metrics present in signals: "
               + ", ".join(cover) + ". " if cover else "")
            + ("Mention " + ", ".join(flag_only) + " ONLY if their state is flag; if "
               "favorable or neutral, leave them out entirely. " if flag_only else "")
            + never
            + "A row with state insufficient has no baseline yet: give its "
            "value without comparing it. The metrics in not_for_narrative and awaiting are "
            "left out on purpose (a total for a day still in progress, a stand-in device's "
            "value, or a value that has not arrived yet): never mention them, not even to "
            "say they are missing. "
            "Cite the device for each number you use. Write sleep durations exactly as "
            "given in the value_hm field (hours and minutes), never as a decimal. Number "
            "style: at most 2 decimals, never a trailing .0, write bpm not count/min, "
            "write percentages like 36%. Final sentence: the single most useful thing to "
            "do today, drawn from rule_actions (if rule_actions is empty, one short "
            "sentence, with no numbers, that matches the verdict). actions: rephrase "
            "the provided rule_actions faithfully, one for each, in the same order, do not "
            "invent new ones. flags: copy context_flags."
            "\n\nDATA:\n" + json.dumps(payload, default=str))
        last_errors: list[str] = []
        crashed = False
        tries = (temperature, 0.0, 0.0)
        for attempt, temp in enumerate(tries):
            try:
                out = lm.structured(
                    [{"role": "system", "content": SYSTEM_GUARDRAILS},
                     {"role": "user", "content": prompt}],
                    NARRATIVE_SCHEMA, temperature=temp,
                    model=lm.primary if attempt < len(tries) - 1 else lm.fallback)
                # The model rephrases the rule actions, it never adds one: keep at
                # most as many as the rules gave (Codex A point 14).
                llm_actions = (out.get("actions") or [])[:len(speakable_rules)]
                errors = validate_text(out.get("narrative", ""), payload)
                # Capped to the rule count (Codex A point 14 on the R2 design), then
                # paired: keys and categories always come from the rules (A2).
                # Wave 4: only the speakable rules are paired and validated; the
                # fixed ones keep the engine's text and are never model output.
                paired = pair_llm_actions(speakable_rules, llm_actions)
                for a in paired:
                    errors += validate_text(a.get("text", ""), payload)
                last_errors = errors
                if not errors:
                    narrative = out["narrative"]
                    actions = merge_actions(rule_actions, paired, speakable_rules)
                    model_used, validated = (lm.primary if attempt < len(tries) - 1 else lm.fallback), True
                    break
                prompt += f"\n\nVALIDATION ERRORS to fix: {errors}\nHow to fix them: {fix_instructions(errors)}."
            except Exception as e:  # noqa: BLE001 - the template stands in; say why
                log.warning("brief %s: model call failed (%s); serving the template", day, type(e).__name__)
                crashed = True
                break
        if narrative is None:
            # Retries ran out: the template is final for this generation, so the
            # fast path does not relaunch the model on every poll (Codex A point 14).
            # Wave 4: say so in the log, without the numbers (this failed silently
            # every morning while the day's steps and strain were held back).
            if last_errors and not crashed:
                log.warning("brief %s: the model's narrative failed validation on every try (%s); "
                            "serving the template", day, "; ".join(_error_kinds(last_errors)))
            model_used = FINAL_TEMPLATE

    if narrative is None:
        narrative = templates.fallback_narrative(day, v, signals)

    if _publish(conn, day, narrative, model_used, validated, gen_at_start, actions):
        status = "ready" if validated else "template"
        published(model_used, validated)
    else:
        # Inputs changed under us: publish nothing; the next read regenerates.
        status = "generating"
    return done(narrative, _read_actions(conn, day, rule_actions), model_used, validated, status)


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
        _persist_actions(c, day, actions)
    return True


def status_file_for(db_path) -> Path:
    """The last-valid-brief file of a store: beside the store file."""
    return Path(db_path).parent / STATUS_FILE_NAME


def _read_status(status_file) -> dict:
    try:
        rec = json.loads(Path(status_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def _aware(v) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _part(rec: dict, name: str) -> dict:
    """One part of the status file ("complete" or "waiting"), or {} when it
    is missing or has no zoned publish time."""
    part = rec.get(name)
    if not isinstance(part, dict) or _aware(part.get("published_at")) is None:
        return {}
    return part


def brief_complete(settled: int, awaiting: list[str] | None) -> bool:
    """A complete (valid) brief: last night's recovery in and at least one
    settled signal. One predicate for the status file and the glance."""
    return "recovery_score" not in (awaiting or []) and settled > 0


def record_brief(status_file, day: date, model: str, validated: bool, settled: int,
                 awaiting: list[str], zone=None, now: datetime | None = None,
                 only_if_missing: bool = False) -> bool:
    """Record a just-published brief for `day`. Content-free: the date, the
    first and the newest publish time of that date, local model or template,
    the count of settled signals and the names of what it still waits for.
    Complete (last night's recovery in and at least one settled signal): the
    "complete" part, the last valid brief. Otherwise the "waiting" part, which
    says since when today's brief has been waiting and never counts as
    delivered. Not recorded: a publish whose instant is not on `day` in the
    reporting zone (a rewrite that finished after midnight). only_if_missing
    (a cached brief served again) writes only when that part has nothing for
    `day` yet. Never raises: a write that fails is logged and the brief is
    served anyway."""
    if status_file is None:
        return False
    local = (now or _utcnow()).astimezone(zone or timezone.utc)
    if local.date() != day:
        return False
    complete = brief_complete(settled, awaiting)
    name = "complete" if complete else "waiting"
    stamp = local.isoformat(timespec="seconds")
    path = Path(status_file)
    try:
        with _STATUS_LOCK:
            rec = _read_status(path)
            cur = _part(rec, name)
            same_day = cur.get("date") == str(day)
            if only_if_missing and same_day:
                return False
            out = {k: _part(rec, k) for k in ("complete", "waiting") if _part(rec, k)}
            out[name] = {"date": str(day),
                         "first_published_at": cur.get("first_published_at") if same_day else stamp,
                         "published_at": stamp, "kind": "model" if validated else "template",
                         "model": model, "signals": settled, "awaiting": sorted(awaiting or [])}
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(out, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        return True
    except OSError as e:
        log.warning("brief %s: could not record it in the brief status file (%s)", day, type(e).__name__)
        return False


def brief_check(today: bool, overdue: bool, age_hours: float | None, due: str = "09:00") -> str:
    """The one line the 09:05 iPhone Shortcut reads (rank 2, channel A): "ok"
    only when today's complete brief exists; "early: ..." before the due time
    in the reporting zone with none yet (the phone stays quiet: on a trip east
    of the reporting zone 09:05 local comes first); "late: ..." after it. The
    phone alerts on anything else too, an error page included, so it never
    reads a true or false value. No publish time, nothing about what is
    awaited (the route is open)."""
    if today:
        return "ok"
    if not overdue:
        return f"early: no complete brief yet; due {due} in the reporting zone"
    last = f"; the last complete one is {round(age_hours)} h old" if age_hours is not None else ""
    return f"late: no complete brief for today{last}"


def brief_status(status_file, zone=None, now: datetime | None = None,
                 due_hour: int = BRIEF_DUE_HOUR, full: bool = True) -> dict:
    """The last valid (complete) brief as the surfaces show it: its date,
    first and newest publish time, age in hours, local model or template,
    whether it is the reporting today's, since when today's brief has been
    waiting for last night's recovery, and overdue (past due_hour in the
    reporting zone with no complete brief for today). full=False is the
    small block for the open /api/health route (no publish times, nothing
    about what is awaited). Reads only the status file, never the store."""
    now = now or _utcnow()
    local = now.astimezone(zone or timezone.utc)
    today = local.date().isoformat()
    rec = _read_status(status_file) if status_file is not None else {}
    c, w = _part(rec, "complete"), _part(rec, "waiting")
    published = _aware(c.get("published_at"))
    is_today = published is not None and c.get("date") == today
    waiting = not is_today and w.get("date") == today
    overdue = not is_today and local.hour >= due_hour
    age = round((now - published).total_seconds() / 3600, 1) if published else None
    due = f"{due_hour:02d}:00"
    small = {"today": is_today, "overdue": overdue, "age_hours": age,
             "kind": c.get("kind"), "due": due, "check": brief_check(is_today, overdue, age, due)}
    if not full:
        return small
    return {**small, "date": c.get("date"), "first_published_at": c.get("first_published_at"),
            "published_at": c.get("published_at"),
            "waiting_since": w.get("first_published_at") if waiting else None,
            "awaiting": (w.get("awaiting") or []) if waiting else []}


# ---- the morning step (rank 2, owner yes 2026-10-23): heliosd publishes the
# morning brief itself, so it exists before anyone opens the app.

def parse_hhmm(spec) -> int:
    """'08:30' (or a TOML local time 08:30) -> 510, minutes of the
    reporting-zone day. Raises ValueError."""
    if isinstance(spec, dtime):
        return spec.hour * 60 + spec.minute
    try:
        hh, _, mm = str(spec).strip().partition(":")
        h, m = int(hh), int(mm or 0)
    except (TypeError, ValueError) as e:
        raise ValueError(f"time {spec!r}: expected HH:MM") from e
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"time {spec!r}: expected HH:MM")
    return h * 60 + m


def morning_plan(now_local: datetime, window: tuple[int, int], publish_by: int, poll_s: int,
                 complete: bool, recovery_in: bool) -> tuple[str, int]:
    """What the morning step does now. Inside the window and with no complete
    brief for today: ("publish", poll_s) once last night's recovery is in, or
    from publish_by on whatever is there (a brief still waiting for Whoop is
    published and keeps being retried until the night lands); before that,
    ("wait", seconds) until the next poll or publish_by, whichever is sooner.
    Otherwise ("wait", seconds) until the next window start, capped at poll_s
    so a clock or zone change is noticed."""
    minute = now_local.hour * 60 + now_local.minute + now_local.second / 60
    start, end = window
    if not complete and start <= minute < end:
        if recovery_in or minute >= publish_by:
            return "publish", poll_s
        return "wait", max(1, min(poll_s, int(round((publish_by - minute) * 60))))
    to_start = (start - minute) if minute < start else (start + 1440 - minute)
    return "wait", max(1, min(poll_s, int(round(to_start * 60))))


def morning_state(conn, policy, status_file, now: datetime | None = None) -> tuple[bool, bool]:
    """(complete, recovery_in) for the reporting today: a complete brief is
    already recorded (the status file); last night's recovery has arrived (the
    same rule as complete, from today's signals, read only while no complete
    brief is recorded)."""
    now = now or _utcnow()
    today = now.astimezone(policy.zone).date()
    if brief_status(status_file, policy.zone, now)["today"]:
        return True, True
    recovery_in = "recovery_score" not in awaiting_markers(signals_for(conn, today, policy, today))
    return False, recovery_in


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
    a swapped pair would pass a position-and-category check). An action whose
    text is the model's carries created_by "llm"; a kept rule text carries
    none and is stored as "engine" (Wave 1 review)."""
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
        a["text"], a["created_by"] = text, "llm"
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


def _persist_actions(c, day: date, actions: list[dict]) -> None:
    """Upsert today's actions under stable ids. A row whose status is not
    'suggested' is never rewritten (text included: the owner keeps seeing the
    sentence they resolved) and never deleted; a suggested row takes the fresh
    text; suggested rows whose rule no longer fires are deleted; a positional
    row from before this rule is re-filed under the stable id when its text is
    exactly a current rule's text (its status, author and time kept), and
    otherwise stays as it is. Idempotent: positional ids are recognizable, so
    a second pass finds nothing left to migrate. No key is deleted and
    re-inserted inside one transaction (DuckDB checks unique constraints
    eagerly). created_by is per action: "llm" only for the model's own wording
    (pair_llm_actions marks it), else "engine"; a validated narrative used to
    label every action "llm", rule texts included (Wave 1 review). `c` is the
    raw connection inside db.transaction."""
    fresh: dict[str, dict] = {}
    for a in actions:
        fresh[action_id(day, a)] = {"text": a["text"], "category": a.get("category") or "general", "key": action_key(a),
                                    "created_by": a.get("created_by") or "engine"}
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
                      [aid, day, a["text"], a["category"], a["created_by"]])
        elif cur["status"] == "suggested" and (cur["text"], cur["category"], cur["created_by"]) != (a["text"], a["category"], a["created_by"]):
            c.execute("UPDATE actions SET text = ?, category = ?, created_by = ? WHERE action_id = ?",
                      [a["text"], a["category"], a["created_by"], aid])
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
