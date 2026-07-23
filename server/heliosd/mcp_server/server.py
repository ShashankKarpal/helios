"""Helios MCP server: exposes the live health store to local AI tooling
(Claude, health workflows) by proxying heliosd's local HTTP API.

It deliberately does NOT open the DuckDB file. heliosd holds the single writer
lock, so a second opener fails with a lock conflict. Instead every tool calls
the daemon's /api/tool/* endpoints, which run the identical logic against the
live store. This lets the MCP server run alongside the daemon.

Requires heliosd to be running (it is, as a launchd login service).

Run: python -m heliosd.mcp_server.server
Claude config:
  {"command": "<repo>/server/.venv/bin/python",
   "args": ["-m", "heliosd.mcp_server.server"]}
"""

from __future__ import annotations

import os

import httpx
from mcp.server.fastmcp import FastMCP

from heliosd.config import load_settings

_settings = load_settings()
# Loopback call to the daemon on this same Mac. TLS verification adds nothing
# over loopback, and the mkcert cert is issued for helios.local, not 127.0.0.1,
# so verification is skipped here on purpose.
_BASE = os.environ.get("HELIOS_API", f"https://127.0.0.1:{_settings.port}")
_client = httpx.Client(base_url=_BASE, verify=False, timeout=30.0)

mcp = FastMCP("helios")


def _get(path: str, params: dict | None = None) -> str:
    try:
        r = _client.get(path, params=params or {})
        r.raise_for_status()
        return r.text
    except httpx.HTTPError as e:
        return f'{{"error": "cannot reach heliosd at {_BASE}: {e}"}}'


@mcp.tool()
def query_metric(metric: str, days: int = 14, stat: str = "series") -> str:
    """Daily canonical values for a metric with device provenance and confidence.
    Metrics include: hrv_rmssd, hrv_sdnn, resting_hr, sleep_duration, recovery_score,
    strain, respiratory_rate, spo2, steps, glucose, body_mass, wrist_temp."""
    return _get("/api/tool/query_metric", {"metric": metric, "days": days, "stat": stat})


@mcp.tool()
def get_daily_signals(day: str = "") -> str:
    """All computed signals (favorable/neutral/flag vs personal baseline) for a date (YYYY-MM-DD, default today)."""
    return _get("/api/tool/signals", {"day": day})


@mcp.tool()
def compare_periods(metric: str, days_a: int = 7, days_b: int = 7) -> str:
    """Compare a metric's median between the recent window and the one before it."""
    return _get("/api/tool/compare", {"metric": metric, "days_a": days_a, "days_b": days_b})


@mcp.tool()
def list_events(kind: str = "all", days: int = 30) -> str:
    """Logged events (quicklog/meds/caffeine/symptoms) and lab results."""
    return _get("/api/tool/events", {"kind": kind, "days": days})


@mcp.tool()
def whoop_live() -> str:
    """Latest cached Whoop recovery, strain, and sleep (live overlay)."""
    return _get("/api/tool/whoop_live")


@mcp.tool()
def freshness() -> str:
    """Sync watchdog report: which metric streams are ok, stale, or silent."""
    return _get("/api/tool/freshness")


@mcp.tool()
def sql(query: str) -> str:
    """Read-only SQL (SELECT/WITH) over the Helios store. Tables: samples,
    daily_values, baselines, signals, events, labs, actions, sync_log."""
    try:
        r = _client.post("/api/tool/sql", json={"query": query})
        if r.status_code == 400:
            return '{"error": "read-only: query must start with SELECT or WITH"}'
        r.raise_for_status()
        return r.text
    except httpx.HTTPError as e:
        return f'{{"error": "cannot reach heliosd at {_BASE}: {e}"}}'


if __name__ == "__main__":
    mcp.run()
