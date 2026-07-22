"""Insights module tests.

These run on synthetic in memory data. The modules under test import cleanly
without scipy, so collection never depends on the optional 'insights' group.
Statistical asserts that genuinely need scipy are guarded with
pytest.importorskip so the suite stays green on a machine without it.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta

import pytest

# Must import without scipy present.
from heliosd.insights import correlations, doctor_report, labs_import, weekly_review
from heliosd.store import db

END = date(2026, 7, 15)
DAYS = 40


def _insert_daily(conn, d, metric, value, device="whoop", unit=""):
    db.execute(conn, """
        INSERT OR REPLACE INTO daily_values
          (date, metric, value, unit, device_key, n_samples, confidence, grade)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [d, metric, value, unit, device, 1, 0.9, "A"])


def _insert_sleep_sample(conn, d, stage, minutes):
    wake = datetime.combine(d, datetime.min.time()) + timedelta(hours=7)
    start = wake - timedelta(minutes=minutes)
    sid = f"{d}-{stage}-{minutes}"
    db.execute(conn, """
        INSERT OR REPLACE INTO samples
          (sample_id, metric, value, text_value, unit, start_ts, end_ts,
           source_name, device_key, sync_path)
        VALUES (?, 'sleep_analysis', ?, ?, 'min', ?, ?, 'WHOOP', 'whoop', 'whoop_live')""",
        [sid, float(minutes), stage, start, wake])


@pytest.fixture(scope="module")
def conn():
    c = db.connect_memory()
    rng = random.Random(7)

    for i in range(DAYS):
        d = END - timedelta(days=DAYS - 1 - i)
        # Planted positive relationship: sleep_duration and hrv_rmssd share a
        # latent factor plus small independent noise.
        latent = rng.gauss(0, 1)
        sleep_h = 7.2 + 0.55 * latent + rng.gauss(0, 0.18)
        hrv = 58.0 + 9.0 * latent + rng.gauss(0, 1.5)
        recovery = 60.0 + 8.0 * latent + rng.gauss(0, 3.0)
        strain = 12.0 + rng.gauss(0, 2.0)

        _insert_daily(c, d, "sleep_duration", round(sleep_h, 2), unit="h")
        _insert_daily(c, d, "hrv_rmssd", round(hrv, 1), unit="ms")
        _insert_daily(c, d, "recovery_score", round(recovery, 1), unit="%")
        _insert_daily(c, d, "strain", round(strain, 1), unit="")
        _insert_daily(c, d, "resting_hr", round(rng.gauss(56, 2), 0), "apple_watch_ultra", "bpm")
        _insert_daily(c, d, "respiratory_rate", round(rng.gauss(15, 0.5), 1),
                      "apple_watch_ultra", "breaths/min")
        _insert_daily(c, d, "spo2", round(rng.gauss(97, 0.6), 1), "apple_watch_ultra", "%")
        _insert_daily(c, d, "steps", round(rng.gauss(8000, 1500)), "iphone", "count")
        if d.weekday() == 0:
            _insert_daily(c, d, "body_mass", round(rng.gauss(109, 0.5), 1),
                          "zepp_life_scale", "kg")

        # Sleep architecture stages for the night.
        _insert_sleep_sample(c, d, "deep", round(rng.gauss(75, 10)))
        _insert_sleep_sample(c, d, "rem", round(rng.gauss(95, 12)))
        _insert_sleep_sample(c, d, "core", round(rng.gauss(210, 20)))

        # Late caffeine on a subset of days (evening dose at 20:00).
        if i % 3 == 0:
            ts = datetime.combine(d, datetime.min.time()) + timedelta(hours=20)
            db.execute(c, "INSERT INTO events (event_id, kind, ts, payload) VALUES (?,?,?,?)",
                       [f"caf-{i}", "caffeine", ts, '{"item":"coffee","amount":"1"}'])

    # One flagged anomaly in the last week.
    db.execute(c, """INSERT OR REPLACE INTO signals
        (date, metric, state, value, why) VALUES (?, 'resting_hr', 'flag', 66.0,
        'Resting heart rate ran above your recent range.')""", [END - timedelta(days=2)])

    return c


def test_modules_import_without_scipy():
    # Import already happened at module top; assert the public API exists.
    assert hasattr(correlations, "top_insights")
    assert hasattr(correlations, "cutoff_finder")
    assert hasattr(weekly_review, "build_weekly_review")
    assert hasattr(doctor_report, "build_doctor_report_html")
    assert hasattr(labs_import, "parse_labs_file")


def test_bh_fdr_monotone_and_bounded():
    q = correlations._bh_fdr([0.001, 0.01, 0.2, 0.5, 0.9])
    assert all(0.0 <= v <= 1.0 for v in q)
    assert len(q) == 5


def test_top_insights_finds_planted_correlation(conn):
    pytest.importorskip("scipy")
    insights = correlations.top_insights(conn, days=90)
    assert insights, "expected at least one insight"
    joined = " ".join((i["title"] + " " + i["detail"]).lower() for i in insights)
    assert "sleep duration" in joined and "hrv" in joined
    # The planted pair should read as a positive association.
    pair = [i for i in insights if "sleep duration" in i["title"].lower()
            and "hrv" in i["title"].lower()]
    assert pair, "planted sleep/HRV pair not surfaced"
    assert "together" in pair[0]["title"].lower()
    assert pair[0]["method"] == "Spearman, BH-FDR"
    assert pair[0]["n"] >= correlations.MIN_PAIRED_DAYS


