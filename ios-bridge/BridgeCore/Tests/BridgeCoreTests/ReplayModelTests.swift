import Foundation
import XCTest
@testable import BridgeCore
@testable import OutboxCrashHarness

/// Replay tests: many random histories of new records, deletions, a flaky link
/// (unreachable, lost acks, refusals), locked phones, Host changes and crashes at
/// random durable steps, each followed by a healthy link. Property: every record
/// HealthKit still holds reached the Mac at least once, nothing reached it that
/// HealthKit never held, no deleted record is alive on the Mac, and the outbox
/// ends empty. Duplicates are allowed (the Mac dedupes) and counted.
final class ReplayModelTests: TempDirTestCase {
    func testRandomHistoriesWithCrashesAndReplaysLoseNothing() async throws {
        var totalDuplicates = 0
        var totalCrashes = 0
        var hiddenDelivered = 0
        for seed in 1...40 {
            var rng = SplitMix(seed: UInt64(seed) * 7919)
            let run = dir.appendingPathComponent("seed-\(seed)", isDirectory: true)
            try FileManager.default.createDirectory(at: run, withIntermediateDirectories: true)
            let health = FakeHealth()
            let server = FakeServer()
            let streams = Workload.streams
            var nextID = 0
            let base = Date(timeIntervalSince1970: 1_790_000_000)
            var now = base

            func addSome() {
                for _ in 0..<(1 + rng.int(8)) {
                    let def = streams[rng.int(streams.count)]
                    let t = now.addingTimeInterval(-Double(rng.int(3 * 86400)))
                    health.add(stream: def.key, uuid: "r\(seed)-\(nextID)", start: t, end: t.addingTimeInterval(60))
                    nextID += 1
                }
                // Now and then a reading only a dated query finds, recorded within the hour.
                if rng.int(3) == 0 {
                    let def = streams[rng.int(streams.count)]
                    let t = now.addingTimeInterval(-Double(rng.int(3600)))
                    health.add(stream: def.key, uuid: "h\(seed)-\(nextID)", start: t, end: t.addingTimeInterval(60),
                               anchorHidden: true)
                    nextID += 1
                }
            }
            func deleteSome() {
                let alive = health.alive()
                guard !alive.isEmpty, rng.int(3) == 0 else { return }
                let victim = alive[rng.int(alive.count)]
                health.delete(stream: victim.stream, uuid: victim.uuid)
            }

            // A running app has swept every stream before: start from a completed sweep.
            do {
                let engine = try makeEngineAt(run, health: health, server: server, crash: nil, clock: { now })
                for def in streams { await engine.sweep(def, reason: "launch", force: true) }
            }

            for round in 0..<6 {
                let crash = CrashSwitch(at: rng.int(4) == 0 ? 1 + rng.int(60) : 0)
                crash.install()
                server.fallback = { _ in
                    switch Int.random(in: 0..<10) {
                    case 0, 1: return .link
                    case 2: return .lostAck
                    case 3 where round % 3 == 2: return .refuse(422)
                    default: return .ok
                    }
                }
                guard let engine = try? makeEngineAt(run, health: health, server: server, crash: crash,
                                                     clock: { now }) else { continue }
                for _ in 0..<(3 + rng.int(6)) {
                    if crash.crashed { break }
                    addSome()
                    deleteSome()
                    now = now.addingTimeInterval(Double(3600 + rng.int(4 * 3600)))   // histories of about four days: longer than the 48-hour sweep window
                    health.locked = rng.int(6) == 0
                    switch rng.int(6) {
                    case 0: await engine.flush()
                    case 1: await engine.sweep(streams[rng.int(streams.count)], reason: "foreground")
                    case 2: await engine.linkChanged()
                    case 3: await engine.sweep(streams[rng.int(streams.count)], reason: "manual", force: true)
                    default: await engine.drain(streams[rng.int(streams.count)], reason: "observer")
                    }
                }
                if crash.crashed { totalCrashes += 1 }
                Faults.hook = nil
                health.locked = false
            }

            // Healthy from here: everything must arrive.
            server.fallback = { _ in .ok }
            let engine = try makeEngineAt(run, health: health, server: server, crash: nil, clock: { now })
            _ = try engine.outbox.unparkAll()
            for _ in 0..<4 {
                for def in streams { await engine.drain(def, reason: "final", manual: true) }
                await engine.flush(force: true)
            }
            for def in streams { await engine.sweep(def, reason: "final", force: true) }
            assertComplete(health, server, engine.outbox)
            hiddenDelivered += server.alive.filter { $0.hasPrefix("h") }.count
            totalDuplicates += server.received.values.reduce(0) { $0 + max(0, $1 - 1) }
        }
        XCTAssertGreaterThan(totalCrashes, 5, "the model must actually crash")
        XCTAssertGreaterThan(hiddenDelivered, 20, "the model must exercise the dated path")
        print("replay model: 40 histories, \(totalCrashes) crashes, \(totalDuplicates) duplicate deliveries (deduped by the Mac)")
    }

    func makeEngineAt(_ run: URL, health: FakeHealth, server: FakeServer, crash: CrashSwitch?,
                      clock: @escaping () -> Date) throws -> SyncEngine {
        let (outbox, anchors, cursors) = try makeStores(run)
        for def in Workload.streams { try? cursors.setPageLimit(def.key, 4) }
        var cfg = EngineConfig()
        cfg.windowPageLimit = 4
        return SyncEngine(outbox: outbox, anchors: anchors, cursors: cursors, source: health,
                          uploader: CrashAwareUploader(server, crash: crash),
                          build: BuildInfo(appVersion: "test", build: "0", osVersion: "test", zone: "UTC"),
                          config: cfg, clock: clock)
    }
}
