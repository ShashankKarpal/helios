import Foundation
import XCTest
@testable import BridgeCore

final class DurableFileTests: TempDirTestCase {
    func testWriteReplacesTheFileAndLeavesNoTemp() throws {
        let url = dir.appendingPathComponent("f")
        try DurableFile.write(Data("one".utf8), to: url)
        try DurableFile.write(Data("two".utf8), to: url)
        XCTAssertEqual(try String(contentsOf: url, encoding: .utf8), "two")
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: dir.path), ["f"])
    }

    func testCrashBeforeTheRenameKeepsTheOldBytes() throws {
        let url = dir.appendingPathComponent("f")
        try DurableFile.write(Data("old".utf8), to: url, point: "t")
        Faults.hook = { if $0 == "t.written" { throw SimulatedCrash(point: $0) } }
        XCTAssertThrowsError(try DurableFile.write(Data("new".utf8), to: url, point: "t"))
        Faults.hook = nil
        XCTAssertEqual(try String(contentsOf: url, encoding: .utf8), "old")
        // The temp file a crash leaves is swept on the next open.
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: dir.path).count, 2)
        DurableFile.removeStaleTemps(in: dir)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: dir.path), ["f"])
    }

    func testCrashAfterTheRenameHasTheNewBytes() throws {
        let url = dir.appendingPathComponent("f")
        try DurableFile.write(Data("old".utf8), to: url, point: "t")
        Faults.hook = { if $0 == "t.renamed" { throw SimulatedCrash(point: $0) } }
        XCTAssertThrowsError(try DurableFile.write(Data("new".utf8), to: url, point: "t"))
        Faults.hook = nil
        XCTAssertEqual(try String(contentsOf: url, encoding: .utf8), "new")
    }

    func testAFailedWriteThrowsInsteadOfPassingSilently() throws {
        let ro = dir.appendingPathComponent("ro", isDirectory: true)
        try FileManager.default.createDirectory(at: ro, withIntermediateDirectories: true)
        try FileManager.default.setAttributes([.posixPermissions: 0o500], ofItemAtPath: ro.path)
        defer { try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: ro.path) }
        XCTAssertThrowsError(try DurableFile.write(Data("x".utf8), to: ro.appendingPathComponent("f"))) { error in
            XCTAssertTrue(error is DurableError)
        }
    }

    func testRemovingAMissingFileIsNotAnError() throws {
        XCTAssertNoThrow(try DurableFile.remove(dir.appendingPathComponent("absent")))
    }

    func testOnlyAMissingFileReadsAsMissing() throws {
        XCTAssertNil(try DurableFile.read(dir.appendingPathComponent("absent")))
        let url = dir.appendingPathComponent("f")
        try DurableFile.write(Data("x".utf8), to: url)
        try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: url.path)
        defer { try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path) }
        XCTAssertThrowsError(try DurableFile.read(url), "a file that exists but cannot be read is not missing")
    }
}

/// Makes a file unreadable until the returned closure runs.
func lockFile(_ url: URL) throws -> () -> Void {
    try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: url.path)
    return { try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path) }
}

final class OutboxStoreTests: TempDirTestCase {
    var root: URL { dir.appendingPathComponent("Outbox", isDirectory: true) }

    func header(_ id: String, stream: String = "S", gen: Int = 0, count: Int = 1, origin: ItemOrigin = .anchored,
                at: Date = Date(timeIntervalSince1970: 1_790_000_000)) -> ItemHeader {
        ItemHeader(batchID: id, origin: origin, stream: origin == .anchored ? stream : nil,
                   generation: origin == .anchored ? gen : nil, nextAnchor: Data([1, 2, 3]),
                   types: ["HKQuantityTypeIdentifierHeartRate"], count: count, createdAt: at)
    }

