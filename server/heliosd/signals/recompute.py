"""Recompute by explicit affected dates (single-source plan v2, Phase 1a item 4).

The old daemon recomputed "span days back from today" for every batch, so a
June batch rebuilt September and left June stale, and a daily value whose
inputs had all been deleted lived on. This module replaces that with:

- a durable journal (dirty_dates) written by every ingest and drained here;
- explicit dependency expansion: a changed input on reporting date D changes
  the daily value for D, every baseline whose window contains D (dates D+1
  through D+max_window), the signals of those dates (their baselines moved)
  and of D itself, the sleep context of the following 14 days (inside the
  same span), and the cached narratives of all of them; everything capped at
  the reporting "today";
- reconciliation against fresh outputs: daily values, baselines and signals
  that no longer qualify are removed, not left behind;
- a generation counter per date so an in-flight narrative cannot publish
  against inputs that moved under it.

Cost is bounded by the span of the expanded intervals, not by how many dates
were dirtied: past WIDE_PASS_DAYS of union span the pass collapses to one
range from the earliest dirty date to today.

Concurrency (checkpoint B, points 1, 2 and 18): the journal drain, the hourly
window and /api/recompute run one at a time behind one re-entrant lock, so an
older pass can never overwrite a newer one. The drain deletes only the journal
rows it read (by enqueued_at): a row an ingest replaced during the pass
survives to the next drain. A window pass that changes a daily value outside
its own derived window journals that date, so dependents are never left stale.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone

from heliosd.ingest.normalize import reporting_today
from heliosd.signals.baselines import compute_baselines, compute_daily_values
from heliosd.signals.markers import compute_signals
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry

WIDE_PASS_DAYS = 120
CONTEXT_DAYS = 14  # sleep context window in signals/context.py

# One recompute pass at a time (journal drain, hourly window, API window).
# Re-entrant: drain_journal holds it while calling recompute_dates.
_PASS_LOCK = threading.RLock()


def intervals(dates: set[date]) -> list[tuple[date, date]]:
    """Contiguous runs of dates, sorted."""
    out: list[tuple[date, date]] = []
    for d in sorted(dates):
        if out and (d - out[-1][1]).days <= 1:
            out[-1] = (out[-1][0], d)
        else:
            out.append((d, d))
    return out


def expand(dates: set[date], max_window: int, today: date) -> tuple[set[date], set[date]]:
    """(daily_value_dates, derived_dates). A change at D moves the daily value
    of D and the baselines, signals and narratives of D through
    D + max(max_window, CONTEXT_DAYS), capped at today."""
    tail = max(max_window, CONTEXT_DAYS)
    daily = {d for d in dates if d <= today}
    derived: set[date] = set()
    for d in daily:
        end = min(today, d + timedelta(days=tail))
        derived.update(d + timedelta(days=i) for i in range((end - d).days + 1))
    return daily, derived


def invalidate_derived(conn, dates: set[date]) -> None:
    """Bump the generation of every date, drop its cached narrative and its
    still-suggested actions (adopted and dismissed are owner decisions and
    stay). Call inside db.transaction."""
    if not dates:
        return
    ds = sorted(dates)
    conn.executemany("INSERT INTO derived_generation (date, generation, updated_at) VALUES (?, 1, ?) "
                     "ON CONFLICT (date) DO UPDATE SET generation = derived_generation.generation + 1, updated_at = excluded.updated_at",
                     [[d, datetime.now()] for d in ds])
    conn.execute("DELETE FROM narratives WHERE date IN (SELECT unnest(?))", [ds])
    conn.execute("DELETE FROM actions WHERE status = 'suggested' AND date IN (SELECT unnest(?))", [ds])


def leftover_dates(conn, today: date) -> set[date]:
    """Closed dates that still carry the state of a day in progress: a running
    total's NULL grade or an in_progress signal, written while the date was
    the reporting today (fix program A4, Codex A point 2). Nothing else
    writes either, so the set empties as soon as each closed date has been
    recomputed once. Every pass adds them, so the first drain after midnight,
    the hourly window and a restart after a long stop all finalize them."""
    rows = db.fetchall(conn, "SELECT date FROM daily_values WHERE grade IS NULL AND date < ? "
                             "UNION SELECT date FROM signals WHERE state = 'in_progress' AND date < ?",
                       [today, today])
    return {r[0] if isinstance(r[0], date) else date.fromisoformat(str(r[0])) for r in rows}


def generation_of(conn, day: date) -> int:
    rows = db.fetchall(conn, "SELECT generation FROM derived_generation WHERE date = ?", [day])
    return int(rows[0][0]) if rows else 0


def recompute_dates(conn, policy: MetricPolicy, registry: SourceRegistry, dates: set[date],
                    today: date | None = None, now: datetime | None = None) -> dict:
    """Rebuild exactly what the given reporting dates can have changed."""
    with _PASS_LOCK:
        today = today or reporting_today(policy.zone, now)
        # Closed dates still in their in-progress state are finalized with
        # whatever else is dirty (fix program A4).
        dates = set(dates) | leftover_dates(conn, today)
        if not dates:
            return {"daily_values": 0, "baselines": 0, "signals": 0, "dates": 0, "derived_dates": 0, "wide": False}
        daily, derived = expand(set(dates), policy.max_window, today)
        if not daily and not derived:
            return {"daily_values": 0, "baselines": 0, "signals": 0, "dates": 0, "derived_dates": 0, "wide": False}
        runs = intervals(daily)
        wide = bool(runs) and (runs[-1][1] - runs[0][0]).days > WIDE_PASS_DAYS
        if wide:
            runs = [(runs[0][0], runs[-1][1])]
            derived = {runs[0][0] + timedelta(days=i) for i in range((today - runs[0][0]).days + 1)}
        # Invalidate at the START as well as the end (checkpoint C, point 12): a
        # narrative published while the pass rewrites baselines and signals is
        # written against the start generation and invalidated by the end bump.
        with db.transaction(conn) as c:
            invalidate_derived(c, derived)
        n_dv = 0
        for start, end in runs:
            n_dv += compute_daily_values(conn, policy, registry, start, end, now=now, as_of=today)
        n_bl = n_sg = 0
        for d in sorted(derived):
            n_bl += compute_baselines(conn, policy, d)
            n_sg += compute_signals(conn, policy, d, today=today)
        with db.transaction(conn) as c:
            invalidate_derived(c, derived)
        return {"daily_values": n_dv, "baselines": n_bl, "signals": n_sg, "dates": len(daily),
                "derived_dates": len(derived), "wide": wide}


def drain_journal(conn, policy: MetricPolicy, registry: SourceRegistry,
                  today: date | None = None, now: datetime | None = None) -> dict | None:
    """Recompute everything the journal names, then remove exactly the rows
    that were read: a row replaced by an ingest during the pass carries a
    newer enqueued_at and stays for the next pass. Returns None when the
    journal is empty."""
    with _PASS_LOCK:
        rows = db.fetchall(conn, "SELECT date, reason, enqueued_at FROM dirty_dates")
        if not rows:
            return None
        dates = {r[0] if isinstance(r[0], date) else date.fromisoformat(str(r[0])) for r in rows}
        out = recompute_dates(conn, policy, registry, dates, today=today, now=now)
        with db.transaction(conn) as c:
            c.executemany("DELETE FROM dirty_dates WHERE date = ? AND reason = ? AND enqueued_at <= ?",
                          [[r[0], r[1], r[2]] for r in rows])
        out["journal_rows"] = len(rows)
        return out


def enqueue(conn, dates: set[date], reason: str, batch_id: str | None = None) -> None:
    # enqueued_at is stamped explicitly: DuckDB 1.5.4 keeps the old DEFAULT
    # value on INSERT OR REPLACE, and the drain relies on a replaced row being
    # newer than the one it read.
    now = datetime.now()
    with db.transaction(conn) as c:
        c.executemany("INSERT OR REPLACE INTO dirty_dates (date, reason, batch_id, enqueued_at) VALUES (?, ?, ?, ?)",
                      [[d, reason, batch_id, now] for d in sorted(dates)])


def recompute_window(conn, policy: MetricPolicy, registry: SourceRegistry, days: int = 3,
                     value_window: int | None = None, now: datetime | None = None) -> dict:
    """Explicit windows for /api/recompute and the hourly loop: daily values
    over the trailing value_window (default days) and derived state over the
    trailing days, both ending at the reporting today. A daily value that
    changed outside the derived window is journaled (reason recompute) so the
    drain rebuilds its baselines, signals and narratives too."""
    with _PASS_LOCK:
        today = reporting_today(policy.zone, now)
        vw = value_window if value_window is not None else days
        derived = {today - timedelta(days=i) for i in range(0, days + 1)}
        with db.transaction(conn) as c:
            invalidate_derived(c, derived)
        changed: set[date] = set()
        # Changed dates outside the derived window are journaled INSIDE the
        # daily-value transactions (checkpoint C, point 10), never after them.
        n_dv = compute_daily_values(conn, policy, registry, today - timedelta(days=vw), today,
                                    now=now, as_of=today, changed=changed,
                                    journal="recompute", journal_skip=derived)
        n_bl = n_sg = 0
        for d in sorted(derived):
            n_bl += compute_baselines(conn, policy, d)
            n_sg += compute_signals(conn, policy, d, today=today)
        with db.transaction(conn) as c:
            invalidate_derived(c, derived)
        outside = {d for d in changed if d not in derived}
        out = {"daily_values": n_dv, "baselines": n_bl, "signals": n_sg, "journaled": len(outside)}
        # A date left in progress outside this window (the daemon was stopped
        # longer than the window) is finalized here too (fix program A4).
        left = leftover_dates(conn, today)
        if left:
            recompute_dates(conn, policy, registry, left, today=today, now=now)
            out["finalized"] = len(left)
        return out
