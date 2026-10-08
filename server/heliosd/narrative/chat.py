"""Conversational layer: tool-calling chat over the owner's real data.
Every number in an answer must trace to a tool result; every citation names
the device and confidence. The model queries; it never receives a data dump."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone

from heliosd.ingest.normalize import reporting_today, to_wall
from heliosd.ingest.whoop import cache_record_key, parse_iso_utc
from heliosd.narrative.lmstudio import ANSWER_SCHEMA, LMStudio, SYSTEM_GUARDRAILS
from heliosd.narrative.validator import validate_text
from heliosd.store import db

UTC = timezone.utc


def _zone(policy_or_zone):
    """The reporting zone from a MetricPolicy, a ZoneInfo, or None (UTC).
    Every date a tool reports is a reporting-zone date, never the Mac clock
    (audit P8)."""
    if policy_or_zone is None:
        return UTC
    return getattr(policy_or_zone, "zone", policy_or_zone)


def _iso_in_zone(naive_utc: datetime | None, zone) -> str | None:
    """A stored naive-UTC instant as an ISO string with the reporting zone's
    offset, so a reader never has to guess which clock stamped it."""
    if naive_utc is None:
        return None
    return naive_utc.replace(tzinfo=UTC).astimezone(zone).isoformat(timespec="seconds")

TOOLS = [
    {"type": "function", "function": {
        "name": "query_metric",
        "description": "Daily canonical values for a metric with device provenance and confidence.",
        "parameters": {"type": "object", "properties": {
            "metric": {"type": "string", "description": "canonical id, e.g. hrv_rmssd, resting_hr, sleep_duration, recovery_score, strain, steps, glucose, body_mass"},
            "days": {"type": "integer", "description": "trailing window, default 14"},
            "stat": {"type": "string", "enum": ["series", "summary"]}},
            "required": ["metric"]}}},
    {"type": "function", "function": {
        "name": "get_daily_signals",
        "description": "All computed signals (state vs personal baseline) for a date. Use for 'how did I sleep', 'should I train'.",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD, default today"}}}}},
    {"type": "function", "function": {
        "name": "compare_periods",
        "description": "Compare metric medians between two trailing windows, e.g. this week vs last week.",
        "parameters": {"type": "object", "properties": {
            "metric": {"type": "string"}, "days_a": {"type": "integer"}, "days_b": {"type": "integer"}},
            "required": ["metric"]}}},
    {"type": "function", "function": {
        "name": "list_events",
        "description": "Recent logged events (quicklog, meds, caffeine, symptoms) and labs.",
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["all", "labs", "quicklog"]},
            "days": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "whoop_live",
        "description": "Newest cached Whoop recovery, sleep and strain, each dated, with stale=true when it is not the reporting today's record and in_progress for the open cycle.",
        "parameters": {"type": "object", "properties": {}}}},
]


def _tool_query_metric(conn, metric: str, days: int = 14, stat: str = "series") -> dict:
    rows = db.fetchdicts(conn, """
        SELECT date, value, unit, device_key, grade, confidence, corroboration
        FROM daily_values WHERE metric = ? AND date >= ?
        ORDER BY date""", [metric, date.today() - timedelta(days=days)])
    for r in rows:
        r["date"] = str(r["date"])
        if r.get("corroboration"):
            r["corroboration"] = json.loads(r["corroboration"])
    if stat == "summary" and rows:
        vals = [r["value"] for r in rows if r["value"] is not None]
        return {"metric": metric, "days": days, "n": len(vals),
                "min": min(vals), "max": max(vals),
                "median": sorted(vals)[len(vals) // 2],
                "latest": rows[-1], "device": rows[-1]["device_key"]}
    return {"metric": metric, "days": days, "series": rows}


def _tool_signals(conn, day_str: str | None) -> dict:
    d = date.fromisoformat(day_str) if day_str else date.today()
    rows = db.fetchdicts(conn, "SELECT * FROM signals WHERE date = ?", [d])
    for r in rows:
        r["date"] = str(r["date"])
    return {"date": str(d), "signals": rows}


def _tool_compare(conn, metric: str, days_a: int = 7, days_b: int = 7) -> dict:
    today = date.today()
    def med(start, end):
        rows = db.fetchall(conn, """SELECT value FROM daily_values
            WHERE metric = ? AND date >= ? AND date < ? AND value IS NOT NULL""",
            [metric, start, end])
        vals = sorted(r[0] for r in rows)
        return (vals[len(vals) // 2] if vals else None), len(vals)
    a, na = med(today - timedelta(days=days_a), today + timedelta(days=1))
    b, nb = med(today - timedelta(days=days_a + days_b), today - timedelta(days=days_a))
    delta = round((a - b) / b * 100, 1) if a is not None and b else None
    return {"metric": metric, "recent_median": a, "previous_median": b,
            "recent_days": na, "previous_days": nb, "change_pct": delta}


def _tool_events(conn, kind: str = "all", days: int = 30) -> dict:
    out: dict = {}
    if kind in ("all", "quicklog"):
        out["events"] = db.fetchdicts(conn, """SELECT kind, ts, payload FROM events
            WHERE ts >= ? ORDER BY ts DESC LIMIT 50""",
            [datetime.now() - timedelta(days=days)])
        for e in out["events"]:
            e["ts"] = str(e["ts"])
    if kind in ("all", "labs"):
        out["labs"] = db.fetchdicts(conn, """SELECT panel_date, biomarker, value, unit, ref_low, ref_high
            FROM labs ORDER BY panel_date DESC LIMIT 100""")
        for l in out["labs"]:
            l["panel_date"] = str(l["panel_date"])
    return out


def _record_fetched_at(conn, record_key: str | None, zone) -> str | None:
    if not record_key:
        return None
    rows = db.fetchall(conn, "SELECT fetched_at FROM whoop_records WHERE record_key = ?", [record_key])
    return _iso_in_zone(rows[0][0], zone) if rows and rows[0][0] else None


def _tool_whoop_live(conn, zone=None, now: datetime | None = None) -> dict:
    """The newest cached Whoop record per kind, each with an explicit `stale`
    flag against the reporting today and the instant it was fetched.

    Audit P1 (2026-10-08): the old query ordered by date DESC and assigned
    out[kind] on every row, so yesterday's row overwrote today's and the
    oldest cached day was labelled live; strain never appeared once the
    open cycle's start date left the two-day window. Now: one row per kind
    (the newest date), the cycle looked up whatever its start date, a nap
    only when it is today's, and `stale` instead of a `live: true` that was
    never checked. Whoop's sleep resting heart rate is `resting_hr_sleep`
    (audit P7): policy keeps it apart from Apple's all-day `resting_hr`."""
    zone = _zone(zone)
    today = reporting_today(zone, now)
    rows = db.fetchdicts(conn, """
        SELECT date, kind, payload FROM (
            SELECT date, kind, payload,
                   ROW_NUMBER() OVER (PARTITION BY kind ORDER BY date DESC) AS rn
            FROM whoop_cache WHERE kind IN ('recovery', 'sleep', 'cycle', 'sleep_nap')) t
        WHERE rn = 1 ORDER BY kind""")
    out: dict = {"reporting_date": str(today)}
    for r in rows:
        try:
            p = json.loads(r["payload"] or "{}") or {}
        except (TypeError, ValueError):
            p = {}
        sc = p.get("score") or {}
        d = r["date"]
        base = {"date": str(d), "device": "whoop",
                "score_state": p.get("score_state") or ("SCORED" if sc else "PENDING_SCORE"),
                "fetched_at": _record_fetched_at(conn, cache_record_key(r["kind"], r["payload"]), zone)}
        if r["kind"] == "recovery":
            out["recovery"] = base | {"recovery_score": sc.get("recovery_score"),
                                      "hrv_rmssd_ms": sc.get("hrv_rmssd_milli"),
                                      "resting_hr_sleep": sc.get("resting_heart_rate"),
                                      "stale": d != today}
            if d != today:
                out["recovery"]["note"] = (f"Whoop's recovery for {today} is not pulled yet; "
                                           f"this is the record for {d}")
        elif r["kind"] == "sleep":
            out["sleep"] = base | {"start": p.get("start"), "end": p.get("end"), "score": sc,
                                   "stale": d != today}
            if d != today:
                out["sleep"]["note"] = (f"Whoop's sleep ending {today} is not pulled yet; "
                                        f"this is the night ending {d}")
        elif r["kind"] == "cycle":
            end = p.get("end")
            in_progress = end is None
            end_d = None
            if end:
                end_utc = parse_iso_utc(end)
                end_d = to_wall(end_utc, zone).date() if end_utc else None
            out["strain"] = base | {"strain": sc.get("strain"), "cycle_start": p.get("start"),
                                    "cycle_end": end, "in_progress": in_progress,
                                    "stale": (not in_progress) and end_d is not None and end_d < today}
            if in_progress:
                out["strain"]["note"] = "the current cycle is still open: strain so far, not a day value"
            elif out["strain"]["stale"]:
                out["strain"]["note"] = f"the newest cycle closed on {end_d}; the current cycle is not pulled yet"
        elif r["kind"] == "sleep_nap" and d == today:
            out["nap"] = base | {"start": p.get("start"), "end": p.get("end"), "score": sc}
    last = db.fetchall(conn, "SELECT MAX(fetched_at) FROM whoop_records")
    out["last_pull_at"] = _iso_in_zone(last[0][0], zone) if last and last[0][0] else None
    if not {"recovery", "sleep", "strain"} & set(out):
        out["note"] = "no whoop data cached; POST /api/whoop/pull"
    return out


