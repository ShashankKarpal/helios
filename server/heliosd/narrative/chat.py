"""Conversational layer: tool-calling chat over the owner's real data.
Every number in an answer must trace to a tool result; every citation names
the device and confidence. The model queries; it never receives a data dump."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta

from heliosd.narrative.lmstudio import ANSWER_SCHEMA, LMStudio, SYSTEM_GUARDRAILS
from heliosd.narrative.validator import validate_text
from heliosd.store import db

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
        "description": "Latest Whoop recovery, strain, and sleep for today (live overlay).",
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


def _tool_whoop_live(conn) -> dict:
    rows = db.fetchdicts(conn, """SELECT date, kind, payload FROM whoop_cache
        WHERE date >= ? ORDER BY date DESC""", [date.today() - timedelta(days=1)])
    out = {}
    for r in rows:
        p = json.loads(r["payload"])
        sc = p.get("score") or {}
        if r["kind"] == "recovery":
            out["recovery"] = {"date": str(r["date"]), "recovery_score": sc.get("recovery_score"),
                               "hrv_rmssd_ms": sc.get("hrv_rmssd_milli"),
                               "resting_hr": sc.get("resting_heart_rate"), "device": "whoop", "live": True}
        elif r["kind"] == "cycle":
            out["strain"] = {"date": str(r["date"]), "strain": sc.get("strain"), "device": "whoop", "live": True}
        elif r["kind"] == "sleep":
            out["sleep"] = {"date": str(r["date"]), "score": sc, "device": "whoop", "live": True}
    return out or {"note": "no whoop data cached for today; run /api/whoop/pull"}


def run_tool(conn, name: str, args: dict) -> dict:
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
            return _tool_whoop_live(conn)
        return {"error": f"unknown tool {name}"}
    except Exception as e:  # tools must never crash the loop
        return {"error": str(e)}


def run_chat(conn, lm: LMStudio, message: str, session_id: str | None = None,
             temperature: float = 0.65, max_rounds: int = 6) -> dict:
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
                result = run_tool(conn, fn["name"], args)
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
