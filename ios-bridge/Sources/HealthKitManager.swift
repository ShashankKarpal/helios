import Foundation
import HealthKit
import Observation

// MARK: - Sweep reasons (for logging / status)
enum SweepReason: String {
    case observer
    case background
    case foreground
    case deepLink
    case manual
    case backfill
}

// MARK: - Per-type status (drives the status screen)
struct TypeStatus {
    /// Newest sample END date seen from ANY path (observer, trailing sweep, or
    /// backfill). This is the freshness number: "how recent is my data".
    var lastSampleDate: Date?
    var lastSyncDate: Date?
    var lastError: String?
    var sentCount: Int = 0
    /// How far through HISTORY the anchored backfill has reached. Kept separate
    /// from lastSampleDate so a backfill grinding through 2024 does not read as
    /// "no fresh data" (which caused false quiet-stream alarms).
    var backfillDate: Date?
    var backfillComplete: Bool = false
}

// MARK: - HealthKitManager
// Owns authorization, the anchored/observer queries, background delivery, the
// backfill, and the sample -> DTO encoding. Marked @MainActor because its
// observable state feeds SwiftUI directly; the HealthKit callbacks hop back onto
// the main actor via Task.
@MainActor
@Observable
final class HealthKitManager {
    static let shared = HealthKitManager()

    @ObservationIgnored let store = HKHealthStore()

    // Observable status
    var lastSyncDate: Date?
    var perTypeStatus: [String: TypeStatus] = [:]
    var authorizationRequested: Bool

    // Internal state
    @ObservationIgnored private var observersStarted = false
    @ObservationIgnored private var consecutiveUploadFailures = 0
    // 2000 samples ≈ 450KB JSON: small enough to upload well inside the request
    // timeout even on congested Wi-Fi. Larger chunks (5000+) produced multi-MB
    // bodies that timed out and poisoned the outbox.
    @ObservationIgnored private let backfillChunk = 2000
    @ObservationIgnored private let defaults = UserDefaults.standard
    @ObservationIgnored private let authKey = "helios.authRequested"
    @ObservationIgnored private let lastSyncKey = "helios.lastSync"

    private init() {
        authorizationRequested = defaults.bool(forKey: authKey)
        if let stored = defaults.object(forKey: lastSyncKey) as? Date {
            lastSyncDate = stored
        }
        for def in HealthTypes.all {
            perTypeStatus[def.identifier] = TypeStatus()
        }
    }

    // MARK: Lifecycle

    /// Called on every launch (foreground or background). Starts observers if the
    /// user has already granted access.
    func bootstrap() async {
        guard HKHealthStore.isHealthDataAvailable() else { return }
        if authorizationRequested {
            startObservers()
        }
    }

    /// First-launch flow: request read authorization, start observers, backfill.
    func requestAuthorizationAndStart() async {
        do {
            try await requestAuthorization()
            authorizationRequested = true
            defaults.set(true, forKey: authKey)
            startObservers()
            await backfill()
        } catch {
            recordGlobalError("Authorization failed: \(error.localizedDescription)")
        }
    }

