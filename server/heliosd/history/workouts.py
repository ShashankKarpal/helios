"""Canonical workout sessions from the export's Workout records (single-source
plan v2 section 4.5, Phase 2).

Rules, in order:
1. Explicit links first. The export carries no link between the records of two
   writers (no WorkoutActivity uuid, no shared identifier), so the only
   explicit link is a same-writer copy: two records of one source name with the
   same activity type, start and end. The first (creation, then document order)
   represents both; the other joins its session as a copy.
2. One-to-one candidates. A record of a lower source family is a candidate for
   a session anchored by a higher family when it has the same activity family,
   starts within 5 minutes of the anchor, overlaps it at least 0.80 of their
   union, and its span is 0.80 to 1.20 times the anchor's. It joins only when
   the candidacy is unique both ways (one session for the record, one record of
   that family for the session). Anchors come family by family: apple, then
   whoop, then every other family in name order; a record left unlinked by one
   family's anchors becomes an anchor itself when its own family's turn comes.
3. Everything the rules do not decide goes to the queue, never guessed: a
   record with several candidates either way (multiple_candidates), a lone
   record that overlaps another family's session by half the shorter span or
   more but fails the rule (near_miss, with the parts it failed), two records
   of one family that overlap that much (same_family_overlap), a route file
   that workouts of different sessions name (shared_route_file) and a route
   file that is not in the export (missing_route_file). A queued record keeps
   a session of its own (status queued), so nothing disappears from a count.

Spans (end minus start) are compared, not the duration attribute: Apple's
duration leaves pauses out, the other writers' does not.

Field owners (all nullable; the first family in the field's order that has
the value wins, then the session's own family order, apple, whoop and the
rest by name, so a session without the owner falls back to its timing
source; strain has no fallback): type and timing
apple, whoop; exercise heart rate zepp, whoop, apple (the owner's decisions
file, section 2 item 1, 2026-10-04: both upper-arm devices above the wrist;
plan v2 4.5 had apple before whoop), read from the workout's own heart-rate
statistics, while the sample coverage gate and the samples inside the window
are Phase 3's exercise_priority; distance, route and energy apple; strain
whoop only. The type order of plan v2 starts with a user correction, which
does not exist yet.
Energy is the session's own and is never added to a daily total. Every value
names the source row it came from.

Independence is unknown unless lineage proves it: a same-writer copy is
marked same_writer_copy, everything else unknown.
"""

from __future__ import annotations

import bisect
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from heliosd.history.export_xml import number

RULE_VERSION = "candidate_v1"
START_WITHIN_S = 300.0
MIN_IOU = 0.80
SPAN_RATIO = (0.80, 1.20)
QUEUE_SHARE = 0.50                 # overlap over the shorter span that puts a lone record in the queue
NEAR_COPY_S = 60.0                 # start and end within a minute of an apple record: a near-exact copy (report only)

TIER_HEAD = ("apple", "whoop")
FIELD_ORDER = {"type": ("apple", "whoop"), "timing": ("apple", "whoop"), "hr": ("zepp", "whoop", "apple"),
               "distance": ("apple",), "route": ("apple",), "energy": ("apple",), "strain": ("whoop",)}
EXCLUSIVE_FIELDS = frozenset({"strain"})
STRAIN_KEY = "WHOOP Strain"
HR_TYPE = "HKQuantityTypeIdentifierHeartRate"
ENERGY_TYPE = "HKQuantityTypeIdentifierActiveEnergyBurned"
DISTANCE_TYPES = ("HKQuantityTypeIdentifierDistanceWalkingRunning", "HKQuantityTypeIdentifierDistanceCycling",
                  "HKQuantityTypeIdentifierDistanceSwimming", "HKQuantityTypeIdentifierDistanceWheelchair",
                  "HKQuantityTypeIdentifierDistanceDownhillSnowSports", "HKQuantityTypeIdentifierDistanceCrossCountrySkiing",
                  "HKQuantityTypeIdentifierDistancePaddleSports", "HKQuantityTypeIdentifierDistanceRowing",
                  "HKQuantityTypeIdentifierDistanceSkatingSports")
