"""P4 (2026-10-31): the M1 spool receiver checks the body length before it
reads any body. Before, do_POST took int(Content-Length) with no check: a
negative value passed the size check and rfile.read(-1) read until the
client closed, a non-number raised (no answer, a traceback in the log), and
a missing header read nothing and answered "not JSON". Now, after the token
check: any Transfer-Encoding 400, no Content-Length 411, more than one 400,
anything but plain ASCII digits 400, over --max-batch-mb 413. Each refusal
comes at once and stores nothing. Real sockets on 127.0.0.1 (plain HTTP; TLS
is wrapped around the same handler in main()), synthetic token and bodies."""

from __future__ import annotations

import importlib.util
import json
import socket
import threading
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("m1_spool_receiver_p4", TOOLS / "m1_spool_receiver.py")
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)

TOKEN = "synthetic-relay-token-0123456789"
CAP = 4096
BODY = b'{"batch_id": "syn-1", "samples": []}'


@pytest.fixture()
def server(tmp_path):
    spool = tmp_path / "spool"
    (spool / "inbox").mkdir(parents=True)
    handler = receiver.make_handler(spool, TOKEN, CAP, 10 * 1024 * 1024, 0.0)
    httpd = receiver.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1], spool / "inbox"
    httpd.shutdown()
    httpd.server_close()


def post(port: int, headers: list[tuple[str, str]], body: bytes = b"", token: str | None = TOKEN,
         timeout: float = 2.0):
    """Send one raw request; returns (status, reply dict) or (None, why) when no answer came in time."""
    lines = ["POST /ingest HTTP/1.1", "Host: spool.test", "Content-Type: application/json"]
    if token is not None:
        lines.append(f"X-Helios-Token: {token}")
    lines += [f"{k}: {v}" for k, v in headers]
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(raw)
        data = b""
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                return None, f"no answer within {timeout} s"
            if not chunk:
                break
            data += chunk
    finally:
        s.close()
    if not data:
        return None, "connection closed with no answer"
    head, _, rest = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(rest or b"{}")


def _stored(inbox: Path) -> list:
    return sorted(p.name for p in inbox.iterdir())


@pytest.mark.parametrize("headers,status,word", [
    ([], 411, "Content-Length required"),
    ([("Content-Length", "-1")], 400, "Content-Length"),
    ([("Content-Length", "-100000")], 400, "Content-Length"),
    ([("Content-Length", "abc")], 400, "Content-Length"),
    ([("Content-Length", "")], 400, "Content-Length"),
    ([("Content-Length", "+36")], 400, "Content-Length"),
    ([("Content-Length", "3_6")], 400, "Content-Length"),
    ([("Content-Length", "3.6e1")], 400, "Content-Length"),
    ([("Content-Length", "３６")], 400, "Content-Length"),          # full-width digits are not ASCII
    ([("Content-Length", "36"), ("Content-Length", "36")], 400, "more than one"),
    ([("Transfer-Encoding", "chunked")], 400, "Transfer-Encoding"),
    ([("Transfer-Encoding", "chunked"), ("Content-Length", "36")], 400, "Transfer-Encoding"),
    ([("Content-Length", str(CAP + 1))], 413, "too large"),
    ([("Content-Length", "9" * 40)], 413, "too large"),
])
def test_a_bad_length_is_refused_at_once_and_nothing_is_stored(server, headers, status, word):
    port, inbox = server
    body = BODY if any(k == "Content-Length" and v == "36" for k, v in headers) else b""
    got, reply = post(port, headers, body)
    assert (got, reply.get("ack") if isinstance(reply, dict) else reply) == (status, False)
    assert word in reply["error"]
    assert _stored(inbox) == []


def test_the_token_is_still_checked_first(server):
    port, inbox = server
    assert post(port, [("Content-Length", "-1")], token="wrong-token-wrong-token")[0] == 401
    assert post(port, [], token=None)[0] == 401
    assert _stored(inbox) == []


def test_a_good_batch_is_still_stored_and_acked(server):
    port, inbox = server
    assert len(BODY) == 36
    got, reply = post(port, [("Content-Length", str(len(BODY)))], BODY)
    assert got == 200 and reply["ack"] is True and reply["batch_id"] == "syn-1"
    names = _stored(inbox)
    assert len(names) == 1 and names[0].endswith("_syn-1.json")
    assert (inbox / names[0]).read_bytes() == BODY
    got, reply = post(port, [("Content-Length", " 36 ")], BODY)      # spaces around the number are allowed
    assert got == 200 and reply["ack"] is True



# ---------- review round 1 (2026-10-31) ----------

@pytest.fixture()
def server11(tmp_path):
    """The same handler with HTTP/1.1 keep-alive switched on: a refusal must still close the
    connection, so an unread body (or a request behind it) is never read as the next request."""
    spool = tmp_path / "spool"
    (spool / "inbox").mkdir(parents=True)
    handler = receiver.make_handler(spool, TOKEN, CAP, 10 * 1024 * 1024, 0.0)
    handler.protocol_version = "HTTP/1.1"
    httpd = receiver.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], spool / "inbox"
    httpd.shutdown()
    httpd.server_close()


def _two_requests(port: int) -> bytes:
    first = ("POST /ingest HTTP/1.1\r\nHost: t\r\nX-Helios-Token: " + TOKEN + "\r\nContent-Length: -1\r\n\r\n").encode()
    second = ("POST /ingest HTTP/1.1\r\nHost: t\r\nX-Helios-Token: " + TOKEN +
              f"\r\nContent-Length: {len(BODY)}\r\n\r\n").encode() + BODY
    s = socket.create_connection(("127.0.0.1", port), timeout=2)
    data = b""
    try:
        s.sendall(first + second)
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
    finally:
        s.close()
    return data


@pytest.mark.parametrize("fixture", ["server", "server11"])
def test_a_refusal_closes_the_connection_so_nothing_behind_it_is_read(request, fixture):
    port, inbox = request.getfixturevalue(fixture)
    data = _two_requests(port)
    assert data.count(b"HTTP/1.") == 1 and b" 400 " in data.split(b"\r\n", 1)[0]
    assert _stored(inbox) == []


def test_a_zero_length_is_not_json_and_not_stored(server):
    port, inbox = server
    got, reply = post(port, [("Content-Length", "0")])
    assert got == 400 and reply["ack"] is False and "not JSON" in reply["error"]
    assert _stored(inbox) == []


def test_only_the_declared_bytes_are_stored(server):
    port, inbox = server
    got, reply = post(port, [("Content-Length", str(len(BODY)))], BODY + b"trailing-bytes")
    assert got == 200 and reply["ack"] is True
    assert (inbox / _stored(inbox)[0]).read_bytes() == BODY
