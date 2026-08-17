"""Quicklog capture: rule classification, the one-shot log() path, backdating,
and source provenance. lm=None exercises the rules fallback; a stub exercises
the model path, so no LM Studio is needed to run these."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from heliosd.narrative import quicklog
from heliosd.store import db


@pytest.fixture()
def conn():
    return db.connect_memory()


class StubLM:
    """Minimal stand-in for LMStudio: available, returns a fixed proposal."""

    fallback = "stub-model"

    def __init__(self, proposal):
        self._proposal = proposal

    def available(self):
        return True

    def structured(self, messages, schema, model=None, temperature=0.0):
        return dict(self._proposal)


def test_rules_classification():
    cases = {
        "double espresso": "caffeine",
        "glass of wine with dinner": "alcohol",
        "took magnesium late": "med",
        "left knee sore": "symptom",
        "big bottle of water": "water",
        "ate lunch early": "food",
        "long day at work": "note",
    }
    for text, kind in cases.items():
        assert quicklog.parse(None, text)["kind"] == kind, text


def test_parse_rules_marks_parser_and_keeps_raw():
    p = quicklog.parse(None, "double espresso")
    assert p["parser"] == "rules"
    assert p["raw_text"] == "double espresso"


def test_log_never_drops_a_capture(conn):
    out = quicklog.log(conn, None, "completely unclassifiable line", source="shortcut")
    assert out["stored"] and out["kind"] == "note"
    row = db.fetchdicts(conn, "SELECT * FROM events WHERE event_id = ?",
                        [out["event_id"]])[0]
    assert row["source"] == "shortcut"
    payload = json.loads(row["payload"])
    assert payload["raw_text"] == "completely unclassifiable line"
    assert payload["parser"] == "rules"


def test_log_llm_path_backdates_minutes_ago(conn):
    lm = StubLM({"kind": "caffeine", "item": "double espresso",
                 "amount": "2 shots", "minutes_ago": 90})
    out = quicklog.log(conn, lm, "double espresso 90 min ago", source="shortcut")
    assert out["kind"] == "caffeine"
    delta = datetime.now() - datetime.fromisoformat(out["ts"])
    assert timedelta(minutes=85) < delta < timedelta(minutes=95)
    row = db.fetchdicts(conn, "SELECT payload FROM events WHERE event_id = ?",
                        [out["event_id"]])[0]
    assert json.loads(row["payload"])["parser"] == "llm"


def test_log_summary_is_speakable(conn):
    out = quicklog.log(conn, None, "coffee", source="chip")
    assert out["summary"].startswith("Logged caffeine")
    assert " at " in out["summary"]


def test_llm_misfile_of_caffeine_is_overridden(conn):
    # Observed live on the 9B fallback: espresso filed as symptom with a
    # garbage amount. The keyword guard must win on caffeine/alcohol and the
    # amount must be sanitized.
    lm = StubLM({"kind": "symptom", "item": "double espresso",
                 "amount": ">&#x2013;90", "minutes_ago": 90})
    p = quicklog.parse(lm, "double espresso 90 minutes ago")
    assert p["kind"] == "caffeine"
    assert p["parser"] == "llm+rules"
    assert p["amount"] == ""


def test_llm_kind_kept_when_rules_have_no_substance_hit():
    lm = StubLM({"kind": "symptom", "item": "left knee",
                 "amount": "", "minutes_ago": 0})
    p = quicklog.parse(lm, "left knee acting up again")
    assert p["kind"] == "symptom"
    assert p["parser"] == "llm"


def test_backdate_clamped_to_seven_days(conn):
    out = quicklog.confirm(conn, {"kind": "note", "item": "x",
                                  "minutes_ago": 999999})
    delta = datetime.now() - datetime.fromisoformat(out["ts"])
    assert delta <= timedelta(days=7, minutes=1)


def test_chip_confirm_stores_source(conn):
    out = quicklog.confirm(conn, {"kind": "med", "item": "medication", "source": "chip"})
    row = db.fetchdicts(conn, "SELECT kind, source FROM events WHERE event_id = ?",
                        [out["event_id"]])[0]
    assert row["kind"] == "med"
    assert row["source"] == "chip"


def test_confirm_defaults_source_to_user(conn):
    out = quicklog.confirm(conn, {"kind": "note", "item": "no source given"})
    row = db.fetchdicts(conn, "SELECT source FROM events WHERE event_id = ?",
                        [out["event_id"]])[0]
    assert row["source"] == "user"
