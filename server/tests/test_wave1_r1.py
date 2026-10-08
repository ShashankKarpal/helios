"""Wave 1 (fix program 2026-10-08), fork R1: A2 stable action ids, A3 first
tick and Whoop wake-window polling, A13 timestamped INFO logging, A24 client
allowlist and TLS fail-closed. Every test here failed on the code before its
fix (the failing line is recorded in the commit message of each item)."""

from __future__ import annotations

import io
import logging
import re

import pytest

from heliosd import main


# ---------------------------------------------------------------- A13 logging

def test_configure_logging_stamps_heliosd_info_lines_and_keeps_third_party_quiet():
    buf = io.StringIO()
    handler = main.configure_logging(stream=buf)
    try:
        logging.getLogger("heliosd.wave1").info("tick %s", {"dates": 2})
        logging.getLogger("httpx").info("HTTP Request: GET https://example.invalid")
    finally:
        logging.getLogger().removeHandler(handler)
    lines = buf.getvalue().splitlines()
    assert len(lines) == 1, lines
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} INFO heliosd\.wave1: tick \{'dates': 2\}$", lines[0]), lines[0]


def test_uvicorn_log_config_carries_timestamps_on_both_formatters():
    cfg = main.logging_config()
    for name in ("default", "access"):
        assert cfg["formatters"][name]["fmt"].startswith("%(asctime)s "), name
        assert cfg["formatters"][name]["datefmt"] == main.LOG_DATEFMT
    # uvicorn's own loggers keep their handlers: nothing is logged twice.
    assert cfg["loggers"]["uvicorn"]["propagate"] is False
    assert cfg["loggers"]["uvicorn.access"]["propagate"] is False


def test_access_log_lines_drop_the_query_string():
    """Codex A point 17: the OAuth callback's code and state rode the query
    string into the access log. The configured access formatter drops it."""
    cfg = main.logging_config()
    assert cfg["formatters"]["access"]["()"] == "heliosd.main.QuietAccessFormatter"
    fmt = main.QuietAccessFormatter(fmt=cfg["formatters"]["access"]["fmt"], datefmt=main.LOG_DATEFMT, use_colors=False)
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                            ("127.0.0.1:50000", "GET", "/whoop/callback?code=SYNTHETIC-CODE&state=S1", "1.1", 200), None)
    line = fmt.format(rec)
    assert '"GET /whoop/callback HTTP/1.1" 200' in line, line
    assert "SYNTHETIC-CODE" not in line and "state=" not in line
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ", line), line
