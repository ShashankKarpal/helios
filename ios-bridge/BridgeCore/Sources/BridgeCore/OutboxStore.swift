import Foundation

// MARK: - Outbox item header
/// What the outbox knows about a queued page without reading its body.
public enum ItemOrigin: String, Codable {
    /// A page read from a stream's anchor. HealthKit can give it again from that
    /// anchor until the anchor moves, and the anchor only moves after an ack.
    case anchored
    /// A page carried over from the v1 outbox, whose origin is unknown. It is the
    /// only copy, so it leaves the outbox only on an ack.
    case legacy
}

public struct ItemHeader: Codable, Equatable {
    public var batchID: String
    public var origin: ItemOrigin
    /// Anchored pages: the stream whose anchor the page was read from, the
    /// stream's anchor generation at that moment, and the anchor HealthKit
    /// returned after the page (so an ack of the queued copy can move the anchor).
    public var stream: String?
    public var generation: Int?
    public var nextAnchor: Data?
    public var types: [String]
    public var count: Int
    public var createdAt: Date
    public var windowStart: Date?
    public var windowEnd: Date?

    enum CodingKeys: String, CodingKey {
        case batchID = "batch_id"
        case origin, stream, generation
        case nextAnchor = "next_anchor"
        case types, count
        case createdAt = "created_at"
        case windowStart = "window_start"
        case windowEnd = "window_end"
    }

    public init(batchID: String, origin: ItemOrigin, stream: String? = nil, generation: Int? = nil,
                nextAnchor: Data? = nil, types: [String], count: Int, createdAt: Date,
                windowStart: Date? = nil, windowEnd: Date? = nil) {
        self.batchID = batchID
        self.origin = origin
        self.stream = stream
        self.generation = generation
        self.nextAnchor = nextAnchor
        self.types = types
        self.count = count
        self.createdAt = createdAt
        self.windowStart = windowStart
        self.windowEnd = windowEnd
    }
}

public struct OutboxEntry: Equatable {
    public let name: String          // file name; sorts by creation time
    public let header: ItemHeader
    public let size: Int
}

/// Retry bookkeeping. Kept apart from the item files and written best effort:
/// losing it resets a backoff, never loses a page.
public struct RetryState: Codable, Equatable {
    public var attempts: Int = 0
    public var nextAttemptAt: Date?
    public var lastAttemptAt: Date?
    public var lastError: String?
    public var lastStatus: Int?
    public var parkedReason: String?

    public init() {}
}

public enum EnqueueResult: Equatable {
    case written(String)
    /// A larger page from the same anchor replaced a smaller queued one.
    case replaced(String, old: String)
    /// The same anchor's page is already queued (or parked) with at least as many records.
    case duplicate(String)
}

public struct OutboxCounts: Equatable {
    public var queued: Int
    public var parked: Int
    public var quarantined: Int
    public var bytes: Int
    public var legacy: Int
}

// MARK: - Outbox store
// One file per page in <root>, named "<created ms>-<seq>-<batch id>.item":
//   line 1  HELIOS-OUTBOX/2
//   line 2  the header as JSON
//   rest    the exact request body
// A page leaves <root> only by: an ack (deleted), a refusal (moved to parked/),
// or a superseded generation (deleted: the anchor moved past it, so its data was
// delivered or is still ahead of the anchor). Nothing is dropped for its attempt
// count. A file that cannot be read is moved to quarantine/, never deleted.
public final class OutboxStore: @unchecked Sendable {   // every member is guarded by `lock`
    public static let magic = Data("HELIOS-OUTBOX/2\n".utf8)
    static let headerLimit = 256 * 1024
    static let suffix = ".item"

    public let root: URL
    public var parkedDir: URL { root.appendingPathComponent("parked", isDirectory: true) }
    public var quarantineDir: URL { root.appendingPathComponent("quarantine", isDirectory: true) }
    var retryURL: URL { root.appendingPathComponent("retry-state.json") }

    private let lock = NSLock()
    private var queued: [String: OutboxEntry] = [:]
    private var parked: [String: OutboxEntry] = [:]
    private var retry: [String: RetryState] = [:]
    private var quarantined = 0
    private var seq = 0
    /// The last failure to save retry bookkeeping (shown on the status screen).
    public private(set) var lastBookkeepingError: String?

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

    public init(root: URL) throws {
        self.root = root
        try DurableFile.makeDirectory(root)
        try DurableFile.makeDirectory(parkedDir)
        try DurableFile.makeDirectory(quarantineDir)
        reload()
    }

