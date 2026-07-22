"""Host-side validation: the model may only speak numbers that exist in its
input, and may never drift into diagnosis or dosing."""

from __future__ import annotations

import json
import re

BLOCKLIST = re.compile(
    r"\b(diagnos\w*|prescri\w*|dosage|dose of|mg of|take \d+ ?mg|disease|disorder|"
    r"syndrome|you (have|suffer)|medical emergency)\b", re.IGNORECASE)

_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _variants(x: float) -> set[str]:
    out = {f"{x:g}", f"{x:.0f}", f"{x:.1f}", f"{x:.2f}"}
    if x >= 1000:
        out.add(f"{x:,.0f}")
    return out


def allowed_numbers(payload) -> set[str]:
    """Every numeric literal reachable in the payload, in common formats."""
    found: set[str] = set()

    def walk(v):
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            found.update(_variants(float(v)))
        elif isinstance(v, str):
            for m in _NUM.finditer(v):
                try:
                    found.update(_variants(float(m.group().replace(",", ""))))
                except ValueError:
                    pass
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    walk(json.loads(json.dumps(payload, default=str)))
    return found


def validate_text(text: str, payload) -> list[str]:
    """Empty list = valid."""
    errors: list[str] = []
    allowed = allowed_numbers(payload)
    for m in _NUM.finditer(text):
        token = m.group().replace(",", "")
        # tolerate times like 07:30 and small counting words (1, 2, 3)
        if float(token) <= 3 or text[max(0, m.start() - 1):m.start()] == ":" or text[m.end():m.end() + 1] == ":":
            continue
        try:
            if not (_variants(float(token)) & allowed):
                errors.append(f"number {m.group()} not present in input data")
        except ValueError:
            pass
    if BLOCKLIST.search(text):
        errors.append(f"blocked vocabulary: {BLOCKLIST.search(text).group()}")
    return errors
