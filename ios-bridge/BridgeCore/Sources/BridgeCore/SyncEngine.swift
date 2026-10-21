import Foundation

// MARK: - What the engine needs from the outside
/// HealthKit, seen through what the engine needs. The app implements it; the
/// tests implement it with a fake store of records.
public protocol RecordSource: AnyObject {
    /// One page of a stream after an opaque anchor (nil: from the beginning).
    func anchoredPage(_ def: StreamDef, anchor: Data?, limit: Int) async throws -> AnchoredPage
    /// The pages of a dated window, oldest first, at most `limit` records each.
    func windowPages(_ kind: WindowKind, start: Date, end: Date, limit: Int) async throws -> [BatchContent]
}

public struct AnchoredPage {
    public var content: BatchContent
    public var newAnchor: Data

    public init(content: BatchContent, newAnchor: Data) {
        self.content = content
        self.newAnchor = newAnchor
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
    /// TLS, 401 or 403 (token or allowlist), 408, 429, 5xx, 507, 2xx without ack.
    case link(String)
    /// The receiver read the batch and refused it (400, 413, 422).
    case refused(Int, String)
}

public protocol BatchUploader: AnyObject {
    func upload(_ body: Data) async -> UploadOutcome
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
    public var minPageLimit = 25
    public var maxConcurrentDrains = 2
    public var windowPageLimit = 2000

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
}

public enum DrainOutcome: String, Equatable {
    case complete, budget, linkDown, refused, readFailed, locked, anchorNotSaved, blocked, cancelled
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
// only after an ack. Anchored pages: the anchor moves after the ack; a failed page
// may wait in the outbox, which deletes it only on an ack or once the anchor has
// moved past it. Dated windows (sweeps, re-reads, summaries, daily totals): never
// queued; their cursor moves after every page of a window was acked.
//
// One drain per stream at a time (C2): a wake during a drain marks it dirty and
// the running drain makes one more pass, instead of a second loop sending the
// same pages. At most `maxConcurrentDrains` streams drain at once. A failed page
// is retried on the next wake with the outbox backoff, the stream is not parked
// (C4). A Host change bumps the link epoch; running work stops at its next page (C7).
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
    private var verifiedEpoch: Int?
    private var lastLinkFailureAt: Date?
    private var lastLinkFailure: String?
    private var lastDeliveryAt: Date?
    private var running: [String: Task<DrainOutcome, Never>] = [:]
    private var dirty: Set<String> = []
    private var flushTask: Task<FlushReport, Never>?
    private var windowTasks: [String: Task<WindowOutcome, Never>] = [:]
    private var jobsTask: Task<Int, Never>?
    private var slotsFree: Int
    private var slotWaiters: [CheckedContinuation<Void, Never>] = []

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
    }

    public func setObserver(_ observer: ((EngineSnapshot) -> Void)?) {
        self.observer = observer
        publish()
    }

    // MARK: Snapshot

