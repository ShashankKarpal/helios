import Foundation

// MARK: - Window cursors
// Everything the Bridge reads by date window instead of by anchor: the trailing
// safety sweep of a sample type, activity summaries, Apple's daily totals, and
// the owner's date-range re-reads. None of these pages ever enters the outbox.
// Each has a cursor that moves only after every page of a window was acked, so a
// failed page is simply read again from HealthKit next time.
public struct WindowCursor: Codable, Equatable {
    /// Every record up to this instant was covered by a fully acked window.
    public var coveredThrough: Date?
    /// When a recent-window run last completed.
    public var lastRunAt: Date?
    /// History backfill: where the next chunk starts (nil: not started).
    public var backfillNext: Date?
    public var backfillDone: Bool = false
    /// Records the backfill has sent so far. A backfill that found nothing is
    /// not marked done: HealthKit answers a type the owner has not granted with
    /// nothing, and the history must still come once it is granted.
    public var backfillRecords: Int = 0

    public init() {}

    enum CodingKeys: String, CodingKey {
        case coveredThrough, lastRunAt, backfillNext, backfillDone, backfillRecords
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        coveredThrough = try c.decodeIfPresent(Date.self, forKey: .coveredThrough)
        lastRunAt = try c.decodeIfPresent(Date.self, forKey: .lastRunAt)
        backfillNext = try c.decodeIfPresent(Date.self, forKey: .backfillNext)
        backfillDone = try c.decodeIfPresent(Bool.self, forKey: .backfillDone) ?? false
        backfillRecords = try c.decodeIfPresent(Int.self, forKey: .backfillRecords) ?? 0
    }
}

/// An owner-requested re-read of one stream over a date range (C8), processed
/// window by window; `next` moves only after a window's pages were all acked.
public struct RereadJob: Codable, Equatable, Identifiable {
    public var id: String
    public var stream: String
    public var start: Date
    public var end: Date
    public var next: Date
    public var reason: String
    public var createdAt: Date
    public var pagesSent: Int = 0
    public var lastError: String?

    public init(id: String = UUID().uuidString, stream: String, start: Date, end: Date, reason: String, createdAt: Date) {
        self.id = id
        self.stream = stream
        self.start = start
        self.end = end
        self.next = start
        self.reason = reason
        self.createdAt = createdAt
    }

    public var progress: Double {
        let total = end.timeIntervalSince(start)
        guard total > 0 else { return 1 }
        return min(1, max(0, next.timeIntervalSince(start) / total))
    }
}

/// A stream the receiver refused: drained again only after `until` (or on a
/// manual sync), so a page the Mac cannot take does not burn every wake (C4).
public struct StreamBlock: Codable, Equatable {
    public var until: Date
    public var reason: String
    public var status: Int
}

struct CursorState: Codable, Equatable {
    var windows: [String: WindowCursor] = [:]
    var jobs: [RereadJob] = []
    var pageLimits: [String: Int] = [:]
    var blocks: [String: StreamBlock] = [:]
    /// Per stream, the records left out of an acked page because a part of
    /// them (route points, ECG voltages, a workout's routes) could not be read:
    /// fetched again by uuid until they go (C10: the anchor moved past them only
    /// after this list held them).
    var retries: [String: [String]] = [:]

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        windows = try c.decodeIfPresent([String: WindowCursor].self, forKey: .windows) ?? [:]
        jobs = try c.decodeIfPresent([RereadJob].self, forKey: .jobs) ?? []
        pageLimits = try c.decodeIfPresent([String: Int].self, forKey: .pageLimits) ?? [:]
        blocks = try c.decodeIfPresent([String: StreamBlock].self, forKey: .blocks) ?? [:]
        retries = try c.decodeIfPresent([String: [String]].self, forKey: .retries) ?? [:]
    }
}

// MARK: - Cursor store
// One small JSON file, written atomically after every change. A file that does
// not decode is kept aside (never overwritten) and the store starts empty:
// sweeps then start from the last 48 hours, backfills from the beginning, both
// safe because the Mac dedupes; only open re-read jobs would be lost, and the
// status screen says so. A file that exists but cannot be READ (the phone
// before its first unlock, an I/O error) is not that case: every read tries the
// file again, and a write refuses while the store has not read the file, so a
// change worked out from the empty state is never written over the real one.
public final class CursorStore: @unchecked Sendable {   // guarded by `lock`
    public let url: URL
    private let lock = NSLock()
    private var state = CursorState()
    public private(set) var recoveredFromDamage = false
    private var notRead = false

