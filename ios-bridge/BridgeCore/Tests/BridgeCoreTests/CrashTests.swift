import Foundation
import XCTest
@testable import BridgeCore
@testable import OutboxCrashHarness

/// Crash tests, in-process: the workload runs until the n-th durable step, where
/// the crash switch freezes the disk and cuts the phone off; the engine is then
/// thrown away and a fresh one opens the same folders, the way the app does after
/// iOS kills it. Every step of a full run is tried. The fake Mac keeps everything
/// it acked (it did not crash).
final class CrashPointTests: TempDirTestCase {
    func countSteps(seed: Int) async throws -> Int {
        let probe = dir.appendingPathComponent("probe", isDirectory: true)
        try FileManager.default.createDirectory(at: probe, withIntermediateDirectories: true)
        let crash = CrashSwitch(at: 0)
        crash.install()
        let health = FakeHealth()
        Workload.populate(health, seed: seed)
        let server = Workload.makeServer()
        try writeLegacyOutbox(probe, batches: [legacyBatch("L1", uuids: ["L-a", "L-b"])])
        let engine = try Workload.makeEngine(dir: probe, health: health, server: server)
        try engine.outbox.migrateLegacy(at: probe.appendingPathComponent("outbox.json"), now: Date())
        await Workload.run(engine)
        Faults.hook = nil
        return crash.hitCount
    }

    func testEveryCrashPointRecoversWithNothingLost() async throws {
        let seed = 11
        let steps = try await countSteps(seed: seed)
        XCTAssertGreaterThan(steps, 50)
        var points: [String: Int] = [:]
        for n in 1...steps {
            let run = dir.appendingPathComponent("run-\(n)", isDirectory: true)
            try FileManager.default.createDirectory(at: run, withIntermediateDirectories: true)
            try writeLegacyOutbox(run, batches: [legacyBatch("L1", uuids: ["L-a", "L-b"])])
            let health = FakeHealth()
            Workload.populate(health, seed: seed)
            let server = Workload.makeServer()

            // Run 1: crashes at step n.
            let crash = CrashSwitch(at: n)
            crash.install()
            do {
                let engine = try makeEngineAt(run, health: health, server: server, crash: crash)
                _ = try? engine.outbox.migrateLegacy(at: run.appendingPathComponent("outbox.json"), now: Date())
                await Workload.run(engine)
            } catch {
                // Opening the stores itself crashed: the next launch opens them again.
            }
            XCTAssertTrue(crash.crashed, "step \(n) was not reached")
            if let p = crash.crashPoint { points[p, default: 0] += 1 }

            // Run 2: a fresh process on the same folders, healthy link.
            Faults.hook = nil
            server.script = []
            server.fallback = { _ in .ok }
            let engine = try makeEngineAt(run, health: health, server: server, crash: nil)
            try engine.outbox.migrateLegacy(at: run.appendingPathComponent("outbox.json"), now: Date())
            await Workload.run(engine, rounds: 3)
            assertComplete(health, server, engine.outbox, legacy: ["L-a", "L-b"])
            XCTAssertEqual(engine.cursors.retryCounts, [:], "a held record was never sent (crash at step \(n))")
            XCTAssertFalse(FileManager.default.fileExists(atPath: run.appendingPathComponent("outbox.json").path))
            try? FileManager.default.removeItem(at: run)
        }
        // Every kind of durable step was crashed at least once.
        for kind in ["outbox.enqueue.written", "outbox.enqueue.renamed", "outbox.remove.before", "outbox.remove.removed",
                     "anchor.save.written", "anchor.save.renamed", "outbox.migrate.item", "outbox.migrate.keep.before",
                     "outbox.park.before", "outbox.park.moved", "outbox.unpark.before", "outbox.unpark.moved",
                     "cursor.save.written", "cursor.save.renamed"] {
            XCTAssertNotNil(points[kind], "no crash at \(kind); points seen: \(points.keys.sorted())")
        }
    }

    func makeEngineAt(_ run: URL, health: FakeHealth, server: FakeServer, crash: CrashSwitch?) throws -> SyncEngine {
        let (outbox, anchors, cursors) = try makeStores(run)
        for def in Workload.streams { try? cursors.setPageLimit(def.key, 5) }
        var cfg = EngineConfig()
        cfg.windowPageLimit = 5
        return SyncEngine(outbox: outbox, anchors: anchors, cursors: cursors, source: health,
                          uploader: CrashAwareUploader(server, crash: crash),
                          build: BuildInfo(appVersion: "test", build: "0", osVersion: "test", zone: "UTC"),
                          config: cfg, clock: { Workload.now })
    }
}

