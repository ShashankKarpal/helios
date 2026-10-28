"""Hardening round 2026-10-28, H8: the relay's spool receiver on the always-on
second Mac listens on that Mac's Tailscale address only. It listened on every
interface (0.0.0.0), so the home network could reach it; the phone reaches it
only by its tailnet name (the receiver's certificate names nothing else). The
default --host is now "tailnet": the one IPv4 address in 100.64.0.0/10 on a
utun interface. With none (Tailscale not up yet at boot) or more than one, it
refuses to start with exit code 3, and launchd (KeepAlive) starts it again.
--host 0.0.0.0 stays as the explicit rollback. Synthetic addresses only."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("m1_spool_receiver_h8", TOOLS / "m1_spool_receiver.py")
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)

IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.1.20 netmask 0xffffff00 broadcast 192.168.1.255
en7: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 100.70.1.2 netmask 0xffc00000
utun3: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
\tinet 10.8.0.2 --> 10.8.0.1 netmask 0xffffffff
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
\tinet 100.101.102.103 --> 100.101.102.103 netmask 0xffffffff
\tinet6 fd7a:115c:a1e0::1 prefixlen 48
"""


def test_the_tailnet_address_is_the_one_on_a_utun_interface():
    # A carrier or hotel LAN numbered from 100.64.0.0/10 (en7) and another VPN's
    # utun (10.8.0.2) are not the tailnet.
    assert receiver.tailnet_addresses(IFCONFIG) == ["100.101.102.103"]


def test_resolve_host_tailnet_picks_it_and_anything_else_is_kept():
    assert receiver.resolve_host("tailnet", lambda: IFCONFIG) == "100.101.102.103"
    assert receiver.resolve_host("0.0.0.0", lambda: "") == "0.0.0.0"
    assert receiver.resolve_host("100.1.2.3", lambda: "") == "100.1.2.3"


@pytest.mark.parametrize("text,why", [
    ("lo0: flags=1\n\tinet 127.0.0.1 netmask 0xff000000\n", "no Tailscale address"),
    (IFCONFIG + "utun9: flags=1\n\tinet 100.90.0.9 --> 100.90.0.9 netmask 0xffffffff\n", "more than one"),
])
def test_no_address_or_two_refuse(text, why):
    with pytest.raises(receiver.HostError, match=why):
        receiver.resolve_host("tailnet", lambda: text)


def _args(tmp_path, *extra):
    tok = tmp_path / "token"
    tok.write_text("x" * 32, encoding="utf-8")
    return ["--spool", str(tmp_path / "spool"), "--token-file", str(tok),
            "--cert", str(tmp_path / "c.pem"), "--key", str(tmp_path / "k.pem"), *extra]


class _Bound(Exception):
    pass


def _capture(monkeypatch):
    seen = []

    def fake_server(addr, handler):
        seen.append(addr)
        raise _Bound
    monkeypatch.setattr(receiver, "ThreadingHTTPServer", fake_server)
    return seen


def test_by_default_it_binds_the_tailnet_address_only(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    monkeypatch.setattr(receiver, "read_ifconfig", lambda: IFCONFIG)
    with pytest.raises(_Bound):
        receiver.main(_args(tmp_path))
    assert seen == [("100.101.102.103", 8420)]


def test_without_tailscale_it_exits_3_and_binds_nothing(tmp_path, monkeypatch, capsys):
    seen = _capture(monkeypatch)
    monkeypatch.setattr(receiver, "read_ifconfig", lambda: "lo0: flags=1\n\tinet 127.0.0.1\n")
    assert receiver.main(_args(tmp_path)) == 3
    assert seen == [] and "no Tailscale address" in capsys.readouterr().err


def test_the_rollback_still_binds_every_interface(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    monkeypatch.setattr(receiver, "read_ifconfig", lambda: pytest.fail("not read for an explicit host"))
    with pytest.raises(_Bound):
        receiver.main(_args(tmp_path, "--host", "0.0.0.0"))
    assert seen == [("0.0.0.0", 8420)]


def test_the_source_still_compiles_on_the_system_python():
    py = Path("/usr/bin/python3")
    if not py.exists():
        pytest.skip("no system Python here")
    r = subprocess.run([str(py), "-c", "import sys; compile(open(sys.argv[1]).read(), sys.argv[1], 'exec')",
                        str(TOOLS / "m1_spool_receiver.py")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert sys.version_info >= (3, 9)
