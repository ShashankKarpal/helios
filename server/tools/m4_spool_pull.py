#!/usr/bin/env python
"""Helios overnight relay, pulling side: drain the spool on the always-on Mac
into the local heliosd, then delete what was acked.

    server/.venv/bin/python server/tools/m4_spool_pull.py run
    server/.venv/bin/python server/tools/m4_spool_pull.py status

Configuration in ~/Helios/helios.toml:

    [relay]
    remote = "user@spool-host"          # ssh target of the spool receiver
    remote_spool = "HeliosSpool"        # relative to the remote home
    keep_delivered_days = 3             # local copies of replayed batches

Cycle: rsync <remote>/inbox/*.json into ~/Helios/relay/staging, POST each file
in filename order to https://127.0.0.1:<port>/ingest with the shared token,
and only on a 2xx with {"ack": true} move the local copy to
~/Helios/relay/delivered/ and delete the remote file. heliosd dedupes on the
sample uuid, so a batch replayed twice (a crash between ack and delete)
inserts nothing new. Anything that fails stays in place for the next cycle.
Writes ~/Helios/relay/LAST_OK after a cycle that ended with no failures
(including a cycle that found nothing to do), so the watchdog can watch it.

Run every few minutes from a LaunchAgent while the Mac is awake; launchd runs
missed StartInterval fires once on wake, which is the catch-up.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from heliosd.config import helios_home, load_settings  # noqa: E402

HOME = helios_home()
RELAY = HOME / "relay"
STAGING = RELAY / "staging"
DELIVERED = RELAY / "delivered"
LOG = HOME / "logs" / "relay.log"
LAST_OK = RELAY / "LAST_OK"


def log(line: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    print(line)


def _cfg() -> dict:
    st = load_settings()
    c = {"remote": "", "remote_spool": "HeliosSpool", "keep_delivered_days": 3}
    c.update(st.raw.get("relay", {}))
    return c


def fetch(remote: str, remote_spool: str) -> int:
    """rsync the remote inbox into staging. Returns the number of files staged."""
    STAGING.mkdir(parents=True, exist_ok=True)
    rc = subprocess.run(["/usr/bin/rsync", "-a", "--include=*.json", "--exclude=*",
                         f"{remote}:{remote_spool}/inbox/", f"{STAGING}/"],
                        capture_output=True, text=True, timeout=600)
    if rc.returncode != 0:
        raise RuntimeError(f"rsync rc={rc.returncode}: {rc.stderr.strip()[:200]}")
    return len(list(STAGING.glob("*.json")))


def replay(post, delete_remote, staging: Path = STAGING, delivered: Path = DELIVERED) -> dict:
    """Replay every staged batch in filename (time) order. `post(bytes) ->
    (status, body_dict)`; `delete_remote(name) -> bool`. Pure apart from the
    file moves, so the test suite drives it with fakes."""
    delivered.mkdir(parents=True, exist_ok=True)
    out = {"replayed": 0, "acked": 0, "failed": 0, "remote_deleted": 0}
    for f in sorted(staging.glob("*.json")):
        out["replayed"] += 1
        try:
            status, body = post(f.read_bytes())
        except Exception as e:  # noqa: BLE001 - network errors are a failed replay, not a crash
            status, body = 0, {"error": type(e).__name__}
        if 200 <= status < 300 and isinstance(body, dict) and body.get("ack") is True:
            out["acked"] += 1
            if delete_remote(f.name):
                out["remote_deleted"] += 1
            shutil.move(str(f), str(delivered / f.name))
        else:
            out["failed"] += 1
    return out


def prune_delivered(keep_days: int) -> int:
    cutoff = datetime.now() - timedelta(days=keep_days)
    n = 0
    for f in DELIVERED.glob("*.json") if DELIVERED.is_dir() else []:
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink()
            n += 1
    return n


def cmd_run() -> int:
    cfg = _cfg()
    if not cfg["remote"]:
        log("no [relay] remote configured; nothing to do")
        return 0
    st = load_settings()
    base = os.environ.get("HELIOS_API", f"https://127.0.0.1:{st.port}")
    client = httpx.Client(base_url=base, verify=False, timeout=120,
                          headers={"X-Helios-Token": st.ingest_token, "Content-Type": "application/json"})

    def post(body: bytes):
        r = client.post("/ingest", content=body)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    def delete_remote(name: str) -> bool:
        safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_.")
        rc = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", cfg["remote"],
                             f"rm -f ~/{cfg['remote_spool']}/inbox/{safe}"], capture_output=True, timeout=60)
        return rc.returncode == 0

    try:
        staged = fetch(cfg["remote"], cfg["remote_spool"])
    except (RuntimeError, subprocess.SubprocessError) as e:
        log(f"aborted: fetch failed: {e}")
        return 1
    res = replay(post, delete_remote)
    pruned = prune_delivered(int(cfg["keep_delivered_days"]))
    log(f"cycle staged={staged} replayed={res['replayed']} acked={res['acked']} failed={res['failed']} "
        f"remote_deleted={res['remote_deleted']} pruned={pruned}")
    if res["failed"] == 0:
        LAST_OK.parent.mkdir(parents=True, exist_ok=True)
        LAST_OK.write_text(datetime.now().isoformat() + "\n", encoding="utf-8")
        return 0
    return 3


def cmd_status() -> int:
    print("last ok:", LAST_OK.read_text().strip() if LAST_OK.is_file() else "never")
    print("staged:", len(list(STAGING.glob("*.json"))) if STAGING.is_dir() else 0,
          "delivered kept:", len(list(DELIVERED.glob("*.json"))) if DELIVERED.is_dir() else 0)
    if LOG.is_file():
        print("".join(LOG.read_text(encoding="utf-8").splitlines(True)[-5:]), end="")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "run":
        return cmd_run()
    if cmd == "status":
        return cmd_status()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
