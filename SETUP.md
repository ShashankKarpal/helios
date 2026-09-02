# Helios setup

Everything you do once to stand Helios up, in order. All local. Two costs to know about: continuous background HealthKit delivery from the Bridge needs an Apple Developer Program membership (99 USD/yr; a free personal team works but apps must be re-signed every 7 days), and the optional Whoop overlay needs an active Whoop membership. Everything else is free. No health data leaves your Mac and iPhone.

## 0. Prerequisites (Mac)

```bash
brew install uv node mkcert xcodegen duckdb
# Optional, for narrative and chat: install LM Studio and download the two
# models named in config/helios.example.toml (or set your own under [llm]).
```

## 1. Backend (heliosd)

```bash
cd server
uv venv && source .venv/bin/activate
uv pip install -e ".[dev,insights,labs,mcp]"

mkdir -p ~/Helios/data ~/Helios/certs
cp ../config/helios.example.toml ~/Helios/helios.toml
# edit ~/Helios/helios.toml: set a long random ingest_token under [server].

# TLS so the iPhone PWA gets a secure context:
mkcert -install
cd ~/Helios/certs && mkcert helios.local
# set tls_cert and tls_key in helios.toml to the files mkcert just wrote.

# Name the Mac 'helios' (System Settings > General > Sharing > local hostname),
# so the iPhone reaches it at helios.local.

cd -
python -m heliosd.main            # serves https://helios.local:8420
pytest                            # all local, synthetic data
```

LM Studio (optional): enable the headless server (`lms server start`), turn on JIT load and a TTL (about 900s) so models auto-unload when idle. Helios talks to it at http://localhost:1234.

## 2. Web app (PWA)

```bash
cd web
npm install && npm run build      # output lands in web/dist, which heliosd serves
```

On the iPhone (same Wi-Fi as the Mac):

1. AirDrop the mkcert root CA (`mkcert -CAROOT` shows the folder) to the iPhone, install the profile, then trust it under Settings > General > About > Certificate Trust Settings.
2. Open Safari to `https://helios.local:8420`, then Share > Add to Home Screen.

## 3. Helios Bridge (continuous HealthKit ingestion)

```bash
cd ios-bridge
xcodegen generate
open HeliosBridge.xcodeproj
```

In Xcode: select your Team, set the bundle id if prompted, plug in the iPhone (or pair over Wi-Fi), Run. Grant the HealthKit prompts (allow all categories). In the Bridge status screen, set the Mac host (`helios.local:8420`) and paste the same ingest_token from helios.toml. Tap Sync Now to kick the historical backfill; watch progress in `GET /api/freshness`. With an Apple Developer Program team, signing lasts about a year; on a free personal team, re-deploy from Xcode every 7 days.

After the backfill finishes (outbox at 0, sent counts stable), run ONE wide recompute so the whole history becomes daily values, baselines, and signals (backfill chunks intentionally skip inline recomputes for speed):

```bash
curl -X POST "https://helios.local:8420/api/recompute?days=14&value_window=4000"
```

The PWA "Pull latest" button opens `helios-bridge://sync` for an on-demand catch-up, then recomputes.

## 4. Whoop live overlay (optional)

Helios talks to the Whoop Developer API v2 (v1 was retired 2025-10-01). An active Whoop membership is required.

1. Create a free app at developer.whoop.com; redirect URI `http://localhost:8420/whoop/callback`.
2. Put client_id/client_secret in helios.toml, set `enabled = true`.
3. Visit `https://helios.local:8420/whoop/login` once to authorize; tokens persist locally.
4. `POST /api/whoop/pull` (the hourly loop then keeps it fresh).

## 5. Claude health source (MCP)

Add the Helios MCP server to your Claude config:

```json
{ "mcpServers": { "helios": { "command": "/path/to/helios/server/.venv/bin/python", "args": ["-m", "heliosd.mcp_server.server"] } } }
```

The MCP server exposes the store read-only, proxying the daemon so the database stays single-writer.

## 6. Labs

Drop panel PDFs or report screenshots; the labs importer extracts biomarkers, you confirm each value, and they land in the labs table (feeding the doctor report at `/api/doctor-report`). Image OCR runs locally on the Mac.

## Fallback: Shortcuts pipe (if you ever skip the Bridge)

A Time of Day automation running Find Health Samples that POSTs to `/ingest` also works. Kept as contingency only; the Bridge is the primary path.

## Troubleshooting

- A metric goes quiet: `GET /api/freshness` names the stream and the exact fix.
- A Zepp or similar companion app stops writing to Apple Health: in the companion app, toggle every Health metric off then on; in the iPhone Health app enable all write categories for it; force-quit and reopen. Opening the companion app briefly each morning helps some devices flush to Health.
- Chat says a number "can't be backed up": the validator caught an ungrounded figure and fell back safely. That is by design.
- LM Studio down: signals and the brief still render from the deterministic template; only the narrative and chat pause.

## Sync watchdog: cadence, cooldown, snoozes

The watchdog (`server/heliosd/signals/watchdog.py`, thresholds in `config/metric_policy.yaml`) alarms only when EVERY source for a metric is late: stale past 2x `cadence_hours`, silent past 4x. macOS notifications post at most once per (metric, status) every 6 hours.

- Heart rate is HealthKit protected data: the phone can read it only while unlocked, so delivery routinely lags hours and backfills losslessly on unlock. Its cadence is 12h for this reason. Delivery lag under 24h is not an outage; do not chase it.
- Expected silence (travel, a scale unused, a device in a drawer): add `snooze_until: YYYY-MM-DD` to the metric in `config/metric_policy.yaml`, then restart the daemon. The watchdog skips that metric through the date and wakes it automatically after. Delete the line to un-snooze early.
- Permanently owner-dependent metrics (for example food logging) use `optional: true` instead; snooze is for temporary muting only.
