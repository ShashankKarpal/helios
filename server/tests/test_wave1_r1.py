"""Wave 1 (fix program 2026-10-08), fork R1: A2 stable action ids, A3 first
tick and Whoop wake-window polling, A13 timestamped INFO logging, A24 client
allowlist and TLS fail-closed. Every test here failed on the code before its
fix (the failing line is recorded in the commit message of each item)."""

from __future__ import annotations

import io
import logging
import re

import pytest

from heliosd import main


# ---------------------------------------------------------------- A13 logging

def test_configure_logging_stamps_heliosd_info_lines_and_keeps_third_party_quiet():
    buf = io.StringIO()
    handler = main.configure_logging(stream=buf)
    try:
        logging.getLogger("heliosd.wave1").info("tick %s", {"dates": 2})
        logging.getLogger("httpx").info("HTTP Request: GET https://example.invalid")
    finally:
        logging.getLogger().removeHandler(handler)
    lines = buf.getvalue().splitlines()
    assert len(lines) == 1, lines
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} INFO heliosd\.wave1: tick \{'dates': 2\}$", lines[0]), lines[0]


def test_uvicorn_log_config_carries_timestamps_on_both_formatters():
    cfg = main.logging_config()
    for name in ("default", "access"):
        assert cfg["formatters"][name]["fmt"].startswith("%(asctime)s "), name
        assert cfg["formatters"][name]["datefmt"] == main.LOG_DATEFMT
    # uvicorn's own loggers keep their handlers: nothing is logged twice.
    assert cfg["loggers"]["uvicorn"]["propagate"] is False
    assert cfg["loggers"]["uvicorn.access"]["propagate"] is False


def test_access_log_lines_drop_the_query_string():
    """Codex A point 17: the OAuth callback's code and state rode the query
    string into the access log. The configured access formatter drops it."""
    cfg = main.logging_config()
    assert cfg["formatters"]["access"]["()"] == "heliosd.main.QuietAccessFormatter"
    fmt = main.QuietAccessFormatter(fmt=cfg["formatters"]["access"]["fmt"], datefmt=main.LOG_DATEFMT, use_colors=False)
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                            ("127.0.0.1:50000", "GET", "/whoop/callback?code=SYNTHETIC-CODE&state=S1", "1.1", 200), None)
    line = fmt.format(rec)
    assert '"GET /whoop/callback HTTP/1.1" 200' in line, line
    assert "SYNTHETIC-CODE" not in line and "state=" not in line
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ", line), line


# ---------------------------------------------------------------- A2 action ids

import json                                                                     # noqa: E402
from datetime import date, datetime, timezone                                   # noqa: E402
from zoneinfo import ZoneInfo                                                   # noqa: E402

from fastapi.testclient import TestClient                                       # noqa: E402

from heliosd.config import Settings                                             # noqa: E402
from heliosd.narrative import brief, templates                                  # noqa: E402
from heliosd.signals import recompute as rc                                     # noqa: E402
from heliosd.store import db                                                    # noqa: E402

DAY = date(2026, 10, 8)
DUBAI = ZoneInfo("Asia/Dubai")
TOKEN = "test-token-0123456789"
H = {"X-Helios-Token": TOKEN}
POSITIONAL = re.compile(r"^\d{4}-\d{2}-\d{2}:\d+$")

SLEEP_SHORT = {"key": "sleep_short", "category": "sleep",
               "text": "Sleep ran short. Set a wind-down alert 45 minutes before your usual bedtime tonight."}
LATE_NIGHT = {"key": "late_night", "category": "sleep",
              "text": "Late night detected. Screens off and Sleep Focus on by 23:30 tonight."}
HEAT = {"key": "heat", "category": "hydration",
        "text": "Heat season: front-load water before noon and keep outdoor efforts early."}
SHIFT = {"key": "schedule_shift", "category": "circadian",
         "text": "Schedule shift detected. Anchor tomorrow with morning daylight and a fixed wake time."}
GREEN = {"key": "recovery_green", "category": "training",
         "text": "Recovery is green. Good day for your harder session if one is planned."}


def _aid(a, day=DAY):
    return f"{day}:{a['category']}:{a['key']}"


def _rows(conn, day=DAY):
    return {r["action_id"]: r for r in db.fetchdicts(
        conn, "SELECT action_id, text, category, status, created_by FROM actions WHERE date = ?", [day])}


def _persist(conn, actions, validated=False, day=DAY):
    with db.transaction(conn) as c:
        brief._persist_actions(c, day, actions, validated)


def _set_status(conn, aid, status):
    db.execute(conn, "UPDATE actions SET status = ? WHERE action_id = ?", [status, aid])


