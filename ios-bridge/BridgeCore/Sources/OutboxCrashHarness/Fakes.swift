import Foundation
import BridgeCore

// Test doubles shared by the crash harness and the test target. No real health
// data: synthetic identifiers and times only.

/// HealthKit as the engine sees it: records and deletions in one change
/// sequence per store, the way HealthKit's anchors order them.
public final class FakeHealth: RecordSource {
    public struct Change: Equatable {
        public let seq: Int
        public let stream: String
        public let uuid: String
        public let start: Date
        public let end: Date
        public let deletion: Bool
        /// Like the glucose readings a writer saves with a sync identifier: real
        /// HealthKit leaves some out of the anchored enumeration, and only a dated
        /// query finds them. The trailing sweep exists for these.
        public var anchorHidden = false
    }

    private let lock = NSLock()
    private var changes: [Change] = []
    private var nextSeq = 1
    public var locked = false
    /// Streams the owner has not granted: HealthKit answers them with an empty
    /// page and an anchor at its latest position, not with an error.
    public var unauthorized: Set<String> = []
    /// Activity summary days (one summary per day from `summaryFirstDay`).
    public var summaryFirstDay: Date?
    public var summaryDays = 0
    public var summariesUnauthorized = false
    public private(set) var anchoredReads = 0

    public init() {}

    public func add(stream: String, uuid: String, start: Date, end: Date, anchorHidden: Bool = false) {
        lock.lock(); defer { lock.unlock() }
        changes.append(Change(seq: nextSeq, stream: stream, uuid: uuid, start: start, end: end, deletion: false,
                              anchorHidden: anchorHidden))
        nextSeq += 1
    }

    public func delete(stream: String, uuid: String) {
        lock.lock(); defer { lock.unlock() }
        guard let rec = changes.first(where: { $0.uuid == uuid && !$0.deletion }) else { return }
        changes.append(Change(seq: nextSeq, stream: stream, uuid: uuid, start: rec.start, end: rec.end, deletion: true))
        nextSeq += 1
    }

    /// Records added and not deleted, per stream.
    public func alive(stream: String? = nil) -> [Change] {
        lock.lock(); defer { lock.unlock() }
        let deleted = Set(changes.filter(\.deletion).map(\.uuid))
        return changes.filter { !$0.deletion && !deleted.contains($0.uuid) && (stream == nil || $0.stream == stream) }
    }

    public var everAdded: Set<String> {
        lock.lock(); defer { lock.unlock() }
        return Set(changes.filter { !$0.deletion }.map(\.uuid))
    }

    public var everDeleted: Set<String> {
        lock.lock(); defer { lock.unlock() }
        return Set(changes.filter(\.deletion).map(\.uuid))
    }

    public static func anchor(_ seq: Int) -> Data {
        var v = UInt64(seq).bigEndian
        return withUnsafeBytes(of: &v) { Data($0) }
    }

    public static func seq(_ anchor: Data) -> Int {
        anchor.reduce(0) { ($0 << 8) | Int($1) }
    }

    static func sample(_ c: Change, _ def: StreamDef) -> SampleV2 {
        SampleV2(uuid: c.uuid, hkType: def.identifier, kind: def.cls == .category ? "category" : "quantity",
                 value: .number(1), unit: def.wireUnit, start: WireTime.string(c.start), end: WireTime.string(c.end),
                 sourceName: "Synthetic Watch")
    }

    public func anchoredPage(_ def: StreamDef, anchor: Data?, limit: Int) async throws -> AnchoredPage {
        try lock.withLock { try pageLocked(def, anchor: anchor, limit: limit) }
    }

