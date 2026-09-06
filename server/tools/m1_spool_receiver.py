#!/usr/bin/env python3
"""Helios overnight relay, receiving side: a store-and-forward spool for Bridge
batches on an always-on second Mac.

Standard library only, Python 3.9 compatible (macOS system Python), no
DuckDB, no heliosd: this box never interprets health data, it only holds
batches until the main Mac wakes and pulls them (m4_spool_pull.py).

    python3 m1_spool_receiver.py --spool ~/HeliosSpool --token-file ~/HeliosSpool/token \
        --cert ~/HeliosSpool/certs/cert.pem --key ~/HeliosSpool/certs/key.pem --port 8420

Endpoints:
    POST /ingest        same wire contract as heliosd: X-Helios-Token required,
                        JSON body, reply {"ack": true} only after the batch is
                        fsynced to <spool>/inbox/<utc-stamp>_<batch_id>.json.
                        The Bridge advances its anchors on ack == true, so the
                        ack is the durability promise; nothing is acked that is
                        not on disk.
    GET  /api/health    {"ok": true, "role": "spool", "queued": N, "free_gb": F}
                        no token, content-free.

Refusals (the Bridge keeps the batch in its own outbox and retries):
    401 bad or missing token
    413 body over --max-batch-mb
    507 spool over --max-spool-mb, or the disk under --min-free-gb

Every other path is 404. Nothing is ever deleted here; the puller deletes a
batch only after heliosd on the main Mac has acked it.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import shutil
import ssl
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SAFE_ID = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


def spool_stats(spool: Path):
    inbox = spool / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(spool, 0o700)
        os.chmod(inbox, 0o700)
    except OSError:
        pass
    files = list(inbox.glob("*.json"))
    size = sum(f.stat().st_size for f in files)
    free_gb = shutil.disk_usage(str(spool)).free / (1024 ** 3)
    return len(files), size, free_gb


def safe_batch_id(raw) -> str:
    s = "".join(ch for ch in str(raw or "no-id") if ch in SAFE_ID)
    return (s or "no-id")[:64]


def accept_batch(body: bytes, spool: Path, max_batch_bytes: int, max_spool_bytes: int,
                 min_free_gb: float, now: datetime | None = None):
    """Validate and persist one batch. Returns (status, reply_dict). Pure
    function of its inputs apart from the write, so it is unit-testable
    without a socket."""
    if len(body) > max_batch_bytes:
        return 413, {"ack": False, "error": "batch too large"}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, {"ack": False, "error": "body is not JSON"}
    if not isinstance(payload, dict):
        return 400, {"ack": False, "error": "body must be an object"}
    n, size, free_gb = spool_stats(spool)
    if size >= max_spool_bytes:
        return 507, {"ack": False, "error": "spool full"}
    if free_gb < min_free_gb:
        return 507, {"ack": False, "error": "disk low"}
    now = now or datetime.now(timezone.utc)
    inbox = spool / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    name = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}_{safe_batch_id(payload.get('batch_id'))}.json"
    tmp = inbox / (name + ".part")
    with open(tmp, "wb") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, inbox / name)
    return 200, {"ack": True, "batch_id": payload.get("batch_id"), "spooled": name, "queued": n + 1}


def make_handler(spool: Path, token: str, max_batch_bytes: int, max_spool_bytes: int, min_free_gb: float):
    class Handler(BaseHTTPRequestHandler):
        server_version = "helios-spool/1"

        def _send(self, status: int, obj: dict) -> None:
            data = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _token_ok(self) -> bool:
            presented = self.headers.get("X-Helios-Token") or ""
            return bool(token) and hmac.compare_digest(presented.encode(), token.encode())

        def do_GET(self):  # noqa: N802
            if self.path == "/api/health":
                n, size, free_gb = spool_stats(spool)
                self._send(200, {"ok": True, "role": "spool", "queued": n,
                                 "queued_mb": round(size / (1024 * 1024), 2), "free_gb": round(free_gb, 1)})
                return
            self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path != "/ingest":
                self._send(404, {"error": "not found"})
                return
            if not self._token_ok():
                self._send(401, {"ack": False, "error": "bad or missing X-Helios-Token"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > max_batch_bytes:
                self._send(413, {"ack": False, "error": "batch too large"})
                return
            body = self.rfile.read(length)
            status, reply = accept_batch(body, spool, max_batch_bytes, max_spool_bytes, min_free_gb)
            self._send(status, reply)

        def log_message(self, fmt, *args):  # quiet: one line per request, no bodies
            sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args))

    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spool", default="~/HeliosSpool")
    ap.add_argument("--token-file", default="~/HeliosSpool/token", help="0600 file holding the shared ingest_token")
    ap.add_argument("--cert", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8420)
    ap.add_argument("--max-batch-mb", type=float, default=32)
    ap.add_argument("--max-spool-mb", type=float, default=2048)
    ap.add_argument("--min-free-gb", type=float, default=10)
    a = ap.parse_args(argv)
    spool = Path(os.path.expanduser(a.spool))
    (spool / "inbox").mkdir(parents=True, exist_ok=True)
    os.chmod(spool, 0o700)
    token = Path(os.path.expanduser(a.token_file)).read_text(encoding="utf-8").strip()
    if len(token) < 16:
        print("refusing to start: token file is empty or too short", file=sys.stderr)
        return 2
    handler = make_handler(spool, token, int(a.max_batch_mb * 1024 * 1024),
                           int(a.max_spool_mb * 1024 * 1024), a.min_free_gb)
    httpd = ThreadingHTTPServer((a.host, a.port), handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(os.path.expanduser(a.cert), os.path.expanduser(a.key))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print(f"helios spool receiver on https://{a.host}:{a.port}, spool {spool}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
