"""Assisted, fully local labs import from PDF and image panels.

The owner's lab reports arrive as PDF exports and as JPEG or PNG screenshots.
This module extracts candidate biomarker rows so the caller can confirm each one
before anything is written. Nothing here calls the cloud.

  - PDF text is pulled with pdfplumber (optional 'labs' dependency group).
  - Image OCR is pluggable. The caller injects ocr_fn(path) -> str, which on the
    Mac is wired to Apple Vision or a local VLM. With no ocr_fn on an image we
    return {"needs_ocr": True} rather than guessing.

Wave 4 (2026-10-18): the extractor reads result ROWS, not keywords. A lab
report lists each test as one line: the printed test name, the result, an
optional H or L flag, the unit, the reference interval and often the method.
Every such line becomes a candidate under the report's own test name; a name
the alias table knows is filed under its canonical name, so a biomarker can be
tracked across labs. The old extractor knew 20 names and took the first number
after a known word anywhere on a line: on a 25-page panel it found 14 values
right, filed 15 under the wrong name (VLDL as LDL, a ratio as HDL, the "25" of
"Vitamin D 25 Hydroxy", SHBG as hemoglobin) and missed about 85. Calculated
rows (ratios, estimates) are marked and left unticked, and so is any row read
with low confidence. Every candidate carries a confidence so the confirming UI
can sort and highlight the shaky ones. Confirmed rows are hashed to a stable
lab_id and inserted into the labs table.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path

from heliosd.insights import lab_units
from heliosd.store import db

# Canonical biomarkers with a plausible range, used to sanity-check a value.
# The first twenty names are the ones stored before Wave 4; they keep their
# spelling so earlier rows and later rows line up.
_BIOMARKERS: list[dict] = [
    {"name": "HbA1c", "lo": 3.0, "hi": 20.0},
    {"name": "Fasting Glucose", "lo": 40.0, "hi": 500.0},
    {"name": "Total Cholesterol", "lo": 50.0, "hi": 500.0},
    {"name": "LDL Cholesterol", "lo": 20.0, "hi": 400.0},
    {"name": "HDL Cholesterol", "lo": 10.0, "hi": 150.0},
    {"name": "Triglycerides", "lo": 20.0, "hi": 2000.0},
    {"name": "Hemoglobin", "lo": 5.0, "hi": 25.0},
    {"name": "Hematocrit", "lo": 15.0, "hi": 65.0},
    {"name": "WBC", "lo": 500.0, "hi": 100000.0},           # home unit /cumm
    {"name": "RBC", "lo": 2.0, "hi": 8.0},
    {"name": "Platelets", "lo": 10.0, "hi": 2000.0},        # home unit 10^3/uL
    {"name": "Vitamin D", "lo": 5.0, "hi": 150.0},
    {"name": "Vitamin B12", "lo": 50.0, "hi": 2000.0},
    {"name": "Ferritin", "lo": 1.0, "hi": 2000.0},
    {"name": "TSH", "lo": 0.01, "hi": 50.0},
    {"name": "Free T3", "lo": 0.5, "hi": 12.0},
    {"name": "Free T4", "lo": 0.2, "hi": 4.0},
    {"name": "Total Testosterone", "lo": 20.0, "hi": 1500.0},
    {"name": "Cortisol", "lo": 1.0, "hi": 60.0},
    {"name": "CRP", "lo": 0.0, "hi": 300.0},
    {"name": "hs-CRP", "lo": 0.0, "hi": 50.0},
    {"name": "VLDL Cholesterol", "lo": 1.0, "hi": 200.0},
    {"name": "Non-HDL Cholesterol", "lo": 20.0, "hi": 500.0},
    {"name": "Total T3", "lo": 0.2, "hi": 5.0},
    {"name": "Total T4", "lo": 1.0, "hi": 25.0},
    {"name": "Free Testosterone", "lo": 0.5, "hi": 100.0},
    {"name": "SHBG", "lo": 1.0, "hi": 200.0},
    {"name": "ESR", "lo": 0.0, "hi": 150.0},
    {"name": "Creatinine", "lo": 0.2, "hi": 15.0},
    {"name": "Uric Acid", "lo": 1.0, "hi": 15.0},
    {"name": "ALT", "lo": 1.0, "hi": 2000.0},
    {"name": "AST", "lo": 1.0, "hi": 2000.0},
    {"name": "ALP", "lo": 5.0, "hi": 2000.0},
    {"name": "GGT", "lo": 1.0, "hi": 2000.0},
    {"name": "Total Bilirubin", "lo": 0.0, "hi": 30.0},
    {"name": "Direct Bilirubin", "lo": 0.0, "hi": 20.0},
    {"name": "Albumin", "lo": 1.0, "hi": 7.0},
    {"name": "Total Protein", "lo": 2.0, "hi": 12.0},
    {"name": "Sodium", "lo": 100.0, "hi": 180.0},
    {"name": "Potassium", "lo": 1.5, "hi": 9.0},
    {"name": "Magnesium", "lo": 0.5, "hi": 5.0},
    {"name": "Iron", "lo": 5.0, "hi": 500.0},
    {"name": "PSA", "lo": 0.0, "hi": 100.0},
    {"name": "Insulin", "lo": 0.5, "hi": 300.0},
    {"name": "eGFR", "lo": 5.0, "hi": 200.0},
    {"name": "Urea", "lo": 5.0, "hi": 300.0},
    {"name": "BUN", "lo": 2.0, "hi": 150.0},
    {"name": "Phosphorus", "lo": 1.0, "hi": 10.0},
    {"name": "Chloride", "lo": 70.0, "hi": 130.0},
    {"name": "Calcium", "lo": 4.0, "hi": 16.0},
    {"name": "Ionized Calcium", "lo": 2.0, "hi": 8.0},
    {"name": "LDH", "lo": 50.0, "hi": 3000.0},
    {"name": "TIBC", "lo": 100.0, "hi": 700.0},
    {"name": "UIBC", "lo": 20.0, "hi": 600.0},
    {"name": "Globulin", "lo": 1.0, "hi": 6.0},
    {"name": "Indirect Bilirubin", "lo": 0.0, "hi": 20.0},
    {"name": "Transferrin", "lo": 50.0, "hi": 600.0},
    {"name": "MCV", "lo": 50.0, "hi": 130.0},
    {"name": "MCH", "lo": 15.0, "hi": 45.0},
    {"name": "MCHC", "lo": 25.0, "hi": 40.0},
]
_PLAUSIBLE = {b["name"]: (b["lo"], b["hi"]) for b in _BIOMARKERS}

# Normalized printed name -> canonical name. Matched on the WHOLE normalized
# name (never a substring), so "Estimated Average Glucose" is not "Fasting
# Glucose" and "Cholesterol/HDL Ratio" is not "HDL Cholesterol".
_ALIASES: dict[str, str] = {}


def _alias(canonical: str, *names: str) -> None:
    for n in names:
        _ALIASES[n] = canonical


_alias("HbA1c", "hba1c", "hba1c glycosylated hemoglobin", "hba1c glycated hemoglobin",
       "glycosylated hemoglobin", "glycated hemoglobin", "hemoglobin a1c", "haemoglobin a1c", "a1c")
_alias("Fasting Glucose", "fasting glucose", "glucose fasting", "fasting blood glucose",
       "blood glucose fasting", "fasting plasma glucose", "plasma glucose fasting", "fbs", "glucose")
_alias("Total Cholesterol", "total cholesterol", "cholesterol total", "cholesterol", "serum cholesterol")
_alias("LDL Cholesterol", "ldl", "ldl cholesterol", "ldl c", "low density lipoprotein",
       "ldl cholesterol direct", "ldl direct", "direct ldl", "low density lipoprotein cholesterol")
_alias("HDL Cholesterol", "hdl", "hdl cholesterol", "hdl c", "high density lipoprotein",
       "hdl direct", "hdl cholesterol direct", "high density lipoprotein cholesterol")
_alias("VLDL Cholesterol", "vldl", "vldl cholesterol", "very low density lipoprotein")
_alias("Non-HDL Cholesterol", "non hdl cholesterol", "non hdl")
_alias("Triglycerides", "triglycerides", "triglyceride", "tg", "serum triglycerides")
_alias("Hemoglobin", "hemoglobin", "haemoglobin", "hemoglobin hb", "haemoglobin hb", "hb", "hgb")
_alias("Hematocrit", "hematocrit", "haematocrit", "hct", "pcv", "haematocrit pcv hct",
       "hematocrit pcv hct", "packed cell volume")
_alias("WBC", "wbc", "wbc count", "white blood cell", "white blood cells", "white blood cell count",
       "total leucocyte count", "total leukocyte count", "total leucocyte count tlc",
       "total leukocyte count tlc", "tlc")
_alias("RBC", "rbc", "rbc count", "red blood cell", "red blood cells", "red blood cell rbc",
       "red blood cell count", "total rbc count")
_alias("Platelets", "platelets", "platelet count", "plt", "platelet")
_alias("Vitamin D", "vitamin d", "vit d", "vitamin d 25 hydroxy", "vitamin d 25 hydroxy total",
       "25 hydroxy vitamin d", "25 oh vitamin d", "vitamin d 25 oh", "vitamin d total",
       "25 hydroxyvitamin d", "vitamin d3")
_alias("Vitamin B12", "vitamin b12", "serum vitamin b12", "b12", "cobalamin", "vitamin b 12")
_alias("Ferritin", "ferritin", "serum ferritin")
_alias("TSH", "tsh", "thyroid stimulating hormone", "tsh ultrasensitive", "ultrasensitive tsh")
_alias("Free T3", "free t3", "ft3", "free triiodothyronine")
_alias("Free T4", "free t4", "ft4", "free thyroxine")
_alias("Total T3", "total t3", "t3", "total tri iodothyronine t3", "total triiodothyronine t3",
       "total triiodothyronine")
_alias("Total T4", "total t4", "t4", "total thyroxine t4", "total thyroxine")
_alias("Total Testosterone", "total testosterone", "testosterone total", "testosterone",
       "testosterone total serum", "testesterone total serum", "testesterone total")
_alias("Free Testosterone", "free testosterone", "testosterone free", "testosterone free serum",
       "testesterone free serum", "testesterone free")
_alias("SHBG", "shbg", "sex hormone binding globulin", "sex hormone binding globulin shbg")
_alias("Cortisol", "cortisol", "serum cortisol", "cortisol morning")
_alias("CRP", "crp", "crp quantitative", "c reactive protein", "c reactive protein crp")
_alias("hs-CRP", "hs crp", "hscrp", "high sensitivity crp", "hs c reactive protein")
_alias("ESR", "esr", "erythrocyte sedimentation rate")
_alias("Creatinine", "creatinine", "serum creatinine")
_alias("Uric Acid", "uric acid", "serum uric acid")
_alias("ALT", "alt", "sgpt", "sgpt alt", "alt sgpt", "alanine aminotransferase")
_alias("AST", "ast", "sgot", "sgot ast", "ast sgot", "aspartate aminotransferase")
_alias("ALP", "alp", "alkaline phosphatase", "alkaline phosphatase alp")
_alias("GGT", "ggt", "gamma gt", "gamma glutamyl transferase", "ggtp")
_alias("Total Bilirubin", "bilirubin total", "total bilirubin")
_alias("Direct Bilirubin", "bilirubin direct", "direct bilirubin")
_alias("Albumin", "albumin", "serum albumin")
_alias("Total Protein", "protein total", "total protein")
_alias("Sodium", "sodium", "sodium na", "na")
_alias("Potassium", "potassium", "potassium k", "k")
_alias("Magnesium", "magnesium", "serum magnesium")
_alias("Iron", "iron", "iron fe", "serum iron")
_alias("PSA", "psa", "psa total", "serum psa total", "total psa")
_alias("Insulin", "insulin", "fasting insulin", "insulin fasting")
_alias("Urea", "urea", "blood urea", "serum urea")
_alias("BUN", "bun", "blood urea nitrogen", "blood urea nitrogen bun", "urea nitrogen")
_alias("Phosphorus", "phosphorus", "inorganic phosphorus", "phosphate", "serum phosphorus")
_alias("Chloride", "chloride", "chloride cl", "cl")
_alias("Calcium", "calcium", "total calcium", "total calcium ca", "serum calcium", "calcium total")
_alias("Ionized Calcium", "ionized calcium", "ionised calcium", "calcium ionized", "ca ionized")
_alias("LDH", "ldh", "lactate dehydrogenase", "ld")
_alias("TIBC", "tibc", "total iron binding capacity")
_alias("UIBC", "uibc", "unsaturated iron binding capacity")
_alias("Globulin", "globulin", "serum globulin")
_alias("Indirect Bilirubin", "bilirubin indirect", "indirect bilirubin")
_alias("Transferrin", "transferrin")
_alias("MCV", "mcv", "mean corpuscular volume")
_alias("MCH", "mch", "mean corpuscular hemoglobin")
_alias("MCHC", "mchc", "mean corpuscular hemoglobin concentration")

_FLAGS = {"h", "l", "high", "low", "hh", "ll", "*", "(h)", "(l)", "↑", "↓"}
_UNIT_WORDS = {"ratio", "fl", "picogram", "pg", "ml", "mg", "g", "iu", "u", "sec", "seconds",
               "mmol", "meq", "ng", "ug", "µg", "mm", "cells", "cumm", "kg", "cm", "mmhg",
               "pmol", "nmol", "units", "copies"}
_NUM_TOKEN = re.compile(r"^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?$|^-?\d+(?:\.\d+)?$")
_NUM_PCT = re.compile(r"^(-?\d+(?:\.\d+)?)%$")
_RANGE_TOKEN = re.compile(r"^\d+(?:\.\d+)?-\d+(?:\.\d+)?$")
_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)")
_LT_RE = re.compile(r"(?:[<≤]|\bup\s*to\b|\bupto\b|\bless than\b)\s*=?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_GT_RE = re.compile(r"(?:[>≥]|\bmore than\b|\bgreater than\b)\s*=?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_CALCULATED = re.compile(r"\bcalculated\b|\bcalc\b|\bderived\b", re.IGNORECASE)
_RATIO_NAME = re.compile(r"\bratio\b|\bestimated\b", re.IGNORECASE)
_MAX_NAME_WORDS = 8
# "recommended.The": a sentence run into the next. Abbreviations ("S. Creatinine",
# "Vit. D", "T. Bilirubin") are names, not prose.
_SENTENCE = re.compile(r"[a-z]{3,}\.[A-Z]")
_TRAILING_STOPWORDS = {"to", "of", "and", "or", "the", "is", "are", "than", "in", "at", "a", "an",
                       "with", "for", "be", "was", "were", "by"}

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DATE_PATTERNS = [
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), "ymd"),
    (re.compile(r"\b(\d{1,2})[/\- ]([A-Za-z]{3})[A-Za-z]*[/\-, ]+(\d{4})\b"), "d_mon_y"),
    (re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b"), "dmy"),
    (re.compile(r"\b(\d{2})-(\d{2})-(\d{4})\b"), "dmy"),
]


def _parse_date(text: str) -> str | None:
    """The first plausible date in the text: 2000 or later, never in the future."""
    today = date.today()
    for rx, kind in _DATE_PATTERNS:
        for m in rx.finditer(text):
            try:
                if kind == "ymd":
                    d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                elif kind == "d_mon_y":
                    mon = _MONTHS.get(m.group(2)[:3].lower())
                    if mon is None:
                        continue
                    d = date(int(m.group(3)), mon, int(m.group(1)))
                else:
                    d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                if d.year < 2000 or d > today:
                    continue
                return d.isoformat()
            except ValueError:
                continue
    return None


def find_panel_date(text: str) -> str | None:
    """Best effort collection date from the text, else any report date. ISO
    string or None. A date after a "collected" or "collection" label wins, so
    the sample date is used rather than the print or report date."""
    for line in text.splitlines():
        low = line.lower()
        i = low.find("collect")
        if i == -1:
            i = low.find("drawn")
        if i != -1:
            found = _parse_date(line[i:])
            if found:
                return found
    # No labelled collection date: any date, but never one from a birth-date line.
    return _parse_date("\n".join(ln for ln in text.splitlines()
                                 if not re.search(r"\bdob\b|birth", ln, re.IGNORECASE)))


def normalize_name(name: str) -> str:
    """Lowercase, punctuation to spaces, one space between words."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", name.lower())).strip()


