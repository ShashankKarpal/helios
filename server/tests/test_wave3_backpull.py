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


# ---------------------------------------------------------------- C2: fetch, then apply

PULL_KEYS = ["recovery", "sleep", "cycle", "samples", "retracted", "replaced_legacy", "superseded_legacy",
             "legacy_kept_pending", "skipped", "unchanged", "older", "cache_rows", "cache_removed", "dates"]


def _policy() -> MetricPolicy:
    return MetricPolicy(default_tz="Asia/Dubai")


def _store(path=None):
    conn = db.connect(path) if path is not None else db.connect_memory()
    _policy().sync_registry(conn)
    return conn


def _tables(conn, fetched_at: bool = True) -> dict:
    """Every row a Whoop apply can write, in a comparable form."""
    rec_cols = "record_key, kind, native_id, sleep_id, cycle_id, start_utc, end_utc, src_offset_min, score_state, nap, " \
               "created_at, updated_at, payload" + (", fetched_at" if fetched_at else "")
    return {"whoop_records": db.fetchall(conn, f"SELECT {rec_cols} FROM whoop_records ORDER BY 1"),
            "samples": db.fetchall(conn, "SELECT * EXCLUDE (ingested_at)" + (", ingested_at" if fetched_at else "")
                                   + " FROM samples ORDER BY sample_id"),
            "whoop_cache": db.fetchall(conn, "SELECT date, kind, payload" + (", fetched_at" if fetched_at else "")
                                       + " FROM whoop_cache ORDER BY 1, 2"),
            "dirty_dates": db.fetchall(conn, "SELECT date, reason" + (", batch_id, enqueued_at" if fetched_at else "")
                                       + " FROM dirty_dates ORDER BY 1, 2"),
            "tombstones": db.fetchall(conn, "SELECT tomb_id, metric, reason FROM tombstones ORDER BY 1"),
            "migrations": db.fetchall(conn, "SELECT * FROM migrations ORDER BY 1")}


def _moved(rec: dict, hours: int, updated_plus_h: int = 30) -> dict:
    """A revision of a sleep that moved by `hours` (a later updated_at)."""
    out = json.loads(json.dumps(rec))
    for k in ("start", "end"):
        out[k] = _z(datetime.fromisoformat(rec[k].replace("Z", "+00:00")) + timedelta(hours=hours))
    out["updated_at"] = _z(datetime.fromisoformat(rec["updated_at"].replace("Z", "+00:00")) + timedelta(hours=updated_plus_h))
    return out


def test_pull_equals_fetch_then_apply(tmp_path):
    from tests.test_whoop_records import NOW, FakeClient, cycle_rec, recovery_rec, sleep_rec
    recs = {"sleep": [sleep_rec("s1", "2026-07-09T19:30:00.000Z", "2026-07-10T02:40:00.000Z"),
                      sleep_rec("n1", "2026-07-09T09:00:00.000Z", "2026-07-09T09:40:00.000Z", nap=True)],
            "recovery": [recovery_rec(900, "s1", "2026-07-10T03:00:00.000Z")],
            "cycle": [cycle_rec(900, "2026-07-09T19:30:00.000Z", None)]}
    a, b = _store(), _store()
    policy = _policy()
    out_a = whoop.pull(a, FakeClient(tmp_path, **recs), policy, days=8, now=NOW)
    assert list(out_a) == PULL_KEYS                                               # the pull's report keeps its shape
    start, end = NOW - timedelta(days=8), NOW
    client_b = FakeClient(tmp_path, **recs)
    fetched = whoop.fetch_records(client_b, start, end)
    assert [p for p, _ in client_b.calls] == ["/recovery", "/activity/sleep", "/cycle"]
    out_b = whoop.apply_records(b, policy, fetched, datetime.now(timezone.utc), "whoop:test",
                                cache_sweep=(start.date(), end.date()))
    assert out_b == out_a and out_a["sleep"] == 2 and out_a["samples"] > 0
    assert _tables(a, fetched_at=False) == _tables(b, fetched_at=False)


