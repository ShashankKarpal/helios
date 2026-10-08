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
    # validated=True stands for the model's own wording, which pair_llm_actions labels "llm".
    with db.transaction(conn) as c:
        brief._persist_actions(c, day, [dict(a, created_by="llm") for a in actions] if validated else actions)


def _set_status(conn, aid, status):
    db.execute(conn, "UPDATE actions SET status = ? WHERE action_id = ?", [status, aid])


def test_rule_based_actions_carry_a_stable_key_per_rule():
    sig = [{"metric": "recovery_score", "state": "favorable", "value": 70},
           {"metric": "sleep_duration", "state": "flag", "value": 5.5},
           {"metric": "steps", "state": "neutral", "value": 1200}]
    out = templates.rule_based_actions(sig, ["heat"])
    assert [a["key"] for a in out] == ["recovery_green", "sleep_short", "heat"]
    assert all(a["category"] for a in out)
    # With nothing judged yet the default is "nothing_yet" (A5, Codex A point 11 on the R2
    # design); with a judged core signal and no rule it is "steady". Both keys are stable.
    assert templates.rule_based_actions([], [])[0]["key"] == "nothing_yet"
    judged = [{"metric": "recovery_score", "state": "neutral", "value": 55, "device_key": "whoop"}]
    assert templates.rule_based_actions(judged, [])[0]["key"] == "steady"
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
    # A neutral recovery keeps the night "in" (A5 skips the model while recovery is pending)
    # and adds no rule action, so the model path and the pairing are what is tested.
    _signals(conn, [("recovery_score", "neutral", 55), ("sleep_duration", "flag", 5.5)], ["late_night", "heat"])
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


def test_a_rule_text_kept_by_the_pairing_is_labelled_engine_though_the_narrative_validated():
    """Wave 1 review: a validated narrative labelled every stored action
    created_by "llm", even when pair_llm_actions threw the model's wording away
    and kept the rule text. The label now says where the stored text came from."""
    conn = db.connect_memory()
    _signals(conn, [("recovery_score", "neutral", 55), ("sleep_duration", "flag", 5.5)], ["late_night", "heat"])
    lm = _FakeLM([{"text": "Screens off early tonight.", "category": "sleep"},      # two sleep rules: not paired
                  {"text": "Wind down earlier tonight.", "category": "sleep"},
                  {"text": "Drink early in the heat.", "category": "hydration"}])
    assert brief.generate_brief(conn, lm, DAY, "Owner", force=True, allow_llm=True)["validated"] is True
    assert {aid: (r["text"], r["created_by"]) for aid, r in _rows(conn).items()} == {
        _aid(SLEEP_SHORT): (SLEEP_SHORT["text"], "engine"), _aid(LATE_NIGHT): (LATE_NIGHT["text"], "engine"),
        _aid(HEAT): (HEAT["text"], "engine")}


NOON = datetime(2026, 10, 8, 12, 0, tzinfo=DUBAI)


def test_a_recompute_between_rendering_and_tapping_keeps_the_tap_working(tmp_path, monkeypatch):
    """Wave 1 review: every recompute pass deleted the reporting today's
    still-suggested actions and only the next /api/today read brought them
    back, so a tap on a row still on screen answered 404 (and the web
    swallowed it). They now stay until the next brief reconciles them by id."""
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    with TestClient(main.create_app(_settings(tmp_path))) as c:
        conn, policy, reg = c.app.state.conn, c.app.state.policy, c.app.state.registry
        _signals(conn, [("sleep_duration", "flag", 5.5)], ["heat"])
        shown = brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False)["actions"]
        assert [a["action_id"] for a in shown] == [_aid(SLEEP_SHORT), _aid(HEAT)]
        rc.recompute_dates(conn, policy, reg, {DAY}, today=DAY)          # the journal drain
        rc.recompute_window(conn, policy, reg, days=2, now=NOON)         # the hourly tick and /api/recompute
        r = c.post(f"/api/actions/{_aid(SLEEP_SHORT)}/adopted", headers=H)
        assert r.status_code == 200, r.status_code
        # The next brief reconciles by id: the adopted row stays, and the heat
        # suggestion goes because its rule no longer fires (the pass rebuilt
        # the day's signals from an empty store).
        brief.generate_brief(conn, None, DAY, "Owner", allow_llm=False)
        rows = _rows(conn)
        assert rows[_aid(SLEEP_SHORT)]["status"] == "adopted" and _aid(HEAT) not in rows


