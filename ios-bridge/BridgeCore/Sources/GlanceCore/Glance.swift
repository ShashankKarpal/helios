import Foundation
#if canImport(Darwin)
import Darwin
#endif

// GlanceCore: the Helios glance (rank 3) without UIKit or WidgetKit. The reply
// heliosd sends at GET /api/glance, the cache the app and the widget share in
// the App Group, the one state the widget shows, its text, where numbers may
// show, when to refresh, and the morning notification plan. Pure where it can
// be, so `swift test` on the Mac covers it.

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
    /// No attempt to reach the Mac for this long and no complete brief for
    /// today in hand: the widget says "Not checked since ...".
    public static let staleAfter: TimeInterval = 3 * 3600
    /// The app and the widget fetch at most this often unless forced.
    public static let minInterval: TimeInterval = 15 * 60
    /// One fetch never takes longer than this in total.
    public static let timeout: TimeInterval = 8
    /// The brief is never published before this (reporting zone) and the
    /// morning notification waits for it on the phone's clock; the reply's
    /// not_before when it carries one (rank 3 fix, 2026-10-27; was 07:00).
    public static let notBefore = "07:30"
    /// heliosd rebuilds a brief Whoop revised until this (reporting zone); the
    /// reply's revise_until when it carries one.
    public static let reviseUntil = "12:00"
    /// The brief's due time when a reply carries none (was 09:00).
    public static let due = "10:00"
    /// From notBefore to reviseUntil the widget reloads every minInterval until
    /// today's complete brief is in hand, then at this interval (a rebuild).
    public static let readyInterval: TimeInterval = 30 * 60
    /// From this hour on the phone's clock the notification waits for the next morning.
    public static let eveningHour = 22
}

// MARK: - The reply (GET /api/glance)

public struct GlanceRow: Codable, Equatable, Sendable {
    public let metric: String
    public let label: String
    public let text: String
    /// favorable, neutral, flag, insufficient, in_progress, stand-in, waiting, unknown
    public let state: String
    /// "other device" or "so far" wherever the Today screen labels the value.
    public let note: String?

