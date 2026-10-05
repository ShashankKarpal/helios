"""Whoop puller (core, owner-approved): recovery, strain, sleep architecture,
sleep need, respiratory rate, rMSSD. OAuth against the owner's own free Whoop
developer app; tokens stored locally; ingress only, nothing leaves.

Whoop API v2 (v1 was removed 2025-10-01; see developer.whoop.com v1-v2 migration
guide). v2 keeps the score payload shapes this module reads.

Single-source Phase 1a item 9 (plan v2; checkpoint A points 9, 11, 12, 13):
- Native identity. Every API record lands in whoop_records keyed
  <kind>:<native id> (recovery has no id of its own: its cycle_id is the
  identity), with sleep_id and cycle_id as columns, score_state, nap, the
  record's own UTC offset, created_at and updated_at, and the raw payload.
- Samples are keyed by the record: wh:<metric>:<kind>:<id>, written for SCORED
  records only. A revision (same id, newer updated_at) re-derives the record's
  samples from the current payload; a record that is no longer SCORED, or no
  longer carries a field, loses that sample and leaves a tombstone
  (whoop_retracted). Naps are stored as records, never as sleep samples.
- Dates are projections: UTC instants plus reporting-zone wall times, like
  every other row. The query bounds are UTC instants, never the Mac clock.
- Non-destructive replacement of the old day-keyed rows (wh:<metric>:<date>):
  a legacy row is removed only when a SCORED record row for the same metric
  and day is written, and sample_aliases records the transition. Each record's
  samples, aliases, legacy removal and dirty dates commit together.
- whoop_cache (date, kind) stays as the dated projection the Today screen and
  the sleep report read; it is rewritten from whoop_records for the dates of
  the pull window (latest updated_at per date and kind; naps under kind
  sleep_nap). Dates outside the window are never touched.
- Every touched reporting date is journaled in dirty_dates (reason whoop) so
  the recompute loop rebuilds exactly those dates.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

from heliosd.ingest.normalize import to_utc_naive, to_wall
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy

API = "https://api.prod.whoop.com/developer/v2"
AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
SCOPES = "read:recovery read:sleep read:cycles read:profile offline"

KINDS = ("recovery", "sleep", "cycle")
PATHS = {"recovery": "/recovery", "sleep": "/activity/sleep", "cycle": "/cycle"}
ALIAS_REASON = "whoop_record_identity"
RETRACTED = "whoop_retracted"
JOURNAL_REASON = "whoop"


# ---- time helpers (UTC in, UTC out; the reporting zone only for projections) ----

def parse_iso_utc(s) -> datetime | None:
    """Whoop ISO8601 ('...Z' or with offset) -> aware UTC datetime."""
    if not s:
        return None
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_z(dt: datetime) -> str:
    """Query-bound rendering: the instant in UTC with a Z suffix, whatever zone
    the caller's datetime carries. A naive datetime is read as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def offset_minutes(tz: str | None) -> int | None:
    """Whoop timezone_offset ('+04:00', '-05:30', '+0400', 'Z') -> minutes."""
    if not tz:
        return None
    tz = str(tz).strip()
    if tz == "Z":
        return 0
    sign = -1 if tz[0] == "-" else 1
    body = tz[1:].replace(":", "") if tz[0] in "+-" else None
    if not body or len(body) != 4 or not body.isdigit():
        return None
    return sign * (int(body[:2]) * 60 + int(body[2:]))


def _naive_utc_as_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# ---- record semantics ----

def score_state(rec: dict) -> str:
    st = rec.get("score_state")
    if st:
        return str(st)
    return "SCORED" if rec.get("score") else "PENDING_SCORE"


def record_identity(kind: str, rec: dict) -> tuple[str, str]:
    """(native_id, record_key). Recovery carries no id of its own in the API,
    so its cycle_id is the identity, under kind recovery (ids collide across
    kinds, hence the kind prefix)."""
    native = rec.get("cycle_id") if kind == "recovery" else rec.get("id")
    if native is None:
        raise ValueError(f"whoop {kind} record without an id")
    native = str(native)
    return native, f"{kind}:{native}"


