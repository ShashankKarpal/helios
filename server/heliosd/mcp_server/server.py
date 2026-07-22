"""Helios MCP server: exposes the live health store to local AI tooling
(Claude, health workflows). Replaces manual-export staleness for good.

Run: python -m heliosd.mcp_server.server
Claude config: {"command": "python", "args": ["-m", "heliosd.mcp_server.server"]}
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from mcp.server.fastmcp import FastMCP

from heliosd.config import load_settings
from heliosd.narrative.chat import (_tool_compare, _tool_events, _tool_query_metric,
                                    _tool_signals, _tool_whoop_live)
from heliosd.signals import watchdog as wd
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy

settings = load_settings()
conn = db.connect(settings.db_path)
policy = MetricPolicy()

mcp = FastMCP("helios")


@mcp.tool()
def query_metric(metric: str, days: int = 14, stat: str = "series") -> str:
    """Daily canonical values for a metric with device provenance and confidence.
    Metrics include: hrv_rmssd, hrv_sdnn, resting_hr, sleep_duration, recovery_score,
    strain, respiratory_rate, spo2, steps, glucose, body_mass, wrist_temp."""
    return json.dumps(_tool_query_metric(conn, metric, days, stat), default=str)


@mcp.tool()
def get_daily_signals(day: str = "") -> str:
    """All computed signals (favorable/neutral/flag vs personal baseline) for a date (YYYY-MM-DD, default today)."""
    return json.dumps(_tool_signals(conn, day or None), default=str)


@mcp.tool()
def compare_periods(metric: str, days_a: int = 7, days_b: int = 7) -> str:
    """Compare a metric's median between the recent window and the one before it."""
    return json.dumps(_tool_compare(conn, metric, days_a, days_b), default=str)


@mcp.tool()
def list_events(kind: str = "all", days: int = 30) -> str:
    """Logged events (quicklog/meds/caffeine/symptoms) and lab results."""
    return json.dumps(_tool_events(conn, kind, days), default=str)


@mcp.tool()
def whoop_live() -> str:
    """Latest cached Whoop recovery, strain, and sleep (live overlay)."""
    return json.dumps(_tool_whoop_live(conn), default=str)


@mcp.tool()
def freshness() -> str:
    """Sync watchdog report: which metric streams are ok, stale, or silent."""
    return json.dumps(wd.check(conn, policy), default=str)


@mcp.tool()
def sql(query: str) -> str:
    """Read-only SQL (SELECT/WITH) over the Helios store. Tables: samples,
    daily_values, baselines, signals, events, labs, actions, sync_log."""
    q = query.strip()
    if not q.upper().startswith(("SELECT", "WITH")):
        return json.dumps({"error": "read-only: query must start with SELECT or WITH"})
    try:
        rows = db.fetchdicts(conn, q)
        return json.dumps(rows[:500], default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    mcp.run()
