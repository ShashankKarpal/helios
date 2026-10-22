import Foundation
import HealthKit
import Observation
import UIKit
import BridgeCore

// MARK: - HealthKitManager
// Owns authorization, the observer queries and background delivery, and the
// sync engine (BridgeCore), and mirrors the engine's state for the status screen.
// The engine does the work off the main thread; this class decides WHEN: an
// observer wake, a background refresh or processing task, the app coming to the
// foreground, an unlock, a button.
@MainActor
@Observable
final class HealthKitManager {
    static let shared = HealthKitManager()
    /// 2: workouts, routes, ECGs, activity summaries and the extra types (Phase 3).
    static let currentPermissionVersion = 2

    @ObservationIgnored let store = HKHealthStore()
    @ObservationIgnored let engine: SyncEngine
    @ObservationIgnored let wakeLog: WakeLog
    @ObservationIgnored private let defaults = UserDefaults.standard
    @ObservationIgnored private var observersStarted: Set<String> = []
    @ObservationIgnored private var hostObserver: NSObjectProtocol?
    /// A run asked for while one is going: it runs once more right after
    /// (the permission grant must not be lost behind the launch's own run).
    @ObservationIgnored private var rerun: (reason: String, force: Bool)?

    var snapshot: EngineSnapshot?
    var authorizationRequested: Bool
    var permissionVersion: Int
    var lastDeliveryDate: Date?
    var recentWakes: [WakeEvent] = []
    var wakeCounts: [String: Int] = [:]
    var setupNote: String?
    var syncing = false

    private enum Key {
        static let authRequested = "helios.authRequested"
        static let permissionVersion = "helios.permissionVersion"
        static let lastDelivery = "helios.lastDelivery"
    }

    private init() {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Helios", isDirectory: true)
        var note: String?
        let stores: (OutboxStore, AnchorStore, CursorStore)
        var fallback = false
        do {
            stores = try Self.makeStores(base)
        } catch {
            // The app container refused its own folders: run from a temporary
            // folder so the screen can say so, rather than crash at every launch.
            note = "Storage problem: \(error). Until it is fixed the Bridge runs from a temporary folder: it may send history again (the Mac removes duplicates), and the old queue stays where it is."
            fallback = true
            stores = try! Self.makeStores(FileManager.default.temporaryDirectory
                .appendingPathComponent("Helios-fallback", isDirectory: true))
        }
        let (outbox, anchors, cursors) = stores
        // Never into the temporary folder: iOS may empty it, and the v1 queue
        // holds the only copy of its pages.
        if !fallback {
            do {
                if let m = try outbox.migrateLegacy(at: base.appendingPathComponent("outbox.json"), now: Date()),
                   m.quarantined {
                    note = "The old outbox file could not be read; it is kept aside, nothing was deleted."
                }
            } catch {
                note = "The old outbox could not be moved yet (\(error)); it is kept and tried again at the next launch."
            }
        }
        // A damaged cursors file took the read-again list with it: before the
        // engine starts, workouts, routes and ECGs start again from the beginning.
        do {
            if !(try SyncEngine.recoverHeldRecords(cursors: cursors, anchors: anchors)).isEmpty {
                note = "The sync cursors file was damaged and kept aside. Sweeps restart from the last 48 hours, and workouts, routes and ECGs are read again from the beginning (the Mac removes duplicates)."
            } else if cursors.recoveredFromDamage {
                note = "The sync cursors file was damaged and kept aside; sweeps restart from the last 48 hours."
            }
        } catch {
            note = "The sync cursors file was damaged; reading workouts, routes and ECGs again could not be set up yet (\(error)). It is tried again at the next launch."
        }
        let info = Bundle.main.infoDictionary
        let build = BuildInfo(appVersion: info?["CFBundleShortVersionString"] as? String ?? "?",
                              build: info?["CFBundleVersion"] as? String ?? "?",
                              osVersion: UIDevice.current.systemVersion, zone: TimeZone.current.identifier)
        engine = SyncEngine(outbox: outbox, anchors: anchors, cursors: cursors,
                            source: HealthKitSource(store: store), uploader: Uploader.shared, build: build)
        wakeLog = WakeLog(url: base.appendingPathComponent("wakelog.json"))
        setupNote = note
        let requested = UserDefaults.standard.bool(forKey: Key.authRequested)
        let stored = UserDefaults.standard.integer(forKey: Key.permissionVersion)
        authorizationRequested = requested
        permissionVersion = stored > 0 ? stored : (requested ? 1 : 0)   // a v1 install asked for the v1 types
        lastDeliveryDate = UserDefaults.standard.object(forKey: Key.lastDelivery) as? Date
        refreshWakes()
        let engine = self.engine
        Task {
            await engine.setObserver { snap in
                Task { @MainActor in HealthKitManager.shared.apply(snap) }
            }
        }
        hostObserver = NotificationCenter.default.addObserver(forName: .heliosHostChanged, object: nil, queue: .main) { _ in
            Task { await engine.linkChanged() }
        }
    }

