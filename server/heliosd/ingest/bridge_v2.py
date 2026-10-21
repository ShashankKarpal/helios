"""Bridge payload v2 (Phase 3): the lossless raw envelope for what the Bridge sends.

A v2 batch carries `"schema": 2`. Its samples keep every v1 field, so the
registered types go through the samples path exactly as before (ingest/bridge.py),
fed the v1 fields only: the samples rows a v2 batch writes are the rows the
same batch would have written as v1. Everything the v1 shape had no room for
lands in raw_records, one row per record, keyed by the record's own identity:

- every sample, registered or not: hk:<uuid>, element Record, with its
  metadata pairs, the HKDevice description, the source version, the sync
  identifier and version, the delivered instants verbatim (milliseconds) and,
  in children, the Bridge-only fields (bundle id, product type, OS version,
  device fields, the category's raw value). link_status bridge when the
  samples row holds it, none (with a reason) when the samples path did not
  store it, NULL for a type the policy does not register;
- workouts (element Workout): statistics, events, activities, totals and the
  routes HealthKit associates with them, in children;
- routes (element WorkoutRoute): the points in route_points under
  rtb:<route uuid>, the owning workout and the count in children;
- ECGs (element Electrocardiogram): the classification, symptoms, rate and
  every voltage in children;
- activity summaries and Apple's daily totals: one row per distinct
  observation of a day (hkas: and hkds: ids end in a hash of the content), so a
  day that changes keeps each version and a re-sent one changes nothing.

No canonical session, ring or ECG row is built here: matching live sources
into the Phase 2 sessions is the R1 class of problem and waits for its full
fix (owner rule 2026-10-21). Phase 4 builds the views over these rows.

Rules shared with the samples path: a tombstoned uuid never lands; a deletion
removes the Bridge raw row of that uuid (and its route points) in the same
transaction; everything is INSERT OR IGNORE on natural keys, so a replay (an
outbox retry, a relay replay after a crash between ack and delete, a sweep)
writes nothing new. Rows go in through one bound JSON value per table: the
DuckDB Python client's per-value cost (about 5.7 s for 2,000 rows through
executemany, measured 2026-10-21) would otherwise dominate every batch.

A schema this code does not know, or a v2 key it does not know, is refused
(PayloadRefused, HTTP 422, ack false): a newer Bridge can then never have
parts of a batch acknowledged and dropped.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import timezone

from heliosd.ingest.normalize import parse_utc
from heliosd.trust.registry import EXCLUDED_KEY

log = logging.getLogger("heliosd.bridge")

SCHEMA = 2
V2_KEYS = frozenset({"batch_id", "device", "sent_at", "sync_path", "schema", "bridge", "sources", "devices",
                     "samples", "deleted", "workouts", "routes", "ecgs", "activity_summaries", "daily_statistics"})
# The fields the v1 samples path reads; a v2 sample is cut down to these before it.
V1_SAMPLE_KEYS = ("uuid", "hk_type", "value", "unit", "start", "end", "source_name")
FILE_SHA = "bridge"

RAW_COLS = ["record_id", "element", "hk_type", "metric", "value_raw", "value", "unit", "source_name",
            "source_version", "device", "device_key", "creation_raw", "start_raw", "end_raw", "creation_utc",
            "start_utc", "end_utc", "print_offset_min", "metadata", "children", "sync_identifier", "sync_version",
            "link_status", "linked_sample_id", "link_reason", "file_sha", "ordinal", "run_id"]
_RAW_TYPES = {"value": "DOUBLE", "creation_utc": "TIMESTAMP", "start_utc": "TIMESTAMP", "end_utc": "TIMESTAMP",
              "print_offset_min": "INTEGER", "sync_version": "BIGINT", "ordinal": "INTEGER"}
POINT_COLS = ["route_id", "seq", "time_raw", "time_utc", "lat", "lon", "ele", "speed", "course", "h_acc", "v_acc"]
_POINT_TYPES = {"seq": "INTEGER", "time_utc": "TIMESTAMP", "lat": "DOUBLE", "lon": "DOUBLE", "ele": "DOUBLE",
                "speed": "DOUBLE", "course": "DOUBLE", "h_acc": "DOUBLE", "v_acc": "DOUBLE"}


class PayloadRefused(ValueError):
    """The batch is in a shape this daemon cannot store whole."""


def schema_of(payload: dict) -> int:
    """1 for the v1 shape (no schema key), 2 for v2; anything else is refused."""
    schema = payload.get("schema")
    if schema is None:
        return 1
    if schema != SCHEMA or isinstance(schema, bool):
        raise PayloadRefused(f"batch schema {schema!r} is not known to this heliosd (it reads 1 and 2)")
    unknown = sorted(set(payload) - V2_KEYS)
    if unknown:
        raise PayloadRefused(f"batch carries keys this heliosd does not store: {unknown}")
    for key in ("samples", "deleted", "workouts", "routes", "ecgs", "activity_summaries", "daily_statistics"):
        if key in payload and not isinstance(payload[key], list):
            raise PayloadRefused(f"{key} must be a list")
    for key in ("sources", "devices", "bridge"):
        if key in payload and not isinstance(payload[key], dict):
            raise PayloadRefused(f"{key} must be an object")
    if not all(isinstance(u, str) for u in payload.get("deleted", [])):
        raise PayloadRefused("deleted must list uuid strings")
    return SCHEMA


def v1_view(sample: dict) -> dict:
    """The v1 fields of a v2 sample: what the samples path reads."""
    return {k: sample[k] for k in V1_SAMPLE_KEYS if k in sample}


def record_uuids(payload: dict) -> list[str]:
    """Every HealthKit uuid a v2 batch delivers (not the deleted ones)."""
    out = []
    for key in ("samples", "workouts", "routes", "ecgs"):
        out += [str(r["uuid"]) for r in payload.get(key, []) if isinstance(r, dict) and r.get("uuid")]
    return out


def store_device_key(registry, source_name: str | None) -> str | None:
    """The registry in store mode: an ignored source is 'excluded', never dropped."""
    if not source_name:
        return None
    key = registry.resolve(source_name)
    return EXCLUDED_KEY if key is None else key


def _jdump(v) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _h16(obj) -> str:
    return hashlib.sha256(_jdump(obj).encode("utf-8")).hexdigest()[:16]


def _utc(raw) -> str | None:
    """The instant in UTC, milliseconds kept, as text DuckDB reads as TIMESTAMP."""
    if raw in (None, ""):
        return None
    try:
        dt = parse_utc(raw, timezone.utc)
    except (TypeError, ValueError):
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="milliseconds")


def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and math.isfinite(v) and v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return str(v)


def _int(v) -> int | None:
    """An integer that fits the BIGINT column, else None: one out-of-range value
    must not fail the whole batch (the relay would retry it forever)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if -(2 ** 63) <= n < 2 ** 63 else None


