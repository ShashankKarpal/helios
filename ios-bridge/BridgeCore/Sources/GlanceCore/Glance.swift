import Foundation
#if canImport(Darwin)
import Darwin
#endif

// GlanceCore: the Helios glance (rank 3) without UIKit or WidgetKit. The reply
// heliosd sends at GET /api/glance, the cache the app and the widget share in
// the App Group, the one state the widget shows, its text, and the morning
// notification plan. Pure where it can be, so `swift test` on the Mac covers it.

// MARK: - Shared names

public enum GlanceShared {
    /// The App Group of the app and its widget extension.
    public static let appGroup = "group.com.shanky.helios.bridge"
    /// UserDefaults keys in the App Group suite, mirrored by the app.
    public static let summaryHostKey = "helios.summaryHost"
    public static let tokenKey = "helios.token"
    static let cacheName = "glance.json"
    static let lockName = "glance.lock"

    /// The shared cache, or nil when the App Group container is not available
    /// (the entitlement missing from a build).
    public static func store() -> GlanceStore? {
        guard let dir = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: appGroup) else {
            return nil
        }
        return GlanceStore(directory: dir)
    }
}

public enum GlanceTiming {
    /// No successful check for this long and no complete brief for today in
    /// hand: the numbers are marked "Not checked since ...".
    public static let staleAfter: TimeInterval = 3 * 3600
    /// The app and the widget fetch at most this often unless forced.
    public static let minInterval: TimeInterval = 15 * 60
    /// One fetch never waits longer than this.
    public static let timeout: TimeInterval = 8
}

// MARK: - The reply (GET /api/glance)

public struct GlanceRow: Codable, Equatable, Sendable {
    public let metric: String
    public let label: String
    public let text: String
    /// favorable, neutral, flag, insufficient, in_progress, stand-in, waiting, unknown
    public let state: String

    public init(metric: String, label: String, text: String, state: String) {
        self.metric = metric
        self.label = label
        self.text = text
        self.state = state
    }
}

public struct GlanceSummary: Codable, Equatable, Sendable {
    /// The reporting day, yyyy-MM-dd.
    public let date: String
    public let madeAt: String
    public let complete: Bool
    public let verdict: String
    public let rows: [GlanceRow]
    public let flags: Int
    public let awaiting: [String]

    enum CodingKeys: String, CodingKey {
        case date, madeAt = "made_at", complete, verdict, rows, flags, awaiting
    }
}

public struct GlanceBrief: Codable, Equatable, Sendable {
    public let today: Bool
    public let overdue: Bool
    public let check: String?
    public let readyAt: String?

    enum CodingKeys: String, CodingKey {
        case today, overdue, check, readyAt = "ready_at"
    }
}

public struct GlanceReply: Codable, Equatable, Sendable {
    public let schema: Int
    public let servedAt: String
    /// The reporting zone, an IANA name such as Asia/Dubai.
    public let zone: String
    /// The brief's due time in the reporting zone, "09:00".
    public let due: String?
    public let brief: GlanceBrief
    public let summary: GlanceSummary?

    enum CodingKeys: String, CodingKey {
        case schema, servedAt = "served_at", zone, due, brief, summary
    }
}

// MARK: - Fetch results

public enum GlanceFailure: Error, Codable, Equatable, Sendable {
    /// No network, the name not found, no connection, a timeout, TLS: the Mac
    /// cannot be reached.
    case link(String)
    /// The Mac answered with an error status (401, 404, 5xx).
    case http(Int)
    /// The Mac answered with something that is not a glance.
    case unreadable
}

public enum GlanceClient {
    /// GET https://<host>/api/glance with the token, or nil when the address
    /// is empty or not https (the token never travels in clear).
    public static func request(host: String, token: String, timeout: TimeInterval) -> URLRequest? {
        let h = host.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !h.isEmpty else { return nil }
        let base = h.contains("://") ? h : "https://\(h)"
        guard let url = URL(string: base + "/api/glance"), url.scheme?.lowercased() == "https",
              let name = url.host, !name.isEmpty else { return nil }
        var r = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        r.httpMethod = "GET"
        r.setValue(token, forHTTPHeaderField: "X-Helios-Token")
        r.setValue("application/json", forHTTPHeaderField: "Accept")
        return r
    }