    private static func makeStores(_ base: URL) throws -> (OutboxStore, AnchorStore, CursorStore) {
        (try OutboxStore(root: base.appendingPathComponent("Outbox", isDirectory: true)),
         try AnchorStore(dir: base.appendingPathComponent("Anchors", isDirectory: true)),
         try CursorStore(url: base.appendingPathComponent("cursors.json")))
    }

    private func apply(_ snap: EngineSnapshot) {
        snapshot = snap
        if let d = snap.lastDeliveryAt, d != lastDeliveryDate {
            lastDeliveryDate = d
            defaults.set(d, forKey: Key.lastDelivery)
        }
    }

    // MARK: Which streams may run

    /// Only streams whose read permission has been asked for: HealthKit answers
    /// a type it was never asked about with nothing, which would look like an
    /// empty history.
    var allowedStreams: [StreamDef] {
        guard authorizationRequested else { return [] }
        let defs = permissionVersion >= 2 ? TypeRegistry.all : TypeRegistry.registered
        return defs.filter { HealthTypes.sampleType($0) != nil }
    }

    var needsNewPermissions: Bool {
        authorizationRequested && permissionVersion < Self.currentPermissionVersion
    }

    func status(_ def: StreamDef) -> StreamStatus? { snapshot?.streams[def.key] }

    // MARK: Authorization

    /// First launch, and the Phase 3 upgrade: iOS shows only the types not
    /// decided yet. Then the new streams drain their history (the backfill).
    func requestAuthorizationAndStart() async {
        guard HKHealthStore.isHealthDataAvailable() else {
            setupNote = "Health data is not available on this device."
            return
        }
        do {
            try await store.requestAuthorization(toShare: [], read: HealthTypes.readTypes)
            authorizationRequested = true
            permissionVersion = Self.currentPermissionVersion
            defaults.set(true, forKey: Key.authRequested)
            defaults.set(permissionVersion, forKey: Key.permissionVersion)
            startObservers()
            await runForeground(reason: "permissions", force: true)
        } catch {
            setupNote = "Authorization failed: \(error.localizedDescription)"
        }
    }

    // MARK: Observers and background delivery

    func bootstrap() {
        guard HKHealthStore.isHealthDataAvailable(), authorizationRequested else { return }
        startObservers()
    }

    /// One observer per stream that has background delivery. The extra types are
    /// drained when the app runs (foreground and BG tasks), to keep wakes few.
    func startObservers() {
        for def in allowedStreams where def.frequency != .none && !observersStarted.contains(def.key) {
            guard let type = HealthTypes.sampleType(def) else { continue }
            observersStarted.insert(def.key)
            let query = HKObserverQuery(sampleType: type, predicate: nil) { _, completion, error in
                if error != nil {
                    completion()   // always complete, so HealthKit does not back off
                    return
                }
                Task { @MainActor in
                    await HealthKitManager.shared.handleObserverWake(def, completion: completion)
                }
            }
            store.execute(query)
            store.enableBackgroundDelivery(for: type, frequency: def.frequency == .immediate ? .immediate : .hourly) { _, _ in }
        }
    }

