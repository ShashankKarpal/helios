"""One unit per lab test, so every report lines up with every other.

Wave 4 (2026-10-18, owner request). Labs print the same test in different
unit systems: US conventional (mg/dL), SI (mmol/L, umol/L), and lab-specific
spellings or scales (/cumm against 10^3/uL, g/L against g/dL, lakhs/cumm).
Each canonical biomarker has ONE home unit, the unit the owner's own reference
panel prints (the first panel imported, 2026-09), and a table of the other
common units with the exact factor into the home unit. The extractor converts
the value and the reference interval into the home unit before the owner
confirms, and the confirm screen shows what the report printed.

A unit the table does not know is never guessed: the row keeps the printed
unit, is marked unit_unknown, and starts unticked.

Factors are the standard clinical conversion factors (molar mass based for
SI units). HbA1c between IFCC (mmol/mol) and NGSP (%) is affine, not a factor:
NGSP = 0.09148 * IFCC + 2.152.
"""
from __future__ import annotations

import re
from typing import Callable

# A converter is either a multiplier into the home unit or a callable.
Converter = float | Callable[[float], float]


def unit_key(unit: str | None) -> str | None:
    """A printed unit reduced to a comparison key: lowercase, no spaces, one
    spelling for micro, cubic millimetre, 10 to the power, and the litre."""
    if not unit:
        return None
    u = unit.strip().lower()
    u = u.replace("μ", "u").replace("µ", "u").replace("mc", "u")
    u = re.sub(r"\s+", "", u)
    u = u.replace("gm/", "g/").replace("gms/", "g/")
    u = u.replace("²", "2")
    u = u.replace("cu.mm", "cumm").replace("mm³", "cumm").replace("mm3", "cumm").replace("mm^3", "cumm")
    u = u.replace("³", "^3")
    u = re.sub(r"/c\.?mm$", "/cumm", u)                  # "cells/cmm"
    u = u.replace("/ul", "/cumm")                       # 1 uL is 1 cubic millimetre
    u = re.sub(r"^[x×]", "", u)                           # "x10^9/L", "×10^3/uL"
    u = u.replace("10e", "10^").replace("10*", "10^").replace("10³", "10^3").replace("10⁶", "10^6")
    u = u.replace("10⁹", "10^9").replace("10¹²", "10^12")
    u = u.replace("thou/", "10^3/").replace("k/", "10^3/")
    u = u.replace("millions/", "10^6/").replace("million/", "10^6/").replace("mill/", "10^6/")
    u = u.replace("mil/", "10^6/")
    u = re.sub(r"la(kh|c)s?/", "lakh/", u)               # "lakhs", "lacs", "lac"
    u = u.rstrip(".")
    return u


# Home unit (as displayed) and the other units, keyed by unit_key, with the
# conversion INTO the home unit. The home unit's own spellings map to 1.0.
_MGDL = ["mg/dl", "mg%"]
_GDL = ["g/dl", "g%"]
_NGML = ["ng/ml", "ug/l"]
_PGML = ["pg/ml", "ng/l"]
_UL = ["u/l", "iu/l", "units/l"]

UNITS: dict[str, tuple[str, dict[str, Converter]]] = {}


def _home(biomarker: str, display: str, same: list[str], **others: Converter) -> None:
    table: dict[str, Converter] = {k: 1.0 for k in same}
    for k, v in others.items():
        table[k.replace("_per_", "/").replace("__", "^")] = v
    UNITS[biomarker] = (display, table)


def _add(biomarker: str, unit: str, conv: Converter) -> None:
    UNITS[biomarker][1][unit] = conv


# Glucose and diabetes
_home("Fasting Glucose", "mg/dL", _MGDL)
_add("Fasting Glucose", "mmol/l", 18.016)
_home("HbA1c", "%", ["%", "%hba1c", "ngsp%"])
_add("HbA1c", "mmol/mol", lambda v: 0.09148 * v + 2.152)
_add("HbA1c", "fraction", 100.0)
_home("Insulin", "uIU/mL", ["uiu/ml", "uu/ml", "miu/l", "mu/l"])
_add("Insulin", "pmol/l", 1 / 6.0)