def canonical_name(printed: str) -> str | None:
    """The canonical biomarker for a printed test name, or None."""
    norm = normalize_name(printed)
    if norm in _ALIASES:
        return _ALIASES[norm]
    # "S. Creatinine", "T. Bilirubin": serum and total abbreviated.
    for prefix, repl in (("s ", ""), ("serum ", ""), ("t ", "total "), ("d ", "direct ")):
        if norm.startswith(prefix):
            alt = repl + norm[len(prefix):]
            if alt in _ALIASES:
                return _ALIASES[alt]
            if repl == "total " and norm[len(prefix):] + " total" in _ALIASES:
                return _ALIASES[norm[len(prefix):] + " total"]
            if repl == "direct " and norm[len(prefix):] + " direct" in _ALIASES:
                return _ALIASES[norm[len(prefix):] + " direct"]
    for suffix in (" serum", " plasma", " whole blood", " blood"):
        if norm.endswith(suffix) and norm[: -len(suffix)] in _ALIASES:
            return _ALIASES[norm[: -len(suffix)]]
    if norm.startswith("egfr"):
        return "eGFR"
    return None


def _is_unit(tok: str) -> bool:
    low = tok.lower().strip(".,;")
    if not low or _NUM_TOKEN.match(low):
        return False
    if any(ch in low for ch in "/%^µ"):
        return True
    return low in _UNIT_WORDS


