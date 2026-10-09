"""Wave 3 (fix program B14, design docs/briefs/build-2026-10-09/wave3/design.md
section 5): the Whoop walk back to the first record. C1 pages without a start,
paced and patient with a 429; C2 fetches and applies as two steps; C3 is the
back-pull tool (a read-only token client, a private records file, an apply to a
store copy). Synthetic records, tokens and dates only; the Whoop API is an
httpx.MockTransport and time never really passes."""

from __future__ import annotations

import json
import os
import time as _time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from heliosd.ingest import whoop
from heliosd.ingest.whoop import WhoopClient
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy

SYN_ACCESS = "syn-access-token-AAAA"
SYN_REFRESH = "syn-refresh-token-RRRR"
BASE = datetime(2031, 3, 1, 20, 0, tzinfo=timezone.utc)        # a synthetic evening, far from any real night


def _z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _token_file(path, minutes_old: float = 1.0, access: str = SYN_ACCESS, refresh: str = SYN_REFRESH,
                expires_in: int = 3600) -> None:
    """A token file in the daemon's shape (saved_at is the naive local clock)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    saved = datetime.now() - timedelta(minutes=minutes_old)
    path.write_text(json.dumps({"access_token": access, "refresh_token": refresh, "expires_in": expires_in,
                                "token_type": "bearer", "saved_at": saved.isoformat()}))
    os.chmod(path, 0o600)


def _client(tmp_path, **cfg) -> WhoopClient:
    tok = tmp_path / "daemon" / "whoop_tokens.json"
    if not tok.exists():
        _token_file(tok)
    return WhoopClient({"token_path": str(tok), "client_id": "x", "client_secret": "y",
                        "redirect_uri": "http://localhost/cb", **cfg})


def cycle(i: int, base: datetime = BASE, strain: float = 9.5) -> dict:
    """Synthetic cycle i: i days before base, newest first when i ascends."""
    s = base - timedelta(days=i)
    return {"id": 70000 + i, "user_id": 1, "created_at": _z(s), "updated_at": _z(s + timedelta(hours=20)),
            "start": _z(s), "end": _z(s + timedelta(hours=24)) if i else None, "timezone_offset": "+04:00",
            "score_state": "SCORED", "score": {"strain": strain + (i % 5) / 10}}


def sleep(i: int, base: datetime = BASE, nap: bool = False) -> dict:
    s = base - timedelta(days=i) + timedelta(hours=2)
    e = s + timedelta(hours=7)
    return {"id": f"sl-{i:04d}", "cycle_id": 70000 + i, "user_id": 1, "created_at": _z(e), "updated_at": _z(e + timedelta(hours=1)),
            "start": _z(s), "end": _z(e), "timezone_offset": "+04:00", "nap": nap, "score_state": "SCORED",
            "score": {"stage_summary": {"total_in_bed_time_milli": 7 * 3_600_000, "total_awake_time_milli": 1_200_000,
                                        "total_light_sleep_time_milli": 3 * 3_600_000 + (i % 7) * 60_000,
                                        "total_slow_wave_sleep_time_milli": 3_600_000,
                                        "total_rem_sleep_time_milli": 5_400_000},
                      "respiratory_rate": 14.5 + (i % 3) / 10, "sleep_efficiency_percentage": 91.0,
                      "sleep_needed": {"baseline_milli": 27_000_000, "need_from_sleep_debt_milli": 600_000,
                                       "need_from_recent_strain_milli": 300_000, "need_from_recent_nap_milli": 0}}}


def recovery(i: int, base: datetime = BASE) -> dict:
    c = base - timedelta(days=i) + timedelta(hours=9, minutes=30)
    return {"cycle_id": 70000 + i, "sleep_id": f"sl-{i:04d}", "user_id": 1, "created_at": _z(c),
            "updated_at": _z(c + timedelta(minutes=5)), "score_state": "SCORED", "timezone_offset": "+04:00",
            "score": {"recovery_score": 40 + i % 50, "hrv_rmssd_milli": 30.0 + i % 20, "resting_heart_rate": 50 + i % 9,
                      "user_calibrating": False}}


def _start_of(path: str, rec: dict) -> str:
    return rec["created_at"] if path == "/recovery" else rec["start"]


class FakeApi:
    """The three collection endpoints over httpx.MockTransport: newest first,
    `limit` per page, a numeric next_token, start/end filters on the record's
    start (a recovery's created_at). `script` answers the next requests first
    with (status, headers); `token_posts` counts any call to the token URL."""

    def __init__(self, data: dict[str, list[dict]] | None = None, guard: int = 50):
        self.data = {p: sorted(v, key=lambda r: _start_of(p, r), reverse=True) for p, v in (data or {}).items()}
        self.calls: list[tuple[str, dict, str | None]] = []
        self.token_posts = 0
        self.script: list[tuple[int, dict]] = []
        self.guard = guard
        self.on_call = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(whoop.TOKEN_URL):
            self.token_posts += 1
            return httpx.Response(400, json={"error": "invalid_grant"})
        path = request.url.path.replace("/developer/v2", "")
        params = dict(request.url.params)
        self.calls.append((path, params, request.headers.get("Authorization")))
        if len(self.calls) > self.guard:
            raise AssertionError(f"more than {self.guard} requests: an endless walk")
        if self.on_call is not None:
            hooked = self.on_call(request, len(self.calls))
            if hooked is not None:
                return hooked
        if self.script:
            status, headers = self.script.pop(0)
            if status != 200:
                return httpx.Response(status, headers=headers, json={"error": status})
        recs = self.data.get(path, [])
        if params.get("end"):
            recs = [r for r in recs if _start_of(path, r) < params["end"]]
        if params.get("start"):
            recs = [r for r in recs if _start_of(path, r) >= params["start"]]
        off, lim = int(params.get("nextToken") or 0), int(params["limit"])
        nxt = str(off + lim) if off + lim < len(recs) else None
        return httpx.Response(200, json={"records": recs[off:off + lim], "next_token": nxt})


@pytest.fixture
def api(monkeypatch):
    fake = FakeApi()
    mock = httpx.Client(transport=httpx.MockTransport(fake))
    monkeypatch.setattr(httpx, "get", lambda url, **kw: mock.get(url, **kw))
    monkeypatch.setattr(httpx, "post", lambda url, **kw: mock.post(url, **kw))
    return fake


# ---------------------------------------------------------------- C1: paging to the first record

def test_paged_without_start_walks_to_the_first_record(tmp_path, api):
    api.data["/cycle"] = [cycle(i) for i in range(60)]
    pages = []
    out = _client(tmp_path)._paged("/cycle", on_page=lambda recs, nxt: pages.append((len(recs), nxt)))
    assert [r["id"] for r in out] == [70000 + i for i in range(60)]               # newest first, down to the first
    assert pages == [(25, "25"), (25, "50"), (10, None)]
    sent = [p for _, p, _ in api.calls]
    assert all("start" not in p and "end" not in p and p["limit"] == "25" for p in sent)
    assert [p.get("nextToken") for p in sent] == [None, "25", "50"]


def test_pace_sleeps_between_pages_only(tmp_path, api):
    api.data["/cycle"] = [cycle(i) for i in range(30)]
    slept = []
    _client(tmp_path)._paged("/cycle", pace_s=2, sleep=slept.append)
    assert slept == [2]                                                            # two pages, one pause


def test_429_is_waited_out_then_retried(tmp_path, api):
    api.data["/cycle"] = [cycle(i) for i in range(3)]
    c = _client(tmp_path)
    slept = []
    api.script = [(429, {"X-RateLimit-Reset": "7"})]
    page = c._get("/cycle", {"limit": 25}, retries=3, sleep=slept.append)
    assert len(page["records"]) == 3 and slept == [7.0]
    api.script = [(503, {}), (502, {})]
    c._get("/cycle", {"limit": 25}, retries=3, sleep=slept.append)
    assert slept == [7.0, 2.0, 4.0]
    api.script = [(429, {"Retry-After": "500"})]                                  # capped at 120 s
    c._get("/cycle", {"limit": 25}, retries=3, sleep=slept.append)
    assert slept[-1] == 120.0
    assert c.http_stats == {"requests": 7, "retries": 4, "http_429": 2, "http_5xx": 2}
    api.script = [(429, {"X-RateLimit-Reset": "1"})] * 4                          # out of tries: the error stands
    with pytest.raises(httpx.HTTPStatusError):
        c._get("/cycle", {"limit": 25}, retries=3, sleep=slept.append)
    assert slept[-3:] == [1.0, 1.0, 1.0] and c.last_error == "GET /cycle failed (HTTP 429)"
    api.script = [(401, {})]                                                       # anything else is not retried
    with pytest.raises(httpx.HTTPStatusError):
        c._get("/cycle", {"limit": 25}, retries=3, sleep=lambda s: pytest.fail("a 401 was retried"))


def test_request_cap_stops_an_endless_next_token(tmp_path, api):
    api.on_call = lambda req, n: httpx.Response(200, json={"records": [cycle(n)], "next_token": "again"})
    with pytest.raises(whoop.RequestCapReached):
        _client(tmp_path)._paged("/cycle", max_requests=5)
    assert len(api.calls) == 5


def test_daemon_pull_params_unchanged(tmp_path, api, monkeypatch):
    """Guard: the daemon's pull sends start, end and limit 25 exactly as before,
    passes no retry or pacing to _get, and a 429 still fails the pull at once."""
    from tests.test_whoop_records import NOW, _env

    class Recorder(WhoopClient):
        def __init__(self):
            super().__init__({"token_path": str(tmp_path / "r" / "whoop_tokens.json"), "client_id": "x",
                              "client_secret": "y", "redirect_uri": "http://localhost/cb"})
            self.seen = []

        def _get(self, path, params, **kw):
            self.seen.append((path, dict(params), kw))
            return {"records": []}

    conn, policy, _ = _env()
    rec = Recorder()
    whoop.pull(conn, rec, policy, days=8, now=NOW)
    window = {"start": "2026-07-02T06:00:00.000Z", "end": "2026-07-10T06:00:00.000Z", "limit": 25}
    assert rec.seen == [("/recovery", window, {}), ("/activity/sleep", window, {}), ("/cycle", window, {})]
    assert [list(p) for _, p, _ in rec.seen] == [["start", "end", "limit"]] * 3      # same keys, same order
    monkeypatch.setattr(_time, "sleep", lambda s: pytest.fail("the daemon's pull slept"))
    api.script = [(429, {"X-RateLimit-Reset": "5"})]
    with pytest.raises(httpx.HTTPStatusError):
        whoop.pull(conn, _client(tmp_path), policy, days=8, now=NOW)
    assert len(api.calls) == 1
