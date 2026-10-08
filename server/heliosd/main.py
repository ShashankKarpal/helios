"""heliosd: the Helios daemon. FastAPI app serving ingestion, the JSON API,
and the PWA. Run: python -m heliosd.main [--config path]."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import uuid

import duckdb
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from uvicorn.logging import AccessFormatter as _UvicornAccessFormatter

from heliosd.config import REPO_ROOT, Settings, active_overlays, helios_home, load_settings
from heliosd.ingest import bridge as bridge_ingest
from heliosd.ingest.whoop import WhoopClient, night_landed, parse_wake_window, pull as whoop_pull, wake_plan
from heliosd.narrative.brief import generate_brief
from heliosd.narrative.chat import run_chat
from heliosd.narrative.lmstudio import LMStudio
from heliosd.narrative import quicklog
from heliosd.signals import watchdog
from heliosd.signals import recompute as rc
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy

log = logging.getLogger("heliosd")

# /api/tool/sql is documented as read-only. A prefix check alone is not
# read-only: DuckDB runs `;`-separated statements, and read_text() and friends
# read arbitrary files through a plain SELECT (audit 2026-09-02).
_SQL_BLOCKED = re.compile(
    r"\b(read_text|read_blob|read_csv|read_csv_auto|read_json|read_json_auto|read_parquet|"
    r"glob|copy|attach|detach|install|load|pragma|set|reset|export|import|"
    r"create|insert|update|delete|drop|alter|call)\b", re.I)
from heliosd.trust.registry import SourceRegistry

# Routes reachable without the shared token. Everything else under /api/ and
# /ingest requires X-Helios-Token (audit 2026-09-02 H3; shape decided
# 2026-08-17: one token from ~/Helios/helios.toml, injected into the served PWA
# at runtime, set once in the Shortcut, sent as a header by the MCP client, no
# same-origin exemption because Origin is spoofable on a LAN).
AUTH_EXEMPT_PATHS = frozenset({"/api/health"})
# Placeholder tokens from the example config are refused at startup, not silently
# accepted as a real secret.
PLACEHOLDER_TOKENS = frozenset({"", "change-me-long-random", "YOUR_TOKEN_HERE"})
TOKEN_META = '<meta name="helios-token" content="{token}">'
# Lab report uploads: capped and deleted after parsing (audit H7).
LABS_MAX_BYTES = 25 * 1024 * 1024
LABS_ALLOWED_EXT = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".heic", ".tif", ".tiff", ".webp"})
# POST /api/whoop/pull runs a real pull at most this often per process (A3);
# any trigger's attempt counts, so Pull latest right after a wake-window pull
# answers with that pull's outcome instead of asking Whoop again.
WHOOP_PULL_MIN_INTERVAL_S = 60.0
# Upper bound on the days one API call may ask for (Codex A point 16); the
# history back-pull is its own tool, not this route.
WHOOP_PULL_MAX_DAYS = 400
# The loops await this name so a test can observe and stop them.
SLEEP = asyncio.sleep


def web_dist_dir() -> Path:
    """Built PWA to serve. HELIOS_WEB_DIST overrides for tests."""
    return Path(os.environ.get("HELIOS_WEB_DIST") or (REPO_ROOT / "web" / "dist"))


def _html_attr(value: str) -> str:
    return (value.replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def inject_token(index_html: str, token: str) -> str:
    """Serve the SPA shell with the shared token in a meta tag so the PWA can
    send X-Helios-Token on every API call. Inserted before </head>; if the
    shell has no head (should not happen), prepend."""
    tag = TOKEN_META.format(token=_html_attr(token))
    if "</head>" in index_html:
        return index_html.replace("</head>", f"    {tag}\n  </head>", 1)
    return tag + index_html


def _labs_ocr_fn():
    """OCR for image lab reports, via Apple Vision (ocrmac, optional 'mac'
    dependency). Returns None if unavailable, so PDFs still work and images ask
    to be OCR'd rather than guessed. Fully local either way."""
    try:
        from ocrmac import ocrmac  # type: ignore
    except Exception:
        return None

    def _ocr(path: str) -> str:
        return "\n".join(a[0] for a in ocrmac.OCR(path).recognize())

    return _ocr


# launchd sends SIGTERM and SIGKILLs 5 s later (Phase 0 finding, 2026-10-04: a
# bootout killed heliosd before DuckDB had closed; the WAL survived and replayed,
# but the daemon should finish on its own). Budget: uvicorn stops taking
# requests and waits at most GRACEFUL_HTTP_S for in-flight ones, then the
# lifespan exit waits at most SHUTDOWN_GRACE_S for store workers and has
# SHUTDOWN_CLOSE_S more for the lock, the checkpoint and the close. The sum
# stays under the 5 s. A worker that cannot be stopped is reported, and the
# close is skipped rather than overrun: the WAL replays on the next open.
GRACEFUL_HTTP_S = 1
SHUTDOWN_GRACE_S = 2.5
SHUTDOWN_CLOSE_S = 1.0


async def run_worker(app: FastAPI, fn, *args):
    """asyncio.to_thread with the future registered on app.state.workers, so
    the shutdown can wait for a store write that is already running. The wait
    is shielded: cancelling the caller (a loop task being stopped) does not
    mark the thread's future done while the thread still runs. Once the
    shutdown has begun no new store work is admitted (503)."""
    if getattr(app.state, "stopping", False):
        raise HTTPException(503, "shutting down")
    fut = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    app.state.workers.add(fut)
    fut.add_done_callback(app.state.workers.discard)
    return await asyncio.shield(fut)