class Context:
    """One v2 batch: its descriptors, the uuids the store must not take, and the
    samples-path outcome per uuid."""

    def __init__(self, payload: dict, policy, registry, batch_id: str, skip: set[str]):
        self.sources = payload.get("sources") or {}
        self.devices = payload.get("devices") or {}
        self.policy = policy
        self.registry = registry
        self.run_id = f"bridge:{batch_id}"
        self.skip = skip          # tombstoned or deleted in this batch
        self.ordinal = 0

    def descriptors(self, rec: dict) -> tuple[str | None, str | None, dict]:
        """(source version, device description, Bridge-only extras) of a record."""
        src_key, dev_key = rec.get("src"), rec.get("dev")
        src = (self.sources.get(src_key) if isinstance(src_key, str) else None) or {}
        dev = (self.devices.get(dev_key) if isinstance(dev_key, str) else None) or {}
        src = src if isinstance(src, dict) else {}
        dev = dev if isinstance(dev, dict) else {}
        extra = {}
        for k in ("bundle_id", "product_type", "os_version"):
            if src.get(k) is not None:
                extra[k] = src[k]
        dev_fields = {k: v for k, v in dev.items() if k != "desc" and v is not None}
        if dev_fields:
            extra["device"] = dev_fields
        return src.get("version"), dev.get("desc"), extra

    def row(self, **fields) -> dict:
        r = {c: None for c in RAW_COLS}
        r.update(fields)
        r["file_sha"] = FILE_SHA
        r["run_id"] = self.run_id
        r["ordinal"] = self.ordinal
        self.ordinal += 1
        return r

    def base(self, rec: dict) -> dict:
        version, desc, extra = self.descriptors(rec)
        name = rec.get("source_name")
        return {"source_name": name, "source_version": version, "device": desc,
                "device_key": store_device_key(self.registry, name),
                "start_raw": rec.get("start"), "end_raw": rec.get("end"),
                "start_utc": _utc(rec.get("start")), "end_utc": _utc(rec.get("end")),
                "metadata": _jdump(rec.get("metadata")) if rec.get("metadata") else None,
                "sync_identifier": rec.get("sync_identifier"), "sync_version": _int(rec.get("sync_version"))}, extra


