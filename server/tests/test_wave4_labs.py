"""Wave 4 (2026-10-18): the labs reader reads result rows, not keywords.

The fixture below copies the LAYOUT of a common multi-page lab report (test
name, result, optional H or L flag, unit, reference interval, method; a
patient header on every page; prose and reference tables between rows). Every
name is generic and every value is made up. No real report is in this file.
"""
from __future__ import annotations

from heliosd.insights import labs_import as L

REPORT = """
Patient Name : Mr. TEST USER
Age/Gender : 41 Y/Male Lab Id. : SYN0000001
Refered By : Self Sample Collection On : 03/Feb/2026 07:15 AM
Barcode No : 11112222 Reporting On : 04/Feb/2026 01:40 PM
Lipid Profile , Serum
Test Name Result Unit Bio. Ref. Int. Method
TOTAL CHOLESTEROL 211 H mg/dl Desirable : Upto 200 CHOD-PAP
Borderline : 200 - 239
High : More than 240
TRIGLYCERIDES 140.5 mg/dl Normal :Upto 161 GPO
HDL 47.00 mg/dl 35.3 - 79.5 HDL Direct
VLDL 28.10 mg/dl 2.0-30.0 Calculated
LOW DENSITY LIPOPROTEIN 135.90 H mg/dL Desirable : <100 Calculated
CHOLESTEROL/HDL RATIO 4.49 Ratio 3.0 - 5.0 Calculated
NON- HDL CHOLESTEROL 164.00 H mg/dl < 160 Calculated
Page 1 of 9
DIABETES PROFILE , EDTA/Fluoride/Urine
BLOOD GLUCOSE FASTING 91.20 mg/dL 74 - 100 GOD-POD
HbA1C (Glycosylated Hemoglobin) 5.4 % 4.0 - 6.0 HPLC
ESTIMATED AVERAGE GLUCOSE 108.28 mg/dL 70-137 Calculated
Vitamin Profile , Serum
VITAMIN D 25 HYDROXY (TOTAL) 27.30 L ng/ml 30 - 100 CLIA
SERUM VITAMIN B12 410.00 pg/ml 75 - 807 CLIA
after 5 or 6 years of a diet supplying inadequate amounts.
Testesterone Free ,Serum , Serum
TESTOSTERONE FREE SERUM 15.20 pg/ml 8.80 - 27.0 CLIA
Males 40-59 9.57 - 40.6
TESTESTERONE TOTAL SERUM 512.00 ng/dL CLIA
Adult Male : 241 - 827
Ferritin Serum , Serum
Serum Ferritin 98.10 ng/mL 23.9-336.2 CLIA
Iron deficiency 0.68 - 34.5
generally recommended.The total PSA range of 4.0 to 10.0 ng/ml has been described
Complete Blood Count (CBC) , Whole Blood EDTA
HEMOGLOBIN (HB) 15.10 gm/dL 13.0-17.0 Cyanide Free
RED BLOOD CELL (RBC) 5.02 10^6/uL 4.5-5.5 Impedance
HAEMATOCRIT / PCV / HCT 46.00 % 40-50 Calculated
TOTAL LEUCOCYTE COUNT (TLC) 7,250.00 /cumm 4000-10000 Flowcytometry
ABSOLUTE NEUTROPHIL COUNT 4.10 10^3 / uL 2.0 - 7.0 Flow cytometry
PLATELET COUNT 254 10^3/ul 150-410 Electrical Impedence
HEMOGLOBIN (HB) 15.10 gm/dL 13.0-17.0 Cyanide Free
% SATURATION TRANSFERRIN 31.20 % 16 - 50 Calculated
Sex Hormone Binding Globulin (SHBG), Serum , Serum
SHBG 33.30 nmol/L 13.7 - 48.9 CLIA
Complete Urine Examination , Urine
pH 6.00 5.5 - 7.0
Albumin Urine* 12.00 mg/L 0-20
"""


def _by_name(cands):
    return {c["biomarker"]: c for c in cands}


def test_every_result_row_is_read_under_the_right_name():
    got = _by_name(L.extract_biomarkers(REPORT))
    # Known tests carry their home unit's spelling (lab_units); the values of
    # this home-unit report are unchanged. Unknown tests keep the printed unit.
    want = {
        "Total Cholesterol": (211.0, "mg/dL", None, 200.0),
        "Triglycerides": (140.5, "mg/dL", None, 161.0),
        "HDL Cholesterol": (47.0, "mg/dL", 35.3, 79.5),
        "VLDL Cholesterol": (28.1, "mg/dL", 2.0, 30.0),
        "LDL Cholesterol": (135.9, "mg/dL", None, 100.0),
        "Non-HDL Cholesterol": (164.0, "mg/dL", None, 160.0),
        "Fasting Glucose": (91.2, "mg/dL", 74.0, 100.0),
        "HbA1c": (5.4, "%", 4.0, 6.0),
        "Vitamin D": (27.3, "ng/mL", 30.0, 100.0),
        "Vitamin B12": (410.0, "pg/mL", 75.0, 807.0),
        "Free Testosterone": (15.2, "pg/mL", 8.8, 27.0),
        "Total Testosterone": (512.0, "ng/dL", None, None),
        "Ferritin": (98.1, "ng/mL", 23.9, 336.2),
        "Hemoglobin": (15.1, "g/dL", 13.0, 17.0),
        "RBC": (5.02, "10^6/uL", 4.5, 5.5),
        "Hematocrit": (46.0, "%", 40.0, 50.0),
        "WBC": (7250.0, "/cumm", 4000.0, 10000.0),
        "ABSOLUTE NEUTROPHIL COUNT": (4.1, "10^3/uL", 2.0, 7.0),
        "Platelets": (254.0, "10^3/uL", 150.0, 410.0),
        "% SATURATION TRANSFERRIN": (31.2, "%", 16.0, 50.0),
        "SHBG": (33.3, "nmol/L", 13.7, 48.9),
        "Urine pH": (6.0, None, 5.5, 7.0),
        "Albumin Urine": (12.0, "mg/L", 0.0, 20.0),
    }
    for name, (value, unit, lo, hi) in want.items():
        assert name in got, (name, sorted(got))
        c = got[name]
        assert (c["value"], c["unit"], c["ref_low"], c["ref_high"]) == (value, unit, lo, hi), (name, c)