def projection_date(kind: str, start_utc: datetime | None, end_utc: datetime | None,
                    created_utc: datetime | None, zone) -> date | None:
    """The reporting date a record files under: recovery by created_at, sleep
    by its end, cycle by its start (the dates the old puller used; Phase 1c
    changes day bases through the policy)."""
    inst = {"recovery": created_utc, "sleep": end_utc, "cycle": start_utc}.get(kind)
    inst = _naive_utc_as_aware(inst)
    return to_wall(inst, zone).date() if inst is not None else None


def derive_samples(kind: str, rec: dict) -> list[dict]:
    """The metric samples a SCORED record yields, each {metric, value, unit,
    start_utc, end_utc} with aware UTC instants. Empty for PENDING_SCORE,
    UNSCORABLE and naps: those records are stored, but produce no sample."""
    if score_state(rec) != "SCORED":
        return []
    sc = rec.get("score") or {}
    out: list[tuple] = []
    if kind == "recovery":
        created = parse_iso_utc(rec.get("created_at"))
        if created is None:
            return []
        if sc.get("recovery_score") is not None:
            out.append(("recovery_score", float(sc["recovery_score"]), "%", created, created))
        if sc.get("hrv_rmssd_milli") is not None:
            out.append(("hrv_rmssd", float(sc["hrv_rmssd_milli"]), "ms", created, created))
    elif kind == "sleep":
        if rec.get("nap"):
            return []
        s, e = parse_iso_utc(rec.get("start")), parse_iso_utc(rec.get("end"))
        if s is None or e is None:
            return []
        stages = sc.get("stage_summary") or {}
        asleep_ms = sum(stages.get(k, 0) for k in
                        ("total_light_sleep_time_milli", "total_slow_wave_sleep_time_milli",
                         "total_rem_sleep_time_milli"))
        if asleep_ms:
            out.append(("sleep_duration", round(asleep_ms / 3.6e6, 2), "h", s, e))
        if sc.get("respiratory_rate") is not None:
            out.append(("respiratory_rate", float(sc["respiratory_rate"]), "count/min", s, e))
        need = (sc.get("sleep_needed") or {}).get("baseline_milli")
        if need:
            out.append(("sleep_need", round(need / 3.6e6, 2), "h", s, e))
    elif kind == "cycle":
        s, e = parse_iso_utc(rec.get("start")), parse_iso_utc(rec.get("end"))
        if s is None:
            return []
        if sc.get("strain") is not None:
            out.append(("strain", float(sc["strain"]), "score", s, e or s))   # e is None for the open cycle
    return [{"metric": m, "value": v, "unit": u, "start_utc": a, "end_utc": b} for m, v, u, a, b in out]


class WhoopClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.token_path = Path(cfg["token_path"])
        # Last failure of a token refresh or pull, for the watchdog to show.
        # None once a pull succeeds again. Message only, never a token.
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None

    def _fail(self, what: str, exc: Exception) -> None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        self.last_error = f"{what} failed" + (f" (HTTP {status})" if status else f": {type(exc).__name__}")
        self.last_error_at = datetime.now()

    # ---- OAuth ----
    def login_url(self, state: str = "helios-whoop-oauth") -> str:
        # Whoop requires state to be >= 8 characters for CSRF entropy, otherwise
        # it rejects the request with invalid_state before the consent screen.
        q = {"client_id": self.cfg["client_id"], "redirect_uri": self.cfg["redirect_uri"],
             "response_type": "code", "scope": SCOPES, "state": state}
        return f"{AUTH_URL}?{urlencode(q)}"

    def exchange_code(self, code: str) -> None:
        r = httpx.post(TOKEN_URL, data={
            "grant_type": "authorization_code", "code": code,
            "client_id": self.cfg["client_id"], "client_secret": self.cfg["client_secret"],
            "redirect_uri": self.cfg["redirect_uri"]}, timeout=30)
        r.raise_for_status()
        self._save_tokens(r.json())

    def _save_tokens(self, tokens: dict) -> None:
        """Atomic: write a 0600 temp file beside the token file, then rename it
        over the old one, so a crash mid-write never leaves a truncated token
        file (a truncated file meant a silent re-authorization). The file holds
        the Whoop access and refresh tokens; 0600 since audit 2026-09-02."""
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        tokens["saved_at"] = datetime.now().isoformat()
        fd, tmp = tempfile.mkstemp(prefix=".whoop_tokens.", suffix=".tmp", dir=str(self.token_path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(tokens))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.token_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            os.chmod(self.token_path, 0o600)
        except OSError:
            pass

    def _tokens(self) -> dict | None:
        if self.token_path.exists():
            return json.loads(self.token_path.read_text())
        return None

    def _access_token(self) -> str | None:
        t = self._tokens()
        if not t:
            return None
        age = (datetime.now() - datetime.fromisoformat(t["saved_at"])).total_seconds()
        if age > t.get("expires_in", 3600) - 300:
            try:
                r = httpx.post(TOKEN_URL, data={
                    "grant_type": "refresh_token", "refresh_token": t["refresh_token"],
                    "client_id": self.cfg["client_id"], "client_secret": self.cfg["client_secret"],
                    "scope": "offline"}, timeout=30)
                r.raise_for_status()
            except httpx.HTTPError as e:
                # A rejected refresh (expired or revoked grant) used to surface
                # only as a traceback in the log; now the watchdog shows it and
                # names the fix (re-authorize at /whoop/login). Audit B9.
                self._fail("token refresh", e)
                raise
            t = r.json() | {"saved_at": datetime.now().isoformat()}
            self._save_tokens(t)
        return t["access_token"]

    def _get(self, path: str, params: dict) -> dict:
        tok = self._access_token()
        if not tok:
            self.last_error = "not authorized: visit /whoop/login"
            self.last_error_at = datetime.now()
            raise RuntimeError("Whoop not authorized. Visit /whoop/login first.")
        try:
            r = httpx.get(f"{API}{path}", params=params,
                          headers={"Authorization": f"Bearer {tok}"}, timeout=30)
            r.raise_for_status()
        except httpx.HTTPError as e:
            self._fail(f"GET {path}", e)
            raise
        self.last_error = None
        self.last_error_at = None
        return r.json()

    def _paged(self, path: str, start: datetime, end: datetime) -> list[dict]:
        """Every record in [start, end]; the bounds are rendered as UTC
        instants whatever zone they carry."""
        records, token = [], None
        while True:
            params = {"start": iso_z(start), "end": iso_z(end), "limit": 25}
            if token:
                params["nextToken"] = token
            page = self._get(path, params)
            records += page.get("records", [])
            token = page.get("next_token")
            if not token:
                return records


def store_direct_sample(conn, metric: str, record_key: str, value: float, unit: str,
                        start_utc: datetime, end_utc: datetime | None, zone,
                        score_state: str = "SCORED", src_offset_min: int | None = None) -> str:
    """Store one Whoop-derived sample keyed by its native record
    (wh:<metric>:<kind>:<id>), with UTC instants and reporting-zone wall
    times. Returns the sample id. Must be called inside db.transaction (uses
    the raw connection)."""
    start_utc = _naive_utc_as_aware(start_utc)
    end_utc = _naive_utc_as_aware(end_utc)
    sid = f"wh:{metric}:{record_key}"
    conn.execute("DELETE FROM samples WHERE sample_id = ?", [sid])
    conn.execute("""
        INSERT INTO samples
          (sample_id, metric, hk_type, value, text_value, unit, start_ts, end_ts,
           source_name, device_key, sync_path, start_utc, end_utc, src_offset_min, time_source, score_state)
        VALUES (?, ?, NULL, ?, NULL, ?, ?, ?, 'WHOOP', 'whoop', 'whoop_live', ?, ?, ?, 'whoop_api', ?)""",
        [sid, metric, value, unit, to_wall(start_utc, zone), to_wall(end_utc or start_utc, zone),
         to_utc_naive(start_utc), to_utc_naive(end_utc or start_utc), src_offset_min, score_state])
    return sid


def apply_record(c, kind: str, rec: dict, policy: MetricPolicy, fetched_at: datetime,
                 batch_id: str) -> dict:
    """Store one API record and re-derive its samples. Runs inside
    db.transaction on the raw connection, so the record row, its samples, the
    legacy-row replacement, aliases, tombstones and dirty dates commit as one.
    Returns {"dirty": set of reporting dates, "samples", "retracted", "replaced"}."""
    zone = policy.zone
    native, key = record_identity(kind, rec)
    state = score_state(rec)
    created = parse_iso_utc(rec.get("created_at"))
    updated = parse_iso_utc(rec.get("updated_at")) or created
    start = created if kind == "recovery" else parse_iso_utc(rec.get("start"))
    end = None if kind == "recovery" else parse_iso_utc(rec.get("end"))
    nap = bool(rec.get("nap")) if kind == "sleep" else None
    offset = offset_minutes(rec.get("timezone_offset"))
    sleep_id = str(rec["id"]) if kind == "sleep" else (str(rec["sleep_id"]) if rec.get("sleep_id") is not None else None)
    cycle_id = str(rec["id"]) if kind == "cycle" else (str(rec["cycle_id"]) if rec.get("cycle_id") is not None else None)

    # 0. Revisions (checkpoint B, point 5): a stored NEWER revision wins over an
    #    older payload (an overlapping pull, a stale page), and an identical
    #    payload is a no-op that rewrites nothing and dirties nothing.
    stored = c.execute("SELECT updated_at, payload FROM whoop_records WHERE record_key = ?", [key]).fetchone()
    if stored is not None:
        stored_updated, stored_payload = stored
        incoming_updated = to_utc_naive(updated)
        if stored_updated is not None and incoming_updated is not None and stored_updated > incoming_updated:
            return {"dirty": set(), "samples": 0, "retracted": 0, "replaced": 0, "skipped": "older"}
        if stored_updated == incoming_updated and _same_payload(stored_payload, rec):
            return {"dirty": set(), "samples": 0, "retracted": 0, "replaced": 0, "skipped": "unchanged"}

    # 1. The record row: one per (kind, id); a revision replaces it.
    c.execute("DELETE FROM whoop_records WHERE record_key = ?", [key])
    c.execute("""INSERT INTO whoop_records (record_key, kind, native_id, sleep_id, cycle_id, start_utc, end_utc,
                 src_offset_min, score_state, nap, created_at, updated_at, payload, fetched_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
              [key, kind, native, sleep_id, cycle_id, to_utc_naive(start), to_utc_naive(end), offset, state, nap,
               to_utc_naive(created), to_utc_naive(updated), json.dumps(rec), to_utc_naive(fetched_at)])

    # 2. Samples this record produced before (any metric): their dates move too.
    old = c.execute("SELECT sample_id, metric, start_ts, end_ts, start_utc FROM samples WHERE sample_id LIKE ?",
                    [f"wh:%:{key}"]).fetchall()
    dirty: set[date] = set()
    for _sid, _m, sts, ets, _su in old:
        dirty.add(sts.date())
        if ets is not None:
            dirty.add(ets.date())

    # 3. Re-derive from the current payload (SCORED only).
    new_ids: set[str] = set()
    replaced = 0
    for sp in derive_samples(kind, rec):
        sid = store_direct_sample(c, sp["metric"], key, sp["value"], sp["unit"], sp["start_utc"], sp["end_utc"],
                                  zone, score_state=state, src_offset_min=offset)
        new_ids.add(sid)
        c.execute("DELETE FROM tombstones WHERE tomb_id = ?", [sid])     # a re-scored record is live again
        ws, we = to_wall(sp["start_utc"], zone), to_wall(sp["end_utc"], zone)
        dirty.update({ws.date(), we.date()})
        # The old puller keyed this metric by day; that row is replaced only now
        # that a SCORED record row for the same day exists (checkpoint A, 12).
        day = projection_date(kind, sp["start_utc"], sp["end_utc"], sp["start_utc"], zone)
        legacy_id = f"wh:{sp['metric']}:{day.isoformat()}"
        legacy = c.execute("SELECT start_ts, end_ts FROM samples WHERE sample_id = ?", [legacy_id]).fetchone()
        if legacy:
            c.execute("DELETE FROM samples WHERE sample_id = ?", [legacy_id])
            c.execute("INSERT OR IGNORE INTO sample_aliases (old_id, new_id, reason) VALUES (?, ?, ?)",
                      [legacy_id, sid, ALIAS_REASON])
            # The removed row's OWN dates move too (its start may be the day
            # before the date in its id; checkpoint B, point 7).
            dirty.add(day)
            dirty.update(ts.date() for ts in legacy if ts is not None)
            replaced += 1

    # 4. Retract what the current payload no longer yields (no longer SCORED,
    #    field gone, now a nap): the sample goes and a tombstone says why.
    retracted = 0
    for sid, metric, _sts, _ets, sutc in old:
        if sid in new_ids:
            continue
        c.execute("DELETE FROM samples WHERE sample_id = ?", [sid])
        c.execute("DELETE FROM tombstones WHERE tomb_id = ?", [sid])
        c.execute("INSERT INTO tombstones (tomb_id, metric, start_utc, reason, batch_id, deleted_at) VALUES (?, ?, ?, ?, ?, ?)",
                  [sid, metric, sutc, RETRACTED, batch_id, datetime.now()])
        retracted += 1

    # 5. Journal every touched reporting date for the recompute loop.
    if dirty:
        now = datetime.now()      # explicit: INSERT OR REPLACE keeps the old DEFAULT otherwise
        c.executemany("INSERT OR REPLACE INTO dirty_dates (date, reason, batch_id, enqueued_at) VALUES (?, ?, ?, ?)",
                      [[d, JOURNAL_REASON, batch_id, now] for d in sorted(dirty)])
    return {"dirty": dirty, "samples": len(new_ids), "retracted": retracted, "replaced": replaced}


def _same_payload(stored_payload: str | None, rec: dict) -> bool:
    try:
        return json.loads(stored_payload or "null") == rec
    except (TypeError, ValueError):
        return False


def _asleep_ms(payload: str) -> int:
    try:
        st = (json.loads(payload).get("score") or {}).get("stage_summary") or {}
    except (TypeError, ValueError, AttributeError):
        return 0
    return int(sum(st.get(k, 0) or 0 for k in ("total_light_sleep_time_milli", "total_slow_wave_sleep_time_milli",
                                                 "total_rem_sleep_time_milli")))


def cache_record_key(cache_kind: str, payload: str) -> str | None:
    """The whoop_records key a cached payload belongs to, or None when the
    payload does not carry the id (very old cache rows)."""
    try:
        p = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if cache_kind in ("sleep", "sleep_nap"):
        return f"sleep:{p['id']}" if p.get("id") is not None else None
    if cache_kind == "recovery":
        return f"recovery:{p['cycle_id']}" if p.get("cycle_id") is not None else None
    if cache_kind == "cycle":
        return f"cycle:{p['id']}" if p.get("id") is not None else None
    return None


def rebuild_cache(c, zone, start_d: date, end_d: date) -> tuple[int, int]:
    """Rewrite the dated projection whoop_cache for dates inside [start_d,
    end_d] from whoop_records. Per (date, kind) the winner is: for a night of
    sleep, the record with the MOST asleep time (the same record the duration
    rule picks, so duration, stages and timing agree; checkpoint B point 14),
    ties by updated_at; for naps, recoveries and cycles the latest updated_at
    (ties by record key). Naps file under kind sleep_nap and never under sleep.

    Reconciliation (point 6): a cache row inside the window whose record is
    KNOWN in whoop_records but now projects elsewhere (another date, or sleep
    versus sleep_nap) is removed. A row whose record is unknown (a pre-1a
    pull) is left for the Phase 1b native re-pull; dates outside the window
    are never touched. Returns (rows written, stale rows removed). Runs inside
    db.transaction."""
    rows = c.execute("SELECT record_key, kind, start_utc, end_utc, created_at, updated_at, nap, payload "
                     "FROM whoop_records").fetchall()
    best: dict[tuple[date, str], tuple] = {}
    projected: dict[str, tuple[date, str]] = {}
    for key, kind, s, e, created, updated, nap, payload in rows:
        d = projection_date(kind, s, e, created, zone)
        ck = "sleep_nap" if (kind == "sleep" and nap) else kind
        if d is not None:
            projected[key] = (d, ck)
        if d is None or not (start_d <= d <= end_d):
            continue
        rev = updated or created or datetime.min
        cand = (_asleep_ms(payload), rev, key) if ck == "sleep" else (rev, key)
        cur = best.get((d, ck))
        if cur is None or cand > cur[0]:
            best[(d, ck)] = (cand, payload)
    removed = 0
    for d, ck, payload in c.execute("SELECT date, kind, payload FROM whoop_cache WHERE date BETWEEN ? AND ?",
                                    [start_d, end_d]).fetchall():
        key = cache_record_key(ck, payload)
        if key is not None and key in projected and projected[key] != (d, ck):
            c.execute("DELETE FROM whoop_cache WHERE date = ? AND kind = ?", [d, ck])
            removed += 1
    now = datetime.now()
    for (d, ck), (_, payload) in best.items():
        c.execute("DELETE FROM whoop_cache WHERE date = ? AND kind = ?", [d, ck])
        c.execute("INSERT INTO whoop_cache (date, kind, payload, fetched_at) VALUES (?, ?, ?, ?)", [d, ck, payload, now])
    return len(best), removed


def pull(conn, client: WhoopClient, policy: MetricPolicy, days: int = 8,
         now: datetime | None = None) -> dict:
    """Fetch the trailing window (UTC bounds), store every record natively,
    re-derive samples, replace legacy day rows non-destructively, rebuild the
    dated projection for the window, journal the touched dates."""
    now = _naive_utc_as_aware(now) or datetime.now(timezone.utc)
    end, start = now, now - timedelta(days=days)
    # The whole window first, so a failing page aborts before any write.
    fetched = {kind: client._paged(PATHS[kind], start, end) for kind in KINDS}
    fetched_at = datetime.now(timezone.utc)
    batch_id = f"whoop:{fetched_at:%Y%m%dT%H%M%SZ}"
    n: dict = {"recovery": 0, "sleep": 0, "cycle": 0, "samples": 0, "retracted": 0,
               "replaced_legacy": 0, "skipped": 0, "unchanged": 0, "older": 0}
    dirty: set[date] = set()
    for kind in KINDS:
        for rec in fetched[kind]:
            try:
                record_identity(kind, rec)
            except ValueError:
                n["skipped"] += 1
                continue
            with db.transaction(conn) as c:
                out = apply_record(c, kind, rec, policy, fetched_at, batch_id)
            if out.get("skipped"):
                n[out["skipped"]] += 1
                continue
            n[kind] += 1
            n["samples"] += out["samples"]
            n["retracted"] += out["retracted"]
            n["replaced_legacy"] += out["replaced"]
            dirty |= out["dirty"]
    with db.transaction(conn) as c:
        n["cache_rows"], n["cache_removed"] = rebuild_cache(c, policy.zone, to_wall(start, policy.zone).date(),
                                                            to_wall(end, policy.zone).date())
    n["dates"] = [str(d) for d in sorted(dirty)]
    return n
