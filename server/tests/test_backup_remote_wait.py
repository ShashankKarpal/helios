"""Backup runs in a dark wake (2026-11-24). launchd runs a slot it missed in
sleep at the next wake, often a dark wake of about 2 s, before the network or
the remote answers, and the Mac goes back to sleep mid-run. On 2026-11-23 the
02:30 slot ran in such a wake, exported, then sat in its first ssh call while
the Mac slept and aborted at a later dark wake with exit 2 ("remote
unreachable"); launchd showed the job as failed until the next slot made the
day's backup. The off-Mac copy now waits for the remote, timed on a clock that
keeps counting in sleep. A wait the Mac slept through ends with a "skipped"
line, exit 0 and LAST_OK unchanged, so the next slot tries again; a remote that
stays down while the Mac is awake still aborts with exit 2."""
from __future__ import annotations

import subprocess
from datetime import date, datetime

import pytest

from tests.test_wave1_backup import _export, tool  # noqa: F401 - the fixture
from tests.test_wave4_backup import _freeze

CFG = {"remote": "u@h", "remote_dir": "Backups/helios", "include_overlays": False}


class FakeMac:
    """The tool's two clocks: `total` keeps counting while the Mac sleeps,
    `awake` stops. tick() is time passing awake; pause() is the tool's pause
    between tries, plus any sleep queued in `pause_sleeps` (one entry per
    pause); sleep() is the Mac asleep."""

    def __init__(self):
        self.total, self.awake = 5000.0, 4000.0
        self.pause_sleeps: list[float] = []

    def clocks(self):
        return self.total, self.awake

    def tick(self, s):
        self.total += s
        self.awake += s

    def pause(self, s):
        self.tick(s)
        if self.pause_sleeps:
            self.sleep(self.pause_sleeps.pop(0))

    def sleep(self, s):
        self.total += s


class R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _remote(mac, want, answers):
    """A fake subprocess.run. Each ssh mkdir try takes the next answer: True (it
    worked), False (it failed), "hang" (it uses its whole timeout awake, then
    times out), or a number: the Mac sleeps that many seconds inside the try,
    which fails. Every other try costs 1 s awake. Past the last answer every
    try fails. run.timeouts keeps each try's timeout."""
    tries = []
    pending = iter(answers)

    def run(cmd, **kw):
        if cmd[0] == "ssh" and "mkdir -p" in cmd[-1]:
            a = next(pending, False)
            tries.append(a)
            run.timeouts.append(kw.get("timeout"))
            if a == "hang":
                mac.tick(kw["timeout"])
                raise subprocess.TimeoutExpired(cmd, kw["timeout"])
            mac.tick(1)
            if not isinstance(a, bool):
                mac.sleep(a)
                return R(255, "", "ssh: connect to host h port 22: Operation timed out")
            return R(0) if a else R(255, "", "ssh: connect to host h port 22: No route to host")
        if cmd[-1].startswith("ls -1 "):
            return R(0, "2026-10-08\n")
        if "shasum" in cmd[-1]:
            return R(0, "\n".join(want) + "\n")
        return R(0)
    run.timeouts = []
    return run, tries


def _arm(tool, monkeypatch):
    mac = FakeMac()
    monkeypatch.setattr(tool, "_clocks", mac.clocks)
    monkeypatch.setattr(tool, "_pause", mac.pause)
    return mac


def _want(m):
    return [spec["sha256"] for spec in m["tables"].values()]


def _log(tmp_path):
    return (tmp_path / "logs" / "backup.log").read_text()


def test_a_remote_that_answers_within_the_wait_gets_the_copy(tool, tmp_path, monkeypatch):
    dest, m = _export(tmp_path)
    mac = _arm(tool, monkeypatch)
    run, tries = _remote(mac, _want(m), [False, False, False, True])
    assert tool._sync(CFG, dest, run=run, today=date(2026, 10, 8)) == []
    assert tries == [False, False, False, True]
    log = _log(tmp_path)
    assert "remote reachable after 10 s (4 tries)" in log        # four 1 s tries, three 2 s pauses
    assert "remote ok" in log and "aborted" not in log


