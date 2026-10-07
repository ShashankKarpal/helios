"""Content twins: one HealthKit sample stored under two or more uuids.

HealthKit holds every copy (the 2026-10-06 re-read delivered every uuid), so
the store keeps every row and every uuid; ONE row per content group is
eligible for daily values and the rest carry quality hk_content_twin (owner
decision 2026-10-07, decisions file section 4d; design-twins.md,
adjudication-A-twins.md). The row's quality is the materialized state; the
table content_twins is the durable, idempotent record of every transition
(demoted by the migration, promoted by a later deletion or restore) and the
only lookup the deletion path needs. sample_aliases stays identity-only.

The key, the exemption, the eligibility predicate and the survivor order live
here, in one place, for the three users: the Phase 1b migration (the
collapse, heliosd/migrate/rebase_history.py), the re-read landing (a demoted
row re-delivered identically is not a variant, heliosd/ingest/landing.py) and
the deletion and restore paths (when a survivor is deleted, a remaining
confirmed member is promoted, heliosd/ingest/bridge.py and heliosd/backup.py).

The key: same source and device, same identity fields, bit-equal value,
instants truncated to the second, the Phase 3 writer fields when present,
NULL-safe. Never across sources. Only Bridge rows (sync_path bridge); export
rows have the export link, Whoop rows the Whoop step. Only rows that satisfy
the eligibility view's predicate take part (quality, excluded device, score
state, time validity, the future ceiling at one fixed instant). Exempt:
dietary_energy (identical entries can be real separate meals; decision 4
counts every nutrition entry).

Survivor order, deterministic and delivery-order independent: a confirmed
instant first (bridge_reread_v1 or bridge_utc before era_rebase_v1), then the
legacy row (the old store counted it; its ch2 ids alias to it), then the
lowest id. For a `last` metric the HIGHEST id comes first, before
confirmation: that is the row the same-instant tie rule (start_ts, sample_id;
owner 4c.3) picks, so a collapse never demotes the row that was winning
(checkpoint B point 4; the migration gates on it). A survivor is an existing
eligible row, never a revival, so provenance may rank second there.

Promotion (a survivor deleted later) takes only a member whose observation is
confirmed: its row carries bridge_utc or bridge_reread_v1, or hk_reread holds a
later bridge_utc landing of exactly its content; never a member with a
landing variant (checkpoint B point 6). Participation needs both UTC instants
(point 7): unknown instants never establish equivalence.
"""

from __future__ import annotations

Q_CONTENT_TWIN = "hk_content_twin"
RULE = "content_twin_v1"
EVENT_DEMOTED, EVENT_PROMOTED = "demoted_v1", "promoted_v1"
EXEMPT_METRICS = ("dietary_energy",)
NEAR_REL = 1e-6                      # a value difference within this (relative) is a NEAR twin: reported, never collapsed
CONFIRMED_TIME_SOURCES = ("bridge_utc", "bridge_reread_v1")
KEY_EXACT = ("hk_type", "metric", "source_name", "device_key", "unit", "unit_rule", "text_value", "value",
             "writer_id", "sync_identifier", "sync_version")
KEY_INSTANTS = ("start_utc", "end_utc")
KEY = KEY_EXACT + KEY_INSTANTS
LAST_METRICS_SQL = "(SELECT metric FROM metric_registry WHERE agg = 'last')"


def key_exprs(alias: str | None = None) -> list[str]:
    """The key as SQL expressions on a row alias (instants to the second)."""
    a = f"{alias}." if alias else ""
    return [f"{a}{c}" for c in KEY_EXACT] + [f"date_trunc('second', {a}{c})" for c in KEY_INSTANTS]


def key_equal_sql(a: str, b: str) -> str:
    """NULL-safe equality of the whole key between two row aliases."""
    return " AND ".join(f"{x} IS NOT DISTINCT FROM {y}" for x, y in zip(key_exprs(a), key_exprs(b)))