def test_the_status_write_never_reports_ok_without_a_stored_row(tmp_path, monkeypatch):
    """Wave 1 review: the 404 check and the UPDATE took the store lock
    separately, so a pass that dropped the row between them turned the tap
    into {"ok": true} with nothing stored. Here the row is gone after the
    route's first store statement, as if a recompute landed right then."""
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    aid = _aid(SLEEP_SHORT)
    with TestClient(main.create_app(_settings(tmp_path))) as c:
        conn = c.app.state.conn
        _persist(conn, [SLEEP_SHORT])
        real_execute, real_fetchall, seen = db.execute, db.fetchall, []

        def racing(real):
            def call(conn_, sql, params=None):
                if seen:                                  # a pass lands between two statements of the route
                    real_execute(conn_, "DELETE FROM actions WHERE action_id = ?", [aid])
                seen.append(sql)
                return real(conn_, sql, params)
            return call
        monkeypatch.setattr(db, "execute", racing(real_execute))
        monkeypatch.setattr(db, "fetchall", racing(real_fetchall))
        r = c.post(f"/api/actions/{aid}/adopted", headers=H)
        monkeypatch.setattr(db, "execute", real_execute)
        monkeypatch.setattr(db, "fetchall", real_fetchall)
        stored = db.fetchall(conn, "SELECT status FROM actions WHERE action_id = ?", [aid])
        # One statement: nothing can land between the check and the write.
        assert (r.status_code, stored) == (200, [("adopted",)])


# ---------------------------------------------------------------- A3 first tick and the Whoop wake window

import asyncio                                                                  # noqa: E402
import threading                                                                # noqa: E402
import time                                                                     # noqa: E402
from types import SimpleNamespace                                               # noqa: E402

from heliosd.ingest import whoop                                                # noqa: E402


def _settings(tmp_path, wake_poll_minutes=0, **server):
    # wake_poll_minutes 0 keeps the wake-window poller off unless a test turns
    # it on: its first check (inside the window, nothing landed) would pull at
    # startup and race the route under test.
    return Settings(raw={"server": {"ingest_token": TOKEN, **server},
                         "owner": {"timezone": "Asia/Dubai"},
                         "storage": {"db_path": str(tmp_path / "helios.duckdb")},
                         "notifications": {"macos_alerts": False},
                         "whoop": {"enabled": True, "client_id": "x", "client_secret": "y",
                                   "redirect_uri": "http://localhost/cb", "wake_poll_minutes": wake_poll_minutes,
                                   "token_path": str(tmp_path / "whoop_tokens.json")}})


def _counts(days):
    return {"recovery": 1, "sleep": 1, "cycle": 1, "samples": 4, "dates": [str(DAY)]}


def test_first_tick_delay_and_cadence_come_from_settings():
    assert Settings().first_tick_seconds == 120
    assert Settings().background_interval_seconds == 3600
    s = Settings(raw={"server": {"first_tick_seconds": 7, "background_interval_seconds": 60}})
    assert (s.first_tick_seconds, s.background_interval_seconds) == (7, 60)
    w = Settings().whoop
    assert (w["wake_window"], w["wake_poll_minutes"]) == ("05:00-10:00", 15)


def test_background_loop_first_sleep_is_the_configured_delay_not_an_hour(tmp_path, monkeypatch):
    delays = []

    async def fake_sleep(s):
        delays.append(s)
        raise asyncio.CancelledError

    monkeypatch.setattr(main, "SLEEP", fake_sleep)
    app = main.create_app(_settings(tmp_path, first_tick_seconds=90))
    app.state.stopping = False
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main._background_loop(app))
    assert delays == [90]


STAGES = {"score": {"stage_summary": {"total_light_sleep_time_milli": 3600000,
                                      "total_slow_wave_sleep_time_milli": 1800000,
                                      "total_rem_sleep_time_milli": 1800000}}}
RECOVERED = {"score": {"recovery_score": 55}}


