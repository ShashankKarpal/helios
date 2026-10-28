"""Hardening round 2026-10-28, H1: the web app's page no longer carries the
shared token. A browser signs in once with POST /api/session (the token in the
body) and gets a cookie: HttpOnly, Secure, SameSite=Strict, its value derived
from the token, so a rotation signs every browser out. The cookie counts only
with the X-Helios-Web header (a cross-site page cannot add it without a
preflight heliosd never answers) and only from the same origin: a request that
says it is cross-site is refused, and every state-changing method must carry
an Origin equal to the daemon's own. /ingest takes the header token only.
Failed sign-ins cost a delay and are capped per client."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from heliosd import main
from heliosd.config import Settings
from heliosd.main import SESSION_COOKIE, create_app, session_value

TOKEN = "test-token-0123456789"
SHELL = "<!doctype html>\n<html><head><title>Helios</title></head><body><div id=root></div></body></html>\n"
BASE = "https://testserver"
ORIGIN = {"Origin": BASE}
WEB = {"X-Helios-Web": "1"}


def _app(tmp_path, monkeypatch, token=TOKEN):
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text(SHELL, encoding="utf-8")
    monkeypatch.setenv("HELIOS_WEB_DIST", str(dist))
    monkeypatch.setattr(main, "SESSION_FAIL_DELAY_S", 0.0)
    raw = {"server": {"ingest_token": token}, "storage": {"db_path": str(tmp_path / "h.duckdb")},
           "notifications": {"macos_alerts": False}}
    return create_app(Settings(raw=raw))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch), base_url=BASE) as c:
        yield c


def _sign_in(c, token=TOKEN, headers=None):
    return c.post("/api/session", json={"token": token}, headers={**WEB, **ORIGIN, **(headers or {})})


def test_the_page_never_carries_the_token(client):
    for path in ("/", "/sleep", "/index.html", "/..%2F..%2Fetc%2Fpasswd"):
        r = client.get(path)
        assert r.status_code == 200 and "id=root" in r.text
        assert TOKEN not in r.text and "helios-token" not in r.text, path
        assert r.headers.get("cache-control") == "no-store"


def test_sign_in_sets_a_strict_http_only_secure_cookie(client):
    r = _sign_in(client)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert TOKEN not in r.text
    cookie = r.headers["set-cookie"]
    parts = {p.strip().split("=")[0].lower() for p in cookie.split(";")}
    assert cookie.startswith(f"{SESSION_COOKIE}={session_value(TOKEN)};")
    assert {"httponly", "secure", "samesite", "path", "max-age"} <= parts
    assert "samesite=strict" in cookie.lower() and "path=/" in cookie.lower()
    assert TOKEN not in cookie


def test_the_cookie_value_is_not_the_token_and_follows_it():
    assert session_value(TOKEN) != TOKEN and TOKEN not in session_value(TOKEN)
    assert session_value(TOKEN) == session_value(TOKEN)
    assert session_value(TOKEN) != session_value(TOKEN + "x")


def test_the_cookie_with_the_web_header_reads_and_writes(client):
    assert _sign_in(client).status_code == 200
    assert client.get("/api/actions", headers=WEB).status_code == 200
    r = client.post("/api/tool/sql", json={"query": "SELECT 1 AS ok"}, headers={**WEB, **ORIGIN})
    assert r.status_code == 200 and r.json() == [{"ok": 1}]
    assert client.delete("/api/quicklog/last", headers={**WEB, **ORIGIN}).status_code != 401


def test_the_cookie_without_the_web_header_is_refused(client):
    assert _sign_in(client).status_code == 200
    assert client.get("/api/actions").status_code == 401
    assert client.post("/api/tool/sql", json={"query": "SELECT 1"}, headers=ORIGIN).status_code == 401


@pytest.mark.parametrize("method,path", [
    ("post", "/api/tool/sql"), ("post", "/api/recompute"), ("post", "/api/quicklog/log"),
    ("delete", "/api/quicklog/last"), ("post", "/api/actions/x/done"), ("post", "/api/whoop/pull"),
    ("post", "/api/labs/confirm"), ("post", "/api/session/end"),
])
def test_a_state_change_needs_the_same_origin(client, method, path):
    assert _sign_in(client).status_code == 200
    kw = {"json": {"query": "SELECT 1"}} if method == "post" else {}
    for bad in ({}, {"Origin": "https://evil.example"}, {"Origin": "null"},
                {"Origin": BASE, "Sec-Fetch-Site": "cross-site"}, {"Origin": BASE, "Sec-Fetch-Site": "same-site"}):
        r = getattr(client, method)(path, headers={**WEB, **bad}, **kw)
        assert r.status_code == 403, (path, bad, r.status_code)
        assert "cross-site" in r.json()["detail"]


def test_a_read_says_cross_site_is_refused(client):
    assert _sign_in(client).status_code == 200
    assert client.get("/api/actions", headers={**WEB, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/actions", headers={**WEB, "Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/actions", headers={**WEB, "Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_sign_in_itself_needs_the_header_and_the_same_origin(client):
    assert client.post("/api/session", json={"token": TOKEN}, headers=ORIGIN).status_code == 403
    assert client.post("/api/session", json={"token": TOKEN}, headers=WEB).status_code == 403
    assert _sign_in(client, headers={"Origin": "https://evil.example"}).status_code == 403
    assert "set-cookie" not in client.post("/api/session", json={"token": TOKEN}, headers=ORIGIN).headers


def test_a_wrong_token_sets_nothing_and_the_tries_are_capped(client):
    for _ in range(main.SESSION_FAIL_LIMIT):
        r = _sign_in(client, token="wrong")
        assert r.status_code == 401 and "set-cookie" not in r.headers
    r = _sign_in(client)
    assert r.status_code == 429 and "set-cookie" not in r.headers
    for bad in ({}, {"token": 5}, {"token": ""}):
        assert client.post("/api/session", json=bad, headers={**WEB, **ORIGIN}).status_code in (401, 429)


def test_the_tries_age_out(client, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: clock[0])
    for _ in range(main.SESSION_FAIL_LIMIT):
        _sign_in(client, token="wrong")
    assert _sign_in(client).status_code == 429
    clock[0] += main.SESSION_FAIL_WINDOW_S + 1
    assert _sign_in(client).status_code == 200


def test_a_rotation_signs_every_browser_out(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch), base_url=BASE) as c:
        assert _sign_in(c).status_code == 200
        old = c.cookies.get(SESSION_COOKIE)
    with TestClient(_app(tmp_path, monkeypatch, token=TOKEN + "-new"), base_url=BASE) as c:
        c.cookies.set(SESSION_COOKIE, old)
        assert c.get("/api/actions", headers=WEB).status_code == 401


def test_sign_out_clears_the_cookie(client):
    assert _sign_in(client).status_code == 200
    r = client.post("/api/session/end", headers={**WEB, **ORIGIN})
    assert r.status_code == 200
    assert f"{SESSION_COOKIE}=" in r.headers["set-cookie"] and "max-age=0" in r.headers["set-cookie"].lower()
    assert client.get("/api/actions", headers=WEB).status_code == 401


def test_ingest_never_takes_the_cookie(client):
    assert _sign_in(client).status_code == 200
    r = client.post("/ingest", json={"samples": [], "sync_path": "bridge"}, headers={**WEB, **ORIGIN})
    assert r.status_code == 401
    r = client.post("/ingest", json={"samples": [], "sync_path": "bridge"}, headers={"X-Helios-Token": TOKEN})
    assert r.status_code == 200


def test_the_header_token_still_works_for_the_bridge_and_the_mcp(client):
    h = {"X-Helios-Token": TOKEN}
    assert client.get("/api/actions", headers=h).status_code == 200
    assert client.post("/api/tool/sql", json={"query": "SELECT 1 AS ok"}, headers=h).status_code == 200


def test_a_forged_cookie_is_refused(client):
    client.cookies.set(SESSION_COOKIE, "0" * 64)
    assert client.get("/api/actions", headers=WEB).status_code == 401
    client.cookies.set(SESSION_COOKIE, TOKEN)
    assert client.get("/api/actions", headers=WEB).status_code == 401