def run_tool(conn, name: str, args: dict, policy=None, now: datetime | None = None) -> dict:
    """`policy` (or a ZoneInfo) supplies the reporting zone every tool dates
    by; None keeps the historical UTC behaviour for callers without one."""
    zone = _zone(policy)
    try:
        if name == "query_metric":
            return _tool_query_metric(conn, args["metric"], int(args.get("days", 14)),
                                      args.get("stat", "series"))
        if name == "get_daily_signals":
            return _tool_signals(conn, args.get("date"))
        if name == "compare_periods":
            return _tool_compare(conn, args["metric"], int(args.get("days_a", 7)),
                                 int(args.get("days_b", 7)))
        if name == "list_events":
            return _tool_events(conn, args.get("kind", "all"), int(args.get("days", 30)))
        if name == "whoop_live":
            return _tool_whoop_live(conn, zone, now)
        return {"error": f"unknown tool {name}"}
    except Exception as e:  # tools must never crash the loop
        return {"error": str(e)}


def run_chat(conn, lm: LMStudio, message: str, session_id: str | None = None,
             temperature: float = 0.65, max_rounds: int = 6, policy=None) -> dict:
    session_id = session_id or uuid.uuid4().hex[:12]
    history = db.fetchdicts(conn, """SELECT role, content FROM chat_messages
        WHERE session_id = ? ORDER BY created_at DESC LIMIT 10""", [session_id])
    messages = [{"role": "system", "content": SYSTEM_GUARDRAILS +
                 " Today is " + str(date.today()) + ". Query tools before answering; "
                 "never answer from memory about the owner's data."}]
    messages += [{"role": h["role"], "content": h["content"]} for h in reversed(history)]
    messages.append({"role": "user", "content": message})

    tool_outputs: list[dict] = []
    for _ in range(max_rounds):
        msg = lm.chat(messages, temperature=temperature, tools=TOOLS)
        if msg.get("tool_calls"):
            messages.append({"role": "assistant", "content": msg.get("content"),
                             "tool_calls": msg["tool_calls"]})
            for tc in msg["tool_calls"]:
                fn = tc["function"]
                args = json.loads(fn.get("arguments") or "{}")
                result = run_tool(conn, fn["name"], args, policy)
                tool_outputs.append({"tool": fn["name"], "args": args, "result": result})
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                 "content": json.dumps(result, default=str)})
            continue
        draft = msg.get("content") or ""
        break
    else:
        draft = "I could not finish querying the data. Try a narrower question."

    # Structure + validate the final answer against what the tools actually returned.
    structured = lm.structured(
        [{"role": "system", "content": SYSTEM_GUARDRAILS},
         {"role": "user", "content":
          "Convert this draft answer into the JSON contract. Cite every number with its "
          "metric, device, and confidence grade taken from TOOL_RESULTS. Do not add numbers.\n"
          f"DRAFT:\n{draft}\n\nTOOL_RESULTS:\n{json.dumps(tool_outputs, default=str)[:12000]}"}],
        ANSWER_SCHEMA, temperature=0.0)
    errors = validate_text(structured.get("answer", ""), tool_outputs)
    if errors:
        structured["caveats"] = structured.get("caveats", []) + \
            [f"validator: {e}" for e in errors[:3]]
        if len(errors) > 2:
            structured["answer"] = draft  # fall back to raw draft, caveated

    for role, content in (("user", message), ("assistant", structured.get("answer", draft))):
        db.execute(conn, """INSERT INTO chat_messages (msg_id, session_id, role, content, citations)
                            VALUES (?, ?, ?, ?, ?)""",
                   [uuid.uuid4().hex, session_id, role, content,
                    json.dumps(structured.get("citations", [])) if role == "assistant" else None])
    structured["session_id"] = session_id
    structured["tool_calls"] = [{"tool": t["tool"], "args": t["args"]} for t in tool_outputs]
    return structured
