import Foundation
import XCTest
@testable import GlanceCore

// Rank 3: the glance logic the widget, the app and the morning notification
// share. Synthetic values; the reporting zone is Asia/Dubai, the phone's zone
// varies (Manila for the trip east). Since the rank 3 fix (Bridge 1.2 (4),
// 2026-10-27) the brief is due at 10:00, the notification waits for 07:30, the
// widget reloads every 15 minutes from 07:30 until the brief is in hand and
// every 30 minutes after it until 12:00, a rebuilt brief says "Brief updated",
// and the Lock Screen line says "Brief late" and "Mac not reachable" plainly.

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
                   due: String? = "10:00") -> GlanceReply {
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
        {"schema": 1, "served_at": "2026-10-26T07:20:00+04:00", "zone": "Asia/Dubai", "due": "10:00",
         "not_before": "07:30", "revise_until": "12:00",
         "brief": {"today": true, "overdue": false, "check": "ok", "ready_at": "2026-10-26T07:12:00+04:00"},
         "summary": {"date": "2026-10-26", "made_at": "2026-10-26T07:12:00+04:00", "complete": true,
                     "verdict": "Recovery signals lean favorable.", "flags": 0, "awaiting": [],
                     "revised_at": "2026-10-26T08:20:00+04:00",
                     "rows": [{"metric": "recovery_score", "label": "Recovery", "text": "72%", "state": "favorable"}]},
         "added_later": {"x": 1}}
        """
        let r = try JSONDecoder().decode(GlanceReply.self, from: Data(json.utf8))
        XCTAssertEqual(r.zone, "Asia/Dubai")
        XCTAssertEqual(r.brief.readyAt, "2026-10-26T07:12:00+04:00")
        XCTAssertEqual(r.summary?.rows.first?.text, "72%")
        XCTAssertEqual(r.notBefore, "07:30")
        XCTAssertEqual(r.reviseUntil, "12:00")
        XCTAssertEqual(r.summary?.revisedAt, "2026-10-26T08:20:00+04:00")
        // A 1.2 (3) era reply (no floor, no revise_until, no revised_at) still decodes.
        let none = try JSONDecoder().decode(GlanceReply.self, from: Data("""
        {"schema": 1, "served_at": "x", "zone": "Asia/Dubai", "due": "09:00",
         "brief": {"today": false, "overdue": false, "check": "early: x", "ready_at": null}, "summary": null}
        """.utf8))
        XCTAssertNil(none.summary)
        XCTAssertNil(none.notBefore)
        XCTAssertNil(none.reviseUntil)
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
        let old = GlanceReply(schema: 1, servedAt: "2026-10-26T05:00:00+04:00", zone: "Asia/Dubai", due: "10:00",
                              brief: GlanceBrief(today: false, overdue: false, check: "early: ...", readyAt: nil),
                              summary: summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"))
        var c = cache(old, success: "2026-10-26T05:00:00+04:00")
        c = c.merging(attemptStart: at("2026-10-26T06:00:01+04:00"), result: .failure(.link("timeout")))
        let fresh = GlanceReply(schema: 1, servedAt: "2026-10-26T06:00:02+04:00", zone: "Asia/Dubai", due: "10:00",
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
                       .notChecked(since: nil, late: false))
    }

    func testOnlyFailuresAreUnreachableOrUnavailable() {
        let now = at("2026-10-26T07:30:00+04:00")
        XCTAssertEqual(glanceState(cache(nil, success: nil, failure: .link("timeout"), failingSince: "2026-10-26T06:10:00+04:00"),
                                   configured: true, now: now), .unreachable(since: at("2026-10-26T06:10:00+04:00"), late: false))
        XCTAssertEqual(glanceState(cache(nil, success: nil, failure: .http(404), failingSince: "2026-10-26T06:10:00+04:00"),
                                   configured: true, now: now), .unavailable(.http(404), late: false))
    }

    func testTodaysCompleteBriefIsReadyAndALaterOutageIsKept() {
        let r = reply(summary("2026-10-26"))
        let now = at("2026-10-26T10:30:00+04:00")
        XCTAssertEqual(glanceState(cache(r, success: "2026-10-26T07:20:00+04:00"), configured: true, now: now),
                       .ready(readyAt: at("2026-10-26T07:12:00+04:00"), updatedAt: nil, offlineSince: nil, numbers: true))
        let down = cache(r, success: "2026-10-26T07:20:00+04:00", failure: .link("timeout"),
                         failingSince: "2026-10-26T10:00:00+04:00")
        XCTAssertEqual(glanceState(down, configured: true, now: now),
                       .ready(readyAt: at("2026-10-26T07:12:00+04:00"), updatedAt: nil,
                              offlineSince: at("2026-10-26T10:00:00+04:00"), numbers: true))
        let http = cache(r, success: "2026-10-26T07:20:00+04:00", failure: .http(500),
                         failingSince: "2026-10-26T10:00:00+04:00")
        XCTAssertEqual(glanceState(http, configured: true, now: now),
                       .ready(readyAt: at("2026-10-26T07:12:00+04:00"), updatedAt: nil, offlineSince: nil, numbers: true))
    }

    func testTheMacsWordMakesItReadyBeforeItsSummaryExists() {
        // Published before the upgrade: brief.today true, no summary yet (or yesterday's).
        let r = GlanceReply(schema: 1, servedAt: "2026-10-26T07:20:00+04:00", zone: "Asia/Dubai", due: "10:00",
                            brief: GlanceBrief(today: true, overdue: false, check: "ok", readyAt: "2026-10-26T05:00:00+04:00"),
                            summary: summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"))
        let c = cache(r, success: "2026-10-26T07:20:00+04:00")
        let state = glanceState(c, configured: true, now: at("2026-10-26T07:30:00+04:00"))
        XCTAssertEqual(state, .ready(readyAt: at("2026-10-26T05:00:00+04:00"), updatedAt: nil, offlineSince: nil, numbers: false))
        let shown = glanceDisplay(state, cache: c, when: when)
        XCTAssertEqual(shown.headline, "Brief ready Mon 05:00")
        XCTAssertTrue(shown.rows.isEmpty)                  // never "Brief ready" over yesterday's numbers
        XCTAssertFalse(shown.numbersCurrent)
        XCTAssertNil(shown.dataLine)
        // A reply served yesterday says nothing about today.
        let old = GlanceReply(schema: 1, servedAt: "2026-10-25T23:00:00+04:00", zone: "Asia/Dubai", due: "10:00",
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
                       .unreachable(since: at("2026-10-25T23:40:00+04:00"), late: false))
    }

    func testAnOldFailureGivesWayToNotChecked() {
        // The last attempt (an HTTP 500 or no answer) was at 22:00 and nothing ran since.
        let r = reply(summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"), today: false, readyAt: nil)
        for failure in [GlanceFailure.http(500), .link("timeout")] {
            let c = cache(r, success: "2026-10-25T21:00:00+04:00", failure: failure, failingSince: "2026-10-25T22:00:00+04:00")
            XCTAssertEqual(glanceState(c, configured: true, now: at("2026-10-26T08:00:00+04:00")),
                           .notChecked(since: at("2026-10-25T22:00:00+04:00"), late: false), "\(failure)")
        }
    }

    func testNoSuccessfulCheckForThreeHoursIsNotChecked() {
        let r = reply(summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"), today: false, readyAt: nil)
        let c = cache(r, success: "2026-10-26T03:40:00+04:00")
        XCTAssertEqual(glanceState(c, configured: true, now: at("2026-10-26T06:41:00+04:00")),
                       .notChecked(since: at("2026-10-26T03:40:00+04:00"), late: false))
        XCTAssertEqual(glanceState(c, configured: true, now: at("2026-10-26T06:39:00+04:00")), .waiting(whoop: false))
    }

    func testWaitingBeforeTenAndLateAfter() {
        let yesterday = reply(summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"), today: false, readyAt: nil)
        XCTAssertEqual(glanceState(cache(yesterday, success: "2026-10-26T09:30:00+04:00"), configured: true,
                                   now: at("2026-10-26T09:59:00+04:00")), .waiting(whoop: false))
        XCTAssertEqual(glanceState(cache(yesterday, success: "2026-10-26T10:00:00+04:00"), configured: true,
                                   now: at("2026-10-26T10:01:00+04:00")), .late(whoop: false))
        let waiting = reply(summary("2026-10-26", complete: false, madeAt: "2026-10-26T09:30:00+04:00"), today: false, readyAt: nil)
        XCTAssertEqual(glanceState(cache(waiting, success: "2026-10-26T09:31:00+04:00"), configured: true,
                                   now: at("2026-10-26T09:45:00+04:00")), .waiting(whoop: true))
        XCTAssertEqual(glanceState(cache(waiting, success: "2026-10-26T10:05:00+04:00"), configured: true,
                                   now: at("2026-10-26T10:06:00+04:00")), .late(whoop: true))
    }

    func testTheReportingZonesMidnightEndsYesterdaysBriefWhereverThePhoneIs() {
        // Complete for 2026-10-26 (Dubai). At 00:30 Dubai on the 27th (04:30 in Manila) it is no longer today's.
        let r = reply(summary("2026-10-26"))
        let c = cache(r, success: "2026-10-27T00:20:00+04:00")
        XCTAssertEqual(glanceState(c, configured: true, now: at("2026-10-26T23:59:00+04:00")),
                       .ready(readyAt: at("2026-10-26T07:12:00+04:00"), updatedAt: nil, offlineSince: nil, numbers: true))
        XCTAssertEqual(glanceState(c, configured: true, now: at("2026-10-27T04:30:00+08:00")), .waiting(whoop: false))
    }

    func testATripEastChecksAgainstDubaisTenNotThePhones() {
        // 10:05 in Manila is 06:05 in Dubai: before the due time, so waiting, not late.
        let r = reply(summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"), today: false, readyAt: nil)
        XCTAssertEqual(glanceState(cache(r, success: "2026-10-26T10:00:00+08:00"), configured: true,
                                   now: at("2026-10-26T10:05:00+08:00")), .waiting(whoop: false))
        XCTAssertEqual(glanceState(cache(r, success: "2026-10-26T14:00:00+08:00"), configured: true,
                                   now: at("2026-10-26T14:05:00+08:00")), .late(whoop: false))
    }

    func testTheChangePointsAreMidnightTheDueTimeAndTheStaleMark() {
        let r = reply(summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"), today: false, readyAt: nil)
        let points = glanceChangePoints(cache(r, success: "2026-10-26T06:00:00+04:00"), now: at("2026-10-26T06:30:00+04:00"),
                                        phone: calendar("Asia/Dubai"))
        XCTAssertEqual(points, [at("2026-10-26T09:00:01+04:00"), at("2026-10-26T10:00:00+04:00"),
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
                                at("2026-10-27T06:00:00+00:00"), at("2026-10-27T20:00:00+00:00")])
    }
}

// MARK: - What is shown

final class GlanceDisplayTests: XCTestCase {
    func testHeadlinesAndMacLinesForEveryState() {
        let c = cache(reply(summary("2026-10-26")), success: "2026-10-26T07:20:00+04:00")
        let t = at("2026-10-26T06:10:00+04:00")
        let u = at("2026-10-26T08:20:00+04:00")
        let expected: [(GlanceState, String, String?)] = [
            (.notSetUp, "Set up in Helios Bridge", nil),
            (.notChecked(since: nil, late: false), "Not checked yet", nil),
            (.notChecked(since: t, late: false), "Not checked since Mon 06:10", nil),
            (.notChecked(since: t, late: true), "Brief late", "Not checked since Mon 06:10"),
            (.notChecked(since: nil, late: true), "Brief late", "Not checked yet"),
            (.ready(readyAt: t, updatedAt: nil, offlineSince: nil, numbers: true), "Brief ready Mon 06:10", nil),
            (.ready(readyAt: t, updatedAt: u, offlineSince: nil, numbers: true), "Brief updated Mon 08:20", nil),
            (.ready(readyAt: nil, updatedAt: nil, offlineSince: nil, numbers: false), "Brief ready", nil),
            (.ready(readyAt: t, updatedAt: nil, offlineSince: u, numbers: true), "Brief ready Mon 06:10",
             "Mac not reachable since Mon 08:20"),
            (.unreachable(since: t, late: false), "Mac not reachable since Mon 06:10", nil),
            (.unreachable(since: t, late: true), "Brief late", "Mac not reachable since Mon 06:10"),
            (.waiting(whoop: true), "Waiting for Whoop's night", nil),
            (.waiting(whoop: false), "Waiting for this morning's brief", nil),
            (.late(whoop: true), "Brief late: waiting for Whoop", nil),
            (.late(whoop: false), "Brief late", nil),
            (.unavailable(.http(404), late: false), "Mac sent no summary (HTTP 404)", nil),
            (.unavailable(.unreadable, late: false), "Mac sent no summary", nil),
            (.unavailable(.http(500), late: true), "Brief late", "Mac sent no summary (HTTP 500)"),
        ]
        for (state, headline, macLine) in expected {
            let shown = glanceDisplay(state, cache: c, when: when)
            XCTAssertEqual(shown.headline, headline, "\(state)")
            XCTAssertEqual(shown.macLine, macLine, "\(state)")
        }
    }

    func testOnlyAReadyBriefShowsCurrentNumbersAndOldOnesSayWhenTheyAreFrom() {
        let old = cache(reply(summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"), today: false, readyAt: nil),
                        success: "2026-10-25T23:00:00+04:00")
        let shown = glanceDisplay(.unreachable(since: at("2026-10-25T23:40:00+04:00"), late: false), cache: old, when: when)
        XCTAssertFalse(shown.numbersCurrent)
        XCTAssertEqual(shown.dataLine, "from Sun 21:40")
        XCTAssertEqual(shown.rows.count, 4)
        let ready = glanceDisplay(.ready(readyAt: at("2026-10-26T07:12:00+04:00"), updatedAt: nil,
                                         offlineSince: at("2026-10-26T10:00:00+04:00"), numbers: true),
                                  cache: cache(reply(summary("2026-10-26")), success: "2026-10-26T07:20:00+04:00"), when: when)
        XCTAssertTrue(ready.numbersCurrent)
        XCTAssertNil(ready.dataLine)
        XCTAssertEqual(ready.macLine, "Mac not reachable since Mon 10:00")
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

    func testBeforeHalfPastSevenItIsScheduledForThenAfterItPostedAtOnce() {
        let c = cache(reply(summary("2026-10-26", madeAt: "2026-10-26T06:00:00+04:00")), success: "2026-10-26T06:20:00+04:00")
        let notice = morningNotice(summary("2026-10-26", madeAt: "2026-10-26T06:00:00+04:00"))
        XCTAssertEqual(morningPlan(c, record: nil, now: at("2026-10-26T06:30:00+04:00"), calendar: dubai),
                       .schedule(at("2026-10-26T07:30:00+04:00"), notice))
        XCTAssertEqual(morningPlan(c, record: nil, now: at("2026-10-26T07:10:00+04:00"), calendar: dubai),
                       .schedule(at("2026-10-26T07:30:00+04:00"), notice))           // 07:00 is no longer enough
        XCTAssertEqual(morningPlan(c, record: nil, now: at("2026-10-26T07:31:00+04:00"), calendar: dubai), .postNow(notice))
    }

    func testOncePerReportingDayAPendingOneIsReplacedAShownOneNotRepeated() {
        let c = cache(reply(summary("2026-10-26")), success: "2026-10-26T06:20:00+04:00")
        let pending = MorningRecord(day: "2026-10-26", at: at("2026-10-26T07:30:00+04:00"))
        XCTAssertEqual(morningPlan(c, record: pending, now: at("2026-10-26T06:45:00+04:00"), calendar: dubai),
                       .schedule(at("2026-10-26T07:30:00+04:00"), morningNotice(summary("2026-10-26"))))
        XCTAssertEqual(morningPlan(c, record: pending, now: at("2026-10-26T07:45:00+04:00"), calendar: dubai), .nothing)
        let yesterdays = MorningRecord(day: "2026-10-25", at: at("2026-10-25T07:30:00+04:00"))
        XCTAssertEqual(morningPlan(c, record: yesterdays, now: at("2026-10-26T07:45:00+04:00"), calendar: dubai),
                       .postNow(morningNotice(summary("2026-10-26"))))
    }

    func testOnlyTodaysCompleteBrief() {
        let waiting = cache(reply(summary("2026-10-26", complete: false)), success: "2026-10-26T08:00:00+04:00")
        XCTAssertEqual(morningPlan(waiting, record: nil, now: at("2026-10-26T08:00:00+04:00"), calendar: dubai), .nothing)
        let yesterday = cache(reply(summary("2026-10-25")), success: "2026-10-26T08:00:00+04:00")
        XCTAssertEqual(morningPlan(yesterday, record: nil, now: at("2026-10-26T08:00:00+04:00"), calendar: dubai), .nothing)
        XCTAssertEqual(morningPlan(GlanceCache(), record: nil, now: at("2026-10-26T08:00:00+04:00"), calendar: dubai), .nothing)
    }

    func testOnATripEastTheFloorIsThePhonesHalfPastSeven() {
        // Dubai's brief for the 26th completes at 06:00 Dubai, 10:00 in Manila: after 07:30 there, so at once.
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
        // Deterministic: writer A is held inside its locked step (after its read,
        // before its write) while writer B, with a newer attempt, tries to update.
        // With the lock B waits for A and builds on A's write; without it B
        // finishes first and A then writes over it from its stale read.
        let store = GlanceStore(directory: dir)
        let r = reply(summary("2026-10-26"))
        let aInside = DispatchSemaphore(value: 0), release = DispatchSemaphore(value: 0)
        let bDone = DispatchSemaphore(value: 0), aDone = DispatchSemaphore(value: 0)
        let first = NSLock()
        var held = false
        GlanceStore.updateHook = {
            first.lock()
            let mine = !held
            held = true
            first.unlock()
            if mine {
                aInside.signal()
                release.wait()
            }
        }
        defer { GlanceStore.updateHook = nil }
        DispatchQueue.global().async {
            _ = try? store.update { $0.merging(attemptStart: at("2026-10-26T07:00:02+04:00"), result: .success(r)) }
            aDone.signal()
        }
        XCTAssertEqual(aInside.wait(timeout: .now() + 5), .success)
        DispatchQueue.global().async {
            _ = try? store.update { $0.merging(attemptStart: at("2026-10-26T07:00:03+04:00"), result: .failure(.link("timeout"))) }
            bDone.signal()
        }
        let bFinishedWhileAHeldTheLock = bDone.wait(timeout: .now() + 0.5) == .success
        release.signal()
        XCTAssertEqual(aDone.wait(timeout: .now() + 5), .success)
        if !bFinishedWhileAHeldTheLock { XCTAssertEqual(bDone.wait(timeout: .now() + 5), .success) }
        XCTAssertFalse(bFinishedWhileAHeldTheLock, "B got in while A held the lock")
        let final = store.load()
        XCTAssertEqual(final.lastAttempt, at("2026-10-26T07:00:03+04:00"))    // B's newer attempt kept
        XCTAssertEqual(final.lastFailure, .link("timeout"))
        XCTAssertEqual(final.reply, r)                                         // and A's reply too
    }
}

// MARK: - Review fixes (2026-10-26)

final class GlanceRefreshTests: XCTestCase {
    private let yesterdays = reply(summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"), today: false, readyAt: nil)

    func testFifteenMinutesFromHalfPastSevenUntilTheBriefHourlyOtherwise() {
        func next(_ now: String, _ c: GlanceCache) -> Date { glanceNextRefresh(c, configured: true, now: at(now)) }
        let recent = { (t: String) in cache(self.yesterdays, success: t) }
        XCTAssertEqual(next("2026-10-26T01:00:00+04:00", recent("2026-10-26T00:50:00+04:00")), at("2026-10-26T02:00:00+04:00"))
        XCTAssertEqual(next("2026-10-26T04:30:00+04:00", recent("2026-10-26T04:20:00+04:00")), at("2026-10-26T05:30:00+04:00"))
        XCTAssertEqual(next("2026-10-26T06:00:00+04:00", recent("2026-10-26T05:50:00+04:00")), at("2026-10-26T07:00:00+04:00"))
        XCTAssertEqual(next("2026-10-26T06:45:00+04:00", recent("2026-10-26T06:40:00+04:00")), at("2026-10-26T07:30:00+04:00"))
        XCTAssertEqual(next("2026-10-26T07:30:00+04:00", recent("2026-10-26T07:20:00+04:00")), at("2026-10-26T07:45:00+04:00"))
        XCTAssertEqual(next("2026-10-26T10:00:00+04:00", recent("2026-10-26T09:50:00+04:00")), at("2026-10-26T10:15:00+04:00"))
        XCTAssertEqual(next("2026-10-26T12:00:00+04:00", recent("2026-10-26T11:50:00+04:00")), at("2026-10-26T13:00:00+04:00"))
        let down = cache(yesterdays, success: "2026-10-26T20:00:00+04:00", failure: .link("timeout"),
                         failingSince: "2026-10-26T22:00:00+04:00", lastAttempt: "2026-10-26T23:20:00+04:00")
        XCTAssertEqual(next("2026-10-26T23:30:00+04:00", down), at("2026-10-27T00:30:00+04:00"))
    }

    func testReadyNotSetUpAndNeverFetched() {
        let ready = cache(reply(summary("2026-10-26")), success: "2026-10-26T07:20:00+04:00")
        // Ready before 12:00: every 30 minutes, for a rebuild; hourly after.
        XCTAssertEqual(glanceNextRefresh(ready, configured: true, now: at("2026-10-26T07:30:00+04:00")),
                       at("2026-10-26T08:00:00+04:00"))
        XCTAssertEqual(glanceNextRefresh(ready, configured: true, now: at("2026-10-26T12:10:00+04:00")),
                       at("2026-10-26T13:10:00+04:00"))
        XCTAssertEqual(glanceNextRefresh(ready, configured: false, now: at("2026-10-26T07:30:00+04:00")),
                       at("2026-10-26T08:30:00+04:00"))
        XCTAssertEqual(glanceNextRefresh(GlanceCache(), configured: true, now: at("2026-10-26T07:30:00+04:00")),
                       at("2026-10-26T08:30:00+04:00"))                // no reply yet: hourly, around the clock
        let failing = cache(nil, success: nil, failure: .link("timeout"), failingSince: "2026-10-26T07:00:00+04:00")
        XCTAssertEqual(glanceNextRefresh(failing, configured: true, now: at("2026-10-26T07:30:00+04:00")),
                       at("2026-10-26T08:30:00+04:00"))
    }
}

final class GlanceSurfaceTests: XCTestCase {
    func testTheLockScreenLineIsTheStatusLineAndTheMacLineAndNoNumber() {
        let c = cache(reply(summary("2026-10-26")), success: "2026-10-26T07:20:00+04:00")
        let states: [GlanceState] = [
            .ready(readyAt: at("2026-10-26T07:12:00+04:00"), updatedAt: nil, offlineSince: at("2026-10-26T10:00:00+04:00"),
                   numbers: true),
            .ready(readyAt: at("2026-10-26T07:12:00+04:00"), updatedAt: at("2026-10-26T08:20:00+04:00"), offlineSince: nil,
                   numbers: true),
            .unreachable(since: at("2026-10-26T06:10:00+04:00"), late: false),
            .unreachable(since: at("2026-10-26T06:10:00+04:00"), late: true),
            .notChecked(since: at("2026-10-26T03:40:00+04:00"), late: false),
            .notChecked(since: at("2026-10-26T03:40:00+04:00"), late: true),
            .waiting(whoop: true), .late(whoop: false), .unavailable(.http(500), late: false),
            .unavailable(.http(500), late: true), .notSetUp]
        for state in states {
            let display = glanceDisplay(state, cache: c, when: when)
            let line = glanceLockLine(display)
            XCTAssertEqual(line, display.macLine.map { display.headline + "\n" + $0 } ?? display.headline)
            for row in rows { XCTAssertFalse(line.contains(row.text), "\(state): \(line)") }
            XCTAssertNil(line.rangeOfCharacter(from: CharacterSet(charactersIn: "%")), line)
        }
    }

    func testTheLockScreenNeverShowsANumberTheHomeScreenOnlyWithoutPrivacy() {
        XCTAssertFalse(surfaceShowsNumbers(.lockScreen, privacyRedacted: false))
        XCTAssertFalse(surfaceShowsNumbers(.lockScreen, privacyRedacted: true))
        XCTAssertTrue(surfaceShowsNumbers(.homeScreen, privacyRedacted: false))
        XCTAssertFalse(surfaceShowsNumbers(.homeScreen, privacyRedacted: true))
    }
}

final class MorningPlanReviewTests: XCTestCase {
    func testFarWestTheEveningBriefWaitsForTheNextMorningOfTheSameReportingDay() {
        // 07:12 Dubai on the 26th is 23:12 in New York on the 25th: scheduled for 07:30 New York (15:30 Dubai, still the 26th).
        let c = cache(reply(summary("2026-10-26")), success: "2026-10-26T07:13:00+04:00")
        XCTAssertEqual(morningPlan(c, record: nil, now: at("2026-10-25T23:15:00-04:00"), calendar: calendar("America/New_York")),
                       .schedule(at("2026-10-26T07:30:00-04:00"), morningNotice(summary("2026-10-26"))))
    }

    func testInTheReportingZoneALateFirstRunNeverAnnouncesItTomorrow() {
        // 22:30 Dubai: the next 07:30 is the 27th, another reporting day.
        let c = cache(reply(summary("2026-10-26")), success: "2026-10-26T22:29:00+04:00")
        XCTAssertEqual(morningPlan(c, record: nil, now: at("2026-10-26T22:30:00+04:00"), calendar: calendar("Asia/Dubai")), .nothing)
    }

    func testInLondonAnEarlyBriefIsScheduledForHalfPastSeven() {
        let c = cache(reply(summary("2026-10-26")), success: "2026-10-26T03:13:00+00:00")
        XCTAssertEqual(morningPlan(c, record: nil, now: at("2026-10-26T03:15:00+00:00"), calendar: calendar("Europe/London")),
                       .schedule(at("2026-10-26T07:30:00+00:00"), morningNotice(summary("2026-10-26"))))
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

// MARK: - The rank 3 fix (Bridge 1.2 (4), 2026-10-27)

private func timedReply(_ s: GlanceSummary?, today: Bool = true, due: String? = "10:00",
                        notBefore: String? = "07:30", reviseUntil: String? = "12:00") -> GlanceReply {
    GlanceReply(schema: 1, servedAt: "2026-10-26T07:40:00+04:00", zone: "Asia/Dubai", due: due,
                brief: GlanceBrief(today: today, overdue: false, check: today ? "ok" : "early: ...",
                                   readyAt: today ? "2026-10-26T07:30:00+04:00" : nil),
                summary: s, notBefore: notBefore, reviseUntil: reviseUntil)
}

private func revised(_ date: String, at revisedAt: String?) -> GlanceSummary {
    GlanceSummary(date: date, madeAt: revisedAt ?? "2026-10-26T07:30:00+04:00", complete: true,
                  verdict: "Recovery signals lean favorable.", rows: rows, flags: 1, awaiting: [], revisedAt: revisedAt)
}

final class GlanceFixTests: XCTestCase {
    private let dubai = calendar("Asia/Dubai")

    // P1: the notification follows the publish: the reply's floor, 07:30 when it carries none.
    func testTheNotificationWaitsForTheRepliesFloorOrHalfPastSeven() {
        let s = summary("2026-10-26", madeAt: "2026-10-26T07:30:00+04:00")
        let eight = cache(timedReply(s, notBefore: "08:00"), success: "2026-10-26T07:40:00+04:00")
        XCTAssertEqual(morningPlan(eight, record: nil, now: at("2026-10-26T07:45:00+04:00"), calendar: dubai),
                       .schedule(at("2026-10-26T08:00:00+04:00"), morningNotice(s)))
        XCTAssertEqual(morningPlan(eight, record: nil, now: at("2026-10-26T08:00:00+04:00"), calendar: dubai),
                       .postNow(morningNotice(s)))
        let none = cache(timedReply(s, notBefore: nil), success: "2026-10-26T07:05:00+04:00")
        XCTAssertEqual(morningPlan(none, record: nil, now: at("2026-10-26T07:10:00+04:00"), calendar: dubai),
                       .schedule(at("2026-10-26T07:30:00+04:00"), morningNotice(s)))
        let broken = cache(timedReply(s, notBefore: "soon"), success: "2026-10-26T07:05:00+04:00")
        XCTAssertEqual(morningPlan(broken, record: nil, now: at("2026-10-26T07:10:00+04:00"), calendar: dubai),
                       .schedule(at("2026-10-26T07:30:00+04:00"), morningNotice(s)))
        XCTAssertEqual(GlanceTiming.notBefore, "07:30")
    }

    // P2: reloads every 15 minutes from the floor until the brief is in hand, then
    // every 30 minutes until revise_until, hourly otherwise (times from the reply).
    func testTheReloadsFollowTheRepliesTimes() {
        func next(_ c: GlanceCache, _ now: String) -> Date { glanceNextRefresh(c, configured: true, now: at(now)) }
        let waiting = cache(timedReply(summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"), today: false,
                                       notBefore: "08:00", reviseUntil: "11:00"), success: "2026-10-26T07:40:00+04:00")
        XCTAssertEqual(next(waiting, "2026-10-26T07:40:00+04:00"), at("2026-10-26T08:00:00+04:00"))
        XCTAssertEqual(next(waiting, "2026-10-26T08:00:00+04:00"), at("2026-10-26T08:15:00+04:00"))
        XCTAssertEqual(next(waiting, "2026-10-26T10:50:00+04:00"), at("2026-10-26T11:05:00+04:00"))
        XCTAssertEqual(next(waiting, "2026-10-26T11:00:00+04:00"), at("2026-10-26T12:00:00+04:00"))
        let ready = cache(timedReply(revised("2026-10-26", at: nil), notBefore: "08:00", reviseUntil: "11:00"),
                          success: "2026-10-26T08:05:00+04:00")
        XCTAssertEqual(next(ready, "2026-10-26T08:10:00+04:00"), at("2026-10-26T08:40:00+04:00"))
        XCTAssertEqual(next(ready, "2026-10-26T11:00:00+04:00"), at("2026-10-26T12:00:00+04:00"))
        // Late evening: hourly, and never past the next morning's floor.
        XCTAssertEqual(next(ready, "2026-10-26T23:30:00+04:00"), at("2026-10-27T00:30:00+04:00"))
        XCTAssertEqual(next(waiting, "2026-10-27T07:30:00+04:00"), at("2026-10-27T08:00:00+04:00"))
    }

    // P3: a rebuilt brief says when it was updated, on the Lock Screen too, with no number.
    func testARebuiltBriefSaysBriefUpdated() {
        let c = cache(timedReply(revised("2026-10-26", at: "2026-10-26T08:20:00+04:00")), success: "2026-10-26T08:25:00+04:00")
        let state = glanceState(c, configured: true, now: at("2026-10-26T08:30:00+04:00"))
        XCTAssertEqual(state, .ready(readyAt: at("2026-10-26T07:30:00+04:00"), updatedAt: at("2026-10-26T08:20:00+04:00"),
                                     offlineSince: nil, numbers: true))
        let shown = glanceDisplay(state, cache: c, when: when)
        XCTAssertEqual(shown.headline, "Brief updated Mon 08:20")
        XCTAssertEqual(glanceLockLine(shown), "Brief updated Mon 08:20")
        XCTAssertTrue(shown.numbersCurrent)
        // A revision of another day's summary is not today's news.
        let old = cache(timedReply(revised("2026-10-25", at: "2026-10-25T08:20:00+04:00"), today: false),
                        success: "2026-10-26T06:00:00+04:00")
        XCTAssertEqual(glanceState(old, configured: true, now: at("2026-10-26T06:10:00+04:00")), .waiting(whoop: false))
    }

    // P4: a rebuild after the notification was shown replaces it quietly, once per revision.
    func testARevisionAfterTheNoticeWasShownReplacesItOncePerRevision() {
        let first = revised("2026-10-26", at: "2026-10-26T08:20:00+04:00")
        let c = cache(timedReply(first), success: "2026-10-26T08:25:00+04:00")
        let shown = MorningRecord(day: "2026-10-26", at: at("2026-10-26T07:30:00+04:00"))
        XCTAssertEqual(morningPlan(c, record: shown, now: at("2026-10-26T08:25:00+04:00"), calendar: dubai),
                       .replaceShown(updatedNotice(first), removing: ["helios.morning"]))
        let n = updatedNotice(first)
        XCTAssertEqual(n.title, "Helios: brief updated")
        XCTAssertNil(n.title.rangeOfCharacter(from: .decimalDigits))
        XCTAssertEqual(n.body, morningNotice(first).body)
        XCTAssertEqual(n.revision, "2026-10-26T08:20:00+04:00")
        let replaced = MorningRecord(day: "2026-10-26", at: at("2026-10-26T08:25:00+04:00"), revision: n.revision)
        XCTAssertEqual(morningPlan(c, record: replaced, now: at("2026-10-26T08:55:00+04:00"), calendar: dubai), .nothing)
        let second = revised("2026-10-26", at: "2026-10-26T09:10:00+04:00")
        let again = cache(timedReply(second), success: "2026-10-26T09:15:00+04:00")
        XCTAssertEqual(morningPlan(again, record: replaced, now: at("2026-10-26T09:15:00+04:00"), calendar: dubai),
                       .replaceShown(updatedNotice(second),
                                     removing: ["helios.morning", "helios.morning.update.2026-10-26T08:20:00+04:00"]))
        // Still pending: the pending one takes the new content and keeps its time and its title.
        let pending = MorningRecord(day: "2026-10-26", at: at("2026-10-26T07:30:00+04:00"))
        XCTAssertEqual(morningPlan(c, record: pending, now: at("2026-10-26T07:20:00+04:00"), calendar: dubai),
                       .schedule(at("2026-10-26T07:30:00+04:00"), morningNotice(first)))
        // A first notice that already carries a revision is not followed by an update for it.
        let firstSeen = MorningRecord(day: "2026-10-26", at: at("2026-10-26T08:25:00+04:00"),
                                      revision: morningNotice(first).revision)
        XCTAssertEqual(morningPlan(c, record: firstSeen, now: at("2026-10-26T08:40:00+04:00"), calendar: dubai), .nothing)
    }

    // P5: from 10:00 without today's complete brief the Lock Screen says "Brief late",
    // whatever the Mac's state; "Mac not reachable" whenever the last try found no Mac.
    func testTheLockScreenSaysBriefLateAndMacNotReachablePlainly() {
        let yesterday = timedReply(summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"), today: false)
        func line(_ c: GlanceCache, _ now: String) -> String {
            let n = at(now)
            return glanceLockLine(glanceDisplay(glanceState(c, configured: true, now: n), cache: c, when: when))
        }
        let reachable = cache(yesterday, success: "2026-10-26T10:00:00+04:00")
        XCTAssertEqual(line(reachable, "2026-10-26T10:05:00+04:00"), "Brief late")
        let down = cache(yesterday, success: "2026-10-26T09:00:00+04:00", failure: .link("timeout"),
                         failingSince: "2026-10-26T09:40:00+04:00", lastAttempt: "2026-10-26T10:00:00+04:00")
        XCTAssertEqual(line(down, "2026-10-26T09:59:00+04:00"), "Mac not reachable since Mon 09:40")
        XCTAssertEqual(line(down, "2026-10-26T10:05:00+04:00"), "Brief late\nMac not reachable since Mon 09:40")
        let stale = cache(yesterday, success: "2026-10-26T06:00:00+04:00")
        XCTAssertEqual(line(stale, "2026-10-26T09:30:00+04:00"), "Not checked since Mon 06:00")
        XCTAssertEqual(line(stale, "2026-10-26T10:05:00+04:00"), "Brief late\nNot checked since Mon 06:00")
        let refused = cache(yesterday, success: "2026-10-26T09:00:00+04:00", failure: .http(500),
                            failingSince: "2026-10-26T10:00:00+04:00")
        XCTAssertEqual(line(refused, "2026-10-26T10:05:00+04:00"), "Brief late\nMac sent no summary (HTTP 500)")
        let never = GlanceCache()
        XCTAssertEqual(glanceState(never, configured: true, now: at("2026-10-26T10:05:00+04:00")),
                       .notChecked(since: nil, late: true))
        let ready = cache(timedReply(revised("2026-10-26", at: nil)), success: "2026-10-26T07:40:00+04:00",
                          failure: .link("cannot connect"), failingSince: "2026-10-26T10:05:00+04:00",
                          lastAttempt: "2026-10-26T10:05:00+04:00")
        XCTAssertEqual(line(ready, "2026-10-26T10:10:00+04:00"), "Brief ready Mon 07:30\nMac not reachable since Mon 10:05")
        for c in [reachable, down, stale, refused, ready] {
            let l = line(c, "2026-10-26T10:10:00+04:00")
            for row in rows { XCTAssertFalse(l.contains(row.text), l) }
        }
    }

    // P6: a reply with no due time is due at 10:00 in its zone.
    func testTheDueTimeFallsBackToTen() {
        XCTAssertEqual(GlanceClock.due(on: at("2026-10-26T06:00:00+04:00"), in: TimeZone(identifier: "Asia/Dubai")!, hhmm: nil),
                       at("2026-10-26T10:00:00+04:00"))
        let r = timedReply(summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"), today: false, due: nil)
        XCTAssertEqual(glanceState(cache(r, success: "2026-10-26T09:50:00+04:00"), configured: true,
                                   now: at("2026-10-26T09:59:00+04:00")), .waiting(whoop: false))
        XCTAssertEqual(glanceState(cache(r, success: "2026-10-26T10:00:00+04:00"), configured: true,
                                   now: at("2026-10-26T10:00:00+04:00")), .late(whoop: false))
    }

    // An old record saved by 1.2 (3) (no revision) still reads.
    func testARecordSavedBeforeTheFixStillDecodes() throws {
        let data = Data(#"{"day": "2026-10-26", "at": 1793000000}"#.utf8)
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .secondsSince1970
        let r = try decoder.decode(MorningRecord.self, from: data)
        XCTAssertEqual(r.day, "2026-10-26")
        XCTAssertNil(r.revision)
    }
}


// MARK: - Review fixes (2026-10-27)

final class GlanceFixReviewTests: XCTestCase {
    private let dubai = calendar("Asia/Dubai")

    // M1: from 10:00 the Home Screen widget keeps the Mac's line beside old numbers, and
    // its privacy view says what the Lock Screen says.
    func testTheHomeScreenKeepsTheMacsLineBesideOldNumbers() {
        let old = cache(reply(summary("2026-10-25", madeAt: "2026-10-25T21:40:00+04:00"), today: false, readyAt: nil),
                        success: "2026-10-26T09:00:00+04:00", failure: .link("timeout"),
                        failingSince: "2026-10-26T09:40:00+04:00", lastAttempt: "2026-10-26T10:00:00+04:00")
        let shown = glanceDisplay(glanceState(old, configured: true, now: at("2026-10-26T10:05:00+04:00")), cache: old, when: when)
        XCTAssertEqual(shown.headline, "Brief late")
        XCTAssertEqual(glanceFooterLines(shown), ["Mac not reachable since Mon 09:40", "from Sun 21:40"])
        XCTAssertEqual(glanceLockLine(shown), "Brief late\nMac not reachable since Mon 09:40")
        let ready = glanceDisplay(.ready(readyAt: at("2026-10-26T07:30:00+04:00"), updatedAt: nil,
                                         offlineSince: at("2026-10-26T10:05:00+04:00"), numbers: true),
                                  cache: cache(reply(summary("2026-10-26")), success: "2026-10-26T07:40:00+04:00"), when: when)
        XCTAssertEqual(glanceFooterLines(ready), ["Mac not reachable since Mon 10:05"])
        let waiting = glanceDisplay(.waiting(whoop: false), cache: old, when: when)
        XCTAssertEqual(glanceFooterLines(waiting), ["from Sun 21:40"])
        XCTAssertEqual(glanceFooterLines(glanceDisplay(.notSetUp, cache: old, when: when)), [])
    }

    // L5: a rebuild that leaves today's summary incomplete withdraws the notification
    // (shown or pending) once per revision, instead of leaving old numbers on screen.
    func testANoticeIsWithdrawnWhenTodaysSummaryIsNoLongerComplete() {
        let incomplete = GlanceSummary(date: "2026-10-26", madeAt: "2026-10-26T08:20:00+04:00", complete: false,
                                       verdict: "Waiting for last night's Whoop data.", rows: rows, flags: 0,
                                       awaiting: ["recovery_score"], revisedAt: "2026-10-26T08:20:00+04:00")
        let c = cache(timedReply(incomplete), success: "2026-10-26T08:25:00+04:00")
        let shown = MorningRecord(day: "2026-10-26", at: at("2026-10-26T07:30:00+04:00"))
        XCTAssertEqual(morningPlan(c, record: shown, now: at("2026-10-26T08:25:00+04:00"), calendar: dubai),
                       .withdraw(removing: ["helios.morning"], revision: "2026-10-26T08:20:00+04:00"))
        let withdrawn = MorningRecord(day: "2026-10-26", at: shown.at, revision: "2026-10-26T08:20:00+04:00")
        XCTAssertEqual(morningPlan(c, record: withdrawn, now: at("2026-10-26T08:55:00+04:00"), calendar: dubai), .nothing)
        let pending = MorningRecord(day: "2026-10-26", at: at("2026-10-26T09:00:00+04:00"))
        XCTAssertEqual(morningPlan(c, record: pending, now: at("2026-10-26T08:25:00+04:00"), calendar: dubai),
                       .withdraw(removing: ["helios.morning"], revision: "2026-10-26T08:20:00+04:00"))
        // Complete again later: the quiet update, never under an identifier it removes.
        let back = revised("2026-10-26", at: "2026-10-26T09:10:00+04:00")
        let again = cache(timedReply(back), success: "2026-10-26T09:15:00+04:00")
        XCTAssertEqual(morningPlan(again, record: withdrawn, now: at("2026-10-26T09:15:00+04:00"), calendar: dubai),
                       .replaceShown(updatedNotice(back),
                                     removing: ["helios.morning", "helios.morning.update.2026-10-26T08:20:00+04:00"]))
        // No notice yet today and an incomplete summary: nothing, as before.
        XCTAssertEqual(morningPlan(c, record: nil, now: at("2026-10-26T08:25:00+04:00"), calendar: dubai), .nothing)
    }

    // L6 (i): the update is posted under its own identifier per revision, so the
    // asynchronous removal of the older ones can never remove it.
    func testAnUpdateIsNeverPostedUnderAnIdentifierItRemoves() {
        XCTAssertEqual(morningNotice(summary("2026-10-26")).id, "helios.morning")
        XCTAssertEqual(MorningIDs.morning, "helios.morning")
        var record = MorningRecord(day: "2026-10-26", at: at("2026-10-26T07:30:00+04:00"))
        for revisedAt in ["2026-10-26T08:20:00+04:00", "2026-10-26T09:10:00+04:00", "2026-10-26T11:45:00+04:00"] {
            let c = cache(timedReply(revised("2026-10-26", at: revisedAt)), success: revisedAt)
            guard case .replaceShown(let notice, let removing) = morningPlan(c, record: record, now: at(revisedAt),
                                                                           calendar: dubai) else {
                return XCTFail("no update for \(revisedAt)")
            }
            XCTAssertEqual(notice.id, MorningIDs.update(revisedAt))
            XCTAssertFalse(removing.contains(notice.id), revisedAt)
            XCTAssertTrue(removing.contains(MorningIDs.morning))
            record = MorningRecord(day: "2026-10-26", at: at(revisedAt), revision: notice.revision)
        }
    }
}
