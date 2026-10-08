"""Monday style seven day review.

Deterministic summary of the last week: recovery trend, sleep architecture,
strain and recovery in Whoop's own bands, anomalies the watchdog flagged, and
one concrete experiment to run. Patterns are pulled from the correlations
module when it can load. Output is the owner's house style: plain language,
clear headings, no em dashes, small tables where a table earns its place.

The week is the seven COMPLETE reporting days ending on the last complete day
(owner decision D7, fix program A14): the partial today never enters an
average, every average says how many values it rests on, and a stage average
is labelled by the device whose vocabulary it uses (Whoop "light" and Apple
"core" are never one row).
"""

from __future__ import annotations

from datetime import date, timedelta

from heliosd.ingest.normalize import last_complete_day
from heliosd.store import db

WINDOW_DAYS = 7

# Whoop's published bands. Strain is on Whoop's 0 to 21 scale; recovery is a
# percentage. Stated here so the review can name the band without judging
# whether load and recovery "match" (the old strain/21 against recovery/100
# ratio fired for any light week and was not evidence; audit T9).
STRAIN_BANDS = [(10.0, "light"), (14.0, "moderate"), (18.0, "strenuous"), (float("inf"), "all out")]
RECOVERY_BANDS = [(34.0, "red"), (67.0, "yellow"), (float("inf"), "green")]
BANDS_NOTE = ("Bands are Whoop's published bands: strain light under 10, moderate 10 to 13.9, "
              "strenuous 14 to 17.9, all out 18 and up; recovery red under 34, yellow 34 to 66, "
              "green 67 and up. A weekly average of each is not a day by day pairing, so the "
              "review names the bands and leaves the judgement to you.")


def _band(x: float | None, bands) -> str | None:
    if x is None:
        return None
    for upper, name in bands:
        if x < upper:
            return name
    return bands[-1][1]


def _anchor(conn, end: date) -> date | None:
    """The last complete reporting day, or the store's newest daily value when
    that is older (a week of silence is shown as such, never padded with the
    partial today)."""
    rows = db.fetchall(conn, "SELECT MAX(date) FROM daily_values")
    if not rows or rows[0][0] is None:
        return None
    d = rows[0][0]
    d = d if isinstance(d, date) else date.fromisoformat(str(d))
    return min(d, end)


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _series(conn, metric, start, end):
    rows = db.fetchall(conn,
        "SELECT date, value FROM daily_values WHERE metric = ? AND date BETWEEN ? AND ? "
        "AND value IS NOT NULL ORDER BY date", [metric, start, end])
    return [(r[0], float(r[1])) for r in rows]


def _recovery_block(conn, start, end):
    """Prefer recovery_score, fall back to hrv_rmssd. Report level, trend and
    the number of days the average rests on."""
    metric = "recovery_score"
    s = _series(conn, metric, start, end)
    if not s:
        metric = "hrv_rmssd"
        s = _series(conn, metric, start, end)
    if not s:
        return {"metric": None, "avg": None, "trend": None, "n": 0, "window_days": WINDOW_DAYS, "series": []}
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
    return {"metric": metric, "avg": _avg(vals), "trend": trend, "n": len(vals),
            "window_days": WINDOW_DAYS, "first_half": first, "second_half": second,
            "series": [(str(d), round(v, 1)) for d, v in s]}


def _device_name(registry, device_key: str) -> str:
    if registry is not None:
        try:
            return registry.label(device_key)
        except Exception:  # noqa: BLE001 - a label is cosmetic
            pass
    return device_key.replace("_", " ")


