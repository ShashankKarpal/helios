"""The glance (rank 3, owner yes 2026-10-26): a small summary of today's brief
for the phone's widget and its morning notification.

It is written when today's brief is published (the same hook that records
last_valid_brief.json; the morning step and /api/today both pass through it)
into glance.json beside the store: 0600, atomic, never raises, and rewritten
only when its content changed. GET /api/glance serves it together with the
brief state read at that moment from the status file, and reads only those two
files, never the store or the local model (the rank 2 rule: a busy or broken
store cannot turn into a hang or a silence on the phone).

Unlike the status file it carries values (the four morning numbers), so the
route needs the token, the file is 0600, and nothing here logs a value.
Display text is made here so the phone never formats units: the Today
screen's rules (sleep "7h 12m"; one decimal under 100, a whole number from 100,
no trailing .0; "bpm" for heart rate), and a note for a value the Today screen
labels (another device's, or a running total so far).
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from datetime import date, datetime, timezone
from pathlib import Path

log = logging.getLogger("heliosd")

GLANCE_FILE_NAME = "glance.json"
SCHEMA = 1
# The four core markers in the phone's order, with labels a small widget can hold.
ROWS = (("recovery_score", "Recovery"), ("sleep_duration", "Sleep"),
        ("hrv_rmssd", "HRV"), ("resting_hr", "Resting HR"))
ROW_STATES = ("favorable", "neutral", "flag", "insufficient", "in_progress")
# Words for a value the Today screen labels, so the phone never shows it as final.
ROW_NOTES = {"stand-in": "other device", "in_progress": "so far"}
_LOCK = threading.Lock()


def glance_file_for(status_file) -> Path:
    """glance.json beside the brief status file (both beside the store)."""
    return Path(status_file).with_name(GLANCE_FILE_NAME)


def _half_up(v: float) -> int:
    # The web app rounds with Math.round and toFixed (half up); Python's round() is half even.
    return int(math.floor(v + 0.5))


def _number(v: float) -> str:
    """The Today screen's formatValue(value, 1): a whole number from 100, else
    one decimal, without a trailing .0."""
    if abs(v) >= 100:
        return str(_half_up(v))
    tenths = _half_up(v * 10)
    return str(tenths // 10) if tenths % 10 == 0 else f"{tenths / 10:.1f}"


def display_text(metric: str, value) -> str | None:
    """72%, 7h 12m, 47.5 ms, 58 bpm; None for a missing or unreadable value."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    if metric == "sleep_duration":
        h, m = divmod(_half_up(v * 60), 60)
        return f"{h}h {m}m" if h > 0 else f"{m}m"
    n = _number(v)
    if metric == "recovery_score":
        return f"{n}%"
    if metric == "hrv_rmssd":
        return f"{n} ms"
    if metric == "resting_hr":
        return f"{n} bpm"
    return n


def row_state(sig: dict | None) -> str:
    """waiting (not in yet), stand-in (a device other than the owner), or the
    signal's own state. A stand-in is never judged, as on the Today screen."""
    if not sig or display_text("", sig.get("value")) is None:
        return "waiting"
    if sig.get("fallback") or sig.get("state") == "fallback":
        return "stand-in"
    state = sig.get("state")
    return state if state in ROW_STATES else "unknown"


def build_glance(day: date, signals: list[dict], verdict: str, awaiting: list[str],
                 complete: bool, made_at: datetime, generation: int | None = None) -> dict:
    by_metric = {s.get("metric"): s for s in signals}
    rows = []
    for metric, label in ROWS:
        sig = by_metric.get(metric)
        state = row_state(sig)
        text = "waiting" if state == "waiting" else display_text(metric, sig.get("value"))
        rows.append({"metric": metric, "label": label, "text": text, "state": state,
                     "note": ROW_NOTES.get(state)})
    return {"date": str(day), "made_at": made_at.isoformat(timespec="seconds"),
            "generation": generation, "complete": bool(complete), "verdict": str(verdict or ""),
            "rows": rows, "flags": sum(1 for r in rows if r["state"] == "flag"),
            "awaiting": sorted(awaiting or [])}


def _same(a: dict, b: dict) -> bool:
    keep = lambda d: {k: v for k, v in d.items() if k != "made_at"}  # noqa: E731
    return keep(a) == keep(b)


def record_glance(glance_file, day: date, signals: list[dict], verdict: str, awaiting: list[str],
                  complete: bool, zone=None, now: datetime | None = None, generation: int | None = None) -> bool:
    """Write today's glance. Not written for a publish whose instant is not on
    `day` in the reporting zone (the record_brief rule), not rewritten when only
    made_at would change (/api/today is polled), and never over a newer
    snapshot of the same day: `generation` is the date's derived generation the
    brief read its signals under, and briefs run in parallel worker threads.
    Never raises: a failure is logged without values and the brief is served
    anyway."""
    if glance_file is None:
        return False
    try:
        local = (now or datetime.now(timezone.utc)).astimezone(zone or timezone.utc)
        if local.date() != day:
            return False
        out = build_glance(day, signals, verdict, awaiting, complete, local, generation)
        path = Path(glance_file)
        with _LOCK:
            old = read_glance(path)
            if old is not None and _same(old, out):
                return True
            if (old is not None and old.get("date") == out["date"] and generation is not None
                    and isinstance(old.get("generation"), int) and old["generation"] > generation):
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(out, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        return True
    except Exception as e:  # noqa: BLE001 - the brief is served whatever happens here
        log.warning("brief %s: could not write the glance file (%s)", day, type(e).__name__)
        return False


def read_glance(glance_file) -> dict | None:
    """The cached glance, or None when it is missing or not a glance."""
    try:
        rec = json.loads(Path(glance_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not (isinstance(rec, dict) and isinstance(rec.get("date"), str) and isinstance(rec.get("rows"), list)):
        return None
    return rec


def glance_reply(summary: dict | None, status: dict, zone_name: str, zone=None,
                 now: datetime | None = None) -> dict:
    """The /api/glance body: the brief state as /api/brief-check sees it now
    (`status` is brief_status(..., full=True)), when today's complete brief
    was first ready, and the cached summary (or null)."""
    local = (now or datetime.now(timezone.utc)).astimezone(zone or timezone.utc)
    return {"schema": SCHEMA, "served_at": local.isoformat(timespec="seconds"), "zone": zone_name,
            "due": status.get("due"),
            "brief": {"today": bool(status.get("today")), "overdue": bool(status.get("overdue")),
                      "check": status.get("check"),
                      "ready_at": status.get("first_published_at") if status.get("today") else None},
            "summary": summary}