def _tidy_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip()
    return name.rstrip(" :.*,-").strip()


def _section_of(line: str, current: str) -> str:
    """A specimen header (a line with no result) moves the section: urine, or
    blood. Lines with numbers never move it."""
    low = line.lower()
    if any(_NUM_TOKEN.match(t) for t in line.split()):
        return current
    if "urine" in low and "edta" not in low and "serum" not in low:
        return "urine"
    if any(k in low for k in (" serum", ",serum", "edta", "whole blood", "plasma")):
        return "blood"
    # Any other section header ends a urine section ("THYROID PROFILE").
    # (Not "examination" or "test": a urine page carries "PHYSICAL EXAMINATION"
    # and the "Test Name Result Unit" column header inside its own section.)
    if any(k in low for k in ("profile", "panel", "studies", "function", "screening")):
        return "blood"
    return current


def parse_row(line: str) -> dict | None:
    """One report line -> one candidate row, or None when the line is not a
    result row (prose, a header, a reference table, a text result)."""
    toks = line.split()
    for i, tok in enumerate(toks):
        pct = _NUM_PCT.match(tok)
        if not (_NUM_TOKEN.match(tok) or pct):
            continue
        raw_name = " ".join(toks[:i])
        name = _tidy_name(raw_name)
        if not name:
            continue                           # "25 OH Vitamin D 27.3": the name has a number in front
        if name[0].isdigit() and ":" not in name and canonical_name(name) is None:
            continue                           # a digit-led name counts only when the table knows it
        if (not (name[0].isalnum() or name[0] == "%") or sum(c.isalpha() for c in name) < 2
                or ":" in name or len(name.split()) > _MAX_NAME_WORDS):
            # A name must lead the line and read as a test name. The first
            # number on a prose or reference line ends the search.
            return None
        words = name.lower().split()
        if _SENTENCE.search(name) or words[-1] in _TRAILING_STOPWORDS:
            # "recommended.The total PSA range of 4.0 to 10.0": prose, not a row.
            return None
        if raw_name.rstrip().endswith(":") and canonical_name(name) is None:
            # "Age/Gender : 40", "Barcode No : 2001": a header label, not a test.
            return None
        rest = toks[i + 1:]
        j = 0
        flag = None
        if j < len(rest) and rest[j].lower() in _FLAGS:
            flag, j = rest[j].upper(), j + 1
        unit = "%" if pct else None
        nxt = rest[j] if j < len(rest) else None
        if nxt is not None and not pct:
            if nxt in ("-", "–") or nxt.startswith("-"):
                return None                    # "0.68 - 34.5": a reference table row
            if _is_unit(nxt):
                unit, j = nxt, j + 1
                # "10^3 / uL" spreads one unit over three tokens.
                while j < len(rest) and (rest[j] == "/" or rest[j].startswith("/") or unit.endswith("/")):
                    unit += rest[j]
                    j += 1
            elif _NUM_TOKEN.match(nxt) and j + 2 < len(rest) and rest[j + 1] in ("-", "–", "to") \
                    and _NUM_TOKEN.match(rest[j + 2]):
                pass                           # "5.50 5.5 - 7.0": no unit, then the range
            elif not (_RANGE_TOKEN.match(nxt) or nxt[0] in "<>≤≥(" or nxt.lower() in ("upto", "up")):
                # "Vitamin D 25 Hydroxy": the number belongs to the name. Keep looking.
                continue
        try:
            value = float((pct.group(1) if pct else tok).replace(",", ""))
        except ValueError:
            return None
        # "4,000-10,000": thousands separators out before any range is read.
        tail = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", " ".join(rest[j:]))
        ref_low = ref_high = None
        # The interval printed first wins ("Desirable <200, Borderline 200-239").
        found = [(m.start(), kind, m) for kind, m in (("range", _RANGE_RE.search(tail)),
                                                      ("lt", _LT_RE.search(tail)),
                                                      ("gt", _GT_RE.search(tail))) if m]
        if found:
            _, kind, m = min(found, key=lambda f: f[0])
            if kind == "range":
                ref_low, ref_high = float(m.group(1)), float(m.group(2))
            elif kind == "lt":
                ref_high = float(m.group(1))
            else:
                ref_low = float(m.group(1))
        if unit is None and ref_low is None and ref_high is None and canonical_name(name) is None:
            # Nothing to confirm the number against ("Page 1 of 25").
            return None
        derived = bool(_RATIO_NAME.search(name))
        calculated = derived or bool(_CALCULATED.search(tail))
        return {"printed_name": name, "value": value, "unit": unit, "flag": flag,
                "ref_low": ref_low, "ref_high": ref_high, "calculated": calculated,
                "derived": derived}
    return None