    /// An observer wake: drain the woken stream inside a short budget, flushing
    /// the outbox first, and ALWAYS complete inside the budget. A completion
    /// that comes late, or never because iOS suspended the app first, makes
    /// HealthKit back off its deliveries.
    func handleObserverWake(_ def: StreamDef, completion: @escaping () -> Void) async {
        let started = Date()
        // HealthKit's completion, exactly once: when the work ends, at the
        // deadline whatever the work is doing, or when iOS ends the background
        // time first (it suspends the app right after, and a timer would never fire).
        let done = OnceCall(completion)
        let task = BackgroundTime.begin("helios.observer", onExpiry: done)
        defer { task.end() }
        guard UIApplication.shared.isProtectedDataAvailable else {
            done.call()
            log("observer", def.shortName, started, outcome: "locked")
            return
        }
        let deadline = Deadline.wake(start: started, budget: 20,
                                     remaining: UIApplication.shared.backgroundTimeRemaining, now: Date())
        let timer = Deadline.callAt(deadline, done)
        await engine.flush(deadline: deadline)
        let outcome = await engine.drain(def, reason: "observer", deadline: deadline)
        await engine.runRetries(def, reason: "observer", deadline: deadline)
        done.call()
        timer.cancel()
        log("observer", def.shortName, started, outcome: outcome.rawValue)
        if outcome == .budget { BGTaskManager.shared.scheduleProcessing() }
        checkAlerts()
    }

    // MARK: Runs

    /// Foreground, Sync Now, the deep link, the permission grant: everything, no
    /// deadline. Fresh data first (the registered types), then the classes and
    /// the extra types, which drain their history on the first runs.
    func runForeground(reason: String, force: Bool) async {
        guard authorizationRequested else { return }
        if syncing {
            rerun = (reason, force || (rerun?.force ?? false))
            return
        }
        syncing = true
        var next: (reason: String, force: Bool)? = (reason, force)
        while let run = next {
            let started = Date()
            await work(reason: run.reason, force: run.force, deadline: nil, includeExtra: true)
            log(run.reason, nil, started, outcome: "done")
            checkAlerts()
            next = rerun
            rerun = nil
        }
        syncing = false
    }

    /// A background refresh or an unlock: about 25 seconds.
    func runBackground(reason: String, budget: TimeInterval) async -> Bool {
        guard authorizationRequested else { return true }
        let started = Date()
        guard UIApplication.shared.isProtectedDataAvailable else {
            await engine.flush(deadline: started.addingTimeInterval(budget))
            log(reason, "outbox only", started, outcome: "locked")
            return true
        }
        await work(reason: reason, force: false, deadline: started.addingTimeInterval(budget), includeExtra: false)
        log(reason, nil, started, outcome: "done")
        checkAlerts()
        return true
    }

    /// A background processing task: as long as iOS allows (it calls interrupt).
    func runProcessing() async {
        guard authorizationRequested else { return }
        let started = Date()
        if UIApplication.shared.isProtectedDataAvailable {
            await work(reason: "processing", force: false, deadline: nil, includeExtra: true)
        } else {
            await engine.flush()
        }
        log("processing", nil, started, outcome: UIApplication.shared.isProtectedDataAvailable ? "done" : "locked")
        checkAlerts()
    }

    private func work(reason: String, force: Bool, deadline: Date?, includeExtra: Bool) async {
        await engine.flush(deadline: deadline, force: force)
        let allowed = allowedStreams
        let first = allowed.filter { $0.registered || $0.cls == .workout || $0.cls == .route || $0.cls == .ecg }
        for def in first {
            await engine.drain(def, reason: reason, deadline: deadline, manual: force)
        }
        for def in allowed where def.trailingSweep {
            await engine.sweep(def, reason: reason, force: force, deadline: deadline)
        }
        await engine.window(.dailyStatistics, reason: reason, deadline: deadline)
        if permissionVersion >= 2 {
            await engine.window(.activitySummaries, reason: reason, deadline: deadline)
        }
        await engine.runJobs(reason: reason, deadline: deadline)
        if includeExtra {
            for def in allowed where !first.contains(def) {
                await engine.drain(def, reason: reason, deadline: deadline, manual: force)
            }
        }
        await engine.runRetries(reason: reason, deadline: deadline, force: force)
        await engine.flush(deadline: deadline)
    }