async def shutdown_store(app: FastAPI, tasks: list, grace: float = SHUTDOWN_GRACE_S,
                         close_budget: float = SHUTDOWN_CLOSE_S) -> dict:
    """Stop the periodic loops, wait up to `grace` seconds for in-flight store
    workers, then, inside `close_budget` more seconds, interrupt a statement
    that is still running, checkpoint the WAL and close the connection. Every
    wait for the store lock is bounded; a step that cannot get the lock in
    time is skipped and logged. Returns what happened, for the log and tests."""
    t0 = time.monotonic()
    app.state.stopping = True
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    pending = {f for f in getattr(app.state, "workers", set()) if not f.done()}
    out = {"workers_pending": len(pending), "drained": True, "interrupted": False,
           "checkpointed": False, "closed": False}
    conn = app.state.conn
    if pending:
        _done, still = await asyncio.wait(pending, timeout=grace)
        out["drained"] = not still
        if still:
            log.warning("shutdown: %d store worker(s) still running after %.1fs; interrupting", len(still), grace)
            try:
                conn.interrupt()          # aborts the statement running on this connection, if any
                out["interrupted"] = True
            except Exception:
                log.exception("shutdown: interrupt failed")
    deadline = t0 + grace + close_budget

    def remaining() -> float:
        return max(0.05, deadline - time.monotonic())

    async def bounded(step: str, fn) -> bool:
        """Run a store step inside the remaining budget: the lock wait AND the
        statement itself (checkpoint C, point 14). On overrun the running
        statement is interrupted and the step is reported as skipped."""
        budget = remaining()
        try:
            await asyncio.wait_for(asyncio.to_thread(fn, conn, budget), timeout=budget + 0.1)
            return True
        except db.LockBusy:
            log.warning("shutdown: %s skipped, the store lock stayed busy; the WAL replays on the next start", step)
        except (asyncio.TimeoutError, TimeoutError):
            # The statement itself overran (asyncio.TimeoutError is TimeoutError on 3.11).
            try:
                conn.interrupt()
            except Exception:
                pass
            log.warning("shutdown: %s overran its %.1fs budget and was interrupted; the WAL replays on the next start", step, budget)
        except Exception:
            log.exception("shutdown: %s failed", step)
        return False

    try:
        out["checkpointed"] = await bounded("checkpoint", db.checkpoint)
    finally:
        out["closed"] = await bounded("close", db.close)
    out["seconds"] = round(time.monotonic() - t0, 2)
    log.info("shutdown: %s", out)
    return out


def recompute(conn, policy: MetricPolicy, registry: SourceRegistry, days: int = 3,
              value_window: int | None = None) -> dict:
    """Explicit trailing windows (the API and the hourly loop). Ingest-driven
    work goes through the dirty-date journal instead (rc.drain_journal), so a
    historical batch rebuilds its own dates, never "span days back from today"."""
    return rc.recompute_window(conn, policy, registry, days, value_window)


@asynccontextmanager
async def lifespan(app: FastAPI):
    st: Settings = app.state.settings
    if st.api_auth_off:
        log.warning("API AUTH IS OFF ([server] api_auth = \"off\"): every /api route "
                    "answers without a token. Rollback mode only; turn it back on.")
    elif st.ingest_token in PLACEHOLDER_TOKENS:
        raise RuntimeError(
            "refusing to start: [server] ingest_token is empty or still the example "
            "placeholder. Set a long random token in ~/Helios/helios.toml; every "
            "/api route and /ingest require it.")
    app.state.conn = db.connect(st.db_path)
    # Reporting zone: policy block, else [owner] timezone. Never the Mac clock.
    app.state.policy = MetricPolicy(default_tz=st.timezone)
    app.state.policy.sync_registry(app.state.conn)
    app.state.registry = SourceRegistry()
    app.state.lm = LMStudio(st.llm)
    app.state.whoop = WhoopClient(st.whoop) if st.whoop.get("client_id") else None
    # Recompute never blocks startup or a request. Ingest marks work as pending;
    # the debounce loop runs it in a worker thread once batches go quiet. The
    # initial wide window (baselines need history) is queued the same way, so
    # the server accepts connections immediately after boot.
    # The journal (dirty_dates) is the durable queue. Seed it with the trailing
    # week so a restart refreshes recent derived state; anything left over from
    # a crash is already in the table and drains with it.
    today = rc.reporting_today(app.state.policy.zone)
    rc.enqueue(app.state.conn, {today - timedelta(days=i) for i in range(0, 8)}, "startup")
    app.state.ingest_clock = {"first": 0.0, "last": 0.0}
    # Days for which a background narrative generation is already in flight, so
    # /api/today never launches more than one model call at a time.
    app.state.narrative_inflight = set()
    # Store workers in flight (see run_worker) and the stop flag the loops read.
    app.state.workers = set()
    app.state.stopping = False
    # Whoop pulls (hourly, wake-window, api) run one at a time under this lock:
    # two pulls must never refresh the OAuth token at once, because Whoop
    # invalidates the refresh token the first refresh used (Codex A point 14).
    app.state.whoop_pull_lock = asyncio.Lock()
    app.state.whoop_pull_last_at = None     # monotonic time of the last attempt, any trigger
    app.state.whoop_last_pull = None        # the last attempt's outcome, for the freshness report
    tasks = [asyncio.create_task(_recompute_loop(app)),
             asyncio.create_task(_background_loop(app)),
             asyncio.create_task(_whoop_wake_loop(app))]
    try:
        yield
    finally:
        await shutdown_store(app, tasks)


