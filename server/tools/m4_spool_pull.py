#!/usr/bin/env python
"""Helios overnight relay, pulling side: drain the spool on the always-on Mac
into the local heliosd, then delete what was acked.

    server/.venv/bin/python server/tools/m4_spool_pull.py run
    server/.venv/bin/python server/tools/m4_spool_pull.py status
    server/.venv/bin/python server/tools/m4_spool_pull.py refused
    server/.venv/bin/python server/tools/m4_spool_pull.py requeue <name or path> [...] | --all

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
inserts nothing new. Anything that fails stays in place for the next cycle,
and an error on one batch never stops the others. When the fetch fails, the
batches already staged are still replayed. One run at a time (a lock file).
Writes ~/Helios/relay/LAST_OK after a cycle that ended with no failures
(including a cycle that found nothing to do), so the watchdog can watch it.

Refused batches (HTTP 422). heliosd answers 422 when a batch is in a shape it
cannot store whole (PayloadRefused, or a body that is not a JSON object); the
same heliosd gives the same answer every time, so the batch is set aside
instead of failing every cycle and stopping LAST_OK. The reason (the status
and the daemon's error text, never record contents) is appended to
~/Helios/relay/refused/refusals.jsonl, the local copy moves to refused/, and
the spool host's copy moves from its inbox/ to refused/ beside it; neither
move overwrites a different batch. Nothing in either refused/ folder is
pruned, and nothing there is deleted before heliosd acks that batch with the
same bytes. The other batches of the cycle carry on, and the heliosd watchdog
lists the refused/ folder as an error row while it holds a batch. The phone
counted these batches as delivered, so their records are not in the store:
each is retried once automatically after every heliosd restart (a fix or an
upgrade takes a restart), and `requeue` copies them back into staging at any
time. One that is acked leaves refused/ on both Macs; one refused again stays.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from heliosd.config import helios_home, load_settings  # noqa: E402

HOME = helios_home()
RELAY = HOME / "relay"
STAGING = RELAY / "staging"
DELIVERED = RELAY / "delivered"
REFUSED = RELAY / "refused"
LOG = HOME / "logs" / "relay.log"
LAST_OK = RELAY / "LAST_OK"
LOCK = RELAY / ".run.lock"
# heliosd's answer for a batch it can never store as it is (see above).
REFUSED_STATUS = 422
REFUSALS_NAME = "refusals.jsonl"
REASON_MAX = 300
SAFE_SPOOL = re.compile(r"^[A-Za-z0-9._/-]+$")
# The one word each remote step prints when it did its job.
SET_ASIDE_OK = {"MOVED", "SAME", "KEPT", "ABSENT"}
RELEASE_OK = {"REMOVED", "ABSENT", "KEPT"}


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


def name_is_safe(name: str) -> bool:
    """The spool receiver only writes names of letters, digits and -_. ; any
    other name is left alone for a person, never passed to a remote shell."""
    return bool(name) and _safe_name(name) == name and not name.startswith(".")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _alt_name(name: str, digest: str) -> str:
    p = Path(name)
    return f"{p.stem}.{digest[:12]}{p.suffix}"


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


# ---- remote steps: one shell line each for the spool host's login shell (zsh
# on macOS, run with ssh), every path quoted, one word printed on success.

def remote_delete_cmd(spool: str, name: str) -> str:
    """The inbox copy of an acked batch (rsync copied it from there; the
    receiver never rewrites a name)."""
    d, n = shlex.quote(spool), shlex.quote(name)
    return f'cd && d={d} && rm -f "$d/inbox/"{n} && echo DELETED'


def remote_set_aside_cmd(spool: str, name: str, digest: str) -> str:
    """Move a refused batch from the host's inbox to its refused/ folder
    without ever overwriting a different batch: MOVED, SAME (an identical
    copy is already there, so the inbox copy goes), KEPT (a different copy
    has that name: this one goes under the hash-suffixed name), ABSENT (not
    in the inbox, for example moved by an earlier cycle)."""
    d, n, alt = shlex.quote(spool), shlex.quote(name), shlex.quote(_alt_name(name, digest))
    return (f'cd && d={d} && mkdir -p "$d/refused" && chmod 700 "$d/refused" && '
            f'if [ ! -f "$d/inbox/"{n} ]; then echo ABSENT; '
            f'elif [ ! -e "$d/refused/"{n} ]; then mv "$d/inbox/"{n} "$d/refused/"{n} && echo MOVED; '
            f'elif cmp -s "$d/inbox/"{n} "$d/refused/"{n}; then rm -f "$d/inbox/"{n} && echo SAME; '
            f'else mv "$d/inbox/"{n} "$d/refused/"{alt} && echo KEPT; fi')


def remote_release_cmd(spool: str, name: str, digest: str) -> str:
    """Remove the host's refused/ copy of a batch heliosd has now acked, only
    when its bytes hash to `digest`: REMOVED, ABSENT, or KEPT (other bytes)."""
    d, n, h = shlex.quote(spool), shlex.quote(name), shlex.quote(digest)
    return (f'cd && d={d} && f="$d/refused/"{n} && '
            f'if [ ! -f "$f" ]; then echo ABSENT; '
            f'elif [ "$(/usr/bin/shasum -a 256 "$f" | cut -c1-64)" = {h} ]; then rm -f "$f" && echo REMOVED; '
            f'else echo KEPT; fi')


def run_remote(remote: str, command: str) -> str | None:
    """The last word the remote command printed, or None when ssh failed,
    timed out or the command did not finish."""
    try:
        rc = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", remote, command],
                            capture_output=True, text=True, timeout=60)
    except (subprocess.SubprocessError, OSError):
        return None
    words = rc.stdout.split()
    return words[-1] if rc.returncode == 0 and words else None


# ---- the review trail and the refused/ folder

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


def trail(refused: Path = REFUSED) -> list[dict]:
    path = refused / REFUSALS_NAME
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def set_aside(f: Path, data: bytes, status: int, body, refused: Path = REFUSED,
              daemon_started_at: str | None = None) -> Path:
    """Record why, then move a refused staged batch into `refused`. A copy
    with the same name and the same bytes already there is the same batch
    refused again; different bytes under the same name (not expected: names
    carry a microsecond stamp) go under a hash-suffixed name, so nothing is
    ever overwritten. The file's mtime becomes the refusal time."""
    refused.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(refused, 0o700)
    except OSError:
        pass
    digest = _sha256(data)
    dest = refused / f.name
    if dest.exists() and _sha256(dest.read_bytes()) != digest:
        dest = refused / _alt_name(f.name, digest)
    record_event(refused, {"event": "refused", "name": dest.name, "status": status,
                           "reason": refusal_reason(body), "sha256": digest, "bytes": len(data),
                           "daemon_started_at": daemon_started_at})
    os.replace(f, dest)
    os.utime(dest)
    _fsync_dir(refused)
    _fsync_dir(f.parent)
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