    /// The sync arrow on one row: that stream now, its sweep forced.
    func syncNow(_ def: StreamDef) async {
        await engine.flush()
        await engine.drain(def, reason: "manual", manual: true)
        await engine.runRetries(def, reason: "manual", force: true)
        await engine.sweep(def, reason: "manual", force: true)
    }

    /// Reset and re-pull one stream: its whole history again (the Mac dedupes).
    func reset(_ def: StreamDef) async {
        do {
            try await engine.reset(def)
        } catch {
            setupNote = "Reset of \(def.shortName) failed: \(error)"
            return
        }
        await engine.drain(def, reason: "reset", manual: true)
    }

    /// Re-read one stream over a date range (C8).
    func reread(_ def: StreamDef, from: Date, to: Date) async {
        do {
            try await engine.addRereadJob(def, start: from, end: to, reason: "owner")
        } catch {
            setupNote = "Could not save the re-read: \(error)"
            return
        }
        await engine.runJobs(reason: "owner")
    }

    func cancelReread(_ id: String) async {
        try? await engine.cancelRereadJob(id)
    }

    /// Puts refused pages back in the queue after a fix on the Mac.
    func retryParked() async {
        _ = try? engine.outbox.unparkAll()
        await engine.flush(force: true)
    }

    func interrupt() async {
        await engine.interrupt()
    }

    // MARK: Wake log

    private func log(_ kind: String, _ detail: String?, _ started: Date, outcome: String) {
        wakeLog.append(WakeEvent(at: started, kind: kind, detail: detail,
                                 seconds: (Date().timeIntervalSince(started) * 10).rounded() / 10, outcome: outcome))
        refreshWakes()
    }

    func refreshWakes() {
        recentWakes = wakeLog.recent(30)
        wakeCounts = wakeLog.counts(since: Date().addingTimeInterval(-24 * 3600))
    }

    // MARK: Alerts

    /// One outage alert per outage (NotificationManager coalesces), and the
    /// writer-app quiet-stream alert for heart rate.
    private func checkAlerts() {
        guard let snap = snapshot else { return }
        let stale = (lastDeliveryDate ?? .distantPast) < Date().addingTimeInterval(-15 * 60)
        if let failure = snap.linkFailure, stale {
            NotificationManager.shared.reportDeliveryFailure(reason: failure, queued: snap.outbox.queued)
        } else if snap.linkFailure == nil, !stale {
            NotificationManager.shared.reportDeliverySuccess()
        }
        if let newest = snap.streams[HKQuantityTypeIdentifier.heartRate.rawValue]?.newestRecord {
            if Date().timeIntervalSince(newest) > Self.quietStreamThreshold {
                NotificationManager.shared.reportQuietStream(lastSample: newest)
            } else {
                NotificationManager.shared.reportStreamRecovered()
            }
        }
    }

    /// Matches the Mac policy (heart_rate cadence 12 h, alert at 2x): heart rate
    /// is protected data, readable only while the phone is unlocked (audit B2).
    static let quietStreamThreshold: TimeInterval = 24 * 3600
}

// MARK: - Background time
/// Extra seconds from iOS while a wake finishes its work. Ended exactly once:
/// by the work, or by iOS's expiry, which also calls `onExpiry` (HealthKit's
/// completion for an observer wake) and stops the engine at its next page.
@MainActor
final class BackgroundTime {
    private var id = UIBackgroundTaskIdentifier.invalid

    static func begin(_ name: String, onExpiry: OnceCall? = nil) -> BackgroundTime {
        let time = BackgroundTime()
        time.id = UIApplication.shared.beginBackgroundTask(withName: name) {
            // iOS calls this on the main thread and wants the task ended before it returns.
            onExpiry?.call()
            MainActor.assumeIsolated { time.end() }
            Task { await HealthKitManager.shared.interrupt() }
        }
        return time
    }

    func end() {
        guard id != .invalid else { return }
        UIApplication.shared.endBackgroundTask(id)
        id = .invalid
    }
}
