import Foundation
import XCTest
@testable import BridgeCore
@testable import OutboxCrashHarness

/// A fresh temp folder per test, removed afterwards; the fault hook is always
/// cleared so one test's crash never leaks into the next.
class TempDirTestCase: XCTestCase {
    var dir: URL!

    override func setUpWithError() throws {
        Faults.hook = nil
        dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("bridgecore-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        Faults.hook = nil
        if let dir {
            _ = try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: dir.path)
            try? FileManager.default.removeItem(at: dir)
        }
    }
}

/// Models a crash at the n-th durable step: from that step on every durable
/// operation throws (the disk stays as the crash left it), and the server
/// stops receiving (a crashed phone sends nothing more).
final class CrashSwitch {
    private let lock = NSLock()
    private var hits = 0
    private(set) var crashed = false
    private(set) var crashPoint: String?
    let at: Int

    init(at: Int) { self.at = at }

    func install() {
        Faults.hook = { [self] point in
            lock.lock()
            hits += 1
            if !crashed && at > 0 && hits >= at {
                crashed = true
                crashPoint = point
            }
            let dead = crashed
            lock.unlock()
            if dead { throw SimulatedCrash(point: point) }
        }
    }

    var hitCount: Int {
        lock.lock(); defer { lock.unlock() }
        return hits
    }
}

/// Wraps a server: after the crash switch trips, nothing reaches it.
final class CrashAwareUploader: BatchUploader {
    let inner: FakeServer
    let crash: CrashSwitch?

    init(_ inner: FakeServer, crash: CrashSwitch?) {
        self.inner = inner
        self.crash = crash
    }

    func upload(_ body: Data) async -> UploadOutcome {
        if crash?.crashed == true { return .link("phone crashed") }
        return await inner.upload(body)
    }
}

/// An uploader that holds every upload until the test opens the gate, and
/// counts how many uploads are in flight at once.
final class GatedUploader: BatchUploader {
    private let lock = NSLock()
    private var waiters: [CheckedContinuation<Void, Never>] = []
    private var open = false
    private(set) var inFlight = 0
    private(set) var maxInFlight = 0
    let inner: FakeServer

    init(_ inner: FakeServer) { self.inner = inner }

    func release() {
        lock.lock()
        open = true
        let w = waiters
        waiters = []
        lock.unlock()
        w.forEach { $0.resume() }
    }

    var waiting: Int {
        lock.lock(); defer { lock.unlock() }
        return waiters.count
    }

    func upload(_ body: Data) async -> UploadOutcome {
        let isOpen: Bool = lock.withLock {
            inFlight += 1
            maxInFlight = max(maxInFlight, inFlight)
            return open
        }
        if !isOpen {
            await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
                let resumeNow: Bool = lock.withLock {
                    if open { return true }
                    waiters.append(c)
                    return false
                }
                if resumeNow { c.resume() }
            }
        }
        let r = await inner.upload(body)
        lock.withLock { inFlight -= 1 }
        return r
    }
}

extension XCTestCase {
    /// Polls an async condition (actor state) until it holds or times out.
    func waitUntil(timeout: TimeInterval = 5, _ condition: @escaping () async -> Bool) async {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if await condition() { return }
            try? await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("condition not met in \(timeout) s")
    }
}

func makeStores(_ dir: URL) throws -> (OutboxStore, AnchorStore, CursorStore) {
    (try OutboxStore(root: dir.appendingPathComponent("Outbox", isDirectory: true)),
     try AnchorStore(dir: dir.appendingPathComponent("Anchors", isDirectory: true)),
     try CursorStore(url: dir.appendingPathComponent("cursors.json")))
}

func makeEngine(_ dir: URL, health: FakeHealth, uploader: BatchUploader, config: EngineConfig = EngineConfig(),
                pageLimit: Int = 5, clock: @escaping () -> Date = Date.init) throws -> SyncEngine {
    let (outbox, anchors, cursors) = try makeStores(dir)
    for def in Workload.streams { try cursors.setPageLimit(def.key, pageLimit) }
    var cfg = config
    cfg.windowPageLimit = pageLimit
    return SyncEngine(outbox: outbox, anchors: anchors, cursors: cursors, source: health, uploader: uploader,
                      build: BuildInfo(appVersion: "test", build: "0", osVersion: "test", zone: "UTC"),
                      config: cfg, clock: clock)
}

func legacyBatch(_ id: String, uuids: [String]) -> BatchV1 {
    BatchV1(batch_id: id, sent_at: "2026-10-01T00:00:00.000Z",
            samples: uuids.map {
                SampleDTOV1(uuid: $0, hk_type: "HKQuantityTypeIdentifierHeartRate", value: .number(60),
                            unit: "count/min", start: "2026-10-01T00:00:00.000Z", end: "2026-10-01T00:01:00.000Z",
                            source_name: "Synthetic Watch")
            }, deleted: [])
}

func writeLegacyOutbox(_ dir: URL, batches: [BatchV1]) throws {
    let items = batches.map { OutboxItemV1(batch: $0, attempts: 3) }
    try JSONEncoder().encode(items).write(to: dir.appendingPathComponent("outbox.json"))
    let uuids = batches.flatMap { $0.samples.map(\.uuid) }
    try uuids.joined(separator: "\n").write(to: dir.appendingPathComponent("legacy-uuids.txt"), atomically: true,
                                            encoding: .utf8)
}

/// What a finished run must show: every record HealthKit holds (and every v1
/// page) reached the Mac, nothing reached it that never existed, no deleted
/// record is alive there, and the outbox is empty.
func assertComplete(_ health: FakeHealth, _ server: FakeServer, _ outbox: OutboxStore, legacy: Set<String> = [],
                    file: StaticString = #filePath, line: UInt = #line) {
    let expected = Set(health.alive().map(\.uuid)).union(legacy)
    XCTAssertEqual(expected.subtracting(server.alive), [], "missing on the Mac", file: file, line: line)
    XCTAssertEqual(Set(server.received.keys).subtracting(health.everAdded).subtracting(legacy), [],
                   "phantom records", file: file, line: line)
    XCTAssertEqual(server.alive.intersection(health.everDeleted), [], "deleted record alive", file: file, line: line)
    outbox.reload()
    XCTAssertEqual(outbox.counts.queued, 0, "outbox not empty", file: file, line: line)
    XCTAssertEqual(outbox.counts.parked, 0, "parked pages", file: file, line: line)
    XCTAssertEqual(outbox.counts.quarantined, 0, "quarantined files", file: file, line: line)
}

/// XCTAssertEqual takes autoclosures, which cannot await; this takes values.
func expect<T: Equatable>(_ value: T, _ expected: T, _ message: String = "",
                          file: StaticString = #filePath, line: UInt = #line) {
    XCTAssertEqual(value, expected, message, file: file, line: line)
}