def test_rule_based_actions_carry_a_stable_key_per_rule():
    sig = [{"metric": "recovery_score", "state": "favorable", "value": 70},
           {"metric": "sleep_duration", "state": "flag", "value": 5.5},
           {"metric": "steps", "state": "neutral", "value": 1200}]
    out = templates.rule_based_actions(sig, ["heat"])
    assert [a["key"] for a in out] == ["recovery_green", "sleep_short", "heat"]
    assert all(a["category"] for a in out)
    assert templates.rule_based_actions([], [])[0]["key"] == "steady"
    # Every rule has its own key: ids can never collide inside one day.
    sig_all = [{"metric": "recovery_score", "state": "flag", "value": 20},
               {"metric": "sleep_duration", "state": "flag", "value": 5.0},
               {"metric": "hrv_rmssd", "state": "flag", "value": 20},
               {"metric": "steps", "state": "flag", "value": 100}]
    keys = [a["key"] for a in templates.rule_based_actions(sig_all, ["late_night", "heat", "travel_or_shifted_schedule"])]
    assert len(keys) == len(set(keys))


def test_adopted_status_stays_on_its_text_when_the_rules_reorder():
    """The audit's case (T3): adopt the sleep action, then regenerate with a
    new first rule. Before: the adopted status moved onto the new first text."""
    conn = db.connect_memory()
    _persist(conn, [SLEEP_SHORT, HEAT, SHIFT])
    assert set(_rows(conn)) == {_aid(SLEEP_SHORT), _aid(HEAT), _aid(SHIFT)}
    _set_status(conn, _aid(SLEEP_SHORT), "adopted")
    _set_status(conn, _aid(SHIFT), "dismissed")

    _persist(conn, [GREEN, SLEEP_SHORT, HEAT])          # the Whoop night landed green; the shift flag is gone
    rows = _rows(conn)
    assert rows[_aid(SLEEP_SHORT)]["status"] == "adopted"
    assert rows[_aid(SLEEP_SHORT)]["text"] == SLEEP_SHORT["text"]
    assert rows[_aid(GREEN)]["status"] == "suggested"
    assert rows[_aid(HEAT)]["status"] == "suggested"
    # The dismissed shift action is kept as the record of what the owner dismissed.
    assert rows[_aid(SHIFT)]["status"] == "dismissed" and rows[_aid(SHIFT)]["text"] == SHIFT["text"]
    assert not [k for k in rows if POSITIONAL.match(k)]


def test_a_resolved_row_is_never_rewritten_even_by_llm_wording():
    conn = db.connect_memory()
    _persist(conn, [SLEEP_SHORT, HEAT])
    _set_status(conn, _aid(SLEEP_SHORT), "adopted")
    reworded = [dict(SLEEP_SHORT, text="Sleep was short: wind down earlier tonight."),
                dict(HEAT, text="Heat season: drink most of your water before noon.")]
    _persist(conn, reworded, validated=True)
    rows = _rows(conn)
    assert rows[_aid(SLEEP_SHORT)]["text"] == SLEEP_SHORT["text"]           # adopted: untouched
    assert rows[_aid(SLEEP_SHORT)]["created_by"] == "engine"
    assert rows[_aid(HEAT)]["text"] == reworded[1]["text"]                  # suggested: reworded
    assert rows[_aid(HEAT)]["created_by"] == "llm"


def _legacy(conn, rows, day=DAY):
    with db.transaction(conn) as c:
        for i, (text, cat, status) in enumerate(rows):
            c.execute("INSERT INTO actions (action_id, date, text, category, status, created_by) "
                      "VALUES (?, ?, ?, ?, ?, 'llm')", [f"{day}:{i}", day, text, cat, status])


def test_legacy_positional_rows_migrate_by_exact_text_only():
    """Codex A point 6: a category match is not an identity. A resolved
    positional row moves to the stable id only when its text is exactly a
    current rule's text; a reworded one keeps its positional id and status."""
    conn = db.connect_memory()
    _legacy(conn, [(SLEEP_SHORT["text"], "sleep", "adopted"),
                   ("Heat season water first thing.", "hydration", "suggested"),
                   ("A schedule shift showed up: anchor tomorrow with daylight.", "circadian", "dismissed")])
    _persist(conn, [SLEEP_SHORT, HEAT, SHIFT])
    rows = _rows(conn)
    assert rows[_aid(SLEEP_SHORT)]["status"] == "adopted"                   # exact text: re-filed with its status
    assert rows[_aid(SLEEP_SHORT)]["created_by"] == "llm"
    assert f"{DAY}:0" not in rows
    assert f"{DAY}:1" not in rows                                            # suggested legacy row: dropped as before
    assert rows[_aid(HEAT)]["status"] == "suggested" and rows[_aid(HEAT)]["text"] == HEAT["text"]
    assert rows[f"{DAY}:2"]["status"] == "dismissed"                         # reworded: stays where it was
    assert rows[_aid(SHIFT)]["status"] == "suggested"                        # and the rule's suggestion sits beside it
    assert len(rows) == 4


