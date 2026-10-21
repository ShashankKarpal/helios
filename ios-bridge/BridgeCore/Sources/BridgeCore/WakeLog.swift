import Foundation

// MARK: - Wake log
// What iOS actually allows: every time the Bridge runs (an observer wake, a
// background refresh or processing task, a foreground, an unlock), one line with
// when, how long and what it achieved. Content-free (no health values). Shown on
// the status screen; the evidence for the delivery-cadence work. Best effort: a
// failed write never stops a sync.
public struct WakeEvent: Codable, Equatable {
    public var at: Date
    public var kind: String
    public var detail: String?
    public var seconds: Double?
    public var outcome: String?

    public init(at: Date, kind: String, detail: String? = nil, seconds: Double? = nil, outcome: String? = nil) {
        self.at = at
        self.kind = kind
        self.detail = detail
        self.seconds = seconds
        self.outcome = outcome
    }
}

public final class WakeLog: @unchecked Sendable {   // guarded by `lock`
    public let url: URL
    public let capacity: Int
    private let lock = NSLock()
    private var events: [WakeEvent] = []

    private static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.dateEncodingStrategy = .millisecondsSince1970
        return e
    }()
    private static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .millisecondsSince1970
        return d
    }()

    public init(url: URL, capacity: Int = 300) {
        self.url = url
        self.capacity = capacity
        try? DurableFile.makeDirectory(url.deletingLastPathComponent())
        if let data = try? Data(contentsOf: url), let stored = try? Self.decoder.decode([WakeEvent].self, from: data) {
            events = Array(stored.suffix(capacity))
        }
    }

    public func append(_ event: WakeEvent) {
        lock.lock(); defer { lock.unlock() }
        events.append(event)
        if events.count > capacity { events.removeFirst(events.count - capacity) }
        if let data = try? Self.encoder.encode(events) {
            try? DurableFile.write(data, to: url, point: "wakelog")
        }
    }

    public func recent(_ n: Int) -> [WakeEvent] {
        lock.lock(); defer { lock.unlock() }
        return Array(events.suffix(n).reversed())
    }

    /// Wakes per kind since a moment (the status screen's "last 24 hours" line).
    public func counts(since: Date) -> [String: Int] {
        lock.lock(); defer { lock.unlock() }
        var out: [String: Int] = [:]
        for e in events where e.at >= since {
            out[e.kind, default: 0] += 1
        }
        return out
    }
}