    func testAPageRoundTripsAndSurvivesAReopen() throws {
        let store = try OutboxStore(root: root)
        let body = Data(#"{"batch_id":"b1","samples":[]}"#.utf8)
        guard case .written(let name) = try store.enqueue(header("b1"), body: body) else { return XCTFail() }
        let again = try OutboxStore(root: root)
        XCTAssertEqual(again.entries.map(\.name), [name])
        XCTAssertEqual(again.entries.first?.header, header("b1"))
        XCTAssertEqual(try again.body(name), body)
    }

    func testFailuresNeverDropAPage() throws {
        let store = try OutboxStore(root: root)
        guard case .written(let name) = try store.enqueue(header("b1"), body: Data("x".utf8)) else { return XCTFail() }
        let t0 = Date(timeIntervalSince1970: 1_790_000_000)
        for i in 0..<50 { store.failed(name, reason: "timeout", status: nil, now: t0.addingTimeInterval(Double(i))) }
        let again = try OutboxStore(root: root)
        XCTAssertEqual(again.entries.map(\.name), [name], "v1 dropped a page after 8 attempts; v2 never does")
        XCTAssertEqual(again.retryState(name)?.attempts, 50)
    }

    func testBackoffDoublesFromThirtySecondsToAnHour() {
        XCTAssertEqual(OutboxStore.backoff(attempts: 1), 30)
        XCTAssertEqual(OutboxStore.backoff(attempts: 2), 60)
        XCTAssertEqual(OutboxStore.backoff(attempts: 3), 120)
        XCTAssertEqual(OutboxStore.backoff(attempts: 9), 3600)
        XCTAssertEqual(OutboxStore.backoff(attempts: 400), 3600)
    }

    func testDueRespectsTheBackoff() throws {
        let store = try OutboxStore(root: root)
        guard case .written(let name) = try store.enqueue(header("b1"), body: Data("x".utf8)) else { return XCTFail() }
        let t0 = Date(timeIntervalSince1970: 1_790_000_000)
        store.failed(name, reason: "timeout", status: nil, now: t0)
        XCTAssertTrue(store.due(now: t0.addingTimeInterval(29)).isEmpty)
        XCTAssertEqual(store.due(now: t0.addingTimeInterval(30)).map(\.name), [name])
    }

    func testAckDeletesAndParkKeeps() throws {
        let store = try OutboxStore(root: root)
        guard case .written(let a) = try store.enqueue(header("a", stream: "A"), body: Data("a".utf8)),
              case .written(let b) = try store.enqueue(header("b", stream: "B"), body: Data("b".utf8)) else {
            return XCTFail()
        }
        try store.acked(a)
        try store.park(b, reason: "HTTP 422")
        let again = try OutboxStore(root: root)
        XCTAssertEqual(again.counts.queued, 0)
        XCTAssertEqual(again.parkedEntries.map(\.name), [b])
        XCTAssertEqual(try again.body(b), Data("b".utf8))
        XCTAssertEqual(try again.unparkAll(), 1)
        XCTAssertEqual(again.entries.map(\.name), [b])
    }

    func testTheSameAnchorKeepsOnlyTheLargestPage() throws {
        let store = try OutboxStore(root: root)
        guard case .written(let small) = try store.enqueue(header("p1", count: 10), body: Data("10".utf8)) else {
            return XCTFail()
        }
        XCTAssertEqual(try store.enqueue(header("p2", count: 10), body: Data("10b".utf8)), .duplicate(small))
        guard case .replaced(let big, old: small) = try store.enqueue(header("p3", count: 500), body: Data("500".utf8)) else {
            return XCTFail()
        }
        XCTAssertEqual(store.entries.map(\.name), [big])
        // Another generation is another page.
        guard case .written = try store.enqueue(header("p4", gen: 1, count: 5), body: Data("5".utf8)) else {
            return XCTFail()
        }
        XCTAssertEqual(store.counts.queued, 2)
    }

    func testPruneTakesOnlyAnchoredPages() throws {
        let store = try OutboxStore(root: root)
        try store.enqueue(header("a", gen: 0), body: Data("a".utf8))
        try store.enqueue(header("l", origin: .legacy), body: Data("l".utf8))
        XCTAssertEqual(store.prune { _ in true }, 1)
        XCTAssertEqual(store.entries.map(\.header.batchID), ["l"])
    }

    func testAnUnreadableFileIsQuarantinedNotDeleted() throws {
        _ = try OutboxStore(root: root)
        let junk = root.appendingPathComponent("0000000000001-0001-junk.item")
        try Data("not an outbox item".utf8).write(to: junk)
        let store = try OutboxStore(root: root)
        XCTAssertEqual(store.counts.queued, 0)
        XCTAssertEqual(store.counts.quarantined, 1)
        let kept = try FileManager.default.contentsOfDirectory(atPath: store.quarantineDir.path)
        XCTAssertEqual(kept.count, 1)
        XCTAssertEqual(try Data(contentsOf: store.quarantineDir.appendingPathComponent(kept[0])),
                       Data("not an outbox item".utf8))
    }

    func testAPageTheDiskRefusesIsReportedNotListed() throws {
        let store = try OutboxStore(root: root)
        try FileManager.default.setAttributes([.posixPermissions: 0o500], ofItemAtPath: root.path)
        defer { try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: root.path) }
        XCTAssertThrowsError(try store.enqueue(header("b1"), body: Data("x".utf8)))
        XCTAssertEqual(store.counts.queued, 0)
    }

