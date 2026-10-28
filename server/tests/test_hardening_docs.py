"""Hardening round 2026-10-28, H9: FastAPI's generated API pages are off.
/docs, /redoc and /openapi.json used to describe every route to any client the
allowlist let in, without the token; now those paths fall through to the PWA
shell like any other unknown path, and no API description is served."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from heliosd.config import Settings
from heliosd.main import create_app

TOKEN = "test-token-0123456789"
SHELL = "<!doctype html>\n<html><head><title>Helios</title></head><body><div id=root></div></body></html>\n"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(SHELL, encoding="utf-8")
    monkeypatch.setenv("HELIOS_WEB_DIST", str(dist))
    raw = {"server": {"ingest_token": TOKEN}, "storage": {"db_path": str(tmp_path / "h.duckdb")},
           "notifications": {"macos_alerts": False}}
    with TestClient(create_app(Settings(raw=raw))) as c:
        yield c


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_no_api_description_is_served(client, path):
    for headers in ({}, {"X-Helios-Token": TOKEN}):
        r = client.get(path, headers=headers)
        body = r.text.lower()
        assert "swagger" not in body and "redoc" not in body, path
        assert '"openapi"' not in body and '"paths"' not in body, path


def test_the_app_object_has_no_docs_routes():
    app = create_app(Settings(raw={"server": {"ingest_token": TOKEN}}))
    assert app.docs_url is None and app.redoc_url is None and app.openapi_url is None
