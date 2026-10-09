#!/usr/bin/env python
"""Wave 3 back-pull tool (fix program B14; design docs/briefs/build-2026-10-09/
wave3/design.md sections 4 and 5, item C3): fetch every Whoop record back to
the first one into a private file with the daemon's token, never refreshing
it; then apply that file to a store (a copy first, the live store only after
the owner's yes, with heliosd stopped).

    cd server && <venv>/bin/python tools/whoop_backpull.py fetch \
        --token-path ~/Helios/data/whoop_tokens.json --out <private dir> \
        [--pace 2] [--max-requests 300] [--retries 3] [--min-token-minutes 25] [--resume]
    cd server && HELIOS_HOME=<stage> <venv>/bin/python tools/whoop_backpull.py apply \
        <store.duckdb> <private dir> --out DIR [--expect-zone Asia/Dubai] [--apply]

Token rules (design 4.1 (a)). Whoop rotates the refresh token at every
refresh and kills the old one, and of two refreshes sent at once only the
first succeeds, so the daemon must stay the only process that refreshes
Helios's grant:
- the fetch never calls the token URL and never writes, copies, logs or
  prints the token file; it only reads it, before every request;
- it starts only when the daemon will not refresh for at least
  --min-token-minutes (the daemon refreshes inside a pull once the token is
  older than its lifetime minus 5 minutes), and stops when that drops under
  2 minutes;
- on a 401 it reads the file once more and retries once only when the
  daemon has meanwhile saved a new access token; otherwise it stops.
Its own error state file lives in --out, never beside the token file.

fetch writes <out>/records.jsonl (0600; one {"kind", "record"} per line, the
record as Whoop sent it; appended and synced per page) and manifest.json
(counts per kind and month, oldest and newest start, complete per kind,
requests, retries, 429s, 401s, seconds, code commit, the file's sha256).
--resume continues each incomplete kind from its oldest saved record. Health
payloads: keep --out private, outside any synced or git folder.

apply refuses the live store unless --apply and no process holds it (the
checks of tools/rebuild_derived.py); checks the file's sha256 against the
manifest; stores the records with apply_records and one cache sweep; deletes
the journal rows it created (the rebuild that follows recomputes every
date); checks that every record is stored with the same or a newer revision
and that every scored record has its samples; records migration
wave3_whoop_backpull_v1 (phase verified, fingerprint the sha256); reopens
the store through init_schema. summary.json holds counts and dates only.
A second apply of the same file writes nothing.

Exit codes: 0 done, 1 stopped by a check or a failed step, 2 refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import heliosd  # noqa: E402
import rebuild_derived as rd  # noqa: E402  (check_store, is_live, holders, Refused: one definition of "live")
from heliosd.config import load_settings  # noqa: E402
from heliosd.ingest import whoop  # noqa: E402
from heliosd.ingest.normalize import to_utc_naive, to_wall  # noqa: E402
from heliosd.migrate.rebase_history import _code_commit  # noqa: E402
from heliosd.store import db  # noqa: E402
from heliosd.trust.policy import MetricPolicy  # noqa: E402

FETCH_KINDS = ("cycle", "recovery", "sleep")
MIGRATION = "wave3_whoop_backpull_v1"
RECORDS = "records.jsonl"
MANIFEST = "manifest.json"
STOP_MINUTES = 2.0            # stop the walk this close to the daemon's own refresh


class Refused(Exception):
    """Nothing was fetched or opened: a precondition failed."""


class TokenStop(RuntimeError):
    """The token rules stopped the walk (records saved so far are kept)."""


# ---------------------------------------------------------------- the read-only token client

class ReadOnlyTokenClient(whoop.WhoopClient):
    """WhoopClient that borrows the daemon's access token and never refreshes
    it: no call to the token URL, no write of the token file, and its own
    error state file in `state_dir`. max_total caps every HTTP attempt."""

    def __init__(self, token_path: Path, state_dir: Path, max_total: int | None = None):
        super().__init__({"token_path": str(Path(token_path).expanduser()),
                          "state_path": str(Path(state_dir) / "whoop_pull_state.json"),
                          "client_id": "", "client_secret": "", "redirect_uri": ""})
        self.max_total = max_total
        self.http_401 = 0
        self._used: str | None = None          # the access token of the last request; memory only

    def _read(self) -> tuple[dict | None, float | None]:
        """One read of the token file: (tokens, minutes until the daemon's next
        refresh is due, computed exactly as WhoopClient._access_token decides
        it), or (None, None) without a readable file."""
        try:
            t = self._tokens()
            saved = datetime.fromisoformat(t["saved_at"]) if t else None
        except (OSError, ValueError, KeyError, TypeError):
            return None, None
        if saved is None or not t.get("access_token"):
            return None, None
        age = (datetime.now() - saved).total_seconds()
        return t, (t.get("expires_in", 3600) - 300 - age) / 60

    def minutes_to_refresh(self) -> float | None:
        return self._read()[1]

    def _access_token(self) -> str | None:
        if self.max_total is not None and self.http_stats["requests"] >= self.max_total:
            raise whoop.RequestCapReached(f"stopped at the request cap of {self.max_total}")
        t, left = self._read()
        if t is None:
            return None
        if left < STOP_MINUTES:
            raise TokenStop(f"the daemon's refresh is due in {max(0, int(left))} minute(s): stopping before it")
        self._used = t["access_token"]
        return self._used

    def _save_tokens(self, tokens: dict) -> None:
        raise TokenStop("the back-pull never writes the token file")

    def exchange_code(self, code: str) -> None:
        raise TokenStop("the back-pull never logs in")

    def _get(self, path: str, params: dict, *, retries: int = 0, sleep=time.sleep) -> dict:
        try:
            return super()._get(path, params, retries=retries, sleep=sleep)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 401:
                raise
            self.http_401 += 1
            used = self._used
        now_held = (self._read()[0] or {}).get("access_token")
        if not now_held or now_held == used:
            raise TokenStop("HTTP 401 and the daemon's token file holds no newer token: stopping")
        try:                                   # the daemon refreshed meanwhile: one more try with its token
            return super()._get(path, params, retries=retries, sleep=sleep)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 401:
                raise
            self.http_401 += 1
            raise TokenStop("HTTP 401 again with the daemon's new token: stopping") from None


# ---------------------------------------------------------------- helpers

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_private_json(path: Path, data: dict) -> None:
    """Atomic 0600 JSON write (manifest, summary)."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, default=str))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _start_of(kind: str, rec: dict) -> str | None:
    """The instant a record's walk position is keyed by (a recovery has no start)."""
    return rec.get("created_at") if kind == "recovery" else rec.get("start")