    public init(metric: String, label: String, text: String, state: String, note: String? = nil) {
        self.metric = metric
        self.label = label
        self.text = text
        self.state = state
        self.note = note
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
    /// When heliosd rewrote today's complete summary with other content (Whoop
    /// revised the night after the first publish); nil from older servers.
    public var revisedAt: String? = nil

    enum CodingKeys: String, CodingKey {
        case date, madeAt = "made_at", complete, verdict, rows, flags, awaiting, revisedAt = "revised_at"
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
    /// The brief's due time in the reporting zone, "10:00".
    public let due: String?
    public let brief: GlanceBrief
    public let summary: GlanceSummary?
    /// No brief before this, reporting zone ("07:30"); nil from older servers.
    public var notBefore: String? = nil
    /// heliosd rebuilds a revised brief until this, reporting zone ("12:00").
    public var reviseUntil: String? = nil

    enum CodingKeys: String, CodingKey {
        case schema, servedAt = "served_at", zone, due, brief, summary
        case notBefore = "not_before", reviseUntil = "revise_until"
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

    public var isLink: Bool {
        if case .link = self { return true }
        return false
    }
}

public enum GlanceClient {
    /// No cache, no cookies, and a total limit per fetch (a request timeout
    /// alone only bounds the time between packets).
    public static let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = GlanceTiming.timeout
        config.timeoutIntervalForResource = GlanceTiming.timeout
        config.waitsForConnectivity = false
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: config)
    }()

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

    /// A usable Summary from address: an https name (and port). Anything else
    /// is "not set up", never "not reachable".
    public static func isValidAddress(_ host: String) -> Bool {
        request(host: host, token: "", timeout: 1) != nil
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
                             session: URLSession = GlanceClient.session) async -> Result<GlanceReply, GlanceFailure>? {
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
    /// The newest good reply and when its request started.
    public var reply: GlanceReply?
    public var lastSuccess: Date?
    /// The start of the newest attempt merged in (a success or a failure).
    public var lastAttempt: Date?
    /// Set while the newest attempt failed; failingSince is the start of that
    /// run of failures of the same kind (no answer, or an answer that failed).
    public var lastFailure: GlanceFailure?
    public var failingSince: Date?

    public init() {}

    /// Merge one attempt. An attempt that started before the newest one
    /// already merged never changes the attempt state (the app and the widget
    /// both fetch), but a reply it got that the Mac served after the cached
    /// one is still the newer data and is kept.
    public func merging(attemptStart: Date, result: Result<GlanceReply, GlanceFailure>) -> GlanceCache {
        if let last = lastAttempt, attemptStart < last {
            guard case .success(let reply) = result, Self.served(reply, after: self.reply) else { return self }
            var c = self
            c.reply = reply
            c.lastSuccess = max(c.lastSuccess ?? attemptStart, attemptStart)
            return c
        }
        var c = self
        c.lastAttempt = attemptStart
        switch result {
        case .success(let reply):
            c.reply = reply
            c.lastSuccess = attemptStart
            c.lastFailure = nil
            c.failingSince = nil
        case .failure(let f):
            if c.lastFailure?.isLink != f.isLink { c.failingSince = attemptStart }
            c.lastFailure = f
        }
        return c
    }

    static func served(_ reply: GlanceReply, after other: GlanceReply?) -> Bool {
        guard let other else { return true }
        guard let a = GlanceClock.parse(reply.servedAt), let b = GlanceClock.parse(other.servedAt) else { return false }
        return a > b
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
    /// Test seam: runs inside the locked step, between the read and the write.
    static var updateHook: (() -> Void)?

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
        let current = load()
        Self.updateHook?()
        let next = change(current)
        var options: Data.WritingOptions = [.atomic]
        #if os(iOS)
        // Readable after the first unlock since a restart, so the widget can
        // draw the status while the phone is locked.
        options.insert(.completeFileProtectionUntilFirstUserAuthentication)
        #endif
        try Self.encoder().encode(next).write(to: file, options: options)
        return next
    }

    /// Merge one fetch result and save it; when the save fails, the merged
    /// cache is still returned, so a good reply is never thrown away in memory.
    public func merge(_ cache: GlanceCache, attemptStart: Date, result: Result<GlanceReply, GlanceFailure>) -> GlanceCache {
        (try? update { $0.merging(attemptStart: attemptStart, result: result) })
            ?? cache.merging(attemptStart: attemptStart, result: result)
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

    /// "HH:MM" as (hour, minute), or nil for anything else.
    public static func hourMinute(_ hhmm: String?) -> (Int, Int)? {
        guard let hhmm else { return nil }
        let parts = hhmm.split(separator: ":", omittingEmptySubsequences: false).map { Int($0) }
        guard parts.count == 2, let h = parts[0], let m = parts[1], (0..<24).contains(h), (0..<60).contains(m) else {
            return nil
        }
        return (h, m)
    }

    /// The instant of "HH:MM" (the 10:00 due time when missing or unreadable)
    /// on the reporting day holding `date`.
    public static func due(on date: Date, in zone: TimeZone, hhmm: String?) -> Date {
        let (h, m) = hourMinute(hhmm) ?? hourMinute(GlanceTiming.due) ?? (10, 0)
        let cal = calendar(zone)
        return cal.date(bySettingHour: h, minute: m, second: 0, of: date) ?? date
    }

    /// The next midnight after `date` in `zone`.
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
    /// No usable summary address set.
    case notSetUp
    /// Set up, no complete brief for today in hand, and no attempt to reach
    /// the Mac for a while (`since`: the last attempt; nil: never tried).
    /// late: past the due time, so the brief is late as far as the phone knows.
    case notChecked(since: Date?, late: Bool)
    /// Today's complete brief is in hand. updatedAt: heliosd rebuilt it after
    /// Whoop revised the night. offlineSince: the Mac stopped answering after
    /// it arrived. numbers: today's summary is in hand too (false when the Mac
    /// said the brief is complete before its summary existed, for example a
    /// brief published before an upgrade).
    case ready(readyAt: Date?, updatedAt: Date?, offlineSince: Date?, numbers: Bool)
    /// No complete brief for today in hand and the Mac cannot be reached.
    case unreachable(since: Date, late: Bool)
    /// Before the due time; whoop: today's brief is out but waits for Whoop.
    case waiting(whoop: Bool)
    /// Past the due time without a complete brief.
    case late(whoop: Bool)
    /// The Mac answered without a usable summary.
    case unavailable(GlanceFailure, late: Bool)
}

/// The one state the widget, the app and the notification agree on.
/// The reply's zone (heliosd's reporting zone) decides the day and the due
/// time, as heliosd does; `phoneZone` stands in only while no reply has ever
/// come or its zone cannot be read.
public func glanceState(_ c: GlanceCache, configured: Bool, now: Date,
                        staleAfter: TimeInterval = GlanceTiming.staleAfter,
                        phoneZone: TimeZone = .current) -> GlanceState {
    guard configured else { return .notSetUp }
    guard let lastAttempt = c.lastAttempt else {
        let zone = c.reply.flatMap { TimeZone(identifier: $0.zone) } ?? phoneZone
        return .notChecked(since: nil, late: now >= GlanceClock.due(on: now, in: zone, hhmm: c.reply?.due))
    }
    let linkDown: Date? = c.lastFailure?.isLink == true ? (c.failingSince ?? lastAttempt) : nil
    if let reply = c.reply {
        let zone = TimeZone(identifier: reply.zone) ?? phoneZone
        let today = GlanceClock.day(now, in: zone)
        let summary = reply.summary
        let numbers = summary?.date == today && summary?.complete == true
        // The Mac's own word counts too: a reply served today that says
        // today's brief is complete.
        let servedToday = GlanceClock.parse(reply.servedAt).map { GlanceClock.day($0, in: zone) == today } ?? false
        let macSaysReady = servedToday && reply.brief.today
        if numbers || macSaysReady {
            let ready = GlanceClock.parse(macSaysReady ? reply.brief.readyAt : nil)
                ?? (numbers ? GlanceClock.parse(summary?.madeAt) : nil)
            let updated = numbers ? GlanceClock.parse(summary?.revisedAt) : nil
            return .ready(readyAt: ready, updatedAt: updated, offlineSince: linkDown, numbers: numbers)
        }
    }
    // No complete brief for today in hand: from the due time (the reply's
    // reporting zone, or the phone's when no reply ever came) it is late,
    // whatever else the phone knows.
    let zone = c.reply.flatMap { TimeZone(identifier: $0.zone) } ?? phoneZone
    let late = now >= GlanceClock.due(on: now, in: zone, hhmm: c.reply?.due)
    // An old attempt says nothing about now.
    if now.timeIntervalSince(lastAttempt) > staleAfter { return .notChecked(since: lastAttempt, late: late) }
    if let since = linkDown { return .unreachable(since: since, late: late) }
    if let f = c.lastFailure { return .unavailable(f, late: late) }
    guard let reply = c.reply else { return .notChecked(since: nil, late: late) }
    let today = GlanceClock.day(now, in: zone)
    let whoop = reply.summary?.date == today && reply.summary?.complete == false
    return late ? .late(whoop: whoop) : .waiting(whoop: whoop)
}

/// When the state or its text can change without a fetch: the reporting
/// zone's midnight, the due time, the stale mark, and the phone's own
/// midnight (a time label turns into "Mon 07:12"). For the widget's timeline.
public func glanceChangePoints(_ c: GlanceCache, now: Date, phone: Calendar = .current,
                               horizon: TimeInterval = 24 * 3600,
                               staleAfter: TimeInterval = GlanceTiming.staleAfter) -> [Date] {
    var points: [Date] = []
    if let phoneMidnight = phone.date(byAdding: .day, value: 1, to: phone.startOfDay(for: now)) {
        points.append(phoneMidnight)
    }
    if let reply = c.reply {
        let zone = TimeZone(identifier: reply.zone) ?? .current
        let midnight = GlanceClock.nextMidnight(after: now, in: zone)
        points += [midnight, GlanceClock.due(on: now, in: zone, hhmm: reply.due),
                   GlanceClock.due(on: midnight, in: zone, hhmm: reply.due)]
    }
    if let last = c.lastAttempt { points.append(last.addingTimeInterval(staleAfter + 1)) }
    let end = now.addingTimeInterval(horizon)
    return Array(Set(points.filter { $0 > now && $0 <= end })).sorted()
}

/// When the widget should ask for a new timeline (reporting zone, times from
/// the reply): from not_before (07:30) to revise_until (12:00), every 15
/// minutes while today's complete brief is not in hand and every 30 minutes
/// once it is (heliosd may rebuild it); otherwise hourly, and never past the
/// next not_before. iOS budgets widget reloads, so the night must not use them
/// up. About 30 a day; Apple: "For a widget the user frequently views, a daily
/// budget typically includes from 40 to 70 refreshes."
public func glanceNextRefresh(_ c: GlanceCache, configured: Bool, now: Date) -> Date {
    let hour = now.addingTimeInterval(3600)
    guard configured else { return hour }
    // No reply yet (never fetched, or every attempt failed): no reporting zone
    // to plan by, so hourly; the app's own runs cover the first fetch.
    guard let reply = c.reply else { return hour }
    let zone = TimeZone(identifier: reply.zone) ?? .current
    let floor = GlanceClock.hourMinute(reply.notBefore) != nil ? reply.notBefore : GlanceTiming.notBefore
    let until = GlanceClock.hourMinute(reply.reviseUntil) != nil ? reply.reviseUntil : GlanceTiming.reviseUntil
    let start = GlanceClock.due(on: now, in: zone, hhmm: floor)
    let end = GlanceClock.due(on: now, in: zone, hhmm: until)
    if now < start { return min(start, hour) }
    if now < end {
        if case .ready = glanceState(c, configured: true, now: now) {
            return now.addingTimeInterval(GlanceTiming.readyInterval)
        }
        return now.addingTimeInterval(GlanceTiming.minInterval)
    }
    return min(hour, GlanceClock.due(on: GlanceClock.nextMidnight(after: now, in: zone), in: zone, hhmm: floor))
}

// MARK: - Where the numbers may show

public enum GlanceSurface: Sendable {
    case lockScreen
    case homeScreen
}

/// Never on the Lock Screen widget, locked or not (owner decision
/// 2026-10-26); on the Home Screen widget only while iOS is not asking for
/// privacy. The app's own screen needs an unlocked phone and always shows them.
public func surfaceShowsNumbers(_ surface: GlanceSurface, privacyRedacted: Bool) -> Bool {
    surface == .homeScreen && !privacyRedacted
}

/// The one thing the Lock Screen widget is given: the status line and, when
/// the last try found no Mac or is old, the Mac's line under it, so a problem
/// is said plainly there and never left to a footnote (owner decision
/// 2026-10-27). Its view receives this string and nothing else, so no number
/// can reach it.
public func glanceLockLine(_ display: GlanceDisplay) -> String {
    display.macLine.map { display.headline + "\n" + $0 } ?? display.headline
}

// MARK: - What the widget and the app show

public struct GlanceDisplay: Equatable, Sendable {
    /// The status line: the brief's state. The Lock Screen widget shows it
    /// with the Mac's line (glanceLockLine).
    public let headline: String
    /// The four morning numbers (Home Screen widget and the app only).
    public let rows: [GlanceRow]
    /// True only with today's complete brief; otherwise the numbers are dimmed.
    public let numbersCurrent: Bool
    /// "from Sun 21:40" when the numbers are not current.
    public let dataLine: String?
    /// The Mac's state when it is a problem the status line does not already
    /// say: "Mac not reachable since 10:05" (also with today's brief in hand),
    /// "Not checked since 06:00" or "Mac sent no summary" under a "Brief late"
    /// line.
    public let macLine: String?

    public init(headline: String, rows: [GlanceRow], numbersCurrent: Bool, dataLine: String?, macLine: String?) {
        self.headline = headline
        self.rows = rows
        self.numbersCurrent = numbersCurrent
        self.dataLine = dataLine
        self.macLine = macLine
    }
}

/// `when` formats an instant for the phone: a time today, a day and time otherwise.
/// From the due time without today's complete brief the status line says
/// "Brief late" and the Mac's state moves to the line under it.
private func lateLines(_ text: String, late: Bool) -> (String, String?) {
    late ? ("Brief late", text) : (text, nil)
}

public func glanceDisplay(_ state: GlanceState, cache: GlanceCache, when: (Date) -> String) -> GlanceDisplay {
    let headline: String
    var macLine: String?
    var current = false
    var showRows = true
    switch state {
    case .notSetUp:
        headline = "Set up in Helios Bridge"
        showRows = false
    case .notChecked(let since, let late):
        (headline, macLine) = lateLines(since.map { "Not checked since \(when($0))" } ?? "Not checked yet", late: late)
    case .ready(let readyAt, let updatedAt, let offline, let numbers):
        if let updatedAt {
            headline = "Brief updated \(when(updatedAt))"
        } else {
            headline = readyAt.map { "Brief ready \(when($0))" } ?? "Brief ready"
        }
        macLine = offline.map { "Mac not reachable since \(when($0))" }
        current = numbers
        // Never "Brief ready" over another day's numbers.
        showRows = numbers
    case .unreachable(let since, let late):
        (headline, macLine) = lateLines("Mac not reachable since \(when(since))", late: late)
    case .waiting(let whoop):
        headline = whoop ? "Waiting for Whoop's night" : "Waiting for this morning's brief"
    case .late(let whoop):
        headline = whoop ? "Brief late: waiting for Whoop" : "Brief late"
    case .unavailable(let f, let late):
        // Short enough for one Lock Screen line under "Brief late" (review L6).
        if case .http(let code) = f {
            (headline, macLine) = lateLines("Mac sent no summary (HTTP \(code))", late: late)
        } else {
            (headline, macLine) = lateLines("Mac sent no summary", late: late)
        }
    }
    let summary = cache.reply?.summary
    let rows = showRows ? (summary?.rows ?? []) : []
    let dataLine: String? = current || rows.isEmpty ? nil : GlanceClock.parse(summary?.madeAt).map { "from \(when($0))" }
    return GlanceDisplay(headline: headline, rows: rows, numbersCurrent: current && !rows.isEmpty,
                         dataLine: dataLine, macLine: macLine)
}

/// The lines under the Home Screen widget's numbers: the Mac's line first (a
/// problem is never dropped for the data line, review M1), then "from Sun
/// 21:40" when the numbers are not current.
public func glanceFooterLines(_ display: GlanceDisplay) -> [String] {
    [display.macLine, display.dataLine].compactMap { $0 }
}

/// A time today ("07:12"), a weekday and time otherwise ("Sun 21:40"), in the
/// phone's zone and its 12 or 24 hour setting. For the widget and the app.
public func glanceWhen(_ date: Date, now: Date, calendar: Calendar = .current, locale: Locale = .current) -> String {
    var time = Date.FormatStyle(date: .omitted, time: .shortened)
    time.timeZone = calendar.timeZone
    time.locale = locale
    let text = date.formatted(time)
    if calendar.isDate(date, inSameDayAs: now) { return text }
    var weekday = Date.FormatStyle().weekday(.abbreviated)
    weekday.timeZone = calendar.timeZone
    weekday.locale = locale
    return date.formatted(weekday) + " " + text
}

// MARK: - The morning notification

/// The notification identifiers. The morning one keeps one identifier, so a
/// pending one is replaced (Apple documents that for scheduled requests); each
/// quiet update has its own, per revision, because removing a shown
/// notification is asynchronous (Apple: removeDeliveredNotifications "executes
/// asynchronously ... on a background thread") and must never catch the one
/// being posted (review L6).
public enum MorningIDs {
    public static let morning = "helios.morning"

    public static func update(_ revision: String?) -> String {
        "helios.morning.update." + (revision ?? "none")
    }
}

public struct GlanceNotice: Equatable, Sendable {
    /// The reporting day it belongs to.
    public let day: String
    /// Carries no number: it may show on a locked phone.
    public let title: String
    public let body: String
    /// The summary's revised_at it carries (nil before any rebuild), so a
    /// revision is announced once.
    public var revision: String? = nil
    /// The request identifier it is posted under.
    public var id: String = MorningIDs.morning
}

/// "Resting HR 58 bpm (other device)": a row with its note, for plain text.
public func rowLine(_ row: GlanceRow) -> String {
    "\(row.label) \(row.text)" + (row.note.map { " (\($0))" } ?? "")
}

private func noticeBody(_ s: GlanceSummary) -> String {
    let numbers = s.rows.map(rowLine).joined(separator: ", ")
    let verdict = s.verdict.trimmingCharacters(in: .whitespacesAndNewlines)
    return [numbers.isEmpty ? nil : numbers + ".", verdict.isEmpty ? nil : verdict]
        .compactMap { $0 }.joined(separator: " ")
}

public func morningNotice(_ s: GlanceSummary) -> GlanceNotice {
    GlanceNotice(day: s.date, title: "Helios: brief ready", body: noticeBody(s), revision: s.revisedAt)
}

/// The quiet replacement once the morning notification was shown and heliosd
/// rebuilt the brief (owner answer (a), 2026-10-27). No number in the title.
public func updatedNotice(_ s: GlanceSummary) -> GlanceNotice {
    GlanceNotice(day: s.date, title: "Helios: brief updated", body: noticeBody(s), revision: s.revisedAt,
                 id: MorningIDs.update(s.revisedAt))
}

/// The day the morning notification was shown or scheduled, for when, and
/// the revision it carried (nil in records saved by 1.2 (3)).
public struct MorningRecord: Codable, Equatable, Sendable {
    public let day: String
    public let at: Date
    public var revision: String? = nil

    public init(day: String, at: Date, revision: String? = nil) {
        self.day = day
        self.at = at
        self.revision = revision
    }
}

public enum MorningPlan: Equatable, Sendable {
    case nothing
    case postNow(GlanceNotice)
    case schedule(Date, GlanceNotice)
    /// The shown ones (`removing`) are removed and this one is posted at the
    /// passive level; its identifier is never in `removing`.
    case replaceShown(GlanceNotice, removing: [String])
    /// Today's summary is no longer complete after a rebuild: the notification,
    /// pending or shown, is taken away; `revision` is recorded so it happens once.
    case withdraw(removing: [String], revision: String?)
}

/// Once per reporting day, for today's complete brief only, from the reply's
/// not_before (07:30 when it carries none) to 22:00 on the phone's clock
/// (`calendar` carries the phone's zone): before the floor it is scheduled
/// for it, from 22:00 for the next morning's floor (far west of the reporting
/// zone a brief arrives in the phone's evening), and only while that time
/// still falls on the brief's reporting day. A later run replaces a pending
/// one's content; a shown one is never repeated, but when heliosd rebuilt the
/// brief since (a revision it did not carry), it is replaced quietly, once per
/// revision, or taken away when today's summary is no longer complete.
public func morningPlan(_ c: GlanceCache, record: MorningRecord?, now: Date, calendar: Calendar,
                        eveningHour: Int = GlanceTiming.eveningHour) -> MorningPlan {
    guard let reply = c.reply, let s = reply.summary else { return .nothing }
    let zone = TimeZone(identifier: reply.zone) ?? .current
    guard s.date == GlanceClock.day(now, in: zone) else { return .nothing }
    let notice = morningNotice(s)
    if let record, record.day == s.date {
        if s.revisedAt == record.revision {
            // Nothing new since the notice: a pending one takes today's content.
            return now < record.at && s.complete ? .schedule(record.at, notice) : .nothing
        }
        let shown = [MorningIDs.morning] + (record.revision.map { [MorningIDs.update($0)] } ?? [])
        if !s.complete { return .withdraw(removing: shown, revision: s.revisedAt) }
        if now < record.at { return .schedule(record.at, notice) }
        return .replaceShown(updatedNotice(s), removing: shown)
    }
    guard s.complete else { return .nothing }
    let (h, m) = GlanceClock.hourMinute(reply.notBefore) ?? GlanceClock.hourMinute(GlanceTiming.notBefore) ?? (7, 30)
    guard let morning = calendar.date(bySettingHour: h, minute: m, second: 0, of: now),
          let evening = calendar.date(bySettingHour: eveningHour, minute: 0, second: 0, of: now) else { return .nothing }
    let at: Date?
    if now < morning {
        at = morning
    } else if now >= evening {
        at = calendar.date(byAdding: .day, value: 1, to: morning)
    } else {
        return .postNow(notice)
    }
    guard let at, GlanceClock.day(at, in: zone) == s.date else { return .nothing }
    return .schedule(at, notice)
}
