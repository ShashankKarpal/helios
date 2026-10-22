import Foundation

// MARK: - What the engine needs from the outside
/// HealthKit, seen through what the engine needs. The app implements it; the
/// tests implement it with a fake store of records.
public protocol RecordSource: AnyObject {
    /// One page of a stream after an opaque anchor (nil: from the beginning).
    /// A record whose parts could not be read (route points, ECG voltages, a
    /// workout's routes) is left out of the content and named in `retry`.
    func anchoredPage(_ def: StreamDef, anchor: Data?, limit: Int) async throws -> AnchoredPage
    /// The pages of a dated window, oldest first, at most `limit` records each.
    func windowPages(_ kind: WindowKind, start: Date, end: Date, limit: Int) async throws -> [BatchContent]
    /// Records of a stream by uuid (the read-again list), with their parts. A
    /// record whose parts still cannot be read is left out, like one HealthKit
    /// does not return (deleted, or its type turned off: HealthKit answers both
    /// the same way, so neither takes a uuid off the list).
    func records(_ def: StreamDef, uuids: [String]) async throws -> BatchContent
}

public struct AnchoredPage {
    public var content: BatchContent
    public var newAnchor: Data
    /// Records of this page left out because a part of them could not be read.
    public var retry: [String]

    public init(content: BatchContent, newAnchor: Data, retry: [String] = []) {
        self.content = content
        self.newAnchor = newAnchor
        self.retry = retry
    }
}

public enum SourceError: Error, Equatable {
    /// Health data is unreadable while the phone is locked (HealthKit
    /// errorDatabaseInaccessible). Not a fault: the drain resumes on unlock.
    case locked
    case unavailable(String)
}

public enum UploadOutcome: Equatable {
    /// The receiver answered 2xx with "ack": true. Nothing else is a delivery.
    case acked(AckReply)
    /// No answer, or one that says nothing about this batch: a timeout, no route,
    /// TLS, 401 or 403 (token or allowlist), 408, 429, 5xx, 507, 2xx without ack,
    /// or no time left before the caller's deadline.
    case link(String)
    /// The receiver read the batch and refused it (400, 413, 422).
    case refused(Int, String)
    /// The caller's deadline cut the upload, or left no time to start it. Not a
    /// link failure: like any work past a deadline, it waits for the next run.
    case outOfTime(String)
}

public protocol BatchUploader: AnyObject {
    /// With a deadline: one attempt, finished (or given up) by the deadline.
    func upload(_ body: Data, deadline: Date?) async -> UploadOutcome
}

public struct BuildInfo: Equatable {
    public var appVersion: String
    public var build: String
    public var osVersion: String
    public var zone: String

    public init(appVersion: String, build: String, osVersion: String, zone: String) {
        self.appVersion = appVersion
        self.build = build
        self.osVersion = osVersion
        self.zone = zone
    }
}

public struct EngineConfig: Equatable {
    public var trailingWindow: TimeInterval = 48 * 3600
    public var sweepEvery: TimeInterval = 6 * 3600
    public var sweepChunk: TimeInterval = 24 * 3600
    public var summariesEvery: TimeInterval = 3600
    public var recentDays = 3
    public var historyChunkDays = 120
    public var historyStart = Date(timeIntervalSince1970: 1_546_300_800)   // 2019-01-01 UTC
    public var jobWindow: TimeInterval = 7 * 86400
    public var linkDownGrace: TimeInterval = 120
    public var refusalBlock: TimeInterval = 3600
    /// A 413 halves a stream's page down to this (one record).
    public var minPageLimit = 1
    /// Open-ended drains (foreground, processing) at once.
    public var maxConcurrentDrains = 2
    /// Drains with a deadline (observer wakes, refresh) at once; a wake that
    /// finds no slot before its deadline leaves the stream for the next run.
    public var maxBudgetDrains = 3
    public var windowPageLimit = 2000
    /// Held records fetched and sent per page, at most (and never more than the
    /// stream's own page limit).
    public var retryBatch = 25

    public init() {}
}

// MARK: - Status for the screen
public struct StreamStatus: Equatable {
    public var sentRecords = 0
    public var pagesAcked = 0
    public var lastAckAt: Date?
    public var lastError: String?
    public var waitingForUnlock = false
    public var newestRecord: Date?
    /// Where the history drain stands: the latest record end of the last acked page.
    public var position: Date?
    public var caughtUp = false
    public var running = false
    public var relayQueued: Int?

    public init() {}
}

public struct EngineSnapshot: Equatable {
    public var streams: [String: StreamStatus]
    public var outbox: OutboxCounts
    public var jobs: [RereadJob]
    public var blocks: [String: StreamBlock]
    public var lastDeliveryAt: Date?
    public var linkFailure: String?
    public var linkVerified: Bool
    public var bookkeepingError: String?
    /// Records per stream waiting to be read again by uuid.
    public var retries: [String: Int]
    public var outboxUnreadable: Int
    public var cursorsUnreadable: Bool
}