async def _recompute_loop(app: FastAPI):
    """Debounced drain of the dirty-date journal. Fires once ingest has been
    quiet for 20s, or every 5 minutes during a long backfill, always via
    asyncio.to_thread so the event loop keeps accepting requests. /ingest never
    does heavy work inline; it only journals its dates."""
    while True:
        await asyncio.sleep(15)
        clock = app.state.ingest_clock
        now = time.monotonic()
        quiet = (now - clock["last"]) >= 20
        if not quiet and (now - clock["first"]) < 300:
            continue
        if not quiet:
            clock["first"] = now  # forced tick mid-backfill: drain what is there
        if app.state.stopping:
            return
        try:
            out = await run_worker(app, rc.drain_journal, app.state.conn, app.state.policy,
                                   app.state.registry)
            if out:
                log.info("recompute: %s", out)
        except asyncio.CancelledError:
            raise
        except HTTPException as e:
            if e.status_code == 503:        # shutdown began mid-tick
                return
            log.exception("recompute loop tick failed")
        except Exception:
            log.exception("recompute loop tick failed")


async def _whoop_pull_now(app: FastAPI, trigger: str, days: int,
                          min_interval_s: float | None = None) -> dict:
    """One Whoop pull, serialized with every other trigger. With
    min_interval_s, an attempt younger than that (any trigger) is not repeated:
    the answer is skipped = rate_limited with the last attempt's outcome, and a
    caller that waited for a running pull gets that pull's outcome this way.
    Every attempt is recorded on app.state.whoop_last_pull (ok, trigger, days,
    time, the counts or the error type) and a success is logged as one INFO
    line with the counts (A13). A failure re-raises for the caller to report."""
    async with app.state.whoop_pull_lock:
        last_at = app.state.whoop_pull_last_at
        if min_interval_s is not None and last_at is not None:
            since = time.monotonic() - last_at
            if since < min_interval_s:
                last = app.state.whoop_last_pull
                return {"ok": bool(last and last.get("ok")), "skipped": "rate_limited",
                        "retry_after_s": int(min_interval_s - since) + 1, "last": last}
        app.state.whoop_pull_last_at = time.monotonic()
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            n = await run_worker(app, whoop_pull, app.state.conn, app.state.whoop, app.state.policy, days)
        except HTTPException:
            raise                                   # 503: the shutdown began; not a pull failure
        except Exception as e:
            app.state.whoop_last_pull = {"ok": False, "trigger": trigger, "days": days,
                                         "pulled_at": started, "error": type(e).__name__}
            raise
    out = {"ok": True, "trigger": trigger, "days": days, "pulled_at": started, **n}
    app.state.whoop_last_pull = out
    log.info("whoop pull (%s, %d days): %s", trigger, days, n)
    return out


async def _background_loop(app: FastAPI):
    """Recompute, Whoop pull, informational feeds, watchdog. The first tick runs
    first_tick_seconds after startup (default 120 s; it was a fixed hour, K3),
    then every background_interval_seconds (default an hour). A failed Whoop
    pull no longer skips the rest of the tick (the watchdog reports it)."""
    st: Settings = app.state.settings
    delay = st.first_tick_seconds
    while True:
        await SLEEP(delay)
        delay = st.background_interval_seconds
        if app.state.stopping:
            return
        try:
            await run_worker(app, recompute, app.state.conn, app.state.policy, app.state.registry)
            if app.state.whoop and app.state.settings.whoop.get("enabled"):
                try:
                    await _whoop_pull_now(app, "hourly", 8)
                    await run_worker(app, recompute, app.state.conn, app.state.policy, app.state.registry, 2)
                except HTTPException:
                    raise
                except Exception:
                    log.exception("hourly whoop pull failed")
            try:
                await run_worker(app, ingest_sources, app)
            except Exception:
                log.exception("informational source ingest failed")
            report = await run_worker(app, watchdog.check, app.state.conn, app.state.policy, None, None, _whoop_state(app))
            worst = watchdog.notifiable(report)
            if worst and app.state.settings.macos_alerts:
                # Cooldown (2026-07-31): this loop runs hourly and used to
                # re-post the identical alert every hour, which trained the
                # owner to ignore it. Notify once per (metric, status) per 6h.
                key = (worst["metric"], worst["status"])
                last = getattr(app.state, "wd_notified", {})
                prev = last.get(key)
                if prev is None or (datetime.now() - prev).total_seconds() >= 6 * 3600:
                    watchdog.notify_macos("Helios sync watchdog",
                                          f"{worst['device_key']} {worst['metric']} is {worst['status']}")
                    last[key] = datetime.now()
                    app.state.wd_notified = last
        except asyncio.CancelledError:
            raise
        except HTTPException as e:
            if e.status_code == 503:        # shutdown began mid-tick
                return
            log.exception("background loop tick failed")
        except Exception:
            log.exception("background loop tick failed")


async def _whoop_wake_loop(app: FastAPI):
    """Fix program A3 (K3). Inside [whoop] wake_window (reporting-zone hours),
    pull Whoop every wake_poll_minutes until the night that ends today has
    landed (whoop.night_landed), so the Today screen moves from the labelled
    fallback to Whoop within one poll of Whoop scoring the night; then wait for
    the next window (revisions come with the hourly 8-day pull). The first
    check runs at once, which covers a restart inside the window. Cost: 3
    requests per pull while each kind fits one page of 25, at most 20 pulls per
    window: a small fraction of Whoop's daily request limit."""
    cfg = app.state.settings.whoop
    if not (app.state.whoop and cfg.get("enabled")):
        return
    try:
        window = parse_wake_window(str(cfg.get("wake_window", "05:00-10:00")))
        poll_s = int(round(float(cfg.get("wake_poll_minutes", 15)) * 60))
    except (TypeError, ValueError) as e:
        log.warning("whoop wake-window polling off: %s", e)
        return
    if poll_s <= 0:
        return
    while True:
        if app.state.stopping:
            return
        zone = app.state.policy.zone
        now_local = datetime.now(zone)
        try:
            landed = await run_worker(app, night_landed, app.state.conn, now_local.date(), zone)
            what, delay = wake_plan(now_local, window, poll_s, landed)
            if what == "pull":
                await _whoop_pull_now(app, "wake-window", 3)
                await run_worker(app, recompute, app.state.conn, app.state.policy, app.state.registry, 2)
        except asyncio.CancelledError:
            raise
        except HTTPException as e:
            if e.status_code == 503:        # shutdown began mid-tick
                return
            log.warning("whoop wake-window tick failed: %s", e.detail)
            delay = poll_s
        except Exception as e:
            # A token or HTTP failure: whoop_last_pull and the client's last_error
            # carry it for the watchdog; try again at the next poll.
            log.warning("whoop wake-window pull failed (%s); next try in %d s", type(e).__name__, poll_s)
            delay = poll_s
        await SLEEP(delay)


