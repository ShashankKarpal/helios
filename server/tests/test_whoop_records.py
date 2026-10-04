"""Phase 1a item 9: the Whoop puller stores native records (whoop_records),
derives record-keyed samples for SCORED records only, re-derives on revision,
retracts with a tombstone, keeps naps out of sleep, replaces the old day-keyed
rows non-destructively with an alias, rebuilds the dated projection for the
pull window only, queries with UTC bounds and writes tokens atomically.
Oracles: the instants and values written into the fake API records."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from heliosd.ingest import whoop
from heliosd.ingest.whoop import WhoopClient, iso_z, offset_minutes
from heliosd.signals import recompute as rc
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry

DUBAI = ZoneInfo("Asia/Dubai")
NOW = datetime(2026, 7, 10, 10, 0, tzinfo=DUBAI)          # 06:00Z


def _env():
    conn = db.connect_memory()
    policy = MetricPolicy(default_tz="Asia/Dubai")
    policy.sync_registry(conn)
    return conn, policy, SourceRegistry()


class FakeClient(WhoopClient):
    """Real _paged over a stub _get: records the query params the API would see."""

    def __init__(self, tmp_path, recovery=(), sleep=(), cycle=()):
        super().__init__({"token_path": str(tmp_path / "whoop_tokens.json"), "client_id": "x",
                          "client_secret": "y", "redirect_uri": "http://localhost/cb"})
        self.data = {"/recovery": list(recovery), "/activity/sleep": list(sleep), "/cycle": list(cycle)}
        self.calls: list[tuple[str, dict]] = []

    def _get(self, path, params):
        self.calls.append((path, dict(params)))
        return {"records": self.data[path]}


def sleep_rec(id_, start, end, updated="2026-07-10T03:00:00.000Z", state="SCORED", nap=False,
              light=240, sws=60, rem=80, rr=15.1, need_h=7.9, tz="+04:00", cycle_id=900):
    score = None
    if state == "SCORED":
        score = {"stage_summary": {"total_in_bed_time_milli": 7 * 3.6e6, "total_awake_time_milli": 20 * 60000,
                                   "total_light_sleep_time_milli": light * 60000,
                                   "total_slow_wave_sleep_time_milli": sws * 60000,
                                   "total_rem_sleep_time_milli": rem * 60000},
                 "sleep_needed": {"baseline_milli": need_h * 3.6e6}, "sleep_efficiency_percentage": 90.0}
        if rr is not None:
            score["respiratory_rate"] = rr
    return {"id": id_, "user_id": 1, "cycle_id": cycle_id, "created_at": end, "updated_at": updated,
            "start": start, "end": end, "timezone_offset": tz, "nap": nap, "score_state": state, "score": score}


def recovery_rec(cycle_id, sleep_id, created, updated=None, state="SCORED", score=66, hrv=45.2, tz="+04:00"):
    return {"cycle_id": cycle_id, "sleep_id": sleep_id, "user_id": 1, "created_at": created,
            "updated_at": updated or created, "score_state": state, "timezone_offset": tz,
            "score": {"recovery_score": score, "hrv_rmssd_milli": hrv, "resting_heart_rate": 52} if state == "SCORED" else None}


def cycle_rec(id_, start, end, updated="2026-07-10T04:00:00.000Z", state="SCORED", strain=12.4, tz="+04:00"):
    return {"id": id_, "user_id": 1, "created_at": start, "updated_at": updated, "start": start, "end": end,
            "timezone_offset": tz, "score_state": state, "score": {"strain": strain} if state == "SCORED" else None}


def _samples(conn):
    return {r["sample_id"]: r for r in db.fetchdicts(conn, "SELECT * FROM samples ORDER BY sample_id")}


def test_scored_sleep_lands_natively_with_utc_bounds_and_journaled_dates(tmp_path):
    conn, policy, reg = _env()
    client = FakeClient(tmp_path, sleep=[sleep_rec("s1", "2026-07-09T19:30:00.000Z", "2026-07-10T02:40:00.000Z")])
    out = whoop.pull(conn, client, policy, days=8, now=NOW)
    assert out["sleep"] == 1 and out["samples"] == 3 and out["dates"] == ["2026-07-09", "2026-07-10"]
    # the API saw UTC instants, not the Mac clock (now is 10:00 Dubai)
    params = dict(client.calls)["/activity/sleep"]
    assert params["start"] == "2026-07-02T06:00:00.000Z" and params["end"] == "2026-07-10T06:00:00.000Z"
    rec = db.fetchdicts(conn, "SELECT * FROM whoop_records")[0]
    assert (rec["record_key"], rec["kind"], rec["native_id"], rec["sleep_id"], rec["cycle_id"]) == ("sleep:s1", "sleep", "s1", "s1", "900")
    assert rec["start_utc"] == datetime(2026, 7, 9, 19, 30) and rec["end_utc"] == datetime(2026, 7, 10, 2, 40)
    assert rec["src_offset_min"] == 240 and rec["score_state"] == "SCORED" and rec["nap"] is False
    assert rec["updated_at"] == datetime(2026, 7, 10, 3, 0) and json.loads(rec["payload"])["id"] == "s1"
    s = _samples(conn)
    sd = s["wh:sleep_duration:sleep:s1"]
    assert sd["value"] == 6.33 and sd["unit"] == "h" and sd["device_key"] == "whoop" and sd["score_state"] == "SCORED"
    assert sd["start_utc"] == datetime(2026, 7, 9, 19, 30) and sd["start_ts"] == datetime(2026, 7, 9, 23, 30)   # Dubai wall
    assert sd["end_ts"] == datetime(2026, 7, 10, 6, 40) and sd["time_source"] == "whoop_api" and sd["src_offset_min"] == 240
    assert s["wh:respiratory_rate:sleep:s1"]["value"] == 15.1 and s["wh:sleep_need:sleep:s1"]["value"] == 7.9
    assert db.fetchall(conn, "SELECT date, kind FROM whoop_cache") == [(date(2026, 7, 10), "sleep")]
    assert sorted((str(d), r) for d, r in db.fetchall(conn, "SELECT date, reason FROM dirty_dates")) == [("2026-07-09", "whoop"), ("2026-07-10", "whoop")]
    rc.drain_journal(conn, policy, reg, today=date(2026, 7, 10))
    assert db.fetchall(conn, "SELECT value, device_key FROM daily_values WHERE metric = 'sleep_duration'") == [(6.33, "whoop")]


def test_revision_rederives_in_place_and_dirties_the_dates_again(tmp_path):
    conn, policy, reg = _env()
    first = sleep_rec("s1", "2026-07-09T19:30:00.000Z", "2026-07-10T02:40:00.000Z")
    whoop.pull(conn, FakeClient(tmp_path, sleep=[first]), policy, now=NOW)
    rc.drain_journal(conn, policy, reg, today=date(2026, 7, 10))
    assert db.fetchall(conn, "SELECT COUNT(*) FROM dirty_dates")[0][0] == 0
    revised = sleep_rec("s1", "2026-07-09T19:30:00.000Z", "2026-07-10T02:40:00.000Z", updated="2026-07-10T05:00:00.000Z", light=300)
    whoop.pull(conn, FakeClient(tmp_path, sleep=[revised]), policy, now=NOW)
    rows = db.fetchall(conn, "SELECT value FROM samples WHERE metric = 'sleep_duration'")
    assert rows == [(7.33,)]                                                 # one row, new value
    assert db.fetchall(conn, "SELECT updated_at FROM whoop_records")[0][0] == datetime(2026, 7, 10, 5, 0)
    assert db.fetchall(conn, "SELECT COUNT(*) FROM whoop_records")[0][0] == 1
    assert db.fetchall(conn, "SELECT COUNT(*) FROM dirty_dates WHERE reason = 'whoop'")[0][0] == 2
    rc.drain_journal(conn, policy, reg, today=date(2026, 7, 10))
    assert db.fetchall(conn, "SELECT value FROM daily_values WHERE metric = 'sleep_duration'") == [(7.33,)]


def test_pending_then_scored_then_unscorable_then_scored_again(tmp_path):
    conn, policy, reg = _env()
    s, e = "2026-07-09T19:30:00.000Z", "2026-07-10T02:40:00.000Z"
    whoop.pull(conn, FakeClient(tmp_path, sleep=[sleep_rec("s1", s, e, state="PENDING_SCORE")]), policy, now=NOW)
    assert db.fetchall(conn, "SELECT score_state FROM whoop_records") == [("PENDING_SCORE",)]
    assert db.fetchall(conn, "SELECT COUNT(*) FROM samples")[0][0] == 0                   # stored, not sampled
    assert db.fetchall(conn, "SELECT kind FROM whoop_cache") == [("sleep",)]               # the projection still shows it
    whoop.pull(conn, FakeClient(tmp_path, sleep=[sleep_rec("s1", s, e, updated="2026-07-10T04:00:00.000Z")]), policy, now=NOW)
    assert db.fetchall(conn, "SELECT COUNT(*) FROM samples")[0][0] == 3
    out = whoop.pull(conn, FakeClient(tmp_path, sleep=[sleep_rec("s1", s, e, state="UNSCORABLE", updated="2026-07-10T04:30:00.000Z")]), policy, now=NOW)
    assert out["retracted"] == 3 and db.fetchall(conn, "SELECT COUNT(*) FROM samples")[0][0] == 0
    tombs = db.fetchall(conn, "SELECT tomb_id, reason, metric FROM tombstones ORDER BY tomb_id")
    assert tombs == [("wh:respiratory_rate:sleep:s1", "whoop_retracted", "respiratory_rate"),
                     ("wh:sleep_duration:sleep:s1", "whoop_retracted", "sleep_duration"),
                     ("wh:sleep_need:sleep:s1", "whoop_retracted", "sleep_need")]
    assert sorted(str(d) for d, in db.fetchall(conn, "SELECT date FROM dirty_dates")) == ["2026-07-09", "2026-07-10"]
    rc.drain_journal(conn, policy, reg, today=date(2026, 7, 10))
    assert db.fetchall(conn, "SELECT COUNT(*) FROM daily_values WHERE metric = 'sleep_duration'")[0][0] == 0
    whoop.pull(conn, FakeClient(tmp_path, sleep=[sleep_rec("s1", s, e, updated="2026-07-10T05:00:00.000Z")]), policy, now=NOW)
    assert db.fetchall(conn, "SELECT COUNT(*) FROM samples")[0][0] == 3
    assert db.fetchall(conn, "SELECT COUNT(*) FROM tombstones")[0][0] == 0                 # live again


def test_naps_are_records_not_sleep_samples_and_the_cache_keeps_them_apart(tmp_path):
    conn, policy, reg = _env()
    main_sleep = sleep_rec("s1", "2026-07-09T19:30:00.000Z", "2026-07-10T02:40:00.000Z")
    nap = sleep_rec("n1", "2026-07-10T10:00:00.000Z", "2026-07-10T10:40:00.000Z", nap=True,
                    updated="2026-07-10T11:00:00.000Z", light=40, sws=0, rem=0)
    whoop.pull(conn, FakeClient(tmp_path, sleep=[main_sleep, nap]), policy, now=datetime(2026, 7, 10, 16, 0, tzinfo=DUBAI))
    assert db.fetchall(conn, "SELECT nap FROM whoop_records WHERE record_key = 'sleep:n1'") == [(True,)]
    assert db.fetchall(conn, "SELECT COUNT(*) FROM samples WHERE sample_id LIKE '%:n1'")[0][0] == 0
    cache = {k: json.loads(p)["id"] for k, p in db.fetchall(conn, "SELECT kind, payload FROM whoop_cache WHERE date = DATE '2026-07-10'")}
    assert cache == {"sleep": "s1", "sleep_nap": "n1"}                  # the later nap never displaces the night


def test_legacy_day_rows_are_replaced_only_by_a_scored_row_for_the_same_day(tmp_path):
    conn, policy, reg = _env()
    legacy = [("wh:sleep_duration:2026-07-10", "sleep_duration", 5.0, "h"),
              ("wh:respiratory_rate:2026-07-10", "respiratory_rate", 14.0, "count/min"),
              ("wh:sleep_need:2026-07-11", "sleep_need", 8.0, "h"),
              ("wh:strain:2026-07-10", "strain", 9.0, "score")]
    for sid, metric, v, u in legacy:
        db.execute(conn, "INSERT INTO samples (sample_id, metric, value, unit, start_ts, end_ts, source_name, device_key, sync_path) "
                         "VALUES (?, ?, ?, ?, '2026-07-10 06:00', '2026-07-10 06:00', 'WHOOP', 'whoop', 'whoop_live')", [sid, metric, v, u])
    rec = sleep_rec("s1", "2026-07-09T19:30:00.000Z", "2026-07-10T02:40:00.000Z", rr=None)   # no respiratory rate this time
    out = whoop.pull(conn, FakeClient(tmp_path, sleep=[rec]), policy, now=NOW)
    assert out["replaced_legacy"] == 1
    ids = set(_samples(conn))
    assert "wh:sleep_duration:2026-07-10" not in ids and "wh:sleep_duration:sleep:s1" in ids
    assert "wh:respiratory_rate:2026-07-10" in ids            # no SCORED replacement: the legacy row stays
    assert "wh:sleep_need:2026-07-11" in ids                  # other day: untouched
    assert "wh:strain:2026-07-10" in ids                      # other metric: untouched
    assert db.fetchall(conn, "SELECT old_id, new_id, reason FROM sample_aliases") == \
        [("wh:sleep_duration:2026-07-10", "wh:sleep_duration:sleep:s1", "whoop_record_identity")]


def test_recovery_is_keyed_by_cycle_and_the_open_cycle_has_no_end(tmp_path):
    conn, policy, reg = _env()
    client = FakeClient(tmp_path,
                        recovery=[recovery_rec("c7", "s1", "2026-07-10T03:05:00.000Z")],
                        cycle=[cycle_rec("c7", "2026-07-09T19:00:00.000Z", None)])
    out = whoop.pull(conn, client, policy, now=NOW)
    assert out["recovery"] == 1 and out["cycle"] == 1 and out["samples"] == 3
    recs = {r["record_key"]: r for r in db.fetchdicts(conn, "SELECT * FROM whoop_records")}
    assert recs["recovery:c7"]["cycle_id"] == "c7" and recs["recovery:c7"]["sleep_id"] == "s1"
    assert recs["cycle:c7"]["end_utc"] is None and recs["cycle:c7"]["start_utc"] == datetime(2026, 7, 9, 19, 0)
    s = _samples(conn)
    assert s["wh:recovery_score:recovery:c7"]["value"] == 66 and s["wh:hrv_rmssd:recovery:c7"]["value"] == 45.2
    assert s["wh:recovery_score:recovery:c7"]["start_ts"] == datetime(2026, 7, 10, 7, 5)       # created_at in Dubai
    assert s["wh:strain:cycle:c7"]["start_ts"] == s["wh:strain:cycle:c7"]["end_ts"] == datetime(2026, 7, 9, 23, 0)
    assert db.fetchall(conn, "SELECT date, kind FROM whoop_cache ORDER BY kind") == [(date(2026, 7, 9), "cycle"), (date(2026, 7, 10), "recovery")]


def test_cache_shows_the_latest_revision_per_date_and_never_touches_other_dates(tmp_path):
    conn, policy, reg = _env()
    db.execute(conn, "INSERT INTO whoop_cache (date, kind, payload) VALUES ('2026-06-01', 'recovery', '{\"old\": true}')")
    a = sleep_rec("a", "2026-07-09T19:00:00.000Z", "2026-07-09T23:30:00.000Z", light=120, sws=30, rem=30, updated="2026-07-10T01:00:00.000Z")
    b = sleep_rec("b", "2026-07-10T00:00:00.000Z", "2026-07-10T02:30:00.000Z", light=90, sws=20, rem=20, updated="2026-07-10T03:00:00.000Z")
    whoop.pull(conn, FakeClient(tmp_path, sleep=[a, b]), policy, now=NOW)         # a split night: both end on 07-10 Dubai
    assert json.loads(db.fetchall(conn, "SELECT payload FROM whoop_cache WHERE date = DATE '2026-07-10' AND kind = 'sleep'")[0][0])["id"] == "b"
    assert db.fetchall(conn, "SELECT payload FROM whoop_cache WHERE date = DATE '2026-06-01'") == [('{"old": true}',)]
    rc.drain_journal(conn, policy, reg, today=date(2026, 7, 10))
    # two direct records on one night are never additive: the longer one is the night
    assert db.fetchall(conn, "SELECT value FROM daily_values WHERE metric = 'sleep_duration'") == [(3.0,)]


def test_token_file_is_written_atomically_with_0600(tmp_path, monkeypatch):
    client = FakeClient(tmp_path)
    client._save_tokens({"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    p = client.token_path
    assert oct(p.stat().st_mode & 0o777) == "0o600" and json.loads(p.read_text())["saved_at"]
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []
    before = p.read_text()

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(whoop.os, "replace", boom)
    with pytest.raises(OSError):
        client._save_tokens({"access_token": "b", "refresh_token": "r2", "expires_in": 3600})
    assert p.read_text() == before                                       # the old file survives a failed write
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []  # and no temp file is left behind


def test_helpers_and_records_without_an_id_are_counted_not_fatal(tmp_path):
    assert offset_minutes("+04:00") == 240 and offset_minutes("-05:30") == -330 and offset_minutes("+0400") == 240
    assert offset_minutes("Z") == 0 and offset_minutes(None) is None and offset_minutes("junk") is None
    assert iso_z(datetime(2026, 7, 10, 10, 0, tzinfo=DUBAI)) == "2026-07-10T06:00:00.000Z"
    assert iso_z(datetime(2026, 7, 10, 6, 0)) == "2026-07-10T06:00:00.000Z"                # naive reads as UTC
    assert iso_z(datetime(2026, 7, 10, 6, 0, tzinfo=timezone.utc)) == "2026-07-10T06:00:00.000Z"
    conn, policy, reg = _env()
    bad = recovery_rec("c1", "s1", "2026-07-10T03:05:00.000Z")
    del bad["cycle_id"]
    out = whoop.pull(conn, FakeClient(tmp_path, recovery=[bad]), policy, now=NOW)
    assert out["skipped"] == 1 and out["recovery"] == 0