FAMILY_DISTANCE = {"cycle": "HKQuantityTypeIdentifierDistanceCycling", "swim": "HKQuantityTypeIdentifierDistanceSwimming",
                   "walk": "HKQuantityTypeIdentifierDistanceWalkingRunning",
                   "run": "HKQuantityTypeIdentifierDistanceWalkingRunning"}

# Activity families (HKWorkoutActivityType without its prefix). A type not listed is its own family.
ACTIVITY_FAMILIES = {
    "walk": ("Walking", "Hiking", "WheelchairWalkPace"),
    "run": ("Running", "WheelchairRunPace"),
    "cycle": ("Cycling", "HandCycling"),
    "swim": ("Swimming",),
    "cardio": ("Elliptical", "StairClimbing", "Stairs", "StepTraining", "Rowing", "MixedCardio",
               "HighIntensityIntervalTraining", "JumpRope", "CrossTraining", "MixedMetabolicCardioTraining"),
    "strength": ("FunctionalStrengthTraining", "TraditionalStrengthTraining", "CoreTraining"),
    "mind_body": ("Yoga", "MindAndBody", "Pilates", "Barre", "Flexibility", "Cooldown", "PreparationAndRecovery",
                  "TaiChi"),
    "racket": ("Badminton", "Tennis", "TableTennis", "Squash", "Racquetball", "Pickleball"),
    "snow": ("DownhillSkiing", "SnowSports", "CrossCountrySkiing", "Snowboarding"),
    "other": ("Other",),
}
_FAMILY_OF = {t: fam for fam, types in ACTIVITY_FAMILIES.items() for t in types}


def activity_family(activity_type: str | None) -> str:
    t = (activity_type or "").removeprefix("HKWorkoutActivityType")
    return _FAMILY_OF.get(t, t.lower() or "unknown")


def source_family(device_key: str | None) -> str:
    """apple, whoop or zepp by the device key's public prefix; any other device is its own family."""
    k = device_key or "other"
    if k.startswith("apple_watch") or k == "iphone":
        return "apple"
    if k.startswith("whoop"):
        return "whoop"
    if k.startswith("zepp"):
        return "zepp"
    return k


def family_order(present: set[str], head: tuple[str, ...] = TIER_HEAD) -> list[str]:
    return [f for f in head if f in present] + sorted(present - set(head))


def _span(s) -> float:
    return max(0.0, (s.end - s.start).total_seconds())


def compare(anchor, other) -> dict:
    """The rule's parts for `other` against `anchor`, rounded for the record."""
    inter = max(0.0, (min(anchor.end, other.end) - max(anchor.start, other.start)).total_seconds())
    union = max(anchor.end, other.end) - min(anchor.start, other.start)
    union_s = union.total_seconds()
    sa, so = _span(anchor), _span(other)
    return {"start_gap_s": abs((other.start - anchor.start).total_seconds()),
            "iou": inter / union_s if union_s > 0 else (1.0 if inter == 0 and sa == so == 0 else 0.0),
            "span_ratio": so / sa if sa > 0 else None,
            "same_activity_family": anchor.act_family == other.act_family,
            "overlap_share": inter / min(sa, so) if min(sa, so) > 0 else 0.0}


def failed_parts(c: dict) -> list[str]:
    out = []
    if not c["same_activity_family"]:
        out.append("activity_family")
    if c["start_gap_s"] > START_WITHIN_S:
        out.append("start_gap")
    if c["iou"] < MIN_IOU:
        out.append("overlap")
    r = c["span_ratio"]
    if r is None or not (SPAN_RATIO[0] <= r <= SPAN_RATIO[1]):
        out.append("span_ratio")
    return out


def passes(c: dict) -> bool:
    return not failed_parts(c)


def _rounded(c: dict) -> dict:
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in c.items()}


@dataclass
class Src:
    id: str
    family: str
    act_family: str
    start: datetime
    end: datetime
    order_key: tuple
    row: dict
    copies: list[str] = field(default_factory=list)


