"""P4 (2026-10-31): GET /api/workouts/{id} sends each source's metadata as
distinct (key, value) pairs in first-seen order, and a distinct total. The
store keeps every pair twice (found 2026-10-30, a data round under rule 4g
later); the route must not pass the doubles on, so the MCP tools never see
them. The store is not changed. Synthetic values only."""

from __future__ import annotations

from datetime import timedelta

import pytest

from .rank5_helpers import AUTH, T0, _insert, add_workout, client_for, new_store

DOUBLED = 0      # a source whose pairs are stored twice, as the live store holds them
MIXED = 1        # one key with two values, plus a repeated pair in between
MANY = 2         # more distinct pairs than the cap, each stored twice
PAIRS = [("HKIndoorWorkout", "0"), ("HKTimeZone", "Synthetic/Zone"), ("HKAverageMETs", "4.2 kcal/hr·kg"),
         ("HKMetadataKeySyncIdentifier", "syn-sync"), ("HKWeatherHumidity", "5000 %")]


def _wid(n: int) -> str:
    return "wk:xw:%024x:%d" % (n, n)


def _anchor(n: int) -> str:
    return "xw:%024x:%d" % (n, n)


def _meta(conn, n: int, pairs: list[tuple[str, str]]) -> None:
    conn.execute("DELETE FROM workout_metadata WHERE source_id = ?", [_anchor(n)])
    for i, (k, v) in enumerate(pairs):
        _insert(conn, "workout_metadata", {"source_id": _anchor(n), "ord": i, "meta_key": k, "meta_value": v})


def _fill(conn):
    for n in (DOUBLED, MIXED, MANY):
        add_workout(conn, n, T0 + timedelta(days=n))
    _meta(conn, DOUBLED, PAIRS + PAIRS)                          # ord 0 to 4 repeated as 5 to 9
    _meta(conn, MIXED, [("HKLapLength", "25 m"), ("HKIndoorWorkout", "1"), ("HKLapLength", "50 m"),
                        ("HKLapLength", "25 m")])
    many = [(f"SyntheticKey{i:03d}", str(i)) for i in range(130)]
    _meta(conn, MANY, many + many)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch, new_store(tmp_path, _fill)) as c:
        yield c


def _metadata(client, n: int) -> dict:
    r = client.get(f"/api/workouts/{_wid(n)}", headers=AUTH)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    return r.json()["sources"][0]["metadata"]


def test_a_doubled_source_sends_each_pair_once_with_a_distinct_total(client):
    m = _metadata(client, DOUBLED)
    visible = [[k, v] for k, v in PAIRS if "sync" not in k.lower()]
    assert m["items"] == visible            # first-seen order, sync keys still left out
    assert m["total"] == len(visible) == 4


def test_one_key_with_two_values_stays_two_pairs(client):
    m = _metadata(client, MIXED)
    assert m["items"] == [["HKLapLength", "25 m"], ["HKIndoorWorkout", "1"], ["HKLapLength", "50 m"]]
    assert m["total"] == 3


def test_the_cap_counts_distinct_pairs(client):
    m = _metadata(client, MANY)
    assert m["total"] == 130
    assert len(m["items"]) == 100
    assert m["items"][0] == ["SyntheticKey000", "0"] and m["items"][-1] == ["SyntheticKey099", "99"]


def test_the_store_keeps_its_rows(tmp_path, monkeypatch):
    # Read-only: the route dedupes what it sends; the stored doubles stay for the rule 4g round.
    from heliosd.store import db
    path = new_store(tmp_path, _fill)
    with client_for(tmp_path, monkeypatch, path) as c:
        assert _metadata(c, DOUBLED)["total"] == 4
    conn = db.connect(path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM workout_metadata WHERE source_id = ?", [_anchor(DOUBLED)]).fetchone()[0]
    finally:
        conn.close()
    assert n == 2 * len(PAIRS)
