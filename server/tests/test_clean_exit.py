"""heliosd exits 0 after a clean stop (2026-11-02, fleet review finding 3).

uvicorn 0.51 re-raises the signal it caught once its graceful shutdown is
done (Server.capture_signals), so a clean SIGTERM stop ended by signal 15 and
launchd showed -15 after every restart. heliosd now chooses its own exit code
from what the lifespan shutdown reported: 0 when every store worker finished
and the checkpoint and the close both succeeded, 1 otherwise. A failed startup
still exits 3 (a port already in use too), a bad TLS or allow_clients setting 2.

Each test runs a real heliosd in a subprocess: a temporary HELIOS_HOME with a
0600 helios.toml passed by --config and HELIOS_CONFIG (so the owner's file is
never a candidate), a throwaway token that is never printed, 127.0.0.1 on a
free port, plain HTTP, a fresh store with one synthetic batch, no Whoop
client, no notifications, the morning step off, the first background tick an
hour away and LM Studio at a dead port. The relay and the backup are separate
jobs and are not started."""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
import uvicorn

from .synth import synth_batch

SERVER = Path(__file__).resolve().parents[1]
CLEAN_LINE = ("'drained': True", "'checkpointed': True", "'closed': True")
PLACEHOLDER = "change-me-long-random"
LAUNCHD_GRACE_S = 5.0          # launchd sends SIGKILL this long after SIGTERM


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Daemon:
    def __init__(self, tmp: Path, *, token: str | None = None, port: int | None = None,
                 preamble: str = ""):
        self.home = tmp / "home"
        (self.home / "data").mkdir(parents=True)
        self.token = token or secrets.token_urlsafe(32)
        self.port = port or _free_port()
        self.toml = self.home / "helios.toml"
        self.toml.write_text(f"""[owner]
name = "Test"
timezone = "Asia/Dubai"

[server]
host = "127.0.0.1"
port = {self.port}
ingest_token = "{self.token}"
first_tick_seconds = 3600
background_interval_seconds = 3600

[storage]
db_path = "{self.home / 'data' / 'helios.duckdb'}"

[notifications]
macos_alerts = false

[brief]
poll_minutes = 0

[llm]
base_url = "http://127.0.0.1:9/v1"
timeout_seconds = 2

[whoop]
enabled = false
""", encoding="utf-8")
        os.chmod(self.toml, 0o600)
        env = {k: v for k, v in os.environ.items() if k != "HELIOS_CONFIG"}
        env.update({"HELIOS_HOME": str(self.home), "HELIOS_CONFIG": str(self.toml),
                    "HELIOS_WEB_DIST": str(tmp / "no-web-dist"), "PYTHONPATH": str(SERVER)})
        self.log_path = tmp / "heliosd.log"
        self._log = open(self.log_path, "wb")
        code = preamble + "from heliosd.main import run; run()"
        self.proc = subprocess.Popen([sys.executable, "-c", code, "--config", str(self.toml)],
                                     env=env, stdout=self._log, stderr=subprocess.STDOUT,
                                     cwd=str(tmp))

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def log(self) -> str:
        self._log.flush()
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def wait_healthy(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                pytest.fail(f"heliosd exited {self.proc.returncode} before it was healthy:\n{self.log()}")
            try:
                with urllib.request.urlopen(self.base + "/api/health", timeout=2) as r:
                    if r.status == 200:
                        return
            except OSError:
                pass
            time.sleep(0.2)
        pytest.fail(f"heliosd not healthy after {timeout}s:\n{self.log()}")

    def ingest_synthetic(self) -> None:
        body = json.dumps(synth_batch(days=3)).encode()
        req = urllib.request.Request(self.base + "/ingest", data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "X-Helios-Token": self.token})
        with urllib.request.urlopen(req, timeout=30) as r:
            assert r.status == 200

    def stop(self, sig: int) -> tuple[int, float]:
        t0 = time.monotonic()
        self.proc.send_signal(sig)
        rc = self.proc.wait(timeout=30)
        return rc, time.monotonic() - t0

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self._log.close()