def _item_id(kind: str, ids) -> str:
    return f"amb:{kind}:" + hashlib.sha256("|".join(sorted(ids)).encode()).hexdigest()[:16]


def build_sessions(sources: list[dict], route_refs: dict[str, list[dict]] | None = None) -> dict:
    """sources: one dict per Workout record with source_id, family, activity_type, start_utc, end_utc,
    duration_s, creation_utc, ordinal, source_name, tz_name, metadata ([[k, v]]), statistics (list of
    dicts), total_distance_raw/unit, total_energy_raw/unit. route_refs: source_id -> its workout_routes rows
    (route_ref_id, route_id, link_status). Returns {'sources': {source_id: placement}, 'sessions': [...],
    'queue': [...], 'stats': {...}}. Pure: no store access."""
    route_refs = route_refs or {}
    timed = [s for s in sources if s.get("start_utc") is not None and s.get("end_utc") is not None]
    untimed = [s for s in sources if s not in timed]
    # 1. same-writer copies
    groups: dict[tuple, list[dict]] = {}
    for s in sorted(timed, key=lambda s: (s.get("creation_utc") or datetime.min, s["ordinal"])):
        groups.setdefault((s.get("source_name"), s.get("activity_type"), s["start_utc"], s["end_utc"]), []).append(s)
    reps: list[Src] = []
    copy_of: dict[str, str] = {}
    for g in groups.values():
        head = g[0]
        r = Src(head["source_id"], head["family"], activity_family(head.get("activity_type")), head["start_utc"],
                head["end_utc"], (head["start_utc"], head["source_id"]), head, [c["source_id"] for c in g[1:]])
        reps.append(r)
        for c in g[1:]:
            copy_of[c["source_id"]] = head["source_id"]
    reps.sort(key=lambda r: r.order_key)
    by_id = {r.id: r for r in reps}
    starts = [r.start for r in reps]
    window = max([_span(r) for r in reps], default=0.0)

    def near(r: Src, share: float | None = None) -> list[Src]:
        """Records whose start is within the rule's 5 minutes (share None), or that overlap r at all."""
        lo = r.start - timedelta(seconds=(START_WITHIN_S if share is None else window))
        hi = r.start + timedelta(seconds=START_WITHIN_S) if share is None else r.end
        i = bisect.bisect_left(starts, lo)
        out = []
        while i < len(reps) and reps[i].start <= hi:
            if reps[i].id != r.id:
                out.append(reps[i])
            i += 1
        return out

    # 2. family by family, one to one
    families = family_order({r.family for r in reps})
    session_of: dict[str, str] = {}             # rep id -> anchor id
    members: dict[str, list[str]] = {}          # anchor id -> rep ids (anchor first)
    detail: dict[str, dict] = {}
    queue: dict[str, dict] = {}
    queued: set[str] = set()
    for i, fam in enumerate(families):
        anchors = [r for r in reps if r.family == fam and r.id not in session_of and r.id not in queued]
        for a in anchors:
            session_of[a.id] = a.id
            members[a.id] = [a.id]
        anchor_ids = {a.id for a in anchors}
        for lower in families[i + 1:]:
            pool = [r for r in reps if r.family == lower and r.id not in session_of and r.id not in queued]
            cand: dict[str, list[tuple[str, dict]]] = {}
            back: dict[str, list[str]] = {}
            for r in pool:
                for a in near(r):
                    if a.id in anchor_ids:
                        c = compare(a, r)
                        if passes(c):
                            cand.setdefault(r.id, []).append((a.id, c))
                            back.setdefault(a.id, []).append(r.id)
            for r in pool:
                cs = cand.get(r.id, [])
                if not cs:
                    continue
                a_id, c = cs[0]
                if len(cs) == 1 and len(back[a_id]) == 1:
                    session_of[r.id] = a_id
                    members[a_id].append(r.id)
                    detail[r.id] = _rounded(c)
                else:
                    rivals = sorted({x for a2, _ in cs for x in back[a2]} - {r.id})
                    iid = _item_id("multiple_candidates", [r.id])
                    queue[iid] = {"item_id": iid, "kind": "multiple_candidates", "source_ids": [r.id],
                                  "candidate_ids": sorted(a2 for a2, _ in cs),
                                  "reason": "more than one session passes the rule, or the session has more than one "
                                            "candidate of this family",
                                  "detail": {"candidates": {a2: _rounded(c2) for a2, c2 in cs},
                                             "rival_records": rivals}}
                    queued.add(r.id)
    # 3. what is left: queued records keep a session of their own
    for r in reps:
        if r.id not in session_of:
            session_of[r.id] = r.id
            members[r.id] = [r.id]
    anchors_all = {a: by_id[a] for a in members}
    # near misses and same-family overlaps, against other sessions' anchors
    for a_id, ms in members.items():
        if len(ms) != 1 or a_id in queued:
            continue
        r = by_id[a_id]
        for o in near(r, share=QUEUE_SHARE):
            if o.id not in anchors_all or o.id == r.id or session_of.get(o.id) == a_id or o.id in queued:
                continue
            c = compare(o, r)
            if c["overlap_share"] < QUEUE_SHARE:
                continue
            if o.family == r.family:
                kind, reason = "same_family_overlap", "two records of one source family overlap; kept apart"
            else:
                kind, reason = "near_miss", "overlaps another family's session but fails the rule"
            iid = _item_id(kind, [r.id, o.id])
            if iid in queue:
                continue
            queue[iid] = {"item_id": iid, "kind": kind, "source_ids": sorted([r.id, o.id]), "candidate_ids": [],
                          "reason": reason, "detail": {**_rounded(c), "failed": failed_parts(c)}}
    # routes: a file belongs to a session only when every workout that names it is in that session
    file_sessions: dict[str, set[str]] = {}
    file_refs: dict[str, list[str]] = {}
    for sid, refs in route_refs.items():
        owner = session_of.get(copy_of.get(sid, sid))
        for ref in refs:
            if ref.get("route_id"):
                file_sessions.setdefault(ref["route_id"], set()).add(owner)
                file_refs.setdefault(ref["route_id"], []).append(sid)
            elif ref.get("link_status") == "missing_file":
                iid = _item_id("missing_route_file", [ref["route_ref_id"]])
                queue[iid] = {"item_id": iid, "kind": "missing_route_file", "source_ids": [sid], "candidate_ids": [],
                              "reason": "the workout names a route file the export does not hold",
                              "detail": {"route_ref_id": ref["route_ref_id"]}}
    unusable = set()
    for rid, sess in file_sessions.items():
        if len(sess) > 1:
            unusable.add(rid)
            iid = _item_id("shared_route_file", file_refs[rid])
            queue[iid] = {"item_id": iid, "kind": "shared_route_file", "source_ids": sorted(set(file_refs[rid])),
                          "candidate_ids": sorted(s for s in sess if s), "reason":
                          "workouts of different sessions name the same route file (the export names files by "
                          "start minute); the route is left off all of them",
                          "detail": {"route_id": rid}}
    # A session is queued when one of its rows is in an item, or when it is a candidate whose make-up is pending.
    in_queue = {sid for q in queue.values() for sid in q["source_ids"]} | {
        sid for q in queue.values() if q["kind"] == "multiple_candidates" for sid in q["candidate_ids"]}
    in_queue |= {copy_of[x] for x in in_queue if x in copy_of}          # a copy's item queues its representative
    # 4. sessions and their fields
    sessions = []
    placement: dict[str, dict] = {}
    for a_id, ms in members.items():
        rows = {m: by_id[m].row for m in ms}
        fam_of = {m: by_id[m].family for m in ms}
        sess = _fields(a_id, ms, rows, fam_of, route_refs, unusable)
        n_src = len(ms) + sum(len(by_id[m].copies) for m in ms)
        status = "queued" if any(m in in_queue for m in ms) and len(ms) == 1 else ("matched" if len(ms) > 1 else "single")
        sess.update({"workout_id": "wk:" + a_id, "status": status, "n_sources": n_src, "rule_version": RULE_VERSION})
        sessions.append(sess)
        for m in ms:
            placement[m] = {"workout_id": "wk:" + a_id, "role": "anchor" if m == a_id else "member",
                            "link_rule": "anchor" if m == a_id else RULE_VERSION,
                            "match_detail": detail.get(m), "independence": "unknown"}
            for c in by_id[m].copies:
                placement[c] = {"workout_id": "wk:" + a_id, "role": "copy", "link_rule": "explicit_copy",
                                "match_detail": None, "independence": "same_writer_copy"}
    for s in untimed:            # cannot be placed in time: a session of its own, in the queue
        iid = _item_id("untimed", [s["source_id"]])
        queue[iid] = {"item_id": iid, "kind": "untimed", "source_ids": [s["source_id"]], "candidate_ids": [],
                      "reason": "the record's start or end could not be read", "detail": {}}
        sessions.append({"workout_id": "wk:" + s["source_id"], "status": "queued", "n_sources": 1,
                         "rule_version": RULE_VERSION, **_fields(s["source_id"], [s["source_id"]],
                                                                  {s["source_id"]: s}, {s["source_id"]: s["family"]},
                                                                  route_refs, unusable)})
        placement[s["source_id"]] = {"workout_id": "wk:" + s["source_id"], "role": "anchor", "link_rule": "anchor",
                                     "match_detail": None, "independence": "unknown"}
    return {"sources": placement, "sessions": sessions, "queue": sorted(queue.values(), key=lambda q: q["item_id"]),
            "stats": _stats(reps, by_id, session_of, members, cand_rule_counts(reps, near))}


