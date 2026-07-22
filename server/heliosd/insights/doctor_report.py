"""Single page, print ready doctor report.

Produces one self contained HTML string: inline CSS only, a Montserrat font
stack, dark text on a light background, sized for A4. No external assets and no
JavaScript, so it prints identically from any browser and can be handed to a
clinician. This is a summary for a conversation, not a diagnosis, which the
footer makes explicit.
"""

from __future__ import annotations

import html
from datetime import date, timedelta

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


def _anchor(conn) -> date | None:
    rows = db.fetchall(conn, "SELECT MAX(date) FROM daily_values")
    if not rows or rows[0][0] is None:
        return None
    d = rows[0][0]
    return d if isinstance(d, date) else date.fromisoformat(str(d))


def _median(xs):
    xs = sorted(v for v in xs if v is not None)
    n = len(xs)
    if not n:
        return None
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def _fmt(v):
    if v is None:
        return "n/a"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:.1f}"


def _vitals_rows(conn, start, end):
    rows = []
    for metric, label, unit in _VITALS:
        recs = db.fetchall(conn,
            "SELECT date, value, device_key FROM daily_values "
            "WHERE metric = ? AND date BETWEEN ? AND ? AND value IS NOT NULL ORDER BY date",
            [metric, start, end])
        if not recs:
            continue
        latest = recs[-1]
        med = _median([r[1] for r in recs])
        rows.append({
            "label": label, "unit": unit,
            "latest": _fmt(latest[1]), "median": _fmt(med),
            "device": str(latest[2] or "").replace("_", " "),
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


def _sleep_activity(conn, start, end):
    arch = db.fetchall(conn, """
        SELECT text_value, SUM(value), COUNT(DISTINCT CAST(end_ts AS DATE))
        FROM samples WHERE metric = 'sleep_analysis'
          AND text_value IN ('deep', 'rem', 'core')
          AND CAST(end_ts AS DATE) BETWEEN ? AND ? GROUP BY text_value""",
        [start, end])
    per_night = {}
    for stage, total, nights in arch:
        if nights:
            per_night[stage] = round(float(total) / nights, 0)
    steps = db.fetchall(conn,
        "SELECT AVG(value) FROM daily_values WHERE metric = 'steps' AND date BETWEEN ? AND ?",
        [start, end])
    avg_steps = steps[0][0] if steps and steps[0][0] is not None else None
    return {
        "deep": per_night.get("deep"), "rem": per_night.get("rem"),
        "core": per_night.get("core"), "avg_steps": avg_steps,
    }


def _esc(x) -> str:
    return html.escape(str(x), quote=True)


def build_doctor_report_html(conn, owner_name: str) -> str:
    """Return a complete, standalone HTML document as a single string."""
    anchor = _anchor(conn) or date.today()
    start = anchor - timedelta(days=29)
    vitals = _vitals_rows(conn, start, anchor)
    labs = _labs_rows(conn)
    sa = _sleep_activity(conn, start, anchor)
    name = _esc(owner_name)

    vital_tr = "".join(
        f"<tr><td>{_esc(v['label'])}</td><td class='num'>{_esc(v['latest'])}</td>"
        f"<td class='num'>{_esc(v['median'])}</td><td>{_esc(v['unit'])}</td>"
        f"<td class='dev'>{_esc(v['device'])}</td></tr>"
        for v in vitals) or "<tr><td colspan='5'>No vitals recorded in this window.</td></tr>"

    lab_tr = "".join(
        f"<tr><td>{_esc(l['biomarker'])}</td><td class='num'>{_esc(l['value'])}</td>"
        f"<td>{_esc(l['unit'])}</td><td>{_esc(l['ref'])}</td>"
        f"<td class='{'flag' if l['flag'] else ''}'>{_esc(l['flag'])}</td>"
        f"<td class='dev'>{_esc(l['date'])}</td></tr>"
        for l in labs) or "<tr><td colspan='6'>No labs on file.</td></tr>"

    def sv(x):
        return f"{x:.0f}" if x is not None else "n/a"

    steps_txt = f"{sa['avg_steps']:,.0f}" if sa["avg_steps"] is not None else "n/a"

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
    <div class="meta">{name} &middot; {_esc(start)} to {_esc(anchor)} &middot; 30 day window</div>
  </header>

  <h2>Vitals summary</h2>
  <table>
    <thead><tr><th>Metric</th><th class="num">Latest</th><th class="num">30 day median</th>
      <th>Unit</th><th>Device</th></tr></thead>
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
    <p>Average daily steps: {steps_txt}.</p>
  </div>

  <footer>Generated locally by Helios. Not a medical document.</footer>
</div>
</body>
</html>"""