    /// One answer, sorted into a reply or a failure kind.
    public static func classify(data: Data?, response: URLResponse?, error: Error?) -> Result<GlanceReply, GlanceFailure> {
        if let error {
            if let u = error as? URLError {
                return .failure(.link(linkReason(u)))
            }
            return .failure(.link(String(describing: type(of: error))))
        }
        guard let http = response as? HTTPURLResponse else { return .failure(.unreadable) }
        guard http.statusCode == 200 else { return .failure(.http(http.statusCode)) }
        guard let data, let reply = try? JSONDecoder().decode(GlanceReply.self, from: data), reply.schema >= 1 else {
            return .failure(.unreadable)
        }
        return .success(reply)
    }

    static func linkReason(_ e: URLError) -> String {
        switch e.code {
        case .timedOut: return "timeout"
        case .cannotFindHost, .dnsLookupFailed: return "cannot find the Mac"
        case .cannotConnectToHost: return "cannot connect"
        case .notConnectedToInternet, .dataNotAllowed, .internationalRoamingOff: return "no network"
        case .networkConnectionLost: return "connection lost"
        case .secureConnectionFailed, .serverCertificateUntrusted, .serverCertificateHasBadDate,
             .serverCertificateNotYetValid, .serverCertificateHasUnknownRoot, .clientCertificateRejected:
            return "TLS not trusted"
        default: return "network error \(e.code.rawValue)"
        }
    }

    /// nil when the fetch was cancelled (iOS ended the background task): no
    /// news about the Mac, so nothing is merged and no outage is recorded.
    public static func fetch(host: String, token: String, timeout: TimeInterval = GlanceTiming.timeout,
                             session: URLSession = .shared) async -> Result<GlanceReply, GlanceFailure>? {
        guard let request = request(host: host, token: token, timeout: timeout) else {
            return .failure(.link("no https address set"))
        }
        do {
            let (data, response) = try await session.data(for: request)
            return classify(data: data, response: response, error: nil)
        } catch {
            if isCancellation(error) || Task.isCancelled { return nil }
            return classify(data: nil, response: nil, error: error)
        }
    }

    public static func isCancellation(_ error: Error) -> Bool {
        error is CancellationError || (error as? URLError)?.code == .cancelled
    }
}

// MARK: - The shared cache

public struct GlanceCache: Codable, Equatable, Sendable {
    /// The last good reply and when its request started.
    public var reply: GlanceReply?
    public var lastSuccess: Date?
    /// The start of the newest attempt merged in (a success or a failure).
    public var lastAttempt: Date?
    /// Set while the newest attempt failed; failingSince is the start of
    /// that run of failures.
    public var lastFailure: GlanceFailure?
    public var failingSince: Date?

    public init() {}

    /// Merge one attempt. An attempt that started before the newest one
    /// already merged is ignored, so a slow fetch never overwrites a newer
    /// result (the app and the widget both fetch).
    public func merging(attemptStart: Date, result: Result<GlanceReply, GlanceFailure>) -> GlanceCache {
        if let last = lastAttempt, attemptStart < last { return self }
        var c = self
        c.lastAttempt = attemptStart
        switch result {
        case .success(let reply):
            c.reply = reply
            c.lastSuccess = attemptStart
            c.lastFailure = nil
            c.failingSince = nil
        case .failure(let f):
            c.lastFailure = f
            if c.failingSince == nil { c.failingSince = attemptStart }
        }
        return c
    }

    /// Fetch now? Forced, never tried, or the last try is older than the interval.
    public func due(now: Date, minInterval: TimeInterval = GlanceTiming.minInterval) -> Bool {
        guard let last = lastAttempt else { return true }
        return now.timeIntervalSince(last) >= minInterval || last > now
    }
}