# Lipids (cholesterol fractions share the cholesterol molar factor)
for _b in ("Total Cholesterol", "LDL Cholesterol", "HDL Cholesterol", "VLDL Cholesterol",
           "Non-HDL Cholesterol"):
    _home(_b, "mg/dL", _MGDL)
    _add(_b, "mmol/l", 38.67)
    _add(_b, "g/l", 100.0)
_home("Triglycerides", "mg/dL", _MGDL)
_add("Triglycerides", "mmol/l", 88.57)
_add("Triglycerides", "g/l", 100.0)

# Blood count
_home("Hemoglobin", "g/dL", _GDL)
_add("Hemoglobin", "g/l", 0.1)
_add("Hemoglobin", "mmol/l", 1.611)
_home("Hematocrit", "%", ["%"])
_add("Hematocrit", "l/l", 100.0)
_add("Hematocrit", "fraction", 100.0)
_home("WBC", "/cumm", ["/cumm", "cells/cumm", "cumm"])
_add("WBC", "10^3/cumm", 1000.0)
_add("WBC", "10^9/l", 1000.0)
_home("RBC", "10^6/uL", ["10^6/cumm", "10^12/l"])
_add("RBC", "/cumm", 1e-6)
_add("RBC", "cells/cumm", 1e-6)
_home("Platelets", "10^3/uL", ["10^3/cumm", "10^9/l"])
_add("Platelets", "lakh/cumm", 100.0)
_add("Platelets", "/cumm", 0.001)
_add("Platelets", "cells/cumm", 0.001)
_home("MCHC", "g/dL", _GDL + ["%"])
_add("MCHC", "g/l", 0.1)
_home("MCV", "fL", ["fl", "um^3", "u^3"])
_home("MCH", "pg", ["pg", "picogram", "pg/cell"])
_home("ESR", "mm/hr", ["mm/hr", "mm/h", "mm/1sthr", "mm/1st", "mm/1sthour", "mm/1sthr."])

# Vitamins, iron
_home("Vitamin D", "ng/mL", _NGML)
_add("Vitamin D", "nmol/l", 1 / 2.496)
_home("Vitamin B12", "pg/mL", _PGML)
_add("Vitamin B12", "pmol/l", 1.355)
_home("Ferritin", "ng/mL", _NGML)
_add("Ferritin", "pmol/l", 1 / 2.247)
for _b in ("Iron", "TIBC", "UIBC"):
    _home(_b, "ug/dL", ["ug/dl", "ug%"])
    _add(_b, "umol/l", 5.585)
_home("Transferrin", "mg/dL", _MGDL)
_add("Transferrin", "g/l", 100.0)

# Thyroid
_home("TSH", "uIU/mL", ["uiu/ml", "miu/l", "uu/ml", "mu/l"])
_home("Total T3", "ng/mL", _NGML)
_add("Total T3", "ng/dl", 0.01)
_add("Total T3", "nmol/l", 0.651)
_home("Total T4", "ug/dL", ["ug/dl", "ug%"])
_add("Total T4", "nmol/l", 1 / 12.87)
_home("Free T3", "pg/mL", _PGML)
_add("Free T3", "pmol/l", 0.651)
_home("Free T4", "ng/dL", ["ng/dl"])
_add("Free T4", "pmol/l", 1 / 12.87)
_add("Free T4", "ng/l", 0.1)
_add("Free T4", "pg/ml", 0.1)

# Hormones
_home("Total Testosterone", "ng/dL", ["ng/dl", "ng%"])
_add("Total Testosterone", "nmol/l", 28.84)
_add("Total Testosterone", "ng/ml", 100.0)
_home("Free Testosterone", "pg/mL", _PGML)
_add("Free Testosterone", "pmol/l", 0.2884)
_add("Free Testosterone", "ng/dl", 10.0)
_home("SHBG", "nmol/L", ["nmol/l"])
_home("Cortisol", "ug/dL", ["ug/dl", "ug%"])
_add("Cortisol", "nmol/l", 1 / 27.59)
_add("Cortisol", "ng/ml", 0.1)
_home("PSA", "ng/mL", _NGML)

# Inflammation
_home("CRP", "mg/L", ["mg/l", "ug/ml"])
_add("CRP", "mg/dl", 10.0)
_home("hs-CRP", "mg/L", ["mg/l", "ug/ml"])
_add("hs-CRP", "mg/dl", 10.0)