    // MARK: v1 migration

    func testTheV1QueueMovesIntoOneFilePerPage() throws {
        let batches = [legacyBatch("L1", uuids: ["u1", "u2"]), legacyBatch("L2", uuids: ["u3"])]
        try writeLegacyOutbox(dir, batches: batches)
        let store = try OutboxStore(root: root)
        let m = try XCTUnwrap(try store.migrateLegacy(at: dir.appendingPathComponent("outbox.json"), now: Date()))
        XCTAssertEqual(m.migrated, 2)
        XCTAssertEqual(m.keptAs?.lastPathComponent, "outbox.v1-migrated.json")
        XCTAssertFalse(FileManager.default.fileExists(atPath: dir.appendingPathComponent("outbox.json").path))
        let bodies = try store.entries.map { try JSONDecoder().decode(BatchV1.self, from: store.body($0.name)) }
        XCTAssertEqual(bodies, batches, "the migrated body is the page the v1 Bridge would have sent")
        XCTAssertEqual(Set(store.entries.map(\.header.origin)), [.legacy])
    }

    func testTheOlderV1FormatMigratesToo() throws {
        let batches = [legacyBatch("L1", uuids: ["u1"])]
        try JSONEncoder().encode(batches).write(to: dir.appendingPathComponent("outbox.json"))
        let store = try OutboxStore(root: root)
        XCTAssertEqual(try store.migrateLegacy(at: dir.appendingPathComponent("outbox.json"), now: Date())?.migrated, 1)
    }

    func testAMigrationCrashMidwayResumesWithoutDuplicates() throws {
        let batches = (1...5).map { legacyBatch("L\($0)", uuids: ["u\($0)"]) }
        try writeLegacyOutbox(dir, batches: batches)
        var seen = 0
        Faults.hook = { point in
            if point == "outbox.migrate.item" {
                seen += 1
                if seen == 3 { throw SimulatedCrash(point: point) }
            }
        }
        let store = try OutboxStore(root: root)
        XCTAssertThrowsError(try store.migrateLegacy(at: dir.appendingPathComponent("outbox.json"), now: Date()))
        Faults.hook = nil
        let again = try OutboxStore(root: root)
        XCTAssertEqual(again.counts.queued, 3)
        let m = try XCTUnwrap(try again.migrateLegacy(at: dir.appendingPathComponent("outbox.json"), now: Date()))
        XCTAssertEqual(m.migrated, 2)
        XCTAssertEqual(m.alreadyPresent, 3)
        XCTAssertEqual(Set(again.entries.map(\.header.batchID)), Set(batches.map(\.batch_id)))
    }

    func testAnUndecodableV1FileIsQuarantinedNeverOverwritten() throws {
        let garbage = Data("[{\"batch\": oops".utf8)
        try garbage.write(to: dir.appendingPathComponent("outbox.json"))
        let store = try OutboxStore(root: root)
        let m = try XCTUnwrap(try store.migrateLegacy(at: dir.appendingPathComponent("outbox.json"), now: Date()))
        XCTAssertTrue(m.quarantined)
        let kept = try FileManager.default.contentsOfDirectory(atPath: store.quarantineDir.path)
        XCTAssertEqual(kept.count, 1)
        XCTAssertEqual(try Data(contentsOf: store.quarantineDir.appendingPathComponent(kept[0])), garbage)
    }

    func testAPageThatCannotBeReadNowStaysWhereItIs() throws {
        let store = try OutboxStore(root: root)
        try store.enqueue(header("a"), body: Data("a".utf8))
        let name = store.entries[0].name
        let unlock = try lockFile(root.appendingPathComponent(name))
        defer { unlock() }
        let later = try OutboxStore(root: root)
        XCTAssertEqual(later.unreadable, 1)
        XCTAssertEqual(later.counts.quarantined, 0, "never quarantined for being unreadable")
        XCTAssertTrue(FileManager.default.fileExists(atPath: root.appendingPathComponent(name).path))
        unlock()
        later.reload()
        XCTAssertEqual(later.unreadable, 0)
        XCTAssertEqual(later.entries.map(\.name), [name], "read again once it can be")
    }