def test_a_wait_the_mac_slept_through_is_skipped_with_exit_0_and_the_next_slot_retries(tool, tmp_path, monkeypatch):
    """The 2026-11-23 shape: the Mac goes back to sleep inside the first ssh try;
    the fresh try at the next 2-second dark wake is slept through too."""
    dest, m = _export(tmp_path)
    monkeypatch.setattr(tool, "_export_via_daemon", lambda: dest)
    monkeypatch.setattr(tool, "_cfg", lambda: dict(tool.DEFAULTS, **CFG))
    now = _freeze(tool, monkeypatch, datetime(2026, 10, 8, 2, 31))  # the fixture export's date, a Thursday
    mac = _arm(tool, monkeypatch)
    run, tries = _remote(mac, _want(m), [1053, 1059])
    assert tool.cmd_run_if_due(now=now, run=run) == 0             # launchd records no failure
    assert tries == [1053, 1059]                                  # one fresh wait, then no more tries
    log = _log(tmp_path)
    assert "skipped: remote unreachable while the Mac slept (2112 s asleep in a 2114 s wait, 2 tries)" in log
    assert "aborted" not in log and "backup ok" not in log
    assert not (tmp_path / "backup" / "LAST_OK").exists() and not tool.ok_today(now)
    # The next slot, awake with the remote back, makes the day's backup.
    run2, _ = _remote(mac, _want(m), [True])
    assert tool.cmd_run_if_due(now=now.replace(hour=9, minute=30), run=run2) == 0
    assert tool.ok_today(now) and "backup ok 2026-10-08" in _log(tmp_path)


def test_a_remote_down_while_the_mac_stays_awake_still_aborts_with_exit_2_after_a_bounded_wait(tool, tmp_path, monkeypatch):
    dest, m = _export(tmp_path)
    mac = _arm(tool, monkeypatch)
    t0 = mac.total
    run, tries = _remote(mac, _want(m), [False] * 100)
    with pytest.raises(SystemExit) as e:
        tool._sync(CFG, dest, run=run, today=date(2026, 10, 8))
    assert e.value.code == 2
    waited = mac.total - t0
    assert tool.REMOTE_WAIT_S <= waited <= tool.REMOTE_WAIT_S + 1 + tool.REMOTE_POLL_S   # one try and one pause past it at most
    assert len(tries) > 1
    log = _log(tmp_path)
    assert log.rstrip().endswith("aborted: remote unreachable") and "skipped" not in log


def test_a_nap_earns_one_fresh_wait_and_a_real_outage_still_aborts_with_exit_2(tool, tmp_path, monkeypatch):
    dest, m = _export(tmp_path)
    mac = _arm(tool, monkeypatch)
    t0 = mac.total
    mac.pause_sleeps = [10]                                       # the lid closed for 10 s during the wait
    run, _ = _remote(mac, _want(m), [False] * 100)
    with pytest.raises(SystemExit) as e:
        tool._sync(CFG, dest, run=run, today=date(2026, 10, 8))
    assert e.value.code == 2
    assert mac.total - t0 >= 2 * tool.REMOTE_WAIT_S               # the wait with the nap, then a whole awake one
    assert "aborted: remote unreachable" in _log(tmp_path) and "skipped" not in _log(tmp_path)


def test_a_full_wake_after_a_long_sleep_gets_a_fresh_try_and_the_copy(tool, tmp_path, monkeypatch):
    """Review round 1: the Mac sleeps 6 h inside the first try; the owner opens
    the lid, that try fails at the wake, and the network works by then."""
    dest, m = _export(tmp_path)
    mac = _arm(tool, monkeypatch)
    run, tries = _remote(mac, _want(m), [21600, True])
    assert tool._sync(CFG, dest, run=run, today=date(2026, 10, 8)) == []
    assert tries == [21600, True]
    log = _log(tmp_path)
    assert "remote reachable after 21602 s (2 tries)" in log and "remote ok" in log and "skipped" not in log