def _sleep_architecture(conn, policy, start, end, registry=None):
    """Average deep, rem and light or core minutes per night, one arbitrated
    device per night via the shared stage helper (eligibility view only).
    Nights with only unstaged 'asleep' rows carry their minutes as core.

    The overall deep_min, rem_min and core_min keys are kept for callers that
    read them; by_device carries one block per device with the stage name
    that device uses ("Light" for Whoop's API record, "Core" for HealthKit
    stage rows) so the two are never one row (audit T9)."""
    from heliosd.signals.sleep_stages import nightly_stages
    nights = nightly_stages(conn, policy, start, end)
    buckets = {"deep": [], "rem": [], "core": []}
    per_device: dict[str, dict] = {}
    for st in nights.values():
        buckets["deep"].append(float(st["deep_min"]))
        buckets["rem"].append(float(st["rem_min"]))
        buckets["core"].append(float(st["light_min"]))
        blk = per_device.setdefault(st["device"], {"deep": [], "rem": [], "light": [],
                                                   "light_label": "Light" if st.get("source") == "whoop_api" else "Core"})
        blk["deep"].append(float(st["deep_min"]))
        blk["rem"].append(float(st["rem_min"]))
        blk["light"].append(float(st["light_min"]))
    by_device = [{
        "device": dev, "device_name": _device_name(registry, dev), "nights": len(b["deep"]),
        "deep_min": round(_avg(b["deep"]) or 0.0, 1), "rem_min": round(_avg(b["rem"]) or 0.0, 1),
        "light_min": round(_avg(b["light"]) or 0.0, 1), "light_label": b["light_label"],
    } for dev, b in sorted(per_device.items(), key=lambda kv: -len(kv[1]["deep"]))]
    return {
        "nights": len(nights),
        "deep_min": round(_avg(buckets["deep"]) or 0.0, 1),
        "rem_min": round(_avg(buckets["rem"]) or 0.0, 1),
        "core_min": round(_avg(buckets["core"]) or 0.0, 1),
        "by_device": by_device,
    }


def _anomalies(conn, start, end):
    rows = db.fetchdicts(conn, """
        SELECT date, metric, value, delta_pct, why FROM signals
        WHERE state = 'flag' AND date BETWEEN ? AND ? ORDER BY date DESC""",
        [start, end])
    return rows


def _testable(ins: dict) -> bool:
    """An insight the owner can act on by changing one input: a pair
    association (same day or lag 1) whose two metrics are independent. A
    pair where one metric is built from the other (Whoop's recovery from
    rMSSD, for example) is the vendor's formula and cannot be tested; a
    four week drift or a weekend rhythm has no single input to change."""
    method = str(ins.get("method") or "")
    if not (method.startswith("Spearman, ") or method.startswith("Lag-1 Spearman")):
        return False
    metrics = list(ins.get("metrics") or [])
    if len(metrics) != 2:
        return False
    try:
        from heliosd.insights.correlations import EXCLUDED_PAIRS
    except Exception:  # noqa: BLE001 - without the module every pair is testable
        return True
    return frozenset(metrics) not in EXCLUDED_PAIRS


def _experiment(recovery, sleep, insights):
    """One concrete, testable suggestion for the week."""
    for top in insights or []:
        if _testable(top):
            return (f"Test the pattern '{top['title']}'. Change one input this week and "
                    f"watch whether the relationship holds in your own numbers.")
    if sleep.get("deep_min") and sleep["deep_min"] < 60:
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