    private static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.dateEncodingStrategy = .millisecondsSince1970
        e.outputFormatting = [.sortedKeys]
        return e
    }()
    private static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .millisecondsSince1970
        return d
    }()

    public init(url: URL) throws {
        self.url = url
        try DurableFile.makeDirectory(url.deletingLastPathComponent())
        DurableFile.removeStaleTemps(in: url.deletingLastPathComponent())
        load()
    }

    /// The file exists but could not be read yet: writes refuse until it is.
    public var unreadable: Bool {
        lock.lock(); defer { lock.unlock() }
        if notRead { load() }
        return notRead
    }

    private func load() {
        let data: Data?
        do {
            data = try DurableFile.read(url)
        } catch {
            notRead = true
            return
        }
        notRead = false
        guard let data else { return }
        if let s = try? Self.decoder.decode(CursorState.self, from: data) {
            state = s
        } else {
            let aside = url.appendingPathExtension("damaged-\(Int(Date().timeIntervalSince1970))")
            try? DurableFile.move(url, to: aside, point: "cursor.aside")
            recoveredFromDamage = true
        }
    }

    /// The state, read again first while the file has not been read (under the lock).
    private var current: CursorState {
        if notRead { load() }
        return state
    }

    private func commit(_ change: (inout CursorState) -> Void) throws {
        lock.lock(); defer { lock.unlock() }
        if notRead {
            // The caller worked from the empty state an unreadable file gave it:
            // refuse, and read the file again for the next caller.
            load()
            throw DurableError(step: "read", path: url.path, code: notRead ? EACCES : EAGAIN)
        }
        var next = state
        change(&next)
        try DurableFile.write(try Self.encoder.encode(next), to: url, point: "cursor.save")
        state = next   // memory follows the disk, never leads it
    }

    // MARK: Windows

    public func window(_ key: String) -> WindowCursor {
        lock.lock(); defer { lock.unlock() }
        return current.windows[key] ?? WindowCursor()
    }

    public func setWindow(_ key: String, _ cursor: WindowCursor) throws {
        try commit { $0.windows[key] = cursor }
    }

    // MARK: Jobs

    public var jobs: [RereadJob] {
        lock.lock(); defer { lock.unlock() }
        return current.jobs
    }

    public func addJob(_ job: RereadJob) throws {
        try commit { $0.jobs.append(job) }
    }

    public func updateJob(_ job: RereadJob) throws {
        try commit { s in
            if let i = s.jobs.firstIndex(where: { $0.id == job.id }) { s.jobs[i] = job }
        }
    }

    public func removeJob(_ id: String) throws {
        try commit { $0.jobs.removeAll { $0.id == id } }
    }

    // MARK: Page limits (halved on HTTP 413) and blocks (refusals)

    public func pageLimit(_ stream: String) -> Int? {
        lock.lock(); defer { lock.unlock() }
        return current.pageLimits[stream]
    }

    public func setPageLimit(_ stream: String, _ limit: Int) throws {
        try commit { $0.pageLimits[stream] = limit }
    }

    public func block(_ stream: String) -> StreamBlock? {
        lock.lock(); defer { lock.unlock() }
        return current.blocks[stream]
    }

    public var blocks: [String: StreamBlock] {
        lock.lock(); defer { lock.unlock() }
        return current.blocks
    }

    public func setBlock(_ stream: String, _ block: StreamBlock?) throws {
        try commit { $0.blocks[stream] = block }
    }

    /// Forgets a stream's window cursor (Reset and re-pull of that stream).
    public func resetWindow(_ key: String) throws {
        try commit { $0.windows[key] = nil }
    }

    // MARK: Records to read again

    public func retries(_ stream: String) -> [String] {
        lock.lock(); defer { lock.unlock() }
        return current.retries[stream] ?? []
    }

    public var retryCounts: [String: Int] {
        lock.lock(); defer { lock.unlock() }
        return current.retries.mapValues(\.count)
    }

    public func addRetries(_ stream: String, _ uuids: [String]) throws {
        guard !uuids.isEmpty else { return }
        try commit { s in
            var list = s.retries[stream] ?? []
            for u in uuids where !list.contains(u) { list.append(u) }
            s.retries[stream] = list
        }
    }

    /// Adds, then removes, in one write.
    public func updateRetries(_ stream: String, add: [String], remove: Set<String>) throws {
        let held = retries(stream)
        guard !add.isEmpty || !remove.isDisjoint(with: held) else { return }
        try commit { s in
            var list = s.retries[stream] ?? []
            for u in add where !list.contains(u) { list.append(u) }
            list.removeAll { remove.contains($0) }
            s.retries[stream] = list.isEmpty ? nil : list
        }
    }

    public func removeRetries(_ stream: String, _ uuids: Set<String>) throws {
        guard !uuids.isEmpty else { return }
        try commit { s in
            let left = (s.retries[stream] ?? []).filter { !uuids.contains($0) }
            s.retries[stream] = left.isEmpty ? nil : left
        }
    }
}