def test_legacy_migration_is_stable_across_two_regenerations():
    """Codex A point 8: inputs change between regenerations; a legacy row that
    matches only on the second one migrates then, and a destination that the
    owner already resolved keeps both records."""
    conn = db.connect_memory()
    _legacy(conn, [(HEAT["text"], "hydration", "adopted"), (GREEN["text"], "training", "dismissed")])
    _persist(conn, [SLEEP_SHORT])                    # neither legacy rule fires: nothing moves
    rows = _rows(conn)
    assert rows[f"{DAY}:0"]["status"] == "adopted" and rows[f"{DAY}:1"]["status"] == "dismissed"
    _set_status(conn, _aid(SLEEP_SHORT), "done")
    _persist(conn, [SLEEP_SHORT, HEAT])              # heat fires now: the adopted legacy row is re-filed
    rows = _rows(conn)
    assert rows[_aid(HEAT)]["status"] == "adopted" and f"{DAY}:0" not in rows
    assert rows[_aid(SLEEP_SHORT)]["status"] == "done"
    # The model rewords green: the legacy green row does not match that text and
    # stays; the owner adopts the new suggestion.
    _persist(conn, [dict(GREEN, text="Recovery is green: push today."), SLEEP_SHORT, HEAT], validated=True)
    rows = _rows(conn)
    assert rows[f"{DAY}:1"]["status"] == "dismissed" and rows[_aid(GREEN)]["status"] == "suggested"
    _set_status(conn, _aid(GREEN), "adopted")
    # The template path brings the rule text back: the legacy row now matches a
    # destination the owner already resolved, so both records stay as they are.
    _persist(conn, [GREEN, SLEEP_SHORT, HEAT])
    rows = _rows(conn)
    assert rows[_aid(GREEN)]["status"] == "adopted" and rows[_aid(GREEN)]["text"] == "Recovery is green: push today."
    assert rows[f"{DAY}:1"]["status"] == "dismissed" and rows[f"{DAY}:1"]["text"] == GREEN["text"]
    before = _rows(conn)
    _persist(conn, [GREEN, SLEEP_SHORT, HEAT])
    assert _rows(conn) == before                     # idempotent


def test_persist_drops_stale_suggestions_only():
    conn = db.connect_memory()
    _persist(conn, [SLEEP_SHORT, HEAT, SHIFT])
    _set_status(conn, _aid(HEAT), "done")
    _persist(conn, [SLEEP_SHORT])
    rows = _rows(conn)
    assert set(rows) == {_aid(SLEEP_SHORT), _aid(HEAT)}        # done stays, the stale shift suggestion goes
    assert rows[_aid(HEAT)]["status"] == "done"


def test_llm_wording_is_taken_only_for_distinct_categories_in_rule_order():
    rules = [GREEN, SLEEP_SHORT, HEAT]
    paired = brief.pair_llm_actions(rules, [
        {"text": "Recovery is green: go for the hard session.", "category": "training"},
        {"text": "Short night: wind down earlier.", "category": "sleep"},
        {"text": "Drink early in the heat.", "category": "hydration"}])
    assert [a["key"] for a in paired] == ["recovery_green", "sleep_short", "heat"]
    assert paired[0]["text"] == "Recovery is green: go for the hard session."
    # Reordered, extra or missing model actions change nothing: the rule texts stay.
    for bad in ([{"text": "x", "category": "sleep"}, {"text": "y", "category": "training"}, {"text": "z", "category": "hydration"}],
                [{"text": "x", "category": "training"}],
                [{"text": "x", "category": "training"}] * 4):
        assert [a["text"] for a in brief.pair_llm_actions(rules, bad)] == [a["text"] for a in rules]
    # Codex A point 7: two rules share the sleep category, so a swapped pair would
    # pass a position-and-category check; the rule texts stay.
    same_cat = [SLEEP_SHORT, LATE_NIGHT, HEAT]
    swapped = [{"text": "Screens off early tonight.", "category": "sleep"},
               {"text": "Wind down earlier tonight.", "category": "sleep"},
               {"text": "Drink early in the heat.", "category": "hydration"}]
    assert [a["text"] for a in brief.pair_llm_actions(same_cat, swapped)] == [a["text"] for a in same_cat]


