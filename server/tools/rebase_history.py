#!/usr/bin/env python
"""Phase 1b migration tool: rebase history, one row per UUID, the re-read
compare, the export link, the Whoop reconciliation, the one-transaction
cutover and the full derived rebuild (heliosd/migrate/rebase_history.py).

    server/.venv/bin/python server/tools/rebase_history.py <store.duckdb> --out DIR
        dry run: every gate on the staged table, the lineage archive and the
        report in DIR; the file is left as it was (staging dropped)
    ... --cutover                 also perform the swap on this file (a COPY)
    ... --cutover --rebuild       and the full derived rebuild plus the diff
    ... --apply                   the live store (daemon STOPPED): cutover,
                                  rebuild, label "apply"
    ... --archive-dir DIR         where the lineage archive goes (repeatable:
                                  the capture directory in both places)
    ... --apple-health PATH       read-only independent anchor (optional)
    ... --accept-reread-mismatches
                                  take the re-read for instant or content
                                  mismatches instead of stopping (owner decision;
                                  recorded in the migrations row)
    ... --today YYYY-MM-DD        the reporting today for the rebuild

Exit 1 on any failed gate or a stop; nothing is written to samples before
every gate passed. Never run against the live file while heliosd runs: the
daemon holds the file lock and the tool would refuse anyway. Counts only in
the output; no sample values, no secrets (the policy is read through the
normal loader, the token is never touched).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from heliosd.config import load_settings  # noqa: E402
from heliosd.migrate.rebase_history import MIGRATION, Migration, render_markdown  # noqa: E402
from heliosd.trust.policy import MetricPolicy  # noqa: E402
from heliosd.trust.registry import SourceRegistry  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=f"Helios {MIGRATION}")
    ap.add_argument("db", help="the store file (a scratch copy, or the live file with heliosd stopped)")
    ap.add_argument("--out", required=True, help="report directory (json, markdown, log, derived snapshots)")
    ap.add_argument("--archive-dir", action="append", default=[], help="lineage archive directory (repeatable)")
    ap.add_argument("--cutover", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--apply", action="store_true", help="cutover and rebuild with label apply")
    ap.add_argument("--accept-reread-mismatches", action="store_true")
    ap.add_argument("--apple-health", default=None)
    ap.add_argument("--today", default=None)
    ap.add_argument("--label", default=None)
    ap.add_argument("--oracle-cells", type=int, default=200)
    args = ap.parse_args()
    st = load_settings()
    policy = MetricPolicy(default_tz=st.timezone)
    registry = SourceRegistry()
    label = args.label or ("apply" if args.apply else "dryrun")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    logf = open(out / f"{label}.log", "a", encoding="utf-8")

    def log(line: str) -> None:
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    log(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {MIGRATION} label={label} db={args.db} cutover={args.cutover or args.apply} "
        f"rebuild={args.rebuild or args.apply} archive={args.archive_dir or ['<out>/lineage']}")
    m = Migration(args.db, policy, registry, out, archive_dirs=args.archive_dir or None,
                  cutover=args.cutover or args.apply, rebuild=args.rebuild or args.apply,
                  accept_reread_mismatches=args.accept_reread_mismatches, apple_health=args.apple_health,
                  today=date.fromisoformat(args.today) if args.today else None, label=label, log=log,
                  oracle_cells=args.oracle_cells)
    R = m.run()
    (out / f"{label}.md").write_text(render_markdown(R), encoding="utf-8")
    log(json.dumps({"ok": R["ok"], "stopped": R["stopped"], "fails": R["fails"], "steps": R["steps"],
                    "rows_before": R["facts"].get("samples_before"), "rows_after": R["facts"].get("rows_after"),
                    "twins": R["facts"].get("twin_uuids"), "compare": R["facts"].get("compare_classes"),
                    "exports": R["facts"].get("export_link_totals"), "whoop": R["facts"].get("whoop_day_rows_by_outcome"),
                    "rss_mb": R.get("rss_mb_final")}, default=str))
    logf.close()
    sys.exit(0 if R["ok"] else 1)


if __name__ == "__main__":
    main()
