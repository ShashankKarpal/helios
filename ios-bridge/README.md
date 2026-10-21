# Helios Bridge (iOS)

A minimal SwiftUI app whose only job is continuous HealthKit ingestion to a Mac.
It reads Health data (samples of every type the owner's devices write, workouts
with their statistics, events and routes, ECG recordings, activity rings, and
Apple's own daily totals for steps and energy), and POSTs JSON batches to
`https://<host>/ingest` with an `X-Helios-Token` header. The host is the Mac
running heliosd, or the always-on relay that holds batches for it.

One rule (since 1.1): nothing leaves the phone's durable state unless the
receiver acked it, or Health can give it again through a cursor that itself
moves only after an ack.

## Layout

```
ios-bridge/
  project.yml                  XcodeGen spec (bundle id, capabilities, Info.plist, entitlements, BridgeCore)
  BridgeCore/                  Swift package: the logic that needs no phone, tested with `swift test` on the Mac
    Sources/BridgeCore/
      SyncEngine.swift         drains, flushes, dated windows, re-read jobs; one drain per stream at a time
      OutboxStore.swift        durable outbox: one file per page, never dropped, refusals parked, v1 migration
      AnchorStore.swift        per-stream HealthKit anchors with a generation counter (v1 files carry over)
      CursorStore.swift        dated-window cursors, re-read jobs, page limits, refusal blocks
      DurableFile.swift        temp file, F_FULLFSYNC, rename; every write checked; crash test points
      Payload.swift            the wire models (v2 batch, v1 kept for the migration)
      TypeRegistry.swift       every stream, its unit and background cadence
      WakeLog.swift            when iOS let the app run, and what it achieved
    Sources/OutboxCrashHarness the child process the crash tests kill with SIGKILL
    Tests/BridgeCoreTests      store, engine, crash, kill and replay tests
  Sources/
    HeliosBridgeApp.swift      app entry, background tasks, unlock handling, notifications
    HealthKitManager.swift     authorization, observers, when to run what; mirrors the engine for the screen
    HealthKitSource.swift      HealthKit queries and the per-class adapters
    HealthTypes.swift          stream to HealthKit type, names as the Apple Health export prints them
    Uploader.swift             URLSession POST, token header, ack and refusal handling
    Settings.swift             host and token (a host change resets the link state)
    StatusView.swift           the status screen
```

## Prerequisites

- A Mac with Xcode 16 or later (iOS 17 SDK or newer) and `xcodegen`.
- An Apple Developer Program membership (HealthKit and Background Modes
  entitlements, one-year device signing).
- The Helios Mac ingest server reachable from the phone over HTTPS with an
  mkcert-issued certificate. By default the daemon answers only loopback, the
  Mac's own addresses and your Tailscale tailnet (`[server] allow_clients =
  "tailnet"`), so the phone needs Tailscale and the Host must be the Mac's (or
  the relay's) Tailscale MagicDNS name (SETUP.md, section 1).

## 1. Generate and build

```
brew install xcodegen
cd ios-bridge
(cd BridgeCore && swift test)       # the engine, outbox, crash and replay tests
xcodegen generate
open HeliosBridge.xcodeproj         # or: xcodebuild -scheme HeliosBridge -destination generic/platform=iOS
```

`xcodegen generate` writes `HeliosBridge.xcodeproj`, plus `Info.plist` and
`HeliosBridge.entitlements` from `project.yml`. Re-run it after any change to
`project.yml`.

## 2. Team and signing

`DEVELOPMENT_TEAM` is set in `project.yml` (the generated project is not kept).
Signing is Automatic. Confirm the HealthKit capability (with background
delivery) and Background Modes (fetch, processing) after xcodegen.

## 3. Trust the mkcert root on the device

1. On the Mac: `mkcert -CAROOT`, copy `rootCA.pem` to the iPhone.
2. On the iPhone: install the profile (Settings > General > VPN and Device
   Management), then enable full trust (Settings > General > About >
   Certificate Trust Settings).
3. The certificate the host serves must name the host you set in the app.

ATS stays strict: a host the certificate does not name, or a certificate the
phone does not trust, fails TLS whatever `NSAppTransportSecurity` says. Fix the
certificate; never add `NSAllowsArbitraryLoads`.

## 4. First run, and the 1.1 upgrade

HealthKit does not deliver real data in the Simulator; run on the iPhone that
is paired with the Watch.

- A new install shows `Grant Health access and start`.
- An install upgraded from 1.0 shows `Allow the new Health types`: workouts,
  routes, ECGs, activity summaries and the extra types. iOS asks only for the
  types not decided yet. The types granted then drain their history; keep the
  app open while they do. Until that button is tapped, 1.1 drains only the 1.0
  types, because Health answers a type it was never asked about with nothing,
  which would look like an empty history.

HealthKit hides read-permission status, so the app cannot tell which types you
granted. If a type stays empty, check Settings > Health > Data Access and
Devices > Helios Bridge. A stream that never returned anything keeps no
anchor, so granting it later still sends its whole history.

## 5. Host and token

Under `Mac` on the status screen: the Host (`host:port`, a bare host or a full
`https://...`; HTTPS is assumed) and the shared token. A Host change resets the
"Mac link" status to untested and stops any running send at its next page.

## 6. How syncing behaves

- Anchored streams (every sample type, workouts, routes, ECGs): one
  `HKAnchoredObjectQuery` page at a time from the stream's saved anchor. The
  anchor moves only after the receiver acks the page. A page that cannot be
  sent waits in the outbox; its ack later moves the anchor too, so it is not
  sent twice. A queued page is deleted only on an ack, or once its stream's
  anchor moved past it (its data was delivered, or is still ahead of the anchor).
  An empty page never moves the anchor: a type turned off in Health settings
  answers with nothing, and what is written meanwhile arrives once it is on again.
- Read again later: a record whose parts cannot be read (a workout's routes, a
  route's points, an ECG's voltages) is left out of its page and held, on disk
  and before the anchor moves past it; later runs fetch it by uuid. It leaves
  the list only once the Mac acked it or its deletion arrived, never because
  Health did not return it (a type turned off looks the same). The status
  screen counts the records held.
- Dated windows: the 48-hour safety sweep of the registered sample types (14
  days for blood glucose; the read path that finds records the anchored one
  can miss), activity summaries,
  Apple's daily totals, and the date-range re-reads. Never queued: each has a
  cursor that moves only after every page of a window was acked, so a failed
  page is read again from Health next time. The sweep runs at most every six
  hours per type unless you press Sync Now.
- One drain per stream at a time: a wake during a drain joins it (the drain
  makes one more pass); at most two open-ended drains and three drains with a
  deadline (observer wakes) send at once.
- A refused page (HTTP 400, 413, 422) is parked on the phone, not deleted;
  the stream waits an hour before trying again. `Retry refused pages` puts
  them back once the Mac is fixed. A 413 halves that stream's page size, down
  to one record (a single record over the relay's 32 MB limit cannot be split).
- Locked phone: Health data is unreadable while the phone is locked. Wakes then
  only flush the outbox; the stream shows "waiting for unlock", and the unlock
  itself starts a send. Before the first unlock after a restart the Bridge's
  own files can be unreadable too: a file that cannot be read is never taken
  for a missing one (no drain restarts from the beginning, no cursor is
  overwritten, no queued page is set aside); it is read again later.
- Wakes: observer queries with background delivery (immediate for the vitals,
  workouts, routes and ECGs; hourly otherwise) get a 20-second budget and
  always complete inside it (a late completion makes HealthKit back off): the
  completion fires at the deadline whatever the work is doing, an upload under
  a deadline is one attempt that ends by it, and no Health read starts after it;
  `BGAppRefreshTask` (`com.shanky.helios.bridge.refresh`) and
  `BGProcessingTask` (`com.shanky.helios.bridge.processing`) are scheduled on
  every foreground. The status screen lists every wake and what it achieved.
- Reset and re-pull (swipe a type left): that type's whole history again (the
  Mac dedupes). Re-read dates: one type over chosen days, by date.

## 7. The deep link

`helios-bridge://sync` runs Sync Now. The PWA's "Pull latest" button opens it.

## 8. JSON batch format (v2)

```json
{
  "batch_id": "uuid", "device": "iphone", "sent_at": "ISO8601", "sync_path": "bridge", "schema": 2,
  "bridge": {"app_version": "1.1", "build": "2", "reason": "observer", "origin": "anchored", "stream": "..."},
  "sources": {"s1": {"name": "...", "bundle_id": "...", "version": "...", "product_type": "...", "os_version": "..."}},
  "devices": {"d1": {"desc": "<<HKDevice: ...>, name:..., ...>", "name": "...", "model": "...", "hardware": "..."}},
  "samples": [{"uuid": "...", "hk_type": "HKQuantityTypeIdentifierHeartRate", "kind": "quantity", "value": 62.0,
               "unit": "count/min", "start": "ISO8601", "end": "ISO8601", "source_name": "...", "src": "s1",
               "dev": "d1", "metadata": [["key", "value"]], "sync_identifier": null, "sync_version": null,
               "writer_id": "bundle id", "category_value": null}],
  "deleted": ["<deleted uuid>"],
  "workouts": [], "routes": [], "ecgs": [], "activity_summaries": [], "daily_statistics": []
}
```

- `samples` and `deleted` are always present and keep the 1.0 fields exactly
  (`uuid`, `hk_type`, `value`, `unit`, `start`, `end`, `source_name`), so the
  Mac's samples path is unchanged. Categories carry their true type (1.0 sent
  every category as sleep) and their raw value; sleep keeps the stage name.
- Instants are UTC with milliseconds. SpO2 and body fat stay fractions with
  unit `%` (the Mac scales). The extra types use the unit the Apple Health
  export prints for the same type.
- The other classes are sent only when present. A receiver that does not know
  `schema` 2, or finds a key it does not know, must refuse the batch (ack
  false), never ack it with parts dropped.

## 9. Signing lifecycle

Device builds are valid for about one year. Reinstall from Xcode before the
expiry to keep background delivery alive. The status screen carries a reminder.
