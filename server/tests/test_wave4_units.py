"""Wave 4 (2026-10-18, owner request): one unit per lab test.

A report in SI units (or a lab's own spelling or scale) is converted into each
test's home unit, value and reference interval together, so two reports from
two labs line up. Synthetic values only.
"""
from __future__ import annotations

import pytest

from heliosd.insights import lab_units as U
from heliosd.insights import labs_import as L


@pytest.mark.parametrize("biomarker,value,unit,expect,home", [
    ("Fasting Glucose", 5.0, "mmol/L", 90.08, "mg/dL"),
    ("Total Cholesterol", 5.2, "mmol/L", 201.1, "mg/dL"),
    ("LDL Cholesterol", 3.0, "mmol/l", 116.0, "mg/dL"),
    ("HDL Cholesterol", 1.2, "mmol/L", 46.4, "mg/dL"),
    ("Triglycerides", 1.7, "mmol/L", 150.6, "mg/dL"),
    ("HbA1c", 36.0, "mmol/mol", 5.445, "%"),
    ("Creatinine", 88.0, "µmol/L", 0.9952, "mg/dL"),
    ("Uric Acid", 400.0, "umol/L", 6.725, "mg/dL"),
    ("Vitamin D", 75.0, "nmol/L", 30.05, "ng/mL"),
    ("Vitamin B12", 300.0, "pmol/L", 406.5, "pg/mL"),
    ("Hemoglobin", 145.0, "g/L", 14.5, "g/dL"),
    ("WBC", 6.4, "x10^9/L", 6400.0, "/cumm"),
    ("WBC", 6.4, "10^3/uL", 6400.0, "/cumm"),
    ("WBC", 6.4, "K/uL", 6400.0, "/cumm"),
    ("Platelets", 2.5, "lakhs/cumm", 250.0, "10^3/uL"),
    ("Platelets", 250000.0, "/cumm", 250.0, "10^3/uL"),
    ("Platelets", 2.5, "lacs/cumm", 250.0, "10^3/uL"),
    ("WBC", 6.4, "×10^3/uL", 6400.0, "/cumm"),
    ("RBC", 4800000.0, "cells/cmm", 4.8, "10^6/uL"),
    ("Free T4", 12.0, "ng/L", 1.2, "ng/dL"),
    ("Total Testosterone", 17.3, "nmol/L", 498.9, "ng/dL"),
    ("Free Testosterone", 60.0, "pmol/L", 17.3, "pg/mL"),
    ("Total T4", 100.0, "nmol/L", 7.77, "ug/dL"),
    ("Free T4", 15.0, "pmol/L", 1.166, "ng/dL"),
    ("Total T3", 1.5, "nmol/L", 0.9765, "ng/mL"),
    ("Total T3", 120.0, "ng/dL", 1.2, "ng/mL"),
    ("Cortisol", 400.0, "nmol/L", 14.5, "ug/dL"),
    ("Total Bilirubin", 17.1, "umol/L", 1.0, "mg/dL"),
    ("Calcium", 2.3, "mmol/L", 9.218, "mg/dL"),
    ("Magnesium", 0.85, "mmol/L", 2.066, "mg/dL"),
    ("Iron", 20.0, "µmol/L", 111.7, "ug/dL"),
    ("CRP", 0.5, "mg/dL", 5.0, "mg/L"),
    ("Urea", 5.0, "mmol/L", 30.03, "mg/dL"),
    ("BUN", 5.0, "mmol/L", 14.0, "mg/dL"),
])
def test_known_units_convert_to_the_home_unit(biomarker, value, unit, expect, home):
    out = U.to_home(biomarker, value, unit)
    assert out["status"] == "converted", out
    assert out["unit"] == home
    assert out["value"] == pytest.approx(expect, rel=2e-3), out
    assert out["original_value"] == value and out["original_unit"] == unit


