"""Hardening review fix (2026-10-28, independent review finding 3): after H3 a
change of a noted row ("so far", "other device") or of the verdict no longer
rebuilds the brief, but the phone must still see current text. Until
morning_window ends, the morning step's tick rewrites glance.json quietly when
its revision key still matches today's data but its content does not: no new
brief, no revised_at, so no "brief updated". Synthetic values only."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from heliosd.narrative import brief, glance

DAY = date(2026, 10, 24)
ON_DAY = datetime(2026, 10, 24, 3, 30, tzinfo=timezone.utc)     # 07:30 in Dubai


class Policy:
    zone = ZoneInfo("Asia/Dubai")


def _sig(metric, value, state="neutral"):
    return {"metric": metric, "value": value, "state": state, "fallback": False}


def _night(sleep=6.0, hrv=50.0):
    return [_sig("recovery_score", 70.0, "favorable"), _sig("sleep_duration", sleep, "in_progress"),
            _sig("hrv_rmssd", hrv), _sig("resting_hr", 58.0)]


def _setup(tmp_path, monkeypatch, signals, verdict="v"):
    monkeypatch.setattr(brief, "signals_for", lambda *a, **k: signals)
    monkeypatch.setattr(brief, "make_verdict", lambda *a, **k: verdict)
    monkeypatch.setattr(brief, "awaiting_markers", lambda s: [])
    monkeypatch.setattr(brief, "generation_of", lambda conn, day: 7)
    return tmp_path / "glance.json", tmp_path / "brief_status.json"


def test_a_moved_running_total_is_rewritten_quietly(tmp_path, monkeypatch):
    f, status = _setup(tmp_path, monkeypatch, _night(sleep=6.5))
    assert glance.record_glance(f, DAY, _night(sleep=6.0), "v", [], True, now=ON_DAY, generation=5)
    later = ON_DAY + timedelta(minutes=30)
    assert brief.glance_quiet_refresh(None, Policy(), status, later) is True
    g = json.loads(f.read_text(encoding="utf-8"))
    assert g["rows"][1]["text"] == "6h 30m" and g["revised_at"] is None and g["generation"] == 7


def test_nothing_is_written_when_nothing_moved(tmp_path, monkeypatch):
    f, status = _setup(tmp_path, monkeypatch, _night())
    assert glance.record_glance(f, DAY, _night(), "v", [], True, now=ON_DAY, generation=5)
    before = f.stat().st_mtime_ns
    assert brief.glance_quiet_refresh(None, Policy(), status, ON_DAY + timedelta(minutes=30)) is False
    assert f.stat().st_mtime_ns == before


def test_a_change_of_the_night_is_left_to_the_rebuild(tmp_path, monkeypatch):
    f, status = _setup(tmp_path, monkeypatch, _night(hrv=44.0))
    assert glance.record_glance(f, DAY, _night(), "v", [], True, now=ON_DAY, generation=5)
    before = f.stat().st_mtime_ns
    assert brief.glance_quiet_refresh(None, Policy(), status, ON_DAY + timedelta(minutes=30)) is False
    assert f.stat().st_mtime_ns == before


def test_the_verdict_alone_is_rewritten_quietly(tmp_path, monkeypatch):
    f, status = _setup(tmp_path, monkeypatch, _night(), verdict="w")
    assert glance.record_glance(f, DAY, _night(), "v", [], True, now=ON_DAY, generation=5)
    assert brief.glance_quiet_refresh(None, Policy(), status, ON_DAY + timedelta(minutes=30)) is True
    g = json.loads(f.read_text(encoding="utf-8"))
    assert g["verdict"] == "w" and g["revised_at"] is None


def test_the_morning_step_calls_it_when_the_night_did_not_change(tmp_path, monkeypatch):
    from heliosd import main
    from tests.test_rank3_rebuild import _published_at_0730
    from tests.test_rank3_settle import _tick, _z
    app = _published_at_0730(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(main, "glance_quiet_refresh", lambda *a, **k: calls.append("quiet") or True)
    _tick(app, monkeypatch, _z(7, 50))                    # nothing of the night changed
    assert calls == ["quiet"]
    monkeypatch.setattr(main, "glance_outdated", lambda *a, **k: True)
    monkeypatch.setattr(main, "generate_brief", lambda *a, **k: calls.append("rebuild"))
    _tick(app, monkeypatch, _z(7, 55))                    # the night changed: the rebuild, not the quiet path
    assert calls[1:2] == ["rebuild"] and "quiet" not in calls[1:]