def _whoop_state(app: FastAPI) -> dict:
    w = app.state.whoop
    at = getattr(w, "last_error_at", None) if w else None
    return {"enabled": bool(w and app.state.settings.whoop.get("enabled")),
            "last_error": getattr(w, "last_error", None) if w else None,
            "last_error_at": at.isoformat(timespec="seconds") if at else None}


def ingest_sources(app: FastAPI, now: datetime | None = None) -> dict:
    """Pull new lines from informational JSONL feeds declared with
    `ingest: events` under `sources:` in the policy overlay (for example a
    Mac power and thermal event log written by another local app) into the
    events table as kind `system`, source = the feed key. Idempotent: the
    event id is a hash of the feed key, timestamp and event name, so replaying
    a file inserts nothing twice. Informational tier only: these rows never
    enter device arbitration; they exist so correlations can see, for example,
    a hot Mac at 02:00 next to a poor HRV night."""
    import hashlib
    policy: MetricPolicy = app.state.policy
    conn = app.state.conn
    out = {"feeds": 0, "inserted": 0}
    for spec in policy.sources:
        if str(spec.get("ingest") or "") != "events":
            continue
        key = str(spec.get("key") or "").strip()
        path = Path(os.path.expanduser(str(spec.get("path") or "")))
        if not key or not path.is_file():
            continue
        out["feeds"] += 1
        ts_field = spec.get("ts_field", "ts")
        ev_field = spec.get("event_field", "event")
        last = db.fetchall(conn, "SELECT MAX(ts) FROM events WHERE source = ? AND kind = 'system'", [key])
        since = last[0][0] if last and last[0][0] else None
        rows = []
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines[-5000:]:
            try:
                rec = json.loads(line)
                raw_ts = rec.get(ts_field)
                name = str(rec.get(ev_field) or "")
                if not raw_ts or not name:
                    continue
                ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
                if ts.tzinfo is not None:
                    ts = ts.astimezone().replace(tzinfo=None)
            except (ValueError, AttributeError, json.JSONDecodeError):
                continue
            if since is not None and ts <= since:
                continue
            eid = hashlib.sha1(f"{key}|{ts.isoformat()}|{name}".encode()).hexdigest()[:16]
            payload = json.dumps({"event": name, "feed": key,
                                  **{k: v for k, v in rec.items() if k not in (ts_field, ev_field, "host")}},
                                 default=str)
            rows.append([eid, "system", ts, payload, key, ts])
        if rows:
            db.insert_batch(conn, "INSERT OR IGNORE INTO events (event_id, kind, ts, payload, source, created_at) "
                                  "VALUES (?, ?, ?, ?, ?, ?)", rows)
            out["inserted"] += len(rows)
    return out


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Helios", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def _no_store_api(request: Request, call_next):
        # API/PWA responses must never be cached by Safari, or the dashboard
        # serves stale numbers (e.g. this morning's step count) until a manual
        # cache clear. Content-hashed static assets keep their own caching.
        resp = await call_next(request)
        if request.url.path.startswith("/api") or request.url.path == "/ingest":
            resp.headers["Cache-Control"] = "no-store"
        return resp
    app.state.settings = settings or load_settings()

    # ---------- auth ----------
    def _token_ok(presented: str | None) -> bool:
        expected = app.state.settings.ingest_token
        return (bool(expected) and presented is not None
                and secrets.compare_digest(presented.encode(), expected.encode()))

    @app.middleware("http")
    async def _require_token(request: Request, call_next):
        # Fail closed: any /api/* route, present or future, needs the token
        # unless listed in AUTH_EXEMPT_PATHS. The SPA shell and its assets stay
        # open (the shell is how the PWA obtains the token). Preflight OPTIONS
        # never carries custom headers, so it passes; the real call is checked.
        path = request.url.path
        guarded = (path.startswith("/api/") and path not in AUTH_EXEMPT_PATHS) or path == "/ingest"
        if guarded and request.method != "OPTIONS" and not app.state.settings.api_auth_off:
            if not _token_ok(request.headers.get("x-helios-token")):
                return JSONResponse({"detail": "bad or missing X-Helios-Token"}, status_code=401,
                                    headers={"Cache-Control": "no-store"})
        return await call_next(request)

    def _auth(x_helios_token: str | None = Header(default=None)):
        # Kept on /ingest as a second, explicit check beside the middleware.
        if not app.state.settings.api_auth_off and not _token_ok(x_helios_token):
            raise HTTPException(401, "bad or missing X-Helios-Token")

    # ---------- ingestion ----------
    @app.post("/ingest", dependencies=[Depends(_auth)])
    async def ingest(payload: dict):
        sync_path = payload.get("sync_path", "bridge")
        # Normalization + bulk insert run in a worker thread so the event loop
        # stays free to accept the Bridge's next connection. NO recompute here,
        # ever: it is marked pending and the debounce loop handles it once
        # batches go quiet. Inline recompute is what stalled the backfill.
        result = await run_worker(app, bridge_ingest.ingest_batch, app.state.conn,
                                  payload, app.state.policy, app.state.registry, sync_path)
        if result.get("affected_dates"):
            now = time.monotonic()
            clock = app.state.ingest_clock
            if clock["last"] == 0.0 or now - clock["last"] >= 20:
                clock["first"] = now
            clock["last"] = now
        return result

    # ---------- read API ----------
    @app.get("/api/health")
    async def health():
        # Raw reader on purpose: the count is every stored row, eligible or
        # not. Analysis reads the eligibility view (eligible_samples).
        n = db.fetchall(app.state.conn, "SELECT COUNT(*) FROM samples")[0][0]
        return {"ok": True, "samples": n, "raw": True, "llm": app.state.lm.available(),
                "whoop": bool(app.state.whoop),
                # Which HELIOS_HOME overlay files were merged over config/ at
                # startup; names only, never their contents.
                "config_overlays": active_overlays(),
                # False only in the documented rollback mode.
                "auth": not app.state.settings.api_auth_off}

    @app.get("/api/freshness")
    async def freshness():
        conn = app.state.conn
        # Raw reader on purpose: freshness is about delivery, so excluded,
        # unscored and flagged rows count here. Analysis reads eligible_samples.
        per_metric = db.fetchdicts(conn, """
            SELECT metric, device_key, MAX(COALESCE(end_ts, start_ts)) AS last_seen, COUNT(*) AS n
            FROM samples GROUP BY metric, device_key ORDER BY metric""")
        for r in per_metric:
            r["last_seen"] = str(r["last_seen"])
        last_batch = db.fetchdicts(conn,
            "SELECT batch_id, received_at, n_samples, sync_path FROM sync_log ORDER BY received_at DESC LIMIT 5")
        for r in last_batch:
            r["received_at"] = str(r["received_at"])
        return {"metrics": per_metric, "recent_batches": last_batch, "raw": True,
                "watchdog": watchdog.check(conn, app.state.policy, whoop=_whoop_state(app))}

    @app.get("/api/today")
    async def today():
        st = app.state.settings
        d = rc.reporting_today(app.state.policy.zone)
        temp = st.llm.get("narrative_temperature", 0.2)
        # Fast path: deterministic numbers plus a cached-or-template narrative.
        # allow_llm=False guarantees this never touches the model, so the tab
        # renders instantly even mid-backfill.
        brief = await run_worker(app, generate_brief, app.state.conn, app.state.lm,
                                 d, st.owner_name, temp, False, False)
        # If we do not yet have a validated local-AI narrative, write one in the
        # background (at most one at a time). The client polls /api/today and
        # picks up the richer text on a later tick; the response never waits.
        if (brief.get("narrative_status") == "generating"
                and d not in app.state.narrative_inflight
                and app.state.lm and app.state.lm.available()):
            app.state.narrative_inflight.add(d)

            async def _upgrade(day=d, temperature=temp, name=st.owner_name):
                try:
                    await run_worker(app, generate_brief, app.state.conn,
                                     app.state.lm, day, name, temperature, True, True)
                except Exception:
                    log.exception("brief upgrade failed")
                finally:
                    app.state.narrative_inflight.discard(day)

            asyncio.create_task(_upgrade())
        steps = db.fetchdicts(app.state.conn,
            "SELECT value FROM daily_values WHERE metric='steps' AND date=?", [d])
        # No steps row yet (after midnight before the first batch, or after a
        # failed recompute) is "no data", never a real 0 of 8,000 (audit P15):
        # current is null and the web app renders a dash.
        brief["focus"] = [{"name": "Step foundation",
                           "current": steps[0]["value"] if steps and steps[0]["value"] is not None else None,
                           "target": 8000, "unit": "steps"}]
        # Honesty stamp: when the phone last delivered a batch. The dashboard
        # shows this so a lagging number reads as lag, not breakage (a sleeping
        # Mac made "frozen" numbers look like a broken pipeline).
        last_rx = db.fetchall(app.state.conn,
            "SELECT MAX(received_at) FROM sync_log WHERE sync_path = 'bridge'")
        if last_rx and last_rx[0][0]:
            brief["as_of"] = str(last_rx[0][0])
        return brief

    @app.get("/api/metrics/{metric}")
    async def metric_series(metric: str, days: int = 30):
        # Every window below is anchored on the reporting day in the policy
        # zone, never date.today() on the Mac clock (audit P8).
        today = rc.reporting_today(app.state.policy.zone)
        rows = db.fetchdicts(app.state.conn, """
            SELECT date, value, unit, device_key, grade, confidence, corroboration
            FROM daily_values WHERE metric = ? AND date >= ? ORDER BY date""",
            [metric, today - timedelta(days=days)])
        # The latest baseline per window on or before the reporting day, with
        # its date and whether it is today's (audit M15: ORDER BY date DESC
        # LIMIT 3 returned three dates of one window, undated, and a baseline
        # months old looked current).
        base = db.fetchdicts(app.state.conn, """
            SELECT b.window_days, b.median, b.mad, b.n_days, b.date FROM baselines b
            JOIN (SELECT window_days, MAX(date) AS date FROM baselines
                  WHERE metric = ? AND date <= ? GROUP BY window_days) latest
              ON latest.window_days = b.window_days AND latest.date = b.date
            WHERE b.metric = ? ORDER BY b.window_days""", [metric, today, metric])
        for b in base:
            b["current"] = b["date"] == today
            b["date"] = str(b["date"])
        for r in rows:
            r["date"] = str(r["date"])
            if r.get("corroboration"):
                r["corroboration"] = json.loads(r["corroboration"])
        return {"metric": metric, "reporting_date": str(today), "series": rows, "baselines": base}

    @app.get("/api/sleep")
    async def sleep(days: int = 31):
        from heliosd.signals.sleep_report import build_sleep_report
        return await run_worker(app, build_sleep_report, app.state.conn, days, app.state.policy)

    @app.get("/api/activity")
    async def activity(days: int = 30):
        today = rc.reporting_today(app.state.policy.zone)
        out: dict = {"reporting_date": str(today)}
        for m in ("steps", "active_energy", "strain", "vo2max"):
            # VO2 Max arrives only with a qualifying outdoor workout, weeks
            # apart; the series keeps its newest stored row whatever the
            # window so the tile never goes blank after 30 quiet days
            # (audit M18). The daily sums stay inside the window.
            keep_latest = "OR date = (SELECT MAX(date) FROM daily_values WHERE metric = ?)" if m == "vo2max" else ""
            params = [m, today - timedelta(days=days)] + ([m] if m == "vo2max" else [])
            rows = db.fetchdicts(app.state.conn, f"""
                SELECT date, value, device_key, grade FROM daily_values
                WHERE metric = ? AND (date >= ? {keep_latest}) ORDER BY date""", params)
            for r in rows:
                r["date"] = str(r["date"])
            out[m] = rows
        return out

    @app.get("/api/actions")
    async def actions(days: int = 7):
        today = rc.reporting_today(app.state.policy.zone)
        rows = db.fetchdicts(app.state.conn, """
            SELECT action_id, date, text, category, status, created_by FROM actions
            WHERE date >= ? ORDER BY date DESC, created_at DESC""",
            [today - timedelta(days=days)])
        for r in rows:
            r["date"] = str(r["date"])
        return {"reporting_date": str(today), "actions": rows}

    @app.post("/api/actions/{action_id}/{status}")
    async def action_status(action_id: str, status: str):
        if status not in ("adopted", "dismissed", "done"):
            raise HTTPException(400, "status must be adopted|dismissed|done")
        db.execute(app.state.conn, "UPDATE actions SET status = ? WHERE action_id = ?",
                   [status, action_id])
        return {"ok": True}

    # ---------- labs (assisted, fully local import) ----------
    @app.post("/api/labs/parse")
    async def labs_parse(file: UploadFile = File(...)):
        """Upload a lab report (PDF or image). Extract candidate biomarker rows
        for confirmation. Nothing is stored here: the owner confirms first."""
        from heliosd.insights.labs_import import parse_labs_file
        inbox = Path(app.state.settings.db_path).expanduser().parent / "labs_inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        ext = Path(file.filename or "").suffix.lower() or ".pdf"
        if ext not in LABS_ALLOWED_EXT:
            raise HTTPException(415, f"unsupported file type {ext}; PDF or image only")
        # Cap the upload (audit H7): read one byte past the limit so an
        # oversized body is refused without ever landing on disk.
        data = await file.read(LABS_MAX_BYTES + 1)
        if len(data) > LABS_MAX_BYTES:
            raise HTTPException(413, f"file larger than {LABS_MAX_BYTES // (1024 * 1024)} MB")
        dest = inbox / f"{uuid.uuid4().hex}{ext}"
        dest.write_bytes(data)
        try:
            result = await asyncio.to_thread(parse_labs_file, str(dest), _labs_ocr_fn())
        except Exception as e:  # noqa: BLE001 - parser errors become a client-facing 422
            log.warning("lab upload %s could not be parsed: %s", file.filename, type(e).__name__)
            raise HTTPException(422, "could not read that file as a lab report")
        finally:
            # The upload is scratch input: the confirmed rows are the record.
            # Retaining every PDF forever was the retention problem (audit H7).
            try:
                dest.unlink()
            except OSError:
                log.warning("could not delete lab upload %s", dest)
        result["filename"] = file.filename
        return result

    @app.post("/api/labs/confirm")
    async def labs_confirm(body: dict):
        """Store owner-confirmed rows. panel_date + rows [{biomarker, value,
        unit?, ref_low?, ref_high?}]. panel_source labels the originating file."""
        from heliosd.insights.labs_import import confirm_and_store
        panel_date = body.get("panel_date") or rc.reporting_today(app.state.policy.zone).isoformat()
        rows = body.get("rows", [])
        src = body.get("panel_source", "assisted_import")
        for r in rows:
            r.setdefault("panel_source", src)
        return await run_worker(app, confirm_and_store, app.state.conn, panel_date, rows)

    @app.get("/api/labs")
    async def labs_list():
        rows = db.fetchdicts(app.state.conn, """
            SELECT lab_id, panel_date, biomarker, value, unit, ref_low, ref_high, panel_source
            FROM labs ORDER BY panel_date DESC, biomarker""")
        for r in rows:
            r["panel_date"] = str(r["panel_date"])
        return {"labs": rows}

    # ---------- intelligence ----------
    @app.post("/api/chat")
    async def chat(body: dict):
        if not app.state.lm.available():
            raise HTTPException(503, "LM Studio is not running (lms server start)")
        return await run_worker(app, run_chat, app.state.conn, app.state.lm,
                                body.get("message", ""),
                                body.get("session_id"),
                                app.state.settings.llm.get("chat_temperature", 0.65),
                                6, app.state.policy)

    @app.post("/api/quicklog")
    async def quicklog_parse(body: dict):
        return quicklog.parse(app.state.lm, body.get("text", ""))

    @app.post("/api/quicklog/confirm")
    async def quicklog_confirm(body: dict):
        return quicklog.confirm(app.state.conn, body)

    @app.post("/api/quicklog/log")
    async def quicklog_log(body: dict):
        """One-shot capture for the Shortcut and the Today chips: parse and
        store in a single call. Returns a speakable `summary` so a Siri
        invocation can read the result back."""
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text is required")
        return await run_worker(app, quicklog.log, app.state.conn, app.state.lm,
                                text, str(body.get("source") or "user"))

    # /last must register before /{event_id} or it would match as an id.
    @app.delete("/api/quicklog/last")
    async def quicklog_undo():
        """Undo: remove the most recently captured event."""
        return await run_worker(app, quicklog.undo_last, app.state.conn)

    @app.delete("/api/quicklog/{event_id}")
    async def quicklog_delete(event_id: str):
        out = await run_worker(app, quicklog.delete_event, app.state.conn, event_id)
        if not out.get("removed"):
            raise HTTPException(404, "no such event")
        return out

    @app.post("/api/recompute")
    async def recompute_api(days: int = 7, value_window: int | None = None):
        """days: how far back to rebuild baselines and signals.
        value_window: how far back to rebuild daily values (use a large value,
        e.g. 4000, once after the Bridge historical backfill)."""
        out = await run_worker(app, recompute, app.state.conn, app.state.policy,
                               app.state.registry, days, value_window)
        return out

    # ---------- insights / reports (module built in M6) ----------
    @app.get("/api/insights")
    async def insights_api(days: int = 90):
        try:
            from heliosd.insights.correlations import insights_report
            return await run_worker(app, insights_report, app.state.conn, days, app.state.policy)
        except ImportError:
            return {"insights": [], "note": "insights module not installed (pip install -e '.[insights]')"}

    @app.get("/api/weekly-review")
    async def weekly_review():
        try:
            from heliosd.insights.weekly_review import build_weekly_review
            return build_weekly_review(app.state.conn, app.state.policy)
        except ImportError:
            raise HTTPException(501, "insights module not installed")

    @app.get("/api/doctor-report", response_class=HTMLResponse)
    async def doctor_report():
        try:
            from heliosd.insights.doctor_report import build_doctor_report_html
            return build_doctor_report_html(app.state.conn, app.state.settings.owner_name, app.state.policy)
        except ImportError:
            raise HTTPException(501, "insights module not installed")

    # ---------- whoop oauth ----------
    @app.get("/whoop/login")
    async def whoop_login():
        if not app.state.whoop:
            raise HTTPException(400, "whoop client_id not configured")
        # Random per-login state, checked in the callback. A constant state let
        # anyone bind this Helios to their own Whoop account (login CSRF).
        state = secrets.token_urlsafe(16)
        app.state.whoop_oauth_state = state
        return RedirectResponse(app.state.whoop.login_url(state))

    @app.get("/whoop/callback")
    async def whoop_callback(code: str | None = None, state: str = "",
                             error: str | None = None, error_description: str | None = None):
        if error or not code:
            raise HTTPException(400, f"Whoop authorization failed: {error or 'no code returned'}. "
                                     f"{error_description or ''}".strip())
        expected = getattr(app.state, "whoop_oauth_state", None)
        if not expected or not secrets.compare_digest(state, expected):
            raise HTTPException(400, "Whoop authorization failed: state mismatch; start again at /whoop/login")
        app.state.whoop_oauth_state = None
        await asyncio.to_thread(app.state.whoop.exchange_code, code)
        return {"ok": True, "note": "Whoop connected. POST /api/whoop/pull to fetch."}

    @app.post("/api/whoop/pull")
    async def whoop_pull_api(days: int = 8):
        """Pull Whoop now (the web's Pull latest calls this before its
        recompute). 200 {"ok": true, "trigger": "api", "pulled_at", counts...}
        after a real pull; 200 {"ok": <last outcome>, "skipped": "rate_limited",
        "retry_after_s", "last"} when any pull was attempted in the last
        minute, including one this call waited for; 502 when the pull failed;
        400 when Whoop is not configured. days is clamped to 1..400."""
        if not app.state.whoop:
            raise HTTPException(400, "whoop not configured")
        days = max(1, min(int(days), WHOOP_PULL_MAX_DAYS))
        try:
            out = await _whoop_pull_now(app, "api", days, WHOOP_PULL_MIN_INTERVAL_S)
        except HTTPException:
            raise
        except Exception as e:
            log.warning("whoop pull (api, %d days) failed: %s", days, type(e).__name__)
            raise HTTPException(502, f"whoop pull failed ({type(e).__name__}); the freshness report has the detail")
        if out.get("skipped"):
            return out
        await run_worker(app, recompute, app.state.conn, app.state.policy, app.state.registry, min(days, 10))
        return out

    # ---------- MCP tool endpoints ----------
    # The local MCP server proxies these instead of opening DuckDB directly
    # (heliosd holds the single writer lock). Each reuses the exact chat tool
    # logic and runs it off the event loop.
    @app.get("/api/tool/query_metric")
    async def tool_query_metric(metric: str, days: int = 14, stat: str = "series", include_today: bool = False):
        from heliosd.narrative.chat import _tool_query_metric
        return await asyncio.to_thread(_tool_query_metric, app.state.conn, metric, days, stat,
                                       app.state.policy.zone, None, include_today)

    @app.get("/api/tool/signals")
    async def tool_signals(day: str = ""):
        from heliosd.narrative.chat import _tool_signals
        return await asyncio.to_thread(_tool_signals, app.state.conn, day or None, app.state.policy.zone)

    @app.get("/api/tool/compare")
    async def tool_compare(metric: str, days_a: int = 7, days_b: int = 7):
        from heliosd.narrative.chat import _tool_compare
        return await asyncio.to_thread(_tool_compare, app.state.conn, metric, days_a, days_b,
                                       app.state.policy.zone)

    @app.get("/api/tool/events")
    async def tool_events(kind: str = "all", days: int = 30):
        from heliosd.narrative.chat import _tool_events
        return await asyncio.to_thread(_tool_events, app.state.conn, kind, days, app.state.policy.zone)

    @app.get("/api/tool/whoop_live")
    async def tool_whoop_live():
        from heliosd.narrative.chat import _tool_whoop_live
        return await asyncio.to_thread(_tool_whoop_live, app.state.conn, app.state.policy.zone)

    @app.get("/api/tool/freshness")
    async def tool_freshness():
        return await asyncio.to_thread(watchdog.check, app.state.conn, app.state.policy,
                                       None, None, _whoop_state(app))

    @app.post("/api/sources/ingest")
    async def sources_ingest():
        """Pull informational feeds now instead of waiting for the hourly tick."""
        return await run_worker(app, ingest_sources, app)

    # ---------- durability ----------
    @app.post("/api/admin/export")
    async def admin_export():
        """Export the irreplaceable tables (events, labs, narratives,
        whoop_cache, actions, chat_messages, profile_facts) as gzipped JSONL
        plus a checksummed manifest into HELIOS_HOME/backup/<date>/. The
        daemon is the only process allowed to read the store, so the backup
        CLI asks it to do this, then ships the directory off the Mac."""
        from heliosd.backup import export_tables
        dest = helios_home() / "backup" / date.today().isoformat()
        manifest = await run_worker(app, export_tables, app.state.conn, dest)
        return {"path": str(dest), "manifest": manifest}

    @app.post("/api/tool/sql")
    async def tool_sql(body: dict):
        q = (body.get("query") or "").strip().rstrip(";").strip()
        if ";" in q or not q.upper().startswith(("SELECT", "WITH")):
            raise HTTPException(400, "read-only: one SELECT or WITH statement, no ';'")
        m = _SQL_BLOCKED.search(q)
        if m:
            raise HTTPException(400, f"read-only: '{m.group(0)}' is not allowed here")
        try:
            rows = await asyncio.to_thread(db.fetchdicts, app.state.conn, q)
        except duckdb.Error as e:
            # The parser, binder or catalog message is the answer the caller
            # needs (audit P9: a reserved word as a bare alias used to give a
            # bare 500 with the reason only in heliosd.err.log).
            raise HTTPException(400, f"{type(e).__name__}: {str(e).strip()}")
        except Exception as e:  # noqa: BLE001 - never a bare "Internal Server Error" for a tool caller
            raise HTTPException(500, f"{type(e).__name__}: {str(e).strip()}")
        return rows[:500]

    # ---------- PWA ----------
    web_dist = web_dist_dir()
    if web_dist.exists():
        if (web_dist / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="assets")

        _web_root = web_dist.resolve()

        def _shell() -> HTMLResponse:
            # The served shell carries the shared token so the PWA can send
            # X-Helios-Token. Read per request (tiny file) so a token change
            # needs only a daemon restart, and never cached by the browser.
            html = (_web_root / "index.html").read_text(encoding="utf-8")
            token = "" if app.state.settings.api_auth_off else app.state.settings.ingest_token
            return HTMLResponse(inject_token(html, token), headers={"Cache-Control": "no-store"})

        @app.get("/{path:path}", response_class=HTMLResponse)
        async def spa(path: str = ""):
            # Contained: the resolved file must stay inside web/dist. Without
            # this, ..%2F segments walked out to ~/Helios/helios.toml and every
            # sibling repo, unauthenticated (audit 2026-09-02).
            f = (_web_root / path).resolve()
            if path and f.is_file() and f.is_relative_to(_web_root):
                if f.name == "index.html":
                    return _shell()
                return FileResponse(f)
            return _shell()

    return app


