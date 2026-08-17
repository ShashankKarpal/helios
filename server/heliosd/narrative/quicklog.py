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

_RULES = [
    (re.compile(r"\b(coffee|espresso|latte|caffeine|chai|tea|red ?bull)\b", re.I), "caffeine"),
    (re.compile(r"\b(beer|wine|whisk|vodka|drink|alcohol)\b", re.I), "alcohol"),
    (re.compile(r"\b(magnesium|omega|vitamin|tablet|capsule|med|pill)\b", re.I), "med"),
    (re.compile(r"\b(sore|pain|headache|fatigue|dizzy|nausea|cramp)\b", re.I), "symptom"),
    (re.compile(r"\b(water|hydrat)\b", re.I), "water"),
    (re.compile(r"\b(ate|meal|lunch|dinner|breakfast|snack)\b", re.I), "food"),
]


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
    proposal["raw_text"] = text
    return proposal


def confirm(conn, proposal: dict) -> dict:
    ts = datetime.now()
    if proposal.get("minutes_ago"):
        # Clamp to [0, 7 days]: a hallucinated backdate must not file an event
        # into last month's history.
        ts -= timedelta(minutes=max(0, min(int(proposal["minutes_ago"]), 10080)))
    event_id = uuid.uuid4().hex[:16]
    db.execute(conn,
               "INSERT INTO events (event_id, kind, ts, payload, source) VALUES (?, ?, ?, ?, ?)",
               [event_id, proposal.get("kind", "note"), ts,
                json.dumps({k: proposal.get(k) for k in ("item", "amount", "raw_text", "parser")}),
                proposal.get("source") or "user"])
    return {"stored": True, "event_id": event_id, "ts": ts.isoformat(),
            "kind": proposal.get("kind", "note"), "item": proposal.get("item", "")}


def log(conn, lm: LMStudio | None, text: str, source: str = "user") -> dict:
    """One-shot capture: parse and store in a single call, no confirm step.
    A capture is never dropped: with the model down or the line unparseable,
    the text still lands as a rules-classified event (kind falls back to
    'note') with the raw line kept in the payload."""
    proposal = parse(lm, text)
    proposal["source"] = source
    out = confirm(conn, proposal)
    when = datetime.fromisoformat(out["ts"]).strftime("%H:%M")
    out["parser"] = proposal["parser"]
    out["summary"] = f"Logged {out['kind']}: {out['item'] or text.strip()} at {when}."
    return out
