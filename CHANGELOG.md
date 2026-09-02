# Changelog

All notable changes to Helios.

## Unreleased

Security fixes from the 2026-09-02 fleet audit. Live verification against the running daemon: the traversal URL that returned the config file before the restart returns the SPA shell after it; the multi-statement and `read_text` queries return 400; a plain SELECT still works.

- SPA catch-all contained to `web/dist`: `..%2F` path segments no longer serve files outside the built site (previously readable without authentication from any host that could reach the port).
- `/api/tool/sql` is now actually read-only: a single SELECT or WITH statement, no `;`, and a blocklist for file, extension, and DDL/DML functions; the DuckDB connection additionally sets `enable_external_access=false`.
- Whoop OAuth uses a random per-login `state` that the callback verifies; the token file is written 0600.
- The three background loops log exceptions instead of swallowing them silently.
- The watchdog report is sorted worst-first (bridge, then silent, then stale) so the hourly notification names the most actionable entry.
- RUNBOOK and SETUP register the MCP server with the venv interpreter, not a bare `python`.
- Known, scheduled separately: the API surfaces other than `/ingest` are still unauthenticated on the tailnet; the shared-token dependency for all `/api/*` routes needs client wiring (PWA, iOS bridge, Shortcut, MCP) and gets its own release.

## v1.0.0

- Continuous HealthKit ingestion via the Helios Bridge with anchored delivery, offline outbox, and full-history backfill.
- Device trust layer: one source of truth per metric, provenance and confidence on every number.
- Per-marker signals against personal baselines, insights engine with FDR correction, sync watchdog.
- Local AI narration and chat with a validator that blocks invented figures; deterministic fallback.
- Dark PWA, Whoop live overlay (optional), weekly review, doctor report, labs import, MCP server.
- Docs: generic SETUP.md replaces the personal runbook; README quickstart now includes the config copy and mkcert steps; ingest_token documented under [server]; Apple Developer Program and Whoop membership costs stated up front; source registry genericized.
