#!/usr/bin/env python
"""Helios overnight relay, pulling side: drain the spool on the always-on Mac
into the local heliosd, then delete what was acked.

    server/.venv/bin/python server/tools/m4_spool_pull.py run
    server/.venv/bin/python server/tools/m4_spool_pull.py status
    server/.venv/bin/python server/tools/m4_spool_pull.py refused
    server/.venv/bin/python server/tools/m4_spool_pull.py requeue <name> [<name> ...] | --all

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

Refused batches (HTTP 422). heliosd answers 422 when a batch is in a shape it
cannot store whole (PayloadRefused, or a body that is not a JSON object); a
retry gets the same answer, so the batch is set aside instead of failing every
cycle and stopping LAST_OK. The local copy moves to ~/Helios/relay/refused/,
the reason (the status and the daemon's error text, never record contents) is
appended to refused/refusals.jsonl, and the spool host's copy moves from its
inbox/ to refused/ beside it. Nothing in either refused/ folder is pruned, and
nothing there is deleted before heliosd acks that batch. The other batches of
the cycle carry on, and the heliosd watchdog lists the refused/ folder as an
error row while it holds a batch. The phone counted these batches as
delivered, so their records are not in the store: `requeue` copies them back
into staging for the next cycle (after a heliosd fix or upgrade); one that is
acked leaves refused/ on both Macs, one refused again stays.
"""

from __future__ import annotations

import hashlib
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
REFUSED = RELAY / "refused"
LOG = HOME / "logs" / "relay.log"
LAST_OK = RELAY / "LAST_OK"
# heliosd's answer for a batch it can never store as it is (see above).
REFUSED_STATUS = 422
REFUSALS_NAME = "refusals.jsonl"
REASON_MAX = 300


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


def _safe_name(name: str) -> str:
    return "".join(ch for ch in name if ch.isalnum() or ch in "-_.")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _fsync_dir(path: Path) -> None:
    """Make a rename inside `path` durable. Best effort: a filesystem that
    cannot fsync a directory still has the rename."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def refusal_reason(body) -> str:
    """The daemon's reason without record contents. heliosd's own refusal is
    {"ack": false, "error": text}; FastAPI's validation 422 is a detail list
    whose "input" can echo the batch, so only each entry's type and message
    are kept."""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, str) and err:
            return err[:REASON_MAX]
        detail = body.get("detail")
        if isinstance(detail, list):
            parts = [f"{d.get('type', '?')}: {d.get('msg', '')}" for d in detail if isinstance(d, dict)]
            if parts:
                return "; ".join(parts)[:REASON_MAX]
        if isinstance(detail, str) and detail:
            return detail[:REASON_MAX]
    return "no reason given"


def record_event(refused: Path, event: dict) -> None:
    """Append one line to refused/refusals.jsonl (fsynced): the review trail."""
    refused.mkdir(parents=True, exist_ok=True)
    with open(refused / REFUSALS_NAME, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": _now_iso(), **event}, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def set_aside(f: Path, data: bytes, status: int, body, refused: Path = REFUSED) -> Path:
    """Move a refused staged batch into `refused` and record why. A copy with
    the same name and the same bytes already there is the same batch refused
    again; different bytes under the same name (not expected: names carry a
    microsecond stamp) go under a hash-suffixed name, so nothing is ever
    overwritten. The file's mtime becomes the refusal time (the watchdog's age)."""
    refused.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(refused, 0o700)
    except OSError:
        pass
    digest = hashlib.sha256(data).hexdigest()
    dest = refused / f.name
    if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() != digest:
        dest = refused / f"{f.stem}.{digest[:12]}{f.suffix}"
    os.replace(f, dest)
    os.utime(dest)
    _fsync_dir(refused)
    _fsync_dir(f.parent)
    record_event(refused, {"event": "refused", "name": dest.name, "status": status,
                           "reason": refusal_reason(body), "sha256": digest, "bytes": len(data)})
    return dest


def fetch(remote: str, remote_spool: str) -> int:
    """rsync the remote inbox into staging. Returns the number of files staged."""
    STAGING.mkdir(parents=True, exist_ok=True)
    rc = subprocess.run(["/usr/bin/rsync", "-a", "--include=*.json", "--exclude=*",
                         f"{remote}:{remote_spool}/inbox/", f"{STAGING}/"],
                        capture_output=True, text=True, timeout=600)
    if rc.returncode != 0:
        raise RuntimeError(f"rsync rc={rc.returncode}: {rc.stderr.strip()[:200]}")
    return len(list(STAGING.glob("*.json")))


def replay(post, delete_remote, staging: Path = STAGING, delivered: Path = DELIVERED,
           refused: Path = REFUSED, set_aside_remote=None) -> dict:
    """Replay every staged batch in filename (time) order. `post(bytes) ->
    (status, body_dict)`; `delete_remote(name) -> bool`;
    `set_aside_remote(name) -> bool` moves the spool host's copy of a refused
    batch out of its inbox (None: no spool host to tell). Pure apart from the
    file moves, so the test suite drives it with fakes.

    acked: in the store; refused: set aside (422); failed: still waiting, in
    staging or, for a refused batch whose host copy could not be moved, in the
    host's inbox (the next cycle stages it again and retries the move)."""
    delivered.mkdir(parents=True, exist_ok=True)
    out = {"replayed": 0, "acked": 0, "refused": 0, "failed": 0, "remote_deleted": 0}
    for f in sorted(staging.glob("*.json")):
        out["replayed"] += 1
        data = f.read_bytes()
        try:
            status, body = post(data)
        except Exception as e:  # noqa: BLE001 - network errors are a failed replay, not a crash
            status, body = 0, {"error": type(e).__name__}
        if 200 <= status < 300 and isinstance(body, dict) and body.get("ack") is True:
            out["acked"] += 1
            if delete_remote(f.name):
                out["remote_deleted"] += 1
            shutil.move(str(f), str(delivered / f.name))
            held = refused / f.name
            # A requeued batch now in the store leaves refused/ (only the same bytes).
            if held.is_file() and held.read_bytes() == data:
                held.unlink()
                record_event(refused, {"event": "acked", "name": f.name})
        elif status == REFUSED_STATUS:
            out["refused"] += 1
            set_aside(f, data, status, body, refused)
            if set_aside_remote is not None and not set_aside_remote(f.name):
                out["failed"] += 1
        else:
            out["failed"] += 1
    return out