def cand_rule_counts(reps: list[Src], near) -> dict:
    """For plan v2 2.5: per non-apple family, records with at least one apple record that passes the rule
    (before one to one) and near-exact copies of an apple record (start and end within a minute)."""
    out: dict[str, dict] = {}
    for r in reps:
        if r.family == "apple":
            continue
        c = out.setdefault(r.family, {"records": 0, "passing_an_apple_record": 0, "near_exact_copy_of_apple": 0})
        c["records"] += 1
        apples = [a for a in near(r) if a.family == "apple"]
        if any(passes(compare(a, r)) for a in apples):
            c["passing_an_apple_record"] += 1
        if any(abs((a.start - r.start).total_seconds()) <= NEAR_COPY_S and abs((a.end - r.end).total_seconds())
               <= NEAR_COPY_S for a in apples):
            c["near_exact_copy_of_apple"] += 1
    return out


def _stats(reps, by_id, session_of, members, rule_counts) -> dict:
    linked_to_apple: dict[str, int] = {}
    for r in reps:
        a = session_of.get(r.id)
        if a and a != r.id and by_id[a].family == "apple":
            linked_to_apple[r.family] = linked_to_apple.get(r.family, 0) + 1
    for fam, c in rule_counts.items():
        c["linked_to_an_apple_session"] = linked_to_apple.get(fam, 0)
    return {"representatives": len(reps), "copies": sum(len(r.copies) for r in reps), "sessions": len(members),
            "by_family_against_apple": rule_counts}


