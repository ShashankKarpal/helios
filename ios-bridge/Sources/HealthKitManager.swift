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
    var lastSampleDate: Date?
    var lastSyncDate: Date?
    var lastError: String?
    var sentCount: Int = 0
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
    @ObservationIgnored private let backfillChunk = 5000
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
        await performSweep(reason: reason)
        await trailingSweep(hours: 48)
    }

    /// One type: run the anchored query from its persisted anchor, build a batch,
    /// upload, and advance the anchor ONLY on a positive ack.
    func syncType(_ def: HealthTypeDef, reason: SweepReason) async {
        let anchor = AnchorStore.shared.anchor(for: def.identifier)
        do {
            let result = try await runAnchored(type: def.sampleType,
                                               anchor: anchor,
                                               limit: HKObjectQueryNoLimit)

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
            } else {
                // Mac unreachable: queue and DO NOT advance the anchor, so the
                // same window is re-fetched next time (idempotent, uuid-keyed).
                Outbox.shared.enqueue(batch)
                perTypeStatus[def.identifier]?.lastError = "Queued (Mac unreachable)"
                consecutiveUploadFailures += 1
                notifyIfFailingRepeatedly()
            }
        } catch {
            perTypeStatus[def.identifier]?.lastError = error.localizedDescription
        }
    }

    /// Time-window safety net. Does not touch anchors; overlapping samples are
    /// deduped by the Mac on uuid.
    func trailingSweep(hours: Int) async {
        guard let start = Calendar.current.date(byAdding: .hour, value: -hours, to: Date()) else { return }
        let predicate = HKQuery.predicateForSamples(withStart: start, end: Date(), options: [])

        for def in HealthTypes.all {
            do {
                let samples = try await runSampleQuery(type: def.sampleType, predicate: predicate)
                guard !samples.isEmpty else { continue }
                let dtos = samples.compactMap { dto(for: $0) }
                guard !dtos.isEmpty else { continue }
                let batch = Batch.make(samples: dtos, deleted: [])
                let acked = await Uploader.shared.upload(batch)
                if !acked { Outbox.shared.enqueue(batch) }
            } catch {
                perTypeStatus[def.identifier]?.lastError = error.localizedDescription
            }
        }
    }

    // MARK: Backfill

    /// Paginated, resumable backfill over full history. For each type we page the
    /// anchored query in chunks; the anchor advances only after each chunk is
    /// acked, so an interruption resumes exactly where it stopped.
    func backfill() async {
        for def in HealthTypes.all {
            await backfillType(def)
        }
        lastSyncDate = Date()
        persistLastSync()
    }

    private func backfillType(_ def: HealthTypeDef) async {
        while true {
            let anchor = AnchorStore.shared.anchor(for: def.identifier)
            do {
                let result = try await runAnchored(type: def.sampleType,
                                                   anchor: anchor,
                                                   limit: backfillChunk)

                updateLastSample(for: def.identifier, from: result.added)

                if result.added.isEmpty && result.deleted.isEmpty {
                    AnchorStore.shared.setAnchor(result.newAnchor, for: def.identifier)
                    markSynced(def.identifier)
                    break
                }

                let samples = result.added.compactMap { dto(for: $0) }
                let deleted = result.deleted.map { $0.uuid.uuidString }
                let batch = Batch.make(samples: samples, deleted: deleted)

                let acked = await Uploader.shared.upload(batch)
                if acked {
                    AnchorStore.shared.setAnchor(result.newAnchor, for: def.identifier)
                    perTypeStatus[def.identifier]?.sentCount += samples.count
                    markSynced(def.identifier)
                    // A short page means we have reached the end of history.
                    if result.added.count < backfillChunk { break }
                } else {
                    // Stop this type and leave the anchor untouched so it resumes.
                    Outbox.shared.enqueue(batch)
                    perTypeStatus[def.identifier]?.lastError = "Backfill paused (Mac unreachable)"
                    break
                }
            } catch {
                perTypeStatus[def.identifier]?.lastError = error.localizedDescription
                break
            }
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
                continuation.resume(returning: (samples ?? [],
                                                deleted ?? [],
                                                newAnchor ?? HKQueryAnchor(fromValue: 0)))
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