/// The cache file in the App Group container, read and written under a file
/// lock so the app and the widget extension never interleave a
/// read-modify-write.
public struct GlanceStore: Sendable {
    public let directory: URL

    public init(directory: URL) {
        self.directory = directory
    }

    var file: URL { directory.appendingPathComponent(GlanceShared.cacheName) }

    static func encoder() -> JSONEncoder {
        let e = JSONEncoder()
        e.dateEncodingStrategy = .secondsSince1970
        e.outputFormatting = [.sortedKeys]
        return e
    }

    static func decoder() -> JSONDecoder {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .secondsSince1970
        return d
    }

    /// The cache; an empty one when the file is missing or unreadable.
    public func load() -> GlanceCache {
        guard let data = try? Data(contentsOf: file),
              let cache = try? Self.decoder().decode(GlanceCache.self, from: data) else { return GlanceCache() }
        return cache
    }

    /// Read, change and write the cache in one locked step; returns what was written.
    @discardableResult
    public func update(_ change: (GlanceCache) -> GlanceCache) throws -> GlanceCache {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let lockPath = directory.appendingPathComponent(GlanceShared.lockName).path
        let fd = open(lockPath, O_RDWR | O_CREAT, 0o600)
        guard fd >= 0 else { throw CocoaError(.fileWriteUnknown) }
        defer { close(fd) }
        guard flock(fd, LOCK_EX) == 0 else { throw CocoaError(.fileLocking) }
        defer { flock(fd, LOCK_UN) }
        let next = change(load())
        var options: Data.WritingOptions = [.atomic]
        #if os(iOS)
        // Readable after the first unlock since a restart, so the widget can
        // draw the status while the phone is locked.
        options.insert(.completeFileProtectionUntilFirstUserAuthentication)
        #endif
        try Self.encoder().encode(next).write(to: file, options: options)
        return next
    }
}

// MARK: - Clock helpers (the reporting zone)

public enum GlanceClock {
    static func calendar(_ zone: TimeZone) -> Calendar {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = zone
        return cal
    }

    /// yyyy-MM-dd of `date` in `zone`.
    public static func day(_ date: Date, in zone: TimeZone) -> String {
        let c = calendar(zone).dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }

    /// The due instant ("09:00", the default) of the reporting day holding `date`.
    public static func due(on date: Date, in zone: TimeZone, hhmm: String?) -> Date {
        let parts = (hhmm ?? "09:00").split(separator: ":").compactMap { Int($0) }
        let h = parts.count == 2 && (0..<24).contains(parts[0]) ? parts[0] : 9
        let m = parts.count == 2 && (0..<60).contains(parts[1]) ? parts[1] : 0
        let cal = calendar(zone)
        return cal.date(bySettingHour: h, minute: m, second: 0, of: date) ?? date
    }

    /// The reporting zone's next midnight after `date`.
    public static func nextMidnight(after date: Date, in zone: TimeZone) -> Date {
        let cal = calendar(zone)
        let start = cal.startOfDay(for: date)
        return cal.date(byAdding: .day, value: 1, to: start) ?? date.addingTimeInterval(86_400)
    }

    /// An ISO 8601 time with its offset, as heliosd writes it.
    public static func parse(_ text: String?) -> Date? {
        guard let text else { return nil }
        return ISO8601DateFormatter().date(from: text)
    }
}

// MARK: - The state

public enum GlanceState: Equatable, Sendable {
    /// No summary address set.
    case notSetUp
    /// Set up, no complete brief for today in hand, and no successful check
    /// for a while (`since` nil: never checked).
    case notChecked(since: Date?)
    /// Today's complete brief is in hand. offlineSince: the Mac stopped
    /// answering after it arrived. numbers: today's summary is in hand too
    /// (false when the Mac said the brief is complete before its summary
    /// existed, for example a brief published before an upgrade).
    case ready(readyAt: Date?, offlineSince: Date?, numbers: Bool)
    /// No complete brief for today in hand and the Mac cannot be reached.
    case unreachable(since: Date)
    /// Before the due time; whoop: today's brief is out but waits for Whoop.
    case waiting(whoop: Bool)
    /// Past the due time without a complete brief.
    case late(whoop: Bool)
    /// The Mac answered without a usable summary.
    case unavailable(GlanceFailure)
}