def _replay_one(f: Path, post, delete_remote, delivered: Path, refused: Path,
                set_aside_remote, release_remote, daemon_started_at) -> dict:
    if not name_is_safe(f.name):
        return {"failed": 1}
    data = f.read_bytes()
    try:
        status, body = post(data)
    except Exception as e:  # noqa: BLE001 - network errors are a failed replay, not a crash
        status, body = 0, {"error": type(e).__name__}
    if 200 <= status < 300 and isinstance(body, dict) and body.get("ack") is True:
        out = {"acked": 1}
        if delete_remote(f.name):
            out["remote_deleted"] = 1
        held = refused / f.name
        if held.is_file() and held.read_bytes() == data:
            # A set-aside batch is in the store now. The host's copy goes first,
            # then ours, and the staged copy moves last: a crash or a failed
            # release leaves it staged, and the next cycle (a replay inserts
            # nothing twice) finishes the job.
            digest = _sha256(data)
            if release_remote is not None and not release_remote(f.name, digest):
                out["failed"] = 1
                return out
            held.unlink()
            _fsync_dir(refused)
            record_event(refused, {"event": "acked", "name": f.name, "sha256": digest})
        shutil.move(str(f), str(delivered / f.name))
        return out
    if status == REFUSED_STATUS:
        set_aside(f, data, status, body, refused, daemon_started_at)
        out = {"refused": 1}
        if set_aside_remote is not None and not set_aside_remote(f.name, _sha256(data)):
            out["failed"] = 1
        return out
    return {"failed": 1}


def replay(post, delete_remote, staging: Path = STAGING, delivered: Path = DELIVERED,
           refused: Path = REFUSED, set_aside_remote=None, release_remote=None,
           daemon_started_at: str | None = None, errors: list | None = None) -> dict:
    """Replay every staged batch in filename (time) order. `post(bytes) ->
    (status, body_dict)`; `delete_remote(name) -> bool`;
    `set_aside_remote(name, sha256) -> bool` moves the spool host's copy of a
    refused batch out of its inbox; `release_remote(name, sha256) -> bool`
    removes the host's refused/ copy of a batch now acked (None: no spool
    host). Pure apart from the file moves, so the test suite drives it with
    fakes; an error on one batch is counted, never raised.

    acked: in the store; refused: set aside (422); failed: still waiting, in
    staging or, for a refused batch whose host copy could not be moved, in the
    host's inbox (the next cycle stages it again and retries the move)."""
    delivered.mkdir(parents=True, exist_ok=True)
    out = {"replayed": 0, "acked": 0, "refused": 0, "failed": 0, "remote_deleted": 0}
    for f in sorted(staging.glob("*.json")):
        out["replayed"] += 1
        try:
            res = _replay_one(f, post, delete_remote, delivered, refused,
                              set_aside_remote, release_remote, daemon_started_at)
        except Exception as e:  # noqa: BLE001 - one batch's error never holds the rest
            res = {"failed": 1}
            if errors is not None:
                errors.append(f"{_safe_name(f.name)}: {type(e).__name__}")
        for k, v in res.items():
            out[k] += v
    return out