/// Crash tests with a real process kill: the harness is killed with SIGKILL at
/// every durable step of a full run (and twice in a row, and at random moments
/// inside a step), then restarted on the same folders, then checked.
final class KillTests: TempDirTestCase {
    var harness: URL {
        Bundle(for: KillTests.self).bundleURL.deletingLastPathComponent().appendingPathComponent("OutboxCrashHarness")
    }

    @discardableResult
    func runHarness(_ args: [String], killAfter: TimeInterval? = nil) throws -> (status: Int32, signaled: Bool, out: String) {
        let p = Process()
        p.executableURL = harness
        p.arguments = args
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = FileHandle.nullDevice
        try p.run()
        if let killAfter {
            DispatchQueue.global().asyncAfter(deadline: .now() + killAfter) {
                if p.isRunning { kill(p.processIdentifier, SIGKILL) }
            }
        }
        p.waitUntilExit()
        let out = String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        return (p.terminationStatus, p.terminationReason == .uncaughtSignal, out)
    }

    func verify(_ run: URL, seed: Int, _ label: String) throws {
        let r = try runHarness(["verify", "--dir", run.path, "--seed", "\(seed)"])
        let json = try XCTUnwrap(try JSONSerialization.jsonObject(with: Data(r.out.utf8)) as? [String: Int], r.out)
        for key in ["missing", "phantom", "resurrected", "queued", "parked", "quarantined", "held", "v1_left"] {
            XCTAssertEqual(json[key], 0, "\(label): \(key) = \(json[key] ?? -1); \(r.out)")
        }
    }

    func freshRun(_ name: String) throws -> URL {
        let run = dir.appendingPathComponent(name, isDirectory: true)
        try FileManager.default.createDirectory(at: run, withIntermediateDirectories: true)
        try writeLegacyOutbox(run, batches: [legacyBatch("L1", uuids: ["L-a", "L-b"]), legacyBatch("L2", uuids: ["L-c"])])
        return run
    }

    func testTheHarnessFinishesCleanWithoutAKill() throws {
        XCTAssertTrue(FileManager.default.isExecutableFile(atPath: harness.path), "harness not built at \(harness.path)")
        let run = try freshRun("clean")
        let r = try runHarness(["run", "--dir", run.path, "--seed", "5"])
        XCTAssertEqual(r.status, 0, r.out)
        XCTAssertTrue(r.out.hasPrefix("hits="))
        try verify(run, seed: 5, "clean run")
    }

    func testAKillAtEveryDurableStepLosesNothing() throws {
        let seed = 5
        let probe = try freshRun("probe")
        let r = try runHarness(["run", "--dir", probe.path, "--seed", "\(seed)"])
        let total = Int(r.out.trimmingCharacters(in: .whitespacesAndNewlines).dropFirst("hits=".count)) ?? 0
        XCTAssertGreaterThan(total, 50, r.out)
        var killed = 0
        for k in 1...total {
            let run = try freshRun("k\(k)")
            let first = try runHarness(["run", "--dir", run.path, "--seed", "\(seed)", "--kill-at", "\(k)"])
            if first.signaled && first.status == SIGKILL { killed += 1 }
            let second = try runHarness(["run", "--dir", run.path, "--seed", "\(seed)"])
            XCTAssertEqual(second.status, 0, "recovery after a kill at step \(k): \(second.out)")
            try verify(run, seed: seed, "kill at step \(k)")
            try? FileManager.default.removeItem(at: run)
        }
        XCTAssertEqual(killed, total, "every step was a real SIGKILL")
    }

    func testTwoKillsInARowStillLoseNothing() throws {
        let seed = 9
        for (k1, k2) in [(3, 2), (17, 5), (40, 11), (75, 30), (120, 60)] {
            let run = try freshRun("kk\(k1)-\(k2)")
            try runHarness(["run", "--dir", run.path, "--seed", "\(seed)", "--kill-at", "\(k1)"])
            try runHarness(["run", "--dir", run.path, "--seed", "\(seed)", "--kill-at", "\(k2)"])
            let last = try runHarness(["run", "--dir", run.path, "--seed", "\(seed)"])
            XCTAssertEqual(last.status, 0, last.out)
            try verify(run, seed: seed, "kills at \(k1) then \(k2)")
        }
    }

    func testKillsAtRandomMomentsLoseNothing() throws {
        var rng = SplitMix(seed: 42)
        for i in 0..<12 {
            let run = try freshRun("t\(i)")
            // 300 microseconds per step: a timed kill lands between, or inside, steps.
            let delay = Double(5 + rng.int(60)) / 1000
            try runHarness(["run", "--dir", run.path, "--seed", "3", "--slow-us", "300"], killAfter: delay)
            let last = try runHarness(["run", "--dir", run.path, "--seed", "3"])
            XCTAssertEqual(last.status, 0, last.out)
            try verify(run, seed: 3, "timed kill \(i) after \(delay) s")
        }
    }
}