def _describe(e: BaseException) -> str:
    """An error for the log and the manifest: the type and an HTTP status,
    never a URL, a header or a payload."""
    status = getattr(getattr(e, "response", None), "status_code", None)
    if isinstance(e, (TokenStop, whoop.RequestCapReached, Refused)):
        return f"{type(e).__name__}: {e}"
    return type(e).__name__ + (f" (HTTP {status})" if status else "")


class KindStats:
    def __init__(self, prior: dict | None = None):
        p = prior or {}
        self.records = 0
        self.months: Counter = Counter()
        self.oldest: str | None = None
        self.newest: str | None = None
        self.pages = int(p.get("pages", 0))
        self.complete = bool(p.get("complete", False))
        self.no_id = 0

    def add(self, kind: str, rec: dict) -> None:
        self.records += 1
        s = _start_of(kind, rec)
        if s:
            self.months[str(s)[:7]] += 1
            inst = whoop.parse_iso_utc(s)
            if self.oldest is None or inst < whoop.parse_iso_utc(self.oldest):
                self.oldest = s
            if self.newest is None or inst > whoop.parse_iso_utc(self.newest):
                self.newest = s

    def as_dict(self) -> dict:
        return {"records": self.records, "complete": self.complete, "pages": self.pages,
                "oldest_start": self.oldest, "newest_start": self.newest, "records_without_id": self.no_id,
                "by_month_utc": dict(sorted(self.months.items()))}


