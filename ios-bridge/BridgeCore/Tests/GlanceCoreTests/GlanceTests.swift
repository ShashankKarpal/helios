import Foundation
import XCTest
@testable import GlanceCore

// Rank 3: the glance logic the widget, the app and the morning notification
// share. Synthetic values; the reporting zone is Asia/Dubai, the phone's zone
// varies (Manila for the trip east).

private func at(_ iso: String) -> Date {
    guard let d = ISO8601DateFormatter().date(from: iso) else { fatalError("bad test date \(iso)") }
    return d
}

private let rows = [
    GlanceRow(metric: "recovery_score", label: "Recovery", text: "72%", state: "favorable"),
    GlanceRow(metric: "sleep_duration", label: "Sleep", text: "7h 12m", state: "neutral"),
    GlanceRow(metric: "hrv_rmssd", label: "HRV", text: "30 ms", state: "flag"),
    GlanceRow(metric: "resting_hr", label: "Resting HR", text: "58 bpm", state: "favorable"),
]

private func summary(_ date: String, complete: Bool = true, madeAt: String = "2026-10-26T07:12:00+04:00") -> GlanceSummary {
    GlanceSummary(date: date, madeAt: madeAt, complete: complete, verdict: "Mostly steady, one signal is off baseline.",
                  rows: rows, flags: 1, awaiting: complete ? [] : ["recovery_score"])
}

private func reply(_ s: GlanceSummary?, today: Bool = true, readyAt: String? = "2026-10-26T07:12:00+04:00",
                   due: String? = "09:00") -> GlanceReply {
    GlanceReply(schema: 1, servedAt: "2026-10-26T07:20:00+04:00", zone: "Asia/Dubai", due: due,
                brief: GlanceBrief(today: today, overdue: false, check: today ? "ok" : "early: ...", readyAt: readyAt),
                summary: s)
}

private func cache(_ r: GlanceReply?, success: String?, failure: GlanceFailure? = nil, failingSince: String? = nil,
                   lastAttempt: String? = nil) -> GlanceCache {
    var c = GlanceCache()
    c.reply = r
    c.lastSuccess = success.map(at)
    c.lastFailure = failure
    c.failingSince = failingSince.map(at)
    c.lastAttempt = (lastAttempt ?? failingSince ?? success).map(at)
    return c
}

private func calendar(_ zone: String) -> Calendar {
    var c = Calendar(identifier: .gregorian)
    c.timeZone = TimeZone(identifier: zone)!
    return c
}

private func when(_ d: Date) -> String {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.timeZone = TimeZone(identifier: "Asia/Dubai")
    f.dateFormat = "EEE HH:mm"
    return f.string(from: d)
}

// MARK: - The reply and the client

final class GlanceClientTests: XCTestCase {
    func testTheServersReplyDecodesAndUnknownKeysAreIgnored() throws {
        let json = """
        {"schema": 1, "served_at": "2026-10-26T07:20:00+04:00", "zone": "Asia/Dubai", "due": "09:00",
         "brief": {"today": true, "overdue": false, "check": "ok", "ready_at": "2026-10-26T07:12:00+04:00"},
         "summary": {"date": "2026-10-26", "made_at": "2026-10-26T07:12:00+04:00", "complete": true,
                     "verdict": "Recovery signals lean favorable.", "flags": 0, "awaiting": [],
                     "rows": [{"metric": "recovery_score", "label": "Recovery", "text": "72%", "state": "favorable"}]},
         "added_later": {"x": 1}}
        """
        let r = try JSONDecoder().decode(GlanceReply.self, from: Data(json.utf8))
        XCTAssertEqual(r.zone, "Asia/Dubai")
        XCTAssertEqual(r.brief.readyAt, "2026-10-26T07:12:00+04:00")
        XCTAssertEqual(r.summary?.rows.first?.text, "72%")
        let none = try JSONDecoder().decode(GlanceReply.self, from: Data("""
        {"schema": 1, "served_at": "x", "zone": "Asia/Dubai", "due": "09:00",
         "brief": {"today": false, "overdue": false, "check": "early: x", "ready_at": null}, "summary": null}
        """.utf8))
        XCTAssertNil(none.summary)
    }

    func testTheRequestIsHttpsWithTheTokenOrNothing() {
        let r = GlanceClient.request(host: "  name.example.ts.net:8420 ", token: "tok", timeout: 8)
        XCTAssertEqual(r?.url?.absoluteString, "https://name.example.ts.net:8420/api/glance")
        XCTAssertEqual(r?.value(forHTTPHeaderField: "X-Helios-Token"), "tok")
        XCTAssertEqual(r?.httpMethod, "GET")
        XCTAssertEqual(r?.timeoutInterval, 8)
        XCTAssertNotNil(GlanceClient.request(host: "https://name.example.ts.net:8420", token: "t", timeout: 8))
        XCTAssertNil(GlanceClient.request(host: "http://name.example.ts.net:8420", token: "t", timeout: 8))
        XCTAssertNil(GlanceClient.request(host: "   ", token: "t", timeout: 8))
    }