    private func pageLocked(_ def: StreamDef, anchor: Data?, limit: Int) throws -> AnchoredPage {
        if locked { throw SourceError.locked }
        anchoredReads += 1
        if unauthorized.contains(def.key) {
            return AnchoredPage(content: BatchContent(), newAnchor: Self.anchor(nextSeq - 1))
        }
        let after = anchor.map(Self.seq) ?? 0
        let mine = changes.filter { $0.stream == def.key && !$0.anchorHidden }
        let page = Array(mine.filter { $0.seq > after }.sorted { $0.seq < $1.seq }.prefix(limit))
        var content = BatchContent()
        for c in page {
            if c.deletion {
                content.deleted.append(c.uuid)
            } else {
                content.samples.append(Self.sample(c, def))
                content.widen(start: c.start, end: c.end)
            }
        }
        let last = page.last?.seq ?? max(after, mine.map(\.seq).max() ?? after)
        return AnchoredPage(content: content, newAnchor: Self.anchor(last))
    }

    public func windowPages(_ kind: WindowKind, start: Date, end: Date, limit: Int) async throws -> [BatchContent] {
        if case .activitySummaries = kind {
            guard !summariesUnauthorized, let first = summaryFirstDay else { return [] }
            var page = BatchContent()
            for i in 0..<summaryDays {
                let day = first.addingTimeInterval(Double(i) * 86400)
                if day >= start && day <= end {   // day ranges are inclusive, like HealthKit's date components
                    page.activitySummaries.append(ActivitySummaryV2(date: WireTime.string(day)))
                }
            }
            return page.isEmpty ? [] : [page]
        }
        guard case .samples(let def) = kind else { return [] }
        let recs: [Change] = try lock.withLock {
            if locked { throw SourceError.locked }
            let deleted = Set(changes.filter(\.deletion).map(\.uuid))
            return changes.filter { $0.stream == def.key && !$0.deletion && !deleted.contains($0.uuid)
                && $0.start >= start && $0.start < end }.sorted { $0.start < $1.start }
        }
        var pages: [BatchContent] = []
        var i = 0
        while i < recs.count {
            var content = BatchContent()
            for c in recs[i..<min(i + limit, recs.count)] {
                content.samples.append(Self.sample(c, def))
                content.widen(start: c.start, end: c.end)
            }
            pages.append(content)
            i += limit
        }
        return pages
    }
}

/// The Mac as the Bridge sees it: stores what it acks, keeps tombstones, never
/// resurrects a deleted uuid. Optionally logs every stored change to a file
/// (fsynced before the ack), so its state survives the harness being killed.
public final class FakeServer: BatchUploader {
    public enum Mode: Equatable {
        case ok
        case link
        /// The batch is stored, the ack never reaches the phone.
        case lostAck
        case refuse(Int)
    }

    private let lock = NSLock()
    public var script: [Mode] = []
    public var fallback: (Int) -> Mode = { _ in .ok }
    public private(set) var calls = 0
    public private(set) var received: [String: Int] = [:]
    public private(set) var tombstones: Set<String> = []
    public private(set) var alive: Set<String> = []
    public private(set) var batchIDs: [String] = []
    public private(set) var summaryDates: Set<String> = []
    private let logURL: URL?

    public init(logURL: URL? = nil) {
        self.logURL = logURL
        if let logURL, let text = try? String(contentsOf: logURL, encoding: .utf8) {
            for line in text.split(separator: "\n") {
                let parts = line.split(separator: " ")
                guard parts.count == 2 else { continue }
                apply(kind: String(parts[0]), uuid: String(parts[1]))
            }
        }
    }

    private func apply(kind: String, uuid: String) {
        if kind == "D" {
            tombstones.insert(uuid)
            alive.remove(uuid)
        } else {
            received[uuid, default: 0] += 1
            if !tombstones.contains(uuid) { alive.insert(uuid) }
        }
    }

    public func upload(_ body: Data) async -> UploadOutcome {
        lock.withLock { uploadLocked(body) }
    }