def read_records(path: Path) -> list[tuple[str, dict]]:
    out = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                out.append((str(row["kind"]), row["record"]))
            except (ValueError, KeyError, TypeError) as e:
                raise Refused(f"{path.name} line {i} is not a record line ({type(e).__name__})") from None
    return out


def _record_key(kind: str, rec: dict) -> str | None:
    try:
        return whoop.record_identity(kind, rec)[1]
    except ValueError:
        return None


# ---------------------------------------------------------------- fetch

def fetch(token_path, out, *, pace: float = 2.0, max_requests: int = 300, retries: int = 3,
          min_token_minutes: float = 25.0, resume: bool = False, sleep=time.sleep, log=print) -> int:
    """Walk cycle, recovery and sleep back to the first record into
    <out>/records.jsonl. Returns the exit code."""
    t0 = time.time()
    out = Path(out).expanduser()
    rec_path, man_path = out / RECORDS, out / MANIFEST
    started = datetime.now()
    try:
        prior: dict = {}
        if resume:
            if not (rec_path.is_file() and man_path.is_file()):
                raise Refused(f"--resume needs {RECORDS} and {MANIFEST} in {out}")
            prior = json.loads(man_path.read_text(encoding="utf-8"))
            if prior.get("records_sha256") and prior["records_sha256"] != sha256_file(rec_path):
                raise Refused(f"{RECORDS} no longer matches its manifest; start a new --out")
        elif rec_path.exists() or man_path.exists():
            raise Refused(f"{out} already holds a fetch; pass --resume or a new --out")
        client = ReadOnlyTokenClient(token_path, out, max_total=None)
        minutes = client.minutes_to_refresh()
        if minutes is None:
            raise Refused(f"no readable token file at {Path(token_path).expanduser()}")
        log(f"token: {int(minutes)} minute(s) before the daemon's next refresh (need {int(min_token_minutes)})")
        if minutes < min_token_minutes:
            raise Refused(f"the daemon's refresh is due in {int(minutes)} minute(s); start within 20 minutes after "
                          f"the daemon's own refresh")
    except Refused as e:
        log(f"refused: {e}")
        return 2

    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    prior_kinds = prior.get("kinds", {})
    stats = {k: KindStats(prior_kinds.get(k)) for k in FETCH_KINDS}
    seen: set[str] = set()
    if resume:
        for kind, rec in read_records(rec_path):
            key = _record_key(kind, rec)
            if key is not None:
                seen.add(key)
            if kind in stats:
                stats[kind].add(kind, rec)
                if key is None:
                    stats[kind].no_id += 1
    totals = Counter({k: int(prior.get(k, 0)) for k in ("requests", "retries", "http_429", "http_401", "http_5xx")})
    budget = max(0, max_requests)
    client.max_total = budget
    stopped = None
    runs = list(prior.get("runs", []))

    def manifest(final: bool) -> dict:
        m = {"tool": "whoop_backpull fetch", "version": 1, "code_commit": _code_commit(Path(heliosd.__file__).parent),
             "started": prior.get("started", started.isoformat(timespec="seconds")),
             "finished": datetime.now().isoformat(timespec="seconds"),
             "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "token_minutes_at_start": int(minutes), "min_token_minutes": int(min_token_minutes),
             "pace_s": pace, "max_requests": max_requests, "retries_per_request": retries,
             "kinds": {k: s.as_dict() for k, s in stats.items()},
             "complete": all(s.complete for s in stats.values()),
             "records_total": sum(s.records for s in stats.values()),
             "stopped": stopped, "runs": runs, "records_file": RECORDS}
        here = {"requests": client.http_stats["requests"], "retries": client.http_stats["retries"],
                "http_429": client.http_stats["http_429"], "http_5xx": client.http_stats["http_5xx"],
                "http_401": client.http_401}
        for k, v in here.items():
            m[k] = totals[k] + v
        m["seconds"] = round(float(prior.get("seconds", 0)) + (time.time() - t0), 1)
        m["records_sha256"] = sha256_file(rec_path) if rec_path.exists() else None
        if final:
            m["runs"] = runs + [{"started": started.isoformat(timespec="seconds"), "resume": resume, **here,
                                 "token_minutes_at_start": int(minutes), "stopped": stopped}]
        return m

    fd = os.open(rec_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        for i, kind in enumerate(FETCH_KINDS):
            st = stats[kind]
            if st.complete:
                log(f"{kind}: complete in the earlier run ({st.records} records)")
                continue
            end = None
            if resume and st.oldest:
                end = whoop.parse_iso_utc(st.oldest) + timedelta(seconds=1)   # the oldest saved record comes again, once
                log(f"{kind}: resuming before {st.oldest[:10]}")
            left = budget - client.http_stats["requests"]
            if left <= 0:
                stopped = f"{kind}: the request cap of {max_requests} was reached"
                break

            def on_page(recs: list[dict], nxt, kind=kind, st=st) -> None:
                lines = []
                for rec in recs:
                    key = _record_key(kind, rec)
                    if key is not None and key in seen:
                        continue
                    if key is None:
                        st.no_id += 1
                    else:
                        seen.add(key)
                    lines.append(json.dumps({"kind": kind, "record": rec}, separators=(",", ":")))
                    st.add(kind, rec)
                if lines:
                    f.write("\n".join(lines) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                st.pages += 1
                log(f"{kind}: page {st.pages}, {st.records} records, oldest {str(st.oldest)[:10]}"
                    + ("" if nxt else ", last page"))

            if i and pace:
                sleep(pace)
            try:
                client._paged(whoop.PATHS[kind], None, end, pace_s=pace, max_requests=left, on_page=on_page,
                              retries=retries, sleep=sleep)
                st.complete = True
            except Exception as e:                    # noqa: BLE001 - every stop is recorded; the records so far stay
                stopped = f"{kind}: {_describe(e)}"
                break
            finally:
                _write_private_json(man_path, manifest(final=False))
    if stopped:
        log(f"STOP: {stopped}")
    m = manifest(final=True)
    _write_private_json(man_path, m)
    log(json.dumps({"complete": m["complete"], "records_total": m["records_total"], "requests": m["requests"],
                    "retries": m["retries"], "http_429": m["http_429"], "http_401": m["http_401"],
                    "seconds": m["seconds"], "stopped": stopped,
                    "kinds": {k: {"records": v["records"], "complete": v["complete"], "oldest": str(v["oldest_start"])[:10]}
                              for k, v in m["kinds"].items()}}))
    return 0 if m["complete"] and not stopped else 1


# ---------------------------------------------------------------- apply

def run_checks(conn, records: list[tuple[str, dict]], zone) -> dict:
    """Design 4.4 checks 3 and 4 over the file's records, read back from the
    store: the stored revision is the same or newer; every recovery's cycle
    and sleep resolve (the rest listed, not a failure); every scored record
    has exactly the samples its stored payload yields. Dates, never values."""
    stored = {k: (u, p, s, n) for k, u, p, s, n in db.fetchall(
        conn, "SELECT record_key, updated_at, payload, score_state, nap FROM whoop_records")}
    cycles = {r[0] for r in db.fetchall(conn, "SELECT native_id FROM whoop_records WHERE kind = 'cycle'")}
    sleeps = {r[0] for r in db.fetchall(conn, "SELECT native_id FROM whoop_records WHERE kind = 'sleep'")}
    have: dict[str, set[str]] = {}
    for (sid,) in db.fetchall(conn, "SELECT sample_id FROM samples WHERE sample_id LIKE 'wh:%:%:%'"):
        _, metric, key = sid.split(":", 2)
        have.setdefault(key, set()).add(metric)

    def day(kind: str, rec: dict) -> str | None:
        try:
            inst = whoop.parse_iso_utc(_start_of(kind, rec) if kind != "sleep" else rec.get("end") or rec.get("start"))
        except (TypeError, ValueError):
            return None
        return str(to_wall(inst, zone).date()) if inst else None

    missing, older, unresolved, bad, info = [], [], [], [], Counter()
    for kind, rec in records:
        key = _record_key(kind, rec)
        if key is None:
            continue
        row = stored.get(key)
        if row is None:
            missing.append({"kind": kind, "date": day(kind, rec)})
            continue
        upd = whoop.parse_iso_utc(rec.get("updated_at")) or whoop.parse_iso_utc(rec.get("created_at"))
        if row[0] is not None and upd is not None and row[0] < to_utc_naive(upd):
            older.append({"kind": kind, "date": day(kind, rec)})
        try:
            payload = json.loads(row[1] or "null")
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            bad.append({"kind": kind, "date": day(kind, rec), "problem": "unreadable payload"})
            continue
        if kind == "recovery":                     # the stored revision's links
            gaps = [f for f, ids in (("cycle_id", cycles), ("sleep_id", sleeps))
                    if payload.get(f) is None or str(payload[f]) not in ids]
            if gaps:
                unresolved.append({"date": day(kind, rec), "missing": gaps})
        want = {sp["metric"] for sp in whoop.derive_samples(kind, payload)}
        got = have.get(key, set())
        if want != got:
            bad.append({"kind": kind, "date": day(kind, rec), "missing": sorted(want - got), "extra": sorted(got - want)})
        if whoop.score_state(payload) == "SCORED":
            expect = {"sleep": () if payload.get("nap") else ("sleep_duration",),
                      "recovery": ("recovery_score", "hrv_rmssd", "resting_hr"), "cycle": ("strain",)}[kind]
            for metric in expect:
                if metric not in want:
                    info[f"scored_{kind}_without_{metric}"] += 1
    return {"stored_same_or_newer": {"ok": not missing and not older, "missing": missing[:50], "n_missing": len(missing),
                                     "older": older[:50], "n_older": len(older)},
            "recoveries_resolve": {"n_unresolved": len(unresolved), "unresolved": unresolved[:50],
                                   "n_recoveries": sum(1 for k, _ in records if k == "recovery")},
            "samples": {"ok": not bad, "n_bad": len(bad), "bad": bad[:50],
                        "payload_without_a_metric": dict(sorted(info.items()))}}


def apply(store, recdir, out, *, apply_live: bool = False, expect_zone: str | None = None,
          policy: MetricPolicy | None = None, log=print) -> tuple[int, dict]:
    """Apply a fetched file to a store. Returns (exit code, summary)."""
    t0 = time.time()
    store, recdir, out = Path(store).expanduser(), Path(recdir).expanduser(), Path(out).expanduser()
    S: dict = {"tool": "whoop_backpull apply", "store": str(store), "records_dir": str(recdir), "apply": apply_live,
               "heliosd_file": heliosd.__file__, "code_commit": _code_commit(Path(heliosd.__file__).parent),
               "started": datetime.now().isoformat(timespec="seconds"), "stopped": None}
    try:
        try:
            rd.check_store(store, apply_live)
        except rd.Refused as e:
            raise Refused(str(e)) from None
        rec_path, man_path = recdir / RECORDS, recdir / MANIFEST
        if not (rec_path.is_file() and man_path.is_file()):
            raise Refused(f"no {RECORDS} and {MANIFEST} in {recdir}")
        policy = policy or MetricPolicy(default_tz=load_settings().timezone)
        S["zone"] = policy.reporting_timezone
        if expect_zone and policy.reporting_timezone != expect_zone:
            raise Refused(f"the policy's reporting zone is {policy.reporting_timezone}, not {expect_zone}: "
                          "stage a metric_policy.yaml with reporting_timezone, or run with HELIOS_HOME=~/Helios")
    except Refused as e:
        log(f"refused: {e}")
        S["stopped"] = f"refused: {e}"
        return 2, S
    out.mkdir(parents=True, exist_ok=True)
    conn = None
    try:
        manifest = json.loads(man_path.read_text(encoding="utf-8"))
        sha = sha256_file(rec_path)
        S["records_sha256"] = sha
        if manifest.get("records_sha256") != sha:
            raise RuntimeError(f"{RECORDS} does not match the sha256 in its manifest: refusing to apply it")
        if not manifest.get("complete"):
            raise RuntimeError("the fetch is incomplete (a kind did not reach its last page): run fetch --resume first")
        records = read_records(rec_path)
        fetched: dict[str, list[dict]] = {}
        for kind, rec in records:
            fetched.setdefault(kind, []).append(rec)
        fetched_at = datetime.fromisoformat(manifest["finished_utc"])
        batch_id = f"backpull:{sha[:16]}"
        S["records"] = {k: len(v) for k, v in sorted(fetched.items())}
        log(f"heliosd from {heliosd.__file__}; store {store}; zone {policy.reporting_timezone}; "
            f"{len(records)} records, sha256 {sha[:12]}")
        conn = db.connect(store)
        journal_before = {(d, r): (b, e) for d, r, b, e in db.fetchall(
            conn, "SELECT date, reason, batch_id, enqueued_at FROM dirty_dates")}
        t1 = time.time()
        n = whoop.apply_records(conn, policy, fetched, fetched_at, batch_id, cache=False)
        S["seconds_apply"] = round(time.time() - t1, 1)
        # The journal rows this run created go (the full rebuild recomputes every date);
        # a row it overwrote gets its earlier batch and time back.
        deleted = restored = 0
        with db.transaction(conn) as c:
            for d, r in c.execute("SELECT date, reason FROM dirty_dates WHERE batch_id = ?", [batch_id]).fetchall():
                if (d, r) in journal_before:
                    b, e = journal_before[(d, r)]
                    c.execute("UPDATE dirty_dates SET batch_id = ?, enqueued_at = ? WHERE date = ? AND reason = ?", [b, e, d, r])
                    restored += 1
                else:
                    c.execute("DELETE FROM dirty_dates WHERE date = ? AND reason = ?", [d, r])
                    deleted += 1
        dates = n.pop("dates")
        S["counts"] = {**n, "dates_touched": len(dates), "first_date": dates[0] if dates else None,
                       "last_date": dates[-1] if dates else None, "journal_rows_deleted": deleted,
                       "journal_rows_restored": restored}
        checks = run_checks(conn, records, policy.zone)
        S["checks"] = checks
        ok = checks["stored_same_or_newer"]["ok"] and checks["samples"]["ok"]
        wrote_rows = any(n[k] for k in ("recovery", "sleep", "cycle", "retracted", "cache_rows", "cache_removed")) \
            or deleted or restored
        row = db.fetchall(conn, "SELECT input_fingerprint, summary FROM migrations WHERE name = ?", [MIGRATION])
        wrote_migration = False
        if ok and (not row or row[0][0] != sha):
            summary = {"phase": "verified", "migration": MIGRATION, "design": "Wave 3 B14 (design.md section 4)",
                       "records_sha256": sha, "records": S["records"], "fetched_utc": manifest["finished_utc"],
                       "oldest": {k: v.get("oldest_start") for k, v in manifest.get("kinds", {}).items()},
                       "counts": {k: v for k, v in S["counts"].items() if isinstance(v, int)},
                       "verified_by": "whoop_backpull apply: checks 3 and 4 of design 4.4 passed"}
            with db.transaction(conn) as c:
                c.execute("DELETE FROM migrations WHERE name = ?", [MIGRATION])
                c.execute("INSERT INTO migrations (name, applied_at, code_commit, input_fingerprint, summary) "
                          "VALUES (?, ?, ?, ?, ?)", [MIGRATION, datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0),
                                                     S["code_commit"], sha, json.dumps(summary, default=str)])
            wrote_migration = True
        S["migration_written"] = wrote_migration
        S["wrote_nothing"] = not wrote_rows and not wrote_migration
        db.checkpoint(conn)
        conn.close()
        conn = None
        c2 = db.connect(store)                           # init_schema refuses an unverified migration row
        try:
            S["checks"]["reopen"] = {"ok": True, "migration_phase": db.migration_phase(c2, MIGRATION),
                                     "unverified": db.unverified_migrations(c2)}
        finally:
            c2.close()
        if not ok:
            S["stopped"] = "a check failed: see checks (no migration row was written)"
    except Exception as e:                                # noqa: BLE001 - recorded in summary.json, exit 1
        S["stopped"] = f"{type(e).__name__}: {str(e)[:300]}"
    finally:
        if conn is not None:
            conn.close()
        S["seconds"] = round(time.time() - t0, 1)
        S["finished"] = datetime.now().isoformat(timespec="seconds")
        _write_private_json(out / "summary.json", S)
    if S["stopped"]:
        log(f"STOP: {S['stopped']}")
    log(json.dumps({"stopped": S["stopped"], "wrote_nothing": S.get("wrote_nothing"), "counts": S.get("counts"),
                    "checks_ok": {k: v.get("ok") for k, v in (S.get("checks") or {}).items() if "ok" in v},
                    "seconds": S["seconds"]}, default=str))
    return (1 if S["stopped"] else 0), S


# ---------------------------------------------------------------- command line

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Wave 3: the Whoop back-pull to the first record (fetch, then apply)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="walk every record into a private file with the daemon's token (never refreshed)")
    f.add_argument("--token-path", required=True, help="the daemon's token file (read only)")
    f.add_argument("--out", required=True, help="a private folder outside any synced or git folder")
    f.add_argument("--pace", type=float, default=2.0, help="seconds between two requests (default 2)")
    f.add_argument("--max-requests", type=int, default=300, help="cap on every HTTP request (default 300)")
    f.add_argument("--retries", type=int, default=3, help="tries per page for a 429 or a 5xx (default 3)")
    f.add_argument("--min-token-minutes", type=float, default=25.0,
                   help="refuse to start unless the daemon's refresh is at least this far away (default 25)")
    f.add_argument("--resume", action="store_true", help="continue each incomplete kind from its oldest saved record")
    a = sub.add_parser("apply", help="apply a fetched file to a store (a copy; the live store only with --apply)")
    a.add_argument("store")
    a.add_argument("recdir", help="the fetch's --out folder")
    a.add_argument("--out", required=True, help="where summary.json goes")
    a.add_argument("--apply", action="store_true", help="allow the live store, only while no process holds it")
    a.add_argument("--expect-zone", default=None, help="refuse unless the policy's reporting zone is this one")
    args = ap.parse_args(argv)
    if args.cmd == "fetch":
        return fetch(args.token_path, args.out, pace=args.pace, max_requests=args.max_requests, retries=args.retries,
                     min_token_minutes=args.min_token_minutes, resume=args.resume, log=lambda m: print(m, flush=True))
    code, _ = apply(args.store, args.recdir, args.out, apply_live=args.apply, expect_zone=args.expect_zone,
                    log=lambda m: print(m, flush=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