def test_read_actions_keeps_the_rule_order_not_the_alphabetical_id_order():
    conn = db.connect_memory()
    _persist(conn, [GREEN, SLEEP_SHORT, HEAT])
    out = brief._read_actions(conn, DAY, [GREEN, SLEEP_SHORT, HEAT])
    assert [a["action_id"].rsplit(":", 1)[-1] for a in out] == ["recovery_green", "sleep_short", "heat"]
    assert set(out[0]) == {"action_id", "text", "category", "status"}


def _signals(conn, rows, flags, day=DAY):
    with db.transaction(conn) as c:
        c.execute("DELETE FROM signals WHERE date = ?", [day])
        for metric, state, value in rows:
            c.execute("INSERT INTO signals (date, metric, state, value, unit, context_flags, why) "
                      "VALUES (?, ?, ?, ?, 'u', ?, 'synthetic')", [day, metric, state, value, json.dumps(flags)])


def _regenerate(conn, day=DAY):
    """What a recompute of the date does to the brief: new generation, cached
    narrative and still-suggested actions dropped."""
    with db.transaction(conn) as c:
        rc.invalidate_derived(c, {day})


def test_generate_brief_fast_path_keeps_each_status_on_its_own_action():
    """End to end through generate_brief (the /api/today path), the audit's
    morning: adopt and dismiss, then the Whoop night lands and the rules change."""
    conn = db.connect_memory()
    _signals(conn, [("sleep_duration", "flag", 5.5)], ["heat", "travel_or_shifted_schedule"])
    first = brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False)
    assert [a["action_id"] for a in first["actions"]] == [_aid(SLEEP_SHORT), _aid(HEAT), _aid(SHIFT)]
    _set_status(conn, _aid(SLEEP_SHORT), "adopted")
    _set_status(conn, _aid(SHIFT), "dismissed")
    _regenerate(conn)
    _signals(conn, [("recovery_score", "favorable", 70), ("sleep_duration", "flag", 5.5)], ["heat"])
    second = brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False)
    got = [(a["action_id"], a["status"], a["text"]) for a in second["actions"]]
    assert got == [(_aid(GREEN), "suggested", GREEN["text"]),
                   (_aid(SLEEP_SHORT), "adopted", SLEEP_SHORT["text"]),
                   (_aid(HEAT), "suggested", HEAT["text"]),
                   (_aid(SHIFT), "dismissed", SHIFT["text"])]


class _FakeLM:
    primary = fallback = "fake-model"

    def __init__(self, actions):
        self.actions = actions

    def available(self):
        return True

    def structured(self, messages, schema, temperature=0.2, model=None):
        return {"narrative": "Signals look steady today.", "actions": self.actions, "flags": []}


def test_generate_brief_slow_path_cannot_swap_two_sleep_actions():
    conn = db.connect_memory()
    _signals(conn, [("sleep_duration", "flag", 5.5)], ["late_night", "heat"])
    lm = _FakeLM([{"text": "Screens off early tonight.", "category": "sleep"},
                  {"text": "Wind down earlier tonight.", "category": "sleep"},
                  {"text": "Drink early in the heat.", "category": "hydration"}])
    out = brief.generate_brief(conn, lm, DAY, "Owner", force=True, allow_llm=True)
    assert out["validated"] is True
    rows = _rows(conn)
    assert rows[_aid(SLEEP_SHORT)]["text"] == SLEEP_SHORT["text"]
    assert rows[_aid(LATE_NIGHT)]["text"] == LATE_NIGHT["text"]
    assert rows[_aid(HEAT)]["text"] == HEAT["text"]


def test_generate_brief_slow_path_rewords_distinct_categories_under_the_same_ids():
    conn = db.connect_memory()
    _signals(conn, [("recovery_score", "favorable", 70), ("sleep_duration", "flag", 5.5)], ["heat"])
    lm = _FakeLM([{"text": "Recovery is green: a good day for the harder session.", "category": "training"},
                  {"text": "Short night: wind down earlier tonight.", "category": "sleep"},
                  {"text": "Drink most of your water before noon.", "category": "hydration"}])
    brief.generate_brief(conn, lm, DAY, "Owner", force=True, allow_llm=True)
    rows = _rows(conn)
    assert set(rows) == {_aid(GREEN), _aid(SLEEP_SHORT), _aid(HEAT)}
    assert rows[_aid(SLEEP_SHORT)]["text"] == "Short night: wind down earlier tonight."
    assert rows[_aid(SLEEP_SHORT)]["created_by"] == "llm"
