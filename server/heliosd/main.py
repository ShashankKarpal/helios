"""heliosd: the Helios daemon. FastAPI app serving ingestion, the JSON API,
and the PWA. Run: python -m heliosd.main [--config path]."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
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
    try:
        # First startup: build daily values over a wide window so baselines populate.
        recompute(app.state.conn, app.state.policy, app.state.registry,
                  days=7, value_window=120)
    except Exception:
        pass
    task = asyncio.create_task(_background_loop(app))
    yield
    task.cancel()
    app.state.conn.close()


async def _background_loop(app: FastAPI):
    """Hourly: recompute, watchdog, whoop pull. Quietly resilient."""
    while True:
        await asyncio.sleep(3600)
        try:
            recompute(app.state.conn, app.state.policy, app.state.registry)
            if app.state.whoop and app.state.settings.whoop.get("enabled"):
                whoop_pull(app.state.conn, app.state.whoop)
                recompute(app.state.conn, app.state.policy, app.state.registry, days=2)
            report = watchdog.check(app.state.conn, app.state.policy)
            if report and app.state.settings.macos_alerts:
                worst = report[0]
                watchdog.notify_macos("Helios sync watchdog",
                                      f"{worst['device_key']} {worst['metric']} is {worst['status']}")
        except Exception:
            pass


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Helios", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings or load_settings()

    def _auth(x_helios_token: str | None = Header(default=None)):
        expected = app.state.settings.ingest_token
        if expected and x_helios_token != expected:
            raise HTTPException(401, "bad or missing X-Helios-Token")

    # ---------- ingestion ----------
    @app.post("/ingest", dependencies=[Depends(_auth)])
    async def ingest(payload: dict):
        sync_path = payload.get("sync_path", "bridge")
        result = bridge_ingest.ingest_batch(app.state.conn, payload, app.state.policy,
                                            app.state.registry, sync_path)
        if payload.get("recompute", True):
            recompute(app.state.conn, app.state.policy, app.state.registry, days=2)
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
        brief = generate_brief(app.state.conn, app.state.lm, d, st.owner_name,
                               st.llm.get("narrative_temperature", 0.2))
        steps = db.fetchdicts(app.state.conn,
            "SELECT value FROM daily_values WHERE metric='steps' AND date=?", [d])
        brief["focus"] = [{"name": "Step foundation", "current": (steps[0]["value"] if steps else 0) or 0,
                           "target": 8000, "unit": "steps"}]
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
    async def sleep(days: int = 30):
        conn = app.state.conn
        nights = db.fetchdicts(conn, """
            SELECT CAST(end_ts AS DATE) AS date, device_key, text_value AS stage,
                   SUM(value) AS minutes
            FROM samples WHERE metric = 'sleep_analysis' AND CAST(end_ts AS DATE) >= ?
            GROUP BY 1, 2, 3 ORDER BY 1""", [date.today() - timedelta(days=days)])
        for r in nights:
            r["date"] = str(r["date"])
        durations = db.fetchdicts(conn, """
            SELECT date, value, device_key, grade FROM daily_values
            WHERE metric = 'sleep_duration' AND date >= ? ORDER BY date""",
            [date.today() - timedelta(days=days)])
        for r in durations:
            r["date"] = str(r["date"])
        return {"stages": nights, "durations": durations}

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

    # ---------- intelligence ----------
    @app.post("/api/chat")
    async def chat(body: dict):
        if not app.state.lm.available():
            raise HTTPException(503, "LM Studio is not running (lms server start)")
        return run_chat(app.state.conn, app.state.lm, body.get("message", ""),
                        body.get("session_id"),
                        app.state.settings.llm.get("chat_temperature", 0.65))

    @app.post("/api/quicklog")
    async def quicklog_parse(body: dict):
        return quicklog.parse(app.state.lm, body.get("text", ""))

    @app.post("/api/quicklog/confirm")
    async def quicklog_confirm(body: dict):
        return quicklog.confirm(app.state.conn, body)

    @app.post("/api/recompute")
    async def recompute_api(days: int = 7):
        return recompute(app.state.conn, app.state.policy, app.state.registry, days)

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
    async def whoop_callback(code: str, state: str = ""):
        app.state.whoop.exchange_code(code)
        return {"ok": True, "note": "Whoop connected. POST /api/whoop/pull to fetch."}

    @app.post("/api/whoop/pull")
    async def whoop_pull_api(days: int = 8):
        if not app.state.whoop:
            raise HTTPException(400, "whoop not configured")
        n = whoop_pull(app.state.conn, app.state.whoop, days)
        recompute(app.state.conn, app.state.policy, app.state.registry, days=min(days, 10))
        return n

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