    /// Reads the folders again, the way a fresh process sees them.
    public func reload() {
        lock.lock(); defer { lock.unlock() }
        DurableFile.removeStaleTemps(in: root)
        DurableFile.removeStaleTemps(in: parkedDir)
        queued = scan(root)
        parked = scan(parkedDir)
        quarantined = (try? FileManager.default.contentsOfDirectory(atPath: quarantineDir.path))?
            .filter { !$0.hasPrefix(".") }.count ?? 0
        if let data = try? Data(contentsOf: retryURL),
           let stored = try? Self.decoder.decode([String: RetryState].self, from: data) {
            retry = stored.filter { queued[$0.key] != nil || parked[$0.key] != nil }
        } else {
            retry = [:]
        }
    }

    private func scan(_ dir: URL) -> [String: OutboxEntry] {
        var out: [String: OutboxEntry] = [:]
        guard let names = try? FileManager.default.contentsOfDirectory(atPath: dir.path) else { return out }
        for name in names where name.hasSuffix(Self.suffix) && !name.hasPrefix(".") {
            let url = dir.appendingPathComponent(name)
            if let (header, _, size) = try? Self.readHeader(url) {
                out[name] = OutboxEntry(name: name, header: header, size: size)
            } else {
                quarantineFile(url)
            }
        }
        return out
    }

    private func quarantineFile(_ url: URL) {
        let stamp = Int(Date().timeIntervalSince1970 * 1000)
        let dest = quarantineDir.appendingPathComponent("\(url.lastPathComponent).\(stamp)")
        try? DurableFile.move(url, to: dest, point: "outbox.quarantine")
        quarantined += 1
    }

    static func readHeader(_ url: URL) throws -> (ItemHeader, Int, Int) {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        let size = Int((try? handle.seekToEnd()) ?? 0)
        try handle.seek(toOffset: 0)
        let prefix = try handle.read(upToCount: Swift.min(size, headerLimit)) ?? Data()
        guard prefix.starts(with: magic) else { throw DurableError(step: "magic", path: url.path, code: EINVAL) }
        let rest = prefix.dropFirst(magic.count)
        guard let newline = rest.firstIndex(of: 0x0A) else {
            throw DurableError(step: "header", path: url.path, code: EINVAL)
        }
        let header = try decoder.decode(ItemHeader.self, from: Data(rest[rest.startIndex..<newline]))
        let bodyOffset = newline + 1   // index into prefix (Data slices keep the parent's indices)
        return (header, bodyOffset, size)
    }

    // MARK: Writing

    /// Writes a page durably. Throws when the disk did not take it: the caller
    /// must then treat the page as not queued (an anchored page is still
    /// re-readable from its anchor, which has not moved).
    @discardableResult
    public func enqueue(_ header: ItemHeader, body: Data) throws -> EnqueueResult {
        lock.lock(); defer { lock.unlock() }
        if header.origin == .anchored, let stream = header.stream, let generation = header.generation {
            let same = (Array(queued.values) + Array(parked.values))
                .filter { $0.header.origin == .anchored && $0.header.stream == stream && $0.header.generation == generation }
                .sorted { $0.header.count > $1.header.count }
            if let best = same.first {
                if best.header.count >= header.count { return .duplicate(best.name) }
                let name = try writeItem(header, body)
                for old in same where queued[old.name] != nil {
                    // The new page starts at the same anchor and holds more, so the
                    // old one is a strict subset of it. If the delete fails both stay:
                    // a duplicate delivery is harmless.
                    try? removeLocked(old.name)
                }
                return .replaced(name, old: best.name)
            }
        }
        if header.origin == .legacy,
           let existing = (Array(queued.values) + Array(parked.values))
            .first(where: { $0.header.origin == .legacy && $0.header.batchID == header.batchID }) {
            return .duplicate(existing.name)   // a migration re-run after a crash
        }
        return .written(try writeItem(header, body))
    }

    private func writeItem(_ header: ItemHeader, _ body: Data) throws -> String {
        let ms = Int64(header.createdAt.timeIntervalSince1970 * 1000)
        seq = (seq + 1) % 10_000
        let safeID = String(header.batchID.unicodeScalars.filter {
            CharacterSet.alphanumerics.contains($0) || $0 == "-"
        }.map(Character.init).prefix(64))
        let name = String(format: "%013lld-%04d-%@%@", ms, seq, safeID, Self.suffix)
        var data = Self.magic
        data.append(try Self.encoder.encode(header))
        data.append(0x0A)
        data.append(body)
        try DurableFile.write(data, to: root.appendingPathComponent(name), point: "outbox.enqueue")
        queued[name] = OutboxEntry(name: name, header: header, size: data.count)
        return name
    }

