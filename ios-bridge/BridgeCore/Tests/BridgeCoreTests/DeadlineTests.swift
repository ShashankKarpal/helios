import Foundation
import XCTest
@testable import BridgeCore

/// A count several threads can bump.
final class Tally: @unchecked Sendable {   // guarded by `lock`
    private let lock = NSLock()
    private var n = 0
    func bump() { lock.withLock { n += 1 } }
    var value: Int { lock.withLock { n } }
}

/// The pieces that bound an observer wake (review ruling 1, verification M2
/// and L9): the once-only completion, the timer at the deadline, the deadline
/// itself and the time limit of one upload.
final class DeadlineTests: XCTestCase {
    func testAOnceCallRunsOnceWhoeverCallsFirst() async {
        let tally = Tally()
        let once = OnceCall { tally.bump() }
        await withTaskGroup(of: Void.self) { group in
            for _ in 0..<20 { group.addTask { once.call() } }
        }
        XCTAssertEqual(tally.value, 1, "the work, the timer and iOS's expiry: HealthKit hears once")
        XCTAssertTrue(once.called)
    }

    func testTheTimerCallsAtTheDeadlineWhileTheWorkStillRuns() async throws {
        let tally = Tally()
        let once = OnceCall { tally.bump() }
        let started = Date()
        Deadline.callAt(started.addingTimeInterval(0.2), once)
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(tally.value, 0, "not before the deadline")
        await waitUntil(timeout: 2) { tally.value == 1 }
        XCTAssertLessThan(Date().timeIntervalSince(started), 1.5)
        once.call()   // the work ends late: nothing more reaches HealthKit
        XCTAssertEqual(tally.value, 1)
    }

    func testACancelledTimerNeverCalls() async throws {
        let tally = Tally()
        let once = OnceCall { tally.bump() }
        Deadline.callAt(Date().addingTimeInterval(0.2), once).cancel()
        try await Task.sleep(nanoseconds: 400_000_000)
        XCTAssertEqual(tally.value, 0)
    }

    func testTheWakeDeadlineIsTheEarlierOfTheBudgetAndTheTimeIOSLeaves() {
        let t = Date(timeIntervalSince1970: 1_790_000_000)
        XCTAssertEqual(Deadline.wake(start: t, budget: 20, remaining: .greatestFiniteMagnitude, now: t),
                       t.addingTimeInterval(20), "in the foreground iOS sets no limit")
        XCTAssertEqual(Deadline.wake(start: t, budget: 20, remaining: 30, now: t), t.addingTimeInterval(20))
        XCTAssertEqual(Deadline.wake(start: t, budget: 20, remaining: 12, now: t), t.addingTimeInterval(10),
                       "12 seconds left, less a 2-second margin")
        XCTAssertEqual(Deadline.wake(start: t, budget: 20, remaining: 12, now: t.addingTimeInterval(5)),
                       t.addingTimeInterval(15), "iOS's time counts from now")
        XCTAssertEqual(Deadline.wake(start: t, budget: 20, remaining: 1, now: t), t, "no time left: due now, never earlier")
    }

    func testATimeLimitGivesUpOnTimeAndCancelsTheOperation() async {
        let started = Date()
        let cancelled = Tally()
        do {
            _ = try await Deadline.run(within: 0.2) { () async throws -> Int in
                do {
                    try await Task.sleep(nanoseconds: 5_000_000_000)
                } catch {
                    cancelled.bump()
                    throw error
                }
                return 1
            }
            XCTFail("the limit did not hold")
        } catch {
            XCTAssertTrue(error is TimeLimitExceeded, "\(error)")
        }
        XCTAssertLessThan(Date().timeIntervalSince(started), 1.5, "back by the limit, not after the operation")
        XCTAssertEqual(cancelled.value, 1, "the upload is cancelled, not left running")
    }

    func testATimeLimitReturnsTheResultWhenTheOperationIsFaster() async throws {
        let value = try await Deadline.run(within: 2) { 42 }
        XCTAssertEqual(value, 42)
        let unlimited = try await Deadline.run(within: nil) { 7 }
        XCTAssertEqual(unlimited, 7)
    }

    func testAnUploadUnderADeadlineIsOneAttemptThatEndsByIt() {
        let t = Date(timeIntervalSince1970: 1_790_000_000)
        XCTAssertEqual(UploadPlan.make(deadline: nil, now: t), UploadPlan(attempts: 3, within: nil))
        XCTAssertEqual(UploadPlan.make(deadline: t.addingTimeInterval(12), now: t), UploadPlan(attempts: 1, within: 12))
        XCTAssertNil(UploadPlan.make(deadline: t.addingTimeInterval(0.5), now: t), "under a second left: not started")
    }
}
