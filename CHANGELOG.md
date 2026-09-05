# Changelog

All notable changes to Helios.

## Unreleased

2026-09-05, five items built and verified against the running daemon, one commit each:

- Config layering. `config/metric_policy.yaml` and `config/source_registry.yaml` are generic public defaults; `$HELIOS_HOME/*.yaml` (default `~/Helios`) overlays them at startup (dicts merge key by key, lists replace). The personal registry no longer depends on `git update-index --skip-worktree`, the tracked policy carries no snoozes or redacted placeholder keys, and a fresh clone passes the suite against `server/tests/fixtures`. `/api/health` lists the overlay files it loaded.
- CI and pins. `.github/workflows/ci.yml` runs pytest with `server/constraints.txt` (exact versions from the reference machine) and the web build plus brand check on every push; gitleaks stays.
- Shared token on every `/api/*` route and `/ingest`: constant-time compare, empty or placeholder token refuses startup, `/api/health` and the SPA shell stay open, the served shell carries the token in a meta tag for the PWA, the MCP client sends it, the Shortcut needs one header (see `shortcuts/LOG-TO-HELIOS.md`). Rollback: `[server] api_auth = "off"` serves without a token and says so at startup and in `/api/health`. Known limit, accepted by design: anyone who can already load the page unauthenticated can read the token; the gain is against LAN or tailnet peers who never load the PWA, against cross-site requests from a browser, and for the DELETE routes.
- Watchdog and ingestion hygiene: `sync_log.received_at` written explicitly in the store's clock; lower-ranked devices that go quiet for a healthy metric are reported as `corroboration_decayed`, informational, never notified; `sources:` in the policy overlay watches other local apps' JSONL feeds for freshness and can ingest them as `system` events (undo never touches them); Whoop token-refresh or pull failures appear as a `whoop_cloud` row with the fix; lab uploads capped at 25 MB, PDF or image only, deleted after parsing; the Bridge's heart-rate quiet alert moved from 6 h to 24 h to match the Mac policy.
- Durability: `POST /api/admin/export` writes the irreplaceable tables (events, labs, narratives, whoop_cache, actions, chat_messages, profile_facts) as checksummed gzipped JSONL under `~/Helios/backup/<date>/`; `server/tools/helios_backup.py run` drives it nightly, prunes, optionally rsyncs to a second machine and verifies remote checksums, and touches `LAST_OK` only when everything passed; `restore-test` loads an export into a fresh schema and compares counts. The 2026-08-17 "weekly full plus nightly incremental" shape was vetoed with evidence: the tables total a few hundred rows, so a nightly full is a few kilobytes.

Security fixes from the 2026-09-02 fleet audit. Live verification against the running daemon: the traversal URL that returned the config file before the restart returns the SPA shell after it; the multi-statement and `read_text` queries return 400; a plain SELECT still works.

- SPA catch-all contained to `web/dist`: `..%2F` path segments no longer serve files outside the built site (previously readable without authentication from any host that could reach the port).
- `/api/tool/sql` is now actually read-only: a single SELECT or WITH statement, no `;`, and a blocklist for file, extension, and DDL/DML functions; the DuckDB connection additionally sets `enable_external_access=false`.
- Whoop OAuth uses a random per-login `state` that the callback verifies; the token file is written 0600.
- The three background loops log exceptions instead of swallowing them silently.
- The watchdog report is sorted worst-first (bridge, then silent, then stale) so the hourly notification names the most actionable entry.
- RUNBOOK and SETUP register the MCP server with the venv interpreter, not a bare `python`.
- Closed above on 2026-09-05: the API surfaces other than `/ingest` were unauthenticated on the tailnet until the shared-token rule landed.

## v1.0.0

- Continuous HealthKit ingestion via the Helios Bridge with anchored delivery, offline outbox, and full-history backfill.
- Device trust layer: one source of truth per metric, provenance and confidence on every number.
- Per-marker signals against personal baselines, insights engine with FDR correction, sync watchdog.
- Local AI narration and chat with a validator that blocks invented figures; deterministic fallback.
- Dark PWA, Whoop live overlay (optional), weekly review, doctor report, labs import, MCP server.
- Docs: generic SETUP.md replaces the personal runbook; README quickstart now includes the config copy and mkcert steps; ingest_token documented under [server]; Apple Developer Program and Whoop membership costs stated up front; source registry genericized.