def extract_biomarkers(text: str) -> list[dict]:
    """Return candidate rows from lab report text.

    Each candidate: biomarker, value, unit, ref_low, ref_high, confidence, plus
    printed_name (the report's own words), calculated, and selected (the
    confirming screen's default tick: a cleanly read, measured row).
    """
    candidates: list[dict] = []
    seen: set = set()
    section = "blood"
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        section = _section_of(line, section)
        row = parse_row(line)
        if row is None:
            continue
        printed = row["printed_name"]
        prefixed = False
        if section == "urine" and "urine" not in printed.lower():
            prefixed = canonical_name(printed) is not None
            printed = "Urine " + printed
        canon = canonical_name(printed)
        name = canon or printed
        key = (name.lower(), row["value"])
        if key in seen:
            continue                         # the same row printed on two pages
        seen.add(key)
        # One unit per test (lab_units): a known test is converted into its
        # home unit, value and interval together, before the owner sees it.
        conv = lab_units.to_home(canon, row["value"], row["unit"], row["ref_low"], row["ref_high"]) \
            if canon else {"value": row["value"], "unit": row["unit"], "ref_low": row["ref_low"],
                           "ref_high": row["ref_high"], "status": "untracked"}
        value = conv["value"]
        confidence = 0.5
        if canon:
            confidence += 0.2
        if conv["unit"] and conv["status"] != "unknown":
            confidence += 0.15
        if conv["ref_low"] is not None or conv["ref_high"] is not None:
            confidence += 0.1
        if canon in _PLAUSIBLE:
            lo, hi = _PLAUSIBLE[canon]
            confidence += 0.05 if lo <= value <= hi else -0.35
        if row["derived"] or prefixed or conv["status"] == "unknown":
            # A ratio or an estimate is arithmetic on other rows; a blood test name
            # under a urine header may be a section the reader missed; a unit the
            # table does not know cannot be put on the common scale. Listed,
            # never pre-ticked.
            confidence = min(confidence, 0.6)
        confidence = round(max(0.05, min(0.95, confidence)), 2)
        cand = {
            "biomarker": name, "value": value, "unit": conv["unit"],
            "ref_low": conv["ref_low"], "ref_high": conv["ref_high"],
            "confidence": confidence, "printed_name": printed,
            "calculated": row["calculated"],
            "selected": (confidence >= 0.75 and not row["derived"] and not prefixed
                         and conv["status"] != "unknown"),
            "flag": row["flag"],
            "unit_status": conv["status"],
        }
        if conv["status"] == "converted":
            cand["original_value"] = conv["original_value"]
            cand["original_unit"] = conv["original_unit"]
        candidates.append(cand)
    return candidates


