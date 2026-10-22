import Foundation
import XCTest
@testable import BridgeCore
@testable import OutboxCrashHarness

/// Replay tests: many random histories of new records, deletions, a flaky link
/// (unreachable, lost acks, refusals), locked phones, Host changes, a type turned
/// off and on again, records whose parts cannot be read for a while, and crashes
/// at random durable steps, each followed by a healthy link. Property: every
/// record HealthKit still holds reached the Mac at least once, nothing reached it
/// that HealthKit never held, no deleted record is alive on the Mac, the outbox
/// ends empty and nothing is left held. Duplicates are allowed (the Mac dedupes)
/// and counted.
final class ReplayModelTests: TempDirTestCase {
    func testRandomHistoriesWithCrashesAndReplaysLoseNothing() async throws {
        var totalDuplicates = 0
        var totalCrashes = 0
        var hiddenDelivered = 0
        var heldSeen = 0
        var writtenWhileOff = 0
        for seed in 1...40 {
            var rng = SplitMix(seed: UInt64(seed) * 7919)
            var linkRng = SplitMix(seed: UInt64(seed) * 104_729)
            let run = dir.appendingPathComponent("seed-\(seed)", isDirectory: true)
            try FileManager.default.createDirectory(at: run, withIntermediateDirectories: true)
            let health = FakeHealth()
            let server = FakeServer()
            let streams = Workload.streams
            var nextID = 0
            let base = Date(timeIntervalSince1970: 1_790_000_000)
            var now = base
            // The stream the model turns off and on. Readings only a dated query
            // finds never go to it: one written while its type is off for longer
            // than the sweep window is the documented limit (a date-range re-read
            // recovers it, C8).
            let toggled = streams[2]

            func addSome() {
                for _ in 0..<(1 + rng.int(8)) {
                    let def = streams[rng.int(streams.count)]
                    let t = now.addingTimeInterval(-Double(rng.int(3 * 86400)))
                    let uuid = "r\(seed)-\(nextID)"
                    health.add(stream: def.key, uuid: uuid, start: t, end: t.addingTimeInterval(60))
                    // Now and then a record whose parts cannot be read for a while.
                    if rng.int(8) == 0 { health.flakyParts[uuid] = 1 + rng.int(4) }
                    if health.unauthorized.contains(def.key) { writtenWhileOff += 1 }
                    nextID += 1
                }
                // Now and then a reading only a dated query finds, recorded within the hour.
                if rng.int(3) == 0 {
                    let def = streams[rng.int(2)]
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
                    switch linkRng.int(10) {
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
                    switch rng.int(9) {
                    case 0: await engine.flush()
                    case 1: await engine.sweep(streams[rng.int(streams.count)], reason: "foreground")
                    case 2: await engine.linkChanged()
                    case 3: await engine.sweep(streams[rng.int(streams.count)], reason: "manual", force: true)
                    case 4: await engine.runRetries(reason: "foreground")
                    case 5:
                        // The owner turns the type off in Health settings, or on again.
                        if health.unauthorized.contains(toggled.key) {
                            health.unauthorized.remove(toggled.key)
                        } else if !health.alive(stream: toggled.key).isEmpty {
                            health.unauthorized.insert(toggled.key)
                        }
                    default: await engine.drain(streams[rng.int(streams.count)], reason: "observer")
                    }
                    if !engine.cursors.retryCounts.isEmpty { heldSeen += 1 }
                }
                if crash.crashed { totalCrashes += 1 }
                Faults.hook = nil
                health.locked = false
            }

            // Healthy from here, every type on and readable: everything must arrive.
            server.fallback = { _ in .ok }
            health.unauthorized = []
            health.flakyParts = [:]
            let engine = try makeEngineAt(run, health: health, server: server, crash: nil, clock: { now })
            _ = try engine.outbox.unparkAll()
            for _ in 0..<4 {
                // Sync Now: past every wait a refusal started (the drains' blocks,
                // the outbox backoff, the read-again run's hour).
                for def in streams { await engine.drain(def, reason: "final", manual: true) }
                await engine.runRetries(reason: "final", force: true)
                await engine.flush(force: true)
            }
            for def in streams { await engine.sweep(def, reason: "final", force: true) }
            assertComplete(health, server, engine.outbox)
            XCTAssertEqual(engine.cursors.retryCounts, [:], "seed \(seed): records still held")
            hiddenDelivered += server.alive.filter { $0.hasPrefix("h") }.count
            totalDuplicates += server.received.values.reduce(0) { $0 + max(0, $1 - 1) }
        }
        XCTAssertGreaterThan(totalCrashes, 5, "the model must actually crash")
        XCTAssertGreaterThan(hiddenDelivered, 20, "the model must exercise the dated path")
        XCTAssertGreaterThan(heldSeen, 20, "the model must hold records to read again")
        XCTAssertGreaterThan(writtenWhileOff, 20, "the model must write while a type is off")
        print("replay model: 40 histories, \(totalCrashes) crashes, \(heldSeen) steps with held records, "
              + "\(writtenWhileOff) records written while their type was off, "
              + "\(totalDuplicates) duplicate deliveries (deduped by the Mac)")
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
