"""Rank 5 (2026-10-29): the ECG archive over the Phase 2 tables, read-only.
GET /api/ecgs pages the recordings newest first with Apple's classification
exactly as recorded (device-reported; Helios interprets nothing) and a count
of newer Bridge raw ECG records; GET /api/ecgs/{id}/waveform returns one
recording's voltages, loaded only when the screen opens it. Synthetic
waveforms only (a sum of sines)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from .rank5_helpers import AUTH, T0, add_ecg, add_raw, client_for, new_store

N = 60
ODD = "Inconclusive: Poor Recording (synthetic)"


def _fill(conn):
    for i in range(N):
        kw = {}
        if i == 3:
            kw = {"classification": ODD, "symptoms": "Synthetic symptom text"}
        if i == 4:
            kw = {"rate": 514.039}                 # 15,421 samples
        if i == 5:
            kw = {"waveform": False}
        if i == 6:
            kw = {"n_samples": 100_001, "offset_min": None}
        add_ecg(conn, T0 + timedelta(days=i, minutes=i), **kw)
    newest = T0 + timedelta(days=N - 1, minutes=N - 1)
    add_raw(conn, "Electrocardiogram", 1, newest + timedelta(hours=1))
    add_raw(conn, "Electrocardiogram", 2, newest - timedelta(days=1))   # a copy of archive history
    add_raw(conn, "Workout", 3, newest + timedelta(hours=2))            # another class: not counted


def _id(i: int) -> str:
    return "ecg:" + (T0 + timedelta(days=i, minutes=i)).strftime("%Y%m%dT%H%M%SZ")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch, new_store(tmp_path, _fill)) as c:
        yield c


def test_list_shape_archive_and_newer_records(client):
    r = client.get("/api/ecgs", headers=AUTH)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["total"] == N and body["reporting_zone"] == "UTC"
    assert body["archive"] == {"origin": "apple_health_export", "oldest_recorded_utc": "2026-01-01T04:00:00Z",
                               "newest_recorded_utc": (T0 + timedelta(days=N - 1, minutes=N - 1))
                               .strftime("%Y-%m-%dT%H:%M:%SZ")}
    assert body["newer_records"] == 1
    assert "device-reported" in body["classification_note"].lower()
    assert "does not interpret" in body["classification_note"].lower()
    rows = body["ecgs"]
    assert len(rows) == 50 and rows[0]["ecg_id"] == _id(N - 1)
    assert [e["recorded_utc"] for e in rows] == sorted((e["recorded_utc"] for e in rows), reverse=True)
    assert "microvolts" not in r.text          # the list never carries a waveform


def test_row_fields_verbatim(client):
    rows = {e["ecg_id"]: e for e in client.get("/api/ecgs", params={"limit": 200}, headers=AUTH).json()["ecgs"]}
    e = rows[_id(3)]
    assert e["classification"] == ODD and e["symptoms"] == "Synthetic symptom text"
    assert e["recorded_local"] == "2026-01-04T08:03:00+04:00"
    assert e["device"] == "Watch7,1" and e["software_version"] == "2.0" and e["lead"] == "Lead I"
    assert e["sample_rate_hz"] == 512.0 and e["n_samples"] == 15360 and e["duration_s"] == 30.0
    assert e["has_waveform"] is True and e["unit"] == "µV"
    assert rows[_id(5)]["has_waveform"] is False
    assert rows[_id(0)]["symptoms"] is None
    # no print offset: the reporting zone (UTC here)
    assert rows[_id(6)]["recorded_local"] == "2026-01-07T04:06:00+00:00"
    assert set(e) == {"ecg_id", "recorded_utc", "recorded_local", "classification", "symptoms", "device",
                      "software_version", "sample_rate_hz", "lead", "unit", "n_samples", "duration_s",
                      "has_waveform"}


def test_paging(client):
    seen, cursor = [], None
    while True:
        body = client.get("/api/ecgs", params={"limit": 25} | ({"cursor": cursor} if cursor else {}),
                          headers=AUTH).json()
        seen += [e["ecg_id"] for e in body["ecgs"]]
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert len(seen) == N == len(set(seen))
    assert client.get("/api/ecgs", params={"cursor": "%%%"}, headers=AUTH).status_code == 422
    assert client.get("/api/ecgs", params={"limit": 201}, headers=AUTH).status_code == 422


def test_waveform_full_rate_integers_and_size(client):
    r = client.get(f"/api/ecgs/{_id(4)}/waveform", headers=AUTH)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    w = r.json()
    n = int(round(514.039 * 30))
    assert w["n_samples"] == n == len(w["microvolts"])
    assert w["sample_rate_hz"] == 514.039 and w["lead"] == "Lead I" and w["unit"] == "µV"
    stored = client.app.state.conn.execute("SELECT microvolts FROM ecg_waveforms WHERE ecg_id = ?",
                                           [_id(4)]).fetchone()[0]
    # whole microvolts, halves away from zero (the store's round)
    assert w["microvolts"] == [int(math.copysign(math.floor(abs(v) + 0.5), v)) for v in stored]
    assert all(isinstance(v, int) for v in w["microvolts"])
    assert len(r.content) < 150_000, len(r.content)


@pytest.mark.parametrize("ecg_id", ["ecg:20990101T000000Z", "ecg:nope", "x" * 40])
def test_waveform_unknown_is_404(client, ecg_id):
    assert client.get(f"/api/ecgs/{ecg_id}/waveform", headers=AUTH).status_code == 404


def test_waveform_missing_is_404(client):
    assert client.get(f"/api/ecgs/{_id(5)}/waveform", headers=AUTH).status_code == 404


def test_waveform_over_cap_is_422(client):
    r = client.get(f"/api/ecgs/{_id(6)}/waveform", headers=AUTH)
    assert r.status_code == 422 and "microvolts" not in r.text


@pytest.mark.parametrize("path", ["/api/ecgs", f"/api/ecgs/{_id(0)}/waveform"])
def test_needs_the_token(client, path):
    assert client.get(path).status_code == 401


def test_empty_store(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch, new_store(tmp_path, lambda c: None)) as c:
        body = c.get("/api/ecgs", headers=AUTH).json()
    assert body["total"] == 0 and body["ecgs"] == [] and body["next_cursor"] is None and body["newer_records"] == 0
    assert body["archive"] == {"origin": "apple_health_export", "oldest_recorded_utc": None,
                               "newest_recorded_utc": None}


def test_importer_output_reads_back(tmp_path, monkeypatch):
    """The real Phase 2 importer on the tracked synthetic export: every ECG it wrote is listed
    with its classification as the CSV printed it, and its waveform answers."""
    import importlib.util
    from pathlib import Path

    import duckdb

    from .phase2_helpers import june, store
    tools = Path(__file__).resolve().parents[1] / "tools"
    spec = importlib.util.spec_from_file_location("import_history_phase2_rank5e", tools / "import_history_phase2.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    st = store(tmp_path)
    assert tool.main([str(st), "--export", str(june(tmp_path)), "--out", str(tmp_path / "out")]) == 0
    with duckdb.connect(str(st), read_only=True) as ro:
        want = dict(ro.execute("SELECT ecg_id, classification FROM ecgs").fetchall())
        lens = dict(ro.execute("SELECT ecg_id, len(microvolts) FROM ecg_waveforms").fetchall())
    assert want
    with client_for(tmp_path, monkeypatch, st) as c:
        rows = c.get("/api/ecgs", headers=AUTH).json()["ecgs"]
        assert {e["ecg_id"]: e["classification"] for e in rows} == want
        for ecg_id in want:
            w = c.get(f"/api/ecgs/{ecg_id}/waveform", headers=AUTH)
            assert w.status_code == 200 and len(w.json()["microvolts"]) == lens[ecg_id]


def test_newest_date_comes_from_the_data(tmp_path, monkeypatch):
    """Owner, 2026-10-29: every archive date is read from the store, never a constant."""
    when = datetime(2026, 2, 23, 6, 15, 0)
    with client_for(tmp_path, monkeypatch, new_store(tmp_path, lambda c: add_ecg(c, when))) as c:
        body = c.get("/api/ecgs", headers=AUTH).json()
    assert body["archive"]["newest_recorded_utc"] == "2026-02-23T06:15:00Z"
