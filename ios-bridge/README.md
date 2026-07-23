# Helios Bridge (iOS)

A minimal SwiftUI app whose only job is continuous HealthKit ingestion to a Mac
on the local network. It requests read access to a fixed set of Health types,
backfills full history, then stays fresh with observer queries plus background
delivery, POSTing JSON batches to `https://<host>/ingest` with an
`X-Helios-Token` header. Anchors advance only after the Mac acks; anything
undelivered lands in an on-disk outbox and flushes on the next wake.

This repo contains source plus an XcodeGen spec. It has not been compiled here.
Build it in Xcode on the owner's Mac.

## Layout

```
ios-bridge/
  project.yml                 XcodeGen spec (bundle id, capabilities, Info.plist, entitlements)
  Sources/
    HeliosBridgeApp.swift     App entry, BGTask registration, deep-link handling, notifications
    HealthTypes.swift         Type registry (identifier, HKObjectType, unit, category flag, frequency)
    HealthKitManager.swift    Authorization, anchored + observer queries, background delivery, backfill, DTO encoding
    AnchorStore.swift         Per-type HKQueryAnchor persistence via NSKeyedArchiver
    Outbox.swift              Durable Codable batch queue, idempotent flush
    Uploader.swift            URLSession POST, token header, ack handling, retry/backoff
    Settings.swift            Host + token, persisted (@Observable)
    StatusView.swift          SwiftUI status screen
    Models.swift              SampleDTO / Batch / AckResponse, ISO8601 helper
  README.md
```

## Prerequisites

- A Mac with Xcode 15 or later (iOS 17 SDK).
- An Apple Developer Program membership (for the HealthKit and Background Modes
  entitlements and for 1-year device signing).
- The Helios Mac ingest server reachable on the LAN, serving HTTPS on the host
  and port you configure (default `helios.local:8420`) with an mkcert-issued
  certificate.

## 1. Generate the Xcode project

```
brew install xcodegen
cd ios-bridge
xcodegen generate
open HeliosBridge.xcodeproj
```

`xcodegen generate` writes `HeliosBridge.xcodeproj`, plus `Info.plist` and
`HeliosBridge.entitlements` derived from `project.yml`. Re-run it any time you
change `project.yml`.

## 2. Set your Team and signing

1. Select the `HeliosBridge` target, `Signing and Capabilities`.
2. Choose your Team. Signing style is Automatic; Xcode manages the provisioning
   profile. You can instead set `DEVELOPMENT_TEAM` in `project.yml` and re-run
   xcodegen.
3. Confirm the `HealthKit` capability is present and that Background Modes shows
   Background fetch, Background processing, and Remote notifications. These come
   from `project.yml`; if Xcode does not show them, verify the generated
   `Info.plist` and `HeliosBridge.entitlements`.

## 3. Trust the mkcert root on the device

The app validates TLS normally, so the device must trust the mkcert root that
signed the Mac's certificate.

1. On the Mac: `mkcert -CAROOT` and copy `rootCA.pem` to the iPhone (AirDrop or
   email).
2. On the iPhone: open the profile, then Settings > General > VPN and Device
   Management, install it.
3. Settings > General > About > Certificate Trust Settings, enable full trust
   for the mkcert root.
4. Make sure the certificate the Mac serves covers the host you use
   (`helios.local`, or the IP / hostname you set in the app).

If you point the app at a raw IP or an untrusted certificate, TLS validation
fails. Either add that host to the mkcert certificate, or loosen
`NSAppTransportSecurity` in `project.yml` (for example add
`NSAllowsArbitraryLoads: true`) and re-run xcodegen. The current spec keeps
validation strict.

## 4. Run on device

HealthKit does not work in the Simulator, so run on a real iPhone (the one paired
with the Watch that produces the data).

1. Select your device, Run.
2. First launch shows the status screen with `Grant Health access and start`.
   Tap it. iOS presents the Health authorization sheet. Grant read access for
   every requested type (heart rate, resting HR, HRV, respiratory rate, SpO2,
   body temperature, sleeping wrist temperature, VO2 Max, steps, active and basal
   energy, sleep, blood glucose, body mass, BMI, body fat, lean body mass,
   dietary energy, workouts).
3. After authorization the app registers observer queries, enables background
   delivery, and starts the backfill automatically. The backfill is paginated
   and resumable; leaving the app is fine.

