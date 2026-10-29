"""Rank 5 (2026-10-29): /api/chat is switched off (owner decisions 2026-10-21
and 2026-10-29: Ask Helios is frozen; Claude through the Helios MCP is the
chat). [chat] enabled = true turns it back on; anything else leaves it off.
Off, the route answers 404 before it reads the body or asks LM Studio; the
token check still runs first. narrative/chat.py stays: the five MCP tool
routes import their helpers from it and must still reach their handlers."""

from __future__ import annotations

import pytest

from heliosd.config import Settings
from heliosd.narrative import chat as chat_module

from .rank5_helpers import AUTH, client_for

TOOLS = {"query_metric": ("/api/tool/query_metric", {"metric": "steps"}),
         "signals": ("/api/tool/signals", {}),
         "compare": ("/api/tool/compare", {"metric": "steps"}),
         "events": ("/api/tool/events", {}),
         "whoop_live": ("/api/tool/whoop_live", {})}


@pytest.mark.parametrize("chat, on", [(None, False), ({}, False), ({"enabled": False}, False),
                                      ({"enabled": "true"}, False), ({"enabled": 1}, False),
                                      ({"enabled": True}, True)])
def test_switch_reads_only_a_toml_true(chat, on):
    raw = {} if chat is None else {"chat": chat}
    assert Settings(raw=raw).chat_enabled is on


@pytest.fixture()
def off(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch) as c:
        yield c


@pytest.fixture()
def on(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch, extra={"chat": {"enabled": True}}) as c:
        yield c


def test_off_by_default_answers_404(off):
    r = off.post("/api/chat", json={"message": "hello"}, headers=AUTH)
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}


def test_off_404_before_the_body_is_read(off):
    r = off.post("/api/chat", content=b"not json", headers=AUTH | {"Content-Type": "application/json"})
    assert r.status_code == 404


def test_off_still_needs_the_token(off):
    assert off.post("/api/chat", json={"message": "hello"}).status_code == 401


def test_off_never_asks_lm_studio(off, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("LM Studio asked while chat is off")
    monkeypatch.setattr(off.app.state.lm, "available", boom)
    monkeypatch.setattr("heliosd.main.run_chat", boom)
    assert off.post("/api/chat", json={"message": "hello"}, headers=AUTH).status_code == 404


def test_on_reaches_the_route(on):
    # LM Studio sits at a dead port in these tests, so the live route says so.
    r = on.post("/api/chat", json={"message": "hello"}, headers=AUTH)
    assert r.status_code == 503 and "LM Studio" in r.json()["detail"]


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_every_tool_route_reaches_its_handler_with_chat_off(off, monkeypatch, name):
    """Each handler imports its helper from narrative/chat.py at call time; a sentinel in its place
    proves the reply came from the handler, not from a 404 or the app shell's catch-all."""
    path, params = TOOLS[name]
    monkeypatch.setattr(chat_module, f"_tool_{name}", lambda *a, **k: {"sentinel": name})
    r = off.get(path, params=params, headers=AUTH)
    assert r.status_code == 200, (name, r.status_code)
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"sentinel": name}


def test_a_top_level_chat_key_is_off_not_an_error(tmp_path, monkeypatch):
    """Review: `chat = true` at the top of the file (not a [chat] table) must read as off."""
    assert Settings(raw={"chat": True}).chat_enabled is False
    with client_for(tmp_path, monkeypatch, extra=None) as c:
        c.app.state.settings.raw["chat"] = True
        assert c.post("/api/chat", json={"message": "hello"}, headers=AUTH).status_code == 404