def _stat(row: dict, hk_type: str) -> dict | None:
    for st in row.get("statistics") or []:
        if st.get("hk_type") == hk_type:
            return st
    return None


def _distance(row: dict, act_family: str) -> tuple[float, str, str] | None:
    found = [st for st in row.get("statistics") or [] if st.get("hk_type") in DISTANCE_TYPES and st.get("stat_sum") is not None]
    if found:
        pref = FAMILY_DISTANCE.get(act_family)
        st = next((s for s in found if s["hk_type"] == pref), found[0])
        return st["stat_sum"], st.get("unit"), st["hk_type"]
    v = number(row.get("total_distance_raw"))
    if v is not None:
        return v, row.get("total_distance_unit"), "totalDistance"
    return None


def _energy(row: dict) -> tuple[float, str] | None:
    st = _stat(row, ENERGY_TYPE)
    if st and st.get("stat_sum") is not None:
        return st["stat_sum"], st.get("unit")
    v = number(row.get("total_energy_raw"))
    if v is not None:
        return v, row.get("total_energy_unit")
    return None


def _strain(row: dict) -> float | None:
    for k, v in row.get("metadata") or []:
        if k == STRAIN_KEY:
            return number(v)
    return None


def _first(field: str, ms: list[str], fam_of: dict[str, str], has) -> str | None:
    """The member that owns `field`: the field's own families first, then the session's family order
    (apple, whoop, the rest by name), so a session without its owner falls back to its timing source."""
    order = list(FIELD_ORDER[field])
    if field not in EXCLUSIVE_FIELDS:
        order += [f for f in family_order({fam_of[m] for m in ms}) if f not in order]
    for fam in order:
        for m in ms:
            if fam_of[m] == fam and has(m):
                return m
    return None