def _record(conn, key, kind, start, end, *, state="SCORED", nap=False, sleep_id=None, payload=None):
    native = key.split(":", 1)[1]
    with db.transaction(conn) as c:
        c.execute("INSERT INTO whoop_records (record_key, kind, native_id, sleep_id, start_utc, end_utc, score_state, "
                  "nap, created_at, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  [key, kind, native, sleep_id, start, end, state, nap, start, start, json.dumps(payload or {})])


def test_night_landed_needs_the_main_sleep_ending_today_and_its_own_scored_recovery():
    conn = db.connect_memory()
    today = date(2026, 10, 8)
    assert whoop.night_landed(conn, today, DUBAI) is False
    # A nap and a pending night do not count, even with a linked recovery.
    _record(conn, "sleep:nap1", "sleep", datetime(2026, 10, 8, 9, 0), datetime(2026, 10, 8, 9, 40), nap=True,
            sleep_id="nap1", payload=STAGES)
    _record(conn, "recovery:c0", "recovery", datetime(2026, 10, 8, 9, 50), None, sleep_id="nap1", payload=RECOVERED)
    _record(conn, "sleep:pend", "sleep", datetime(2026, 10, 7, 18, 0), datetime(2026, 10, 8, 1, 10), state="PENDING_SCORE",
            sleep_id="pend", payload=STAGES)
    _record(conn, "recovery:c1", "recovery", datetime(2026, 10, 8, 1, 30), None, sleep_id="pend", payload=RECOVERED)
    assert whoop.night_landed(conn, today, DUBAI) is False
    # The scored night ends 01:18Z = 05:18 in Dubai on Oct 8; without its recovery still False.
    _record(conn, "sleep:main", "sleep", datetime(2026, 10, 7, 18, 8), datetime(2026, 10, 8, 1, 18), sleep_id="main",
            payload=STAGES)
    assert whoop.night_landed(conn, today, DUBAI) is False
    # Codex A point 11: a scored recovery recorded today but linked to another sleep does not count.
    _record(conn, "recovery:c2", "recovery", datetime(2026, 10, 8, 2, 0), None, sleep_id="other", payload=RECOVERED)
    assert whoop.night_landed(conn, today, DUBAI) is False
    # Point 13: the linked recovery is SCORED but carries no recovery_score yet.
    _record(conn, "recovery:c3", "recovery", datetime(2026, 10, 8, 1, 39), None, sleep_id="main", payload={"score": {}})
    assert whoop.night_landed(conn, today, DUBAI) is False
    db.execute(conn, "UPDATE whoop_records SET payload = ? WHERE record_key = 'recovery:c3'", [json.dumps(RECOVERED)])
    assert whoop.night_landed(conn, today, DUBAI) is True
    # Yesterday's question is answered from yesterday's records only.
    assert whoop.night_landed(conn, date(2026, 10, 7), DUBAI) is False


def test_night_landed_ignores_a_scored_sleep_without_stages():
    conn = db.connect_memory()
    _record(conn, "sleep:bare", "sleep", datetime(2026, 10, 7, 18, 8), datetime(2026, 10, 8, 1, 18), sleep_id="bare",
            payload={"score": {"stage_summary": {}}})
    _record(conn, "recovery:c9", "recovery", datetime(2026, 10, 8, 1, 39), None, sleep_id="bare", payload=RECOVERED)
    assert whoop.night_landed(conn, date(2026, 10, 8), DUBAI) is False


def test_night_landed_on_records_written_by_the_real_puller(tmp_path):
    """The live shapes: recovery rows carry sleep_id and are stored by created_at."""
    from tests.test_whoop_records import FakeClient, recovery_rec, sleep_rec
    from heliosd.trust.policy import MetricPolicy
    conn = db.connect_memory()
    policy = MetricPolicy(default_tz="Asia/Dubai")
    policy.sync_registry(conn)
    client = FakeClient(tmp_path, sleep=[sleep_rec("s-1", "2026-10-07T18:08:00.000Z", "2026-10-08T01:18:00.000Z")],
                        recovery=[recovery_rec(77, "s-1", "2026-10-08T01:39:00.000Z")])
    whoop.pull(conn, client, policy, days=3, now=datetime(2026, 10, 8, 3, 0, tzinfo=timezone.utc))
    assert whoop.night_landed(conn, date(2026, 10, 8), DUBAI) is True
    assert whoop.night_landed(conn, date(2026, 10, 9), DUBAI) is False


def test_wake_plan_pulls_every_poll_until_landed_then_waits_for_the_next_window():
    plan = whoop.wake_plan
    window, poll = (5 * 60, 10 * 60), 15 * 60
    assert plan(datetime(2026, 10, 8, 5, 31, tzinfo=DUBAI), window, poll, landed=False) == ("pull", poll)
    assert plan(datetime(2026, 10, 8, 5, 31, tzinfo=DUBAI), window, poll, landed=True) == ("wait", poll)
    assert plan(datetime(2026, 10, 8, 4, 50, tzinfo=DUBAI), window, poll, landed=False) == ("wait", 10 * 60)
    assert plan(datetime(2026, 10, 8, 2, 0, tzinfo=DUBAI), window, poll, landed=False) == ("wait", poll)
    assert plan(datetime(2026, 10, 8, 10, 0, tzinfo=DUBAI), window, poll, landed=False) == ("wait", poll)
    assert plan(datetime(2026, 10, 8, 23, 59, tzinfo=DUBAI), window, poll, landed=False) == ("wait", poll)
    assert plan(datetime(2026, 10, 8, 5, 31, tzinfo=DUBAI), window, 0, landed=False) == ("wait", 15 * 60)


def test_parse_wake_window():
    assert whoop.parse_wake_window("05:00-10:00") == (300, 600)
    assert whoop.parse_wake_window("6:30-7") == (390, 420)
    for bad in ("", "10:00-05:00", "x", "05:00", "25:00-26:00"):
        with pytest.raises(ValueError):
            whoop.parse_wake_window(bad)


def test_wake_loop_pulls_at_once_inside_the_window_when_nothing_has_landed(tmp_path, monkeypatch):
    """The restart case (K3): the daemon comes up inside the wake window with
    the night not yet pulled; the poller pulls within seconds, not an hour."""
    calls, delays = [], []

    def fake_pull(conn, client, policy, days=8, now=None):
        calls.append(days)
        return _counts(days)

    async def fake_sleep(s):
        delays.append(s)
        raise asyncio.CancelledError

    monkeypatch.setattr(main, "whoop_pull", fake_pull)
    monkeypatch.setattr(main, "SLEEP", fake_sleep)
    monkeypatch.setattr(main, "wake_plan", lambda now_local, window, poll_s, landed: ("pull", poll_s))
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    app = main.create_app(_settings(tmp_path, wake_poll_minutes=15))

    async def scenario():
        async with app.router.lifespan_context(app):
            for _ in range(100):
                if app.state.whoop_last_pull is not None and 15 * 60 in delays:
                    break
                await asyncio.sleep(0.05)
    asyncio.run(scenario())
    assert calls == [3], calls
    assert delays[0] == 120 and 15 * 60 in delays, delays
    assert app.state.whoop_last_pull["trigger"] == "wake-window" and app.state.whoop_last_pull["ok"] is True


def test_wake_loop_survives_a_failed_pull_and_records_it(tmp_path, monkeypatch, caplog):
    delays = []

    def failing_pull(conn, client, policy, days=8, now=None):
        raise RuntimeError("synthetic token failure")

    async def fake_sleep(s):
        delays.append(s)
        raise asyncio.CancelledError

    monkeypatch.setattr(main, "whoop_pull", failing_pull)
    monkeypatch.setattr(main, "SLEEP", fake_sleep)
    monkeypatch.setattr(main, "wake_plan", lambda now_local, window, poll_s, landed: ("pull", poll_s))
    app = main.create_app(_settings(tmp_path, wake_poll_minutes=15))
    app.state.stopping, app.state.workers = False, set()
    app.state.conn = db.connect_memory()
    app.state.policy = SimpleNamespace(zone=DUBAI)
    app.state.whoop = object()
    app.state.whoop_pull_last_at, app.state.whoop_last_pull = None, None

    async def scenario():
        app.state.whoop_pull_lock = asyncio.Lock()
        with pytest.raises(asyncio.CancelledError):
            await main._whoop_wake_loop(app)
    with caplog.at_level(logging.WARNING, logger="heliosd"):
        asyncio.run(scenario())
    assert delays == [15 * 60]
    assert app.state.whoop_last_pull == {"ok": False, "trigger": "wake-window", "days": 3,
                                         "pulled_at": app.state.whoop_last_pull["pulled_at"], "error": "RuntimeError"}
    assert any("RuntimeError" in r.getMessage() for r in caplog.records)
    assert not any("synthetic token failure" in r.getMessage() for r in caplog.records)


def test_pulls_from_different_triggers_never_overlap(tmp_path, monkeypatch):
    """Codex A point 14: concurrent token refreshes invalidate each other, so
    the hourly, wake-window and api triggers run one at a time."""
    active, peak = [0], [0]
    guard = threading.Lock()

    def slow_pull(conn, client, policy, days=8, now=None):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.2)
        with guard:
            active[0] -= 1
        return _counts(days)

    monkeypatch.setattr(main, "whoop_pull", slow_pull)
    app = main.create_app(_settings(tmp_path))
    app.state.stopping, app.state.workers = False, set()
    app.state.conn, app.state.policy, app.state.whoop = None, None, object()
    app.state.whoop_pull_last_at, app.state.whoop_last_pull = None, None

    async def scenario():
        app.state.whoop_pull_lock = asyncio.Lock()
        return await asyncio.gather(main._whoop_pull_now(app, "hourly", 8),
                                    main._whoop_pull_now(app, "wake-window", 3))
    out = asyncio.run(scenario())
    assert peak[0] == 1
    assert [o["trigger"] for o in out] == ["hourly", "wake-window"]


