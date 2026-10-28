"""Hardening round 2026-10-28, H3: "Helios: brief updated" only for a real
change of the night. The phone posts it when glance.json's revised_at moves.
revised_at used to move at ANY content change after the first complete
summary, so a HealthKit batch that moved a running total ("so far"), another
device's stand-in value ("other device") or the verdict line could post it
with no Whoop revision. Now revised_at, and the morning step's rebuild check,
follow one revision key: complete, plus the text and state of every row that
carries no note. glance.json is still rewritten when the rest changes, so the
phone shows current text, but quietly. Synthetic values only."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from heliosd.narrative import brief, glance

DAY = date(2026, 10, 24)
ON_DAY = datetime(2026, 10, 24, 3, 30, tzinfo=timezone.utc)     # 07:30 in Dubai


def _sig(metric, value, state="neutral", fallback=False):
    return {"metric": metric, "value": value, "state": state, "fallback": fallback}


def _night(hrv=50.0, steps_state="in_progress", rhr_value=58.0, rhr_fallback=False, sleep=7.2):
    return [_sig("recovery_score", 70.0, "favorable"), _sig("sleep_duration", sleep, steps_state),
            _sig("hrv_rmssd", hrv), _sig("resting_hr", rhr_value, "fallback" if rhr_fallback else "neutral",
                                         fallback=rhr_fallback)]


def _rec(f):
    return json.loads(f.read_text(encoding="utf-8"))


def _write(f, signals, verdict, minutes, generation):
    t = ON_DAY + timedelta(minutes=minutes)
    assert glance.record_glance(f, DAY, signals, verdict, [], True, now=t, generation=generation)
    return t.isoformat(timespec="seconds")


def test_a_running_total_that_moves_is_rewritten_quietly(tmp_path):
    f = tmp_path / "glance.json"
    _write(f, _night(sleep=6.0), "v", 0, 1)
    _write(f, _night(sleep=6.5), "v", 20, 2)                      # a HealthKit batch, "so far" moved
    g = _rec(f)
    assert [r["text"] for r in g["rows"]][1] == "6h 30m" and g["rows"][1]["note"] == "so far"
    assert g["revised_at"] is None


def test_a_stand_in_value_that_moves_is_rewritten_quietly(tmp_path):
    f = tmp_path / "glance.json"
    _write(f, _night(rhr_value=58.0, rhr_fallback=True, steps_state="neutral"), "v", 0, 1)
    _write(f, _night(rhr_value=61.0, rhr_fallback=True, steps_state="neutral"), "v", 20, 2)
    g = _rec(f)
    assert g["rows"][3]["text"] == "61 bpm" and g["rows"][3]["note"] == "other device"
    assert g["revised_at"] is None


def test_the_verdict_line_alone_is_no_revision(tmp_path):
    f = tmp_path / "glance.json"
    _write(f, _night(steps_state="neutral"), "Mostly steady.", 0, 1)
    _write(f, _night(steps_state="neutral"), "One signal is off baseline.", 20, 2)
    g = _rec(f)
    assert g["verdict"] == "One signal is off baseline." and g["revised_at"] is None


def test_a_whoop_revision_of_a_shown_number_still_moves_revised_at(tmp_path):
    f = tmp_path / "glance.json"
    _write(f, _night(hrv=50.0), "v", 0, 1)
    t = _write(f, _night(hrv=44.0), "v", 40, 2)
    assert _rec(f)["revised_at"] == t


def test_a_stand_in_replaced_by_the_owner_s_own_value_is_a_revision(tmp_path):
    f = tmp_path / "glance.json"
    _write(f, _night(rhr_value=61.0, rhr_fallback=True, steps_state="neutral"), "v", 0, 1)
    t = _write(f, _night(rhr_value=57.0, steps_state="neutral"), "v", 40, 2)
    assert _rec(f)["revised_at"] == t


def test_a_quiet_rewrite_keeps_an_earlier_revision(tmp_path):
    f = tmp_path / "glance.json"
    _write(f, _night(hrv=50.0, sleep=6.0), "v", 0, 1)
    t = _write(f, _night(hrv=44.0, sleep=6.0), "v", 40, 2)
    _write(f, _night(hrv=44.0, sleep=6.5), "w", 60, 3)
    assert _rec(f)["revised_at"] == t


def test_the_revision_key_reads_old_files_without_notes():
    old = {"complete": True, "rows": [{"metric": "sleep_duration", "text": "6h", "state": "in_progress"}]}
    new = {"complete": True, "rows": [{"metric": "sleep_duration", "text": "6h 30m", "state": "in_progress",
                                       "note": "so far"}]}
    assert glance.revision_key(old) == glance.revision_key(new)
    assert glance.revision_key(old) != glance.revision_key({**old, "complete": False})


def test_the_morning_step_check_follows_the_same_key(tmp_path, monkeypatch):
    f = tmp_path / "glance.json"
    status = tmp_path / "brief_status.json"
    _write(f, _night(sleep=6.0), "v", 0, 1)
    now = ON_DAY + timedelta(minutes=20)

    class Policy:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo("Asia/Dubai")

    def outdated(signals, verdict="v"):
        monkeypatch.setattr(brief, "signals_for", lambda *a, **k: signals)
        monkeypatch.setattr(brief, "make_verdict", lambda *a, **k: verdict)
        monkeypatch.setattr(brief, "awaiting_markers", lambda s: [])
        return brief.glance_outdated(None, Policy(), status, now)

    assert outdated(_night(sleep=6.0)) is False
    assert outdated(_night(sleep=6.5)) is False                  # a running total moved
    assert outdated(_night(sleep=6.0), verdict="w") is False     # the verdict alone
    assert outdated(_night(hrv=44.0, sleep=6.0)) is True         # Whoop revised HRV
    f.unlink()
    assert outdated(_night()) is True                            # no glance at all