def test_top_insights_never_raises_on_empty():
    empty = db.connect_memory()
    assert correlations.top_insights(empty, days=90) == []
    out = correlations.cutoff_finder(empty)
    assert isinstance(out, dict) and out["cutoff_hour"] is None


def test_cutoff_finder_returns_dict(conn):
    out = correlations.cutoff_finder(conn, substance="caffeine",
                                     metric="sleep_duration", days=120)
    assert isinstance(out, dict)
    assert out["substance"] == "caffeine" and out["metric"] == "sleep_duration"
    assert "note" in out


def test_weekly_review_sections(conn):
    review = weekly_review.build_weekly_review(conn, policy=None)
    md = review["markdown"]
    for heading in ["# Weekly Review", "## Recovery trend", "## Sleep architecture",
                    "## Strain versus recovery", "## Flagged anomalies",
                    "## Patterns", "## Experiment for the week"]:
        assert heading in md, f"missing section: {heading}"
    assert "\u2014" not in md, "no em dashes allowed"
    assert review["data"]["sleep"]["nights"] >= 1
    assert review["data"]["experiment"]


def test_doctor_report_html(conn):
    # Seed a couple of labs so the section is populated.
    labs_import.confirm_and_store(conn, "2026-07-01", [
        {"biomarker": "HbA1c", "value": 5.4, "unit": "%", "ref_high": 5.7},
        {"biomarker": "LDL Cholesterol", "value": 150.0, "unit": "mg/dL",
         "ref_high": 100.0},
    ])
    html = doctor_report.build_doctor_report_html(conn, "Shashank Karpal")
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "Shashank Karpal" in html
    assert "Vitals summary" in html and "Recent labs" in html
    assert "HbA1c" in html
    assert "Generated locally by Helios. Not a medical document." in html
    assert "\u2014" not in html, "no em dashes allowed"
    # LDL above ref should be flagged High.
    assert "High" in html


SAMPLE_LAB_TEXT = """
    LifeLabs Comprehensive Panel
    Collection Date: 2026-06-18

    Lipid Panel
    Total Cholesterol      185 mg/dL      < 200
    LDL Cholesterol        110 mg/dL      0 - 100
    HDL Cholesterol         58 mg/dL      > 40
    Triglipes... Triglycerides   95 mg/dL   < 150

    Metabolic
    HbA1c                  5.4 %          < 5.7
    Fasting Glucose        92 mg/dL       70 - 99

    Vitamin D (25-OH)      32 ng/mL       30 - 100
    TSH                    2.1 mIU/L      0.4 - 4.0
"""


def test_labs_extraction_from_text():
    cands = labs_import.extract_biomarkers(SAMPLE_LAB_TEXT)
    found = {c["biomarker"]: c for c in cands}
    assert "HbA1c" in found
    assert abs(found["HbA1c"]["value"] - 5.4) < 1e-6
    assert found["HbA1c"]["ref_high"] == 5.7
    assert "LDL Cholesterol" in found
    assert found["LDL Cholesterol"]["value"] == 110.0
    assert found["LDL Cholesterol"]["ref_low"] == 0.0
    assert found["LDL Cholesterol"]["ref_high"] == 100.0
    assert "Total Cholesterol" in found
    assert "Vitamin D" in found
    assert "TSH" in found
    # Confidence should be high where unit and range were both recovered.
    assert found["HbA1c"]["confidence"] >= 0.8


def test_labs_panel_date_detected():
    assert labs_import.find_panel_date(SAMPLE_LAB_TEXT) == "2026-06-18"


def test_labs_parse_image_needs_ocr(tmp_path):
    img = tmp_path / "panel.png"
    img.write_bytes(b"\x89PNG\r\n")
    out = labs_import.parse_labs_file(str(img))
    assert out.get("needs_ocr") is True

    # With an injected OCR callable it should extract instead.
    out2 = labs_import.parse_labs_file(str(img), ocr_fn=lambda p: SAMPLE_LAB_TEXT)
    assert out2["panel_date"] == "2026-06-18"
    assert any(c["biomarker"] == "HbA1c" for c in out2["candidates"])


def test_labs_confirm_and_store_inserts(conn):
    before = db.fetchall(conn, "SELECT COUNT(*) FROM labs")[0][0]
    res = labs_import.confirm_and_store(conn, "2026-05-10", [
        {"biomarker": "Ferritin", "value": 120.0, "unit": "ng/mL",
         "ref_low": 30.0, "ref_high": 400.0},
        {"biomarker": "Vitamin B12", "value": 550.0, "unit": "pg/mL",
         "ref_low": 200.0, "ref_high": 900.0},
    ])
    assert res["stored"] == 2
    after = db.fetchall(conn, "SELECT COUNT(*) FROM labs")[0][0]
    assert after == before + 2
    row = db.fetchdicts(conn,
        "SELECT biomarker, value, unit FROM labs WHERE biomarker = 'Ferritin'")
    assert row and row[0]["value"] == 120.0