def test_whoop_pull_route_is_rate_limited_and_echoes_the_counts(tmp_path, monkeypatch):
    calls = []

    def fake_pull(conn, client, policy, days=8, now=None):
        calls.append(days)
        return _counts(days)

    monkeypatch.setattr(main, "whoop_pull", fake_pull)
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    with TestClient(main.create_app(_settings(tmp_path))) as c:
        r = c.post("/api/whoop/pull?days=3", headers=H)
        assert r.status_code == 200 and r.json()["ok"] is True
        assert r.json()["sleep"] == 1 and r.json()["trigger"] == "api" and r.json()["pulled_at"]
        r2 = c.post("/api/whoop/pull?days=3", headers=H)
        assert r2.status_code == 200
        body = r2.json()
        assert body["skipped"] == "rate_limited" and 0 < body["retry_after_s"] <= 61
        assert body["ok"] is True and body["last"]["trigger"] == "api"
    assert calls == [3]


def test_whoop_pull_route_reports_a_failure_and_the_cooldown_carries_it(tmp_path, monkeypatch):
    def failing_pull(conn, client, policy, days=8, now=None):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(main, "whoop_pull", failing_pull)
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    with TestClient(main.create_app(_settings(tmp_path))) as c:
        r = c.post("/api/whoop/pull?days=3", headers=H)
        assert r.status_code == 502 and "RuntimeError" in r.json()["detail"]
        r2 = c.post("/api/whoop/pull?days=3", headers=H)
        assert r2.status_code == 200
        assert r2.json()["ok"] is False and r2.json()["last"]["error"] == "RuntimeError"


