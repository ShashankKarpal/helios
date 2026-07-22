"""Monday style seven day review.

Deterministic summary of the last week: recovery trend, sleep architecture,
strain against recovery, anomalies the watchdog flagged, and one concrete
experiment to run. Patterns are pulled from the correlations module when it can
load. Output is the owner's house style: plain language, clear headings, no em
dashes, small tables where a table earns its place.
"""

from __future__ import annotations

from datetime import date, timedelta

from heliosd.store import db


def _anchor(conn) -> date | None:
    rows = db.fetchall(conn, "SELECT MAX(date) FROM daily_values")
    if not rows or rows[0][0] is None:
        return None
    d = rows[0][0]
    return d if isinstance(d, date) else date.fromisoformat(str(d))


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _series(conn, metric, start, end):
    rows = db.fetchall(conn,
        "SELECT date, value FROM daily_values WHERE metric = ? AND date BETWEEN ? AND ? "
        "AND value IS NOT NULL ORDER BY date", [metric, start, end])
    return [(r[0], float(r[1])) for r in rows]


def _recovery_block(conn, start, end):
    """Prefer recovery_score, fall back to hrv_rmssd. Report level and trend."""
    metric = "recovery_score"
    s = _series(conn, metric, start, end)
    if not s:
        metric = "hrv_rmssd"
        s = _series(conn, metric, start, end)
    if not s:
        return {"metric": None, "avg": None, "trend": None, "series": []}
    vals = [v for _, v in s]
    half = max(1, len(vals) // 2)
    first, second = _avg(vals[:half]), _avg(vals[half:])
    trend = None
    if first is not None and second is not None:
        if second > first * 1.03:
            trend = "improving"
        elif second < first * 0.97:
            trend = "slipping"
        else:
            trend = "steady"
    return {"metric": metric, "avg": _avg(vals), "trend": trend,
            "first_half": first, "second_half": second,
            "series": [(str(d), round(v, 1)) for d, v in s]}


def _sleep_architecture(conn, start, end):
    """Average deep, rem and core minutes per night from the sleep stage samples."""
    rows = db.fetchall(conn, """
        SELECT text_value, CAST(end_ts AS DATE) AS wake, SUM(value)
        FROM samples
        WHERE metric = 'sleep_analysis'
          AND text_value IN ('deep', 'rem', 'core')
          AND CAST(end_ts AS DATE) BETWEEN ? AND ?
        GROUP BY text_value, wake""", [start, end])
    buckets = {"deep": [], "rem": [], "core": []}
    for stage, _wake, mins in rows:
        if stage in buckets and mins is not None:
            buckets[stage].append(float(mins))
    nights = len({r[1] for r in rows})
    return {
        "nights": nights,
        "deep_min": round(_avg(buckets["deep"]) or 0.0, 1),
        "rem_min": round(_avg(buckets["rem"]) or 0.0, 1),
        "core_min": round(_avg(buckets["core"]) or 0.0, 1),
    }


def _anomalies(conn, start, end):
    rows = db.fetchdicts(conn, """
        SELECT date, metric, value, delta_pct, why FROM signals
        WHERE state = 'flag' AND date BETWEEN ? AND ? ORDER BY date DESC""",
        [start, end])
    return rows


def _experiment(recovery, sleep, insights):
    """One concrete, testable suggestion for the week."""
    if insights:
        top = insights[0]
        return (f"Test the pattern '{top['title']}'. Change one input this week and "
                f"watch whether the relationship holds in your own numbers.")
    if sleep["deep_min"] and sleep["deep_min"] < 60:
        return ("Deep sleep is running under an hour a night. Try a fixed lights out "
                "time for seven nights and compare deep sleep minutes against this week.")
    if recovery.get("trend") == "slipping":
        return ("Recovery is slipping. Pick two lighter training days this week and "
                "check whether recovery scores recover by next Monday.")
    return ("Hold routine steady for seven days and log caffeine timing. A clean week "
            "gives the next review a better baseline to measure against.")


def _md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join(["---"] * len(headers)) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def build_weekly_review(conn, policy) -> dict:
    """Return {markdown, data} for the trailing seven days.

    Never raises on sparse data. If there is no data at all the markdown still
    renders with a short note under each heading.
    """
    anchor = _anchor(conn)
    if anchor is None:
        md = ("# Weekly Review\n\nNo daily data is available yet. Once a few days of "
              "device data land, this review will fill in.\n")
        return {"markdown": md, "data": {"anchor": None}}

    end = anchor
    start = end - timedelta(days=6)
    recovery = _recovery_block(conn, start, end)
    sleep = _sleep_architecture(conn, start, end)
    strain = _avg([v for _, v in _series(conn, "strain", start, end)])
    rec_avg = recovery["avg"]
    anomalies = _anomalies(conn, start, end)

    insights = []
    try:
        from heliosd.insights.correlations import top_insights
        insights = top_insights(conn, days=90)
    except Exception:
        insights = []

    experiment = _experiment(recovery, sleep, insights)

    data = {
        "anchor": str(anchor), "start": str(start), "end": str(end),
        "recovery": recovery, "sleep": sleep,
        "strain_avg": round(strain, 1) if strain is not None else None,
        "anomalies": anomalies, "insights": insights, "experiment": experiment,
    }

    lines = []
    lines.append(f"# Weekly Review, {start} to {end}")
    lines.append("")

    # Recovery trend
    lines.append("## Recovery trend")
    if recovery["metric"]:
        label = "recovery score" if recovery["metric"] == "recovery_score" else "HRV (rMSSD)"
        trend = recovery["trend"] or "flat"
        lines.append(f"Seven day average {label}: {round(recovery['avg'], 1)}. "
                     f"The trend across the week is {trend}.")
    else:
        lines.append("No recovery or HRV data landed this week.")
    lines.append("")

    # Sleep architecture
    lines.append("## Sleep architecture")
    if sleep["nights"]:
        lines.append(f"Averaged over {sleep['nights']} nights with stage data:")
        lines.append("")
        lines.append(_md_table(
            ["Stage", "Avg minutes per night"],
            [["Deep", sleep["deep_min"]], ["REM", sleep["rem_min"]],
             ["Core", sleep["core_min"]]]))
    else:
        lines.append("No sleep stage data this week.")
    lines.append("")

    # Strain versus recovery
    lines.append("## Strain versus recovery")
    if strain is not None and rec_avg is not None:
        balance = ("Load and recovery look matched this week."
                   if abs((strain / 21.0) - (rec_avg / 100.0)) < 0.15
                   else "Load and recovery look out of step, worth a closer look.")
        lines.append(_md_table(
            ["Measure", "Seven day average"],
            [["Strain", round(strain, 1)], ["Recovery", round(rec_avg, 1)]]))
        lines.append("")
        lines.append(balance)
    else:
        lines.append("Not enough strain or recovery data to compare this week.")
    lines.append("")

    # Flagged anomalies
    lines.append("## Flagged anomalies")
    if anomalies:
        rows = [[str(a["date"]), a["metric"],
                 (a["why"] or "").replace("\n", " ")] for a in anomalies]
        lines.append(_md_table(["Date", "Metric", "Why"], rows))
    else:
        lines.append("No metrics were flagged in the last seven days.")
    lines.append("")

    # Patterns
    lines.append("## Patterns")
    if insights:
        for ins in insights[:5]:
            lines.append(f"- {ins['title']}. {ins['verdict']} ({ins['method']}, {ins['stat']}).")
    else:
        lines.append("No associations cleared the confidence bar this week.")
    lines.append("")

    # Experiment
    lines.append("## Experiment for the week")
    lines.append(experiment)
    lines.append("")

    return {"markdown": "\n".join(lines), "data": data}
