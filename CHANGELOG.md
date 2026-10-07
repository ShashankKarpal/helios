# Changelog

All notable changes to Helios.

## Unreleased

2026-10-07, Bridge delivery over a Tailscale tailnet (an ops change; no daemon or app code moved) and a CI timing budget:

- README and SETUP: the mkcert certificate may carry the Mac's Tailscale MagicDNS name beside the `.local` name, written to the same file names; the Bridge Host field accepts `<name>:8420`, the app validates TLS as before and needs no rebuild. The notes say what reachability means (the PWA shell carries the token, so the port stays restricted to your own devices), that the certificate stays a private mkcert one (never `tailscale cert`, `serve` or `funnel`), and what the outbox does and does not recover during an outage. The Bridge README and the project.yml comment no longer suggest loosening App Transport Security for a certificate problem, which it cannot fix.
- CI: the two 5.0 s landing throughput budgets in `server/tests/test_reread_landing.py` (2,000 guarded rows land, then land again identically) scale by `HELIOS_TEST_TIME_SCALE` (default 1; `.github/workflows/ci.yml` sets 3; a finite factor in (0, 100], refused at import otherwise, read before collection). The GitHub runner missed the first budget by 5 to 14 percent on both attempts of run 37621550851 with the first landing's count exactly 2,000; the repeat was never reached because the timing assertion came first, so the test now asserts both counts before either budget. The counts and the shutdown deadline tests never scale; the budget is a coarse guard, not a benchmark.
2026-09-30, default model:

- Default LM Studio model is Qwen3.6-35B-A3B (MLX 4-bit, identifier `qwen3.6-35b-a3b`) for both primary and fallback, in `server/heliosd/config.py` and `config/helios.example.toml`. The previous defaults named models that were no longer installed (`qwen3-14b-mlx`, `qwen3.5-9b`). Measured on an M4 Pro 48 GB with the real brief code against a copy of the live database, three days, validator on: 6.3, 10.1 and 11.2 s, all validated on the first attempt, versus 66.4, 52.7 and 50.4 s for Qwen3.8-27B MLX 4-bit. Gemma 4 26B A4B was rejected: under `json_schema` it looped on a phrase until the 120 s timeout, and without a schema it spent its budget reasoning. Known style slips on the new model that the validator passes: a trailing `.0` on some values and `count/min` instead of bpm; a number-style post-pass is a candidate follow-up.
- SETUP recommends a pinned model instead of JIT load plus idle TTL, and `launchd/com.shanky.helios.lmstudio.plist.example` now tracks the headless-server LaunchAgent, which ran on the reference Mac but was tracked nowhere.

2026-09-05, five items built and verified against the running daemon, one commit each:

- Config layering. `config/metric_policy.yaml` and `config/source_registry.yaml` are generic public defaults; `$HELIOS_HOME/*.yaml` (default `~/Helios`) overlays them at startup (dicts merge key by key, lists replace). The personal registry no longer depends on `git update-index --skip-worktree`, the tracked policy carries no snoozes or redacted placeholder keys, and a fresh clone passes the suite against `server/tests/fixtures`. `/api/health` lists the overlay files it loaded.
- CI and pins. `.github/workflows/ci.yml` runs pytest with `server/constraints.txt` (exact versions from the reference machine) and the web build plus brand check on every push; gitleaks stays.
- Shared token on every `/api/*` route and `/ingest`: constant-time compare, empty or placeholder token refuses startup, `/api/health` and the SPA shell stay open, the served shell carries the token in a meta tag for the PWA, the MCP client sends it, the Shortcut needs one header (see `shortcuts/LOG-TO-HELIOS.md`). Rollback: `[server] api_auth = "off"` serves without a token and says so at startup and in `/api/health`. Known limit, accepted by design: anyone who can already load the page unauthenticated can read the token; the gain is against LAN or tailnet peers who never load the PWA, against cross-site requests from a browser, and for the DELETE routes.
- Watchdog and ingestion hygiene: `sync_log.received_at` written explicitly in the store's clock; lower-ranked devices that go quiet for a healthy metric are reported as `corroboration_decayed`, informational, never notified; `sources:` in the policy overlay watches other local apps' JSONL feeds for freshness and can ingest them as `system` events (undo never touches them); Whoop token-refresh or pull failures appear as a `whoop_cloud` row with the fix; lab uploads capped at 25 MB, PDF or image only, deleted after parsing; the Bridge's heart-rate quiet alert moved from 6 h to 24 h to match the Mac policy.
- Overnight relay tooling (2026-09-06): `server/tools/m1_spool_receiver.py`, a standard-library spool receiver for an always-on second Mac that speaks the Bridge's `/ingest` contract (token, `{"ack": true}` only after fsync, 401/413/507 refusals that leave the Bridge outbox intact, `/api/health` with queue depth and free disk), and `server/tools/m4_spool_pull.py`, which rsyncs the inbox, replays into the local `/ingest`, and deletes remote copies only after heliosd acks; replay is idempotent by sample uuid. LaunchAgent templates for both machines; see SETUP.md. Measured motive: the longest overnight gap in Bridge batches was 7 to 11 hours on 9 of the last 14 nights.
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