    func testTheKeptV1CopyStaysWhileAnyPageCannotBeRead() throws {
        try writeLegacyOutbox(dir, batches: [legacyBatch("L1", uuids: ["u1"])])
        let store = try OutboxStore(root: root)
        try store.migrateLegacy(at: dir.appendingPathComponent("outbox.json"), now: Date())
        try store.enqueue(header("b"), body: Data("b".utf8))
        let legacy = try XCTUnwrap(store.entries.first { $0.header.origin == .legacy })
        let unlock = try lockFile(root.appendingPathComponent(legacy.name))
        defer { unlock() }
        store.reload()
        XCTAssertEqual(store.counts.legacy, 0, "the migrated page cannot be read, so it is not listed")
        XCTAssertEqual(store.removeMigratedCopies(in: dir), 0, "it could be a migrated page: keep the v1 copy")
        XCTAssertTrue(FileManager.default.fileExists(atPath: dir.appendingPathComponent("outbox.v1-migrated.json").path))
    }

    func testTheKeptV1CopyGoesOnlyWhenNoMigratedPageWaits() throws {
        try writeLegacyOutbox(dir, batches: [legacyBatch("L1", uuids: ["u1"])])
        let store = try OutboxStore(root: root)
        try store.migrateLegacy(at: dir.appendingPathComponent("outbox.json"), now: Date())
        XCTAssertEqual(store.removeMigratedCopies(in: dir), 0)
        try store.acked(store.entries[0].name)
        XCTAssertEqual(store.removeMigratedCopies(in: dir), 1)
        XCTAssertFalse(FileManager.default.fileExists(atPath: dir.appendingPathComponent("outbox.v1-migrated.json").path))
    }
}

final class AnchorStoreTests: TempDirTestCase {
    func testAV1AnchorFileCarriesOverAsGenerationZero() throws {
        let anchors = dir.appendingPathComponent("Anchors", isDirectory: true)
        try FileManager.default.createDirectory(at: anchors, withIntermediateDirectories: true)
        let v1 = Data("an archived HKQueryAnchor".utf8)
        try v1.write(to: anchors.appendingPathComponent("HKQuantityTypeIdentifierHeartRate"))
        let store = try AnchorStore(dir: anchors)
        XCTAssertEqual(store.anchor("HKQuantityTypeIdentifierHeartRate"), v1)
        XCTAssertEqual(store.generation("HKQuantityTypeIdentifierHeartRate"), 0)
        try store.save(Data("next".utf8), for: "HKQuantityTypeIdentifierHeartRate")
        XCTAssertEqual(store.anchor("HKQuantityTypeIdentifierHeartRate"), Data("next".utf8))
        XCTAssertEqual(store.generation("HKQuantityTypeIdentifierHeartRate"), 1)
    }

    func testConditionalSaveAndClear() throws {
        let store = try AnchorStore(dir: dir)
        try store.save(Data("a".utf8), for: "S")
        XCTAssertFalse(try store.save(Data("b".utf8), for: "S", ifGeneration: 0))
        XCTAssertTrue(try store.save(Data("b".utf8), for: "S", ifGeneration: 1))
        try store.clear("S")
        XCTAssertNil(store.anchor("S"))
        XCTAssertEqual(store.generation("S"), 3, "a clear moves the generation too")
    }

    func testAnAnchorThatCannotBeReadIsAnErrorNotAFreshStart() throws {
        let store = try AnchorStore(dir: dir)
        try store.save(Data("a".utf8), for: "S")
        let unlock = try lockFile(dir.appendingPathComponent("S"))
        defer { unlock() }
        XCTAssertThrowsError(try store.state("S"))
        XCTAssertEqual(store.generation("S"), 0, "0 on a read error: prune keeps every queued page")
        XCTAssertThrowsError(try store.save(Data("b".utf8), for: "S", ifGeneration: 0), "never written over")
        unlock()
        XCTAssertEqual(try store.state("S").anchor, Data("a".utf8))
        XCTAssertEqual(store.generation("S"), 1)
    }

    func testACrashDuringASaveKeepsTheOldAnchorAndGeneration() throws {
        let store = try AnchorStore(dir: dir)
        try store.save(Data("a".utf8), for: "S")
        Faults.hook = { if $0 == "anchor.save.written" { throw SimulatedCrash(point: $0) } }
        XCTAssertThrowsError(try store.save(Data("b".utf8), for: "S"))
        Faults.hook = nil
        let again = try AnchorStore(dir: dir)
        XCTAssertEqual(again.anchor("S"), Data("a".utf8))
        XCTAssertEqual(again.generation("S"), 1)
    }
}