def test_one_cache_sweep_equals_per_record_rebuilds():
    first = [sleep(i) for i in range(6)] + [sleep(40, nap=True)]
    first.append(dict(sleep(2), id="sl-short", updated_at=sleep(2)["updated_at"],
                      score={**sleep(2)["score"], "stage_summary": {"total_light_sleep_time_milli": 600_000}}))
    batch1 = {"sleep": first, "recovery": [recovery(i) for i in range(6)], "cycle": [cycle(i) for i in range(6)]}
    batch2 = {"sleep": [_moved(sleep(3), 24), _moved(sleep(5), -26)], "recovery": [], "cycle": [cycle(6)]}
    policy = _policy()
    per_record, swept = _store(), _store()
    at = datetime(2031, 3, 2, 8, 0, tzinfo=timezone.utc)
    for batch in (batch1, batch2):
        whoop.apply_records(per_record, policy, batch, at, "b-per-record", cache=True)
        out = whoop.apply_records(swept, policy, batch, at, "b-swept", cache=False)
        assert out["cache_sweep"] is not None
    cached = "SELECT date, kind, payload FROM whoop_cache ORDER BY 1, 2"
    assert db.fetchall(per_record, cached) == db.fetchall(swept, cached)
    assert len(db.fetchall(swept, cached)) > 12
    # A second apply of the same batch is a no-op: no record, sample, cache row or journal row changes.
    before = _tables(swept)
    again = whoop.apply_records(swept, policy, batch2, datetime(2031, 3, 3, 8, 0, tzinfo=timezone.utc), "b-again", cache=False)
    assert (again["unchanged"], again["sleep"], again["cycle"], again["cache_rows"], again["cache_removed"]) == (3, 0, 0, 0, 0)
    assert _tables(swept) == before


# ---------------------------------------------------------------- C3: the back-pull tool

import hashlib  # noqa: E402
import importlib.util  # noqa: E402
from pathlib import Path  # noqa: E402

TOOLS = Path(__file__).resolve().parents[1] / "tools"
NEW_ACCESS, NEW_REFRESH = "syn-access-token-BBBB", "syn-refresh-token-SSSS"
SECRETS = (SYN_ACCESS, SYN_REFRESH, NEW_ACCESS, NEW_REFRESH)


