# Helios

> **Helios is dedicated to Claude.**
>
> Most software built this way ships without a word of thanks to the model that wrote it. This project says it plainly and gladly: Helios was designed and built by Claude (Claude Fable 5). The product direction, the data, and the daily use are Shashank Karpal's; the architecture, the code, and the care are Claude's. Private, local, and personal, the way health software should be.

Helios is a free, open-source, local-only personal health system. It reads every metric from every wearable through Apple Health, applies a trust layer that knows which device to believe for which metric, computes research-backed signals against the user's own baselines, and layers local AI narrative, chat, and statistical insight on top. No cloud, no accounts, no subscriptions, no telemetry. Health data never leaves the user's devices.

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

## What it needs

- A Mac (Apple Silicon recommended) that is regularly on the same network as the iPhone. It runs the daemon, the database, and the local AI.
- An iPhone with Apple Health data, plus Xcode to build the Bridge onto it. A paid Apple Developer membership gives year-long signing; free provisioning works with a weekly re-sign.
- At least one device or app that writes to Apple Health. Any combination works: a smartwatch alone, a smart band plus a scale, a ring plus a strap. Helios adapts to whatever is present.
- Optional: a Whoop membership and free developer app for the live recovery layer (recovery, strain, rMSSD, sleep architecture arrive via Whoop's API rather than HealthKit).
- Optional: LM Studio (or any OpenAI-compatible local server) with a local model for narrative and chat. Without it, deterministic templates render everything anyway.
- mkcert for LAN TLS, so the iPhone talks to the Mac over HTTPS with a locally trusted certificate.

## Bring any devices: the policy is data, not code

Helios does not assume a specific device lineup. Every metric in `config/metric_policy.yaml` lists a priority order of device slots; the top device present wins, and the rest are kept as labeled corroboration. A user with Device 1 (a wrist smartwatch), Device 2 (an upper-arm band), and Device 3 (a smart scale) edits one YAML file to declare who to believe for which metric, and the whole engine, provenance chips included, follows.

The shipped policy is the author's reference setup, grounded in a per-metric validation audit (placement studies, PSG comparisons, ECG concordance): a Whoop MG worn on the upper arm anchors HRV (rMSSD), respiratory rate, sleep, and exercise heart rate; an Apple Watch Ultra anchors resting HR, SpO2, temperature, VO2 Max, and energy; a bicep-worn Amazfit Helio corroborates; a smart scale owns body composition. A different lineup (an Oura ring, a Garmin, a single Apple Watch) means different priorities, and the audit reasoning in the config comments shows how to think about the choice. If a metric's best device is not owned, the next best present simply takes over.

## Features

- **Helios Bridge: continuous HealthKit ingestion.** A minimal SwiftUI app watches every Health type with observer queries and background delivery, and streams anchored deltas to the Mac over LAN TLS. It kills the manual export-and-import ritual: anchors advance only after the Mac acknowledges each batch, so nothing is ever lost.

- **One-time full-history backfill, round-robin across types.** The Bridge pages the entire HealthKit history (years, millions of samples) to the Mac in resumable chunks, rotating through all types so one dense stream never starves the others, with an on-disk outbox for offline tolerance. Baselines are built on real history, not on whatever the last month looked like.

- **Trust layer: one source of truth per metric.** Each metric has a device priority; the top device present wins and the rest are kept as labeled corroboration, never averaged. Multi-device conflicts become information instead of noise, and disagreement is shown rather than hidden.

- **Provenance and confidence on every number.** Every value carries its source device and an A-to-D confidence grade computed from source rank, freshness, coverage, and cross-device agreement. It is always visible where a number came from and how much to trust it.

- **Per-marker signals against personal baselines.** Each marker is compared to the user's own rolling median with a MAD band (30/60/90-day windows), never to population norms, and never collapsed into a composite score. The evidence supports the markers, not a formula.

- **Trust flags for weakly validated metrics.** VO2 Max, calorie estimates, and BIA body fat are labeled trend-only; SpO2 is screening-only; single-night sleep stages are directional. Helios is honest about what consumer hardware can and cannot measure.

- **Sleep analysis, not just sleep numbers.** Nightly asleep vs in-bed hours, efficiency, stage architecture with percentages, fell-asleep and woke times, and week-over-week comparisons. Time in bed is never counted as sleep, and awake minutes never inflate the total.

- **Whoop live layer (optional).** Recovery, strain, HRV (rMSSD), sleep, and respiratory rate pull hourly from the Whoop API using the user's own free developer app, ingress only. The metrics that never land in HealthKit still land in Helios.

- **Local AI narrative: the LLM narrates, never computes.** A local model (via LM Studio) writes the morning brief and answers chat, but every number is calculated deterministically first, and a validator blocks any invented figure and any diagnosis or dosing language. If the model is down, deterministic templates still render.

- **Ask Helios: chat over the user's own data.** Tool-calling chat queries the live store and cites the device and confidence grade behind every number it speaks. It answers "how has my sleep been this week" from the data, and refuses to answer from vibes.

- **Quick-log: free text to structured events.** "Double espresso at 4pm" parses into a structured event confirmed with one tap. Logged inputs feed the correlation engine, so habits can be tested against outcomes.

- **Insights engine with honest statistics.** Spearman correlations, lag-1 effects (yesterday's strain vs today's recovery), weekend-vs-weekday rhythms, four-week drift detection with Theil-Sen slopes, and caffeine/alcohol cutoff finding, all corrected together with Benjamini-Hochberg FDR. Findings below the significance bar surface as clearly labeled early hints instead of being silently promoted.

- **Sync watchdog.** Every source's freshness is checked against its expected cadence, and a silently dead stream (the classic wearable failure mode) raises an alert with the exact fix steps. Upstream sync failure is treated as a first-class product surface.

- **Context-aware signals.** Travel (sleep-midpoint shift), configurable hot-season months, and late nights annotate signals as context ribbons instead of letting them false-alarm. An HRV dip two nights after a long-haul flight reads differently than one from nowhere, and Helios knows it.

- **A calm, dark PWA.** Today, Sleep, Activity, Insights, Actions, and chat, installable on the iPhone Home Screen and the Mac Dock, with provenance chips everywhere and no streaks, confetti, or cheering. Built to be glanced at, not to farm engagement.

- **Claude-native MCP server.** The live store is exposed to local AI tooling via MCP (query metrics, signals, comparisons, read-only SQL), proxying the daemon's API so the database stays single-writer. Health data becomes infrastructure for the user's own AI workflows, not a silo.

- **Weekly review, doctor report, and labs import.** A Monday markdown review with one experiment to run, a print-ready one-page HTML report with provenance for a clinician, and an assisted PDF lab importer where every value is confirmed before it is stored.

- **Privacy as architecture, not policy.** Everything at rest lives in a local DuckDB on the Mac and the Apple-encrypted HealthKit store on the iPhone; the only network traffic is iPhone-to-Mac LAN TLS and Mac-to-localhost inference. There is no account system, no telemetry, and nothing to leak.

## Principles

1. Per-marker signals compared to the user's own baseline. No fabricated composite scores.
2. Never average across devices. Every number carries its source device and a confidence grade.
3. The LLM narrates; it never measures. All numbers are computed deterministically and validated before display.
4. Honest freshness and honest uncertainty, always.

## Stack

Python 3.11, FastAPI, DuckDB, and pure-Python statistics on the server; React 19, Vite, TypeScript, Tailwind, and ECharts for the PWA; SwiftUI and HealthKit for the Bridge; LM Studio (OpenAI-compatible, localhost) for inference; MCP for AI-tool access. Everything runs on hardware the user already owns.

## Quickstart

See `RUNBOOK.md` for the full setup (Mac daemon, TLS, LM Studio, Xcode signing, iPhone permissions).

```bash
cd server
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,insights,labs,mcp]"
pytest                      # synthetic-fixture test suite (no real health data)
python -m heliosd.main      # starts the API + PWA on https://helios.local:8420
```

## Privacy

Data at rest: `~/Helios/data/helios.duckdb` on the Mac and the Apple-encrypted HealthKit store on the iPhone. Network traffic: iPhone to Mac over LAN TLS, and Mac to `localhost` (LM Studio). Optional, off by default: Whoop API pull (ingress of the user's own Whoop data), Tailscale, silent push wake. No analytics, no telemetry, no cloud AI. The repo ships only synthetic fixtures; `.gitignore` blocks health data, certificates, tokens, and runtime config.

## Roadmap

Sleep stage cross-checking across devices, trust-flag-aware narration, richer lab panel coverage with on-device OCR, executable actions (one tap sets the wind-down Focus or books the Zone 2 block), home-screen widgets and silent-push refresh, and deeper long-horizon insights as history accumulates.

## License

MIT. Built by Claude.