    func requestAuthorization() async throws {
        guard HKHealthStore.isHealthDataAvailable() else {
            throw NSError(domain: "Helios", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "Health data not available on this device."])
        }
        // Read-only: nothing to share.
        try await store.requestAuthorization(toShare: [], read: HealthTypes.readTypes)
    }

    // MARK: Observers + background delivery

    func startObservers() {
        guard !observersStarted else { return }
        observersStarted = true

        for def in HealthTypes.all {
            let query = HKObserverQuery(sampleType: def.sampleType, predicate: nil) { [weak self] _, completionHandler, error in
                guard let self else { completionHandler(); return }
                if error != nil {
                    // Always signal completion so HealthKit does not back off.
                    completionHandler()
                    return
                }
                Task { @MainActor in
                    await self.syncType(def, reason: .observer)
                    self.lastSyncDate = Date()
                    self.persistLastSync()
                    // Signal completion only after the wake's work is done.
                    completionHandler()
                }
            }
            store.execute(query)
            enableBackgroundDelivery(for: def)
        }
    }

    private func enableBackgroundDelivery(for def: HealthTypeDef) {
        // frequency is .immediate for vitals where allowed, .hourly otherwise.
        // iOS may clamp .immediate to hourly for cumulative types; that is fine.
        store.enableBackgroundDelivery(for: def.sampleType, frequency: def.frequency) { _, _ in }
    }

    // MARK: Sweeps

    /// Incremental anchored sweep across all types. Flushes the outbox first.
    func performSweep(reason: SweepReason) async {
        await Outbox.shared.flush()
        for def in HealthTypes.all {
            await syncType(def, reason: reason)
        }
        lastSyncDate = Date()
        persistLastSync()
        checkQuietStreams()
    }

    /// Foreground / manual / deep-link path: incremental sweep plus a trailing
    /// 48h safety sweep to catch anything the anchored stream missed.
    func foregroundSweep(reason: SweepReason) async {
        // Sweep the recent 48h FIRST so today's data (steps, workouts, energy)
        // lands within seconds, even while years of history are still draining
        // behind it. Flush the outbox up front too.
        await Outbox.shared.flush()
        await trailingSweep(hours: 48)
        await performSweep(reason: reason)
    }

    /// Per-type manual sync (the button next to each type): flush the outbox,
    /// push this type's last 48h immediately, then advance its history drain.
    func syncTypeNow(_ def: HealthTypeDef) async {
        await Outbox.shared.flush()
        await trailingSweepType(def, hours: 48)
        await syncType(def, reason: .manual)
        lastSyncDate = Date()
        persistLastSync()
    }

    /// Force a full re-pull of ONE type: clear its anchor and drain its whole
    /// history again. Content-hash dedup on the Mac means already stored samples
    /// are not duplicated; only the genuinely missing ones land. Recovery path
    /// when a type's anchor advanced past data that never reached the Mac (for
    /// example a chunk that timed out mid-backfill).
    func resyncType(_ def: HealthTypeDef) async {
        AnchorStore.shared.clear(for: def.identifier)
        perTypeStatus[def.identifier]?.sentCount = 0
        perTypeStatus[def.identifier]?.backfillComplete = false
        perTypeStatus[def.identifier]?.backfillDate = nil
        perTypeStatus[def.identifier]?.lastError = nil
        await Outbox.shared.flush()
        await syncType(def, reason: .manual)
        // The anchored enumeration does not surface every sample: sync-identifier
        // samples (e.g. [redacted] glucose) are missing even from a nil anchor paged to
        // empty. Follow with a DATED sweep, a different HealthKit read path, over
        // recent years to pull whatever the anchor missed. De-duplicated on the Mac.
        let since = Calendar.current.date(byAdding: .year, value: -3, to: Date())
            ?? Date(timeIntervalSince1970: 0)
        await dateWindowedSweep(def, since: since)
        lastSyncDate = Date()
        persistLastSync()
    }

    /// Recover a type by DATE windows using HKSampleQuery. This is a different read
    /// path from HKAnchoredObjectQuery, which does not return some samples (e.g.
    /// [redacted] glucose written with a HealthKit sync identifier). Windowed so a dense
    /// multi-year history is never loaded into memory at once. Anchors untouched;
    /// the Mac de-duplicates on content hash.
    func dateWindowedSweep(_ def: HealthTypeDef, since: Date, windowDays: Int = 60) async {
        let cal = Calendar.current
        let now = Date()
        var start = since
        while start < now {
            let end = min(cal.date(byAdding: .day, value: windowDays, to: start) ?? now, now)
            let predicate = HKQuery.predicateForSamples(withStart: start, end: end, options: [])
            do {
                let samples = try await runSampleQuery(type: def.sampleType, predicate: predicate)
                if !samples.isEmpty {
                    updateLastSample(for: def.identifier, from: samples)
                    let dtos = samples.compactMap { dto(for: $0) }
                    var i = 0
                    while i < dtos.count {
                        let j = min(i + backfillChunk, dtos.count)
                        let batch = Batch.make(samples: Array(dtos[i..<j]), deleted: [])
                        if await Uploader.shared.upload(batch) {
                            perTypeStatus[def.identifier]?.sentCount += (j - i)
                            markSynced(def.identifier)
                        } else {
                            Outbox.shared.enqueue(batch)
                        }
                        i = j
                    }
                }
            } catch {
                perTypeStatus[def.identifier]?.lastError = error.localizedDescription
            }
            start = end
        }
    }

    /// One type: run the anchored query from its persisted anchor, build a batch,
    /// upload, and advance the anchor ONLY on a positive ack.
    func syncType(_ def: HealthTypeDef, reason: SweepReason) async {
        // Page in bounded chunks. The first sync for a type has no anchor, so an
        // unbounded query (HKObjectQueryNoLimit) would pull the entire multi-year
        // history into memory in one array, and because this type is @MainActor it
        // would also block the main thread while converting and encoding it. That
        // froze the UI and triggered an out-of-memory / watchdog crash on launch.
        // Draining in `backfillChunk`-sized pages keeps every batch small and lets
        // the main actor breathe (each page suspends on the network await).
        while true {
            let anchor = AnchorStore.shared.anchor(for: def.identifier)
            do {
                let result = try await runAnchored(type: def.sampleType,
                                                   anchor: anchor,
                                                   limit: backfillChunk)

                updateLastSample(for: def.identifier, from: result.added)

                if result.added.isEmpty && result.deleted.isEmpty {
                    // Nothing new. Advancing to the returned anchor is harmless and
                    // avoids rescanning, and there is no unacked data at stake.
                    AnchorStore.shared.setAnchor(result.newAnchor, for: def.identifier)
                    markSynced(def.identifier)
                    return
                }

                let samples = result.added.compactMap { dto(for: $0) }
                let deleted = result.deleted.map { $0.uuid.uuidString }
                let batch = Batch.make(samples: samples, deleted: deleted)

                let acked = await Uploader.shared.upload(batch)
                if acked {
                    AnchorStore.shared.setAnchor(result.newAnchor, for: def.identifier)
                    perTypeStatus[def.identifier]?.sentCount += samples.count
                    markSynced(def.identifier)
                    consecutiveUploadFailures = 0
                    // Keep paging until HealthKit returns an EMPTY result (handled
                    // at the top of the loop). A short page is NOT the end: at the
                    // boundary between separately-synced batches (an imported CGM
                    // block vs live-entered [redacted] readings) HealthKit hands back a
                    // partial page, and stopping there silently skipped everything
                    // after the boundary (this hid all [redacted] glucose).
                } else {
                    // Mac unreachable or the batch was rejected: queue and DO NOT
                    // advance the anchor, so the same window is re-fetched next time
                    // (idempotent, uuid-keyed). Stop draining this type for now.
                    Outbox.shared.enqueue(batch)
                    perTypeStatus[def.identifier]?.lastError = "Queued (\(Uploader.lastFailureReason))"
                    consecutiveUploadFailures += 1
                    notifyIfFailingRepeatedly()
                    return
                }
            } catch {
                perTypeStatus[def.identifier]?.lastError = error.localizedDescription
                return
            }
        }
    }

    /// Time-window safety net. Does not touch anchors; overlapping samples are
    /// deduped by the Mac on uuid.
    func trailingSweep(hours: Int) async {
        for def in HealthTypes.all {
            await trailingSweepType(def, hours: hours)
        }
    }

    /// One type's trailing window. Updates the freshness display too: the
    /// trailing sweep sees TODAY'S samples, so it is the honest source for
    /// "how recent is my data" while the anchored backfill grinds through
    /// history. (Not updating it here caused false quiet-stream alarms.)
    func trailingSweepType(_ def: HealthTypeDef, hours: Int) async {
        guard let start = Calendar.current.date(byAdding: .hour, value: -hours, to: Date()) else { return }
        let predicate = HKQuery.predicateForSamples(withStart: start, end: Date(), options: [])
        do {
            let samples = try await runSampleQuery(type: def.sampleType, predicate: predicate)
            guard !samples.isEmpty else { return }
            updateLastSample(for: def.identifier, from: samples)
            let dtos = samples.compactMap { dto(for: $0) }
            guard !dtos.isEmpty else { return }
            // Chunked: one giant 48h heart-rate batch produced multi-MB uploads
            // that timed out and clogged the outbox.
            var i = 0
            while i < dtos.count {
                let end = min(i + backfillChunk, dtos.count)
                let batch = Batch.make(samples: Array(dtos[i..<end]), deleted: [])
                let acked = await Uploader.shared.upload(batch)
                if !acked { Outbox.shared.enqueue(batch) }
                i = end
            }
        } catch {
            perTypeStatus[def.identifier]?.lastError = error.localizedDescription
        }
    }

    // MARK: Backfill

    /// Paginated, resumable backfill over full history, ROUND-ROBIN across
    /// types: every type advances one chunk per cycle, so a huge stream (years
    /// of heart rate) can never starve the others into showing "never". The
    /// anchor advances only after each chunk is acked, so an interruption
    /// resumes exactly where it stopped.
    func backfill() async {
        var active = HealthTypes.all
        while !active.isEmpty {
            var still: [HealthTypeDef] = []
            for def in active {
                if await backfillOneChunk(def) {
                    still.append(def)
                }
            }
            active = still
        }
        // A type whose chunk upload failed (e.g. a timeout while the Mac was
        // saturated) parks itself out of the loop above. Retry those a few times
        // so a transient stall never silently strands a type's history. syncType
        // drains a type to completion, so one pass per paused type suffices once
        // the Mac is reachable again; bounded so a real outage does not spin.
        for _ in 0..<5 {
            let paused = HealthTypes.all.filter { perTypeStatus[$0.identifier]?.lastError != nil }
            if paused.isEmpty { break }
            try? await Task.sleep(nanoseconds: 30_000_000_000) // 30s
            await Outbox.shared.flush()
            for def in paused {
                await syncType(def, reason: .backfill)
            }
        }
        lastSyncDate = Date()
        persistLastSync()
    }

    /// One anchored page for one type. Returns true when the type has more
    /// history to drain, false when it is complete or paused (upload failed).
    private func backfillOneChunk(_ def: HealthTypeDef) async -> Bool {
        let anchor = AnchorStore.shared.anchor(for: def.identifier)
        do {
            let result = try await runAnchored(type: def.sampleType,
                                               anchor: anchor,
                                               limit: backfillChunk)

            updateLastSample(for: def.identifier, from: result.added)
            if let pos = result.added.map({ $0.endDate }).max() {
                perTypeStatus[def.identifier]?.backfillDate = pos
            }

            if result.added.isEmpty && result.deleted.isEmpty {
                AnchorStore.shared.setAnchor(result.newAnchor, for: def.identifier)
                perTypeStatus[def.identifier]?.backfillComplete = true
                markSynced(def.identifier)
                return false
            }

            let samples = result.added.compactMap { dto(for: $0) }
            let deleted = result.deleted.map { $0.uuid.uuidString }
            let batch = Batch.make(samples: samples, deleted: deleted)

            let acked = await Uploader.shared.upload(batch)
            if acked {
                AnchorStore.shared.setAnchor(result.newAnchor, for: def.identifier)
                perTypeStatus[def.identifier]?.sentCount += samples.count
                markSynced(def.identifier)
                // Keep going until an EMPTY result (handled above) marks the end.
                // A short page is NOT the end: HealthKit can return a partial page
                // at a boundary between separately-synced batches, and treating it
                // as complete silently drops everything past the boundary. That is
                // what hid the [redacted] glucose behind the imported [redacted] block.
                return true
            } else {
                // Leave the anchor untouched so this window is re-fetched.
                Outbox.shared.enqueue(batch)
                perTypeStatus[def.identifier]?.lastError = "Backfill paused (\(Uploader.lastFailureReason))"
                return false
            }
        } catch {
            perTypeStatus[def.identifier]?.lastError = error.localizedDescription
            return false
        }
    }

    // MARK: Query wrappers

    private func runAnchored(type: HKSampleType,
                             anchor: HKQueryAnchor?,
                             limit: Int) async throws
        -> (added: [HKSample], deleted: [HKDeletedObject], newAnchor: HKQueryAnchor) {
        try await withCheckedThrowingContinuation { continuation in
            let query = HKAnchoredObjectQuery(type: type,
                                              predicate: nil,
                                              anchor: anchor,
                                              limit: limit) { _, samples, deleted, newAnchor, error in
                if let error {
                    continuation.resume(throwing: error)
                    return
                }
                // Never fall back to a zero anchor: that would silently reset the
                // resume point to the beginning of history. Reuse the prior anchor.
                continuation.resume(returning: (samples ?? [],
                                                deleted ?? [],
                                                newAnchor ?? anchor ?? HKQueryAnchor(fromValue: 0)))
            }
            store.execute(query)
        }
    }

    private func runSampleQuery(type: HKSampleType,
                                predicate: NSPredicate) async throws -> [HKSample] {
        try await withCheckedThrowingContinuation { continuation in
            let query = HKSampleQuery(sampleType: type,
                                      predicate: predicate,
                                      limit: HKObjectQueryNoLimit,
                                      sortDescriptors: nil) { _, samples, error in
                if let error {
                    continuation.resume(throwing: error)
                    return
                }
                continuation.resume(returning: samples ?? [])
            }
            store.execute(query)
        }
    }

    // MARK: Sample -> DTO

    private func dto(for sample: HKSample) -> SampleDTO? {
        let source = sample.sourceRevision.source.name // carries the curly apostrophe
        let start = ISO8601.string(from: sample.startDate)
        let end = ISO8601.string(from: sample.endDate)

        if let quantity = sample as? HKQuantitySample {
            guard let def = HealthTypes.byIdentifier[quantity.quantityType.identifier],
                  let unit = def.unit else { return nil }
            let value = quantity.quantity.doubleValue(for: unit)
            return SampleDTO(uuid: quantity.uuid.uuidString,
                             hk_type: def.identifier,
                             value: .number(value),
                             unit: def.unitString,
                             start: start,
                             end: end,
                             source_name: source)
        }

        if let category = sample as? HKCategorySample {
            // Currently only sleep analysis is a category type in the registry.
            let name = SleepValue.name(for: category.value)
            return SampleDTO(uuid: category.uuid.uuidString,
                             hk_type: HKCategoryTypeIdentifier.sleepAnalysis.rawValue,
                             value: .string(name),
                             unit: "",
                             start: start,
                             end: end,
                             source_name: source)
        }

        if let workout = sample as? HKWorkout {
            // Workouts are optional; we send duration in seconds as the value.
            return SampleDTO(uuid: workout.uuid.uuidString,
                             hk_type: "HKWorkoutTypeIdentifier",
                             value: .number(workout.duration),
                             unit: "s",
                             start: start,
                             end: end,
                             source_name: source)
        }

        return nil
    }

    // MARK: Status + notifications

    private func updateLastSample(for identifier: String, from samples: [HKSample]) {
        guard let latest = samples.map({ $0.endDate }).max() else { return }
        let current = perTypeStatus[identifier]?.lastSampleDate
        if current == nil || latest > current! {
            perTypeStatus[identifier]?.lastSampleDate = latest
        }
    }

    private func markSynced(_ identifier: String) {
        perTypeStatus[identifier]?.lastSyncDate = Date()
        perTypeStatus[identifier]?.lastError = nil
    }

    private func recordGlobalError(_ message: String) {
        for id in perTypeStatus.keys {
            perTypeStatus[id]?.lastError = message
        }
    }

    private func persistLastSync() {
        if let date = lastSyncDate {
            defaults.set(date, forKey: lastSyncKey)
        }
    }

    private func notifyIfFailingRepeatedly() {
        if consecutiveUploadFailures == 3 {
            NotificationManager.shared.notify(
                title: "Helios Bridge cannot reach the Mac",
                body: "Batches are queued in the outbox and will retry on the next sweep.",
                id: "helios.upload.failing")
        }
    }

    /// Notifies if a normally chatty stream (heart rate) has gone silent, which
    /// usually means the watch stopped feeding data or authorization was revoked.
    private func checkQuietStreams() {
        let id = HKQuantityTypeIdentifier.heartRate.rawValue
        guard let last = perTypeStatus[id]?.lastSampleDate else { return }
        if Date().timeIntervalSince(last) > 6 * 3600 {
            NotificationManager.shared.notify(
                title: "Helios Bridge: heart rate stream is quiet",
                body: "No new heart rate samples in over 6 hours.",
                id: "helios.stream.quiet")
        }
    }
}