@pytest.mark.parametrize("fails", [False, True])
def test_a_caller_that_waited_behind_a_long_pull_gets_its_outcome_not_a_second_pull(tmp_path, monkeypatch, fails):
    """Wave 1 review (A3): the cooldown was stamped only when a pull started,
    so a Pull latest that waited behind a pull longer than the cooldown pulled
    again. It is stamped when the pull ends as well, and the waiter's
    rate-limited reply carries that pull's outcome, a failure included."""
    calls = []

    def slow_pull(conn, client, policy, days=8, now=None):
        calls.append(days)
        time.sleep(0.8)                                  # longer than the 0.5 s cooldown below
        if fails:
            raise RuntimeError("synthetic")
        return _counts(days)

    monkeypatch.setattr(main, "whoop_pull", slow_pull)
    app = main.create_app(_settings(tmp_path))
    app.state.stopping, app.state.workers = False, set()
    app.state.conn, app.state.policy, app.state.whoop = None, None, object()
    app.state.whoop_pull_last_at, app.state.whoop_last_pull = None, None

    async def scenario():
        app.state.whoop_pull_lock = asyncio.Lock()
        return await asyncio.gather(main._whoop_pull_now(app, "api", 3, 0.5),
                                    main._whoop_pull_now(app, "api", 3, 0.5), return_exceptions=True)
    first, second = asyncio.run(scenario())
    assert calls == [3]
    assert isinstance(first, RuntimeError) if fails else first["ok"] is True
    assert second["skipped"] == "rate_limited" and second["last"] == app.state.whoop_last_pull
    assert second["ok"] is (not fails) and second["last"]["ok"] is (not fails)
    assert second["last"].get("error") == ("RuntimeError" if fails else None)


def test_whoop_pull_route_clamps_days(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "whoop_pull", lambda conn, client, policy, days=8, now=None: calls.append(days) or _counts(days))
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    with TestClient(main.create_app(_settings(tmp_path))) as c:
        assert c.post("/api/whoop/pull?days=5000", headers=H).status_code == 200
    assert calls == [400]


def _recomputes_after_a_pull(tmp_path, monkeypatch, trigger, dates):
    """The recompute windows (days) one pass of `trigger` runs when Whoop's
    pull reports `dates` as changed."""
    calls = []
    monkeypatch.setattr(main, "whoop_pull", lambda conn, client, policy, days=8, now=None: dict(_counts(days), dates=dates))
    monkeypatch.setattr(main, "recompute", lambda conn, policy, registry, days=3, value_window=None: calls.append(days) or {})
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    if trigger == "api":
        with TestClient(main.create_app(_settings(tmp_path))) as c:
            assert c.post("/api/whoop/pull?days=3", headers=H).json()["ok"] is True
        return calls
    monkeypatch.setattr(main, "ingest_sources", lambda app: {})
    monkeypatch.setattr(main.watchdog, "check", lambda *a, **k: [])
    monkeypatch.setattr(main, "wake_plan", lambda now_local, window, poll_s, landed: ("pull", poll_s))
    sleeps = []

    async def fake_sleep(s):                    # the hourly tick's first sleep returns; the next sleep stops the loop
        sleeps.append(s)
        if trigger == "wake-window" or len(sleeps) > 1:
            raise asyncio.CancelledError
    monkeypatch.setattr(main, "SLEEP", fake_sleep)
    app = main.create_app(_settings(tmp_path, wake_poll_minutes=15))
    app.state.stopping, app.state.workers = False, set()
    app.state.conn, app.state.policy, app.state.registry = db.connect_memory(), SimpleNamespace(zone=DUBAI), None
    app.state.whoop, app.state.whoop_pull_last_at, app.state.whoop_last_pull = object(), None, None
    loop = main._whoop_wake_loop if trigger == "wake-window" else main._background_loop

    async def scenario():
        app.state.whoop_pull_lock = asyncio.Lock()
        await loop(app)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scenario())
    return calls


@pytest.mark.parametrize("trigger, unchanged, changed", [("api", [], [3]), ("wake-window", [], [2]),
                                                         ("hourly", [3], [3, 2])])
def test_a_pull_recomputes_only_when_it_changed_dates(tmp_path, monkeypatch, trigger, unchanged, changed):
    """Wave 1 review (A3): every pull ran a recompute, which drops today's
    cached narrative, even when Whoop returned nothing new (an unchanged
    record dirties no date): up to 20 times a morning on a night Whoop has
    not scored. The hourly tick keeps its own 3-day window either way."""
    assert _recomputes_after_a_pull(tmp_path / "unchanged", monkeypatch, trigger, []) == unchanged
    assert _recomputes_after_a_pull(tmp_path / "changed", monkeypatch, trigger, [str(DAY)]) == changed