def build_weekly_review(conn, policy, today: date | None = None, registry=None) -> dict:
    """Return {markdown, data} for the seven complete days ending on the last
    complete reporting day (yesterday in the reporting zone, or the store's
    newest day when older). `today` is the reporting today (tests); the live
    value comes from the policy zone, never from the Mac's clock.

    Never raises on sparse data. If there is no data at all the markdown still
    renders with a short note under each heading.
    """
    if policy is None:
        from heliosd.trust.policy import MetricPolicy
        policy = MetricPolicy()
    if registry is None:
        try:
            from heliosd.trust.registry import SourceRegistry
            registry = SourceRegistry()
        except Exception:  # noqa: BLE001 - labels fall back to the device key
            registry = None
    end_limit = (today - timedelta(days=1)) if today else last_complete_day(policy.zone)
    anchor = _anchor(conn, end_limit)
    if anchor is None:
        md = ("# Weekly Review\n\nNo daily data is available yet. Once a few days of "
              "device data land, this review will fill in.\n")
        return {"markdown": md, "data": {"anchor": None, "window_days": WINDOW_DAYS}}

    end = anchor
    start = end - timedelta(days=WINDOW_DAYS - 1)
    recovery = _recovery_block(conn, start, end)
    sleep = _sleep_architecture(conn, policy, start, end, registry)
    strain_series = _series(conn, "strain", start, end)
    strain = _avg([v for _, v in strain_series])
    rec_avg = recovery["avg"]
    anomalies = _anomalies(conn, start, end)

    insights = []
    try:
        from heliosd.insights.correlations import top_insights
        insights = top_insights(conn, days=90, policy=policy, today=end + timedelta(days=1))
    except Exception:  # noqa: BLE001 - the review stands without patterns
        insights = []

    experiment = _experiment(recovery, sleep, insights)
    strain_band = _band(strain, STRAIN_BANDS)
    recovery_band = _band(rec_avg, RECOVERY_BANDS) if recovery["metric"] == "recovery_score" else None

    data = {
        "anchor": str(anchor), "start": str(start), "end": str(end), "window_days": WINDOW_DAYS,
        "recovery": recovery, "sleep": sleep,
        "strain_avg": round(strain, 1) if strain is not None else None, "strain_n": len(strain_series),
        "strain_band": strain_band, "recovery_band": recovery_band,
        "anomalies": anomalies, "insights": insights, "experiment": experiment,
    }

    lines = []
    lines.append(f"# Weekly Review, {start} to {end}")
    lines.append("")
    lines.append(f"Seven complete days ending {end}. Today is still in progress and is not included.")
    lines.append("")

    # Recovery trend
    lines.append("## Recovery trend")
    if recovery["metric"]:
        label = "recovery score" if recovery["metric"] == "recovery_score" else "HRV (rMSSD)"
        trend = recovery["trend"] or "flat"
        n = recovery["n"]
        lines.append(f"{WINDOW_DAYS}-day average {label} ({n} day{'s' if n != 1 else ''} with data): "
                     f"{round(recovery['avg'], 1)}. The trend across the week is {trend}.")
    else:
        lines.append("No recovery or HRV data landed this week.")
    lines.append("")

    # Sleep architecture
    lines.append("## Sleep architecture")
    if sleep["nights"]:
        for blk in sleep["by_device"]:
            n = blk["nights"]
            lines.append(f"{blk['device_name']}, averaged over {n} night{'s' if n != 1 else ''} with stage data:")
            lines.append("")
            lines.append(_md_table(
                ["Stage", "Avg minutes per night"],
                [[f"Deep ({blk['device_name']})", blk["deep_min"]],
                 [f"REM ({blk['device_name']})", blk["rem_min"]],
                 [f"{blk['light_label']} ({blk['device_name']})", blk["light_min"]]]))
            lines.append("")
        if len(sleep["by_device"]) > 1:
            lines.append("Whoop reports light sleep and HealthKit devices report core sleep; "
                         "they are different definitions, so they are listed apart.")
    else:
        lines.append("No sleep stage data this week.")
    lines.append("")

    # Strain and recovery in Whoop's bands
    lines.append("## Strain versus recovery")
    if strain is not None or rec_avg is not None:
        rows = []
        if strain is not None:
            rows.append(["Strain", round(strain, 1), f"{len(strain_series)} days", strain_band or "n/a"])
        if rec_avg is not None and recovery["metric"] == "recovery_score":
            rows.append(["Recovery", round(rec_avg, 1), f"{recovery['n']} days", recovery_band or "n/a"])
        lines.append(_md_table(["Measure", f"{WINDOW_DAYS}-day average", "Days with data", "Whoop band"], rows))
        lines.append("")
        lines.append(BANDS_NOTE)
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