@pytest.fixture
def daemons(tmp_path):
    made: list[Daemon] = []

    def make(**kw) -> Daemon:
        d = Daemon(tmp_path / f"d{len(made)}", **kw)
        made.append(d)
        return d
    yield make
    for d in made:
        d.close()


def _shutdown_lines(log: str) -> list[str]:
    return [ln for ln in log.splitlines() if " heliosd: shutdown: {" in ln]


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_a_clean_stop_exits_0_after_the_full_shutdown(daemons, sig):
    d = daemons()
    d.wait_healthy()
    d.ingest_synthetic()
    rc, took = d.stop(sig)
    log = d.log()
    assert rc == 0, f"exit {rc}, expected 0:\n{log}"
    assert took < LAUNCHD_GRACE_S, f"stop took {took:.2f}s"
    lines = _shutdown_lines(log)
    assert len(lines) == 1 and all(k in lines[0] for k in CLEAN_LINE), log
    assert d.token not in log


def test_a_failed_checkpoint_at_shutdown_exits_1(daemons):
    # Planted fault in the subprocess only: the shutdown's checkpoint raises.
    d = daemons(preamble=(
        "from heliosd.store import db as _db\n"
        "def _planted(*a, **k):\n"
        "    raise RuntimeError('planted checkpoint failure')\n"
        "_db.checkpoint = _planted\n"))
    d.wait_healthy()
    rc, _ = d.stop(signal.SIGTERM)
    log = d.log()
    assert rc == 1, f"exit {rc}, expected 1:\n{log}"
    assert "shutdown: checkpoint failed" in log
    assert "'checkpointed': False" in _shutdown_lines(log)[0]
    assert "shutdown did not finish cleanly" in log


def test_a_lifespan_shutdown_that_raises_exits_1(daemons):
    d = daemons(preamble=(
        "import heliosd.main as _m\n"
        "async def _planted(app, tasks, *a, **k):\n"
        "    raise RuntimeError('planted shutdown failure')\n"
        "_m.shutdown_store = _planted\n"))
    d.wait_healthy()
    rc, _ = d.stop(signal.SIGTERM)
    log = d.log()
    assert rc == 1, f"exit {rc}, expected 1:\n{log}"
    assert "shutdown did not finish cleanly" in log


def test_a_failed_startup_still_exits_non_zero(daemons):
    # Control: the placeholder token stops the start in the lifespan.
    d = daemons(token=PLACEHOLDER)
    rc = d.proc.wait(timeout=60)
    log = d.log()
    assert rc == 3, f"exit {rc}, expected 3 (uvicorn's startup failure):\n{log}"
    assert "refusing to start" in log


def test_a_port_already_in_use_still_exits_non_zero(daemons):
    # Control: something else holds the port.
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        d = daemons(port=holder.getsockname()[1])
        rc = d.proc.wait(timeout=60)
    log = d.log()
    assert rc == 3, f"exit {rc}, expected 3 (uvicorn's startup failure):\n{log}"
    assert "address already in use" in log


# The rule, as a pure function (Q1, owner 2026-11-02).

@pytest.mark.parametrize("result,clean", [
    ({"drained": True, "checkpointed": True, "closed": True}, True),
    ({"drained": False, "checkpointed": True, "closed": True}, False),
    ({"drained": True, "checkpointed": False, "closed": True}, False),
    ({"drained": True, "checkpointed": True, "closed": False}, False),
    ({}, False),
    (None, False),
])
def test_clean_means_drained_checkpointed_and_closed(result, clean):
    from heliosd.main import shutdown_was_clean
    assert shutdown_was_clean(result) is clean


def test_uvicorn_still_keeps_the_captured_signal_list():
    # The exit fix clears this list after a normal return from serve(); if a
    # uvicorn upgrade renames it, this fails before launchd shows -15 again.
    server = uvicorn.Server(uvicorn.Config(app=None))
    assert server._captured_signals == []
    assert callable(getattr(uvicorn.Server, "capture_signals", None))