    private func uploadLocked(_ body: Data) -> UploadOutcome {
        let mode = script.isEmpty ? fallback(calls) : script.removeFirst()
        calls += 1
        switch mode {
        case .link:
            return .link("fake: unreachable")
        case .refuse(let status):
            return .refused(status, "fake refusal")
        case .ok, .lostAck:
            guard let obj = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else {
                return .refused(400, "not JSON")
            }
            var lines: [String] = []
            for uuid in (obj["deleted"] as? [String]) ?? [] {
                apply(kind: "D", uuid: uuid)
                lines.append("D \(uuid)")
            }
            for s in (obj["samples"] as? [[String: Any]]) ?? [] {
                guard let uuid = s["uuid"] as? String else { continue }
                apply(kind: "R", uuid: uuid)
                lines.append("R \(uuid)")
            }
            if let id = obj["batch_id"] as? String { batchIDs.append(id) }
            for a in (obj["activity_summaries"] as? [[String: Any]]) ?? [] {
                if let d = a["date"] as? String { summaryDates.insert(d) }
            }
            if let logURL, !lines.isEmpty { Self.appendDurably(lines.joined(separator: "\n") + "\n", to: logURL) }
            return mode == .lostAck ? .link("fake: ack lost") : .acked(AckReply(ack: true))
        }
    }

    static func appendDurably(_ text: String, to url: URL) {
        let fd = open(url.path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0o600)
        guard fd >= 0 else { return }
        let bytes = Array(text.utf8)
        _ = bytes.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
        _ = fsync(fd)
        close(fd)
    }
}

/// A small synthetic workload, the same for a given seed in every process.
public enum Workload {
    public static let streams: [StreamDef] = Array(TypeRegistry.registered.prefix(3))

    public static func populate(_ health: FakeHealth, seed: Int, perStream: Int = 23) {
        var rng = SplitMix(seed: UInt64(seed))
        let base = Date(timeIntervalSince1970: 1_790_000_000)
        for (s, def) in streams.enumerated() {
            for i in 0..<perStream {
                let start = base.addingTimeInterval(Double(i * 300 + Int(rng.next() % 60)))
                health.add(stream: def.key, uuid: "S\(s)-\(seed)-\(i)", start: start, end: start.addingTimeInterval(60))
            }
        }
        // A few deletions after the fact, as a writer app correcting itself.
        for (s, def) in streams.enumerated() where s != 1 {
            health.delete(stream: def.key, uuid: "S\(s)-\(seed)-\(perStream / 2)")
        }
    }

    /// The server's answers by call index: early link failures and a lost ack,
    /// then healthy. Deterministic, so a killed run and its recovery agree.
    public static func serverMode(_ call: Int) -> FakeServer.Mode {
        switch call {
        case 2, 3, 7: return .link
        case 5: return .lostAck
        default: return .ok
        }
    }

    public static func makeEngine(dir: URL, health: FakeHealth, server: FakeServer, pageLimit: Int = 5) throws -> SyncEngine {
        let outbox = try OutboxStore(root: dir.appendingPathComponent("Outbox", isDirectory: true))
        let anchors = try AnchorStore(dir: dir.appendingPathComponent("Anchors", isDirectory: true))
        let cursors = try CursorStore(url: dir.appendingPathComponent("cursors.json"))
        var config = EngineConfig()
        config.windowPageLimit = pageLimit
        for def in streams { try cursors.setPageLimit(def.key, pageLimit) }
        return SyncEngine(outbox: outbox, anchors: anchors, cursors: cursors, source: health, uploader: server,
                          build: BuildInfo(appVersion: "test", build: "0", osVersion: "test", zone: "UTC"),
                          config: config)
    }

    /// Drains and flushes until everything is delivered (bounded rounds).
    public static func run(_ engine: SyncEngine, rounds: Int = 8) async {
        for _ in 0..<rounds {
            for def in streams {
                await engine.drain(def, reason: "test", manual: true)
            }
            await engine.flush(force: true)
        }
    }
}

/// A small deterministic random generator (SplitMix64).
public struct SplitMix {
    private var state: UInt64
    public init(seed: UInt64) { state = seed &+ 0x9E3779B97F4A7C15 }
    public mutating func next() -> UInt64 {
        state = state &+ 0x9E3779B97F4A7C15
        var z = state
        z = (z ^ (z >> 30)) &* 0xBF58476D1CE4E5B9
        z = (z ^ (z >> 27)) &* 0x94D049BB133111EB
        return z ^ (z >> 31)
    }
    public mutating func int(_ n: Int) -> Int { Int(next() % UInt64(max(1, n))) }
}