    // MARK: Reading

    public func body(_ name: String) throws -> Data {
        lock.lock()
        let inQueue = queued[name] != nil
        lock.unlock()
        let url = (inQueue ? root : parkedDir).appendingPathComponent(name)
        let data = try Data(contentsOf: url)
        guard data.starts(with: Self.magic),
              let newline = data.dropFirst(Self.magic.count).firstIndex(of: 0x0A) else {
            throw DurableError(step: "body", path: url.path, code: EINVAL)
        }
        return data.subdata(in: (newline + 1)..<data.endIndex)
    }

    /// Queued pages in creation order (parked pages excluded).
    public var entries: [OutboxEntry] {
        lock.lock(); defer { lock.unlock() }
        return queued.values.sorted { $0.name < $1.name }
    }

    public var parkedEntries: [OutboxEntry] {
        lock.lock(); defer { lock.unlock() }
        return parked.values.sorted { $0.name < $1.name }
    }

    /// Queued pages whose backoff has run out, in creation order.
    public func due(now: Date) -> [OutboxEntry] {
        lock.lock(); defer { lock.unlock() }
        return queued.values
            .filter { (retry[$0.name]?.nextAttemptAt ?? .distantPast) <= now }
            .sorted { $0.name < $1.name }
    }

    public func retryState(_ name: String) -> RetryState? {
        lock.lock(); defer { lock.unlock() }
        return retry[name]
    }

    public var counts: OutboxCounts {
        lock.lock(); defer { lock.unlock() }
        return OutboxCounts(queued: queued.count, parked: parked.count, quarantined: quarantined,
                            bytes: queued.values.reduce(0) { $0 + $1.size },
                            legacy: queued.values.filter { $0.header.origin == .legacy }.count)
    }

    // MARK: Outcomes

    /// The receiver acked the page: delete it. Throws when the delete failed;
    /// the page then stays and is sent again later (harmless: the Mac dedupes).
    public func acked(_ name: String) throws {
        lock.lock(); defer { lock.unlock() }
        try removeLocked(name)
    }

    private func removeLocked(_ name: String) throws {
        let inQueue = queued[name] != nil
        try DurableFile.remove((inQueue ? root : parkedDir).appendingPathComponent(name), point: "outbox.remove")
        queued[name] = nil
        parked[name] = nil
        retry[name] = nil
        saveRetryLocked()
    }

    /// A delivery attempt failed on the link (timeout, unreachable, 5xx).
    /// Backoff 30 s doubling to an hour; the page stays queued.
    public func failed(_ name: String, reason: String, status: Int?, now: Date) {
        lock.lock(); defer { lock.unlock() }
        guard queued[name] != nil else { return }
        var state = retry[name] ?? RetryState()
        state.attempts += 1
        state.lastAttemptAt = now
        state.lastError = reason
        state.lastStatus = status
        state.nextAttemptAt = now.addingTimeInterval(Self.backoff(attempts: state.attempts))
        retry[name] = state
        saveRetryLocked()
    }

    public static func backoff(attempts: Int) -> TimeInterval {
        guard attempts > 0 else { return 0 }
        return Swift.min(30 * pow(2, Double(Swift.min(attempts, 20) - 1)), 3600)
    }

    /// The receiver refused the page (HTTP 400, 413, 422): keep it, out of the
    /// queue, visible on the status screen, until the owner retries it.
    public func park(_ name: String, reason: String) throws {
        lock.lock(); defer { lock.unlock() }
        guard let entry = queued[name] else { return }
        try DurableFile.move(root.appendingPathComponent(name), to: parkedDir.appendingPathComponent(name),
                             point: "outbox.park")
        queued[name] = nil
        parked[name] = entry
        var state = retry[name] ?? RetryState()
        state.parkedReason = reason
        retry[name] = state
        saveRetryLocked()
    }

    /// Puts every parked page back in the queue with a fresh backoff.
    @discardableResult
    public func unparkAll() throws -> Int {
        lock.lock(); defer { lock.unlock() }
        var n = 0
        for (name, entry) in parked {
            try DurableFile.move(parkedDir.appendingPathComponent(name), to: root.appendingPathComponent(name),
                                 point: "outbox.unpark")
            parked[name] = nil
            queued[name] = entry
            retry[name] = nil
            n += 1
        }
        saveRetryLocked()
        return n
    }