final class CursorStoreTests: TempDirTestCase {
    func testWindowsAndJobsPersist() throws {
        let url = dir.appendingPathComponent("cursors.json")
        let store = try CursorStore(url: url)
        var w = WindowCursor()
        w.coveredThrough = Date(timeIntervalSince1970: 1_790_000_000)
        try store.setWindow("sweep:S", w)
        let job = RereadJob(stream: "S", start: Date(timeIntervalSince1970: 0), end: Date(timeIntervalSince1970: 100),
                            reason: "owner", createdAt: Date(timeIntervalSince1970: 0))
        try store.addJob(job)
        let again = try CursorStore(url: url)
        XCTAssertEqual(again.window("sweep:S"), w)
        XCTAssertEqual(again.jobs, [job])
    }

    func testAFailedSaveLeavesMemoryAsTheDiskHasIt() throws {
        let store = try CursorStore(url: dir.appendingPathComponent("cursors.json"))
        Faults.hook = { if $0 == "cursor.save.written" { throw SimulatedCrash(point: $0) } }
        var w = WindowCursor()
        w.coveredThrough = Date()
        XCTAssertThrowsError(try store.setWindow("k", w))
        Faults.hook = nil
        XCTAssertEqual(store.window("k"), WindowCursor(), "memory never leads the disk")
    }

    func testAFileThatCannotBeReadIsNeverWrittenOver() throws {
        let url = dir.appendingPathComponent("cursors.json")
        var w = WindowCursor()
        w.coveredThrough = Date(timeIntervalSince1970: 1_790_000_000)
        try CursorStore(url: url).setWindow("sweep:S", w)
        let unlock = try lockFile(url)
        defer { unlock() }
        let store = try CursorStore(url: url)
        XCTAssertTrue(store.unreadable)
        XCTAssertFalse(store.recoveredFromDamage, "unreadable is not damaged: nothing is kept aside")
        XCTAssertThrowsError(try store.setPageLimit("S", 10), "a write would lose the cursors it could not read")
        XCTAssertEqual(store.window("sweep:S"), WindowCursor())
        unlock()
        XCTAssertEqual(store.window("sweep:S"), w, "the first read once the file can be read sees it")
        try store.setPageLimit("S", 10)
        XCTAssertFalse(store.unreadable)
        let again = try CursorStore(url: url)
        XCTAssertEqual(again.window("sweep:S"), w, "the old cursors survived")
        XCTAssertEqual(again.pageLimit("S"), 10)
    }

    func testAWriteWorkedOutWhileUnreadableIsRefusedOnceItCanBeRead() throws {
        let url = dir.appendingPathComponent("cursors.json")
        var w = WindowCursor()
        w.backfillDone = true
        try CursorStore(url: url).setWindow("summaries", w)
        let unlock = try lockFile(url)
        let store = try CursorStore(url: url)
        let seen = store.window("summaries")   // empty: what the caller works from
        unlock()
        XCTAssertThrowsError(try store.setWindow("summaries", seen), "never written over the real cursors")
        XCTAssertEqual(store.window("summaries"), w)
        XCTAssertEqual(try CursorStore(url: url).window("summaries"), w)
    }

    func testTheReadAgainListPersistsAndAddsBeforeItRemoves() throws {
        let url = dir.appendingPathComponent("cursors.json")
        let store = try CursorStore(url: url)
        try store.addRetries("S", ["a", "b", "a"])
        try store.updateRetries("S", add: ["c", "d"], remove: ["b", "d"])
        XCTAssertEqual(store.retries("S"), ["a", "c"])
        let again = try CursorStore(url: url)
        XCTAssertEqual(again.retries("S"), ["a", "c"])
        XCTAssertEqual(again.retryCounts, ["S": 2])
        try again.removeRetries("S", ["a", "c"])
        XCTAssertEqual(again.retryCounts, [:])
    }

    func testADamagedFileIsKeptAsideAndTheStoreStartsEmpty() throws {
        let url = dir.appendingPathComponent("cursors.json")
        try Data("{damaged".utf8).write(to: url)
        let store = try CursorStore(url: url)
        XCTAssertTrue(store.recoveredFromDamage)
        let names = try FileManager.default.contentsOfDirectory(atPath: dir.path)
        XCTAssertTrue(names.contains { $0.hasPrefix("cursors.json.damaged-") })
    }
}