def test_a_full_wake_after_a_long_sleep_with_the_remote_down_aborts_with_exit_2(tool, tmp_path, monkeypatch):
    dest, m = _export(tmp_path)
    mac = _arm(tool, monkeypatch)
    a0 = mac.awake
    run, _ = _remote(mac, _want(m), [21600])                      # then every try fails, the Mac awake
    with pytest.raises(SystemExit) as e:
        tool._sync(CFG, dest, run=run, today=date(2026, 10, 8))
    assert e.value.code == 2
    assert mac.awake - a0 >= tool.REMOTE_WAIT_S                   # exit 2 only after a whole wait awake
    assert "aborted: remote unreachable" in _log(tmp_path) and "skipped" not in _log(tmp_path)


def test_a_wake_that_finds_the_remote_goes_on_even_when_the_wait_ran_out_in_sleep(tool, tmp_path, monkeypatch):
    """A sleep can use up the wait, but the try at the wake still counts."""
    dest, m = _export(tmp_path)
    mac = _arm(tool, monkeypatch)
    mac.pause_sleeps = [1053]                                     # asleep during the first pause
    run, tries = _remote(mac, _want(m), [False, True])
    assert tool._sync(CFG, dest, run=run, today=date(2026, 10, 8)) == []
    assert tries == [False, True]
    log = _log(tmp_path)
    assert "remote reachable after 1057 s (2 tries)" in log and "remote ok" in log and "skipped" not in log


def test_a_hung_ssh_try_counts_as_a_failed_try_and_leaves_room_for_another(tool, tmp_path, monkeypatch):
    dest, m = _export(tmp_path)
    mac = _arm(tool, monkeypatch)
    run, tries = _remote(mac, _want(m), ["hang", True])
    assert tool._sync(CFG, dest, run=run, today=date(2026, 10, 8)) == []
    assert tries == ["hang", True]
    assert all(t <= tool.REMOTE_WAIT_S / 2 for t in run.timeouts)  # review round 3: one hang never uses the whole wait
    log = _log(tmp_path)
    assert f"remote reachable after {tool.REMOTE_TRY_S + 3} s (2 tries)" in log and "remote ok" in log


def test_a_skip_is_exit_0_under_run_if_due_and_an_aborted_exit_2_for_a_manual_run(tool, tmp_path, monkeypatch):
    def skipped(**kw):
        raise tool.Skipped
    monkeypatch.setattr(tool, "cmd_run", skipped)
    assert tool.main(["helios_backup.py", "run-if-due"]) == 0      # what launchd runs
    with pytest.raises(SystemExit) as e:                           # a person or a script sees the failure
        tool.main(["helios_backup.py", "run"])
    assert e.value.code == 2
    assert _log(tmp_path).rstrip().endswith("aborted: remote unreachable (manual run, the Mac slept during the wait)")


def test_the_wait_clock_counts_sleep_and_the_awake_clock_does_not(tool, monkeypatch):
    """On macOS CLOCK_MONOTONIC_RAW and CLOCK_UPTIME_RAW are one raw counter with
    and without sleep (man clock_gettime). CLOCK_MONOTONIC is the wall clock
    minus the boot time on macOS (checked live 2026-11-24), so a clock
    correction would read as sleep; time.monotonic() stops in sleep
    (mach_absolute_time), so a wait timed with it could last hours of dark
    wakes."""
    class MacTime:
        CLOCK_MONOTONIC_RAW, CLOCK_MONOTONIC, CLOCK_UPTIME_RAW = 4, 6, 8

        @staticmethod
        def clock_gettime(clock):
            return {4: 1000.0, 6: 999.0, 8: 400.0}[clock]

        @staticmethod
        def monotonic():
            return 400.0

    monkeypatch.setattr(tool, "time", MacTime)
    assert tool._clocks() == (1000.0, 400.0)

    class OtherTime:                                              # no raw clocks: no sleep is ever seen
        CLOCK_MONOTONIC_RAW = 4

        @staticmethod
        def monotonic():
            return 7.0

    monkeypatch.setattr(tool, "time", OtherTime)
    assert tool._clocks() == (7.0, 7.0)
