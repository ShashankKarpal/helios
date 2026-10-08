"""Single page, print ready doctor report.

Produces one self contained HTML string: inline CSS only, a Montserrat font
stack, dark text on a light background, sized for A4. No external assets and no
JavaScript, so it prints identically from any browser and can be handed to a
clinician. This is a summary for a conversation, not a diagnosis, which the
footer makes explicit.
"""

from __future__ import annotations

import html
import statistics
from datetime import date, timedelta

from heliosd.ingest.normalize import last_complete_day
from heliosd.store import db

# Metrics shown in the vitals table, in clinical reading order.
_VITALS = [
    ("resting_hr", "Resting heart rate", "bpm"),
    ("hrv_rmssd", "HRV (rMSSD)", "ms"),
    ("respiratory_rate", "Respiratory rate", "breaths/min"),
    ("spo2", "Blood oxygen", "%"),
    ("sleep_duration", "Sleep duration", "h"),
    ("body_mass", "Body mass", "kg"),
    ("wrist_temp", "Wrist temperature", "C"),
    ("steps", "Daily steps", "count"),
]


def _anchor(conn, end: date) -> date | None:
    """The last complete reporting day (owner decision D7), or the store's
    newest daily value when that is older. The partial today is never the
    anchor of a document meant for a clinician (audit T11)."""
    rows = db.fetchall(conn, "SELECT MAX(date) FROM daily_values")
    if not rows or rows[0][0] is None:
        return None
    d = rows[0][0]
    d = d if isinstance(d, date) else date.fromisoformat(str(d))
    return min(d, end)


def _median(xs):
    xs = [v for v in xs if v is not None]
    return statistics.median(xs) if xs else None


def _fmt(v, unit: str | None = None):
    if v is None:
        return "n/a"
    if unit == "count" or abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:.1f}"


def _owner_device(policy, metric: str) -> str | None:
    pr = policy.priority(metric) if policy is not None else []
    return pr[0] if pr else None


def _device_name(registry, device_key: str | None) -> str:
    if not device_key:
        return ""
    if registry is not None:
        try:
            return registry.label(device_key)
        except Exception:  # noqa: BLE001 - a label is cosmetic
            pass
    return device_key.replace("_", " ")


def _vitals_rows(conn, policy, start, end, registry=None):
    """One row per vital from the metric's OWNER device only (the first device
    of its priority list), with the date of the latest value and the number of
    days behind the median. A row never mixes devices: a fallback day from
    another device is left out rather than averaged in (audit T11)."""
    rows = []
    for metric, label, unit in _VITALS:
        owner = _owner_device(policy, metric)
        if owner is None:
            latest_any = db.fetchall(conn,
                "SELECT device_key FROM daily_values WHERE metric = ? AND date BETWEEN ? AND ? "
                "AND value IS NOT NULL ORDER BY date DESC LIMIT 1", [metric, start, end])
            owner = latest_any[0][0] if latest_any else None
        if owner is None:
            continue
        recs = db.fetchall(conn,
            "SELECT date, value FROM daily_values "
            "WHERE metric = ? AND device_key = ? AND date BETWEEN ? AND ? AND value IS NOT NULL ORDER BY date",
            [metric, owner, start, end])
        others = db.fetchall(conn,
            "SELECT COUNT(*) FROM daily_values WHERE metric = ? AND device_key <> ? AND date BETWEEN ? AND ? "
            "AND value IS NOT NULL", [metric, owner, start, end])[0][0]
        if not recs and not others:
            continue
        latest = recs[-1] if recs else None
        med = _median([r[1] for r in recs])
        rows.append({
            "label": label, "unit": unit,
            "latest": _fmt(latest[1], unit) if latest else "n/a",
            "latest_date": str(latest[0]) if latest else "",
            "median": _fmt(med, unit), "n": len(recs),
            "device": _device_name(registry, owner),
        })
    return rows


def _labs_rows(conn):
    recs = db.fetchdicts(conn,
        "SELECT panel_date, biomarker, value, unit, ref_low, ref_high "
        "FROM labs ORDER BY panel_date DESC, biomarker LIMIT 40")
    out = []
    for r in recs:
        lo, hi, val = r.get("ref_low"), r.get("ref_high"), r.get("value")
        flag = ""
        if val is not None:
            if lo is not None and val < lo:
                flag = "Low"
            elif hi is not None and val > hi:
                flag = "High"
        if lo is not None and hi is not None:
            ref = f"{_fmt(lo)} to {_fmt(hi)}"
        elif hi is not None:
            ref = f"< {_fmt(hi)}"
        elif lo is not None:
            ref = f"> {_fmt(lo)}"
        else:
            ref = "n/a"
        out.append({
            "date": str(r["panel_date"]), "biomarker": r["biomarker"],
            "value": _fmt(val), "unit": r.get("unit") or "",
            "ref": ref, "flag": flag,
        })
    return out


def _sleep_activity(conn, policy, start, end):
    """Per-night stage averages from ONE arbitrated device per night (shared
    stage helper over the eligibility view), never summed across devices."""
    from heliosd.signals.sleep_stages import nightly_stages
    nights = nightly_stages(conn, policy, start, end)
    per_night = {}
    if nights:
        n = len(nights)
        per_night = {"deep": round(sum(s["deep_min"] for s in nights.values()) / n, 0),
                     "rem": round(sum(s["rem_min"] for s in nights.values()) / n, 0),
                     "core": round(sum(s["light_min"] for s in nights.values()) / n, 0)}
    # Steps from the owner device only, complete days only, with the count.
    owner = _owner_device(policy, "steps")
    if owner:
        steps = db.fetchall(conn,
            "SELECT AVG(value), COUNT(*) FROM daily_values WHERE metric = 'steps' AND device_key = ? "
            "AND date BETWEEN ? AND ? AND value IS NOT NULL", [owner, start, end])
    else:
        steps = db.fetchall(conn,
            "SELECT AVG(value), COUNT(*) FROM daily_values WHERE metric = 'steps' AND date BETWEEN ? AND ? "
            "AND value IS NOT NULL", [start, end])
    avg_steps = steps[0][0] if steps and steps[0][0] is not None else None
    return {
        "deep": per_night.get("deep"), "rem": per_night.get("rem"),
        "core": per_night.get("core"), "avg_steps": avg_steps,
        "steps_n": int(steps[0][1]) if steps and steps[0][1] else 0, "steps_device": owner,
    }