/// The one state the widget, the app and the notification agree on.
public func glanceState(_ c: GlanceCache, configured: Bool, now: Date,
                        staleAfter: TimeInterval = GlanceTiming.staleAfter) -> GlanceState {
    guard configured else { return .notSetUp }
    let linkDown: Date? = {
        if case .link? = c.lastFailure { return c.failingSince ?? c.lastAttempt }
        return nil
    }()
    guard let reply = c.reply else {
        if let since = linkDown { return .unreachable(since: since) }
        if let f = c.lastFailure { return .unavailable(f) }
        return .notChecked(since: nil)
    }
    let zone = TimeZone(identifier: reply.zone) ?? .current
    let today = GlanceClock.day(now, in: zone)
    let summary = reply.summary
    let numbers = summary?.date == today && summary?.complete == true
    // The Mac's own word counts too: a reply served today that says today's
    // brief is complete.
    let servedToday = GlanceClock.parse(reply.servedAt).map { GlanceClock.day($0, in: zone) == today } ?? false
    let macSaysReady = servedToday && reply.brief.today
    if numbers || macSaysReady {
        let ready = GlanceClock.parse(macSaysReady ? reply.brief.readyAt : nil)
            ?? (numbers ? GlanceClock.parse(summary?.madeAt) : nil)
        return .ready(readyAt: ready, offlineSince: linkDown, numbers: numbers)
    }
    if let since = linkDown { return .unreachable(since: since) }
    if let f = c.lastFailure { return .unavailable(f) }
    if let last = c.lastSuccess, now.timeIntervalSince(last) > staleAfter { return .notChecked(since: last) }
    let whoop = summary?.date == today && summary?.complete == false
    let due = GlanceClock.due(on: now, in: zone, hhmm: reply.due)
    return now < due ? .waiting(whoop: whoop) : .late(whoop: whoop)
}

/// When the state can change without a fetch: the reporting zone's midnight,
/// the due time, the stale mark. For the widget's timeline.
public func glanceChangePoints(_ c: GlanceCache, now: Date, horizon: TimeInterval = 24 * 3600,
                               staleAfter: TimeInterval = GlanceTiming.staleAfter) -> [Date] {
    guard let reply = c.reply else { return [] }
    let zone = TimeZone(identifier: reply.zone) ?? .current
    let midnight = GlanceClock.nextMidnight(after: now, in: zone)
    var points = [midnight, GlanceClock.due(on: now, in: zone, hhmm: reply.due),
                  GlanceClock.due(on: midnight, in: zone, hhmm: reply.due)]
    if let last = c.lastSuccess { points.append(last.addingTimeInterval(staleAfter + 1)) }
    let end = now.addingTimeInterval(horizon)
    return Array(Set(points.filter { $0 > now && $0 <= end })).sorted()
}

// MARK: - What the widget and the app show

public struct GlanceDisplay: Equatable, Sendable {
    /// The status line. The Lock Screen widget shows only this.
    public let headline: String
    /// The four morning numbers (Home Screen widget and the app only).
    public let rows: [GlanceRow]
    /// True only with today's complete brief; otherwise the numbers are dimmed.
    public let numbersCurrent: Bool
    /// "from Sun 21:40" when the numbers are not current.
    public let dataLine: String?
    /// "Mac offline since 10:00" when the Mac stopped answering after the brief.
    public let footnote: String?

    public init(headline: String, rows: [GlanceRow], numbersCurrent: Bool, dataLine: String?, footnote: String?) {
        self.headline = headline
        self.rows = rows
        self.numbersCurrent = numbersCurrent
        self.dataLine = dataLine
        self.footnote = footnote
    }
}

