"""Conversational logging (the ROX UX, local): free text in, structured event
out, one-tap confirm before anything is stored."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime

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

_RULES = [
    (re.compile(r"\b(coffee|espresso|latte|caffeine|chai|tea|red ?bull)\b", re.I), "caffeine"),
    (re.compile(r"\b(beer|wine|whisk|vodka|drink|alcohol)\b", re.I), "alcohol"),
    (re.compile(r"\b(magnesium|omega|vitamin|tablet|capsule|med|pill)\b", re.I), "med"),
    (re.compile(r"\b(sore|pain|headache|fatigue|dizzy|nausea|cramp)\b", re.I), "symptom"),
    (re.compile(r"\b(water|hydrat)\b", re.I), "water"),
    (re.compile(r"\b(ate|meal|lunch|dinner|breakfast|snack)\b", re.I), "food"),
]


def parse(lm: LMStudio | None, text: str) -> dict:
    """Proposal only; nothing is stored until confirm()."""
    proposal = None
    if lm and lm.available():
        try:
            proposal = lm.structured(
                [{"role": "system", "content": "Parse the log line into the schema. minutes_ago is 0 unless stated."},
                 {"role": "user", "content": text}],
                QUICKLOG_SCHEMA, model=lm.fallback, temperature=0.0)
        except Exception:
            proposal = None
    if not proposal:
        kind = next((k for rx, k in _RULES if rx.search(text)), "note")
        proposal = {"kind": kind, "item": text.strip(), "amount": "", "minutes_ago": 0}
    proposal["raw_text"] = text
    return proposal


def confirm(conn, proposal: dict) -> dict:
    ts = datetime.now()
    if proposal.get("minutes_ago"):
        from datetime import timedelta
        ts -= timedelta(minutes=int(proposal["minutes_ago"]))
    event_id = uuid.uuid4().hex[:16]
    db.execute(conn, "INSERT INTO events (event_id, kind, ts, payload) VALUES (?, ?, ?, ?)",
               [event_id, proposal.get("kind", "note"), ts,
                json.dumps({k: proposal.get(k) for k in ("item", "amount", "raw_text")})])
    return {"stored": True, "event_id": event_id, "ts": ts.isoformat()}