    func testOnlyAnHttpsNameIsAUsableAddress() {
        XCTAssertTrue(GlanceClient.isValidAddress("name.example.ts.net:8420"))
        XCTAssertTrue(GlanceClient.isValidAddress("https://name.example.ts.net:8420"))
        XCTAssertFalse(GlanceClient.isValidAddress("http://name.example.ts.net:8420"))
        XCTAssertFalse(GlanceClient.isValidAddress(""))
        XCTAssertFalse(GlanceClient.isValidAddress("   "))
        XCTAssertEqual(GlanceClient.session.configuration.timeoutIntervalForResource, GlanceTiming.timeout)
    }

    func testAnswersAreSortedIntoLinkHttpAndUnreadable() throws {
        let url = URL(string: "https://name.example.ts.net:8420/api/glance")!
        func http(_ code: Int) -> HTTPURLResponse { HTTPURLResponse(url: url, statusCode: code, httpVersion: nil, headerFields: nil)! }
        let good = try JSONEncoder().encode(reply(summary("2026-10-26")))
        XCTAssertEqual(try GlanceClient.classify(data: good, response: http(200), error: nil).get().zone, "Asia/Dubai")
        XCTAssertEqual(GlanceClient.classify(data: Data("<html>".utf8), response: http(200), error: nil), .failure(.unreadable))
        XCTAssertEqual(GlanceClient.classify(data: Data(), response: http(401), error: nil), .failure(.http(401)))
        XCTAssertEqual(GlanceClient.classify(data: Data(), response: http(404), error: nil), .failure(.http(404)))
        XCTAssertEqual(GlanceClient.classify(data: nil, response: nil, error: URLError(.timedOut)), .failure(.link("timeout")))
        XCTAssertEqual(GlanceClient.classify(data: nil, response: nil, error: URLError(.cannotFindHost)),
                       .failure(.link("cannot find the Mac")))
        XCTAssertEqual(GlanceClient.classify(data: nil, response: nil, error: URLError(.notConnectedToInternet)),
                       .failure(.link("no network")))
        XCTAssertEqual(GlanceClient.classify(data: nil, response: nil, error: URLError(.serverCertificateUntrusted)),
                       .failure(.link("TLS not trusted")))
    }

    func testFetchGoesThroughTheSessionAndAnUnsetAddressIsAFailure() async throws {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubProtocol.self]
        let session = URLSession(configuration: config)
        StubProtocol.reply = { req in
            XCTAssertEqual(req.value(forHTTPHeaderField: "X-Helios-Token"), "tok")
            return (200, try! JSONEncoder().encode(reply(summary("2026-10-26"))))
        }
        let ok = await GlanceClient.fetch(host: "name.example.ts.net:8420", token: "tok", session: session)
        XCTAssertEqual(try ok?.get().summary?.date, "2026-10-26")
        StubProtocol.reply = { _ in (401, Data("{\"detail\":\"bad or missing X-Helios-Token\"}".utf8)) }
        let refused = await GlanceClient.fetch(host: "name.example.ts.net:8420", token: "bad", session: session)
        XCTAssertEqual(refused, .failure(.http(401)))
        let unset = await GlanceClient.fetch(host: "", token: "tok", session: session)
        XCTAssertEqual(unset, .failure(.link("no https address set")))
    }

    func testACancelledFetchIsNoNewsNotAnOutage() async {
        XCTAssertTrue(GlanceClient.isCancellation(URLError(.cancelled)))
        XCTAssertTrue(GlanceClient.isCancellation(CancellationError()))
        XCTAssertFalse(GlanceClient.isCancellation(URLError(.timedOut)))
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubProtocol.self]
        let session = URLSession(configuration: config)
        StubProtocol.reply = { _ in (200, Data()) }
        let task = Task { () -> Result<GlanceReply, GlanceFailure>? in
            withUnsafeCurrentTask { $0?.cancel() }
            return await GlanceClient.fetch(host: "name.example.ts.net:8420", token: "tok", session: session)
        }
        let result = await task.value
        XCTAssertNil(result)
    }
}

