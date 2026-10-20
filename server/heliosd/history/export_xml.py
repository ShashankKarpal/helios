"""Streaming reader of an Apple Health export.xml (single-source plan v2, Phase 2).

iter_export() reads the file once with iterparse and yields every top-level
element of HealthData as (ordinal, kind, row) in document order. The ordinal
is the element's index among the top-level elements, from 0, ExportDate and
Me included: the "row" of plan v2's "export file hash plus row". Children
are read before the element is cleared, so memory stays flat on a file of
several gigabytes. A Record inside a Correlation is read with its
correlation; Apple writes each such record at the top level as well.

Nothing is coerced. Every attribute is kept verbatim beside its parsed form:
an instant parses to UTC from the offset the export printed, a value becomes
a float only when it is a finite number. An element, attribute or child the
export format does not declare raises UnknownShape, so a new export version
stops the import instead of losing fields silently.

Identity (ident, 24 hex): the sha256 of an element's own fields with the
instants in UTC and the device string's memory address blanked, so the same
record hashes the same in any export, whatever zone the exporting device was
in. Children never enter it (HRV beat times print as local clock times).
Identical records share an ident; the importer numbers them in document
order, so each one is kept.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ([+-])(\d{2})(\d{2})$")
_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")
_NUM_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_SEP = "\x1f"

SYNC_ID_KEY = "HKMetadataKeySyncIdentifier"
SYNC_VERSION_KEY = "HKMetadataKeySyncVersion"
TZ_KEY = "HKTimeZone"

RECORD_ATTRS = frozenset({"type", "unit", "value", "sourceName", "sourceVersion", "device", "creationDate",
                          "startDate", "endDate"})
RECORD_CHILDREN = frozenset({"MetadataEntry", "HeartRateVariabilityMetadataList"})
CORRELATION_ATTRS = frozenset({"type", "sourceName", "sourceVersion", "device", "creationDate", "startDate", "endDate"})
CORRELATION_CHILDREN = frozenset({"MetadataEntry", "Record"})
WORKOUT_ATTRS = frozenset({"workoutActivityType", "duration", "durationUnit", "totalDistance", "totalDistanceUnit",
                           "totalEnergyBurned", "totalEnergyBurnedUnit", "sourceName", "sourceVersion", "device",
                           "creationDate", "startDate", "endDate"})
WORKOUT_CHILDREN = frozenset({"MetadataEntry", "WorkoutEvent", "WorkoutRoute", "WorkoutStatistics"})
EVENT_ATTRS = frozenset({"type", "date", "duration", "durationUnit"})
STAT_ATTRS = frozenset({"type", "startDate", "endDate", "average", "minimum", "maximum", "sum", "unit"})
ROUTE_ATTRS = frozenset({"sourceName", "sourceVersion", "device", "creationDate", "startDate", "endDate"})
ROUTE_CHILDREN = frozenset({"MetadataEntry", "FileReference"})
SUMMARY_ATTRS = frozenset({"dateComponents", "activeEnergyBurned", "activeEnergyBurnedGoal", "activeEnergyBurnedUnit",
                           "appleMoveTime", "appleMoveTimeGoal", "appleExerciseTime", "appleExerciseTimeGoal",
                           "appleStandHours", "appleStandHoursGoal"})
# Top-level health elements without a table of their own: kept whole in raw_records.
GENERIC_ELEMENTS = frozenset({"VisionPrescription", "Audiogram", "ClinicalRecord"})
DURATION_SECONDS = {"s": 1.0, "sec": 1.0, "min": 60.0, "hr": 3600.0, "h": 3600.0}


class UnknownShape(ValueError):
    """export.xml holds an element, an attribute or a child this reader does not know."""


def parse_ts(s: str | None) -> tuple[datetime | None, int | None]:
    """'2026-05-01 10:00:00 +0400' -> (the instant as a naive UTC datetime, the printed offset in minutes).
    Anything else -> (None, None); the caller keeps the raw string."""
    if not s:
        return None, None
    m = _TS_RE.match(s)
    if not m:
        return None, None
    off = int(m.group(3)) * 60 + int(m.group(4))
    if m.group(2) == "-":
        off = -off
    try:
        wall = datetime.fromisoformat(m.group(1))
    except ValueError:
        return None, None
    return wall - timedelta(minutes=off), off


def number(s: str | None) -> float | None:
    """A finite decimal number, else None (never a bool, never NaN or infinity)."""
    if s is None:
        return None
    t = s.strip()
    if not _NUM_RE.match(t):
        return None
    f = float(t)
    return f if math.isfinite(f) else None


def device_for_ident(s: str | None) -> str:
    """The device string with its memory address blanked (it changes from export to export)."""
    return _ADDR_RE.sub("0x", s) if s else ""


def ident(parts: list[str]) -> str:
    return hashlib.sha256(_SEP.join(parts).encode("utf-8")).hexdigest()[:24]


def _iso(dt: datetime | None, raw: str | None) -> str:
    return dt.isoformat() if dt is not None else (raw or "")


def _jdump(v) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _check_attrs(el, allowed: frozenset, where: str) -> None:
    extra = set(el.attrib) - allowed
    if extra:
        raise UnknownShape(f"{where}: unknown attribute(s) {sorted(extra)}")


def _meta_entry(ch) -> list:
    extra = set(ch.attrib) - {"key", "value"}
    if extra or len(ch):
        raise UnknownShape(f"MetadataEntry: unknown attribute(s) or children {sorted(extra)}")
    return [ch.get("key"), ch.get("value")]


def _sync(meta: list[list]) -> tuple[str | None, int | None]:
    sid = sver = None
    for k, v in meta:
        if k == SYNC_ID_KEY and sid is None:
            sid = v
        elif k == SYNC_VERSION_KEY and sver is None:
            n = number(v)
            sver = int(n) if n is not None and n == int(n) else None
    return sid, sver


def _times(a) -> dict:
    c_utc, _ = parse_ts(a.get("creationDate"))
    s_utc, off = parse_ts(a.get("startDate"))
    e_utc, _ = parse_ts(a.get("endDate"))
    return {"creation_raw": a.get("creationDate"), "start_raw": a.get("startDate"), "end_raw": a.get("endDate"),
            "creation_utc": c_utc, "start_utc": s_utc, "end_utc": e_utc, "print_offset_min": off}


def read_record(el, ordinal: int | None) -> dict:
    a = el.attrib
    _check_attrs(el, RECORD_ATTRS, "Record")
    meta: list[list] = []
    children: list[dict] = []
    for ch in el:
        if ch.tag == "MetadataEntry":
            meta.append(_meta_entry(ch))
        elif ch.tag == "HeartRateVariabilityMetadataList":
            beats = []
            for b in ch:
                if b.tag != "InstantaneousBeatsPerMinute" or set(b.attrib) - {"bpm", "time"}:
                    raise UnknownShape(f"HeartRateVariabilityMetadataList: unknown child {b.tag}")
                beats.append([b.get("time"), b.get("bpm")])
            children.append({"tag": "HeartRateVariabilityMetadataList", "beats": beats})
        else:
            raise UnknownShape(f"Record: unknown child {ch.tag}")
    t = _times(a)
    meta_json = _jdump(meta) if meta else None
    sid, sver = _sync(meta)
    row = {"ordinal": ordinal, "element": "Record", "hk_type": a.get("type"), "unit": a.get("unit"),
           "value_raw": a.get("value"), "value": number(a.get("value")), "source_name": a.get("sourceName"),
           "source_version": a.get("sourceVersion"), "device": a.get("device"), **t, "metadata": meta_json,
           "children": _jdump(children) if children else None, "sync_identifier": sid, "sync_version": sver}
    row["ident"] = ident(["Record", row["hk_type"] or "", row["unit"] or "", row["value_raw"] or "",
                          row["source_name"] or "", row["source_version"] or "", device_for_ident(row["device"]),
                          _iso(t["creation_utc"], t["creation_raw"]), _iso(t["start_utc"], t["start_raw"]),
                          _iso(t["end_utc"], t["end_raw"]), meta_json or ""])
    return row


def read_correlation(el, ordinal: int) -> dict:
    a = el.attrib
    _check_attrs(el, CORRELATION_ATTRS, "Correlation")
    meta: list[list] = []
    members: list[dict] = []
    for ch in el:
        if ch.tag == "MetadataEntry":
            meta.append(_meta_entry(ch))
        elif ch.tag == "Record":
            members.append(read_record(ch, None))
        else:
            raise UnknownShape(f"Correlation: unknown child {ch.tag}")
    t = _times(a)
    meta_json = _jdump(meta) if meta else None
    sid, sver = _sync(meta)
    row = {"ordinal": ordinal, "element": "Correlation", "hk_type": a.get("type"), "unit": None, "value_raw": None,
           "value": None, "source_name": a.get("sourceName"), "source_version": a.get("sourceVersion"),
           "device": a.get("device"), **t, "metadata": meta_json, "sync_identifier": sid, "sync_version": sver,
           "members": members}
    row["ident"] = ident(["Correlation", row["hk_type"] or "", row["source_name"] or "", row["source_version"] or "",
                          device_for_ident(row["device"]), _iso(t["creation_utc"], t["creation_raw"]),
                          _iso(t["start_utc"], t["start_raw"]), _iso(t["end_utc"], t["end_raw"]), meta_json or "",
                          *sorted(m["ident"] for m in members)])
    return row


def _tree(el) -> dict:
    """An element with its attributes and children, as JSON-ready data (generic elements)."""
    d: dict = {"tag": el.tag, "attrs": dict(el.attrib)}
    if len(el):
        d["children"] = [_tree(c) for c in el]
    if el.text and el.text.strip():
        d["text"] = el.text.strip()
    return d


def read_generic(el, ordinal: int) -> dict:
    """VisionPrescription, Audiogram, ClinicalRecord: the whole element verbatim, plus the fields the
    raw envelope can name (type, source, the start or issue date)."""
    a = el.attrib
    meta = [_meta_entry(ch) for ch in el if ch.tag == "MetadataEntry"]
    parts = [_tree(ch) for ch in el if ch.tag != "MetadataEntry"]
    start_raw = a.get("startDate") or a.get("dateIssued") or a.get("receivedDate")
    s_utc, off = parse_ts(start_raw)
    e_utc, _ = parse_ts(a.get("endDate"))
    c_utc, _ = parse_ts(a.get("creationDate"))
    meta_json = _jdump(meta) if meta else None
    children = _jdump({"attrs": dict(a), "parts": parts})
    sid, sver = _sync(meta)
    row = {"ordinal": ordinal, "element": el.tag, "hk_type": a.get("type"), "unit": None, "value_raw": None,
           "value": None, "source_name": a.get("sourceName"), "source_version": a.get("sourceVersion"),
           "device": a.get("device"), "creation_raw": a.get("creationDate"), "start_raw": start_raw,
           "end_raw": a.get("endDate"), "creation_utc": c_utc, "start_utc": s_utc, "end_utc": e_utc,
           "print_offset_min": off, "metadata": meta_json, "children": children, "sync_identifier": sid,
           "sync_version": sver}
    row["ident"] = ident([el.tag, _iso(s_utc, start_raw), _jdump(sorted((k, v) for k, v in a.items()
                                                                     if k not in ("startDate", "dateIssued",
                                                                                  "receivedDate"))),
                          meta_json or "", _jdump(parts)])
    return row


def read_workout(el, ordinal: int) -> dict:
    a = el.attrib
    _check_attrs(el, WORKOUT_ATTRS, "Workout")
    meta: list[list] = []
    stats: list[dict] = []
    events: list[dict] = []
    routes: list[dict] = []
    for ch in el:
        tag = ch.tag
        if tag == "MetadataEntry":
            meta.append(_meta_entry(ch))
        elif tag == "WorkoutStatistics":
            _check_attrs(ch, STAT_ATTRS, "WorkoutStatistics")
            if len(ch):
                raise UnknownShape("WorkoutStatistics: unexpected children")
            s_utc, _ = parse_ts(ch.get("startDate"))
            e_utc, _ = parse_ts(ch.get("endDate"))
            stats.append({"ord": len(stats), "hk_type": ch.get("type"), "start_raw": ch.get("startDate"),
                          "end_raw": ch.get("endDate"), "start_utc": s_utc, "end_utc": e_utc,
                          "stat_sum": number(ch.get("sum")), "stat_avg": number(ch.get("average")),
                          "stat_min": number(ch.get("minimum")), "stat_max": number(ch.get("maximum")),
                          "unit": ch.get("unit")})
        elif tag == "WorkoutEvent":
            _check_attrs(ch, EVENT_ATTRS, "WorkoutEvent")
            emeta = []
            for g in ch:
                if g.tag != "MetadataEntry":
                    raise UnknownShape(f"WorkoutEvent: unknown child {g.tag}")
                emeta.append(_meta_entry(g))
            d_utc, _ = parse_ts(ch.get("date"))
            events.append({"ord": len(events), "event_type": ch.get("type"), "date_raw": ch.get("date"),
                           "date_utc": d_utc, "duration": number(ch.get("duration")),
                           "duration_unit": ch.get("durationUnit"), "metadata": _jdump(emeta) if emeta else None})
        elif tag == "WorkoutRoute":
            _check_attrs(ch, ROUTE_ATTRS, "WorkoutRoute")
            rmeta, refs = [], []
            for g in ch:
                if g.tag == "MetadataEntry":
                    rmeta.append(_meta_entry(g))
                elif g.tag == "FileReference":
                    if set(g.attrib) - {"path"}:
                        raise UnknownShape("FileReference: unknown attribute")
                    refs.append(g.get("path"))
                else:
                    raise UnknownShape(f"WorkoutRoute: unknown child {g.tag}")
            rt = _times(ch)
            routes.append({"ord": len(routes), "source_name": ch.get("sourceName"),
                           "source_version": ch.get("sourceVersion"), "device": ch.get("device"),
                           "creation_raw": rt["creation_raw"], "start_raw": rt["start_raw"], "end_raw": rt["end_raw"],
                           "start_utc": rt["start_utc"], "end_utc": rt["end_utc"],
                           "metadata": _jdump(rmeta) if rmeta else None, "file_refs": refs})
        else:
            raise UnknownShape(f"Workout: unknown child {tag}")
    t = _times(a)
    meta_json = _jdump(meta) if meta else None
    sid, sver = _sync(meta)
    dur = number(a.get("duration"))
    factor = DURATION_SECONDS.get((a.get("durationUnit") or "").strip())
    tz = next((v for k, v in meta if k == TZ_KEY), None)
    row = {"ordinal": ordinal, "activity_type": a.get("workoutActivityType"), "duration_raw": a.get("duration"),
           "duration_unit": a.get("durationUnit"),
           "duration_s": dur * factor if dur is not None and factor is not None else None,
           "total_distance_raw": a.get("totalDistance"), "total_distance_unit": a.get("totalDistanceUnit"),
           "total_energy_raw": a.get("totalEnergyBurned"), "total_energy_unit": a.get("totalEnergyBurnedUnit"),
           "source_name": a.get("sourceName"), "source_version": a.get("sourceVersion"), "device": a.get("device"),
           **t, "tz_name": tz, "sync_identifier": sid, "sync_version": sver, "metadata": meta,
           "statistics": stats, "events": events, "routes": routes}
    row["ident"] = ident(["Workout", row["activity_type"] or "", row["duration_raw"] or "", row["duration_unit"] or "",
                          row["total_distance_raw"] or "", row["total_distance_unit"] or "",
                          row["total_energy_raw"] or "", row["total_energy_unit"] or "", row["source_name"] or "",
                          row["source_version"] or "", device_for_ident(row["device"]),
                          _iso(t["creation_utc"], t["creation_raw"]), _iso(t["start_utc"], t["start_raw"]),
                          _iso(t["end_utc"], t["end_raw"]), meta_json or ""])
    return row


def read_summary(el, ordinal: int) -> dict:
    a = el.attrib
    _check_attrs(el, SUMMARY_ATTRS, "ActivitySummary")
    if len(el):
        raise UnknownShape("ActivitySummary: unexpected children")
    dc = a.get("dateComponents")
    try:
        day = datetime.strptime(dc, "%Y-%m-%d").date() if dc else None
    except ValueError:
        day = None
    return {"ordinal": ordinal, "date_components": dc, "day": day,
            "active_energy": number(a.get("activeEnergyBurned")),
            "active_energy_goal": number(a.get("activeEnergyBurnedGoal")),
            "active_energy_unit": a.get("activeEnergyBurnedUnit"),
            "move_time": number(a.get("appleMoveTime")), "move_time_goal": number(a.get("appleMoveTimeGoal")),
            "exercise_time": number(a.get("appleExerciseTime")),
            "exercise_time_goal": number(a.get("appleExerciseTimeGoal")),
            "stand_hours": number(a.get("appleStandHours")), "stand_hours_goal": number(a.get("appleStandHoursGoal"))}


def iter_export(path: str | Path) -> Iterator[tuple[int, str, dict]]:
    """Yield (ordinal, kind, row) for every top-level element of HealthData, in document order.
    The first item is (-1, 'HealthData', {'locale': ...}). kind is the element's tag. The caller
    must finish with a row before asking for the next one (the element is cleared after)."""
    ctx = ET.iterparse(str(path), events=("start", "end"))
    _ev, root = next(ctx)
    if root.tag != "HealthData":
        raise UnknownShape(f"root element {root.tag}, expected HealthData")
    yield -1, "HealthData", {"locale": root.get("locale")}
    depth, ordinal, done = 1, -1, 0
    for ev, el in ctx:
        if ev == "start":
            depth += 1
            if depth == 2:
                ordinal += 1
            continue
        depth -= 1
        if depth != 1:
            continue
        tag = el.tag
        if tag == "Record":
            yield ordinal, tag, read_record(el, ordinal)
        elif tag == "Workout":
            yield ordinal, tag, read_workout(el, ordinal)
        elif tag == "ActivitySummary":
            yield ordinal, tag, read_summary(el, ordinal)
        elif tag == "Correlation":
            yield ordinal, tag, read_correlation(el, ordinal)
        elif tag in GENERIC_ELEMENTS:
            yield ordinal, tag, read_generic(el, ordinal)
        elif tag == "ExportDate":
            yield ordinal, tag, {"value": el.get("value")}
        elif tag == "Me":
            yield ordinal, tag, {"n_attributes": len(el.attrib)}       # personal characteristics: counted, never kept
        else:
            raise UnknownShape(f"top-level element {tag} at ordinal {ordinal}")
        el.clear()
        done += 1
        if done % 1000 == 0:
            root.clear()                 # the root keeps a reference to every finished element