def test_the_old_mislabels_are_gone():
    got = L.extract_biomarkers(REPORT)
    names = [c["biomarker"] for c in got]
    # The old reader filed a ratio, VLDL and non-HDL under LDL or HDL, took the
    # "25" of the vitamin D test name, and read SHBG as hemoglobin.
    assert names.count("LDL Cholesterol") == 1 and names.count("HDL Cholesterol") == 1
    assert names.count("Hemoglobin") == 1          # the CBC row printed twice is one row
    assert _by_name(got)["Vitamin D"]["value"] == 27.3
    assert "Fasting Glucose" in names and names.count("Fasting Glucose") == 1
    assert "ESTIMATED AVERAGE GLUCOSE" in names       # its own row, not fasting glucose
    assert "CHOLESTEROL/HDL RATIO" in names


def test_headers_prose_and_reference_tables_never_become_rows():
    names = {c["biomarker"].lower() for c in L.extract_biomarkers(REPORT)}
    for junk in ("age/gender", "barcode no", "page", "borderline", "high", "males 40-59",
                 "iron deficiency", "adult male", "after 5 or", "pediatric"):
        assert not any(n.startswith(junk) for n in names), (junk, sorted(names))
    assert not any("generally" in n or "psa range" in n for n in names)


def test_ratios_and_estimates_are_listed_but_not_ticked():
    got = _by_name(L.extract_biomarkers(REPORT))
    for name in ("CHOLESTEROL/HDL RATIO", "ESTIMATED AVERAGE GLUCOSE"):
        assert got[name]["selected"] is False and got[name]["calculated"] is True
    # A measured row read cleanly starts ticked; a lab-calculated one is marked but ticked.
    assert got["Total Cholesterol"]["selected"] is True
    assert got["LDL Cholesterol"]["calculated"] is True and got["LDL Cholesterol"]["selected"] is True
    # No unit: listed, not ticked.
    assert got["Urine pH"]["selected"] is False


def test_the_collection_date_wins_over_the_report_date():
    assert L.find_panel_date(REPORT) == "2026-02-03"
    assert L.find_panel_date("Reported: 2026-05-02\nCollected: 2026-04-30 08:00") == "2026-04-30"
    assert L.find_panel_date("printed 12-Mar-2026") == "2026-03-12"
    assert L.find_panel_date("no date here") is None


def test_canonical_names_match_whole_names_only():
    assert L.canonical_name("ESTIMATED AVERAGE GLUCOSE") is None
    assert L.canonical_name("Sex Hormone Binding Globulin (SHBG)") == "SHBG"
    assert L.canonical_name("Urine Creatinine") is None
    assert L.canonical_name("SERUM CREATININE") == "Creatinine"
    assert L.canonical_name("eGFR (CKD-EPI Creatinine 2009)") == "eGFR"


def test_review_edge_cases():
    rows = _by_name(L.extract_biomarkers("""
TOTAL LEUCOCYTE COUNT (TLC) 6,100 /cumm 4,000-10,000 Flowcytometry
TOTAL CHOLESTEROL 190 mg/dl Desirable <200, Borderline 200-239
S. Creatinine 0.9 mg/dL 0.7 - 1.3
25 OH Vitamin D 31.0 ng/mL 30 - 100
Omega 3 Index 8.2 % 8 - 12
Complete Urine Examination , Urine
PHYSICAL EXAMINATION
Test Name Result Unit Bio. Ref. Int. Method
pH 6.00 5.5 - 7.0
THYROID PROFILE
TSH 2.2 uIU/mL 0.35 - 5.50 CLIA
"""))
    assert (rows["WBC"]["ref_low"], rows["WBC"]["ref_high"]) == (4000.0, 10000.0)
    assert (rows["Total Cholesterol"]["ref_low"], rows["Total Cholesterol"]["ref_high"]) == (None, 200.0)
    assert rows["Creatinine"]["value"] == 0.9
    assert rows["Vitamin D"]["value"] == 31.0
    assert rows["Omega 3 Index"]["value"] == 8.2 and rows["Omega 3 Index"]["unit"] == "%"
    assert "Urine pH" in rows                      # the urine page's own subheaders keep the section
    assert "TSH" in rows and "Urine TSH" not in rows


def test_a_birth_date_or_a_future_date_is_never_the_panel_date():
    assert L.find_panel_date("DOB 12/05/1985\nReport 2026-01-04") == "2026-01-04"
    assert L.find_panel_date("Next review 2099-01-01") is None