final class StubProtocol: URLProtocol {
    static var reply: ((URLRequest) -> (Int, Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let (code, data) = Self.reply?(request) ?? (500, Data())
        let response = HTTPURLResponse(url: request.url!, statusCode: code, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

// MARK: - The cache

final class GlanceCacheTests: XCTestCase {
    func testASlowerOlderAttemptNeverOverwritesANewerResult() {
        let r = reply(summary("2026-10-26"))
        var c = GlanceCache().merging(attemptStart: at("2026-10-26T07:20:00+04:00"), result: .success(r))
        c = c.merging(attemptStart: at("2026-10-26T07:19:00+04:00"), result: .failure(.link("timeout")))
        XCTAssertNil(c.lastFailure)
        XCTAssertEqual(c.lastSuccess, at("2026-10-26T07:20:00+04:00"))
    }

    func testFailuresKeepTheStartOfTheRunAndASuccessClearsIt() {
        var c = GlanceCache()
        c = c.merging(attemptStart: at("2026-10-26T06:10:00+04:00"), result: .failure(.link("timeout")))
        c = c.merging(attemptStart: at("2026-10-26T06:40:00+04:00"), result: .failure(.link("cannot connect")))
        XCTAssertEqual(c.failingSince, at("2026-10-26T06:10:00+04:00"))
        XCTAssertEqual(c.lastFailure, .link("cannot connect"))
        XCTAssertNil(c.reply)
        c = c.merging(attemptStart: at("2026-10-26T07:20:00+04:00"), result: .success(reply(summary("2026-10-26"))))
        XCTAssertNil(c.lastFailure)
        XCTAssertNil(c.failingSince)
        XCTAssertNotNil(c.reply)
    }

    func testTheOutageStartRestartsWhenTheFailureKindChanges() {
        var c = GlanceCache()
        c = c.merging(attemptStart: at("2026-10-26T06:00:00+04:00"), result: .failure(.http(500)))
        c = c.merging(attemptStart: at("2026-10-26T06:30:00+04:00"), result: .failure(.link("timeout")))
        XCTAssertEqual(c.failingSince, at("2026-10-26T06:30:00+04:00"))       // the Mac answered at 06:00
        c = c.merging(attemptStart: at("2026-10-26T07:00:00+04:00"), result: .failure(.link("cannot connect")))
        XCTAssertEqual(c.failingSince, at("2026-10-26T06:30:00+04:00"))
    }

    func testAnOlderSuccessStillBringsANewerReply() {
        // Two fetches at once: the newer one failed first, the older one's reply was served later.
        let old = GlanceReply(schema: 1, servedAt: "2026-10-26T05:00:00+04:00", zone: "Asia/Dubai", due: "09:00",
                              brief: GlanceBrief(today: false, overdue: false, check: "early: ...", readyAt: nil),
                              summary: summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"))
        var c = cache(old, success: "2026-10-26T05:00:00+04:00")
        c = c.merging(attemptStart: at("2026-10-26T06:00:01+04:00"), result: .failure(.link("timeout")))
        let fresh = GlanceReply(schema: 1, servedAt: "2026-10-26T06:00:02+04:00", zone: "Asia/Dubai", due: "09:00",
                                brief: GlanceBrief(today: true, overdue: false, check: "ok", readyAt: "2026-10-26T05:00:00+04:00"),
                                summary: summary("2026-10-26"))
        c = c.merging(attemptStart: at("2026-10-26T06:00:00+04:00"), result: .success(fresh))
        XCTAssertEqual(c.reply, fresh)
        XCTAssertEqual(c.lastAttempt, at("2026-10-26T06:00:01+04:00"))        // the attempt state stays the newer one's
        XCTAssertEqual(c.lastFailure, .link("timeout"))
        // An older success with an older reply changes nothing.
        let stale = c.merging(attemptStart: at("2026-10-26T05:59:00+04:00"), result: .success(old))
        XCTAssertEqual(stale, c)
    }

    func testAFetchIsDueOnlyAfterTheInterval() {
        var c = GlanceCache()
        let t = at("2026-10-26T07:00:00+04:00")
        XCTAssertTrue(c.due(now: t))
        c.lastAttempt = t
        XCTAssertFalse(c.due(now: t.addingTimeInterval(14 * 60)))
        XCTAssertTrue(c.due(now: t.addingTimeInterval(15 * 60)))
        XCTAssertTrue(c.due(now: t.addingTimeInterval(-60)))      // the clock went back
    }
}

// MARK: - The state table (design.md section 1)

final class GlanceStateTests: XCTestCase {
    func testNotSetUpAndNeverChecked() {
        XCTAssertEqual(glanceState(cache(reply(summary("2026-10-26")), success: "2026-10-26T07:20:00+04:00"),
                                   configured: false, now: at("2026-10-26T07:30:00+04:00")), .notSetUp)
        XCTAssertEqual(glanceState(GlanceCache(), configured: true, now: at("2026-10-26T07:30:00+04:00")),
                       .notChecked(since: nil))
    }

    func testOnlyFailuresAreUnreachableOrUnavailable() {
        let now = at("2026-10-26T07:30:00+04:00")
        XCTAssertEqual(glanceState(cache(nil, success: nil, failure: .link("timeout"), failingSince: "2026-10-26T06:10:00+04:00"),
                                   configured: true, now: now), .unreachable(since: at("2026-10-26T06:10:00+04:00")))
        XCTAssertEqual(glanceState(cache(nil, success: nil, failure: .http(404), failingSince: "2026-10-26T06:10:00+04:00"),
                                   configured: true, now: now), .unavailable(.http(404)))
    }

    func testTodaysCompleteBriefIsReadyAndALaterOutageIsOnlyAFootnote() {
        let r = reply(summary("2026-10-26"))
        let now = at("2026-10-26T10:30:00+04:00")
        XCTAssertEqual(glanceState(cache(r, success: "2026-10-26T07:20:00+04:00"), configured: true, now: now),
                       .ready(readyAt: at("2026-10-26T07:12:00+04:00"), offlineSince: nil, numbers: true))
        let down = cache(r, success: "2026-10-26T07:20:00+04:00", failure: .link("timeout"),
                         failingSince: "2026-10-26T10:00:00+04:00")
        XCTAssertEqual(glanceState(down, configured: true, now: now),
                       .ready(readyAt: at("2026-10-26T07:12:00+04:00"), offlineSince: at("2026-10-26T10:00:00+04:00"), numbers: true))
        let http = cache(r, success: "2026-10-26T07:20:00+04:00", failure: .http(500),
                         failingSince: "2026-10-26T10:00:00+04:00")
        XCTAssertEqual(glanceState(http, configured: true, now: now),
                       .ready(readyAt: at("2026-10-26T07:12:00+04:00"), offlineSince: nil, numbers: true))
    }

    func testTheMacsWordMakesItReadyBeforeItsSummaryExists() {
        // Published before the upgrade: brief.today true, no summary yet (or yesterday's).
        let r = GlanceReply(schema: 1, servedAt: "2026-10-26T07:20:00+04:00", zone: "Asia/Dubai", due: "09:00",
                            brief: GlanceBrief(today: true, overdue: false, check: "ok", readyAt: "2026-10-26T05:00:00+04:00"),
                            summary: summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"))
        let c = cache(r, success: "2026-10-26T07:20:00+04:00")
        let state = glanceState(c, configured: true, now: at("2026-10-26T07:30:00+04:00"))
        XCTAssertEqual(state, .ready(readyAt: at("2026-10-26T05:00:00+04:00"), offlineSince: nil, numbers: false))
        let shown = glanceDisplay(state, cache: c, when: when)
        XCTAssertEqual(shown.headline, "Brief ready Mon 05:00")
        XCTAssertTrue(shown.rows.isEmpty)                  // never "Brief ready" over yesterday's numbers
        XCTAssertFalse(shown.numbersCurrent)
        XCTAssertNil(shown.dataLine)
        // A reply served yesterday says nothing about today.
        let old = GlanceReply(schema: 1, servedAt: "2026-10-25T23:00:00+04:00", zone: "Asia/Dubai", due: "09:00",
                              brief: GlanceBrief(today: true, overdue: false, check: "ok", readyAt: "2026-10-25T05:00:00+04:00"),
                              summary: nil)
        XCTAssertEqual(glanceState(cache(old, success: "2026-10-25T23:00:00+04:00"), configured: true,
                                   now: at("2026-10-26T01:00:00+04:00")), .waiting(whoop: false))
    }

    func testWithoutTodaysBriefAnUnreachableMacIsItsOwnState() {
        let r = reply(summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"), today: false, readyAt: nil)
        let c = cache(r, success: "2026-10-25T23:00:00+04:00", failure: .link("cannot connect"),
                      failingSince: "2026-10-25T23:40:00+04:00", lastAttempt: "2026-10-26T07:20:00+04:00")
        XCTAssertEqual(glanceState(c, configured: true, now: at("2026-10-26T07:30:00+04:00")),
                       .unreachable(since: at("2026-10-25T23:40:00+04:00")))
    }

    func testAnOldFailureGivesWayToNotChecked() {
        // The last attempt (an HTTP 500 or no answer) was at 22:00 and nothing ran since.
        let r = reply(summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"), today: false, readyAt: nil)
        for failure in [GlanceFailure.http(500), .link("timeout")] {
            let c = cache(r, success: "2026-10-25T21:00:00+04:00", failure: failure, failingSince: "2026-10-25T22:00:00+04:00")
            XCTAssertEqual(glanceState(c, configured: true, now: at("2026-10-26T08:00:00+04:00")),
                           .notChecked(since: at("2026-10-25T22:00:00+04:00")), "\(failure)")
        }
    }

    func testNoSuccessfulCheckForThreeHoursIsNotChecked() {
        let r = reply(summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"), today: false, readyAt: nil)
        let c = cache(r, success: "2026-10-26T03:40:00+04:00")
        XCTAssertEqual(glanceState(c, configured: true, now: at("2026-10-26T06:41:00+04:00")),
                       .notChecked(since: at("2026-10-26T03:40:00+04:00")))
        XCTAssertEqual(glanceState(c, configured: true, now: at("2026-10-26T06:39:00+04:00")), .waiting(whoop: false))
    }

    func testWaitingBeforeNineAndLateAfter() {
        let yesterday = reply(summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"), today: false, readyAt: nil)
        XCTAssertEqual(glanceState(cache(yesterday, success: "2026-10-26T08:30:00+04:00"), configured: true,
                                   now: at("2026-10-26T08:59:00+04:00")), .waiting(whoop: false))
        XCTAssertEqual(glanceState(cache(yesterday, success: "2026-10-26T09:00:00+04:00"), configured: true,
                                   now: at("2026-10-26T09:01:00+04:00")), .late(whoop: false))
        let waiting = reply(summary("2026-10-26", complete: false, madeAt: "2026-10-26T08:30:00+04:00"), today: false, readyAt: nil)
        XCTAssertEqual(glanceState(cache(waiting, success: "2026-10-26T08:31:00+04:00"), configured: true,
                                   now: at("2026-10-26T08:45:00+04:00")), .waiting(whoop: true))
        XCTAssertEqual(glanceState(cache(waiting, success: "2026-10-26T09:05:00+04:00"), configured: true,
                                   now: at("2026-10-26T09:06:00+04:00")), .late(whoop: true))
    }

    func testTheReportingZonesMidnightEndsYesterdaysBriefWhereverThePhoneIs() {
        // Complete for 2026-10-26 (Dubai). At 00:30 Dubai on the 27th (04:30 in Manila) it is no longer today's.
        let r = reply(summary("2026-10-26"))
        let c = cache(r, success: "2026-10-27T00:20:00+04:00")
        XCTAssertEqual(glanceState(c, configured: true, now: at("2026-10-26T23:59:00+04:00")),
                       .ready(readyAt: at("2026-10-26T07:12:00+04:00"), offlineSince: nil, numbers: true))
        XCTAssertEqual(glanceState(c, configured: true, now: at("2026-10-27T04:30:00+08:00")), .waiting(whoop: false))
    }

    func testATripEastChecksAgainstDubaisNineNotThePhones() {
        // 09:05 in Manila is 05:05 in Dubai: before the due time, so waiting, not late.
        let r = reply(summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"), today: false, readyAt: nil)
        XCTAssertEqual(glanceState(cache(r, success: "2026-10-26T09:00:00+08:00"), configured: true,
                                   now: at("2026-10-26T09:05:00+08:00")), .waiting(whoop: false))
        XCTAssertEqual(glanceState(cache(r, success: "2026-10-26T13:00:00+08:00"), configured: true,
                                   now: at("2026-10-26T13:05:00+08:00")), .late(whoop: false))
    }

    func testTheChangePointsAreMidnightTheDueTimeAndTheStaleMark() {
        let r = reply(summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"), today: false, readyAt: nil)
        let points = glanceChangePoints(cache(r, success: "2026-10-26T06:00:00+04:00"), now: at("2026-10-26T06:30:00+04:00"),
                                        phone: calendar("Asia/Dubai"))
        XCTAssertEqual(points, [at("2026-10-26T09:00:00+04:00"), at("2026-10-26T09:00:01+04:00"),
                                at("2026-10-27T00:00:00+04:00")])
        XCTAssertEqual(glanceChangePoints(GlanceCache(), now: at("2026-10-26T06:30:00+04:00"), phone: calendar("Asia/Dubai")),
                       [at("2026-10-27T00:00:00+04:00")])
    }

    func testInLondonThePhonesMidnightIsAChangePointToo() {
        // 20:30 London is 00:30 Dubai on the 27th: the phone's own midnight turns "07:12" into "Mon 07:12".
        let r = reply(summary("2026-10-26"))
        let points = glanceChangePoints(cache(r, success: "2026-10-26T19:30:00+00:00"), now: at("2026-10-26T20:30:00+00:00"),
                                        phone: calendar("Europe/London"))
        XCTAssertEqual(points, [at("2026-10-26T22:30:01+00:00"), at("2026-10-27T00:00:00+00:00"),
                                at("2026-10-27T05:00:00+00:00"), at("2026-10-27T20:00:00+00:00")])
    }
}

// MARK: - What is shown

final class GlanceDisplayTests: XCTestCase {
    func testHeadlinesForEveryState() {
        let c = cache(reply(summary("2026-10-26")), success: "2026-10-26T07:20:00+04:00")
        let t = at("2026-10-26T06:10:00+04:00")
        let expected: [(GlanceState, String)] = [
            (.notSetUp, "Set up in Helios Bridge"),
            (.notChecked(since: nil), "Not checked yet"),
            (.notChecked(since: t), "Not checked since Mon 06:10"),
            (.ready(readyAt: t, offlineSince: nil, numbers: true), "Brief ready Mon 06:10"),
            (.ready(readyAt: nil, offlineSince: nil, numbers: false), "Brief ready"),
            (.unreachable(since: t), "Mac not reachable since Mon 06:10"),
            (.waiting(whoop: true), "Waiting for Whoop's night"),
            (.waiting(whoop: false), "Waiting for this morning's brief"),
            (.late(whoop: true), "Brief late: waiting for Whoop"),
            (.late(whoop: false), "Brief late"),
            (.unavailable(.http(404)), "Mac answered without a summary (HTTP 404)"),
            (.unavailable(.unreadable), "Mac answered without a summary"),
        ]
        for (state, headline) in expected {
            XCTAssertEqual(glanceDisplay(state, cache: c, when: when).headline, headline, "\(state)")
        }
    }

    func testOnlyAReadyBriefShowsCurrentNumbersAndOldOnesSayWhenTheyAreFrom() {
        let old = cache(reply(summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"), today: false, readyAt: nil),
                        success: "2026-10-25T23:00:00+04:00")
        let shown = glanceDisplay(.unreachable(since: at("2026-10-25T23:40:00+04:00")), cache: old, when: when)
        XCTAssertFalse(shown.numbersCurrent)
        XCTAssertEqual(shown.dataLine, "from Sun 21:40")
        XCTAssertEqual(shown.rows.count, 4)
        let ready = glanceDisplay(.ready(readyAt: at("2026-10-26T07:12:00+04:00"), offlineSince: at("2026-10-26T10:00:00+04:00"), numbers: true),
                                  cache: cache(reply(summary("2026-10-26")), success: "2026-10-26T07:20:00+04:00"), when: when)
        XCTAssertTrue(ready.numbersCurrent)
        XCTAssertNil(ready.dataLine)
        XCTAssertEqual(ready.footnote, "Mac offline since Mon 10:00")
        let unset = glanceDisplay(.notSetUp, cache: old, when: when)
        XCTAssertTrue(unset.rows.isEmpty)
    }
}

// MARK: - The morning notification

final class MorningPlanTests: XCTestCase {
    private let dubai: Calendar = {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "Asia/Dubai")!
        return c
    }()
    private let manila: Calendar = {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "Asia/Manila")!
        return c
    }()

    func testBeforeSevenItIsScheduledForSevenAfterItPostedAtOnce() {
        let c = cache(reply(summary("2026-10-26", madeAt: "2026-10-26T06:00:00+04:00")), success: "2026-10-26T06:20:00+04:00")
        let notice = morningNotice(summary("2026-10-26", madeAt: "2026-10-26T06:00:00+04:00"))
        XCTAssertEqual(morningPlan(c, record: nil, now: at("2026-10-26T06:30:00+04:00"), calendar: dubai),
                       .schedule(at("2026-10-26T07:00:00+04:00"), notice))
        XCTAssertEqual(morningPlan(c, record: nil, now: at("2026-10-26T07:30:00+04:00"), calendar: dubai), .postNow(notice))
    }

    func testOncePerReportingDayAPendingOneIsReplacedAShownOneNotRepeated() {
        let c = cache(reply(summary("2026-10-26")), success: "2026-10-26T06:20:00+04:00")
        let pending = MorningRecord(day: "2026-10-26", at: at("2026-10-26T07:00:00+04:00"))
        XCTAssertEqual(morningPlan(c, record: pending, now: at("2026-10-26T06:45:00+04:00"), calendar: dubai),
                       .schedule(at("2026-10-26T07:00:00+04:00"), morningNotice(summary("2026-10-26"))))
        XCTAssertEqual(morningPlan(c, record: pending, now: at("2026-10-26T07:30:00+04:00"), calendar: dubai), .nothing)
        let yesterdays = MorningRecord(day: "2026-10-25", at: at("2026-10-25T07:00:00+04:00"))
        XCTAssertEqual(morningPlan(c, record: yesterdays, now: at("2026-10-26T07:30:00+04:00"), calendar: dubai),
                       .postNow(morningNotice(summary("2026-10-26"))))
    }

    func testOnlyTodaysCompleteBrief() {
        let waiting = cache(reply(summary("2026-10-26", complete: false)), success: "2026-10-26T08:00:00+04:00")
        XCTAssertEqual(morningPlan(waiting, record: nil, now: at("2026-10-26T08:00:00+04:00"), calendar: dubai), .nothing)
        let yesterday = cache(reply(summary("2026-10-25")), success: "2026-10-26T08:00:00+04:00")
        XCTAssertEqual(morningPlan(yesterday, record: nil, now: at("2026-10-26T08:00:00+04:00"), calendar: dubai), .nothing)
        XCTAssertEqual(morningPlan(GlanceCache(), record: nil, now: at("2026-10-26T08:00:00+04:00"), calendar: dubai), .nothing)
    }

    func testOnATripEastSevenIsThePhonesSeven() {
        // Dubai's brief for the 26th completes at 06:00 Dubai, 10:00 in Manila: after 07:00 there, so at once.
        let c = cache(reply(summary("2026-10-26", madeAt: "2026-10-26T06:00:00+04:00")), success: "2026-10-26T10:00:00+08:00")
        XCTAssertEqual(morningPlan(c, record: nil, now: at("2026-10-26T10:00:00+08:00"), calendar: manila),
                       .postNow(morningNotice(summary("2026-10-26", madeAt: "2026-10-26T06:00:00+04:00"))))
    }

    func testTheTitleCarriesNoNumberAndTheBodyCarriesThem() {
        let n = morningNotice(summary("2026-10-26"))
        XCTAssertEqual(n.title, "Helios: brief ready")
        XCTAssertNil(n.title.rangeOfCharacter(from: .decimalDigits))
        XCTAssertEqual(n.body, "Recovery 72%, Sleep 7h 12m, HRV 30 ms, Resting HR 58 bpm. Mostly steady, one signal is off baseline.")
        XCTAssertEqual(n.day, "2026-10-26")
    }
}

// MARK: - The shared store

final class GlanceStoreTests: XCTestCase {
    var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("glancecore-\(UUID().uuidString)")
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    func testAMissingOrDamagedFileReadsAsAnEmptyCacheAndUpdateRoundTrips() throws {
        let store = GlanceStore(directory: dir)
        XCTAssertEqual(store.load(), GlanceCache())
        let r = reply(summary("2026-10-26"))
        let written = try store.update { $0.merging(attemptStart: at("2026-10-26T07:20:00+04:00"), result: .success(r)) }
        XCTAssertEqual(store.load(), written)
        XCTAssertEqual(store.load().reply, r)
        try Data("{not json".utf8).write(to: store.file)
        XCTAssertEqual(store.load(), GlanceCache())
    }

    func testUpdatesFromTwoWritersNeverLoseTheNewerAttempt() throws {
        let store = GlanceStore(directory: dir)
        let r = reply(summary("2026-10-26"))
        let group = DispatchGroup()
        // Probabilistic: without the file lock a read-modify-write race can drop
        // the newest attempt; 100 writers make that race likely.
        for i in 0..<100 {
            group.enter()
            DispatchQueue.global().async {
                let start = at("2026-10-26T07:00:00+04:00").addingTimeInterval(TimeInterval(i))
                _ = try? store.update { $0.merging(attemptStart: start, result: i % 2 == 0 ? .success(r) : .failure(.link("timeout"))) }
                group.leave()
            }
        }
        group.wait()
        XCTAssertEqual(store.load().lastAttempt, at("2026-10-26T07:01:39+04:00"))
        XCTAssertEqual(store.load().lastFailure, .link("timeout"))      // attempt 99 failed
    }
}

// MARK: - Review fixes (2026-10-26)

final class GlanceRefreshTests: XCTestCase {
    private let yesterdays = reply(summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"), today: false, readyAt: nil)

    func testFifteenMinutesOnlyInsideTheMorningWindowHourlyOtherwise() {
        func next(_ now: String, _ c: GlanceCache) -> Date { glanceNextRefresh(c, configured: true, now: at(now)) }
        let recent = { (t: String) in cache(self.yesterdays, success: t) }
        XCTAssertEqual(next("2026-10-26T01:00:00+04:00", recent("2026-10-26T00:50:00+04:00")), at("2026-10-26T02:00:00+04:00"))
        XCTAssertEqual(next("2026-10-26T04:30:00+04:00", recent("2026-10-26T04:20:00+04:00")), at("2026-10-26T05:00:00+04:00"))
        XCTAssertEqual(next("2026-10-26T06:00:00+04:00", recent("2026-10-26T05:50:00+04:00")), at("2026-10-26T06:15:00+04:00"))
        XCTAssertEqual(next("2026-10-26T10:00:00+04:00", recent("2026-10-26T09:50:00+04:00")), at("2026-10-26T11:00:00+04:00"))
        let down = cache(yesterdays, success: "2026-10-26T20:00:00+04:00", failure: .link("timeout"),
                         failingSince: "2026-10-26T22:00:00+04:00", lastAttempt: "2026-10-26T23:20:00+04:00")
        XCTAssertEqual(next("2026-10-26T23:30:00+04:00", down), at("2026-10-27T00:30:00+04:00"))
    }

    func testReadyNotSetUpAndNeverFetched() {
        let ready = cache(reply(summary("2026-10-26")), success: "2026-10-26T07:20:00+04:00")
        XCTAssertEqual(glanceNextRefresh(ready, configured: true, now: at("2026-10-26T07:30:00+04:00")),
                       at("2026-10-26T08:30:00+04:00"))
        XCTAssertEqual(glanceNextRefresh(ready, configured: false, now: at("2026-10-26T07:30:00+04:00")),
                       at("2026-10-26T08:30:00+04:00"))
        XCTAssertEqual(glanceNextRefresh(GlanceCache(), configured: true, now: at("2026-10-26T07:30:00+04:00")),
                       at("2026-10-26T07:45:00+04:00"))
    }
}

final class GlanceSurfaceTests: XCTestCase {
    func testTheLockScreenNeverShowsANumberTheHomeScreenOnlyWithoutPrivacy() {
        XCTAssertFalse(surfaceShowsNumbers(.lockScreen, privacyRedacted: false))
        XCTAssertFalse(surfaceShowsNumbers(.lockScreen, privacyRedacted: true))
        XCTAssertTrue(surfaceShowsNumbers(.homeScreen, privacyRedacted: false))
        XCTAssertFalse(surfaceShowsNumbers(.homeScreen, privacyRedacted: true))
    }
}

final class MorningPlanReviewTests: XCTestCase {
    func testFarWestTheEveningBriefWaitsForTheNextMorningOfTheSameReportingDay() {
        // 07:12 Dubai on the 26th is 23:12 in New York on the 25th: scheduled for 07:00 New York (15:00 Dubai, still the 26th).
        let c = cache(reply(summary("2026-10-26")), success: "2026-10-26T07:13:00+04:00")
        XCTAssertEqual(morningPlan(c, record: nil, now: at("2026-10-25T23:15:00-04:00"), calendar: calendar("America/New_York")),
                       .schedule(at("2026-10-26T07:00:00-04:00"), morningNotice(summary("2026-10-26"))))
    }

    func testInTheReportingZoneALateFirstRunNeverAnnouncesItTomorrow() {
        // 22:30 Dubai: the next 07:00 is the 27th, another reporting day.
        let c = cache(reply(summary("2026-10-26")), success: "2026-10-26T22:29:00+04:00")
        XCTAssertEqual(morningPlan(c, record: nil, now: at("2026-10-26T22:30:00+04:00"), calendar: calendar("Asia/Dubai")), .nothing)
    }

    func testInLondonAnEarlyBriefIsScheduledForSeven() {
        let c = cache(reply(summary("2026-10-26")), success: "2026-10-26T03:13:00+00:00")
        XCTAssertEqual(morningPlan(c, record: nil, now: at("2026-10-26T03:15:00+00:00"), calendar: calendar("Europe/London")),
                       .schedule(at("2026-10-26T07:00:00+00:00"), morningNotice(summary("2026-10-26"))))
    }

    func testANotedValueKeepsItsWordsInTheNotification() {
        let s = GlanceSummary(date: "2026-10-26", madeAt: "2026-10-26T07:12:00+04:00", complete: true, verdict: "Steady.",
                              rows: [GlanceRow(metric: "resting_hr", label: "Resting HR", text: "58 bpm", state: "stand-in",
                                               note: "other device"),
                                     GlanceRow(metric: "sleep_duration", label: "Sleep", text: "3h 30m", state: "in_progress",
                                               note: "so far")],
                              flags: 0, awaiting: [])
        XCTAssertEqual(morningNotice(s).body, "Resting HR 58 bpm (other device), Sleep 3h 30m (so far). Steady.")
        XCTAssertEqual(rowLine(s.rows[0]), "Resting HR 58 bpm (other device)")
    }
}

final class GlanceWhenTests: XCTestCase {
    func testATimeTodayAndAWeekdayOtherwiseInThePhonesZone() {
        let posix = Locale(identifier: "en_US_POSIX")
        let today = glanceWhen(at("2026-10-26T07:12:00+04:00"), now: at("2026-10-26T09:00:00+04:00"),
                               calendar: calendar("Asia/Dubai"), locale: posix)
        XCTAssertTrue(today.contains("7:12"), today)
        XCTAssertFalse(today.hasPrefix("Mon"), today)
        let earlier = glanceWhen(at("2026-10-25T21:40:00+04:00"), now: at("2026-10-26T09:00:00+04:00"),
                                 calendar: calendar("Asia/Dubai"), locale: posix)
        XCTAssertTrue(earlier.hasPrefix("Sun "), earlier)
        // The same instant is "today" in London (17:40 on the 25th) when London's clock is still on the 25th.
        let london = glanceWhen(at("2026-10-25T21:40:00+04:00"), now: at("2026-10-25T20:00:00+00:00"),
                                calendar: calendar("Europe/London"), locale: posix)
        XCTAssertFalse(london.hasPrefix("Sun"), london)
    }
}

final class GlanceStoreFallbackTests: XCTestCase {
    func testAFailedSaveStillReturnsTheMergedReply() throws {
        // The store's folder is a plain file: every save fails.
        let blocker = FileManager.default.temporaryDirectory.appendingPathComponent("glance-blocked-\(UUID().uuidString)")
        try Data("x".utf8).write(to: blocker)
        defer { try? FileManager.default.removeItem(at: blocker) }
        let store = GlanceStore(directory: blocker)
        let r = reply(summary("2026-10-26"))
        let merged = store.merge(GlanceCache(), attemptStart: at("2026-10-26T07:20:00+04:00"), result: .success(r))
        XCTAssertEqual(merged.reply, r)
        XCTAssertEqual(merged.lastSuccess, at("2026-10-26T07:20:00+04:00"))
    }
}