# ---------------------------------------------------------------- A24 client allowlist and TLS fail-closed

from datetime import timedelta                                                  # noqa: E402

LAN_PEER = ("192.168.77.50", 50000)          # synthetic private address, never the Mac's own


def _client(tmp_path, monkeypatch, peer, own=frozenset(), **server):
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    monkeypatch.setattr(main, "own_addresses", lambda: (set(own), set()))
    return TestClient(main.create_app(_settings(tmp_path, **server)), client=peer)


@pytest.mark.parametrize("peer", [LAN_PEER, ("10.11.12.13", 1), ("2001:db8::9", 1), ("::ffff:192.168.77.50", 1)])
def test_a_lan_or_internet_peer_gets_403_on_every_path_by_default(tmp_path, monkeypatch, peer):
    with _client(tmp_path, monkeypatch, peer) as c:
        for path, headers in (("/api/health", {}), ("/", {}), ("/api/today", H), ("/manifest.webmanifest", {}),
                              ("/ingest", H)):
            r = c.get(path, headers=headers)
            assert r.status_code == 403, (path, r.status_code)
            assert r.json() == {"detail": "client not allowed"}
            assert r.headers.get("cache-control") == "no-store"


@pytest.mark.parametrize("peer", [("127.0.0.1", 1), ("127.9.9.9", 1), ("::1", 1), ("::ffff:127.0.0.1", 1),
                                  ("100.100.1.2", 1), ("100.127.255.254", 1), ("fd7a:115c:a1e0::1234", 1)])
def test_loopback_and_tailnet_peers_are_served(tmp_path, monkeypatch, peer):
    with _client(tmp_path, monkeypatch, peer) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/actions", headers=H).status_code == 200
        assert c.get("/api/actions").status_code == 401          # the token check still runs behind the gate


@pytest.mark.parametrize("peer", [("100.63.255.255", 1), ("100.128.0.1", 1), ("fd7a:115c:a1e1::1", 1)])
def test_addresses_just_outside_the_tailnet_ranges_are_refused(tmp_path, monkeypatch, peer):
    with _client(tmp_path, monkeypatch, peer) as c:
        assert c.get("/api/health").status_code == 403


def test_the_macs_own_addresses_are_served_and_a_link_local_must_match_its_zone(tmp_path, monkeypatch):
    own = {"fe80::1234:5678%en0", "198.51.100.36"}
    for peer, code in ((("fe80::1234:5678%en0", 1), 200),     # the Mac's own link-local on its own link
                       (("198.51.100.36", 1), 200),            # the Mac's own LAN address
                       (("fe80::1234:5678%en1", 1), 403),      # same address, another link: another machine (point 2)
                       (("fe80::1234:5678", 1), 403),          # no zone: the link cannot be checked
                       (("fe80::dead:beef%en0", 1), 403)):     # a neighbour on the Mac's link
        with _client(tmp_path, monkeypatch, peer, own=own) as c:
            assert c.get("/api/health").status_code == code, peer


def test_failed_interface_enumeration_keeps_loopback_and_refuses_the_lan(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="heliosd"):
        with _client(tmp_path, monkeypatch, ("127.0.0.1", 1), own=frozenset()) as c:
            assert c.get("/api/health").status_code == 200
        with _client(tmp_path, monkeypatch, LAN_PEER, own=frozenset()) as c:
            assert c.get("/api/health").status_code == 403
    assert any("interface addresses" in r.getMessage() for r in caplog.records)


def test_a_miss_rereads_the_interfaces_at_most_every_few_seconds(tmp_path, monkeypatch):
    reads = []
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    monkeypatch.setattr(main, "own_addresses", lambda: reads.append(1) or (set(), set()))
    with TestClient(main.create_app(_settings(tmp_path)), client=LAN_PEER) as c:
        for _ in range(5):
            assert c.get("/api/health").status_code == 403
    assert len(reads) == 1


def test_rollback_switch_serves_everyone_and_says_so(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="heliosd"):
        with _client(tmp_path, monkeypatch, LAN_PEER, allow_clients="any") as c:
            assert c.get("/api/health").status_code == 200
    assert any("allow_clients" in r.getMessage() for r in caplog.records)


def test_an_unknown_allow_clients_value_is_a_config_error(tmp_path):
    from heliosd.config import ConfigError
    with pytest.raises(ConfigError, match="allow_clients"):
        Settings(raw={"server": {"allow_clients": "lan"}}).allow_clients
    assert Settings().allow_clients == "tailnet"


def test_non_ip_peer_is_treated_as_in_process(tmp_path, monkeypatch):
    # Starlette's TestClient presents ("testclient", 50000); with proxy headers
    # off, uvicorn always presents the socket peer, so a name is in-process.
    with _client(tmp_path, monkeypatch, ("testclient", 50000)) as c:
        assert c.get("/api/health").status_code == 200


