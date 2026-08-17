"""Conversational logging (the ROX UX, local): free text in, structured event
out. Two paths in: the PWA's parse-then-confirm flow, and the one-shot log()
behind /api/quicklog/log (Shortcut, Today chips) that never drops a capture."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta

from heliosd.narrative.lmstudio import LMStudio
from heliosd.store import db

QUICKLOG_SCHEMA = {
    "name": "quicklog_event",
    "strict": True,
    "schema": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["caffeine", "alcohol", "med", "symptom",
                                            "food", "water", "note"]},
        "item": {"type": "string"},
        "amount": {"type": "string"},
        "minutes_ago": {"type": "integer"},
    }, "required": ["kind", "item", "amount", "minutes_ago"], "additionalProperties": False},
}

_UNDO_RX = re.compile(r"^\s*undo(\s+(that|last|it))?(\s+one)?\s*[.!]?\s*$", re.I)

_TIME_RX = re.compile(
    r"\b(?:at\s+)?(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?m\.?\b"  # 4pm, at 4:30 PM, 7 a.m.
    r"|\bat\s+(\d{1,2}):([0-5]\d)\b",                          # at 16:00 / at 4:30 (needs 'at')
    re.I)

_RULES = [
    (re.compile(r"\b(coffee|espresso|latte|caffeine|chai|tea|red ?bull)\b", re.I), "caffeine"),
    (re.compile(r"\b(beer|wine|whisk|vodka|drink|alcohol)\b", re.I), "alcohol"),
    (re.compile(r"\b(magnesium|omega|vitamin|tablet|capsule|med|pill)\b", re.I), "med"),
    (re.compile(r"\b(sore|pain|headache|fatigue|dizzy|nausea|cramp)\b", re.I), "symptom"),
    (re.compile(r"\b(water|hydrat)\b", re.I), "water"),
    (re.compile(r"\b(ate|meal|lunch|dinner|breakfast|snack)\b", re.I), "food"),
]


def _absolute_minutes_ago(text: str, now: datetime) -> int | None:
    """Deterministic clock arithmetic for a stated absolute time. The small
    model gets this wrong ('Coffee at 4 PM' spoken at 17:19 came back as
    minutes_ago=120 instead of 79), so a stated time always overrides it.
    A bare hour without am/pm takes its most recent past occurrence, and a
    time still ahead of now is read as yesterday: captures log the past."""
    m = _TIME_RX.search(text)
    if not m:
        return None
    if m.group(1) is not None:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if hour > 12:
            return None
        hour %= 12
        if m.group(3).lower() == "p":
            hour += 12
        hours = [hour]
    else:
        hour, minute = int(m.group(4)), int(m.group(5))
        if hour > 23:
            return None
        hours = [hour] if hour > 12 else [hour, (hour + 12) % 24]
    candidates = []
    for h in hours:
        cand = now.replace(hour=h, minute=minute, second=0, microsecond=0)
        if cand > now:
            cand -= timedelta(days=1)
        candidates.append(cand)
    return int((now - max(candidates)).total_seconds() // 60)


def parse(lm: LMStudio | None, text: str, now: datetime | None = None) -> dict:
    """Proposal only; nothing is stored until confirm()."""
    now = now or datetime.now()
    proposal = None
    if lm and lm.available():
        try:
            proposal = lm.structured(
                [{"role": "system", "content":
                    f"Now is {now:%A %H:%M}. Parse the log line into the schema. "
                    "minutes_ago is minutes before now: 0 unless stated; convert "
                    "absolute times like 'at 4pm' using the current time."},
                 {"role": "user", "content": text}],
                QUICKLOG_SCHEMA, model=lm.fallback, temperature=0.0)
            if proposal:
                proposal["parser"] = "llm"
        except Exception:
            proposal = None
    if not proposal:
        kind = next((k for rx, k in _RULES if rx.search(text)), "note")
        proposal = {"kind": kind, "item": text.strip(), "amount": "",
                    "minutes_ago": 0, "parser": "rules"}
    else:
        # Deterministic guards over the small model's output. Observed on the
        # 9B fallback: "double espresso" filed as symptom with amount
        # ">&#x2013;90". A misfiled caffeine or alcohol event silently starves
        # the cutoff finder, so on those two kinds a keyword hit overrides the
        # model (cost: a rare negation like "no coffee today" is miscounted;
        # a misfiled positive is worse for the analysis than that).
        rules_kind = next((k for rx, k in _RULES if rx.search(text)), None)
        if rules_kind in ("caffeine", "alcohol") and proposal.get("kind") != rules_kind:
            proposal["kind"] = rules_kind
            proposal["parser"] = "llm+rules"
        amount = str(proposal.get("amount") or "")
        if not re.fullmatch(r"[\w .,/x×%-]{0,40}", amount):
            proposal["amount"] = ""
    stated = _absolute_minutes_ago(text, now)
    if stated is not None and proposal.get("minutes_ago") != stated:
        proposal["minutes_ago"] = stated
        if proposal.get("parser") == "llm":
            proposal["parser"] = "llm+rules"
    proposal["raw_text"] = text
    return proposal


def confirm(conn, proposal: dict) -> dict:
    ts = datetime.now()
    if proposal.get("minutes_ago"):
        # Clamp to [0, 7 days]: a hallucinated backdate must not file an event
        # into last month's history.
        ts -= timedelta(minutes=max(0, min(int(proposal["minutes_ago"]), 10080)))
    kind = proposal.get("kind", "note")
    payload = json.dumps({k: proposal.get(k) for k in ("item", "amount", "raw_text", "parser")})
    # Double-log guard: an identical capture inside 2 minutes (double-tapped
    # chip, Shortcut invoked twice) updates the existing row instead of
    # inserting a twin that would double-count in the cutoff finder.
    recent = db.fetchdicts(conn,
        "SELECT event_id, kind, payload FROM events WHERE created_at >= ?",
        [datetime.now() - timedelta(minutes=2)])
    raw = proposal.get("raw_text")
    for r in recent:
        if r["kind"] != kind:
            continue
        rp = json.loads(r["payload"] or "{}")
        same = (raw and rp.get("raw_text") == raw) or (
            not raw and not rp.get("raw_text") and rp.get("item") == proposal.get("item"))
        if same:
            db.execute(conn, "UPDATE events SET ts = ?, payload = ? WHERE event_id = ?",
                       [ts, payload, r["event_id"]])
            return {"stored": True, "deduped": True, "event_id": r["event_id"],
                    "ts": ts.isoformat(), "kind": kind, "item": proposal.get("item", "")}
    event_id = uuid.uuid4().hex[:16]
    db.execute(conn,
               "INSERT INTO events (event_id, kind, ts, payload, source) VALUES (?, ?, ?, ?, ?)",
               [event_id, kind, ts, payload, proposal.get("source") or "user"])
    return {"stored": True, "event_id": event_id, "ts": ts.isoformat(),
            "kind": kind, "item": proposal.get("item", "")}


def undo_last(conn) -> dict:
    """Remove the most recently captured event. The 'undo' utterance and
    DELETE /api/quicklog/last both land here."""
    rows = db.fetchdicts(conn,
        "SELECT event_id, kind, payload FROM events ORDER BY created_at DESC LIMIT 1")
    if not rows:
        return {"removed": False, "summary": "Nothing to undo."}
    r = rows[0]
    db.execute(conn, "DELETE FROM events WHERE event_id = ?", [r["event_id"]])
    item = (json.loads(r["payload"] or "{}")).get("item") or r["kind"]
    return {"removed": True, "event_id": r["event_id"], "kind": r["kind"],
            "summary": f"Removed {r['kind']}: {item}."}


def delete_event(conn, event_id: str) -> dict:
    rows = db.fetchdicts(conn, "SELECT event_id FROM events WHERE event_id = ?", [event_id])
    if not rows:
        return {"removed": False}
    db.execute(conn, "DELETE FROM events WHERE event_id = ?", [event_id])
    return {"removed": True, "event_id": event_id}


def log(conn, lm: LMStudio | None, text: str, source: str = "user") -> dict:
    """One-shot capture: parse and store in a single call, no confirm step.
    A capture is never dropped: with the model down or the line unparseable,
    the text still lands as a rules-classified event (kind falls back to
    'note') with the raw line kept in the payload. Saying 'undo' removes the
    last capture instead of storing anything."""
    if _UNDO_RX.match(text):
        out = undo_last(conn)
        out["stored"] = False
        return out
    proposal = parse(lm, text)
    proposal["source"] = source
    out = confirm(conn, proposal)
    when = datetime.fromisoformat(out["ts"]).strftime("%H:%M")
    out["parser"] = proposal["parser"]
    out["summary"] = f"Logged {out['kind']}: {out['item'] or text.strip()} at {when}."
    return out