    /// Deletes anchored pages the predicate calls superseded (queued or parked).
    /// Only ever call it with "the stream's anchor generation moved past the
    /// page's": then the page's data was delivered or is still ahead of the anchor.
    @discardableResult
    public func prune(where superseded: (ItemHeader) -> Bool) -> Int {
        lock.lock(); defer { lock.unlock() }
        var n = 0
        for entry in Array(queued.values) + Array(parked.values)
        where entry.header.origin == .anchored && superseded(entry.header) {
            if (try? removeLocked(entry.name)) != nil { n += 1 }
        }
        return n
    }

    private func saveRetryLocked() {
        do {
            let data = try Self.encoder.encode(retry)
            try DurableFile.write(data, to: retryURL, point: "outbox.retry")
            lastBookkeepingError = nil
        } catch let crash as SimulatedCrash {
            // Tests model a crash here: the in-memory state dies with the process.
            lastBookkeepingError = crash.description
        } catch {
            lastBookkeepingError = "\(error)"
        }
    }

    // MARK: v1 migration

    public struct Migration: Equatable {
        public var migrated: Int
        public var alreadyPresent: Int
        public var keptAs: URL?
        public var quarantined: Bool
    }

    /// Moves the v1 outbox.json into one file per page. Idempotent: a re-run
    /// after a crash skips pages already written. The v1 file is renamed, never
    /// overwritten or deleted here; an unreadable v1 file goes to quarantine.
    @discardableResult
    public func migrateLegacy(at url: URL, now: Date) throws -> Migration? {
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        let data = try Data(contentsOf: url)
        let dec = JSONDecoder()
        var batches: [BatchV1]
        if let items = try? dec.decode([OutboxItemV1].self, from: data) {
            batches = items.map(\.batch)
        } else if let plain = try? dec.decode([BatchV1].self, from: data) {
            batches = plain
        } else if data.isEmpty {
            batches = []
        } else {
            let dest = quarantineDir.appendingPathComponent("outbox-v1-undecodable-\(Int(now.timeIntervalSince1970)).json")
            try DurableFile.move(url, to: dest, point: "outbox.migrate.quarantine")
            lock.lock(); quarantined += 1; lock.unlock()
            return Migration(migrated: 0, alreadyPresent: 0, keptAs: nil, quarantined: true)
        }
        batches = batches.filter { !$0.samples.isEmpty || !$0.deleted.isEmpty }
        var migrated = 0, present = 0
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        for (i, batch) in batches.enumerated() {
            let starts = batch.samples.compactMap { WireTime.date($0.start) }
            let ends = batch.samples.compactMap { WireTime.date($0.end) }
            let header = ItemHeader(batchID: batch.batch_id, origin: .legacy,
                                    types: Array(Set(batch.samples.map(\.hk_type))).sorted(),
                                    count: batch.samples.count + batch.deleted.count,
                                    createdAt: now.addingTimeInterval(Double(i) / 1000),
                                    windowStart: starts.min(), windowEnd: ends.max())
            switch try enqueue(header, body: try enc.encode(batch)) {
            case .written: migrated += 1
            case .duplicate, .replaced: present += 1
            }
            try Faults.hit("outbox.migrate.item")
        }
        var kept = url.deletingLastPathComponent().appendingPathComponent("outbox.v1-migrated.json")
        if FileManager.default.fileExists(atPath: kept.path) {
            kept = url.deletingLastPathComponent()
                .appendingPathComponent("outbox.v1-migrated-\(Int(now.timeIntervalSince1970)).json")
        }
        try DurableFile.move(url, to: kept, point: "outbox.migrate.keep")
        return Migration(migrated: migrated, alreadyPresent: present, keptAs: kept, quarantined: false)
    }

    /// The renamed v1 file is a second copy of the migrated pages; it can go once
    /// no migrated page is waiting any more (delivered, or parked with its own file).
    @discardableResult
    public func removeMigratedCopies(in dir: URL) -> Int {
        guard counts.legacy == 0,
              let names = try? FileManager.default.contentsOfDirectory(atPath: dir.path) else { return 0 }
        var n = 0
        for name in names where name.hasPrefix("outbox.v1-migrated") && name.hasSuffix(".json") {
            if (try? DurableFile.remove(dir.appendingPathComponent(name), point: "outbox.migrate.cleanup")) != nil {
                n += 1
            }
        }
        return n
    }
}
