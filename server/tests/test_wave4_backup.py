"""Wave 4 (2026-10-18): a missed or aborted nightly backup is made up the same
day. launchd runs `run-if-due` at several times a day; only the first run of a
day without a backup does the work."""
from __future__ import annotations

from datetime import datetime

import pytest

from tests.test_wave1_backup import _export, tool  # noqa: F401 - the fixture


def _arm(tool, tmp_path, monkeypatch, calls):
    dest, _ = _export(tmp_path)

    def export():
        calls.append("export")
        return dest
    monkeypatch.setattr(tool, "_export_via_daemon", export)
    monkeypatch.setattr(tool, "_cfg", lambda: dict(tool.DEFAULTS, remote=""))


def test_run_if_due_runs_when_today_has_no_backup_and_then_stays_quiet(tool, tmp_path, monkeypatch):
    calls: list[str] = []
    _arm(tool, tmp_path, monkeypatch, calls)
    now = datetime.now().replace(hour=10, minute=30)
    assert tool.cmd_run_if_due(now=now) == 0
    assert calls == ["export"]
    log = (tmp_path / "logs" / "backup.log").read_text()
    # A second slot the same day: no export, no log line.
    assert tool.cmd_run_if_due(now=now.replace(hour=14)) == 0
    assert calls == ["export"]
    assert (tmp_path / "logs" / "backup.log").read_text() == log


def test_yesterdays_last_ok_does_not_count_for_today(tool, tmp_path, monkeypatch):
    calls: list[str] = []
    _arm(tool, tmp_path, monkeypatch, calls)
    (tmp_path / "backup" / "LAST_OK").write_text("2026-10-17T02:30:08\n")
    assert tool.ok_today(datetime(2026, 10, 18, 10, 30)) is False
    assert tool.ok_today(datetime(2026, 10, 17, 23, 0)) is True
    tool.cmd_run_if_due(now=datetime(2026, 10, 18, 10, 30))
    assert calls == ["export"]


def test_an_aborted_slot_is_retried_at_the_next_one(tool, tmp_path, monkeypatch):
    calls: list[str] = []
    _arm(tool, tmp_path, monkeypatch, calls)
    real = tool._export_via_daemon

    def down():
        calls.append("down")
        tool.abort(3, "daemon export failed: synthetic")
    monkeypatch.setattr(tool, "_export_via_daemon", down)
    now = datetime.now().replace(hour=2, minute=30)
    with pytest.raises(SystemExit) as e:
        tool.cmd_run_if_due(now=now)
    assert e.value.code == 3
    assert not tool.ok_today(now)
    monkeypatch.setattr(tool, "_export_via_daemon", real)
    assert tool.cmd_run_if_due(now=now.replace(hour=10)) == 0
    assert calls == ["down", "export"]
    assert tool.ok_today(now)


def test_a_damaged_last_ok_never_blocks_a_run(tool, tmp_path, monkeypatch):
    calls: list[str] = []
    _arm(tool, tmp_path, monkeypatch, calls)
    (tmp_path / "backup" / "LAST_OK").write_text("not a date\n")
    assert tool.ok_today(datetime(2026, 10, 18)) is False
    tool.cmd_run_if_due(now=datetime(2026, 10, 18, 10, 30))
    assert calls == ["export"]


def test_the_cli_knows_run_if_due(tool, monkeypatch):
    seen = []
    monkeypatch.setattr(tool, "cmd_run_if_due", lambda: seen.append(1) or 0)
    assert tool.main(["helios_backup.py", "run-if-due"]) == 0 and seen == [1]


def test_an_export_only_run_with_a_remote_never_touches_last_ok(tool, tmp_path, monkeypatch):
    dest, _ = _export(tmp_path)
    monkeypatch.setattr(tool, "_export_via_daemon", lambda: dest)
    monkeypatch.setattr(tool, "_cfg", lambda: dict(tool.DEFAULTS, remote="user@host"))
    assert tool.cmd_run(sync=False, now=datetime(2026, 10, 18, 10)) == 0
    assert not (tmp_path / "backup" / "LAST_OK").exists()
    assert "LAST_OK unchanged" in (tmp_path / "logs" / "backup.log").read_text()