def requeue(names: list[str], refused: Path = REFUSED, staging: Path = STAGING) -> tuple[list[str], list[str]]:
    """Copy refused batches back into staging for the next cycle. The refused
    copy stays until heliosd acks the batch. Returns (requeued, not found)."""
    held = {p.name: p for p in sorted(refused.glob("*.json"))} if refused.is_dir() else {}
    wanted = list(held) if names == ["--all"] else names
    staging.mkdir(parents=True, exist_ok=True)
    done, missing = [], []
    for name in wanted:
        p = held.get(name)
        if p is None:
            missing.append(name)
            continue
        tmp = staging / (name + ".part")
        with open(tmp, "wb") as fh:
            fh.write(p.read_bytes())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, staging / name)
        record_event(refused, {"event": "requeued", "name": name})
        done.append(name)
    _fsync_dir(staging)
    return done, missing


def refused_batches(refused: Path = REFUSED) -> list[dict]:
    """The batches waiting in refused/, oldest first, each with its latest
    refusal reason from the trail."""
    if not refused.is_dir():
        return []
    reasons: dict[str, dict] = {}
    trail = refused / REFUSALS_NAME
    if trail.is_file():
        for line in trail.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") == "refused":
                reasons[str(ev.get("name"))] = ev
    out = []
    for p in sorted(refused.glob("*.json")):
        ev = reasons.get(p.name, {})
        out.append({"name": p.name, "bytes": p.stat().st_size,
                    "refused_at": ev.get("at") or datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
                    "status": ev.get("status"), "reason": ev.get("reason")})
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
    spool = cfg["remote_spool"]

    def post(body: bytes):
        r = client.post("/ingest", content=body)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    def remote(command: str) -> bool:
        rc = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", cfg["remote"], command],
                            capture_output=True, timeout=60)
        return rc.returncode == 0

    def delete_remote(name: str) -> bool:
        # The inbox copy, and the refused/ copy of a requeued batch heliosd has now acked.
        safe = _safe_name(name)
        return remote(f"rm -f ~/{spool}/inbox/{safe} ~/{spool}/refused/{safe}")

    def set_aside_remote(name: str) -> bool:
        safe = _safe_name(name)
        log(f"refused {safe}: heliosd answered {REFUSED_STATUS}; kept in {REFUSED}, see {REFUSALS_NAME}")
        return remote(f"mkdir -p ~/{spool}/refused && chmod 700 ~/{spool}/refused && "
                      f"if [ -f ~/{spool}/inbox/{safe} ]; then mv -f ~/{spool}/inbox/{safe} ~/{spool}/refused/{safe}; fi")

    try:
        staged = fetch(cfg["remote"], spool)
    except (RuntimeError, subprocess.SubprocessError) as e:
        log(f"aborted: fetch failed: {e}")
        return 1
    res = replay(post, delete_remote, set_aside_remote=set_aside_remote)
    pruned = prune_delivered(int(cfg["keep_delivered_days"]))
    log(f"cycle staged={staged} replayed={res['replayed']} acked={res['acked']} refused={res['refused']} "
        f"failed={res['failed']} remote_deleted={res['remote_deleted']} pruned={pruned}")
    if res["failed"] == 0:
        LAST_OK.parent.mkdir(parents=True, exist_ok=True)
        LAST_OK.write_text(datetime.now().isoformat() + "\n", encoding="utf-8")
        return 0
    return 3


def cmd_status() -> int:
    print("last ok:", LAST_OK.read_text().strip() if LAST_OK.is_file() else "never")
    print("staged:", len(list(STAGING.glob("*.json"))) if STAGING.is_dir() else 0,
          "delivered kept:", len(list(DELIVERED.glob("*.json"))) if DELIVERED.is_dir() else 0,
          "refused:", len(refused_batches()))
    if LOG.is_file():
        print("".join(LOG.read_text(encoding="utf-8").splitlines(True)[-5:]), end="")
    return 0


def cmd_refused() -> int:
    rows = refused_batches()
    if not rows:
        print("no refused batches")
        return 0
    for r in rows:
        print(f"{r['name']}  {r['bytes']} bytes  refused {r['refused_at']}  HTTP {r['status']}: {r['reason']}")
    print(f"{len(rows)} refused; `requeue <name>` or `requeue --all` replays them on the next cycle")
    return 0


def cmd_requeue(names: list[str]) -> int:
    if not names:
        print("requeue: name one or more refused batches, or --all")
        return 2
    done, missing = requeue(names)
    for name in done:
        log(f"requeued {_safe_name(name)} from refused/ into staging")
    for name in missing:
        print(f"not in refused/: {name}")
    print(f"requeued {len(done)}; the next run replays them")
    return 1 if missing else 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "run":
        return cmd_run()
    if cmd == "status":
        return cmd_status()
    if cmd == "refused":
        return cmd_refused()
    if cmd == "requeue":
        return cmd_requeue(argv[2:])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