def _tool():
    spec = importlib.util.spec_from_file_location("whoop_backpull_under_test", TOOLS / "whoop_backpull.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _daemon_token(tmp_path, minutes_old: float = 1.0, access: str = SYN_ACCESS):
    tok = tmp_path / "daemon" / "whoop_tokens.json"
    _token_file(tok, minutes_old=minutes_old, access=access)
    return tok


def _full_api(api, n_cycles: int = 60, n_nights: int = 30) -> int:
    api.data["/cycle"] = [cycle(i) for i in range(n_cycles)]
    api.data["/recovery"] = [recovery(i) for i in range(n_nights)]
    api.data["/activity/sleep"] = [sleep(i) for i in range(n_nights)] + [sleep(n_nights + 1, nap=True)]
    return n_cycles + 2 * n_nights + 1


def _quiet(*_a, **_k):
    return None


def _fetched(tmp_path, api, tool=None, name: str = "rec"):
    tool = tool or _tool()
    tok = _daemon_token(tmp_path)
    _full_api(api)
    out = tmp_path / name
    assert tool.fetch(tok, out, sleep=_quiet, log=_quiet) == 0
    return out


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(out: Path) -> dict:
    return json.loads((out / "manifest.json").read_text())


def test_readonly_client_never_calls_the_token_url(tmp_path, api):
    tool = _tool()
    tok = _daemon_token(tmp_path, minutes_old=56)                 # the daemon's own client refreshes at 55
    api.data["/cycle"] = [cycle(i) for i in range(3)]
    daemon = WhoopClient({"token_path": str(tok), "client_id": "x", "client_secret": "y", "redirect_uri": "http://l/cb"})
    with pytest.raises(httpx.HTTPStatusError):                    # today's client posts to the token URL
        daemon._get("/cycle", {"limit": 25})
    assert api.token_posts == 1 and api.calls == []
    ro = tool.ReadOnlyTokenClient(tok, tmp_path / "out")
    with pytest.raises(tool.TokenStop):
        ro._get("/cycle", {"limit": 25})
    with pytest.raises(tool.TokenStop):
        ro._save_tokens({"access_token": "z"})
    with pytest.raises(tool.TokenStop):
        ro.exchange_code("code")
    assert api.token_posts == 1 and api.calls == []                # the read-only client asked nothing at all
    assert tool.fetch(tok, tmp_path / "rec", sleep=_quiet, log=_quiet) == 2
    _token_file(tok, minutes_old=1)                                # a fresh daemon token: requests, still no refresh
    assert len(ro._get("/cycle", {"limit": 25})["records"]) == 3
    assert api.token_posts == 1 and api.calls[-1][2] == f"Bearer {SYN_ACCESS}"


def test_refuses_a_token_near_its_refresh(tmp_path, api):
    tool = _tool()
    tok = _daemon_token(tmp_path, minutes_old=39.5)               # the daemon refreshes in 15.5 minutes
    _full_api(api)
    out, logs = tmp_path / "rec", []
    assert tool.fetch(tok, out, sleep=_quiet, log=logs.append) == 2
    assert api.calls == [] and api.token_posts == 0 and not out.exists()
    assert logs[0].startswith("token: 15 minute(s)") and logs[-1].startswith("refused:")
    # During the walk: once the daemon's refresh is under 2 minutes away, the walk stops and keeps what it has.
    _token_file(tok, minutes_old=1)
    api.on_call = lambda req, n: _token_file(tok, minutes_old=54) if n == 1 else None
    assert tool.fetch(tok, out, sleep=_quiet, log=_quiet) == 1
    m = _manifest(out)
    assert len(api.calls) == 1 and m["kinds"]["cycle"]["records"] == 25 and not m["complete"]
    assert "TokenStop" in m["stopped"] and m["records_sha256"] == _sha(out / "records.jsonl")


def test_401_takes_the_daemons_new_token_once(tmp_path, api):
    tool = _tool()
    tok = _daemon_token(tmp_path)
    total = _full_api(api)

    def daemon_refreshes_during_page_two(req, n):
        if n == 2:
            _token_file(tok, access=NEW_ACCESS, refresh=NEW_REFRESH)
            return httpx.Response(401, json={})
        return None
    api.on_call = daemon_refreshes_during_page_two
    assert tool.fetch(tok, tmp_path / "rec", sleep=_quiet, log=_quiet) == 0
    auths = [a for _, _, a in api.calls]
    assert auths[:2] == [f"Bearer {SYN_ACCESS}"] * 2 and set(auths[2:]) == {f"Bearer {NEW_ACCESS}"}
    m = _manifest(tmp_path / "rec")
    assert (m["http_401"], m["complete"], m["records_total"], api.token_posts) == (1, True, total, 0)
    # The file did not change: one 401, no second try with the same token, a stop.
    api.calls.clear()
    api.on_call = lambda req, n: httpx.Response(401, json={})
    assert tool.fetch(tok, tmp_path / "rec2", sleep=_quiet, log=_quiet) == 1
    m = _manifest(tmp_path / "rec2")
    assert len(api.calls) == 1 and m["http_401"] == 1 and "401" in m["stopped"] and api.token_posts == 0


def test_token_and_state_files_untouched(tmp_path, api):
    tool = _tool()
    tok = _daemon_token(tmp_path)
    state = tok.parent / "whoop_pull_state.json"
    state.write_text(json.dumps({"last_error": None, "last_error_at": None}))

    def fp(p: Path):
        st = os.stat(p)
        return p.read_bytes(), st.st_ino, st.st_mtime_ns, st.st_mode

    before, listing = {p.name: fp(p) for p in (tok, state)}, sorted(os.listdir(tok.parent))
    total = _full_api(api)
    api.script = [(429, {"X-RateLimit-Reset": "3"}), (500, {})]
    slept = []
    out = tmp_path / "rec"
    assert tool.fetch(tok, out, sleep=slept.append, log=_quiet) == 0
    assert {p.name: fp(p) for p in (tok, state)} == before and sorted(os.listdir(tok.parent)) == listing
    assert slept[:2] == [3.0, 4.0] and slept.count(2.0) >= 6                       # the waits, then the pace
    m = _manifest(out)
    assert (m["http_429"], m["http_5xx"], m["retries"], m["requests"], m["records_total"]) == (1, 1, 2, 9, total)
    assert all(m["kinds"][k]["complete"] for k in ("cycle", "recovery", "sleep"))
    assert m["kinds"]["cycle"]["oldest_start"] == cycle(59)["start"] and m["kinds"]["cycle"]["newest_start"] == cycle(0)["start"]
    assert sum(m["kinds"]["cycle"]["by_month_utc"].values()) == 60
    assert oct(os.stat(out).st_mode & 0o777) == "0o700" and oct(os.stat(out / "records.jsonl").st_mode & 0o777) == "0o600"
    assert sorted(os.listdir(out)) == ["manifest.json", "records.jsonl"]


def test_outputs_never_hold_the_token(tmp_path, api, capsys):
    tool = _tool()
    tok = _daemon_token(tmp_path)
    _full_api(api)
    api.on_call = lambda req, n: (_token_file(tok, access=NEW_ACCESS, refresh=NEW_REFRESH) or httpx.Response(401, json={})) \
        if n == 3 else None
    rec = tmp_path / "rec"
    assert tool.main(["fetch", "--token-path", str(tok), "--out", str(rec), "--pace", "0"]) == 0
    store = tmp_path / "copy.duckdb"
    _store(store).close()
    code, _ = tool.apply(store, rec, tmp_path / "apply", policy=_policy(), log=print)
    assert code == 0
    printed = capsys.readouterr()
    written = [p for d in (rec, tmp_path / "apply") for p in d.rglob("*") if p.is_file()]
    # The 401 left the walk's own error state beside its records, never beside the daemon's token file.
    assert sorted(p.name for p in written) == ["manifest.json", "records.jsonl", "summary.json", "whoop_pull_state.json"]
    assert sorted(os.listdir(tok.parent)) == ["whoop_tokens.json"]
    for blob in [p.read_bytes() for p in written] + [printed.out.encode(), printed.err.encode()]:
        assert not any(s.encode() in blob for s in SECRETS) and b"Bearer" not in blob


def test_apply_refuses_the_live_store(tmp_path, api, monkeypatch):
    tool = _tool()
    rec = _fetched(tmp_path, api, tool)
    live = tmp_path / "home" / "data" / "helios.duckdb"
    live.parent.mkdir(parents=True)
    _store(live).close()
    monkeypatch.setattr(tool.rd, "LIVE_STORE", live)
    before = _sha(live)
    assert tool.apply(live, rec, tmp_path / "o", policy=_policy(), log=_quiet)[0] == 2
    link = tmp_path / "alias.duckdb"
    os.symlink(live, link)
    assert tool.apply(link, rec, tmp_path / "o", policy=_policy(), log=_quiet)[0] == 2
    monkeypatch.setattr(tool.rd, "holders", lambda p: [4242])                     # the daemon still holds it
    assert tool.apply(live, rec, tmp_path / "o", apply_live=True, policy=_policy(), log=_quiet)[0] == 2
    assert _sha(live) == before and not (tmp_path / "o").exists()
    assert tool.main(["apply", str(live), str(rec), "--out", str(tmp_path / "o")]) == 2
    monkeypatch.setattr(tool.rd, "holders", lambda p: [])                         # stopped: --apply may run
    code, S = tool.apply(live, rec, tmp_path / "o", apply_live=True, policy=_policy(), log=_quiet)
    assert code == 0 and S["apply"] is True and S["counts"]["cycle"] == 60


def test_apply_twice_writes_nothing(tmp_path, api):
    tool = _tool()
    rec = _fetched(tmp_path, api, tool)
    store = tmp_path / "copy.duckdb"
    _store(store).close()
    code, S1 = tool.apply(store, rec, tmp_path / "a1", policy=_policy(), log=_quiet)
    assert code == 0 and S1["migration_written"] and not S1["wrote_nothing"]
    assert (S1["counts"]["cycle"], S1["counts"]["recovery"], S1["counts"]["sleep"]) == (60, 30, 31)
    assert S1["checks"]["stored_same_or_newer"]["ok"] and S1["checks"]["samples"]["ok"]
    assert S1["counts"]["journal_rows_deleted"] > 0 and S1["counts"]["cache_rows"] > 0
    conn = db.connect(store)
    before = _tables(conn)
    assert before["dirty_dates"] == []                                           # its own journal rows are gone
    conn.close()
    code, S2 = tool.apply(store, rec, tmp_path / "a2", policy=_policy(), log=_quiet)
    assert code == 0 and S2["wrote_nothing"] and S2["counts"]["unchanged"] == 121 and not S2["migration_written"]
    conn = db.connect(store)
    assert _tables(conn) == before
    conn.close()


def test_init_schema_opens_a_store_with_the_backpull_row(tmp_path, api):
    tool = _tool()
    rec = _fetched(tmp_path, api, tool)
    store = tmp_path / "copy.duckdb"
    _store(store).close()
    assert tool.apply(store, rec, tmp_path / "a", policy=_policy(), log=_quiet)[0] == 0
    conn = db.connect(store)                                                      # the daemon's own startup path
    try:
        assert db.unverified_migrations(conn) == [] and db.migration_applied(conn, tool.MIGRATION)
        fp, summary = db.fetchall(conn, "SELECT input_fingerprint, summary FROM migrations WHERE name = ?", [tool.MIGRATION])[0]
        assert fp == _sha(rec / "records.jsonl") and json.loads(summary)["phase"] == "verified"
    finally:
        conn.close()


def test_apply_keeps_a_newer_revision(tmp_path, api):
    tool = _tool()
    rec = _fetched(tmp_path, api, tool)
    store = tmp_path / "copy.duckdb"
    conn = _store(store)
    newer = cycle(3, strain=17.0)
    newer["updated_at"] = _z(datetime.fromisoformat(newer["updated_at"].replace("Z", "+00:00")) + timedelta(days=2))
    whoop.apply_records(conn, _policy(), {"cycle": [newer]}, datetime(2031, 3, 5, tzinfo=timezone.utc), "daemon")
    pending = db.fetchall(conn, "SELECT date, reason, batch_id, enqueued_at FROM dirty_dates ORDER BY 1")
    conn.close()
    assert pending and {r[2] for r in pending} == {"daemon"}
    code, S = tool.apply(store, rec, tmp_path / "a", policy=_policy(), log=_quiet)
    assert code == 0 and S["counts"]["older"] == 1 and S["checks"]["stored_same_or_newer"]["ok"]
    assert S["counts"]["journal_rows_restored"] >= 1 and S["counts"]["journal_rows_deleted"] > 0
    conn = db.connect(store)
    try:
        # The daemon's pending journal rows are exactly as they were; the apply's own rows are gone.
        assert db.fetchall(conn, "SELECT date, reason, batch_id, enqueued_at FROM dirty_dates ORDER BY 1") == pending
        payload = json.loads(db.fetchall(conn, "SELECT payload FROM whoop_records WHERE record_key = 'cycle:70003'")[0][0])
        assert payload == newer and payload["score"]["strain"] != cycle(3)["score"]["strain"]
        assert db.fetchall(conn, "SELECT value FROM samples WHERE sample_id = 'wh:strain:cycle:70003'") == [(newer["score"]["strain"],)]
    finally:
        conn.close()


def test_resume_continues_from_the_oldest_record(tmp_path, api):
    tool = _tool()
    tok = _daemon_token(tmp_path)
    total = _full_api(api)
    out = tmp_path / "rec"
    assert tool.fetch(tok, out, max_requests=2, sleep=_quiet, log=_quiet) == 1       # the cap stops the cycles
    m1 = _manifest(out)
    assert (m1["kinds"]["cycle"]["records"], m1["kinds"]["cycle"]["complete"], m1["complete"]) == (50, False, False)
    assert "cap" in m1["stopped"] and m1["kinds"]["recovery"]["records"] == 0
    n_before = len(api.calls)
    assert tool.fetch(tok, out, resume=True, sleep=_quiet, log=_quiet) == 0
    resumed = [p for path, p, _ in api.calls[n_before:] if path == "/cycle"]
    oldest = datetime.fromisoformat(cycle(49)["start"].replace("Z", "+00:00"))
    assert resumed == [{"end": _z(oldest + timedelta(seconds=1)), "limit": "25"}]
    keys = [tool._record_key(k, r) for k, r in tool.read_records(out / "records.jsonl")]
    assert len(keys) == len(set(keys)) == total
    m2 = _manifest(out)
    assert m2["complete"] and m2["records_total"] == total and m2["kinds"]["cycle"]["records"] == 60
    assert len(m2["runs"]) == 2 and m2["requests"] == 2 + len(api.calls) - n_before
    assert m2["records_sha256"] == _sha(out / "records.jsonl")
    assert tool.fetch(tok, out, sleep=_quiet, log=_quiet) == 2                  # a finished folder is never mixed


def test_apply_refuses_a_changed_or_incomplete_file(tmp_path, api):
    tool = _tool()
    rec = _fetched(tmp_path, api, tool)
    store = tmp_path / "copy.duckdb"
    _store(store).close()
    with open(rec / "records.jsonl", "a") as f:
        f.write(json.dumps({"kind": "cycle", "record": cycle(80)}) + "\n")
    code, S = tool.apply(store, rec, tmp_path / "a", policy=_policy(), log=_quiet)
    assert code == 1 and "sha256" in S["stopped"]
    conn = db.connect(store)
    assert db.fetchall(conn, "SELECT COUNT(*) FROM whoop_records")[0][0] == 0
    conn.close()
    assert tool.apply(store, rec, tmp_path / "z", expect_zone="Mars/Olympus", policy=_policy(), log=_quiet)[0] == 2


def test_checks_list_a_missing_sample_and_an_unresolved_recovery(tmp_path, api):
    tool = _tool()
    tok = _daemon_token(tmp_path)
    _full_api(api, n_cycles=5, n_nights=5)
    orphan = recovery(9)                                     # its cycle and sleep are not in the file
    api.data["/recovery"].append(orphan)
    rec = tmp_path / "rec"
    assert tool.fetch(tok, rec, sleep=_quiet, log=_quiet) == 0
    store = tmp_path / "copy.duckdb"
    _store(store).close()
    code, S = tool.apply(store, rec, tmp_path / "a", policy=_policy(), log=_quiet)
    unresolved = S["checks"]["recoveries_resolve"]
    assert code == 0 and unresolved["n_unresolved"] == 1 and unresolved["unresolved"][0]["missing"] == ["cycle_id", "sleep_id"]
    conn = db.connect(store)
    try:
        conn.execute("DELETE FROM samples WHERE sample_id = 'wh:hrv_rmssd:recovery:70002'")
        checks = tool.run_checks(conn, tool.read_records(rec / "records.jsonl"), _policy().zone)
    finally:
        conn.close()
    assert checks["samples"]["ok"] is False and checks["samples"]["bad"][0]["missing"] == ["hrv_rmssd"]
    assert checks["stored_same_or_newer"]["ok"]
