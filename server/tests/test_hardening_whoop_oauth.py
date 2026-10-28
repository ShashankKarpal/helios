"""Hardening review fix (2026-10-28, independent review finding 2): /whoop/login
and /whoop/callback sit outside /api/, so neither the token nor the cookie
guarded them, and they change state (the Whoop account Helios is linked to).
Any allowed client without the token could start a login, read the state from
the redirect and link its own Whoop account; a cross-site page could reset a
login in progress. Now /whoop/login needs the header token or the web
sign-in cookie on a same-origin or typed navigation (Sec-Fetch-Site none or
same-origin), and sets a short-lived state cookie (HttpOnly, Secure,
SameSite=Lax so it survives Whoop's redirect back); /whoop/callback needs the
state to match both the daemon's and that cookie's."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from heliosd import main
from heliosd.config import Settings
from heliosd.main import SESSION_COOKIE, WHOOP_STATE_COOKIE, create_app, session_value

TOKEN = "test-token-0123456789"
BASE = "https://testserver"


class FakeWhoop:
    def __init__(self):
        self.codes = []

    def login_url(self, state):
        return f"https://whoop.example/oauth?state={state}"

    def exchange_code(self, code):
        self.codes.append(code)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_WEB_DIST", str(tmp_path / "nodist"))
    raw = {"server": {"ingest_token": TOKEN}, "storage": {"db_path": str(tmp_path / "h.duckdb")},
           "notifications": {"macos_alerts": False}}
    with TestClient(create_app(Settings(raw=raw)), base_url=BASE, follow_redirects=False) as c:
        c.app.state.whoop = FakeWhoop()
        yield c


def _state_from(r):
    return r.headers["location"].split("state=", 1)[1]


def test_login_without_the_token_or_the_cookie_is_refused(client):
    r = client.get("/whoop/login")
    assert r.status_code == 401 and "location" not in r.headers
    assert getattr(client.app.state, "whoop_oauth_state", None) is None


def test_login_with_the_cookie_from_another_site_is_refused(client):
    client.cookies.set(SESSION_COOKIE, session_value(TOKEN))
    for site in ("cross-site", "same-site"):
        r = client.get("/whoop/login", headers={"Sec-Fetch-Site": site})
        assert r.status_code == 403 and "location" not in r.headers
    assert getattr(client.app.state, "whoop_oauth_state", None) is None


def test_login_with_the_cookie_on_a_typed_navigation_redirects_and_sets_the_state_cookie(client):
    client.cookies.set(SESSION_COOKIE, session_value(TOKEN))
    r = client.get("/whoop/login", headers={"Sec-Fetch-Site": "none"})
    assert r.status_code in (302, 307)
    state = _state_from(r)
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{WHOOP_STATE_COOKIE}={state};")
    low = cookie.lower()
    assert "httponly" in low and "secure" in low and "samesite=lax" in low and "max-age=600" in low


def test_login_with_the_header_token_works_for_a_script(client):
    r = client.get("/whoop/login", headers={"X-Helios-Token": TOKEN})
    assert r.status_code in (302, 307)


def test_the_callback_needs_the_state_cookie_too(client):
    r = client.get("/whoop/login", headers={"X-Helios-Token": TOKEN})
    state = _state_from(r)
    client.cookies.clear()                              # another device that read the state
    r = client.get(f"/whoop/callback?code=C1&state={state}")
    assert r.status_code == 400 and client.app.state.whoop.codes == []


def test_the_callback_with_the_matching_cookie_links_once(client):
    client.cookies.set(SESSION_COOKIE, session_value(TOKEN))
    r = client.get("/whoop/login", headers={"Sec-Fetch-Site": "none"})
    state = _state_from(r)
    r = client.get(f"/whoop/callback?code=C1&state={state}", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200 and client.app.state.whoop.codes == ["C1"]
    assert f"{WHOOP_STATE_COOKIE}=" in r.headers.get("set-cookie", "")       # cleared
    r = client.get(f"/whoop/callback?code=C2&state={state}")
    assert r.status_code == 400 and client.app.state.whoop.codes == ["C1"]


def test_a_wrong_state_is_refused(client):
    client.cookies.set(SESSION_COOKIE, session_value(TOKEN))
    _state_from(client.get("/whoop/login", headers={"Sec-Fetch-Site": "none"}))
    client.cookies.set(WHOOP_STATE_COOKIE, "forged")
    assert client.get("/whoop/callback?code=C1&state=forged").status_code == 400
    assert client.app.state.whoop.codes == []
