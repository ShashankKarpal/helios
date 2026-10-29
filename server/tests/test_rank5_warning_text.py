"""Rank 5 (P3, 2026-10-29): since the cookie sign-in (hardening H1,
2026-10-28) the served page carries no token, so the warning for the
allow_clients rollback must not say it does."""

from __future__ import annotations

import logging

from heliosd.config import Settings
from heliosd.main import create_app

from .rank5_helpers import TOKEN


def test_allow_clients_any_warning_names_no_token(tmp_path, caplog):
    raw = {"server": {"ingest_token": TOKEN, "allow_clients": "any"},
           "storage": {"db_path": str(tmp_path / "h.duckdb")}}
    with caplog.at_level(logging.WARNING):
        create_app(Settings(raw=raw))
    lines = [r.getMessage() for r in caplog.records if "CLIENT ALLOWLIST IS OFF" in r.getMessage()]
    assert len(lines) == 1
    assert "token" not in lines[0].lower()
    assert "the sign-in page included" in lines[0]
    assert "Rollback mode only." in lines[0]