    public func snapshot() -> EngineSnapshot {
        EngineSnapshot(streams: statuses, outbox: outbox.counts, jobs: cursors.jobs, blocks: cursors.blocks,
                       lastDeliveryAt: lastDeliveryAt, linkFailure: lastLinkFailure,
                       linkVerified: verifiedEpoch == linkEpoch, bookkeepingError: outbox.lastBookkeepingError)
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

    // MARK: Concurrency limit

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
    @discardableResult
    public func drain(_ def: StreamDef, reason: String, deadline: Date? = nil, manual: Bool = false) async -> DrainOutcome {
        if let task = running[def.key] {
            dirty.insert(def.key)
            return await task.value
        }
        let epoch = linkEpoch
        let task = Task { await self.runDrain(def, reason: reason, deadline: deadline, manual: manual, epoch: epoch) }
        running[def.key] = task
        return await task.value
    }

    public func isDraining(_ key: String) -> Bool { running[key] != nil }

    private func runDrain(_ def: StreamDef, reason: String, deadline: Date?, manual: Bool, epoch: Int) async -> DrainOutcome {
        await acquireSlot()
        mark(def.key) { $0.running = true }
        publish()
        var outcome = DrainOutcome.complete
        repeat {
            dirty.remove(def.key)
            outcome = await drainPass(def, reason: reason, deadline: deadline, manual: manual, epoch: epoch)
        } while outcome == .complete && dirty.contains(def.key) && !expired(deadline)
        // No suspension between the last dirty check and here: a wake that arrives
        // later finds no running drain and starts its own.
        running[def.key] = nil
        dirty.remove(def.key)
        mark(def.key) { $0.running = false }
        releaseSlot()
        publish()
        return outcome
    }

    private func drainPass(_ def: StreamDef, reason: String, deadline: Date?, manual: Bool, epoch: Int) async -> DrainOutcome {
        if !manual, let block = cursors.block(def.key), block.until > clock() { return .blocked }
        var limit = cursors.pageLimit(def.key) ?? def.pageLimit
        while true {
            if Task.isCancelled || epoch != linkEpoch { return .cancelled }
            if expired(deadline) { return .budget }
            let generation = anchors.generation(def.key)
            let anchor = anchors.anchor(def.key)
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
            if page.content.isEmpty {
                if page.newAnchor != anchor {
                    // Nothing new after the anchor: moving it puts no data at risk.
                    do {
                        try anchors.save(page.newAnchor, for: def.key, ifGeneration: generation)
                    } catch {
                        mark(def.key) { $0.lastError = "resume point not saved: \(error)" }
                        return .anchorNotSaved
                    }
                    prune(stream: def.key)
                }
                mark(def.key) { s in s.caughtUp = true; s.lastError = nil; s.waitingForUnlock = false }
                if cursors.block(def.key) != nil { try? cursors.setBlock(def.key, nil) }
                return .complete
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
            switch await uploader.upload(encoded.body) {
            case .acked(let reply):
                delivered(reply, epoch: epoch)
                let moved: Bool
                do {
                    moved = try anchors.save(page.newAnchor, for: def.key, ifGeneration: generation)
                } catch {
                    mark(def.key) { $0.lastError = "delivered, but the resume point was not saved: \(error)" }
                    return .anchorNotSaved
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
        let current = anchors.generation(stream)
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
    /// queued page of the old drain is superseded. The Mac dedupes the re-sent
    /// records. No 3-year dated sweep any more: that is a date-range re-read now (C8).
    public func reset(_ def: StreamDef) throws {
        try anchors.clear(def.key)
        prune(stream: def.key)
        try cursors.resetWindow(WindowKind.samples(def).cursorKey)
        if cursors.block(def.key) != nil { try cursors.setBlock(def.key, nil) }
        statuses[def.key] = StreamStatus()
        publish()
    }

    // MARK: Outbox flush

    /// Delivers queued pages oldest first. Stops after two link failures in a
    /// row. An acked anchored copy moves its stream's anchor when no other save
    /// happened since it was read, so the drain does not send it a second time.
    @discardableResult
    public func flush(deadline: Date? = nil, force: Bool = false) async -> FlushReport {
        if let task = flushTask { return await task.value }
        let epoch = linkEpoch
        let task = Task { await self.runFlush(deadline: deadline, force: force, epoch: epoch) }
        flushTask = task
        return await task.value
    }

    private func runFlush(deadline: Date?, force: Bool, epoch: Int) async -> FlushReport {
        var report = FlushReport()
        report.pruned = pruneSuperseded()
        var consecutive = 0
        let candidates = force ? outbox.entries : outbox.due(now: clock())
        loop: for entry in candidates {
            if Task.isCancelled || epoch != linkEpoch || expired(deadline) { break }
            guard let body = try? outbox.body(entry.name) else { continue }
            switch await uploader.upload(body) {
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
            }
        }
        _ = outbox.removeMigratedCopies(in: outbox.root.deletingLastPathComponent())
        flushTask = nil
        publish()
        return report
    }

    // MARK: Dated windows

    /// The trailing safety sweep of a registered sample type: the last 48 hours,
    /// or back to where the last fully acked sweep reached when sweeps have been
    /// failing. At most every `sweepEvery`, never while the link is down, unless forced.
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
        if let task = windowTasks[key] { return await task.value }
        let epoch = linkEpoch
        let task = Task { await self.runWindow(kind, reason: reason, force: force, deadline: deadline, epoch: epoch) }
        windowTasks[key] = task
        let outcome = await task.value
        return outcome
    }

    private func runWindow(_ kind: WindowKind, reason: String, force: Bool, deadline: Date?, epoch: Int) async -> WindowOutcome {
        let key = kind.cursorKey
        defer { windowTasks[key] = nil }
        let now = clock()
        var cursor = cursors.window(key)
        if !force, let block = cursors.block(key), block.until > now { return .skipped }
        let every: TimeInterval
        let recentStart: Date
        let chunk: TimeInterval
        switch kind {
        case .samples:
            every = config.sweepEvery
            chunk = config.sweepChunk
            recentStart = min(now.addingTimeInterval(-config.trailingWindow),
                              (cursor.coveredThrough ?? now).addingTimeInterval(-config.trailingWindow))
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
                let outcome = await sendWindow(kind, start: start, end: end, reason: reason, deadline: deadline, epoch: epoch)
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
            let outcome = await sendWindow(kind, start: next, end: end, reason: reason, deadline: deadline, epoch: epoch)
            if outcome != .done { return outcome }
            next = end
            cursor.backfillNext = next
            do { try cursors.setWindow(key, cursor) } catch { return .notSaved }
        }
        cursor.backfillDone = true
        do { try cursors.setWindow(key, cursor) } catch { return .notSaved }
        return .done
    }

    /// Reads one window and sends every page. Nothing is queued: on a failure
    /// the caller's cursor stays put and the window is read again next time.
    private func sendWindow(_ kind: WindowKind, start: Date, end: Date, reason: String, deadline: Date?,
                            epoch: Int) async -> WindowOutcome {
        let pages: [BatchContent]
        do {
            pages = try await source.windowPages(kind, start: start, end: end, limit: config.windowPageLimit)
        } catch SourceError.locked {
            return .locked
        } catch {
            if case .samples(let def) = kind { mark(def.key) { $0.lastError = "sweep: \(error)" } }
            return .readFailed
        }
        for page in pages where !page.isEmpty {
            if Task.isCancelled || epoch != linkEpoch { return .cancelled }
            if expired(deadline) { return .budget }
            let stream: String?
            if case .samples(let def) = kind { stream = def.key } else { stream = nil }
            guard let encoded = try? encode(page, origin: kind.origin, stream: stream, generation: nil, reason: reason) else {
                return .readFailed
            }
            switch await uploader.upload(encoded.body) {
            case .acked(let reply):
                delivered(reply, epoch: epoch)
            case .link(let why):
                linkFailed(why)
                return .linkDown
            case .refused(let status, let why):
                try? cursors.setBlock(kind.cursorKey, StreamBlock(until: clock().addingTimeInterval(config.refusalBlock),
                                                                  reason: why, status: status))
                return .refused
            }
        }
        return .done
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
        if let task = jobsTask { return await task.value }
        let epoch = linkEpoch
        let task = Task { await self.runJobsNow(reason: reason, deadline: deadline, epoch: epoch) }
        jobsTask = task
        return await task.value
    }

    private func runJobsNow(reason: String, deadline: Date?, epoch: Int) async -> Int {
        defer { jobsTask = nil; publish() }
        var finished = 0
        for var job in cursors.jobs {
            guard let def = TypeRegistry.byKey[job.stream] else {
                try? cursors.removeJob(job.id)
                continue
            }
            while job.next < job.end {
                if expired(deadline) || epoch != linkEpoch { return finished }
                let end = min(job.next.addingTimeInterval(config.jobWindow), job.end)
                let outcome = await sendWindow(.samples(def), start: job.next, end: end, reason: "reread:" + reason,
                                               deadline: deadline, epoch: epoch)
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