def test_refusals_are_logged_by_address_once_per_window_without_the_request(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="heliosd"):
        with _client(tmp_path, monkeypatch, ("192.168.77.77", 1)) as c:
            for _ in range(3):
                c.get("/api/today?probe=synthetic-canary", headers=H)
    refusals = [r.getMessage() for r in caplog.records if "192.168.77.77" in r.getMessage()]
    assert len(refusals) == 1
    assert "synthetic-canary" not in refusals[0] and TOKEN not in refusals[0] and "/api/today" not in refusals[0]


IFCONFIG_SAMPLE = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
\tinet6 fe80::1%lo0 prefixlen 64 scopeid 0x1
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet6 fe80::aa:bb:cc:dd%en0 prefixlen 64 secured scopeid 0xc
\tinet 192.0.2.36 netmask 0xffffff00 broadcast 192.0.2.255
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
\tinet 100.64.0.9 --> 100.64.0.9 netmask 0xffffffff
\tinet6 fd7a:115c:a1e0::1234:5678 prefixlen 48
eth1: flags=4163<UP,BROADCAST,RUNNING,MULTICAST>  mtu 1500
        inet addr:198.51.100.7  Bcast:198.51.100.255  Mask:255.255.255.0
"""


def test_ifconfig_parser_keeps_link_local_zones_and_drops_the_rest():
    assert main.parse_ifconfig(IFCONFIG_SAMPLE) == {
        "127.0.0.1", "::1", "fe80::1%lo0", "fe80::aa:bb:cc:dd%en0", "192.0.2.36", "100.64.0.9",
        "fd7a:115c:a1e0::1234:5678", "198.51.100.7"}
    assert main.parse_ifconfig("") == set()


def test_own_addresses_survives_a_missing_or_hanging_ifconfig(monkeypatch):
    import subprocess as sp
    monkeypatch.setattr(main, "_ifconfig_path", lambda: None)
    assert main.own_addresses() == (set(), set())
    monkeypatch.setattr(main, "_ifconfig_path", lambda: "/bin/ifconfig-synthetic")

    def hang(*a, **k):
        raise sp.TimeoutExpired(cmd="ifconfig", timeout=5)
    monkeypatch.setattr(main.subprocess, "run", hang)
    assert main.own_addresses() == (set(), set())


def _ifconfig(monkeypatch, text):
    """Serve `text` as this Mac's ifconfig output to the real reader."""
    real = main.subprocess.run
    monkeypatch.setattr(main, "_ifconfig_path", lambda: "/sbin/ifconfig-synthetic")
    monkeypatch.setattr(main.subprocess, "run", lambda args, *a, **k: (
        main.subprocess.CompletedProcess(args, 0, stdout=text, stderr="")
        if args == ["/sbin/ifconfig-synthetic"] else real(args, *a, **k)))


TAILNET_PEER = ("100.64.0.20", 50000)        # synthetic tailnet-range peer


def test_a_tailnet_range_peer_is_served_only_on_the_macs_tailscale_address(tmp_path, monkeypatch):
    """Wave 1 review: some hotel, carrier and office LANs are numbered from
    100.64.0.0/10, and a source in it can be spoofed while tailscaled is down.
    A peer from the range is served only when the connection arrived on this
    Mac's own Tailscale (utun) address; before, the range alone sufficed."""
    _ifconfig(monkeypatch, IFCONFIG_SAMPLE)          # en0 192.0.2.36, utun4 100.64.0.9 and fd7a:115c:a1e0::1234:5678
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    lan, tailscale = "http://192.0.2.36:8420", "http://100.64.0.9:8420"     # the local address the peer reached
    batch = {"batch_id": "synthetic-1", "samples": [], "sync_path": "bridge"}
    with TestClient(main.create_app(_settings(tmp_path)), client=TAILNET_PEER) as c:
        for path, headers in (("/api/health", {}), ("/", {}), ("/api/today", H)):
            r = c.get(lan + path, headers=headers)
            assert r.status_code == 403, (path, r.status_code)
            assert r.json() == {"detail": "client not allowed"}
        # Refused before the body is read: the batch is never ingested.
        assert c.post(lan + "/ingest", json=batch, headers=H).status_code == 403
        assert db.fetchall(c.app.state.conn, "SELECT COUNT(*) FROM sync_log")[0][0] == 0
        assert c.get(tailscale + "/api/health").status_code == 200
        assert c.get(tailscale + "/api/actions", headers=H).status_code == 200
        assert c.post(tailscale + "/ingest", json=batch, headers=H).status_code == 200
        assert db.fetchall(c.app.state.conn, "SELECT COUNT(*) FROM sync_log")[0][0] == 1


