"""The full derived rebuild (fix program Wave 2 design, section 3): clear every
derived table and recompute every date from the first eligible one to the
reporting today, offline (heliosd stopped, or a copy of the store).

Extracted from the Phase 1b migration (migrate/rebase_history.py
Migration._rebuild_derived, which now calls this) so the Wave 2 rebuild tool
(server/tools/rebuild_derived.py) runs exactly the same steps:

1. one transaction deletes daily_values, baselines, device_baselines, signals,
   derived_generation and narratives, and the actions still only suggested
   (adopted, dismissed and done actions are the owner's and stay);
2. daily values from the first eligible date to today in chunks of 366 days;
3. baselines: through the range form in signals/baselines.py when it exists
   (RANGE_BASELINES: every window of every date from one read per metric),
   else date by date; signals date by date, each after its baselines;
4. the dirty_dates rows dated inside [first, today] that were journaled before
   the rebuild began are removed, the way the drain removes the rows it read
   (signals/recompute.py drain_journal): the rebuild has recomputed everything
   they ask for. Rows outside the range, and rows journaled while the rebuild
   ran, stay for the daemon's drain (Wave 3 design C4: an apply used to leave
   about 800 dates behind, redone by the first drain as one wide pass);
5. CHECKPOINT.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

from heliosd.signals import baselines as bl
from heliosd.signals.markers import compute_signals
from heliosd.store import db

DERIVED = ("daily_values", "baselines", "device_baselines", "signals")
CLEARED = DERIVED + ("derived_generation", "narratives")
CHUNK_DAYS = 365                      # a chunk is [start, start + 365 days], as Phase 1b ran it
# The range form of compute_baselines (Wave 2 group C): fn(conn, policy, start, end)
# writes the baselines (and device baselines) of every date in [start, end].
RANGE_BASELINES = "compute_baselines_range"


def clear_covered_journal(conn, journal: list[tuple], first: date, today: date) -> int:
    """Remove the journal rows read before the rebuild (date, reason,
    enqueued_at) that are dated inside [first, today]. Like drain_journal, a
    row replaced since (a newer enqueued_at) stays. Returns the rows removed."""
    def day(v) -> date:
        return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])
    covered = [r for r in journal if first <= day(r[0]) <= today]
    stamped = [[r[0], r[1], r[2]] for r in covered if r[2] is not None]
    unstamped = [[r[0], r[1]] for r in covered if r[2] is None]
    with db.transaction(conn) as c:
        before = c.execute("SELECT COUNT(*) FROM dirty_dates").fetchone()[0]
        if stamped:
            c.executemany("DELETE FROM dirty_dates WHERE date = ? AND reason = ? AND enqueued_at <= ?", stamped)
        if unstamped:
            c.executemany("DELETE FROM dirty_dates WHERE date = ? AND reason = ? AND enqueued_at IS NULL", unstamped)
        after = c.execute("SELECT COUNT(*) FROM dirty_dates").fetchone()[0]
    return before - after


def first_eligible_date(conn) -> date | None:
    """The start date of the oldest eligible row (where history begins)."""
    return db.fetchall(conn, "SELECT MIN(CAST(start_ts AS DATE)) FROM eligible_samples")[0][0]


def rebuild_all(conn, policy, today: date, registry=None, first: date | None = None) -> dict:
    """Clear and rebuild every derived table for [first, today] (first: the
    oldest eligible date). Returns the range, the rows written, which baseline
    path ran, the seconds per step and the row count of each derived table."""
    if registry is None:
        from heliosd.trust.registry import SourceRegistry
        registry = SourceRegistry()
    seconds: dict[str, float] = {}
    t0 = time.time()
    with db.transaction(conn) as c:
        journal = c.execute("SELECT date, reason, enqueued_at FROM dirty_dates").fetchall()
        for table in CLEARED:
            c.execute(f"DELETE FROM {table}")
        c.execute("DELETE FROM actions WHERE status = 'suggested'")
    first = first or first_eligible_date(conn) or today
    n_dv = 0
    start = first
    while start <= today:
        end = min(today, start + timedelta(days=CHUNK_DAYS))
        n_dv += bl.compute_daily_values(conn, policy, registry, start, end, as_of=today)
        start = end + timedelta(days=1)
    seconds["daily_values"] = round(time.time() - t0, 2)          # with the clear, as Phase 1b timed it
    t1 = time.time()
    n_bl = n_sg = 0
    ranged = getattr(bl, RANGE_BASELINES, None)
    days = [first + timedelta(days=i) for i in range((today - first).days + 1)]
    if callable(ranged):
        n_bl = ranged(conn, policy, first, today)
        seconds["baselines"] = round(time.time() - t1, 2)
        for d in days:
            n_sg += compute_signals(conn, policy, d, today=today)
    else:
        for d in days:
            n_bl += bl.compute_baselines(conn, policy, d)
            n_sg += compute_signals(conn, policy, d, today=today)
    seconds["baselines_signals"] = round(time.time() - t1, 2)
    n_journal = clear_covered_journal(conn, journal, first, today)
    t2 = time.time()
    db.checkpoint(conn)
    seconds["checkpoint"] = round(time.time() - t2, 2)
    return {"range": [first, today], "daily_values": n_dv, "baselines": n_bl, "signals": n_sg,
            "journal_cleared": n_journal,
            "baselines_path": "range" if callable(ranged) else "per_date", "seconds": seconds,
            "derived_after": {t: db.fetchall(conn, f"SELECT COUNT(*) FROM {t}")[0][0] for t in DERIVED}}
