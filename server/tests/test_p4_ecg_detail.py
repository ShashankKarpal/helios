"""P4 (2026-10-31): GET /api/ecgs/{ecg_id} returns one recording: the list's
row fields, the reporting zone and the device-reported classification note,
and never the waveform. Before P4 the path fell into the app shell's
catch-all and answered index.html with 200, so every test reads the JSON
shape. Unknown and malformed ids are 404; no token is 401. Synthetic only."""

from __future__ import annotations

from datetime import timedelta

import pytest

from .rank5_helpers import AUTH, T0, add_ecg, client_for, new_store

ODD = "Inconclusive: Poor Recording (synthetic)"


def _fill(conn):
    add_ecg(conn, T0)
    add_ecg(conn, T0 + timedelta(days=1), classification=ODD, symptoms="Synthetic symptom text", rate=514.039)
    add_ecg(conn, T0 + timedelta(days=2), waveform=False, offset_min=None)


def _id(i: int) -> str:
    return "ecg:" + (T0 + timedelta(days=i)).strftime("%Y%m%dT%H%M%SZ")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch, new_store(tmp_path, _fill)) as c:
        yield c


def test_one_recording_as_json_without_a_waveform(client):
    r = client.get(f"/api/ecgs/{_id(1)}", headers=AUTH)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["reporting_zone"] == "UTC"
    assert "device-reported" in body["classification_note"].lower()
    e = body["ecg"]
    assert e["ecg_id"] == _id(1)
    assert e["classification"] == ODD and e["symptoms"] == "Synthetic symptom text"
    assert e["recorded_utc"] == "2026-01-02T04:00:00Z" and e["recorded_local"] == "2026-01-02T08:00:00+04:00"
    assert e["sample_rate_hz"] == 514.039 and e["n_samples"] == 15421 and e["duration_s"] == 30.0
    assert e["lead"] == "Lead I" and e["device"] == "Watch7,1" and e["has_waveform"] is True
    assert "microvolts" not in r.text


def test_the_detail_row_equals_the_list_row(client):
    rows = {e["ecg_id"]: e for e in client.get("/api/ecgs", headers=AUTH).json()["ecgs"]}
    for i in range(3):
        assert client.get(f"/api/ecgs/{_id(i)}", headers=AUTH).json()["ecg"] == rows[_id(i)]


def test_no_waveform_and_no_offset_still_answer(client):
    e = client.get(f"/api/ecgs/{_id(2)}", headers=AUTH).json()["ecg"]
    assert e["has_waveform"] is False and e["recorded_local"] == "2026-01-03T04:00:00+00:00"


@pytest.mark.parametrize("bad", ["ecg:20990101T000000Z", "ecg:2026", "nope", "wk:xw:1:1", "ecg:20260101T040000Z.."])
def test_unknown_or_malformed_ids_are_404_json(client, bad):
    r = client.get(f"/api/ecgs/{bad}", headers=AUTH)
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")


def test_no_token_is_401(client):
    assert client.get(f"/api/ecgs/{_id(0)}").status_code == 401


def test_the_waveform_route_is_unchanged(client):
    r = client.get(f"/api/ecgs/{_id(0)}/waveform", headers=AUTH)
    assert r.status_code == 200 and len(r.json()["microvolts"]) == 15360
