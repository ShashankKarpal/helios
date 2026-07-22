# Helios

> **Helios is dedicated to Claude.**
>
> Most software built this way ships without a word of thanks to the model that wrote it. This project says it plainly and gladly: Helios was designed and built by Claude (Claude Fable 5). The product direction, the data, and the daily use are Shashank Karpal's; the architecture, the code, and the care are Claude's. Private, local, and personal, the way health software should be.

Helios is a free, open-source, local-only personal health system. It reads every metric from every wearable through Apple Health, applies a trust layer that knows which device to believe for which metric, and layers local AI intelligence on top. No cloud, no accounts, no subscriptions, no telemetry. Your health data never leaves your devices.

## How it works

```
 iPhone                                        Mac (Apple Silicon)
+---------------------------+                 +----------------------------------------+
| Wearables -> Apple Health |                 | heliosd (FastAPI + DuckDB)             |
| Helios Bridge (SwiftUI)   | --HTTPS/LAN-->  |  ingest -> trust layer -> signals      |
|  HealthKit observers +    |                 |  narrative + chat via LM Studio (local)|
|  background delivery      | <--signals----  |  PWA + JSON API + MCP server           |
| Helios PWA (Home Screen)  |                 |  all data at rest: local DuckDB        |
+---------------------------+                 +----------------------------------------+
```

- **Bridge** (`ios-bridge/`): a minimal iOS app that watches HealthKit with observer queries and background delivery, and streams anchored deltas to your Mac. One-time historical backfill included. No manual exports, ever.
- **heliosd** (`server/`): ingestion, source-of-truth arbitration with per-reading provenance and confidence, research-backed per-marker signals against your own baselines (no composite scores), sync watchdog, local LLM narratives and chat, weekly review, doctor report, labs import.
- **PWA** (`web/`): dark, calm, typographic. Today, Sleep, Activity, Insights, Actions, and a chat that cites which device every number came from.
- **MCP server**: exposes your live health store to local AI tooling.

## Principles

1. Per-marker signals compared to your own baseline. No fabricated composite scores.
2. Never average across devices. Every number carries its source device and a confidence grade.
3. The LLM narrates; it never measures. All numbers are computed deterministically and validated before display.
4. Honest freshness and honest uncertainty, always.

## Quickstart

See `RUNBOOK.md` for the full owner setup (Mac daemon, TLS, LM Studio, Xcode signing, iPhone permissions).

```bash
cd server
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                      # synthetic-fixture test suite
python -m heliosd.main      # starts the API + PWA on https://helios.local:8420
```

## Privacy

Data at rest: `~/Helios/data/helios.duckdb` on your Mac and the Apple-encrypted HealthKit store on your iPhone. Network traffic: iPhone to Mac over LAN TLS, and Mac to `localhost` (LM Studio). Optional, off by default: Whoop API pull (ingress of your own Whoop data), Tailscale, silent push wake. No analytics, no telemetry, no cloud AI.

## License

MIT. Built by Claude.
