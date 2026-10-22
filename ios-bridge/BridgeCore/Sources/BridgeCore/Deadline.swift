import Foundation

// MARK: - A wake's deadline
// An observer wake must give HealthKit its completion inside the time iOS
// allows, or HealthKit backs off its deliveries. These pieces bound the work:
// the deadline itself, a once-only completion that a timer, the work and iOS's
// expiry handler can all call, and a time limit for one upload. They live here,
// not in the app, so the Mac's tests run them.

/// A call that runs at most once: from the work when it ends, from a timer at
/// the deadline, or from iOS ending the background time, whichever is first.
public final class OnceCall: @unchecked Sendable {   // guarded by `lock`
    private let lock = NSLock()
    private var action: (() -> Void)?

    public init(_ action: @escaping () -> Void) { self.action = action }

    public func call() {
        lock.lock()
        let run = action
        action = nil
        lock.unlock()
        run?()
    }

    public var called: Bool {
        lock.lock(); defer { lock.unlock() }
        return action == nil
    }
}

/// The time limit of one upload ran out.
public struct TimeLimitExceeded: Error, Equatable {
    public init() {}
}

public enum Deadline {
    /// A wake's deadline: `budget` after `start`, or earlier when iOS has less
    /// background time left (`remaining`, less `margin`), so the completion is
    /// never due after iOS suspends the app. Never before `now`.
    public static func wake(start: Date, budget: TimeInterval, remaining: TimeInterval, margin: TimeInterval = 2,
                            now: Date) -> Date {
        let byBudget = start.addingTimeInterval(budget)
        guard remaining.isFinite, remaining < 1e9 else { return byBudget }   // in the foreground iOS reports no limit
        let byIOS = now.addingTimeInterval(remaining - margin)
        return max(now, min(byBudget, byIOS))
    }

    /// Calls `once` at `deadline` unless the returned task is cancelled first.
    @discardableResult
    public static func callAt(_ deadline: Date, _ once: OnceCall) -> Task<Void, Never> {
        Task.detached {
            let wait = deadline.timeIntervalSinceNow
            if wait > 0 {
                do {
                    try await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
                } catch {
                    return   // cancelled: the work called it, or will
                }
            }
            once.call()
        }
    }

    /// Runs `operation`, giving up after `seconds` (nil: no limit) with
    /// TimeLimitExceeded and cancelling it. It returns by then when the
    /// operation honours cancellation (a URLSession request does): the task
    /// group waits for its child to finish.
    public static func run<T: Sendable>(within seconds: TimeInterval?,
                                        _ operation: @escaping @Sendable () async throws -> T) async throws -> T {
        guard let seconds else { return try await operation() }
        return try await withThrowingTaskGroup(of: T?.self) { group in
            group.addTask { try await operation() }
            group.addTask {
                try await Task.sleep(nanoseconds: UInt64(max(0, seconds) * 1_000_000_000))
                return nil
            }
            defer { group.cancelAll() }
            guard let first = try await group.next(), let value = first else { throw TimeLimitExceeded() }
            return value
        }
    }
}

/// How one upload is tried. Without a deadline: three attempts with a short
/// backoff and no limit of its own. Under a deadline: one attempt that must end
/// by it. Nothing when less than `minimum` seconds are left.
public struct UploadPlan: Equatable {
    public var attempts: Int
    public var within: TimeInterval?

    public static func make(deadline: Date?, now: Date, minimum: TimeInterval = 1) -> UploadPlan? {
        guard let deadline else { return UploadPlan(attempts: 3, within: nil) }
        let left = deadline.timeIntervalSince(now)
        guard left >= minimum else { return nil }
        return UploadPlan(attempts: 1, within: left)
    }
}