Note on authorization: HealthKit deliberately hides read-permission status, so
the app cannot tell which types you actually granted. The status screen tracks
"requested", not "granted". If a type stays empty, re-check it in
Settings > Health > Data Access and Devices > Helios Bridge.

## 5. Set the Mac host and token

On the status screen under `Mac`:

- Host: `helios.local:8420` by default. Accepts `host:port`, a bare host, or a
  full `https://...` string. Without a scheme, HTTPS is assumed. The app always
  POSTs to `<host>/ingest`.
- Token: the shared secret sent as the `X-Helios-Token` header. It must match
  what the Mac expects.

Both persist immediately.

## 6. How syncing behaves

- Backfill: paginated `HKAnchoredObjectQuery` over full history, chunked and
  resumable, one batch per chunk per type.
- Stay fresh: one `HKObserverQuery` per type plus `enableBackgroundDelivery`.
  Frequency is `.immediate` for vitals where allowed and `.hourly` otherwise;
  iOS may clamp `.immediate` to hourly for cumulative types like steps and
  energy, which is expected.
- On each wake the anchored query runs from the persisted per-type anchor,
  collects new and deleted samples, and POSTs a batch.
- Ack protocol: the per-type anchor advances only after the Mac returns
  `{ "ack": true }`. If the Mac is unreachable, the batch goes to the on-disk
  outbox and the anchor is left untouched, so nothing is lost and the window is
  re-fetched next time (safe: the Mac dedupes by sample uuid).
- The outbox flushes at the start of every sweep, on `BGAppRefreshTask`, and on
  foreground.
- Foreground and deep-link sweeps also run a trailing 48h safety sweep.
- `BGAppRefreshTask` (`com.shanky.helios.bridge.refresh`) is scheduled on each
  foreground for periodic background sweeps.

## 7. Test the deep link

`helios-bridge://sync` triggers an immediate foreground sweep. This is what the
PWA "Pull latest" button opens.

- From Safari on the device, type `helios-bridge://sync` in the address bar.
- Or from the Mac over a paired setup:
  `xcrun simctl openurl booted helios-bridge://sync` (Simulator only; use a real
  device for actual HealthKit data).
- Or add a Shortcut with an "Open URL" action pointing at `helios-bridge://sync`.

## 8. JSON batch format

The app POSTs exactly this shape (the Mac dedupes on `uuid`):

```json
{
  "batch_id": "uuid",
  "device": "iphone",
  "sent_at": "ISO8601",
  "sync_path": "bridge",
  "samples": [
    {
      "uuid": "<HKSample.uuid>",
      "hk_type": "HKQuantityTypeIdentifierHeartRate",
      "value": 62.0,
      "unit": "count/min",
      "start": "ISO8601",
      "end": "ISO8601",
      "source_name": "Owner's Ultra 1"
    }
  ],
  "deleted": ["<deleted sample uuid>"]
}
```

- Quantity samples send a numeric `value` in the canonical unit and the unit
  string (heart rate `count/min`, energy `kcal`, temperature `degC`, glucose
  `mg/dL`, mass `kg`, and so on).
- Sleep sends `hk_type` `HKCategoryTypeIdentifierSleepAnalysis` with `value` set
  to the `HKCategoryValueSleepAnalysis` case name string (for example
  `HKCategoryValueSleepAnalysisAsleepDeep`) and an empty `unit`.
- SpO2 and body fat are stored by HealthKit as a fraction in 0...1. The app
  sends the raw fraction with unit `%`; the Mac scales to a human percentage.
- `source_name` is `sample.sourceRevision.source.name`, which carries the curly
  apostrophe the Mac keys on.

## 9. Signing lifecycle (Apple Developer Program)

Device builds signed with an Apple Developer Program certificate and profile are
valid for about one year. When they expire, background delivery and the app stop
working until you re-sign. Reinstall from Xcode before the expiry to keep
continuous ingestion alive. The status screen carries a reminder note.

## Things to verify in Xcode

- Team and automatic signing resolve, and the HealthKit and Background Modes
  capabilities appear after xcodegen.
- The generated `Info.plist` contains `NSHealthShareUsageDescription`,
  `UIBackgroundModes`, `BGTaskSchedulerPermittedIdentifiers`, the
  `helios-bridge` URL scheme, and the ATS exception.
- Background delivery may need "Background App Refresh" enabled for the app in
  iOS Settings.
- Test the token and host against the real Mac; a first successful `Sync Now`
  should return the outbox depth to 0.