# Kidney
_home("Creatinine", "mg/dL", _MGDL)
_add("Creatinine", "umol/l", 1 / 88.42)
_home("Uric Acid", "mg/dL", _MGDL)
_add("Uric Acid", "umol/l", 1 / 59.48)
_add("Uric Acid", "mmol/l", 16.81)
_home("Urea", "mg/dL", _MGDL)
_add("Urea", "mmol/l", 6.006)
_home("BUN", "mg/dL", _MGDL)
_add("BUN", "mmol/l", 2.801)
# Plain mL/min is a raw clearance (not adjusted for body size): not the same unit.
_home("eGFR", "mL/min/1.73m2", ["ml/min/1.73m2", "ml/min/1.73", "ml/min/1.73m^2"])

# Liver
for _b in ("Total Bilirubin", "Direct Bilirubin", "Indirect Bilirubin"):
    _home(_b, "mg/dL", _MGDL)
    _add(_b, "umol/l", 1 / 17.1)
for _b in ("Albumin", "Total Protein", "Globulin"):
    _home(_b, "g/dL", _GDL)
    _add(_b, "g/l", 0.1)
# Some labs print globulin (albumin subtracted from total protein, both g/dL)
# as "mg/dL". The number is g/dL; it is shown as converted so the relabel is visible.
_add("Globulin", "mg/dl", lambda v: v if v < 20 else v / 1000)
for _b in ("ALT", "AST", "ALP", "GGT", "LDH"):
    _home(_b, "U/L", _UL)
    _add(_b, "ukat/l", 60.0)

# Electrolytes and minerals
for _b in ("Sodium", "Potassium", "Chloride"):
    _home(_b, "mmol/L", ["mmol/l", "meq/l"])
for _b in ("Calcium", "Ionized Calcium"):
    _home(_b, "mg/dL", _MGDL)
    _add(_b, "mmol/l", 4.008)
    _add(_b, "meq/l", 2.004)
_home("Magnesium", "mg/dL", _MGDL)
_add("Magnesium", "mmol/l", 2.431)
_add("Magnesium", "meq/l", 1.215)
_home("Phosphorus", "mg/dL", _MGDL)
_add("Phosphorus", "mmol/l", 3.097)


def home_unit(biomarker: str) -> str | None:
    entry = UNITS.get(biomarker)
    return entry[0] if entry else None


def _apply(conv: Converter, v: float) -> float:
    return conv(v) if callable(conv) else v * conv


def _round(v: float) -> float:
    """Four significant figures, enough for any lab value, no float noise."""
    if v == 0:
        return 0.0
    from math import floor, log10
    return round(v, max(0, 3 - int(floor(log10(abs(v))))))


def to_home(biomarker: str, value: float, unit: str | None,
            ref_low: float | None = None, ref_high: float | None = None) -> dict:
    """Convert one row into its biomarker's home unit.

    Returns value, unit, ref_low, ref_high in the home unit, plus status:
      "home"      the printed unit already is the home unit (values unchanged)
      "converted" converted from another known unit (original_* say what was printed)
      "unknown"   a unit the table does not know (values unchanged, unit as printed)
      "none"      the row printed no unit (values unchanged)
      "untracked" the biomarker has no home unit (values unchanged)
    """
    out = {"value": value, "unit": unit, "ref_low": ref_low, "ref_high": ref_high}
    entry = UNITS.get(biomarker)
    if entry is None:
        return out | {"status": "untracked"}
    display, table = entry
    key = unit_key(unit)
    if key is None:
        return out | {"status": "none"}
    conv = table.get(key)
    if conv is None:
        return out | {"status": "unknown"}
    if not callable(conv) and conv == 1.0:
        return out | {"unit": display, "status": "home"}
    return {"value": _round(_apply(conv, value)), "unit": display,
            "ref_low": _round(_apply(conv, ref_low)) if ref_low is not None else None,
            "ref_high": _round(_apply(conv, ref_high)) if ref_high is not None else None,
            "status": "converted", "original_value": value, "original_unit": unit,
            "original_ref_low": ref_low, "original_ref_high": ref_high}
