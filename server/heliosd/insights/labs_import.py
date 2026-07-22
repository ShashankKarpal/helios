"""Assisted, fully local labs import from PDF and image panels.

The owner's lab reports arrive as PDF exports and as JPEG or PNG screenshots.
This module extracts candidate biomarker rows so the caller can confirm each one
before anything is written. Nothing here calls the cloud.

  - PDF text is pulled with pdfplumber (optional 'labs' dependency group).
  - Image OCR is pluggable. The caller injects ocr_fn(path) -> str, which on the
    Mac is wired to Apple Vision or a local VLM. With no ocr_fn on an image we
    return {"needs_ocr": True} rather than guessing.

The extractor is regex and heuristic based over the recovered text, tuned for
the common panels (CBC, lipids, HbA1c, fasting glucose, vitamin D, B12,
ferritin, thyroid, hormones, inflammation). Every candidate carries a confidence
so the confirming UI can sort and highlight the shaky ones. Confirmed rows are
hashed to a stable lab_id and inserted into the labs table.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from pathlib import Path

from heliosd.store import db

# Each entry: canonical biomarker -> aliases, expected units, plausible range.
# Aliases are matched case insensitively; longer aliases are tried first so that
# "LDL cholesterol" wins over a bare "cholesterol".
_BIOMARKERS: list[dict] = [
    {"name": "HbA1c", "aliases": ["hba1c", "hemoglobin a1c", "glycated hemoglobin", "a1c"],
     "units": ["%", "mmol/mol"], "lo": 3.0, "hi": 20.0},
    {"name": "Fasting Glucose", "aliases": ["fasting glucose", "fasting blood glucose",
     "fasting plasma glucose", "glucose fasting", "glucose"],
     "units": ["mg/dl", "mmol/l"], "lo": 40.0, "hi": 500.0},
    {"name": "Total Cholesterol", "aliases": ["total cholesterol", "cholesterol total",
     "cholesterol, total"], "units": ["mg/dl", "mmol/l"], "lo": 50.0, "hi": 500.0},
    {"name": "LDL Cholesterol", "aliases": ["ldl cholesterol", "ldl-c", "ldl"],
     "units": ["mg/dl", "mmol/l"], "lo": 20.0, "hi": 400.0},
    {"name": "HDL Cholesterol", "aliases": ["hdl cholesterol", "hdl-c", "hdl"],
     "units": ["mg/dl", "mmol/l"], "lo": 10.0, "hi": 150.0},
    {"name": "Triglycerides", "aliases": ["triglycerides", "trigs", "tg"],
     "units": ["mg/dl", "mmol/l"], "lo": 20.0, "hi": 2000.0},
    {"name": "Hemoglobin", "aliases": ["hemoglobin", "haemoglobin", "hgb", "hb"],
     "units": ["g/dl", "g/l"], "lo": 5.0, "hi": 25.0},
    {"name": "Hematocrit", "aliases": ["hematocrit", "haematocrit", "hct"],
     "units": ["%"], "lo": 15.0, "hi": 65.0},
    {"name": "WBC", "aliases": ["white blood cell", "wbc", "leukocytes"],
     "units": ["10^3/ul", "x10^9/l", "/ul"], "lo": 1.0, "hi": 50.0},
    {"name": "RBC", "aliases": ["red blood cell", "rbc", "erythrocytes"],
     "units": ["10^6/ul", "x10^12/l"], "lo": 2.0, "hi": 8.0},
    {"name": "Platelets", "aliases": ["platelet count", "platelets", "plt"],
     "units": ["10^3/ul", "x10^9/l"], "lo": 50.0, "hi": 800.0},
    {"name": "Vitamin D", "aliases": ["vitamin d", "25-hydroxyvitamin d",
     "25-oh vitamin d", "25 oh vitamin d", "vit d"],
     "units": ["ng/ml", "nmol/l"], "lo": 5.0, "hi": 150.0},
    {"name": "Vitamin B12", "aliases": ["vitamin b12", "b12", "cobalamin"],
     "units": ["pg/ml", "pmol/l"], "lo": 100.0, "hi": 2000.0},
    {"name": "Ferritin", "aliases": ["ferritin"], "units": ["ng/ml", "ug/l"],
     "lo": 5.0, "hi": 1000.0},
    {"name": "TSH", "aliases": ["tsh", "thyroid stimulating hormone", "thyrotropin"],
     "units": ["miu/l", "uiu/ml", "µiu/ml"], "lo": 0.05, "hi": 20.0},
    {"name": "Free T3", "aliases": ["free t3", "ft3", "triiodothyronine free"],
     "units": ["pg/ml", "pmol/l"], "lo": 1.0, "hi": 12.0},
    {"name": "Free T4", "aliases": ["free t4", "ft4", "thyroxine free"],
     "units": ["ng/dl", "pmol/l"], "lo": 0.4, "hi": 4.0},
    {"name": "Total Testosterone", "aliases": ["total testosterone",
     "testosterone total", "testosterone"], "units": ["ng/dl", "nmol/l"],
     "lo": 50.0, "hi": 1500.0},
    {"name": "Cortisol", "aliases": ["cortisol"], "units": ["ug/dl", "nmol/l"],
     "lo": 1.0, "hi": 60.0},
    {"name": "CRP", "aliases": ["c-reactive protein", "hs-crp", "crp"],
     "units": ["mg/l", "mg/dl"], "lo": 0.0, "hi": 100.0},
]

# Reference range shapes: "13.5 - 17.5", "13.5 to 17.5", "< 5.7", "> 40".
_RANGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)")
_LT_RE = re.compile(r"[<≤]\s*(\d+(?:\.\d+)?)")
_GT_RE = re.compile(r"[>≥]\s*(\d+(?:\.\d+)?)")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")

_DATE_PATTERNS = [
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), "%Y-%m-%d"),
    (re.compile(r"(\d{2})/(\d{2})/(\d{4})"), "%d/%m/%Y"),
    (re.compile(r"(\d{2})-(\d{2})-(\d{4})"), "%d-%m-%Y"),
]


def find_panel_date(text: str) -> str | None:
    """Best effort collection or report date from the text. ISO string or None."""
    for rx, fmt in _DATE_PATTERNS:
        m = rx.search(text)
        if m:
            try:
                return datetime.strptime(m.group(0), fmt).date().isoformat()
            except ValueError:
                continue
    return None


def _match_unit(segment: str, expected: list[str]) -> str | None:
    low = segment.lower()
    for u in expected:
        if u.lower() in low:
            return u
    return None


def extract_biomarkers(text: str) -> list[dict]:
    """Return candidate rows from free lab text.

    Each candidate: biomarker, value, unit, ref_low, ref_high, confidence.
    """
    candidates: list[dict] = []
    seen: set = set()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in lines:
        low = line.lower()
        for spec in _BIOMARKERS:
            alias = _first_alias(low, spec["aliases"])
            if alias is None:
                continue
            # Look only at the text after the alias so we do not grab a code number.
            tail = line[low.index(alias) + len(alias):]
            nums = _NUM_RE.findall(tail)
            if not nums:
                continue
            value = float(nums[0])

            ref_low = ref_high = None
            after_value = tail[tail.index(nums[0]) + len(nums[0]):]
            rng = _RANGE_RE.search(after_value)
            lt = _LT_RE.search(after_value)
            gt = _GT_RE.search(after_value)
            if rng:
                ref_low, ref_high = float(rng.group(1)), float(rng.group(2))
            elif lt:
                ref_high = float(lt.group(1))
            elif gt:
                ref_low = float(gt.group(1))

            unit = _match_unit(tail, spec["units"])

            key = (spec["name"], value)
            if key in seen:
                continue
            seen.add(key)

            confidence = 0.5
            if unit:
                confidence += 0.2
            if ref_low is not None or ref_high is not None:
                confidence += 0.2
            if spec["lo"] <= value <= spec["hi"]:
                confidence += 0.1
            else:
                confidence -= 0.2
            confidence = round(max(0.05, min(0.95, confidence)), 2)

            candidates.append({
                "biomarker": spec["name"], "value": value, "unit": unit,
                "ref_low": ref_low, "ref_high": ref_high,
                "confidence": confidence,
            })
            break  # one biomarker per line
    return candidates


def _first_alias(low_line: str, aliases: list[str]) -> str | None:
    # Longest aliases first so specific names beat generic substrings.
    for alias in sorted(aliases, key=len, reverse=True):
        if alias in low_line:
            return alias
    return None


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
