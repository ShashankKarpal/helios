"""Wave 3 (design note docs/briefs/build-2026-10-09/wave3/design.md, section 5):
C4, the full rebuild leaves no journal behind for the dates it covered, on
tiny synthetic stores. Every expected value is worked out by hand.
Synthetic data only."""

from __future__ import annotations

from datetime import datetime, timedelta

from heliosd.signals.rebuild import rebuild_all
from heliosd.store import db
from heliosd.trust.registry import SourceRegistry
from tests.test_wave2_export import _policy
from tests.test_wave2_tools import FIRST, TODAY, _tiny_store


# ---------------------------------------------------------------- C4: the rebuild clears the journal it covers

def test_rebuild_all_clears_the_journal_it_covers(tmp_path):
    path = tmp_path / "s.duckdb"
    _tiny_store(path)
    conn = db.connect(path)
    policy = _policy()
    policy.sync_registry(conn)
    at = datetime(2026, 5, 1, 9, 0)
    inside = [(FIRST, "whoop"), (FIRST + timedelta(days=4), "whoop"), (FIRST + timedelta(days=4), "ingest"), (TODAY, "startup")]
    outside = [(FIRST - timedelta(days=3), "ingest"), (TODAY + timedelta(days=2), "manual")]
    db.insert_batch(conn, "INSERT INTO dirty_dates (date, reason, batch_id, enqueued_at) VALUES (?, ?, ?, ?)",
                    [[d, r, "b-1", at] for d, r in inside + outside])
    db.execute(conn, "INSERT INTO dirty_dates (date, reason, batch_id, enqueued_at) VALUES (?, 'delete', 'b-2', NULL)",
               [FIRST + timedelta(days=6)])                                # an unstamped row inside the range goes too
    out = rebuild_all(conn, policy, TODAY, registry=SourceRegistry())
    assert out["range"] == [FIRST, TODAY]
    left = sorted((d, r) for d, r in db.fetchall(conn, "SELECT date, reason FROM dirty_dates"))
    assert left == sorted(outside)                                        # only the dates the rebuild did not cover
    assert out["journal_cleared"] == len(inside) + 1
    conn.close()


def test_rebuild_all_keeps_a_journal_row_written_while_it_ran(tmp_path, monkeypatch):
    """A row replaced during the rebuild (a newer enqueued_at) stays, as in the drain."""
    from heliosd.signals import rebuild as rb
    path = tmp_path / "s.duckdb"
    _tiny_store(path)
    conn = db.connect(path)
    policy = _policy()
    policy.sync_registry(conn)
    day = FIRST + timedelta(days=2)
    db.execute(conn, "INSERT INTO dirty_dates (date, reason, batch_id, enqueued_at) VALUES (?, 'ingest', 'b-1', ?)",
               [day, datetime(2026, 5, 1, 9, 0)])
    real = rb.bl.compute_daily_values

    def ingest_meanwhile(*a, **k):                                         # an ingest journals the day again mid-rebuild
        db.execute(conn, "INSERT OR REPLACE INTO dirty_dates (date, reason, batch_id, enqueued_at) VALUES (?, 'ingest', 'b-2', ?)",
                   [day, datetime(2026, 5, 1, 9, 5)])
        return real(*a, **k)
    monkeypatch.setattr(rb.bl, "compute_daily_values", ingest_meanwhile)
    out = rebuild_all(conn, policy, TODAY, registry=SourceRegistry())
    assert db.fetchall(conn, "SELECT date, reason, batch_id FROM dirty_dates") == [(day, "ingest", "b-2")]
    assert out["journal_cleared"] == 0
    conn.close()