# ---------- logging (fix program A13, 2026-10-08) ----------
# Nothing configured the "heliosd" logger before, so Python's last-resort
# handler printed WARNING and above with no timestamp, and the INFO summaries
# (every recompute tick, every Whoop pull, the shutdown record) never reached
# the error log: the audit found 0 "recompute:" lines in 105,817 lines.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


class QuietAccessFormatter(_UvicornAccessFormatter):
    """uvicorn's access line without the query string. The full URL carried the
    Whoop OAuth callback's code and state into the access log (Codex A point 17
    on the logging change); no route needs its query in the log."""

    def formatMessage(self, record: logging.LogRecord) -> str:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            client_addr, method, full_path, http_version, status_code = args
            record = copy.copy(record)
            record.args = (client_addr, method, str(full_path).split("?", 1)[0], http_version, status_code)
        return super().formatMessage(record)


def logging_config() -> dict:
    """uvicorn's own logging config with a timestamp in front of both its
    formatters, so the access log (stdout) and the server log (stderr) carry
    the time; the 2026-10-07 session had to correlate batches by line order.
    The access formatter also drops the query string (QuietAccessFormatter)."""
    from uvicorn.config import LOGGING_CONFIG
    cfg = copy.deepcopy(LOGGING_CONFIG)
    for fmt in cfg["formatters"].values():
        fmt["fmt"] = "%(asctime)s " + fmt["fmt"]
        fmt["datefmt"] = LOG_DATEFMT
    cfg["formatters"]["access"]["()"] = f"{__name__}.QuietAccessFormatter"
    return cfg


def configure_logging(stream=None, level: int = logging.INFO) -> logging.Handler:
    """One timestamped handler on the root logger and the heliosd logger at
    INFO. The root logger keeps its WARNING default, so third-party INFO
    chatter (httpx request lines, for example) stays out while heliosd's own
    lines propagate through. Called from run() only: pytest keeps its capture
    and the tests pass an explicit stream."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATEFMT))
    logging.getLogger().addHandler(handler)
    logging.getLogger("heliosd").setLevel(level)
    return handler


def run():
    import argparse
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    configure_logging()
    settings = load_settings(args.config)
    app = create_app(settings)
    # Bounded graceful stop: in-flight requests get GRACEFUL_HTTP_S, then the
    # lifespan exit drains store workers, checkpoints and closes (see
    # shutdown_store), all inside launchd's 5 s SIGTERM-to-SIGKILL window.
    kw = {"timeout_graceful_shutdown": GRACEFUL_HTTP_S, "log_config": logging_config()}
    tls = settings.tls
    if tls:
        kw.update({"ssl_certfile": tls[0], "ssl_keyfile": tls[1]})
    uvicorn.run(app, host=settings.host, port=settings.port, **kw)


if __name__ == "__main__":
    run()