def requeue(names: list[str], refused: Path = REFUSED, staging: Path = STAGING,
            why: str = "requeue") -> tuple[list[str], list[str]]:
    """Copy refused batches back into staging for the next cycle. The refused
    copy stays until heliosd acks the batch. Names may be paths (the file name
    is used). Returns (requeued, not found)."""
    held = {p.name: p for p in sorted(refused.glob("*.json"))} if refused.is_dir() else {}
    wanted = list(held) if "--all" in names else [Path(n).name for n in names]
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
        record_event(refused, {"event": "requeued", "name": name, "why": why})
        done.append(name)
    _fsync_dir(staging)
    return done, missing


def retry_after_restart(daemon_started_at: str | None, refused: Path = REFUSED,
                        staging: Path = STAGING) -> list[str]:
    """Requeue, once per heliosd process, every set-aside batch whose last
    refusal came from an earlier process: a fix or an upgrade takes a restart,
    and the new heliosd may store what the old one refused. Bounded: the next
    refusal records this process's start."""
    if not daemon_started_at or not refused.is_dir():
        return []
    last = {}
    for ev in trail(refused):
        if ev.get("event") == "refused":
            last[str(ev.get("name"))] = ev
    names = [p.name for p in sorted(refused.glob("*.json"))
             if last.get(p.name, {}).get("daemon_started_at") != daemon_started_at
             and not (staging / p.name).exists()]
    return requeue(names, refused, staging, why="heliosd restarted")[0] if names else []


def refused_batches(refused: Path = REFUSED) -> list[dict]:
    """The batches waiting in refused/, oldest first, each with its latest
    refusal reason from the trail."""
    if not refused.is_dir():
        return []
    reasons = {str(ev.get("name")): ev for ev in trail(refused) if ev.get("event") == "refused"}
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
    import httpx  # here, so `status`, `refused` and `requeue` run with any python

    cfg = _cfg()
    if not cfg["remote"]:
        log("no [relay] remote configured; nothing to do")
        return 0
    spool = str(cfg["remote_spool"])
    if not SAFE_SPOOL.match(spool) or spool.startswith("/") or ".." in spool.split("/"):
        log("aborted: [relay] remote_spool must be a relative path of letters, digits and ._/-")
        return 2
    RELAY.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another run is in progress; this one stops")
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
        return run_remote(cfg["remote"], remote_delete_cmd(spool, name)) == "DELETED"

    def set_aside_remote(name: str, digest: str) -> bool:
        word = run_remote(cfg["remote"], remote_set_aside_cmd(spool, name, digest))
        log(f"refused {name}: heliosd answered {REFUSED_STATUS}; kept in {REFUSED} ({REFUSALS_NAME}); "
            f"spool host copy: {word or 'not moved, retried next cycle'}")
        return word in SET_ASIDE_OK

    def release_remote(name: str, digest: str) -> bool:
        word = run_remote(cfg["remote"], remote_release_cmd(spool, name, digest))
        log(f"acked {name} after it was set aside; spool host refused/ copy: {word or 'not reached, retried next cycle'}")
        return word in RELEASE_OK

    try:
        started = client.get("/api/health").json().get("started_at")
    except Exception:  # noqa: BLE001 - heliosd down: no retry this cycle
        started = None
    for name in retry_after_restart(started):
        log(f"requeued {name}: heliosd restarted since it was refused")

    fetch_failed = None
    try:
        staged = fetch(cfg["remote"], spool)
    except (RuntimeError, subprocess.SubprocessError, OSError) as e:
        fetch_failed = e
        staged = len(list(STAGING.glob("*.json"))) if STAGING.is_dir() else 0
        log(f"fetch failed: {e}; replaying the {staged} batch(es) already staged")
    errors: list[str] = []
    res = replay(post, delete_remote, set_aside_remote=set_aside_remote, release_remote=release_remote,
                 daemon_started_at=started, errors=errors)
    for err in errors:
        log(f"batch error: {err}")
    pruned = prune_delivered(int(cfg["keep_delivered_days"]))
    log(f"cycle staged={staged} replayed={res['replayed']} acked={res['acked']} refused={res['refused']} "
        f"failed={res['failed']} remote_deleted={res['remote_deleted']} pruned={pruned}")
    if fetch_failed is not None:
        return 1
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
    print(f"{len(rows)} refused; each is retried once after a heliosd restart; "
          f"`requeue <name>` or `requeue --all` replays them on the next cycle")
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
