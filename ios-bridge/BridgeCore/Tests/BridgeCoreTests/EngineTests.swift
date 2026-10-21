import Foundation
import XCTest
@testable import BridgeCore
@testable import OutboxCrashHarness

final class EngineTests: TempDirTestCase {
    let def = Workload.streams[0]
    let base = Date(timeIntervalSince1970: 1_790_000_000)

    func fill(_ health: FakeHealth, _ n: Int, stream: StreamDef? = nil, from: Int = 0) {
        let s = stream ?? def
        for i in from..<(from + n) {
            let t = base.addingTimeInterval(Double(i * 60))
            health.add(stream: s.key, uuid: "\(s.shortName)-\(i)", start: t, end: t.addingTimeInterval(30))
        }
    }

    func testADrainDeliversEverythingAndMovesTheAnchor() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 12)
        let engine = try makeEngine(dir, health: health, uploader: server)
        let outcome = await engine.drain(def, reason: "t")
        XCTAssertEqual(outcome, .complete)
        XCTAssertEqual(server.alive.count, 12)
        XCTAssertEqual(server.calls, 3, "pages of 5, 5 and 2")
        // Nothing new: an empty read, no upload.
        await engine.drain(def, reason: "t")
        XCTAssertEqual(server.calls, 3)
    }

    func testAPageThatCannotBeSentWaitsInTheOutboxAndItsAckMovesTheAnchor() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 5)
        server.script = [.link]
        let engine = try makeEngine(dir, health: health, uploader: server)
        let outcome = await engine.drain(def, reason: "t")
        XCTAssertEqual(outcome, .linkDown)
        let entries = engine.outbox.entries
        XCTAssertEqual(entries.count, 1)
        XCTAssertEqual(entries[0].header.origin, .anchored)
        XCTAssertEqual(entries[0].header.stream, def.key)
        XCTAssertEqual(engine.anchors.generation(def.key), 0, "the anchor stays until an ack")
        let report = await engine.flush(force: true)
        XCTAssertEqual(report.delivered, 1)
        XCTAssertEqual(report.anchorsAdvanced, 1, "the queued copy's ack moves the anchor")
        let calls = server.calls
        await engine.drain(def, reason: "t")
        XCTAssertEqual(server.calls, calls, "so the drain does not send the same page again")
        assertComplete(health, server, engine.outbox)
    }

    func testALostAckIsSentAgainAndNothingIsLost() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 5)
        server.script = [.lostAck]
        let engine = try makeEngine(dir, health: health, uploader: server)
        await engine.drain(def, reason: "t")
        XCTAssertEqual(engine.outbox.counts.queued, 1)
        await engine.drain(def, reason: "t")
        await engine.flush(force: true)
        assertComplete(health, server, engine.outbox)
        XCTAssertEqual(server.received.values.max(), 2, "delivered twice; the Mac dedupes")
    }

    func testARefusedPageIsParkedAndTheStreamWaitsInsteadOfSpinning() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 3)
        server.script = [.refuse(422)]
        let engine = try makeEngine(dir, health: health, uploader: server)
        expect(await engine.drain(def, reason: "t"), .refused)
        XCTAssertEqual(engine.outbox.counts.parked, 1)
        expect(await engine.drain(def, reason: "t"), .blocked, "no retry every wake")
        XCTAssertEqual(server.calls, 1)
        // The owner syncs by hand once the Mac is fixed.
        expect(await engine.drain(def, reason: "t", manual: true), .complete)
        await engine.pruneSuperseded()
        assertComplete(health, server, engine.outbox)
    }

    func testAnOversizedPageHalvesThePageLimit() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 40)
        server.script = [.refuse(413)]
        var cfg = EngineConfig()
        cfg.minPageLimit = 5
        let engine = try makeEngine(dir, health: health, uploader: server, config: cfg, pageLimit: 20)
        expect(await engine.drain(def, reason: "t"), .complete)
        XCTAssertEqual(engine.cursors.pageLimit(def.key), 10)
        XCTAssertEqual(server.alive.count, 40)
    }

    func testWakesDuringADrainJoinItInsteadOfStartingAnother() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 10)
        let gate = GatedUploader(server)
        let engine = try makeEngine(dir, health: health, uploader: gate)
        let first = Task { await engine.drain(def, reason: "observer") }
        await waitUntil { gate.waiting == 1 }
        var joins: [Task<DrainOutcome, Never>] = []
        for _ in 0..<5 { joins.append(Task { await engine.drain(def, reason: "observer") }) }
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(gate.waiting, 1, "one drain, one page in flight")
        gate.release()
        _ = await first.value
        for j in joins { _ = await j.value }
        XCTAssertEqual(gate.maxInFlight, 1)
        XCTAssertEqual(server.received.values.max(), 1, "every page sent once (v1 sent the same pages 6 times)")
        XCTAssertEqual(server.alive.count, 10)
    }

    func testAtMostTwoStreamsDrainAtOnce() async throws {
        let health = FakeHealth(), server = FakeServer()
        for s in Workload.streams { fill(health, 10, stream: s) }
        let gate = GatedUploader(server)
        let engine = try makeEngine(dir, health: health, uploader: gate)
        let tasks = Workload.streams.map { s in Task { await engine.drain(s, reason: "foreground") } }
        await waitUntil { gate.waiting == 2 }
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(gate.waiting, 2)
        gate.release()
        for t in tasks { _ = await t.value }
        XCTAssertLessThanOrEqual(gate.maxInFlight, 2)
        XCTAssertEqual(server.alive.count, 30)
    }

    func testAHostChangeStopsARunningDrainAndResetsTheLink() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 20)
        let gate = GatedUploader(server)
        let engine = try makeEngine(dir, health: health, uploader: gate)
        let task = Task { await engine.drain(def, reason: "manual") }
        await waitUntil { gate.waiting == 1 }
        await engine.linkChanged()
        gate.release()
        expect(await task.value, .cancelled, "the page in flight finishes, no further page goes to the old host")
        XCTAssertEqual(server.calls, 1)
        let verified = await engine.snapshot().linkVerified
        XCTAssertFalse(verified, "the new host is untested")
        await engine.drain(def, reason: "manual")
        let after = await engine.snapshot().linkVerified
        XCTAssertTrue(after)
    }

    func testALockedPhoneWaitsAndClearsOnUnlock() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 3)
        health.locked = true
        let engine = try makeEngine(dir, health: health, uploader: server)
        expect(await engine.drain(def, reason: "observer"), .locked)
        var status = await engine.snapshot().streams[def.key]
        XCTAssertEqual(status?.waitingForUnlock, true)
        XCTAssertEqual(status?.lastError, "waiting for unlock")
        health.locked = false
        expect(await engine.drain(def, reason: "unlock"), .complete)
        status = await engine.snapshot().streams[def.key]
        XCTAssertEqual(status?.waitingForUnlock, false)
        XCTAssertNil(status?.lastError, "the old error does not stay after unlock (C1)")
    }

    func testAQueuedPageIsPrunedOnceTheAnchorMovesPastIt() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 3)
        server.script = [.link]
        let engine = try makeEngine(dir, health: health, uploader: server)
        await engine.drain(def, reason: "t")
        XCTAssertEqual(engine.outbox.counts.queued, 1)
        await engine.drain(def, reason: "t")   // reads the same anchor, acked, anchor moves
        XCTAssertEqual(engine.outbox.counts.queued, 0, "superseded: its data was just delivered")
        assertComplete(health, server, engine.outbox)
    }

    func testAnOutageQueuesOnePagePerAnchorNotOnePerWake() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 3)
        server.fallback = { _ in .link }
        let engine = try makeEngine(dir, health: health, uploader: server)
        for i in 0..<20 {
            fill(health, 1, from: 100 + i)
            await engine.drain(def, reason: "observer")
        }
        XCTAssertEqual(engine.outbox.counts.queued, 1, "the largest page from the one anchor, not 20 copies")
        XCTAssertEqual(engine.outbox.entries[0].header.count, 5)
    }

    func testTheSweepNeverQueuesAndKeepsItsCursorOnFailure() async throws {
        let health = FakeHealth(), server = FakeServer()
        let now = base.addingTimeInterval(3600)
        fill(health, 10)
        server.script = [.ok, .link]
        let engine = try makeEngine(dir, health: health, uploader: server, clock: { now })
        expect(await engine.sweep(def, reason: "foreground"), .linkDown)
        XCTAssertEqual(engine.outbox.counts.queued, 0, "a sweep page is read again, never queued")
        XCTAssertNil(engine.cursors.window("sweep:" + def.key).lastRunAt)
        expect(await engine.sweep(def, reason: "foreground", force: true), .done)
        XCTAssertEqual(server.alive.count, 10)
        XCTAssertNotNil(engine.cursors.window("sweep:" + def.key).lastRunAt)
        let calls = server.calls
        expect(await engine.sweep(def, reason: "foreground"), .skipped, "at most every 6 hours")
        XCTAssertEqual(server.calls, calls)
    }

    func testTheSweepReachesBackToTheLastFullyAckedWindow() async throws {
        let health = FakeHealth(), server = FakeServer()
        var now = base
        let engine = try makeEngine(dir, health: health, uploader: server, clock: { now })
        // A sweep ran on day 0; then five days of failures; a record from day 2 is
        // still sent on day 5, although it is older than 48 hours by then.
        expect(await engine.sweep(def, reason: "t", force: true), .done)
        health.add(stream: def.key, uuid: "late", start: base.addingTimeInterval(2 * 86400),
                   end: base.addingTimeInterval(2 * 86400 + 60))
        now = base.addingTimeInterval(5 * 86400)
        expect(await engine.sweep(def, reason: "t", force: true), .done)
        XCTAssertTrue(server.alive.contains("late"))
    }

    func testAReReadJobWorksWindowByWindowAndResumesAfterAFailure() async throws {
        let health = FakeHealth(), server = FakeServer()
        for d in 0..<30 {
            let t = base.addingTimeInterval(Double(d) * 86400)
            health.add(stream: def.key, uuid: "d\(d)", start: t, end: t.addingTimeInterval(60))
        }
        server.fallback = { $0 == 2 ? .link : .ok }
        let engine = try makeEngine(dir, health: health, uploader: server)
        try await engine.addRereadJob(def, start: base, end: base.addingTimeInterval(30 * 86400), reason: "owner")
        expect(await engine.runJobs(reason: "t"), 0)
        let job = try XCTUnwrap(engine.cursors.jobs.first)
        XCTAssertGreaterThan(job.next, base)
        XCTAssertLessThan(job.next, job.end)
        expect(await engine.runJobs(reason: "t"), 1)
        XCTAssertTrue(engine.cursors.jobs.isEmpty)
        XCTAssertEqual(server.alive.count, 30)
        XCTAssertEqual(engine.outbox.counts.queued, 0)
    }

    func testResetSendsTheWholeStreamAgainWithoutQueuedLeftovers() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 6)
        let engine = try makeEngine(dir, health: health, uploader: server)
        await engine.drain(def, reason: "t")
        server.script = [.link]
        fill(health, 2, from: 50)
        await engine.drain(def, reason: "t")
        XCTAssertEqual(engine.outbox.counts.queued, 1)
        try await engine.reset(def)
        XCTAssertEqual(engine.outbox.counts.queued, 0, "the old drain's queued page is superseded")
        await engine.drain(def, reason: "manual")
        XCTAssertEqual(server.received["\(def.shortName)-0"], 2, "re-sent once")
        assertComplete(health, server, engine.outbox)
    }

    func testDeletionsReachTheMacAndNeverResurrect() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 4)
        let engine = try makeEngine(dir, health: health, uploader: server)
        await engine.drain(def, reason: "t")
        health.delete(stream: def.key, uuid: "\(def.shortName)-1")
        await engine.drain(def, reason: "t")
        XCTAssertFalse(server.alive.contains("\(def.shortName)-1"))
        assertComplete(health, server, engine.outbox)
    }
}