@pytest.mark.parametrize("biomarker,unit", [
    ("Fasting Glucose", "mg/dl"), ("Hemoglobin", "gm/dL"), ("WBC", "/cumm"), ("RBC", "10^6/uL"),
    ("Platelets", "10^3/ul"), ("TSH", "uIU/mL"), ("TSH", "mIU/L"), ("TSH", "µIU/mL"),
    ("Vitamin B12", "pg/ml"), ("ESR", "mm/1st"), ("MCH", "Picogram"), ("eGFR", "mL/min/1.73"),
    ("Sodium", "mEq/L"), ("ALT", "IU/L"), ("Total T4", "mcg/dL"),
    ("Platelets", "10^9/L"), ("RBC", "10^12/L"), ("WBC", "cells/uL"),
    ("WBC", "cells/cmm"), ("RBC", "millions/cumm"), ("TSH", "mU/L"), ("CRP", "ug/mL"),
    ("MCHC", "%"), ("eGFR", "mL/min/1.73m²"), ("WBC", "/mm³"),
])
def test_the_home_unit_in_any_spelling_leaves_the_value_alone(biomarker, unit):
    out = U.to_home(biomarker, 4.2, unit, 1.0, 9.0)
    assert out["status"] == "home", out
    assert (out["value"], out["ref_low"], out["ref_high"]) == (4.2, 1.0, 9.0)
    assert out["unit"] == U.home_unit(biomarker)


def test_the_reference_interval_converts_with_the_value():
    out = U.to_home("Fasting Glucose", 5.0, "mmol/L", 3.9, 5.5)
    assert out["ref_low"] == pytest.approx(70.26, rel=1e-3)
    assert out["ref_high"] == pytest.approx(99.09, rel=1e-3)


def test_an_unknown_unit_is_never_guessed():
    out = U.to_home("Fasting Glucose", 5.0, "mg/cup")
    assert out["status"] == "unknown" and out["value"] == 5.0 and out["unit"] == "mg/cup"
    assert U.to_home("Fasting Glucose", 5.0, None)["status"] == "none"
    assert U.to_home("SOMETHING NEW", 5.0, "mg/dL")["status"] == "untracked"
    # Raw clearance is not body-size adjusted eGFR; milli-units are not micro-units.
    assert U.to_home("eGFR", 90.0, "mL/min")["status"] == "unknown"
    assert U.to_home("TSH", 2.0, "mIU/mL")["status"] == "unknown"


def test_every_home_unit_maps_to_itself():
    for biomarker, (display, table) in U.UNITS.items():
        conv = table.get(U.unit_key(display))
        assert conv == 1.0, (biomarker, display)


SI_REPORT = """
Sample collected: 2026-03-01 08:00
Glucose fasting 5.1 mmol/L 3.9 - 5.5
Cholesterol total 5.4 mmol/L < 5.2
HbA1c 38 mmol/mol 20 - 42
Haemoglobin 148 g/L 130 - 170
White blood cells 6.1 10^9/L 4.0 - 10.0
Platelets 260 10^9/L 150 - 410
Vitamin D 62 nmol/L 75 - 250
Creatinine 81 umol/L 62 - 106
Sodium 140 mmol/L 135 - 145
Testosterone 18.0 nmol/L 8.6 - 29
Ferritin 5.0 mg/cup 1 - 9
"""


def test_an_si_report_lands_on_the_same_scale_as_the_home_report():
    rows = {c["biomarker"]: c for c in L.extract_biomarkers(SI_REPORT)}
    g = rows["Fasting Glucose"]
    assert g["unit"] == "mg/dL" and g["value"] == pytest.approx(91.88, rel=1e-3)
    assert g["original_value"] == 5.1 and g["original_unit"] == "mmol/L" and g["unit_status"] == "converted"
    assert g["selected"] is True
    assert rows["Total Cholesterol"]["ref_high"] == pytest.approx(201.1, rel=1e-3)
    assert rows["HbA1c"]["unit"] == "%" and rows["HbA1c"]["value"] == pytest.approx(5.628, rel=1e-3)
    assert rows["Hemoglobin"]["value"] == 14.8 and rows["Hemoglobin"]["unit"] == "g/dL"
    assert rows["WBC"]["value"] == 6100.0 and rows["WBC"]["unit"] == "/cumm"
    assert rows["Platelets"]["value"] == 260.0 and rows["Platelets"]["unit"] == "10^3/uL"
    assert rows["Vitamin D"]["value"] == pytest.approx(24.84, rel=1e-3)
    assert rows["Creatinine"]["value"] == pytest.approx(0.9161, rel=1e-3)
    assert rows["Sodium"]["unit_status"] == "home" and rows["Sodium"]["value"] == 140.0
    assert rows["Total Testosterone"]["value"] == pytest.approx(519.1, rel=1e-3)


def test_a_row_with_an_unknown_unit_is_listed_unticked():
    rows = [c for c in L.extract_biomarkers(SI_REPORT) if c["unit"] == "mg/cup"]
    assert len(rows) == 1 and rows[0]["unit_status"] == "unknown" and rows[0]["selected"] is False