def _extract_pdf_text(path: str) -> str | None:
    try:
        import pdfplumber  # type: ignore
    except Exception:
        return None
    parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n".join(parts)


_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff"}


def parse_labs_file(path: str, ocr_fn=None) -> dict:
    """Parse a lab report file into candidate rows.

    ocr_fn, if given, is a callable path -> extracted text, used for images. For
    an image with no ocr_fn we return {"needs_ocr": True} so the caller can wire
    up Apple Vision or a local VLM and try again.
    """
    p = Path(path)
    ext = p.suffix.lower()
    text = None
    if ext == ".pdf":
        text = _extract_pdf_text(str(p))
        if text is None:
            return {"error": "pdfplumber not installed, cannot read PDF",
                    "candidates": []}
    elif ext in _IMAGE_EXT:
        if ocr_fn is None:
            return {"needs_ocr": True, "candidates": []}
        text = ocr_fn(str(p))
    elif ext in (".txt", ".text", ""):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            text = None
    else:
        # Unknown type: try ocr_fn if provided, else ask for OCR.
        if ocr_fn is not None:
            text = ocr_fn(str(p))
        else:
            return {"needs_ocr": True, "candidates": []}
    if not text:
        return {"panel_date": None, "candidates": []}
    return {
        "panel_date": find_panel_date(text),
        "candidates": extract_biomarkers(text),
    }


def _lab_id(panel_date, biomarker: str, value) -> str:
    raw = f"{panel_date}|{biomarker}|{value}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def confirm_and_store(conn, panel_date, confirmed_rows: list[dict]) -> dict:
    """Insert user confirmed rows into the labs table.

    panel_date may be a date or ISO string. Each row needs biomarker and value;
    unit and ref range are optional. Returns a small summary with the lab_ids.
    """
    if isinstance(panel_date, str):
        panel_date = date.fromisoformat(panel_date)
    stored = []
    for row in confirmed_rows:
        biomarker = row.get("biomarker")
        value = row.get("value")
        if not biomarker or value is None:
            continue
        lab_id = _lab_id(panel_date, biomarker, value)
        db.execute(conn, """
            INSERT OR REPLACE INTO labs
              (lab_id, panel_date, biomarker, value, unit, ref_low, ref_high, panel_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [lab_id, panel_date, biomarker, float(value), row.get("unit"),
             row.get("ref_low"), row.get("ref_high"),
             row.get("panel_source", "assisted_import")])
        stored.append(lab_id)
    return {"stored": len(stored), "lab_ids": stored, "panel_date": str(panel_date)}