def raw_rows(payload: dict, policy, registry, batch_id: str, skip: set[str], stored: dict[str, str]) -> tuple[list, list, dict]:
    """(raw_records rows, route_points rows, counts per class) of a v2 batch.
    `skip`: uuids tombstoned or deleted in this batch. `stored`: per sample uuid
    of a registered type, 'bridge' when the samples path holds it, else the
    reason it did not store it."""
    ctx = Context(payload, policy, registry, batch_id, skip)
    rows, points = [], []
    counts = {"samples": 0, "workouts": 0, "routes": 0, "ecgs": 0, "activity_summaries": 0, "daily_statistics": 0,
              "skipped_tombstoned": 0}

    for s in payload.get("samples", []):
        uuid = s.get("uuid") if isinstance(s, dict) else None
        if not uuid:
            continue
        if uuid in skip:
            counts["skipped_tombstoned"] += 1
            continue
        hk_type = s.get("hk_type")
        metric = policy.hk_to_metric.get(hk_type) if hk_type else None
        fields, extra = ctx.base(s)
        for k in ("kind", "category_value", "note", "writer_id"):
            if s.get(k) is not None:
                extra[k] = s[k]
        link = stored.get(uuid) if metric else None
        rows.append(ctx.row(record_id=f"hk:{uuid}", element="Record", hk_type=hk_type, metric=metric,
                            value_raw=_text(s.get("value")), value=_num(s.get("value")), unit=s.get("unit") or None,
                            children=_jdump(extra) if extra else None,
                            link_status=None if not metric else ("bridge" if link == "bridge" else "none"),
                            linked_sample_id=f"hk:{uuid}" if link == "bridge" else None,
                            link_reason=None if (not metric or link == "bridge") else (link or "not_stored"),
                            **fields))
        counts["samples"] += 1

    for w in payload.get("workouts", []):
        uuid = w.get("uuid") if isinstance(w, dict) else None
        if not uuid:
            continue
        if uuid in skip:
            counts["skipped_tombstoned"] += 1
            continue
        fields, extra = ctx.base(w)
        for k in ("activity_type", "activity_name", "duration_s", "statistics", "events", "activities",
                  "route_uuids", "total_energy_kcal", "total_distance_m", "writer_id"):
            if w.get(k) is not None:
                extra[k] = w[k]
        rows.append(ctx.row(record_id=f"hk:{uuid}", element="Workout", hk_type="HKWorkoutTypeIdentifier",
                            value_raw=w.get("activity_name"), value=_num(w.get("duration_s")), unit="s",
                            children=_jdump(extra), **fields))
        counts["workouts"] += 1

    for r in payload.get("routes", []):
        uuid = r.get("uuid") if isinstance(r, dict) else None
        if not uuid:
            continue
        if uuid in skip:
            counts["skipped_tombstoned"] += 1
            continue
        route_id = f"rtb:{uuid}"
        fields, extra = ctx.base(r)
        pts = r.get("points") or []
        acc = {"speed_acc": [], "course_acc": []}
        for seq, p in enumerate(pts):
            if not isinstance(p, list) or len(p) < 3:
                continue
            q = list(p) + [None] * (10 - len(p))
            points.append({"route_id": route_id, "seq": seq, "time_raw": q[0], "time_utc": _utc(q[0]),
                           "lat": _num(q[1]), "lon": _num(q[2]), "ele": _num(q[3]), "speed": _num(q[6]),
                           "course": _num(q[7]), "h_acc": _num(q[4]), "v_acc": _num(q[5])})
            acc["speed_acc"].append(_num(q[8]))
            acc["course_acc"].append(_num(q[9]))
        extra.update({"workout_uuid": r.get("workout_uuid"), "route_id": route_id, "n_points": len(pts),
                      "points_sha": hashlib.sha256(_jdump(pts).encode("utf-8")).hexdigest()})
        if any(v is not None for v in acc["speed_acc"] + acc["course_acc"]):
            extra["accuracy"] = acc   # the two per-point fields route_points has no column for
        rows.append(ctx.row(record_id=f"hk:{uuid}", element="WorkoutRoute", hk_type="HKWorkoutRouteTypeIdentifier",
                            value_raw=str(len(pts)), value=float(len(pts)), unit="count", children=_jdump(extra),
                            **fields))
        counts["routes"] += 1

    for e in payload.get("ecgs", []):
        uuid = e.get("uuid") if isinstance(e, dict) else None
        if not uuid:
            continue
        if uuid in skip:
            counts["skipped_tombstoned"] += 1
            continue
        fields, extra = ctx.base(e)
        for k in ("classification", "classification_name", "symptoms_status", "symptoms_name", "average_hr",
                  "sampling_hz", "n_voltages", "lead", "unit", "microvolts"):
            if k in e:
                extra[k] = e[k]
        rows.append(ctx.row(record_id=f"hk:{uuid}", element="Electrocardiogram",
                            hk_type="HKDataTypeIdentifierElectrocardiogram", value_raw=e.get("classification_name"),
                            value=_num(e.get("average_hr")), unit="count/min", children=_jdump(extra), **fields))
        counts["ecgs"] += 1

    for a in payload.get("activity_summaries", []):
        if not isinstance(a, dict) or not a.get("date"):
            continue
        rows.append(ctx.row(record_id=f"hkas:{a['date']}:{_h16(a)}", element="ActivitySummary",
                            hk_type="HKActivitySummaryTypeIdentifier", value_raw=a["date"],
                            value=_num(a.get("active_energy")), unit=a.get("active_energy_unit"),
                            start_raw=a["date"], children=_jdump(a)))
        counts["activity_summaries"] += 1

    for d in payload.get("daily_statistics", []):
        if not isinstance(d, dict) or not d.get("day") or not d.get("hk_type"):
            continue
        rows.append(ctx.row(record_id=f"hkds:{d['hk_type']}:{d.get('zone')}:{d['day']}:{_h16(d)}",
                            element="DailyStatistic", hk_type=d["hk_type"], value_raw=d["day"],
                            value=_num(d.get("value")), unit=d.get("unit"), start_raw=d["day"],
                            children=_jdump({k: d.get(k) for k in ("zone", "complete", "sources")})))
        counts["daily_statistics"] += 1
    return rows, points, counts


