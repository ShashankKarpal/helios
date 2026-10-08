"""Wave 2 B9: export twins of Bridge rows, linked one to one and taken out of
eligibility (fix program design B9; owner decision 4c.1 for what stays).

Phase 1b linked an export row to its Bridge row only within a tolerance
tighter than the export's printed rounding (rebase_history.EXPORT_TOLERANCE:
0.0001 by default, 0.001 for energy), and only when the two rows carried the
same unit_rule marker. So thousands of export rows that are the same sample as
an eligible Bridge row stayed eligible and were counted twice (dietary energy,
SDNN, heart rate, Whoop's SpO2 copy). This module finishes that link:

- Candidates: eligible export rows (sync_path health_export) and eligible
  Bridge rows of the same metric, device, source, raw unit and text, at exactly
  the same start and end instants. A Bridge row that an export row is already
  linked to (an export_link alias of any version) is not a candidate.
- Values are compared as the daily values read them (the eligibility view's
  value), within a frozen tolerance: exact for steps and sleep_analysis, 0.01
  for every other metric (the export prints 2 to 4 decimals). The unit_rule
  marker is not part of the key: it records how the Bridge copy was normalised
  (Whoop's SpO2 rows carry frac_to_pct_v1 with the same percent the export
  prints), and a value in another scale can never fall inside the tolerance.
- One to one: inside each key group both sides are sorted by (value,
  sample_id) and merged in order; a pair links only within the tolerance, and
  extra rows on either side stay as they are.
- A linked export row gets quality export_duplicate and a sample_aliases row
  (old_id the export row, new_id the Bridge row, reason export_link_v2). Every
  other column stays. Unlinked export rows stay eligible (decision 4c.1) and
  are listed per type and source, with the reason, in the migrations row.
- One transaction: the link, the checks and the migrations row (phase
  verified, so init_schema lets the daemon start) commit together or not at
  all. A second run on a store that carries the row changes nothing and
  reports what it would have linked.

It must run before apple_watch_6_legacy joins any priority list (B10), or the
legacy watch's SDNN and heart-rate history would start doubled. Phase 2's
export importer must use the same rule.

The same module records the owner's decision D5 (B13, wave2_d5_scale_rows) and
lists what stays unresolved for /api/freshness. Counts and ids only, never a
value, in anything it writes.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from heliosd.store import db

MIGRATION = "wave2_export_relink_v1"
ALIAS_REASON = "export_link_v2"
EXPORT_LINK_REASONS = ("export_link_v1", ALIAS_REASON)     # v1: the Phase 1b link
Q_EXPORT_DUP = "export_duplicate"
Q_EXPORT_AMBIG = "export_ambiguous"
EXACT_METRICS = ("steps", "sleep_analysis")
TOLERANCE = 0.01
_FLOAT_SLACK = 1e-9                  # a difference of exactly 0.01 printed in decimal is 0.010000000000000009 in binary
GROUP_KEY = ("metric", "device_key", "source_name", "raw_unit", "text_value", "start_utc", "end_utc")
PHASE_VERIFIED = "verified"
UNLINKED_REASONS = ("no_bridge_row_at_the_same_instants", "bridge_row_already_linked", "no_value_within_tolerance")

_EXPORT_SQL = """
    SELECT e.sample_id, e.metric, e.device_key, e.source_name, s.unit AS raw_unit, e.text_value,
           e.start_utc, e.end_utc, e.value
    FROM eligible_samples e JOIN samples s ON s.sample_id = e.sample_id
    WHERE e.sync_path = 'health_export'"""


def tolerance(metric: str) -> float:
    return 0.0 if metric in EXACT_METRICS else TOLERANCE


def _within(metric: str, a: float, b: float) -> bool:
    if metric in EXACT_METRICS:
        return a == b
    return abs(a - b) <= TOLERANCE + _FLOAT_SLACK


def _code_commit() -> str | None:
    from heliosd.migrate.rebase_history import _code_commit as commit_of
    return commit_of(Path(__file__).parent)


def _read(conn) -> tuple[list[dict], list[dict]]:
    """(export rows, Bridge rows sharing a key with one of them), as dicts."""
    xs = db.fetchdicts(conn, _EXPORT_SQL)
    bs = db.fetchdicts(conn, f"""
        WITH x AS ({_EXPORT_SQL})
        SELECT b.sample_id, b.metric, b.device_key, b.source_name, s.unit AS raw_unit, b.text_value,
               b.start_utc, b.end_utc, b.value,
               b.sample_id IN (SELECT new_id FROM sample_aliases WHERE reason IN (SELECT unnest(?))) AS linked
        FROM eligible_samples b JOIN samples s ON s.sample_id = b.sample_id
        WHERE b.sync_path = 'bridge'
          AND EXISTS (SELECT 1 FROM x WHERE x.metric = b.metric AND x.device_key = b.device_key
                        AND x.source_name = b.source_name AND x.raw_unit IS NOT DISTINCT FROM s.unit
                        AND x.text_value IS NOT DISTINCT FROM b.text_value
                        AND x.start_utc = b.start_utc AND x.end_utc = b.end_utc)""", [list(EXPORT_LINK_REASONS)])
    return xs, bs


def pair(xs: list[dict], bs: list[dict]) -> tuple[list[tuple[str, str, float]], dict[str, str]]:
    """The one-to-one links ([(export id, Bridge id, |difference|)]) and, for
    every export row left unlinked, its reason. Inside a key group both sides
    are sorted by (value, sample_id) and merged in order: a NULL value pairs
    only with a NULL value; two numbers link when they are within the metric's
    tolerance, otherwise the smaller one is passed over."""
    groups: dict[tuple, tuple[list, list, list]] = defaultdict(lambda: ([], [], []))
    for x in xs:
        groups[tuple(x[k] for k in GROUP_KEY)][0].append(x)
    for b in bs:
        groups[tuple(b[k] for k in GROUP_KEY)][2 if b["linked"] else 1].append(b)
    links: list[tuple[str, str, float]] = []
    unlinked: dict[str, str] = {}
    for key, (gx, gb, already) in groups.items():
        if not gx:
            continue
        metric = key[0]
        done: set[str] = set()
        nx = sorted((x for x in gx if x["value"] is None), key=lambda r: r["sample_id"])
        nb = sorted((b for b in gb if b["value"] is None), key=lambda r: r["sample_id"])
        for x, b in zip(nx, nb):
            links.append((x["sample_id"], b["sample_id"], 0.0))
            done.add(x["sample_id"])
        vx = sorted((x for x in gx if x["value"] is not None), key=lambda r: (r["value"], r["sample_id"]))
        vb = sorted((b for b in gb if b["value"] is not None), key=lambda r: (r["value"], r["sample_id"]))
        i = j = 0
        while i < len(vx) and j < len(vb):
            xv, bv = vx[i]["value"], vb[j]["value"]
            if _within(metric, xv, bv):
                links.append((vx[i]["sample_id"], vb[j]["sample_id"], abs(xv - bv)))
                done.add(vx[i]["sample_id"])
                i += 1
                j += 1
            elif xv < bv:
                i += 1
            else:
                j += 1
        for x in gx:
            if x["sample_id"] not in done:
                unlinked[x["sample_id"]] = (UNLINKED_REASONS[0] if not gb and not already
                                            else UNLINKED_REASONS[1] if not gb else UNLINKED_REASONS[2])
    return links, unlinked


def _by_metric_device(xs: list[dict], links: list[tuple], unlinked: dict[str, str]) -> list[dict]:
    """Per type (metric) and source (device key): links, their largest
    difference and the unlinked rows by reason. Counts only."""
    of = {x["sample_id"]: (x["metric"], x["device_key"]) for x in xs}
    out: dict[tuple, dict] = {}

    def cell(k):
        return out.setdefault(k, {"metric": k[0], "device_key": k[1], "linked": 0, "max_difference": 0.0,
                                  "unlinked": {r: 0 for r in UNLINKED_REASONS}})
    for x_id, _b_id, delta in links:
        c = cell(of[x_id])
        c["linked"] += 1
        c["max_difference"] = max(c["max_difference"], round(delta, 6))
    for x_id, reason in unlinked.items():
        cell(of[x_id])["unlinked"][reason] += 1
    return [out[k] for k in sorted(out)]


def _fingerprint(xs: list[dict], bs: list[dict]) -> str:
    h = hashlib.sha256()
    for r in sorted(xs, key=lambda r: r["sample_id"]):
        h.update(f"x|{r['sample_id']}|{r['value']!r}\n".encode())
    for r in sorted(bs, key=lambda r: r["sample_id"]):
        h.update(f"b|{r['sample_id']}|{r['value']!r}|{bool(r['linked'])}\n".encode())
    return h.hexdigest()


def relink(conn, now: datetime | None = None, code_commit: str | None = None) -> dict:
    """Link every export twin of an eligible Bridge row (see the module
    docstring) and write the verified migrations row, in one transaction.
    Returns the summary. A store whose row exists is left as it is: the result
    says already_applied and how many links a run would make now."""
    phase = db.migration_phase(conn, MIGRATION)
    xs, bs = _read(conn)
    links, unlinked = pair(xs, bs)
    if phase == PHASE_VERIFIED:
        return {"migration": MIGRATION, "already_applied": True, "linked": 0, "would_link": len(links)}
    if phase is not None:
        raise RuntimeError(f"{MIGRATION}: the migrations row exists with phase {phase!r}; inspect it before a rerun")
    stamp = (now or datetime.now()).replace(microsecond=0)
    n = len(links)
    checks: dict[str, bool] = {}
    summary = {
        "migration": MIGRATION, "design": "Wave 2 B9", "phase": PHASE_VERIFIED, "verified_at": stamp.isoformat(sep=" "),
        "code_commit": code_commit if code_commit is not None else _code_commit(),
        "constants": {"key": list(GROUP_KEY), "exact_metrics": list(EXACT_METRICS), "tolerance": TOLERANCE,
                      "values": "as the eligibility view reads them", "pairing": "sorted by (value, sample_id), merged in order",
                      "quality": Q_EXPORT_DUP, "alias_reason": ALIAS_REASON,
                      "bridge_not_a_candidate_when_linked_by": list(EXPORT_LINK_REASONS)},
        "counts": {"eligible_export_rows_before": len(xs), "linked": n, "eligible_export_rows_after": len(xs) - n,
                   "unlinked_by_reason": {r: sum(1 for v in unlinked.values() if v == r) for r in UNLINKED_REASONS}},
        "by_metric_device": _by_metric_device(xs, links, unlinked),
        "note": "Unlinked export rows stay eligible (decision 4c.1). Phase 2's export importer must use the same rule.",
    }
    with db.transaction(conn) as c:
        c.execute("CREATE OR REPLACE TEMP TABLE _relink (x_id VARCHAR, b_id VARCHAR)")
        if links:
            c.executemany("INSERT INTO _relink VALUES (?, ?)", [[x, b] for x, b, _d in links])
        upd = c.execute("UPDATE samples SET quality = ? WHERE quality IS NULL AND sync_path = 'health_export' "
                        "AND sample_id IN (SELECT x_id FROM _relink)", [Q_EXPORT_DUP]).fetchone()
        c.execute("INSERT INTO sample_aliases (old_id, new_id, reason, created_at) SELECT x_id, b_id, ?, ? FROM _relink",
                  [ALIAS_REASON, stamp])

        def one(sql: str, params=None):
            return c.execute(sql, params or []).fetchone()[0]
        checks["every_link_marked_its_export_row"] = int(upd[0] if upd else 0) == n
        checks["one_alias_row_per_link"] = one("SELECT COUNT(*) FROM sample_aliases WHERE reason = ?", [ALIAS_REASON]) == n
        checks["one_to_one_inside_the_run"] = bool(one("SELECT COUNT(DISTINCT x_id) = COUNT(*) AND COUNT(DISTINCT b_id) = COUNT(*) "
                                                       "FROM _relink"))
        checks["no_bridge_row_linked_twice"] = one(
            "SELECT COUNT(*) FROM sample_aliases a JOIN _relink r ON a.new_id = r.b_id "
            "WHERE a.reason IN (SELECT unnest(?)) AND a.old_id <> r.x_id", [list(EXPORT_LINK_REASONS)]) == 0
        checks["linked_export_rows_left_eligibility"] = one(
            "SELECT COUNT(*) FROM eligible_samples WHERE sample_id IN (SELECT x_id FROM _relink)") == 0
        checks["their_bridge_rows_stay_eligible"] = one(
            "SELECT COUNT(*) FROM eligible_samples WHERE sample_id IN (SELECT b_id FROM _relink)") == n
        checks["eligible_export_rows_fell_by_the_links"] = one(
            "SELECT COUNT(*) FROM eligible_samples WHERE sync_path = 'health_export'") == len(xs) - n
        summary["checks"] = checks
        failed = [k for k, ok in checks.items() if not ok]
        if failed:
            raise RuntimeError(f"{MIGRATION}: checks failed {failed}; nothing was written")
        c.execute("INSERT INTO migrations (name, applied_at, code_commit, input_fingerprint, summary) VALUES (?, ?, ?, ?, ?)",
                  [MIGRATION, stamp, summary["code_commit"], _fingerprint(xs, bs), json.dumps(summary, default=str)])
        c.execute("DROP TABLE _relink")
    return summary


# ---- B13: the owner's decision D5 on the ambiguous scale rows ----

D5_MIGRATION = "wave2_d5_scale_rows"
D5_METRICS = ("bmi", "body_mass")
D5_DECISION = {"id": "D5", "decided": "2026-10-08",
               "text": "The ambiguous BMI and body-mass export rows (audit P13): excluded from eligibility and listed; "
                       "the rows are kept."}


def record_d5_rows(conn, now: datetime | None = None, code_commit: str | None = None) -> dict:
    """Write the verified migrations row wave2_d5_scale_rows: the export rows
    of BMI and body mass that Phase 1b left export_ambiguous, found by query
    (never a hard-coded id), with the decision and its date. The rows are not
    touched: the quality they already carry keeps them out of eligibility, and
    the checks prove that. A store that carries the row is left as it is."""
    phase = db.migration_phase(conn, D5_MIGRATION)
    if phase == PHASE_VERIFIED:
        return {"migration": D5_MIGRATION, "already_applied": True}
    if phase is not None:
        raise RuntimeError(f"{D5_MIGRATION}: the migrations row exists with phase {phase!r}; inspect it before a rerun")
    stamp = (now or datetime.now()).replace(microsecond=0)
    sel = "FROM samples WHERE quality = ? AND metric IN (SELECT unnest(?))"
    args = [Q_EXPORT_AMBIG, list(D5_METRICS)]
    rows = db.fetchall(conn, f"SELECT sample_id, metric, device_key {sel} ORDER BY metric, start_ts, sample_id", args)
    digest = f"SELECT COUNT(*), CAST(bit_xor(hash(t)) AS VARCHAR) FROM (SELECT * {sel}) t"     # every column of every row
    before = db.fetchall(conn, digest, args)[0]
    by = db.fetchdicts(conn, f"""SELECT metric, device_key, COUNT(*) AS n, CAST(MIN(CAST(start_ts AS DATE)) AS VARCHAR) AS first,
                                        CAST(MAX(CAST(start_ts AS DATE)) AS VARCHAR) AS last {sel} GROUP BY 1, 2 ORDER BY 1, 2""", args)
    twins = db.fetchdicts(conn, f"""
        SELECT a.metric, COUNT(DISTINCT b.sample_id) AS bridge_rows, COUNT(DISTINCT CAST(b.start_ts AS DATE)) AS days
        FROM (SELECT * {sel}) a JOIN eligible_samples b ON b.metric = a.metric AND b.device_key = a.device_key
             AND b.start_utc = a.start_utc AND b.end_utc = a.end_utc AND b.sync_path = 'bridge'
        GROUP BY 1 ORDER BY 1""", args)
    outside = db.fetchdicts(conn, "SELECT metric, COUNT(*) AS n FROM samples WHERE quality = ? AND metric NOT IN "
                                  "(SELECT unnest(?)) GROUP BY 1 ORDER BY 1", args)
    summary = {"migration": D5_MIGRATION, "design": "Wave 2 B13", "phase": PHASE_VERIFIED,
               "verified_at": stamp.isoformat(sep=" "), "decision": D5_DECISION,
               "code_commit": code_commit if code_commit is not None else _code_commit(),
               "query": f"quality = '{Q_EXPORT_AMBIG}' AND metric IN {D5_METRICS}",
               "rows": [{"sample_id": s, "metric": m, "device_key": d} for s, m, d in rows],
               "by_metric_device": by, "eligible_bridge_rows_at_the_same_instants": twins,
               "export_ambiguous_outside_d5": outside}
    ids = [r[0] for r in rows]
    with db.transaction(conn) as c:
        checks = {
            "none_of_them_eligible": c.execute("SELECT COUNT(*) FROM eligible_samples WHERE sample_id IN (SELECT unnest(?))",
                                               [ids]).fetchone()[0] == 0,
            "rows_unchanged": tuple(c.execute(digest, args).fetchone()) == tuple(before)}
        summary["checks"] = checks
        if not all(checks.values()):
            raise RuntimeError(f"{D5_MIGRATION}: checks failed {[k for k, v in checks.items() if not v]}; nothing was written")
        c.execute("INSERT INTO migrations (name, applied_at, code_commit, input_fingerprint, summary) VALUES (?, ?, ?, ?, ?)",
                  [D5_MIGRATION, stamp, summary["code_commit"], hashlib.sha256("\n".join(ids).encode()).hexdigest(),
                   json.dumps(summary, default=str)])
    return summary


def unresolved_exports(conn) -> list[dict]:
    """Per metric and device: export rows Phase 1b left ambiguous (out of
    eligibility, D5) and export rows no Bridge row accounts for, which stay
    eligible (decision 4c.1). Counts only; /api/freshness serves the list."""
    return db.fetchdicts(conn, """
        WITH a AS (SELECT metric, device_key, COUNT(*) AS n FROM samples WHERE quality = ? GROUP BY 1, 2),
             u AS (SELECT metric, device_key, COUNT(*) AS n FROM eligible_samples WHERE sync_path = 'health_export' GROUP BY 1, 2)
        SELECT COALESCE(a.metric, u.metric) AS metric, COALESCE(a.device_key, u.device_key) AS device_key,
               COALESCE(a.n, 0) AS export_ambiguous, COALESCE(u.n, 0) AS export_unmatched
        FROM a FULL OUTER JOIN u ON u.metric = a.metric AND u.device_key = a.device_key
        ORDER BY 1, 2""", [Q_EXPORT_AMBIG])