def key_param_sql(alias: str) -> str:
    """NULL-safe equality of the key of a row alias with bound parameters, in KEY order."""
    a = f"{alias}." if alias else ""
    parts = [f"{a}{c} IS NOT DISTINCT FROM ?" for c in KEY_EXACT]
    parts += [f"date_trunc('second', {a}{c}) IS NOT DISTINCT FROM date_trunc('second', CAST(? AS TIMESTAMP))" for c in KEY_INSTANTS]
    return " AND ".join(parts)


def key_params(row: dict) -> list:
    return [row.get(c) for c in KEY]


def eligible_sql(alias: str | None, ceiling_expr: str, quality: str = "IS NULL") -> str:
    """The eligibility view's predicate on a row alias, with the future ceiling
    given as an expression (a fixed instant in the migration, so every member
    of a group is judged at the same time; the view's own clock otherwise) and
    the quality condition pluggable (a promotion candidate carries the mark)."""
    a = f"{alias}." if alias else ""
    return (f"{a}quality {quality} AND {a}device_key <> 'excluded' AND ({a}score_state IS NULL OR {a}score_state = 'SCORED') "
            f"AND {a}start_ts IS NOT NULL AND {a}start_utc IS NOT NULL AND {a}end_utc IS NOT NULL "
            f"AND (CASE WHEN {a}start_utc IS NOT NULL THEN ({a}end_utc IS NULL OR {a}end_utc >= {a}start_utc) "
            f"ELSE ({a}end_ts IS NULL OR {a}end_ts >= {a}start_ts) END) "
            f"AND COALESCE({a}start_utc, {a}start_ts) <= {ceiling_expr}")


def survivor_order_sql(alias: str | None, legacy_expr: str, last_expr: str) -> str:
    """ORDER BY terms for the survivor. A `last` metric: the highest id first
    (the tie rule's winner), then confirmed. Any other metric: confirmed first,
    then the legacy row, then the lowest id. legacy_expr says the row is a
    legacy (rebased) row; last_expr says its metric is a `last` metric
    (metric_registry.agg)."""
    a = f"{alias}." if alias else ""
    confirmed = ", ".join(repr(t) for t in CONFIRMED_TIME_SOURCES)
    return (f"(CASE WHEN {last_expr} THEN {a}sample_id END) DESC NULLS LAST, "
            f"NOT COALESCE({a}time_source IN ({confirmed}), FALSE), "
            f"(CASE WHEN {last_expr} THEN FALSE ELSE NOT COALESCE({legacy_expr}, FALSE) END), {a}sample_id")


def sorts_before_sql(a: str, b: str, legacy_a: str, legacy_b: str, last_a: str) -> str:
    """The survivor order spelled out as a comparison: TRUE when row a sorts
    before row b of the same group. An independent formulation of
    survivor_order_sql for the migration's re-derivation gate."""
    confirmed = ", ".join(repr(t) for t in CONFIRMED_TIME_SOURCES)
    ca, cb = f"COALESCE({a}.time_source IN ({confirmed}), FALSE)", f"COALESCE({b}.time_source IN ({confirmed}), FALSE)"
    la, lb = f"COALESCE({legacy_a}, FALSE)", f"COALESCE({legacy_b}, FALSE)"
    # a `last` metric: the higher id sorts first, full stop (ids are unique); otherwise confirmed, legacy, lower id
    return (f"(CASE WHEN {last_a} THEN {a}.sample_id > {b}.sample_id ELSE "
            f"(({ca} AND NOT {cb}) OR ({ca} = {cb} AND {la} AND NOT {lb}) OR ({ca} = {cb} AND {la} = {lb} AND {a}.sample_id < {b}.sample_id)) END)")


def confirmed_sql(alias: str) -> str:
    """The row's observation is confirmed: its own time source, or a later
    bridge_utc landing in hk_reread with exactly its content."""
    a = f"{alias}."
    confirmed = ", ".join(repr(t) for t in CONFIRMED_TIME_SOURCES)
    return (f"({a}time_source IN ({confirmed}) OR EXISTS (SELECT 1 FROM hk_reread h WHERE h.hk_uuid = {a}hk_uuid AND h.time_source = 'bridge_utc' "
            f"AND date_trunc('second', h.start_utc) IS NOT DISTINCT FROM date_trunc('second', {a}start_utc) "
            f"AND date_trunc('second', h.end_utc) IS NOT DISTINCT FROM date_trunc('second', {a}end_utc) "
            f"AND h.value IS NOT DISTINCT FROM {a}value AND h.unit IS NOT DISTINCT FROM {a}unit AND h.text_value IS NOT DISTINCT FROM {a}text_value))")