def _insert_json(c, table: str, cols: list[str], types: dict, rows: list[dict]) -> int:
    """INSERT OR IGNORE through one bound JSON value; returns the rows written."""
    if not rows:
        return 0
    spec = json.dumps([{col: types.get(col, "VARCHAR") for col in cols}])
    select = ", ".join(f"r.{col}" for col in cols)
    sql = (f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) "
           f"SELECT {select} FROM (SELECT unnest(from_json(?, '{spec}')) AS r)")
    res = c.execute(sql, [json.dumps(rows, ensure_ascii=False)]).fetchone()
    return int(res[0]) if res else 0


def write(c, rows: list[dict], points: list[dict]) -> dict:
    """Inside the batch transaction."""
    return {"raw_written": _insert_json(c, "raw_records", RAW_COLS, _RAW_TYPES, rows),
            "points_written": _insert_json(c, "route_points", POINT_COLS, _POINT_TYPES, points)}


def delete(c, uuids: list[str]) -> int:
    """A HealthKit deletion: the Bridge raw row of each uuid and its route points go.
    Export rows (xr: ids, their own file_sha) are never touched here."""
    if not uuids:
        return 0
    ids = [f"hk:{u}" for u in uuids]
    res = c.execute("DELETE FROM raw_records WHERE file_sha = ? AND record_id IN (SELECT unnest(?))",
                    [FILE_SHA, ids]).fetchone()
    c.execute("DELETE FROM route_points WHERE route_id IN (SELECT unnest(?))", [[f"rtb:{u}" for u in uuids]])
    return int(res[0]) if res else 0


def log_batch(payload: dict, batch_id: str, counts: dict, written: dict, n_inserted: int, guarded: int) -> None:
    """One content-free line per v2 batch: what iOS allowed and what landed."""
    b = payload.get("bridge") or {}
    log.info("bridge v2 batch %s reason=%s origin=%s stream=%s build=%s/%s sent_at=%s samples=%d inserted=%d "
             "guarded=%d workouts=%d routes=%d ecgs=%d summaries=%d dailystats=%d raw_written=%d points_written=%d",
             batch_id, b.get("reason"), b.get("origin"), b.get("stream"), b.get("app_version"), b.get("build"),
             payload.get("sent_at"), counts["samples"], n_inserted, guarded, counts["workouts"], counts["routes"],
             counts["ecgs"], counts["activity_summaries"], counts["daily_statistics"], written["raw_written"],
             written["points_written"])

