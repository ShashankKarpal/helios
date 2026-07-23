"""Correlation and effect discovery across the owner's daily metrics and logged
events.

Two families of tests are run over a trailing window:

  1. Metric versus metric association, using Spearman rank correlation. This is
     robust to outliers and to non linear but monotone relationships.
  2. Event day versus non event day comparison, using the Mann-Whitney U test.
     For example: nights following a late caffeine dose versus nights following
     no dose, compared on sleep_duration or next day hrv_rmssd.

All p values are corrected together with Benjamini-Hochberg FDR so that running
many tests does not inflate false positives. Only associations that clear
q < 0.10 (and, for correlations, an effect size floor of rho >= 0.30) are
surfaced. Language is deliberately cautious: we report associations, never
causation.

The module imports without scipy. When scipy is missing we fall back to a self
contained rank correlation and a normal approximation Mann-Whitney, so the
functions still return sensible output rather than raising.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import date, timedelta

from heliosd.store import db

try:  # optional dependency group 'insights'
    from scipy import stats as _scipy_stats  # type: ignore

    HAVE_SCIPY = True
except Exception:  # pragma: no cover - exercised on machines without scipy
    _scipy_stats = None
    HAVE_SCIPY = False


# Tunables. Kept module level so callers and tests can reason about them.
MIN_PAIRED_DAYS = 12
MIN_GROUP = 5
RHO_FLOOR = 0.30
FDR_Q = 0.10
EARLY_Q = 0.25      # near-threshold findings surface as clearly labeled hints
MAX_EARLY = 3
MAX_INSIGHTS = 10

# Directed lag-1 hypotheses: does yesterday's A relate to today's B? These are
# the questions a coach would actually ask (load vs next-day recovery, movement
# vs that night's sleep). Curated so the FDR pool is not polluted with noise.
LAG1_PAIRS = [
    ("strain", "recovery_score"),
    ("strain", "hrv_rmssd"),
    ("strain", "resting_hr"),
    ("steps", "sleep_duration"),
    ("active_energy", "sleep_duration"),
]

# Metrics worth testing for weekend vs weekday rhythm differences.
WEEKEND_TARGETS = ["sleep_duration", "recovery_score", "steps", "strain"]

# Metrics watched for a monotonic drift over the trailing four weeks.
TREND_TARGETS = ["resting_hr", "hrv_rmssd", "sleep_duration", "body_mass", "steps"]
TREND_WINDOW_DAYS = 28

# Pairs the owner's policy forbids comparing (rMSSD and SDNN are different
# measures and are never blended, charted together, or correlated).
EXCLUDED_PAIRS = {frozenset(("hrv_rmssd", "hrv_sdnn"))}

# Readable labels for metric ids. Anything not listed is de-underscored.
_LABELS = {
    "hrv_rmssd": "HRV (rMSSD)",
    "hrv_sdnn": "HRV (SDNN)",
    "resting_hr": "resting heart rate",
    "sleep_duration": "sleep duration",
    "recovery_score": "recovery score",
    "strain": "strain",
    "respiratory_rate": "respiratory rate",
    "spo2": "blood oxygen",
    "steps": "steps",
    "glucose": "glucose",
    "body_mass": "body mass",
    "wrist_temp": "wrist temperature",
    "dietary_energy": "dietary energy",
}


def _label(metric: str) -> str:
    return _LABELS.get(metric, metric.replace("_", " "))


def _cap(text: str) -> str:
    """Sentence-case that preserves interior capitals: 'HRV (rMSSD) and...'
    stays intact instead of str.capitalize() flattening it to 'Hrv (rmssd)'."""
    return text[:1].upper() + text[1:] if text else text


# --------------------------------------------------------------------------
# Self contained statistics (used as scipy fallback, and for FDR always).
# --------------------------------------------------------------------------

def _norm_sf(x: float) -> float:
    """Upper tail of the standard normal, via the complementary error function."""
    return 0.5 * math.erfc(x / math.sqrt(2.0))


def _rankdata(values: list[float]) -> list[float]:
    """Average ranks, ties shared. Mirrors scipy.stats.rankdata default."""
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(x: list[float], y: list[float]) -> float:
    n = len(x)
    if n == 0:
        return 0.0
    mx = sum(x) / n
    my = sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return 0.0
    return sxy / math.sqrt(sxx * syy)


def _spearman(x: list[float], y: list[float]) -> tuple[float, float]:
    """Return (rho, p). Uses scipy when present, otherwise a Fisher z p value."""
    n = len(x)
    if n < 3:
        return 0.0, 1.0
    if HAVE_SCIPY:
        rho, p = _scipy_stats.spearmanr(x, y)
        if rho != rho:  # NaN guard (constant input)
            return 0.0, 1.0
        return float(rho), float(p)
    rho = _pearson(_rankdata(x), _rankdata(y))
    rho = max(min(rho, 0.999999), -0.999999)
    z = math.atanh(rho) * math.sqrt(n - 3) if n > 3 else 0.0
    return rho, 2.0 * _norm_sf(abs(z))


def _mannwhitney(a: list[float], b: list[float]) -> tuple[float, float]:
    """Return (U, p) for a two sided comparison of the two samples."""
    na, nb = len(a), len(b)
    if na == 0 or nb == 0:
        return 0.0, 1.0
    if HAVE_SCIPY:
        try:
            u, p = _scipy_stats.mannwhitneyu(a, b, alternative="two-sided")
            return float(u), float(p)
        except ValueError:
            return 0.0, 1.0
    combined = [(v, 0) for v in a] + [(v, 1) for v in b]
    vals = [v for v, _ in combined]
    ranks = _rankdata(vals)
    rank_a = sum(r for r, (_, g) in zip(ranks, combined) if g == 0)
    u1 = rank_a - na * (na + 1) / 2.0
    n = na + nb
    mu = na * nb / 2.0
    tie = sum(t ** 3 - t for t in Counter(vals).values())
    var = na * nb / 12.0 * ((n + 1) - tie / (n * (n - 1)))
    if var <= 0:
        return u1, 1.0
    z = (u1 - mu) / math.sqrt(var)
    return u1, 2.0 * _norm_sf(abs(z))


def _bh_fdr(pvals: list[float]) -> list[float]:
    """Benjamini-Hochberg adjusted p values (q values), original order."""
    n = len(pvals)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: pvals[i])
    q = [0.0] * n
    prev = 1.0
    for rank in range(n - 1, -1, -1):
        i = order[rank]
        val = pvals[i] * n / (rank + 1)
        prev = min(prev, val)
        q[i] = min(prev, 1.0)
    return q


# --------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------

def _daily_metrics(conn, since: date) -> list[str]:
    rows = db.fetchall(conn,
        "SELECT DISTINCT metric FROM daily_values WHERE date >= ? AND value IS NOT NULL",
        [since])
    return sorted(r[0] for r in rows)


def _metric_series(conn, metric: str, since: date) -> dict:
    rows = db.fetchall(conn,
        "SELECT date, value FROM daily_values WHERE metric = ? AND date >= ? AND value IS NOT NULL",
        [metric, since])
    out: dict = {}
    for d, v in rows:
        out[d if isinstance(d, date) else date.fromisoformat(str(d))] = float(v)
    return out


def _event_days(conn, kind: str, since: date, hour_min: int | None = None) -> set:
    rows = db.fetchall(conn,
        "SELECT ts FROM events WHERE kind = ? AND CAST(ts AS DATE) >= ?", [kind, since])
    days = set()
    for (ts,) in rows:
        if hour_min is not None and ts.hour < hour_min:
            continue
        days.add(ts.date())
    return days


# --------------------------------------------------------------------------
# Test builders. Each returns a raw record with a p value; FDR is applied once
# across all records so the multiple comparison correction is honest.
# --------------------------------------------------------------------------

def _correlation_tests(conn, since: date) -> list[dict]:
    metrics = _daily_metrics(conn, since)
    series = {m: _metric_series(conn, m, since) for m in metrics}
    out: list[dict] = []
    for i in range(len(metrics)):
        for j in range(i + 1, len(metrics)):
            ma, mb = metrics[i], metrics[j]
            if frozenset((ma, mb)) in EXCLUDED_PAIRS:
                continue
            sa, sb = series[ma], series[mb]
            common = sorted(set(sa) & set(sb))
            if len(common) < MIN_PAIRED_DAYS:
                continue
            x = [sa[d] for d in common]
            y = [sb[d] for d in common]
            rho, p = _spearman(x, y)
            out.append({
                "family": "correlation", "a": ma, "b": mb,
                "rho": rho, "p": p, "n": len(common),
            })
    return out


def _lag_tests(conn, since: date) -> list[dict]:
    """Yesterday's A versus today's B, Spearman over aligned day pairs."""
    out: list[dict] = []
    for a, b in LAG1_PAIRS:
        sa = _metric_series(conn, a, since - timedelta(days=1))
        sb = _metric_series(conn, b, since)
        if not sa or not sb:
            continue
        days = sorted(d for d in sb if (d - timedelta(days=1)) in sa)
        if len(days) < MIN_PAIRED_DAYS:
            continue
        x = [sa[d - timedelta(days=1)] for d in days]
        y = [sb[d] for d in days]
        rho, p = _spearman(x, y)
        out.append({"family": "lag", "a": a, "b": b, "rho": rho, "p": p, "n": len(days)})
    return out


def _weekend_tests(conn, since: date) -> list[dict]:
    """Weekend versus weekday distribution differences, Mann-Whitney."""
    out: list[dict] = []
    for metric in WEEKEND_TARGETS:
        s = _metric_series(conn, metric, since)
        weekend = [v for d, v in s.items() if d.weekday() >= 5]
        weekday = [v for d, v in s.items() if d.weekday() < 5]
        if len(weekend) < MIN_GROUP or len(weekday) < MIN_GROUP:
            continue
        med_we, med_wd = _median(weekend), _median(weekday)
        if med_wd and abs(med_we - med_wd) / abs(med_wd) < 0.05:
            continue  # under 5% difference: not worth surfacing even if significant
        u, p = _mannwhitney(weekend, weekday)
        out.append({"family": "weekend", "metric": metric, "p": p,
                    "median_weekend": med_we, "median_weekday": med_wd,
                    "n": len(weekend) + len(weekday),
                    "n_weekend": len(weekend), "n_weekday": len(weekday)})
    return out


def _trend_tests(conn, anchor: date) -> list[dict]:
    """Monotonic drift over the trailing four weeks: Spearman against time,
    with a Theil-Sen (median pairwise) slope for an honest per-week rate."""
    out: list[dict] = []
    since = anchor - timedelta(days=TREND_WINDOW_DAYS)
    for metric in TREND_TARGETS:
        s = _metric_series(conn, metric, since)
        days = sorted(s)
        if len(days) < MIN_PAIRED_DAYS:
            continue
        xs = [(d - since).days for d in days]
        ys = [s[d] for d in days]
        rho, p = _spearman([float(x) for x in xs], ys)
        slopes = [
            (ys[j] - ys[i]) / (xs[j] - xs[i])
            for i in range(len(xs)) for j in range(i + 1, len(xs))
            if xs[j] != xs[i]
        ]
        slope_week = _median(slopes) * 7 if slopes else 0.0
        out.append({"family": "trend", "metric": metric, "rho": rho, "p": p,
                    "slope_per_week": slope_week, "n": len(days)})
    return out


def _event_effect_tests(conn, since: date) -> list[dict]:
    # (kind, late hour threshold). None means any dose that day counts as exposure.
    exposures = [("caffeine", 15), ("alcohol", 12)]
    targets = ["sleep_duration", "hrv_rmssd", "resting_hr", "recovery_score"]
    out: list[dict] = []
    for kind, hour_min in exposures:
        late_days = _event_days(conn, kind, since - timedelta(days=1), hour_min=hour_min)
        any_days = _event_days(conn, kind, since - timedelta(days=1), hour_min=None)
        if not late_days:
            continue
        for metric in targets:
            s = _metric_series(conn, metric, since)
            if not s:
                continue
            exposed, control = [], []
            for d, v in s.items():
                prior = d - timedelta(days=1)
                if prior in late_days:
                    exposed.append(v)
                elif prior not in any_days:
                    control.append(v)
            if len(exposed) < MIN_GROUP or len(control) < MIN_GROUP:
                continue
            u, p = _mannwhitney(exposed, control)
            med_e = _median(exposed)
            med_c = _median(control)
            out.append({
                "family": "event", "kind": kind, "metric": metric,
                "hour_min": hour_min, "U": u, "p": p,
                "median_exposed": med_e, "median_control": med_c,
                "delta": med_e - med_c,
                "n": len(exposed) + len(control),
                "n_exposed": len(exposed), "n_control": len(control),
            })
    return out


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return 0.0
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


# --------------------------------------------------------------------------
# Verdict language. Grounded, cautious, never causal.
# --------------------------------------------------------------------------

def _verdict(q: float) -> str:
    if q < 0.01:
        return "Strong signal, very unlikely to be chance"
    if q < 0.05:
        return "Looks real, not a fluke"
    if q < FDR_Q:
        return "Probably real, worth keeping an eye on"
    return "Early hint, needs more data"


def _corr_insight(rec: dict, q: float) -> dict:
    a, b, rho = rec["a"], rec["b"], rec["rho"]
    direction = "move together" if rho >= 0 else "move in opposite directions"
    title = f"{_cap(_label(a))} and {_label(b)} {direction}"
    detail = (
        f"Across {rec['n']} days with both values, {_label(a)} and {_label(b)} "
        f"{direction} (Spearman rho {rho:+.2f}). This is an association in your own "
        f"data, not proof that one causes the other."
    )
    return {
        "title": title, "detail": detail,
        "method": "Spearman, BH-FDR", "verdict": _verdict(q),
        "stat": f"rho = {rho:+.2f}, q = {q:.3f}", "n": rec["n"],
        "_sort": (q, -abs(rho)),
    }


def _lag_insight(rec: dict, q: float) -> dict:
    a, b, rho = rec["a"], rec["b"], rec["rho"]
    higher_lower = ("higher", "higher") if rho >= 0 else ("higher", "lower")
    title = _cap(f"{higher_lower[0]} {_label(a)} yesterday, "
                 f"{higher_lower[1]} {_label(b)} today")
    detail = (
        f"Across {rec['n']} day pairs, a {higher_lower[0]} {_label(a)} on one day "
        f"lines up with {higher_lower[1]} {_label(b)} the next day "
        f"(Spearman rho {rho:+.2f} at lag 1). Association in your own data, "
        f"not proof of cause."
    )
    return {
        "title": title, "detail": detail,
        "method": "Lag-1 Spearman, BH-FDR", "verdict": _verdict(q),
        "stat": f"rho = {rho:+.2f}, q = {q:.3f}", "n": rec["n"],
        "_sort": (q, -abs(rho)),
    }


def _weekend_insight(rec: dict, q: float) -> dict:
    metric = rec["metric"]
    we, wd = rec["median_weekend"], rec["median_weekday"]
    more_less = "higher" if we > wd else "lower"
    pct = abs(we - wd) / abs(wd) * 100 if wd else 0.0
    title = f"Your {_label(metric)} runs {more_less} on weekends"
    detail = (
        f"Weekend {_label(metric)} is typically {more_less} than weekdays "
        f"(median {we:.2f} versus {wd:.2f}, about {pct:.0f}% different), over "
        f"{rec['n_weekend']} weekend and {rec['n_weekday']} weekday days. "
        f"A rhythm in your own data, worth designing around."
    )
    return {
        "title": title, "detail": detail,
        "method": "Mann-Whitney U, BH-FDR", "verdict": _verdict(q),
        "stat": f"delta = {we - wd:+.2f}, q = {q:.3f}", "n": rec["n"],
        "_sort": (q, -pct),
    }


def _trend_insight(rec: dict, q: float) -> dict:
    metric, slope = rec["metric"], rec["slope_per_week"]
    direction = "drifting up" if rec["rho"] > 0 else "drifting down"
    title = f"{_cap(_label(metric))} has been {direction} for four weeks"
    detail = (
        f"Over the last {TREND_WINDOW_DAYS} days, {_label(metric)} shows a steady "
        f"{direction.split()[-1]}ward drift of about {abs(slope):.2f} per week "
        f"(Theil-Sen slope, Spearman rho {rec['rho']:+.2f} against time, "
        f"{rec['n']} days). Trends this persistent are usually worth a look."
    )
    return {
        "title": title, "detail": detail,
        "method": "Spearman vs time + Theil-Sen, BH-FDR", "verdict": _verdict(q),
        "stat": f"slope/week = {slope:+.2f}, q = {q:.3f}", "n": rec["n"],
        "_sort": (q, -abs(rec["rho"])),
    }


def _event_insight(rec: dict, q: float) -> dict:
    metric, kind, delta = rec["metric"], rec["kind"], rec["delta"]
    lower = "lower" if delta < 0 else "higher"
    when = "a late" if rec.get("hour_min") else "a"
    subject = "caffeine dose" if kind == "caffeine" else f"{kind} intake"
    title = f"Nights after {when} {subject} show {lower} {_label(metric)}"
    detail = (
        f"On days following {when} {subject}, {_label(metric)} was typically {lower} "
        f"(median {rec['median_exposed']:.2f} versus {rec['median_control']:.2f} on "
        f"clean days). Compared over {rec['n_exposed']} exposed and {rec['n_control']} "
        f"control days. Association only, other factors may be involved."
    )
    return {
        "title": title, "detail": detail,
        "method": "Mann-Whitney U, BH-FDR", "verdict": _verdict(q),
        "stat": f"delta = {delta:+.2f}, q = {q:.3f}", "n": rec["n"],
        "_sort": (q, -abs(delta)),
    }


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def top_insights(conn, days: int = 90) -> list[dict]:
    """Discover the strongest associations over the trailing window.

    Returns a list of dicts: title, detail, method, verdict, stat, n. Capped at
    roughly eight, strongest first, symmetric metric pairs de-duplicated. Never
    raises on sparse data: an empty list simply means nothing cleared the bar.
    """
    try:
        anchor = _anchor_date(conn)
        if anchor is None:
            return []
        since = anchor - timedelta(days=days)
        records = (_correlation_tests(conn, since) + _lag_tests(conn, since)
                   + _weekend_tests(conn, since) + _trend_tests(conn, anchor)
                   + _event_effect_tests(conn, since))
        if not records:
            return []
        qvals = _bh_fdr([r["p"] for r in records])
        # SDNN is the spot-check HRV; when the rMSSD anchor already qualifies
        # against the same partner, the SDNN version reads as a duplicate
        # insight and is suppressed.
        rmssd_partners = {
            (rec["b"] if rec["a"] == "hrv_rmssd" else rec["a"])
            for rec, q in zip(records, qvals)
            if rec["family"] == "correlation" and q < EARLY_Q
            and "hrv_rmssd" in (rec["a"], rec["b"])
            and abs(rec.get("rho", 0)) >= RHO_FLOOR
        }
        confirmed: list[dict] = []
        early: list[dict] = []
        seen_pairs: set = set()
        for rec, q in zip(records, qvals):
            if q >= EARLY_Q:
                continue
            fam = rec["family"]
            if fam == "correlation":
                if abs(rec["rho"]) < RHO_FLOOR:
                    continue
                if "hrv_sdnn" in (rec["a"], rec["b"]):
                    partner = rec["b"] if rec["a"] == "hrv_sdnn" else rec["a"]
                    if partner in rmssd_partners:
                        continue
                key = frozenset((rec["a"], rec["b"]))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                built = _corr_insight(rec, q)
            elif fam == "lag":
                if abs(rec["rho"]) < RHO_FLOOR:
                    continue
                built = _lag_insight(rec, q)
            elif fam == "weekend":
                built = _weekend_insight(rec, q)
            elif fam == "trend":
                if abs(rec["rho"]) < RHO_FLOOR:
                    continue
                built = _trend_insight(rec, q)
            else:
                built = _event_insight(rec, q)
            (confirmed if q < FDR_Q else early).append(built)
        confirmed.sort(key=lambda d: d["_sort"])
        early.sort(key=lambda d: d["_sort"])
        insights = confirmed + early[:MAX_EARLY]
        for d in insights:
            d.pop("_sort", None)
        return insights[:MAX_INSIGHTS]
    except Exception:
        # Insights are a nicety. Never let them take down the caller.
        return []


def cutoff_finder(conn, substance: str = "caffeine",
                  metric: str = "sleep_duration", days: int = 120) -> dict:
    """Find the hour of day beyond which a dose associates with a worse night.

    Buckets events by hour of day, then for each candidate cutoff compares nights
    following a dose at or after that hour against nights following no dose,
    looking for the hour where late intake most reduces the metric (accounting for
    whether higher or lower is the healthy direction). Cautious and guarded: on
    sparse data it returns a note rather than a false precision answer.
    """
    result = {"substance": substance, "metric": metric, "cutoff_hour": None,
              "note": None, "candidates": []}
    try:
        anchor = _anchor_date(conn)
        if anchor is None:
            result["note"] = "no daily data available"
            return result
        since = anchor - timedelta(days=days)
        series = _metric_series(conn, metric, since)
        if not series:
            result["note"] = f"no {metric} values in the window"
            return result

        rows = db.fetchall(conn,
            "SELECT ts FROM events WHERE kind = ? AND CAST(ts AS DATE) >= ?",
            [substance, since - timedelta(days=1)])
        if not rows:
            result["note"] = f"no {substance} events logged in the window"
            return result
        dose_hours = {}
        any_days = set()
        for (ts,) in rows:
            any_days.add(ts.date())
            dose_hours.setdefault(ts.date(), []).append(ts.hour)

        # Healthy direction: for sleep_duration, hrv, recovery, higher is better,
        # so a reduction (negative delta) is the harm we are hunting for.
        from heliosd.trust.policy import MetricPolicy
        try:
            direction = MetricPolicy().direction(metric)
        except Exception:
            direction = "higher"
        harmful_sign = -1.0 if direction != "lower" else 1.0

        best = None
        for hour in range(11, 22):
            late_days = {d for d, hs in dose_hours.items() if any(h >= hour for h in hs)}
            exposed, control = [], []
            for d, v in series.items():
                prior = d - timedelta(days=1)
                if prior in late_days:
                    exposed.append(v)
                elif prior not in any_days:
                    control.append(v)
            if len(exposed) < MIN_GROUP or len(control) < MIN_GROUP:
                continue
            u, p = _mannwhitney(exposed, control)
            delta = _median(exposed) - _median(control)
            harm = delta * harmful_sign  # positive harm means the metric got worse
            cand = {"hour": hour, "delta": round(delta, 3), "p": round(p, 4),
                    "n_exposed": len(exposed), "n_control": len(control),
                    "harm": round(harm, 3)}
            result["candidates"].append(cand)
            if harm > 0 and (best is None or (harm, -p) > (best["harm"], -best["p"])):
                best = cand

        if best is None:
            result["note"] = "no cutoff hour showed a clear association"
            return result
        result["cutoff_hour"] = best["hour"]
        result["delta"] = best["delta"]
        result["p"] = best["p"]
        result["n_exposed"] = best["n_exposed"]
        result["n_control"] = best["n_control"]
        worse = "less" if metric == "sleep_duration" else "lower"
        result["verdict"] = _verdict(best["p"])
        result["note"] = (
            f"{substance.capitalize()} at or after {best['hour']}:00 is associated with "
            f"{worse} {_label(metric)} that night. Association only, treat as a nudge to "
            f"experiment with, not a hard rule."
        )
        return result
    except Exception:
        result["note"] = "could not compute a cutoff on the available data"
        return result


def _anchor_date(conn) -> date | None:
    rows = db.fetchall(conn, "SELECT MAX(date) FROM daily_values")
    if not rows or rows[0][0] is None:
        return None
    d = rows[0][0]
    return d if isinstance(d, date) else date.fromisoformat(str(d))