def _group_anchors(c, sample_ids: list[str]) -> dict[str, str]:
    """For every deleted row, the survivor the group's demoted rows name: a
    promoted row stands in for the survivor it replaced, so promoted_v1 rows
    are followed back, set-wise per chain depth (content_twins is indexed on
    sample_id), until no row was promoted."""
    anchor = {sid: sid for sid in sample_ids}
    frontier = list(sample_ids)
    seen: set[str] = set()
    while frontier:
        rows = c.execute("""SELECT sample_id, survivor_id FROM content_twins WHERE event = ? AND sample_id IN (SELECT unnest(?))
                            QUALIFY row_number() OVER (PARTITION BY sample_id ORDER BY created_at DESC) = 1""", [EVENT_PROMOTED, frontier]).fetchall()
        seen.update(frontier)
        nxt = {}
        for sid, surv in rows:
            if surv and surv not in seen:
                nxt[sid] = surv
        for origin, a in anchor.items():
            if a in nxt:
                anchor[origin] = nxt[a]
        frontier = sorted(set(nxt.values()))
    return anchor


def promote_after_delete(c, victims: list[dict], source: str, now) -> int:
    """After rows were deleted by uuid or id (same transaction): for every
    deleted Bridge row that was ELIGIBLE (quality NULL) and represents a
    recorded content group (a migration survivor, or a row promoted in its
    place), the first remaining demoted member of that group that still
    exists, is confirmed (confirmed_sql), has no landing variant, satisfies the
    eligibility predicate apart from the mark and shares the deleted row's key
    becomes eligible again (quality NULL), and the transition is recorded with
    `source` (batch:<id> or restore), idempotently. Its reporting dates are
    the victim's dates, which the caller has already journaled. A deleted
    demoted row promotes nothing. Lookups go through the table's indexes
    (sample_id, survivor_id); an ordinary deletion costs two indexed queries.
    Returns the number promoted."""
    candidates = [v for v in victims if v.get("quality") is None and v.get("sync_path") == "bridge"]
    if not candidates:
        return 0
    anchors = _group_anchors(c, sorted(v["sample_id"] for v in candidates))
    leading = {r[0] for r in c.execute("SELECT DISTINCT survivor_id FROM content_twins WHERE event = ? AND survivor_id IN (SELECT unnest(?))",
                                       [EVENT_DEMOTED, sorted(set(anchors.values()))]).fetchall()}
    last_expr = f"s.metric IN {LAST_METRICS_SQL}"
    n = 0
    for v in sorted(candidates, key=lambda r: r["sample_id"]):
        anchor = anchors[v["sample_id"]]
        if anchor not in leading:
            continue
        row = c.execute(f"""SELECT s.sample_id FROM content_twins t JOIN samples s ON s.sample_id = t.sample_id
            WHERE t.event = ? AND t.survivor_id = ? AND s.sync_path = 'bridge' AND {confirmed_sql('s')}
              AND NOT EXISTS (SELECT 1 FROM hk_reread_variants v WHERE v.hk_uuid = s.hk_uuid)
              AND {eligible_sql('s', "timezone('UTC', now()) + INTERVAL 1 DAY", quality='= ?')} AND {key_param_sql('s')}
            ORDER BY {survivor_order_sql('s', 's.rebase_era IS NOT NULL', last_expr)} LIMIT 1""",
                        [EVENT_DEMOTED, anchor, Q_CONTENT_TWIN] + key_params(v)).fetchone()
        if row:
            c.execute("UPDATE samples SET quality = NULL WHERE sample_id = ? AND quality = ?", [row[0], Q_CONTENT_TWIN])
            c.execute("INSERT OR IGNORE INTO content_twins (sample_id, survivor_id, event, source, created_at) VALUES (?, ?, ?, ?, ?)",
                      [row[0], v["sample_id"], EVENT_PROMOTED, source, now])
            n += 1
    return n
