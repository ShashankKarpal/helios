"""heliosd: the Helios daemon. FastAPI app serving ingestion, the JSON API,
and the PWA. Run: python -m heliosd.main [--config path]."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import uuid

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from heliosd.config import REPO_ROOT, Settings, load_settings
from heliosd.ingest import bridge as bridge_ingest
from heliosd.ingest.whoop import WhoopClient, pull as whoop_pull
from heliosd.narrative.brief import generate_brief
from heliosd.narrative.chat import run_chat
from heliosd.narrative.lmstudio import LMStudio
from heliosd.narrative import quicklog
from heliosd.signals import watchdog
from heliosd.signals.baselines import compute_baselines, compute_daily_values
from heliosd.signals.markers import compute_signals
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry

WEB_DIST = REPO_ROOT / "web" / "dist"


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


def recompute(conn, policy: MetricPolicy, registry: SourceRegistry, days: int = 3,
              value_window: int | None = None) -> dict:
    """Recompute daily values for the trailing `days`, but always (re)build
    baselines over a wide window so today's baseline has enough history.
    value_window forces a wider daily-value recompute (used on first startup)."""
    end = date.today()
    vw = value_window if value_window is not None else days
    n_dv = compute_daily_values(conn, policy, registry, end - timedelta(days=vw), end)
    n_bl = n_sg = 0
    for d in range(0, days + 1):
        n_bl += compute_baselines(conn, policy, end - timedelta(days=d))
        n_sg += compute_signals(conn, policy, end - timedelta(days=d))
    return {"daily_values": n_dv, "baselines": n_bl, "signals": n_sg}


@asynccontextmanager
async def lifespan(app: FastAPI):
    st: Settings = app.state.settings
    app.state.conn = db.connect(st.db_path)
    app.state.policy = MetricPolicy()
    app.state.registry = SourceRegistry()
    app.state.lm = LMStudio(st.llm)
    app.state.whoop = WhoopClient(st.whoop) if st.whoop.get("client_id") else None
    # Recompute never blocks startup or a request. Ingest marks work as pending;
    # the debounce loop runs it in a worker thread once batches go quiet. The
    # initial wide window (baselines need history) is queued the same way, so
    # the server accepts connections immediately after boot.
    app.state.pending_recompute = {
        "date_min": date.today() - timedelta(days=120),
        "date_max": date.today(), "days": 7,
        "first": 0.0, "last": 0.0,
    }
    # Days for which a background narrative generation is already in flight, so
    # /api/today never launches more than one model call at a time.
    app.state.narrative_inflight = set()
    tasks = [asyncio.create_task(_recompute_loop(app)),
             asyncio.create_task(_background_loop(app))]
    yield
    for t in tasks:
        t.cancel()
    app.state.conn.close()


async def _recompute_loop(app: FastAPI):
    """Debounced recompute. Fires once ingest has been quiet for 20s, or every
    5 minutes during a long backfill, always via asyncio.to_thread so the event
    loop keeps accepting requests. This is what keeps the Bridge from timing
    out: /ingest never does heavy work inline."""
    while True:
        await asyncio.sleep(15)
        p = app.state.pending_recompute
        if not p:
            continue
        now = time.monotonic()
        quiet = (now - p["last"]) >= 20
        if not quiet and (now - p["first"]) < 300:
            continue
        if quiet:
            app.state.pending_recompute = None
            span_days = max(2, (p["date_max"] - p["date_min"]).days)
            days = p.get("days", 3)
        else:
            # Forced tick during an active backfill: refresh only the recent
            # window now and keep the full span pending for the quiet pass, so
            # the DB lock is never hogged while the Bridge is mid-stream.
            p["first"] = now
            span_days = 30
            days = 2
        try:
            await asyncio.to_thread(recompute, app.state.conn, app.state.policy,
                                    app.state.registry, days, span_days)
            # Only invalidate the cached narrative on the full quiet pass. The
            # forced mid-backfill tick must NOT delete it, or opening Today every
            # few minutes during a long backfill would regenerate it every time.
            if quiet:
                db.execute(app.state.conn, "DELETE FROM narratives WHERE date = ?",
                           [date.today()])
        except Exception:
            pass


async def _background_loop(app: FastAPI):
    """Hourly: recompute, watchdog, whoop pull. Quietly resilient."""
    while True:
        await asyncio.sleep(3600)
        try:
            await asyncio.to_thread(recompute, app.state.conn, app.state.policy,
                                    app.state.registry)
            if app.state.whoop and app.state.settings.whoop.get("enabled"):
                await asyncio.to_thread(whoop_pull, app.state.conn, app.state.whoop)
                await asyncio.to_thread(recompute, app.state.conn, app.state.policy,
                                        app.state.registry, 2)
            report = watchdog.check(app.state.conn, app.state.policy)
            if report and app.state.settings.macos_alerts:
                worst = report[0]
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
        except Exception:
            pass


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

    def _auth(x_helios_token: str | None = Header(default=None)):
        expected = app.state.settings.ingest_token
        if expected and x_helios_token != expected:
            raise HTTPException(401, "bad or missing X-Helios-Token")

    # ---------- ingestion ----------
    @app.post("/ingest", dependencies=[Depends(_auth)])
    async def ingest(payload: dict):
        sync_path = payload.get("sync_path", "bridge")
        # Normalization + bulk insert run in a worker thread so the event loop
        # stays free to accept the Bridge's next connection. NO recompute here,
        # ever: it is marked pending and the debounce loop handles it once
        # batches go quiet. Inline recompute is what stalled the backfill.
        result = await asyncio.to_thread(bridge_ingest.ingest_batch, app.state.conn,
                                         payload, app.state.policy,
                                         app.state.registry, sync_path)
        if payload.get("recompute", True) and result.get("date_min"):
            dmin = date.fromisoformat(result["date_min"])
            dmax = date.fromisoformat(result["date_max"])
            now = time.monotonic()
            p = app.state.pending_recompute
            if p is None:
                app.state.pending_recompute = {"date_min": dmin, "date_max": dmax,
                                               "days": 3, "first": now, "last": now}
            else:
                p["date_min"] = min(p["date_min"], dmin)
                p["date_max"] = max(p["date_max"], dmax)
                p["last"] = now
        return result

    # ---------- read API ----------
    @app.get("/api/health")
    async def health():
        n = db.fetchall(app.state.conn, "SELECT COUNT(*) FROM samples")[0][0]
        return {"ok": True, "samples": n, "llm": app.state.lm.available(),
                "whoop": bool(app.state.whoop)}

    @app.get("/api/freshness")
    async def freshness():
        conn = app.state.conn
        per_metric = db.fetchdicts(conn, """
            SELECT metric, device_key, MAX(COALESCE(end_ts, start_ts)) AS last_seen, COUNT(*) AS n
            FROM samples GROUP BY metric, device_key ORDER BY metric""")
        for r in per_metric:
            r["last_seen"] = str(r["last_seen"])
        last_batch = db.fetchdicts(conn,
            "SELECT batch_id, received_at, n_samples, sync_path FROM sync_log ORDER BY received_at DESC LIMIT 5")
        for r in last_batch:
            r["received_at"] = str(r["received_at"])
        return {"metrics": per_metric, "recent_batches": last_batch,
                "watchdog": watchdog.check(conn, app.state.policy)}

    @app.get("/api/today")
    async def today():
        st = app.state.settings
        d = date.today()
        temp = st.llm.get("narrative_temperature", 0.2)
        # Fast path: deterministic numbers plus a cached-or-template narrative.
        # allow_llm=False guarantees this never touches the model, so the tab
        # renders instantly even mid-backfill.
        brief = await asyncio.to_thread(generate_brief, app.state.conn, app.state.lm,
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
                    await asyncio.to_thread(generate_brief, app.state.conn,
                                            app.state.lm, day, name, temperature,
                                            True, True)
                except Exception:
                    pass
                finally:
                    app.state.narrative_inflight.discard(day)

            asyncio.create_task(_upgrade())
        steps = db.fetchdicts(app.state.conn,
            "SELECT value FROM daily_values WHERE metric='steps' AND date=?", [d])
        brief["focus"] = [{"name": "Step foundation", "current": (steps[0]["value"] if steps else 0) or 0,
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
        rows = db.fetchdicts(app.state.conn, """
            SELECT date, value, unit, device_key, grade, confidence, corroboration
            FROM daily_values WHERE metric = ? AND date >= ? ORDER BY date""",
            [metric, date.today() - timedelta(days=days)])
        base = db.fetchdicts(app.state.conn, """
            SELECT window_days, median, mad FROM baselines
            WHERE metric = ? ORDER BY date DESC LIMIT 3""", [metric])
        for r in rows:
            r["date"] = str(r["date"])
            if r.get("corroboration"):
                r["corroboration"] = json.loads(r["corroboration"])
        return {"metric": metric, "series": rows, "baselines": base}

    @app.get("/api/sleep")
    async def sleep(days: int = 31):
        from heliosd.signals.sleep_report import build_sleep_report
        return await asyncio.to_thread(build_sleep_report, app.state.conn, days)

    @app.get("/api/activity")
    async def activity(days: int = 30):
        out = {}
        for m in ("steps", "active_energy", "strain", "vo2max"):
            rows = db.fetchdicts(app.state.conn, """
                SELECT date, value, device_key, grade FROM daily_values
                WHERE metric = ? AND date >= ? ORDER BY date""",
                [m, date.today() - timedelta(days=days)])
            for r in rows:
                r["date"] = str(r["date"])
            out[m] = rows
        return out

    @app.get("/api/actions")
    async def actions(days: int = 7):
        rows = db.fetchdicts(app.state.conn, """
            SELECT action_id, date, text, category, status, created_by FROM actions
            WHERE date >= ? ORDER BY date DESC, created_at DESC""",
            [date.today() - timedelta(days=days)])
        for r in rows:
            r["date"] = str(r["date"])
        return {"actions": rows}

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
        dest = inbox / f"{uuid.uuid4().hex}{ext}"
        dest.write_bytes(await file.read())
        result = await asyncio.to_thread(parse_labs_file, str(dest), _labs_ocr_fn())
        result["filename"] = file.filename
        return result

    @app.post("/api/labs/confirm")
    async def labs_confirm(body: dict):
        """Store owner-confirmed rows. panel_date + rows [{biomarker, value,
        unit?, ref_low?, ref_high?}]. panel_source labels the originating file."""
        from heliosd.insights.labs_import import confirm_and_store
        panel_date = body.get("panel_date") or date.today().isoformat()
        rows = body.get("rows", [])
        src = body.get("panel_source", "assisted_import")
        for r in rows:
            r.setdefault("panel_source", src)
        out = await asyncio.to_thread(confirm_and_store, app.state.conn, panel_date, rows)
        return out

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
        return await asyncio.to_thread(run_chat, app.state.conn, app.state.lm,
                                       body.get("message", ""),
                                       body.get("session_id"),
                                       app.state.settings.llm.get("chat_temperature", 0.65))

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
        return await asyncio.to_thread(quicklog.log, app.state.conn, app.state.lm,
                                       text, str(body.get("source") or "user"))

    # /last must register before /{event_id} or it would match as an id.
    @app.delete("/api/quicklog/last")
    async def quicklog_undo():
        """Undo: remove the most recently captured event."""
        return await asyncio.to_thread(quicklog.undo_last, app.state.conn)

    @app.delete("/api/quicklog/{event_id}")
    async def quicklog_delete(event_id: str):
        out = await asyncio.to_thread(quicklog.delete_event, app.state.conn, event_id)
        if not out.get("removed"):
            raise HTTPException(404, "no such event")
        return out

    @app.post("/api/recompute")
    async def recompute_api(days: int = 7, value_window: int | None = None):
        """days: how far back to rebuild baselines and signals.
        value_window: how far back to rebuild daily values (use a large value,
        e.g. 4000, once after the Bridge historical backfill)."""
        out = await asyncio.to_thread(recompute, app.state.conn, app.state.policy,
                                      app.state.registry, days, value_window)
        # Fresh numbers deserve a fresh narrative on the next /api/today.
        db.execute(app.state.conn, "DELETE FROM narratives WHERE date = ?", [date.today()])
        return out

    # ---------- insights / reports (module built in M6) ----------
    @app.get("/api/insights")
    async def insights_api(days: int = 90):
        try:
            from heliosd.insights.correlations import top_insights
            return {"insights": top_insights(app.state.conn, days=days)}
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
            return build_doctor_report_html(app.state.conn, app.state.settings.owner_name)
        except ImportError:
            raise HTTPException(501, "insights module not installed")

    # ---------- whoop oauth ----------
    @app.get("/whoop/login")
    async def whoop_login():
        if not app.state.whoop:
            raise HTTPException(400, "whoop client_id not configured")
        return RedirectResponse(app.state.whoop.login_url())

    @app.get("/whoop/callback")
    async def whoop_callback(code: str | None = None, state: str = "",
                             error: str | None = None, error_description: str | None = None):
        if error or not code:
            raise HTTPException(400, f"Whoop authorization failed: {error or 'no code returned'}. "
                                     f"{error_description or ''}".strip())
        await asyncio.to_thread(app.state.whoop.exchange_code, code)
        return {"ok": True, "note": "Whoop connected. POST /api/whoop/pull to fetch."}

    @app.post("/api/whoop/pull")
    async def whoop_pull_api(days: int = 8):
        if not app.state.whoop:
            raise HTTPException(400, "whoop not configured")
        n = await asyncio.to_thread(whoop_pull, app.state.conn, app.state.whoop, days)
        await asyncio.to_thread(recompute, app.state.conn, app.state.policy,
                                app.state.registry, min(days, 10))
        return n

    # ---------- MCP tool endpoints ----------
    # The local MCP server proxies these instead of opening DuckDB directly
    # (heliosd holds the single writer lock). Each reuses the exact chat tool
    # logic and runs it off the event loop.
    @app.get("/api/tool/query_metric")
    async def tool_query_metric(metric: str, days: int = 14, stat: str = "series"):
        from heliosd.narrative.chat import _tool_query_metric
        return await asyncio.to_thread(_tool_query_metric, app.state.conn, metric, days, stat)

    @app.get("/api/tool/signals")
    async def tool_signals(day: str = ""):
        from heliosd.narrative.chat import _tool_signals
        return await asyncio.to_thread(_tool_signals, app.state.conn, day or None)

    @app.get("/api/tool/compare")
    async def tool_compare(metric: str, days_a: int = 7, days_b: int = 7):
        from heliosd.narrative.chat import _tool_compare
        return await asyncio.to_thread(_tool_compare, app.state.conn, metric, days_a, days_b)

    @app.get("/api/tool/events")
    async def tool_events(kind: str = "all", days: int = 30):
        from heliosd.narrative.chat import _tool_events
        return await asyncio.to_thread(_tool_events, app.state.conn, kind, days)

    @app.get("/api/tool/whoop_live")
    async def tool_whoop_live():
        from heliosd.narrative.chat import _tool_whoop_live
        return await asyncio.to_thread(_tool_whoop_live, app.state.conn)

    @app.get("/api/tool/freshness")
    async def tool_freshness():
        return await asyncio.to_thread(watchdog.check, app.state.conn, app.state.policy)

    @app.post("/api/tool/sql")
    async def tool_sql(body: dict):
        q = (body.get("query") or "").strip()
        if not q.upper().startswith(("SELECT", "WITH")):
            raise HTTPException(400, "read-only: query must start with SELECT or WITH")
        rows = await asyncio.to_thread(db.fetchdicts, app.state.conn, q)
        return rows[:500]

    # ---------- PWA ----------
    if WEB_DIST.exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{path:path}", response_class=HTMLResponse)
        async def spa(path: str = ""):
            f = WEB_DIST / path
            if path and f.is_file():
                return FileResponse(f)
            return FileResponse(WEB_DIST / "index.html")

    return app


def run():
    import argparse
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    settings = load_settings(args.config)
    app = create_app(settings)
    kw = {}
    tls = settings.tls
    if tls:
        kw = {"ssl_certfile": tls[0], "ssl_keyfile": tls[1]}
    uvicorn.run(app, host=settings.host, port=settings.port, **kw)


if __name__ == "__main__":
    run()