def _esc(x) -> str:
    return html.escape(str(x), quote=True)


def build_doctor_report_html(conn, owner_name: str, policy=None, today: date | None = None,
                             registry=None) -> str:
    """Return a complete, standalone HTML document as a single string. The 30
    day window ends on the last complete reporting day (`today` is the
    reporting today, for tests; live it comes from the policy zone)."""
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
    anchor = _anchor(conn, end_limit) or end_limit
    start = anchor - timedelta(days=29)
    vitals = _vitals_rows(conn, policy, start, anchor, registry)
    labs = _labs_rows(conn)
    sa = _sleep_activity(conn, policy, start, anchor)
    name = _esc(owner_name)

    vital_tr = "".join(
        f"<tr><td>{_esc(v['label'])}</td><td class='num'>{_esc(v['latest'])}</td>"
        f"<td class='dev'>{_esc(v['latest_date'])}</td>"
        f"<td class='num'>{_esc(v['median'])}</td><td class='num'>{v['n']}</td><td>{_esc(v['unit'])}</td>"
        f"<td class='dev'>{_esc(v['device'])}</td></tr>"
        for v in vitals) or "<tr><td colspan='7'>No vitals recorded in this window.</td></tr>"

    lab_tr = "".join(
        f"<tr><td>{_esc(l['biomarker'])}</td><td class='num'>{_esc(l['value'])}</td>"
        f"<td>{_esc(l['unit'])}</td><td>{_esc(l['ref'])}</td>"
        f"<td class='{'flag' if l['flag'] else ''}'>{_esc(l['flag'])}</td>"
        f"<td class='dev'>{_esc(l['date'])}</td></tr>"
        for l in labs) or "<tr><td colspan='6'>No labs on file.</td></tr>"

    def sv(x):
        return f"{x:.0f}" if x is not None else "n/a"

    steps_txt = f"{sa['avg_steps']:,.0f}" if sa["avg_steps"] is not None else "n/a"
    steps_dev = _device_name(registry, sa.get("steps_device")) or "all devices"
    steps_n = sa.get("steps_n", 0)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Health Summary, {name}</title>
<style>
  @page {{ size: A4; margin: 16mm; }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: Montserrat, "Helvetica Neue", Arial, sans-serif;
    color: #1f2933; background: #ffffff; margin: 0;
    font-size: 12px; line-height: 1.45;
  }}
  .page {{ max-width: 800px; margin: 0 auto; padding: 8px; }}
  header {{ border-bottom: 2px solid #1f2933; padding-bottom: 10px; margin-bottom: 16px; }}
  header h1 {{ font-size: 20px; margin: 0 0 2px 0; letter-spacing: 0.3px; }}
  header .meta {{ color: #52606d; font-size: 11px; }}
  h2 {{ font-size: 13px; text-transform: uppercase; letter-spacing: 0.6px;
        color: #323f4b; border-bottom: 1px solid #cbd2d9; padding-bottom: 4px;
        margin: 18px 0 8px 0; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 6px; }}
  th, td {{ text-align: left; padding: 5px 8px; border-bottom: 1px solid #e4e7eb; }}
  th {{ font-size: 10px; text-transform: uppercase; letter-spacing: 0.4px;
        color: #616e7c; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.dev {{ color: #7b8794; font-size: 10.5px; }}
  td.flag {{ color: #ab091e; font-weight: 600; }}
  .summary p {{ margin: 4px 0; }}
  footer {{ margin-top: 22px; padding-top: 8px; border-top: 1px solid #cbd2d9;
            color: #7b8794; font-size: 10px; text-align: center; }}
</style>
</head>
<body>
<div class="page">
  <header>
    <h1>Health Summary</h1>
    <div class="meta">{name} &middot; {_esc(start)} to {_esc(anchor)} &middot; 30 day window of complete days</div>
  </header>

  <h2>Vitals summary</h2>
  <table>
    <thead><tr><th>Metric</th><th class="num">Latest</th><th>Date</th><th class="num">30 day median</th>
      <th class="num">Days</th><th>Unit</th><th>Device</th></tr></thead>
    <tbody>{vital_tr}</tbody>
  </table>

  <h2>Recent labs</h2>
  <table>
    <thead><tr><th>Biomarker</th><th class="num">Value</th><th>Unit</th>
      <th>Reference</th><th>Flag</th><th>Panel date</th></tr></thead>
    <tbody>{lab_tr}</tbody>
  </table>

  <h2>Sleep and activity</h2>
  <div class="summary">
    <p>Average sleep stages per night over the window: deep {sv(sa['deep'])} min,
       REM {sv(sa['rem'])} min, core {sv(sa['core'])} min.</p>
    <p>Average daily steps ({_esc(steps_dev)}, {steps_n} complete day{"s" if steps_n != 1 else ""}): {steps_txt}.</p>
  </div>

  <footer>Generated locally by Helios. Not a medical document.</footer>
</div>
</body>
</html>"""
