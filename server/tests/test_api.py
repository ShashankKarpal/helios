"""HTTP-level tests: the shared-token rule on every /api route, the exemptions,
the served PWA shell carrying the token, the read-only SQL guard, and the SPA
containment. Runs against a temporary DuckDB and a stub web/dist."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from heliosd.config import Settings
from heliosd.main import create_app, inject_token

TOKEN = "test-token-0123456789"
SHELL = "<!doctype html>\n<html><head><title>Helios</title></head><body><div id=root></div></body></html>\n"


def _settings(tmp_path, **server):
    raw = {"server": {"ingest_token": TOKEN, **server},
           "storage": {"db_path": str(tmp_path / "helios.duckdb")},
           "notifications": {"macos_alerts": False}}
    return Settings(raw=raw)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(SHELL, encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setenv("HELIOS_WEB_DIST", str(dist))
    with TestClient(create_app(_settings(tmp_path))) as c:
        yield c


H = {"X-Helios-Token": TOKEN}


def test_health_is_open_and_reports_auth_and_overlays(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["auth"] is True
    assert sorted(body["config_overlays"]) == ["metric_policy.yaml", "source_registry.yaml"]


@pytest.mark.parametrize("method,path", [
    ("get", "/api/tool/freshness"),
    ("get", "/api/freshness"),
    ("get", "/api/actions"),
    ("get", "/api/labs"),
    ("post", "/api/tool/sql"),
    ("post", "/api/quicklog/log"),
    ("delete", "/api/quicklog/last"),
    ("delete", "/api/quicklog/some-id"),
    ("post", "/api/recompute"),
    ("post", "/ingest"),
])
def test_every_api_route_refuses_without_token(client, method, path):
    r = getattr(client, method)(path)
    assert r.status_code == 401, (path, r.status_code)
    r = getattr(client, method)(path, headers={"X-Helios-Token": "wrong"})
    assert r.status_code == 401, (path, r.status_code)
    assert r.headers.get("cache-control") == "no-store"


def test_token_grants_access(client):
    assert client.get("/api/tool/freshness", headers=H).status_code == 200
    assert client.get("/api/actions", headers=H).status_code == 200
    r = client.post("/api/tool/sql", json={"query": "SELECT 1 AS ok"}, headers=H)
    assert r.status_code == 200 and r.json() == [{"ok": 1}]


def test_ingest_with_token_accepts_empty_batch(client):
    r = client.post("/ingest", json={"samples": [], "sync_path": "bridge"}, headers=H)
    assert r.status_code == 200


def test_sql_guard_read_only(client):
    for bad in ("SELECT 1; SELECT 2", "SELECT read_text('/etc/hosts')", "DELETE FROM events",
                "WITH x AS (SELECT 1) INSERT INTO events SELECT 1", "COPY events TO '/tmp/x'"):
        r = client.post("/api/tool/sql", json={"query": bad}, headers=H)
        assert r.status_code == 400, bad


def test_shell_carries_token_and_never_caches(client):
    r = client.get("/")
    assert r.status_code == 200
    assert f'<meta name="helios-token" content="{TOKEN}">' in r.text
    assert r.headers.get("cache-control") == "no-store"
    # Deep links and index.html itself get the same shell.
    assert client.get("/sleep").text == r.text
    assert client.get("/index.html").text == r.text


def test_spa_containment_serves_shell_not_files(client):
    r = client.get("/..%2F..%2Fetc%2Fpasswd")
    assert r.status_code == 200 and "helios-token" in r.text and "root:" not in r.text
    # A real asset inside dist is served as-is, without the meta tag.
    r = client.get("/assets/app.js")
    assert r.status_code == 200 and "console.log" in r.text and "helios-token" not in r.text


def test_inject_token_escapes_and_handles_missing_head():
    out = inject_token('<html><head></head><body></body></html>', 'a"b<c>')
    assert '<meta name="helios-token" content="a&quot;b&lt;c&gt;">' in out
    assert inject_token("<html></html>", "t").startswith('<meta name="helios-token" content="t">')


def test_refuses_to_start_with_placeholder_token(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    for bad in ("", "change-me-long-random"):
        app = create_app(_settings(tmp_path, ingest_token=bad))
        with pytest.raises(RuntimeError, match="refusing to start"):
            with TestClient(app):
                pass


def test_rollback_switch_serves_without_token_and_says_so(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(SHELL, encoding="utf-8")
    monkeypatch.setenv("HELIOS_WEB_DIST", str(dist))
    with TestClient(create_app(_settings(tmp_path, api_auth="off"))) as c:
        assert c.get("/api/health").json()["auth"] is False
        assert c.get("/api/tool/freshness").status_code == 200
        # No token is handed to the browser while auth is off.
        assert 'content=""' in c.get("/").text