def test_the_tailscale_address_check_covers_ipv6_mapped_locals_and_a_late_tailscaled(monkeypatch):
    _ifconfig(monkeypatch, IFCONFIG_SAMPLE)
    assert main.own_addresses()[1] == {"100.64.0.9", "fd7a:115c:a1e0::1234:5678"}   # utun4 only, tailnet ranges only
    real_read, reads = main.own_addresses, []

    def interfaces():                                # tailscaled comes up after the first read
        reads.append(1)
        return ({"192.0.2.36"}, set()) if len(reads) == 1 else real_read()
    monkeypatch.setattr(main, "own_addresses", interfaces)
    monkeypatch.setattr(main, "OWN_ADDRESS_REFRESH_MIN_S", 0.0)
    gate = main.ClientGate("tailnet")
    v4, v6 = ("100.64.0.9", 8420), ("fd7a:115c:a1e0::1234:5678", 8420)

    async def decide():
        return [await gate.allowed(TAILNET_PEER, v4),                                   # no Tailscale address yet
                await gate.allowed(TAILNET_PEER, v4),                                   # the next miss re-reads it
                await gate.allowed(("fd7a:115c:a1e0::20", 1), v6),
                await gate.allowed(("::ffff:100.64.0.20", 1), ("::ffff:100.64.0.9", 8420)),   # dual-stack socket
                await gate.allowed(("fd7a:115c:a1e0::20", 1), ("2001:db8::36", 8420)),     # arrived elsewhere
                await gate.allowed(TAILNET_PEER, ("192.0.2.36", 8420)),
                await gate.allowed(TAILNET_PEER, ("testserver", 80)),                   # no local IP: in-process
                await gate.allowed(("100.64.0.9", 1), ("100.64.0.9", 8420))]            # the Mac to its own tailnet name
    assert asyncio.run(decide()) == [False, True, True, True, False, False, True, True]


def _make_pair(tmp_path, name):
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1)).sign(key, hashes.SHA256()))
    c, k = tmp_path / f"{name}.pem", tmp_path / f"{name}-key.pem"
    c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    return str(c), str(k)


def test_tls_returns_a_loadable_pair_and_none_when_unset(tmp_path):
    cert, key = _make_pair(tmp_path, "a")
    assert Settings(raw={"server": {"tls_cert": cert, "tls_key": key}}).tls == (cert, key)
    assert Settings(raw={"server": {}}).tls is None
    assert Settings(raw={"server": {"tls_cert": "", "tls_key": ""}}).tls is None


def test_tls_fails_closed_on_every_unusable_pair(tmp_path):
    from heliosd.config import ConfigError
    cert_a, key_a = _make_pair(tmp_path, "a")
    _cert_b, key_b = _make_pair(tmp_path, "b")
    garbage = tmp_path / "garbage.pem"
    garbage.write_text("not a certificate")
    cases = [({"tls_cert": str(tmp_path / "missing.pem"), "tls_key": key_a}, "missing.pem"),
             ({"tls_cert": cert_a}, "both"),
             ({"tls_key": key_a}, "both"),
             ({"tls_cert": str(tmp_path), "tls_key": key_a}, "not a file"),
             ({"tls_cert": str(garbage), "tls_key": key_a}, "garbage.pem"),
             ({"tls_cert": cert_a, "tls_key": key_b}, "do not load")]
    for server, needle in cases:
        with pytest.raises(ConfigError, match=re.escape(needle)):
            Settings(raw={"server": server}).tls


@pytest.fixture()
def _restore_logging():
    root = logging.getLogger()
    handlers, level = list(root.handlers), logging.getLogger("heliosd").level
    yield
    for h in list(root.handlers):
        if h not in handlers:
            root.removeHandler(h)
    logging.getLogger("heliosd").setLevel(level)


def test_run_exits_2_with_the_message_on_a_bad_tls_config(tmp_path, monkeypatch, capsys, _restore_logging):
    import uvicorn
    monkeypatch.setattr(main, "load_settings",
                        lambda path=None: Settings(raw={"server": {"ingest_token": TOKEN, "tls_cert": str(tmp_path / "nope.pem"),
                                                                   "tls_key": str(tmp_path / "nope-key.pem")}}))
    monkeypatch.setattr("sys.argv", ["heliosd"])
    # Never let this test reach a real bind: the old code served plain HTTP on
    # the daemon's port from inside pytest.
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("uvicorn.run reached")))
    with pytest.raises(SystemExit) as e:
        main.run()
    assert e.value.code == 2
    assert "nope.pem" in capsys.readouterr().err


def test_run_serves_tls_with_proxy_headers_off(tmp_path, monkeypatch, _restore_logging):
    import uvicorn
    cert, key = _make_pair(tmp_path, "a")
    seen = {}
    monkeypatch.setattr(main, "load_settings",
                        lambda path=None: Settings(raw={"server": {"ingest_token": TOKEN, "tls_cert": cert, "tls_key": key},
                                                        "storage": {"db_path": str(tmp_path / "helios.duckdb")}}))
    monkeypatch.setattr("sys.argv", ["heliosd"])
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    main.run()
    assert seen["proxy_headers"] is False
    assert (seen["ssl_certfile"], seen["ssl_keyfile"]) == (cert, key)
    assert seen["log_config"]["formatters"]["access"]["()"] == "heliosd.main.QuietAccessFormatter"