public enum DrainOutcome: String, Equatable {
    /// notSaved: a durable write failed (the read-again list or the resume
    /// point); nothing moved past the page, which is read again next time.
    case complete, budget, linkDown, refused, readFailed, locked, notSaved, blocked, cancelled
    /// A caller with a budget found the stream already draining: the running
    /// drain makes one more pass, and the caller returns at once.
    case joined
}

public enum WindowOutcome: String, Equatable {
    case done, skipped, linkDown, refused, readFailed, locked, notSaved, budget, cancelled
}

public struct FlushReport: Equatable {
    public var delivered = 0
    public var failed = 0
    public var parked = 0
    public var pruned = 0
    public var anchorsAdvanced = 0
    public var stoppedOnLink = false

    public init() {}
}

// MARK: - The engine
// One rule (C10): nothing leaves the phone's durable state unless the receiver
// acked it, or HealthKit can give it again through a cursor that itself moves
// only after an ack. Anchored pages: the anchor moves after the ack and never on
// an empty page; a failed page may wait in the outbox, which deletes it only on
// an ack or once the anchor has moved past it; a record whose parts could not be
// read goes into a durable read-again list before the anchor moves past it.
// Dated windows (sweeps, re-reads, summaries, daily totals): never queued; their
// cursor moves after every page of a window was acked, and never backwards.
//
// One drain per stream at a time (C2): a wake during a drain marks it dirty and
// the running drain makes one more pass, instead of a second loop sending the
// same pages. At most `maxConcurrentDrains` open-ended drains and
// `maxBudgetDrains` drains with a deadline at once. A failed page is retried on
// the next wake with the outbox backoff, the stream is not parked (C4). A Host
// change bumps the link epoch; running work stops at its next page (C7). A
// caller with a deadline never waits past it: not for running work, not for a
// slot, not for an upload.
public actor SyncEngine {
    // Thread-safe stores, readable from outside the actor (the status screen, tests).
    public nonisolated let outbox: OutboxStore
    public nonisolated let anchors: AnchorStore
    public nonisolated let cursors: CursorStore
    private let source: RecordSource
    private let uploader: BatchUploader
    private let clock: () -> Date
    private let build: BuildInfo
    public private(set) var config: EngineConfig
    private var observer: ((EngineSnapshot) -> Void)?

    private var statuses: [String: StreamStatus] = [:]
    private var linkEpoch = 0
    /// Moves when iOS ends a background task: running work stops at its next page.
    private var runEpoch = 0
    private var verifiedEpoch: Int?
    private var lastLinkFailureAt: Date?
    private var lastLinkFailure: String?
    private var lastDeliveryAt: Date?
    private var running: [String: Task<DrainOutcome, Never>] = [:]
    private var dirty: Set<String> = []
    private var flushTask: Task<FlushReport, Never>?
    private var windowTasks: [String: Task<WindowOutcome, Never>] = [:]
    private var jobsTask: Task<Int, Never>?
    private var retryTasks: [String: Task<Int, Never>] = [:]
    private var slotsFree: Int
    private var slotWaiters: [CheckedContinuation<Void, Never>] = []
    private var budgetSlotsFree: Int
    private var budgetWaiters: [(id: Int, resume: CheckedContinuation<Bool, Never>)] = []
    private var nextWaiterID = 0

    public init(outbox: OutboxStore, anchors: AnchorStore, cursors: CursorStore, source: RecordSource,
                uploader: BatchUploader, build: BuildInfo, config: EngineConfig = EngineConfig(),
                clock: @escaping () -> Date = Date.init) {
        self.outbox = outbox
        self.anchors = anchors
        self.cursors = cursors
        self.source = source
        self.uploader = uploader
        self.build = build
        self.config = config
        self.clock = clock
        self.slotsFree = max(1, config.maxConcurrentDrains)
        self.budgetSlotsFree = max(1, config.maxBudgetDrains)
    }

    public func setObserver(_ observer: ((EngineSnapshot) -> Void)?) {
        self.observer = observer
        publish()
    }

    // MARK: Snapshot

    public func snapshot() -> EngineSnapshot {
        EngineSnapshot(streams: statuses, outbox: outbox.counts, jobs: cursors.jobs, blocks: cursors.blocks,
                       lastDeliveryAt: lastDeliveryAt, linkFailure: lastLinkFailure,
                       linkVerified: verifiedEpoch == linkEpoch, bookkeepingError: outbox.lastBookkeepingError,
                       retries: cursors.retryCounts, outboxUnreadable: outbox.unreadable,
                       cursorsUnreadable: cursors.unreadable)
    }

    private func publish() {
        observer?(snapshot())
    }

    private func mark(_ key: String, _ change: (inout StreamStatus) -> Void) {
        var s = statuses[key] ?? StreamStatus()
        change(&s)
        statuses[key] = s
    }

    // MARK: Link state

    /// A Host change (C7): the old link's "ok" no longer says anything, and
    /// running drains and flushes stop at their next page.
    public func linkChanged() {
        linkEpoch += 1
        verifiedEpoch = nil
        lastLinkFailure = nil
        lastLinkFailureAt = nil
        publish()
    }

    /// iOS is ending the app's background time: stop at the next page. Nothing
    /// is lost; every cursor is where the last ack left it.
    public func interrupt() {
        runEpoch += 1
    }

    private func current(_ epoch: Int, _ run: Int) -> Bool {
        epoch == linkEpoch && run == runEpoch
    }

    public var linkRecentlyDown: Bool {
        guard let at = lastLinkFailureAt else { return false }
        return clock().timeIntervalSince(at) < config.linkDownGrace
    }

    /// An ack. It verifies the link only when the upload started on the current
    /// host: a page that was in flight across a Host change says nothing about the new one.
    private func delivered(_ reply: AckReply, epoch: Int) {
        lastDeliveryAt = clock()
        guard epoch == linkEpoch else { return }
        lastLinkFailure = nil
        lastLinkFailureAt = nil
        verifiedEpoch = epoch
    }

    private func linkFailed(_ why: String) {
        lastLinkFailure = why
        lastLinkFailureAt = clock()
    }

    private func expired(_ deadline: Date?) -> Bool {
        guard let deadline else { return false }
        return clock() >= deadline
    }

    // MARK: Concurrency limits

    private func acquireSlot() async {
        if slotsFree > 0 {
            slotsFree -= 1
            return
        }
        await withCheckedContinuation { slotWaiters.append($0) }
    }

    private func releaseSlot() {
        if slotWaiters.isEmpty {
            slotsFree += 1
        } else {
            slotWaiters.removeFirst().resume()
        }
    }

    /// A slot for a drain with a deadline; false when none freed up in time.
    private func acquireBudgetSlot(deadline: Date) async -> Bool {
        if budgetSlotsFree > 0 {
            budgetSlotsFree -= 1
            return true
        }
        let wait = deadline.timeIntervalSince(clock())
        guard wait > 0 else { return false }
        nextWaiterID += 1
        let id = nextWaiterID
        return await withCheckedContinuation { (c: CheckedContinuation<Bool, Never>) in
            budgetWaiters.append((id, c))
            Task {
                try? await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
                self.giveUpBudgetWait(id)   // this Task runs on the engine's actor
            }
        }
    }

    private func giveUpBudgetWait(_ id: Int) {
        if let i = budgetWaiters.firstIndex(where: { $0.id == id }) {
            budgetWaiters.remove(at: i).resume.resume(returning: false)
        }
    }

    private func releaseBudgetSlot() {
        if budgetWaiters.isEmpty {
            budgetSlotsFree += 1
        } else {
            budgetWaiters.removeFirst().resume.resume(returning: true)
        }
    }

    // MARK: Encoding

    private func encode(_ content: BatchContent, origin: PageOrigin, stream: String?, generation: Int?,
                        reason: String) throws -> (body: Data, batchID: String) {
        let meta = BridgeMeta(appVersion: build.appVersion, build: build.build, osVersion: build.osVersion,
                              zone: build.zone, reason: reason, origin: origin, stream: stream,
                              generation: generation,
                              windowStart: content.windowStart.map(WireTime.string),
                              windowEnd: content.windowEnd.map(WireTime.string))
        let batch = BatchV2(sentAt: clock(), bridge: meta, content: content)
        return (try batch.body(), batch.batchID)
    }

    // MARK: Anchored drains

    /// Drains one stream from its anchor until HealthKit returns an empty page,
    /// the deadline passes, or a page fails. A call while the stream is already
    /// draining joins that drain, which then makes one more pass.
    /// A caller with a deadline (an observer wake, a background task) never waits
    /// for work it did not start: HealthKit wants its completion inside the wake's
    /// budget, and a late one makes it back off its deliveries.
    @discardableResult
    public func drain(_ def: StreamDef, reason: String, deadline: Date? = nil, manual: Bool = false) async -> DrainOutcome {
        if let task = running[def.key] {
            dirty.insert(def.key)
            if deadline != nil { return .joined }
            let epoch = linkEpoch, run = runEpoch
            let outcome = await task.value
            // Carry on without a deadline when the running drain ran out of its
            // deadline (or never got a slot), skipped a block this caller
            // overrides, or was stopped by an interrupt or a Host change that
            // came before this caller did.
            let carryOn = outcome == .budget || (manual && outcome == .blocked)
                || (outcome == .cancelled && current(epoch, run))
            guard carryOn else { return outcome }
            return await drain(def, reason: reason, deadline: nil, manual: manual)
        }
        let epoch = linkEpoch, run = runEpoch
        let task = Task { await self.runDrain(def, reason: reason, deadline: deadline, manual: manual, epoch: epoch, run: run) }
        running[def.key] = task
        return await task.value
    }

    public func isDraining(_ key: String) -> Bool { running[key] != nil }

    private func runDrain(_ def: StreamDef, reason: String, deadline: Date?, manual: Bool, epoch: Int,
                          run: Int) async -> DrainOutcome {
        // Two limits: open-ended drains (the v1 storm was 18 of them at once), and
        // drains with a deadline (every observer wake at a launch), which wait for
        // a slot only until their deadline.
        if let deadline {
            guard await acquireBudgetSlot(deadline: deadline) else {
                running[def.key] = nil
                return .budget
            }
        } else {
            await acquireSlot()
        }
        mark(def.key) { $0.running = true }
        publish()
        var outcome = DrainOutcome.complete
        repeat {
            dirty.remove(def.key)
            outcome = await drainPass(def, reason: reason, deadline: deadline, manual: manual, epoch: epoch, run: run)
        } while outcome == .complete && dirty.contains(def.key) && !expired(deadline)
        // No suspension between the last dirty check and here: a wake that arrives
        // later finds no running drain and starts its own. A pass that did not
        // complete keeps a joined wake's mark for the next drain.
        running[def.key] = nil
        if outcome == .complete { dirty.remove(def.key) }
        mark(def.key) { $0.running = false }
        if deadline != nil { releaseBudgetSlot() } else { releaseSlot() }
        publish()
        return outcome
    }

    private func drainPass(_ def: StreamDef, reason: String, deadline: Date?, manual: Bool, epoch: Int,
                           run: Int) async -> DrainOutcome {
        if !manual, let block = cursors.block(def.key), block.until > clock() { return .blocked }
        var limit = cursors.pageLimit(def.key) ?? def.pageLimit
        while true {
            if Task.isCancelled || !current(epoch, run) { return .cancelled }
            if expired(deadline) { return .budget }
            let generation: Int, anchor: Data?
            do {
                (generation, anchor) = try anchors.state(def.key)
            } catch {
                // Never read as "no anchor": that would send the whole history again
                // and, worse, save a fresh anchor over the real one.
                mark(def.key) { $0.lastError = "resume point unreadable: \(error)" }
                return .readFailed
            }
            let page: AnchoredPage
            do {
                page = try await source.anchoredPage(def, anchor: anchor, limit: limit)
            } catch SourceError.locked {
                mark(def.key) { $0.waitingForUnlock = true; $0.lastError = "waiting for unlock" }
                return .locked
            } catch {
                mark(def.key) { $0.lastError = "HealthKit: \(error)" }
                return .readFailed
            }
            if page.content.isEmpty && page.retry.isEmpty {
                // Nothing after the anchor: keep the anchor where it is. HealthKit
                // answers a type the owner has turned off with an empty page and a
                // later anchor; saving that anchor would skip whatever is written
                // while the type is off, once it is on again. Reading from the old
                // anchor again costs one cheap query.
                mark(def.key) { s in s.caughtUp = true; s.lastError = nil; s.waitingForUnlock = false }
                if cursors.block(def.key) != nil { try? cursors.setBlock(def.key, nil) }
                return .complete
            }
            // The left-out records are held before anything else: a copy of this
            // page left in the outbox can move the anchor past them on its own ack.
            // A held record this page deletes has nothing left to send.
            do {
                try cursors.updateRetries(def.key, add: page.retry, remove: Set(page.content.deleted))
            } catch {
                mark(def.key) { $0.lastError = "read-again list not saved: \(error)" }
                return .notSaved
            }
            if page.content.isEmpty {
                // Only records whose parts could not be read, now held: move on.
                do {
                    try anchors.save(page.newAnchor, for: def.key, ifGeneration: generation)
                } catch {
                    mark(def.key) { $0.lastError = "resume point not saved: \(error)" }
                    return .notSaved
                }
                prune(stream: def.key)
                continue
            }
            let encoded: (body: Data, batchID: String)
            do {
                encoded = try encode(page.content, origin: .anchored, stream: def.key, generation: generation,
                                     reason: reason)
            } catch {
                mark(def.key) { $0.lastError = "could not encode a page: \(error)" }
                return .readFailed
            }
            let header = ItemHeader(batchID: encoded.batchID, origin: .anchored, stream: def.key,
                                    generation: generation, nextAnchor: page.newAnchor, types: page.content.types,
                                    count: page.content.recordCount + page.content.deleted.count,
                                    createdAt: clock(), windowStart: page.content.windowStart,
                                    windowEnd: page.content.windowEnd)
            switch await uploader.upload(encoded.body, deadline: deadline) {
            case .acked(let reply):
                delivered(reply, epoch: epoch)
                let moved: Bool
                do {
                    moved = try anchors.save(page.newAnchor, for: def.key, ifGeneration: generation)
                } catch {
                    mark(def.key) { $0.lastError = "delivered, but the resume point was not saved: \(error)" }
                    return .notSaved
                }
                prune(stream: def.key)
                let count = page.content.recordCount
                mark(def.key) { s in
                    s.sentRecords += count
                    s.pagesAcked += 1
                    s.lastAckAt = self.clock()
                    s.lastError = nil
                    s.waitingForUnlock = false
                    s.caughtUp = false
                    s.relayQueued = reply.queued
                    if let end = page.content.windowEnd {
                        s.position = end
                        s.newestRecord = max(s.newestRecord ?? end, end)
                    }
                }
                if cursors.block(def.key) != nil { try? cursors.setBlock(def.key, nil) }
                if !moved {
                    // A queued copy's ack moved the anchor meanwhile; read on from there.
                    continue
                }
            case .link(let why):
                linkFailed(why)
                var note = "queued (\(why))"
                do {
                    try outbox.enqueue(header, body: encoded.body)
                } catch {
                    note = "not queued, read again later (\(error))"
                }
                mark(def.key) { $0.lastError = note }
                return .linkDown
            case .outOfTime:
                // The deadline, not the link: the page waits in the outbox for the
                // next run (it may have arrived; the Mac dedupes), or is read again
                // from the anchor when the disk did not take it.
                _ = try? outbox.enqueue(header, body: encoded.body)
                return .budget
            case .refused(let status, let why):
                if status == 413, limit > config.minPageLimit {
                    limit = max(config.minPageLimit, limit / 2)
                    try? cursors.setPageLimit(def.key, limit)
                    continue
                }
                if let result = try? outbox.enqueue(header, body: encoded.body) {
                    switch result {
                    case .written(let name), .replaced(let name, _):
                        try? outbox.park(name, reason: "HTTP \(status): \(why)")
                    case .duplicate:
                        break
                    }
                }
                try? cursors.setBlock(def.key, StreamBlock(until: clock().addingTimeInterval(config.refusalBlock),
                                                           reason: why, status: status))
                mark(def.key) { $0.lastError = "the Mac refused a page (HTTP \(status))" }
                return .refused
            }
        }
    }

    /// Deletes this stream's queued pages the anchor has moved past.
    @discardableResult
    private func prune(stream: String) -> Int {
        let current = anchors.generation(stream)   // 0 when unreadable: nothing is pruned
        return outbox.prune { $0.stream == stream && ($0.generation ?? Int.max) < current }
    }

    @discardableResult
    public func pruneSuperseded() -> Int {
        outbox.prune { header in
            guard let stream = header.stream, let generation = header.generation else { return false }
            return generation < anchors.generation(stream)
        }
    }

    /// Reset and re-pull (C6): the stream starts again from the beginning; every
    /// queued page of the old drain is superseded, and its read-again list too
    /// (the re-read reads those records again). The Mac dedupes the re-sent
    /// records. No 3-year dated sweep any more: that is a date-range re-read (C8).
    public func reset(_ def: StreamDef) throws {
        try anchors.clear(def.key)
        prune(stream: def.key)
        try cursors.resetWindow(WindowKind.samples(def).cursorKey)
        try cursors.removeRetries(def.key, Set(cursors.retries(def.key)))
        if cursors.block(def.key) != nil { try cursors.setBlock(def.key, nil) }
        statuses[def.key] = StreamStatus()
        publish()
    }

    // MARK: Read-again list

    /// The block key of a stream's read-again run (after a refusal).
    public static func retryBlockKey(_ stream: String) -> String { "retry:" + stream }

    /// Fetches every stream's held records by uuid and sends them. Returns the
    /// records delivered. `force` (Sync Now) runs a stream whose read-again
    /// run waits after a refusal.
    @discardableResult
    public func runRetries(reason: String, deadline: Date? = nil, force: Bool = false) async -> Int {
        var delivered = 0
        for key in cursors.retryCounts.keys.sorted() {
            guard let def = TypeRegistry.byKey[key] else { continue }
            if expired(deadline) { break }
            delivered += await runRetries(def, reason: reason, deadline: deadline, force: force)
        }
        return delivered
    }

    /// Fetches one stream's held records by uuid and sends them, in pages no
    /// larger than the stream's own (a 413 halves it, down to one record). A
    /// uuid leaves the list when its record was acked, or when an anchored page
    /// carried its deletion; never because HealthKit did not return it. A page
    /// the Mac refuses goes to the end of the list, so the records behind it
    /// are not held up, and the stream's read-again run waits an hour; its
    /// records stay held and counted on the status screen.
    @discardableResult
    public func runRetries(_ def: StreamDef, reason: String, deadline: Date? = nil, force: Bool = false) async -> Int {
        if let task = retryTasks[def.key] {
            if deadline != nil { return 0 }
            return await task.value
        }
        let epoch = linkEpoch, run = runEpoch
        let task = Task {
            await self.runRetriesNow(def, reason: reason, deadline: deadline, force: force, epoch: epoch, run: run)
        }
        retryTasks[def.key] = task
        return await task.value
    }

    private func runRetriesNow(_ def: StreamDef, reason: String, deadline: Date?, force: Bool, epoch: Int,
                               run: Int) async -> Int {
        defer { retryTasks[def.key] = nil; publish() }
        let blockKey = Self.retryBlockKey(def.key)
        if !force, let block = cursors.block(blockKey), block.until > clock() { return 0 }
        var delivered = 0
        var limit = max(config.minPageLimit, min(config.retryBatch, cursors.pageLimit(def.key) ?? def.pageLimit))
        var pending = cursors.retries(def.key)
        while !pending.isEmpty {
            if Task.isCancelled || !current(epoch, run) || expired(deadline) { return delivered }
            let chunk = Array(pending.prefix(limit))
            let content: BatchContent
            do {
                content = try await source.records(def, uuids: chunk)
            } catch {
                return delivered     // locked, or HealthKit failed: the list stays as it is
            }
            let found = content.recordUUIDs.intersection(chunk)
            guard !found.isEmpty else {
                pending.removeFirst(chunk.count)   // still unreadable, or not returned: held, try the next ones
                continue
            }
            guard let encoded = try? encode(content, origin: .reread, stream: def.key, generation: nil,
                                            reason: "retry:" + reason) else { return delivered }
            switch await uploader.upload(encoded.body, deadline: deadline) {
            case .acked(let reply):
                self.delivered(reply, epoch: epoch)
                delivered += content.recordCount
                do { try cursors.removeRetries(def.key, found) } catch { return delivered }
                pending.removeFirst(chunk.count)
                if cursors.block(blockKey) != nil { try? cursors.setBlock(blockKey, nil) }
            case .link(let why):
                linkFailed(why)
                return delivered
            case .outOfTime:
                return delivered
            case .refused(let status, let why):
                if status == 413, limit > config.minPageLimit {
                    // Too large for the Mac or the relay: the same records again, fewer per page.
                    limit = max(config.minPageLimit, limit / 2)
                    try? cursors.setPageLimit(def.key, limit)
                    continue
                }
                try? cursors.rotateRetries(def.key, chunk)
                try? cursors.setBlock(blockKey, StreamBlock(until: clock().addingTimeInterval(config.refusalBlock),
                                                            reason: why, status: status))
                return delivered
            }
        }
        return delivered
    }

    // MARK: After a damaged cursors file

    /// A cursors file that did not decode took the read-again list with it,
    /// and the anchors of the streams with parts (workouts, routes, ECGs) had
    /// moved past those records. Those streams start again from the beginning
    /// (the Mac dedupes). The damaged copy is marked only afterwards, so a crash
    /// on the way means the next launch does it again. Call before the engine
    /// starts. Returns the streams started again.
    @discardableResult
    public static func recoverHeldRecords(cursors: CursorStore, anchors: AnchorStore) throws -> [String] {
        let copies = cursors.damagedCopies
        guard !copies.isEmpty else { return [] }
        let streams = TypeRegistry.all.filter(\.hasParts).map(\.key)
        for key in streams { try anchors.clear(key) }
        for copy in copies { try cursors.markRecovered(copy) }
        return streams
    }

    // MARK: Outbox flush

    /// Delivers queued pages oldest first. Stops after two link failures in a
    /// row. An acked anchored copy moves its stream's anchor when no other save
    /// happened since it was read, so the drain does not send it a second time.
    @discardableResult
    public func flush(deadline: Date? = nil, force: Bool = false) async -> FlushReport {
        if let task = flushTask {
            if deadline != nil { return FlushReport() }   // a running flush is already doing it
            return await task.value
        }
        let epoch = linkEpoch, run = runEpoch
        let task = Task { await self.runFlush(deadline: deadline, force: force, epoch: epoch, run: run) }
        flushTask = task
        return await task.value
    }

    private func runFlush(deadline: Date?, force: Bool, epoch: Int, run: Int) async -> FlushReport {
        var report = FlushReport()
        // Pages that could not be read at launch (before the first unlock) are
        // listed once they can be.
        if outbox.unreadable > 0 { outbox.reload() }
        report.pruned = pruneSuperseded()
        var consecutive = 0
        let candidates = force ? outbox.entries : outbox.due(now: clock())
        loop: for entry in candidates {
            if Task.isCancelled || !current(epoch, run) || expired(deadline) { break }
            guard let body = try? outbox.body(entry.name) else { continue }
            switch await uploader.upload(body, deadline: deadline) {
            case .acked(let reply):
                delivered(reply, epoch: epoch)
                consecutive = 0
                if (try? outbox.acked(entry.name)) != nil { report.delivered += 1 }
                if entry.header.origin == .anchored, let stream = entry.header.stream,
                   let generation = entry.header.generation, let next = entry.header.nextAnchor,
                   (try? anchors.save(next, for: stream, ifGeneration: generation)) == true {
                    report.anchorsAdvanced += 1
                    prune(stream: stream)
                }
            case .link(let why):
                linkFailed(why)
                outbox.failed(entry.name, reason: why, status: nil, now: clock())
                report.failed += 1
                consecutive += 1
                if consecutive >= 2 {
                    report.stoppedOnLink = true
                    break loop
                }
            case .refused(let status, let why):
                if (try? outbox.park(entry.name, reason: "HTTP \(status): \(why)")) != nil { report.parked += 1 }
            case .outOfTime:
                break loop   // the deadline: no failure, no backoff
            }
        }
        _ = outbox.removeMigratedCopies(in: outbox.root.deletingLastPathComponent())
        flushTask = nil
        publish()
        return report
    }

    // MARK: Dated windows

    /// The trailing safety sweep of a registered sample type: the last 48 hours
    /// (blood glucose: 14 days), or back to where the last fully acked sweep
    /// reached when sweeps have been failing. At most every `sweepEvery`, never
    /// while the link is down, unless forced.
    @discardableResult
    public func sweep(_ def: StreamDef, reason: String, force: Bool = false, deadline: Date? = nil) async -> WindowOutcome {
        guard def.trailingSweep else { return .skipped }
        return await window(.samples(def), reason: reason, force: force, deadline: deadline)
    }

    /// Activity summaries or Apple's daily totals: the recent days every run,
    /// then the history in chunks until it is complete.
    @discardableResult
    public func window(_ kind: WindowKind, reason: String, force: Bool = false, deadline: Date? = nil) async -> WindowOutcome {
        let key = kind.cursorKey
        if let task = windowTasks[key] {
            if deadline != nil { return .skipped }
            return await task.value
        }
        let epoch = linkEpoch, run = runEpoch
        let task = Task { await self.runWindow(kind, reason: reason, force: force, deadline: deadline, epoch: epoch, run: run) }
        windowTasks[key] = task
        let outcome = await task.value
        return outcome
    }

    private func runWindow(_ kind: WindowKind, reason: String, force: Bool, deadline: Date?, epoch: Int,
                           run: Int) async -> WindowOutcome {
        let key = kind.cursorKey
        defer { windowTasks[key] = nil }
        let now = clock()
        var cursor = cursors.window(key)
        if !force, let block = cursors.block(key), block.until > now { return .skipped }
        let every: TimeInterval
        let recentStart: Date
        let chunk: TimeInterval
        switch kind {
        case .samples(let def):
            every = config.sweepEvery
            chunk = config.sweepChunk
            let reach = def.sweepWindow ?? config.trailingWindow
            recentStart = min(now.addingTimeInterval(-reach), (cursor.coveredThrough ?? now).addingTimeInterval(-reach))
        case .activitySummaries, .dailyStatistics:
            every = config.summariesEvery
            chunk = Double(config.historyChunkDays) * 86400
            let recent = now.addingTimeInterval(-Double(config.recentDays) * 86400)
            recentStart = min(recent, cursor.coveredThrough.map { $0.addingTimeInterval(-86400) } ?? recent)
        }
        let due = force || cursor.lastRunAt.map { now.timeIntervalSince($0) >= every } ?? true
        if due {
            if !force && linkRecentlyDown { return .skipped }
            var start = recentStart
            while start < now {
                let end = min(start.addingTimeInterval(chunk), now)
                let (outcome, _) = await sendWindow(kind, start: start, end: end, reason: reason, deadline: deadline,
                                                    epoch: epoch, run: run)
                if outcome != .done { return outcome }
                // Never backwards: a window that starts before the old mark re-covers
                // ground the mark already vouches for.
                cursor.coveredThrough = max(cursor.coveredThrough ?? end, end)
                do { try cursors.setWindow(key, cursor) } catch { return .notSaved }
                start = end
            }
            cursor.lastRunAt = now
            do { try cursors.setWindow(key, cursor) } catch { return .notSaved }
        }
        if case .samples = kind { return due ? .done : .skipped }
        guard !cursor.backfillDone else { return due ? .done : .skipped }
        var next = cursor.backfillNext ?? config.historyStart
        while next < recentStart {
            if expired(deadline) { return .budget }
            let end = min(next.addingTimeInterval(chunk), recentStart)
            let (outcome, records) = await sendWindow(kind, start: next, end: end, reason: reason,
                                                      deadline: deadline, epoch: epoch, run: run)
            if outcome != .done { return outcome }
            next = end
            cursor.backfillNext = next
            cursor.backfillRecords += records
            do { try cursors.setWindow(key, cursor) } catch { return .notSaved }
        }
        if cursor.backfillRecords > 0 {
            cursor.backfillDone = true
        } else {
            cursor.backfillNext = nil   // found nothing: look again next time (cheap while empty)
        }
        do { try cursors.setWindow(key, cursor) } catch { return .notSaved }
        return .done
    }

    /// Reads one window and sends every page. Nothing is queued: on a failure
    /// the caller's cursor stays put and the window is read again next time.
    private func sendWindow(_ kind: WindowKind, start: Date, end: Date, reason: String, deadline: Date?,
                            epoch: Int, run: Int) async -> (WindowOutcome, Int) {
        if Task.isCancelled || !current(epoch, run) { return (.cancelled, 0) }
        if expired(deadline) { return (.budget, 0) }
        let pages: [BatchContent]
        do {
            pages = try await source.windowPages(kind, start: start, end: end, limit: config.windowPageLimit)
        } catch SourceError.locked {
            return (.locked, 0)
        } catch {
            if case .samples(let def) = kind { mark(def.key) { $0.lastError = "sweep: \(error)" } }
            return (.readFailed, 0)
        }
        var records = 0
        for page in pages where !page.isEmpty {
            if Task.isCancelled || !current(epoch, run) { return (.cancelled, records) }
            if expired(deadline) { return (.budget, records) }
            let stream: String?
            if case .samples(let def) = kind { stream = def.key } else { stream = nil }
            guard let encoded = try? encode(page, origin: kind.origin, stream: stream, generation: nil, reason: reason) else {
                return (.readFailed, records)
            }
            switch await uploader.upload(encoded.body, deadline: deadline) {
            case .acked(let reply):
                delivered(reply, epoch: epoch)
                records += page.recordCount
            case .link(let why):
                linkFailed(why)
                return (.linkDown, records)
            case .outOfTime:
                return (.budget, records)
            case .refused(let status, let why):
                try? cursors.setBlock(kind.cursorKey, StreamBlock(until: clock().addingTimeInterval(config.refusalBlock),
                                                                  reason: why, status: status))
                return (.refused, records)
            }
        }
        return (.done, records)
    }

    // MARK: Date-range re-reads (C8)

    @discardableResult
    public func addRereadJob(_ def: StreamDef, start: Date, end: Date, reason: String) throws -> RereadJob {
        let job = RereadJob(stream: def.key, start: start, end: end, reason: reason, createdAt: clock())
        try cursors.addJob(job)
        publish()
        return job
    }

    public func cancelRereadJob(_ id: String) throws {
        try cursors.removeJob(id)
        publish()
    }

    /// Works through the open re-read jobs, window by window. Returns how many finished.
    @discardableResult
    public func runJobs(reason: String, deadline: Date? = nil) async -> Int {
        if let task = jobsTask {
            if deadline != nil { return 0 }
            return await task.value
        }
        let epoch = linkEpoch, run = runEpoch
        let task = Task { await self.runJobsNow(reason: reason, deadline: deadline, epoch: epoch, run: run) }
        jobsTask = task
        return await task.value
    }

    private func runJobsNow(reason: String, deadline: Date?, epoch: Int, run: Int) async -> Int {
        defer { jobsTask = nil; publish() }
        var finished = 0
        for var job in cursors.jobs {
            guard let def = TypeRegistry.byKey[job.stream] else {
                try? cursors.removeJob(job.id)
                continue
            }
            while job.next < job.end {
                if expired(deadline) || !current(epoch, run) { return finished }
                let end = min(job.next.addingTimeInterval(config.jobWindow), job.end)
                let (outcome, _) = await sendWindow(.samples(def), start: job.next, end: end, reason: "reread:" + reason,
                                                    deadline: deadline, epoch: epoch, run: run)
                if outcome != .done {
                    job.lastError = outcome.rawValue
                    try? cursors.updateJob(job)
                    return finished
                }
                job.next = end
                job.pagesSent += 1
                job.lastError = nil
                do { try cursors.updateJob(job) } catch { return finished }
            }
            try? cursors.removeJob(job.id)
            finished += 1
        }
        return finished
    }
}