def _fields(a_id: str, ms: list[str], rows: dict[str, dict], fam_of: dict[str, str], route_refs: dict,
            unusable: set[str]) -> dict:
    out: dict = {k: None for k in ("activity_type", "activity_family", "type_source", "start_utc", "end_utc",
                                   "duration_s", "timing_source", "tz_name", "tz_source", "distance", "distance_unit",
                                   "distance_type", "distance_source", "energy_kcal", "energy_unit", "energy_source",
                                   "hr_avg", "hr_min", "hr_max", "hr_unit", "hr_source", "strain", "strain_source",
                                   "route_id", "route_source")}
    m = _first("type", ms, fam_of, lambda x: rows[x].get("activity_type"))
    if m:
        out.update(activity_type=rows[m]["activity_type"], activity_family=activity_family(rows[m]["activity_type"]),
                   type_source=m)
    m = _first("timing", ms, fam_of, lambda x: rows[x].get("start_utc") is not None)
    if m:
        r = rows[m]
        span = (r["end_utc"] - r["start_utc"]).total_seconds() if r.get("end_utc") else None
        out.update(start_utc=r["start_utc"], end_utc=r.get("end_utc"),
                   duration_s=r.get("duration_s") if r.get("duration_s") is not None else span, timing_source=m)
        tz_m = m if r.get("tz_name") else next((x for x in ms if rows[x].get("tz_name")), None)
        if tz_m:
            out.update(tz_name=rows[tz_m]["tz_name"], tz_source=tz_m)
    fam = out["activity_family"] or ""
    m = _first("distance", ms, fam_of, lambda x: _distance(rows[x], fam) is not None)
    if m:
        v, unit, kind = _distance(rows[m], fam)
        out.update(distance=v, distance_unit=unit, distance_type=kind, distance_source=m)
    m = _first("energy", ms, fam_of, lambda x: _energy(rows[x]) is not None)
    if m:
        v, unit = _energy(rows[m])
        out.update(energy_kcal=v, energy_unit=unit, energy_source=m)

    def hr(x):
        st = _stat(rows[x], HR_TYPE)
        return st is not None and any(st.get(k) is not None for k in ("stat_avg", "stat_min", "stat_max"))
    m = _first("hr", ms, fam_of, hr)
    if m:
        st = _stat(rows[m], HR_TYPE)
        out.update(hr_avg=st.get("stat_avg"), hr_min=st.get("stat_min"), hr_max=st.get("stat_max"),
                   hr_unit=st.get("unit"), hr_source=m)
    m = _first("strain", ms, fam_of, lambda x: _strain(rows[x]) is not None)
    if m:
        out.update(strain=_strain(rows[m]), strain_source=m)

    def usable_route(x):
        return next((r for r in route_refs.get(x, []) if r.get("route_id") and r["route_id"] not in unusable), None)
    m = _first("route", ms, fam_of, lambda x: usable_route(x) is not None)
    if m:
        ref = usable_route(m)
        out.update(route_id=ref["route_id"], route_source=ref["route_ref_id"])
    return out


def to_json(v) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str)
