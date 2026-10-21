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
        let engine = try makeEngine(dir, health: health, uploader: server, pageLimit: 20)
        expect(await engine.drain(def, reason: "t"), .complete)
        XCTAssertEqual(engine.cursors.pageLimit(def.key), 10)
        XCTAssertEqual(server.alive.count, 40)
    }

    func testAnOversizedPageHalvesDownToOneRecordThenParks() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 4)
        server.script = [.refuse(413), .refuse(413), .refuse(413)]
        let engine = try makeEngine(dir, health: health, uploader: server, pageLimit: 4)
        expect(await engine.drain(def, reason: "t"), .refused, "one record over the limit cannot be split")
        XCTAssertEqual(engine.cursors.pageLimit(def.key), 1, "halved 4, 2, 1: the real minimum")
        XCTAssertEqual(server.calls, 3)
        XCTAssertEqual(engine.outbox.counts.parked, 1)
        expect(await engine.drain(def, reason: "manual", manual: true), .complete)
        assertComplete(health, server, engine.outbox)
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

    func testATypeAskedBeforeItIsGrantedKeepsItsHistory() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 7)
        health.unauthorized = [def.key]
        let engine = try makeEngine(dir, health: health, uploader: server)
        expect(await engine.drain(def, reason: "launch"), .complete)
        XCTAssertNil(engine.anchors.anchor(def.key), "an empty first page leaves no anchor")
        health.unauthorized = []
        await engine.drain(def, reason: "granted")
        XCTAssertEqual(server.alive.count, 7, "the whole history arrives once the type is granted")
    }

    func testAnInterruptStopsRunningWorkAtTheNextPage() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 20)
        let gate = GatedUploader(server)
        let engine = try makeEngine(dir, health: health, uploader: gate)
        let task = Task { await engine.drain(def, reason: "processing") }
        await waitUntil { gate.waiting == 1 }
        await engine.interrupt()
        gate.release()
        expect(await task.value, .cancelled)
        XCTAssertEqual(server.calls, 1)
        let verified = await engine.snapshot().linkVerified
        XCTAssertTrue(verified, "an interrupt is not a Host change")
        expect(await engine.drain(def, reason: "next wake"), .complete)
        XCTAssertEqual(server.alive.count, 20)
    }

    func testSummariesSendTheRecentDaysThenTheHistoryAndWaitWhileNotGranted() async throws {
        let health = FakeHealth(), server = FakeServer()
        health.summaryFirstDay = base.addingTimeInterval(-30 * 86400)
        health.summaryDays = 31
        health.summariesUnauthorized = true
        var cfg = EngineConfig()
        cfg.historyStart = base.addingTimeInterval(-30 * 86400)
        cfg.historyChunkDays = 10
        cfg.recentDays = 3
        let engine = try makeEngine(dir, health: health, uploader: server, config: cfg, clock: { self.base })
        expect(await engine.window(.activitySummaries, reason: "launch"), .done)
        XCTAssertFalse(engine.cursors.window("summaries").backfillDone, "nothing found: not done")
        XCTAssertTrue(server.summaryDates.isEmpty)
        health.summariesUnauthorized = false
        expect(await engine.window(.activitySummaries, reason: "granted", force: true), .done)
        XCTAssertTrue(engine.cursors.window("summaries").backfillDone)
        XCTAssertEqual(server.summaryDates.count, 31, "every day, history and recent")
        XCTAssertEqual(engine.outbox.counts.queued, 0, "never queued")
    }

    func testAWakeWithABudgetNeverWaitsForARunningDrain() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 10)
        let gate = GatedUploader(server)
        let engine = try makeEngine(dir, health: health, uploader: gate)
        let long = Task { await engine.drain(def, reason: "foreground") }
        await waitUntil { gate.waiting == 1 }
        let started = Date()
        expect(await engine.drain(def, reason: "observer", deadline: Date().addingTimeInterval(20)), .joined)
        XCTAssertLessThan(Date().timeIntervalSince(started), 1, "returned at once, the page still in flight")
        let flush = await engine.flush(deadline: Date().addingTimeInterval(20))
        XCTAssertEqual(flush, FlushReport())
        gate.release()
        expect(await long.value, .complete)
        XCTAssertEqual(server.alive.count, 10)
    }

    func testADrainWithABudgetDoesNotQueueForASlot() async throws {
        let health = FakeHealth(), server = FakeServer()
        for s in Workload.streams { fill(health, 6, stream: s) }
        let gate = GatedUploader(server)
        let engine = try makeEngine(dir, health: health, uploader: gate)
        let a = Task { await engine.drain(Workload.streams[0], reason: "foreground") }
        let b = Task { await engine.drain(Workload.streams[1], reason: "foreground") }
        await waitUntil { gate.waiting == 2 }
        let c = Task { await engine.drain(Workload.streams[2], reason: "observer", deadline: Date().addingTimeInterval(20)) }
        await waitUntil { gate.waiting == 3 }   // the third runs beside the two open-ended drains
        gate.release()
        for t in [a, b, c] { _ = await t.value }
        XCTAssertEqual(server.alive.count, 18)
    }

    // MARK: Review rulings of 2026-10-21

    func testATypeTurnedOffAndOnAgainLosesNothingWrittenMeanwhile() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 5)
        let engine = try makeEngine(dir, health: health, uploader: server)
        expect(await engine.drain(def, reason: "t"), .complete)
        let generation = engine.anchors.generation(def.key)
        health.unauthorized = [def.key]
        fill(health, 3, from: 100)
        expect(await engine.drain(def, reason: "observer"), .complete)
        XCTAssertEqual(engine.anchors.generation(def.key), generation, "an empty page never moves the anchor")
        health.unauthorized = []
        expect(await engine.drain(def, reason: "turned on"), .complete)
        XCTAssertEqual(server.alive.count, 8, "what was written while the type was off arrives")
        assertComplete(health, server, engine.outbox)
    }

    func testARecordWhosePartsCannotBeReadIsHeldThenSent() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 5)
        let held = "\(def.shortName)-2"
        health.brokenParts = [held]
        let engine = try makeEngine(dir, health: health, uploader: server)
        expect(await engine.drain(def, reason: "t"), .complete, "one bad record does not stop the stream")
        XCTAssertEqual(server.alive.count, 4)
        XCTAssertEqual(engine.cursors.retries(def.key), [held])
        let shown = await engine.snapshot().retries
        XCTAssertEqual(shown, [def.key: 1])
        expect(await engine.runRetries(reason: "t", deadline: Date().addingTimeInterval(-1)), 0)
        XCTAssertEqual(health.uuidReads, 0, "no read past the deadline")
        expect(await engine.runRetries(reason: "t"), 0)
        XCTAssertEqual(engine.cursors.retries(def.key), [held], "still unreadable: still held")
        health.brokenParts = []
        expect(await engine.runRetries(reason: "t"), 1)
        XCTAssertEqual(engine.cursors.retryCounts, [:])
        assertComplete(health, server, engine.outbox)
    }

    func testAHeldRecordStaysWhileItsTypeIsOffAndGoesWithItsDeletion() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 3)
        let held = "\(def.shortName)-1"
        health.brokenParts = [held]
        let engine = try makeEngine(dir, health: health, uploader: server)
        await engine.drain(def, reason: "t")
        health.brokenParts = []
        health.unauthorized = [def.key]
        expect(await engine.runRetries(reason: "t"), 0)
        XCTAssertEqual(engine.cursors.retries(def.key), [held], "not returned is not deleted: kept")
        health.unauthorized = []
        health.delete(stream: def.key, uuid: held)
        expect(await engine.runRetries(reason: "t"), 0)
        XCTAssertEqual(engine.cursors.retries(def.key), [held])
        await engine.drain(def, reason: "t")
        XCTAssertEqual(engine.cursors.retryCounts, [:], "its deletion lets it go")
        assertComplete(health, server, engine.outbox)
    }

    func testAPageOfOnlyUnreadableRecordsHoldsThemAndMovesOn() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 2)
        let held: Set = ["\(def.shortName)-0", "\(def.shortName)-1"]
        health.brokenParts = held
        let engine = try makeEngine(dir, health: health, uploader: server)
        expect(await engine.drain(def, reason: "t"), .complete)
        XCTAssertEqual(server.calls, 0, "nothing to send")
        XCTAssertEqual(engine.anchors.generation(def.key), 1, "the anchor moved once they were held")
        XCTAssertEqual(Set(engine.cursors.retries(def.key)), held)
        health.brokenParts = []
        expect(await engine.runRetries(reason: "t"), 2)
        assertComplete(health, server, engine.outbox)
    }

    func testARecordLeftOutOfAQueuedPageIsHeldWhenTheQueuedCopyMovesTheAnchor() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 4)
        let held = "\(def.shortName)-3"
        health.brokenParts = [held]
        server.script = [.link]
        let engine = try makeEngine(dir, health: health, uploader: server)
        expect(await engine.drain(def, reason: "t"), .linkDown)
        XCTAssertEqual(engine.cursors.retries(def.key), [held], "held before the page waits in the outbox")
        let report = await engine.flush(force: true)
        XCTAssertEqual(report.anchorsAdvanced, 1, "the queued copy's ack moved the anchor past it")
        health.brokenParts = []
        expect(await engine.runRetries(reason: "t"), 1)
        assertComplete(health, server, engine.outbox)
    }

    func testEveryUploadUnderADeadlineCarriesIt() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 7)
        let engine = try makeEngine(dir, health: health, uploader: server)
        let deadline = Date().addingTimeInterval(25)
        expect(await engine.drain(def, reason: "observer", deadline: deadline), .complete)
        XCTAssertEqual(server.deadlines, [deadline, deadline])
        fill(health, 1, from: 50)
        await engine.drain(def, reason: "foreground")
        XCTAssertEqual(server.deadlines.count, 3)
        XCTAssertNil(server.deadlines[2] as Date?, "no deadline, none passed on")
    }

    func testADrainStopsAtItsDeadlineBeforeReadingAgain() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 12)
        var now = base
        let hook = HookUploader(server) { _ in now = now.addingTimeInterval(31) }   // the first upload uses the budget up
        let engine = try makeEngine(dir, health: health, uploader: hook, clock: { now })
        expect(await engine.drain(def, reason: "observer", deadline: base.addingTimeInterval(30)), .budget)
        XCTAssertEqual(server.calls, 1)
        XCTAssertEqual(health.anchoredReads, 1, "no HealthKit read after the deadline")
        expect(await engine.drain(def, reason: "foreground"), .complete)
        XCTAssertEqual(server.alive.count, 12)
    }

    func testDrainsWithABudgetHaveTheirOwnLimitOfThree() async throws {
        let health = FakeHealth(), server = FakeServer()
        let streams = Array(TypeRegistry.registered.prefix(5))
        for s in streams { fill(health, 3, stream: s) }
        let gate = GatedUploader(server)
        let engine = try makeEngine(dir, health: health, uploader: gate)
        let deadline = Date().addingTimeInterval(20)
        let tasks = streams.map { s in Task { await engine.drain(s, reason: "observer", deadline: deadline) } }
        await waitUntil { gate.waiting == 3 }
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(gate.waiting, 3, "the observer storm at a launch runs three at a time")
        gate.release()
        for t in tasks { expect(await t.value, .complete) }
        XCTAssertEqual(server.alive.count, 15)
    }

    func testABudgetDrainStopsWaitingForASlotAtItsDeadline() async throws {
        let health = FakeHealth(), server = FakeServer()
        for s in Workload.streams { fill(health, 3, stream: s) }
        let gate = GatedUploader(server)
        var cfg = EngineConfig()
        cfg.maxBudgetDrains = 1
        let engine = try makeEngine(dir, health: health, uploader: gate, config: cfg)
        let first = Task { await engine.drain(Workload.streams[0], reason: "observer", deadline: Date().addingTimeInterval(20)) }
        await waitUntil { gate.waiting == 1 }
        let started = Date()
        expect(await engine.drain(Workload.streams[1], reason: "observer", deadline: Date().addingTimeInterval(0.3)), .budget)
        let waited = Date().timeIntervalSince(started)
        XCTAssertGreaterThan(waited, 0.2)
        XCTAssertLessThan(waited, 2, "gave up at its deadline instead of waiting for the running drain")
        gate.release()
        expect(await first.value, .complete)
        expect(await engine.drain(Workload.streams[1], reason: "next wake"), .complete)
        XCTAssertEqual(server.alive.count, 6)
    }

    func testAnOpenEndedCallerFinishesWhatABudgetDrainLeft() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 12)
        var now = base
        let gate = GatedUploader(server)
        let engine = try makeEngine(dir, health: health, uploader: gate, clock: { now })
        let wake = Task { await engine.drain(self.def, reason: "observer", deadline: self.base.addingTimeInterval(30)) }
        await waitUntil { gate.waiting == 1 }
        let foreground = Task { await engine.drain(self.def, reason: "foreground") }
        try await Task.sleep(nanoseconds: 50_000_000)
        now = base.addingTimeInterval(31)   // the wake's budget runs out with its first page in flight
        gate.release()
        expect(await wake.value, .budget)
        expect(await foreground.value, .complete)
        XCTAssertEqual(server.alive.count, 12)
    }

    func testTheGlucoseSweepReachesBackFourteenDays() async throws {
        let health = FakeHealth(), server = FakeServer()
        let glucose = try XCTUnwrap(TypeRegistry.registered.first { $0.identifier == "HKQuantityTypeIdentifierBloodGlucose" })
        let now = base.addingTimeInterval(20 * 86400)
        let tenDaysAgo = now.addingTimeInterval(-10 * 86400)
        health.add(stream: glucose.key, uuid: "g-old", start: tenDaysAgo, end: tenDaysAgo.addingTimeInterval(60),
                   anchorHidden: true)
        health.add(stream: def.key, uuid: "h-old", start: tenDaysAgo, end: tenDaysAgo.addingTimeInterval(60),
                   anchorHidden: true)
        let engine = try makeEngine(dir, health: health, uploader: server, clock: { now })
        expect(await engine.sweep(glucose, reason: "t", force: true), .done)
        expect(await engine.sweep(def, reason: "t", force: true), .done)
        XCTAssertTrue(server.alive.contains("g-old"), "glucose: a reading only a dated query finds, 10 days back")
        XCTAssertFalse(server.alive.contains("h-old"), "other types: 48 hours (the documented limit)")
    }

    func testAnAnchorThatCannotBeReadStopsTheDrainInsteadOfStartingOver() async throws {
        let health = FakeHealth(), server = FakeServer()
        fill(health, 5)
        let engine = try makeEngine(dir, health: health, uploader: server)
        await engine.drain(def, reason: "t")
        fill(health, 2, from: 50)
        let unlock = try lockFile(dir.appendingPathComponent("Anchors").appendingPathComponent(def.key))
        defer { unlock() }
        expect(await engine.drain(def, reason: "t"), .readFailed)
        XCTAssertEqual(server.calls, 1, "nothing sent from the beginning again")
        unlock()
        expect(await engine.drain(def, reason: "t"), .complete)
        XCTAssertEqual(server.received.values.max(), 1, "no record sent twice")
        XCTAssertEqual(server.alive.count, 7)
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