/// `when` formats an instant for the phone: a time today, a day and time otherwise.
public func glanceDisplay(_ state: GlanceState, cache: GlanceCache, when: (Date) -> String) -> GlanceDisplay {
    let headline: String
    var footnote: String?
    var current = false
    switch state {
    case .notSetUp:
        headline = "Set up in Helios Bridge"
    case .notChecked(let since):
        headline = since.map { "Not checked since \(when($0))" } ?? "Not checked yet"
    case .ready(let readyAt, let offline, let numbers):
        headline = readyAt.map { "Brief ready \(when($0))" } ?? "Brief ready"
        footnote = offline.map { "Mac offline since \(when($0))" }
        current = numbers
    case .unreachable(let since):
        headline = "Mac not reachable since \(when(since))"
    case .waiting(let whoop):
        headline = whoop ? "Waiting for Whoop's night" : "Waiting for this morning's brief"
    case .late(let whoop):
        headline = whoop ? "Brief late: waiting for Whoop" : "Brief late"
    case .unavailable(let f):
        if case .http(let code) = f {
            headline = "Mac answered without a summary (HTTP \(code))"
        } else {
            headline = "Mac answered without a summary"
        }
    }
    let summary = state == .notSetUp ? nil : cache.reply?.summary
    let rows = summary?.rows ?? []
    let dataLine: String? = current || rows.isEmpty ? nil : GlanceClock.parse(summary?.madeAt).map { "from \(when($0))" }
    return GlanceDisplay(headline: headline, rows: rows, numbersCurrent: current && !rows.isEmpty,
                         dataLine: dataLine, footnote: footnote)
}

/// A time today ("07:12"), a weekday and time otherwise ("Sun 21:40"), in the
/// phone's zone and its 12 or 24 hour setting. For the widget and the app.
public func glanceWhen(_ date: Date, now: Date) -> String {
    let time = date.formatted(date: .omitted, time: .shortened)
    if Calendar.current.isDate(date, inSameDayAs: now) { return time }
    return date.formatted(.dateTime.weekday(.abbreviated)) + " " + time
}

// MARK: - The morning notification

public struct GlanceNotice: Equatable, Sendable {
    /// The reporting day it belongs to.
    public let day: String
    /// Carries no number: it may show on a locked phone.
    public let title: String
    public let body: String
}

public func morningNotice(_ s: GlanceSummary) -> GlanceNotice {
    let numbers = s.rows.map { "\($0.label) \($0.text)" }.joined(separator: ", ")
    let verdict = s.verdict.trimmingCharacters(in: .whitespacesAndNewlines)
    let body = [numbers.isEmpty ? nil : numbers + ".", verdict.isEmpty ? nil : verdict]
        .compactMap { $0 }.joined(separator: " ")
    return GlanceNotice(day: s.date, title: "Helios: brief ready", body: body)
}

/// The day the morning notification was shown or scheduled, and for when.
public struct MorningRecord: Codable, Equatable, Sendable {
    public let day: String
    public let at: Date

    public init(day: String, at: Date) {
        self.day = day
        self.at = at
    }
}

public enum MorningPlan: Equatable, Sendable {
    case nothing
    case postNow(GlanceNotice)
    case schedule(Date, GlanceNotice)
}

/// Once per reporting day, for today's complete brief only, never before
/// `notBeforeHour` on the phone's clock (`calendar` carries the phone's zone):
/// before it, scheduled for that time (a later run replaces the content while
/// it is pending); after it, at once.
public func morningPlan(_ c: GlanceCache, record: MorningRecord?, now: Date, calendar: Calendar,
                        notBeforeHour: Int = 7) -> MorningPlan {
    guard let reply = c.reply, let s = reply.summary, s.complete else { return .nothing }
    let zone = TimeZone(identifier: reply.zone) ?? .current
    guard s.date == GlanceClock.day(now, in: zone) else { return .nothing }
    let notice = morningNotice(s)
    if let record, record.day == s.date {
        return now < record.at ? .schedule(record.at, notice) : .nothing
    }
    let notBefore = calendar.date(bySettingHour: notBeforeHour, minute: 0, second: 0, of: now) ?? now
    return now < notBefore ? .schedule(notBefore, notice) : .postNow(notice)
}
