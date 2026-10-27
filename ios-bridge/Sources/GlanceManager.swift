import Foundation
import Observation
import UIKit
import UserNotifications
import WidgetKit
import GlanceCore

// MARK: - Glance (rank 3)
// Fetches the summary heliosd caches (GET /api/glance, from the "Summary from"
// address: the Mac itself, not the relay) into the App Group the widget reads,
// reloads the widget, and posts the one morning notification. Runs when the app
// opens, at unlock, and in the refresh and processing tasks, beside the
// HealthKit work, never inside an observer wake (that wake must finish inside
// 20 s). The widget fetches on its own too; the cache merge keeps the newest.
@MainActor
@Observable
final class GlanceManager {
    static let shared = GlanceManager()

    private(set) var cache = GlanceCache()
    @ObservationIgnored private var inFlight = false
    @ObservationIgnored private let defaults = UserDefaults.standard

    nonisolated static let morningID = MorningIDs.morning
    nonisolated static let morningCategory = "helios.morning"
    private static let morningKey = "helios.glance.morning"

    private init() {
        cache = GlanceShared.store()?.load() ?? GlanceCache()
    }

    /// A usable Summary from address (an https name and port).
    var configured: Bool { GlanceClient.isValidAddress(Settings.shared.summaryHost) }

    var state: GlanceState { glanceState(cache, configured: configured, now: Date()) }

    /// Fetch when due (or forced) and the address is set, then reload the
    /// widget and plan the morning notification. The fetch runs inside its own
    /// background time; when iOS ends it, the fetch is cancelled, and a
    /// cancelled fetch records nothing (a request frozen by a suspension must
    /// never come back as "Mac not reachable").
    func refresh(reason: String, force: Bool = false) async {
        guard let store = GlanceShared.store() else { return }
        cache = store.load()        // the widget may have fetched since
        let host = Settings.shared.summaryHost.trimmingCharacters(in: .whitespacesAndNewlines)
        let token = Settings.shared.token
        let now = Date()
        guard GlanceClient.isValidAddress(host), !inFlight, force || cache.due(now: now) else {
            planMorning()
            return
        }
        inFlight = true
        defer { inFlight = false }
        let fetch = Task { await GlanceClient.fetch(host: host, token: token) }
        let time = GlanceBackgroundTime.begin { fetch.cancel() }
        // Ended after the cache write (it holds a lock in the shared
        // container), the widget reload and the morning plan.
        defer { time.end() }
        let result = await withTaskCancellationHandler {
            await fetch.value
        } onCancel: {
            fetch.cancel()          // the refresh or processing task ended
        }
        guard let result else { return }
        cache = store.merge(cache, attemptStart: now, result: result)
        WidgetCenter.shared.reloadAllTimelines()
        planMorning()
    }

    // MARK: The morning notification

    /// The category hides the body (the numbers) wherever iOS hides previews,
    /// keeping the title, which carries no number.
    nonisolated static func registerCategory() {
        let category = UNNotificationCategory(identifier: morningCategory, actions: [], intentIdentifiers: [],
                                              hiddenPreviewsBodyPlaceholder: "Open Helios Bridge for the numbers",
                                              options: [.hiddenPreviewsShowTitle])
        UNUserNotificationCenter.current().setNotificationCategories([category])
    }

    private func planMorning() {
        let record = loadRecord()
        let center = UNUserNotificationCenter.current()
        switch morningPlan(cache, record: record, now: Date(), calendar: .current) {
        case .nothing:
            return
        case .postNow(let notice):
            removeEarlierUpdate(record, before: notice)
            add(notice, at: nil)
            saveRecord(MorningRecord(day: notice.day, at: Date(), revision: notice.revision))
        case .schedule(let date, let notice):
            removeEarlierUpdate(record, before: notice)
            add(notice, at: date)
            saveRecord(MorningRecord(day: notice.day, at: date, revision: notice.revision))
        case .replaceShown(let notice, let removing):
            // heliosd rebuilt the brief after the morning one was shown (owner
            // answer (a), 2026-10-27): take the old numbers away and post the
            // new ones quietly, under an identifier the removal never names.
            // Apple: passive "adds the notification to the notification list
            // without lighting up the screen or playing a sound".
            center.removeDeliveredNotifications(withIdentifiers: removing)
            add(notice, at: nil, passive: true)
            saveRecord(MorningRecord(day: notice.day, at: Date(), revision: notice.revision))
        case .withdraw(let removing, let revision):
            // Today's summary is no longer complete: no numbers heliosd no
            // longer stands behind stay on screen or in the queue.
            center.removePendingNotificationRequests(withIdentifiers: removing)
            center.removeDeliveredNotifications(withIdentifiers: removing)
            if let record { saveRecord(MorningRecord(day: record.day, at: record.at, revision: revision)) }
        }
    }

    /// A quiet update left from an earlier day goes when the next day's
    /// notification comes (its identifier differs from the new one's).
    private func removeEarlierUpdate(_ record: MorningRecord?, before notice: GlanceNotice) {
        guard let record, record.day != notice.day, let revision = record.revision else { return }
        UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: [MorningIDs.update(revision)])
    }

    private func add(_ notice: GlanceNotice, at date: Date?, passive: Bool = false) {
        let content = UNMutableNotificationContent()
        content.title = notice.title
        content.body = notice.body
        content.sound = nil                 // a silent banner (the 09:10 alarm shortcut is retired)
        content.interruptionLevel = passive ? .passive : .active
        content.categoryIdentifier = Self.morningCategory
        let trigger = date.map {
            UNCalendarNotificationTrigger(dateMatching: Calendar.current.dateComponents(
                [.year, .month, .day, .hour, .minute, .second], from: $0), repeats: false)
        }
        // The morning one keeps one identifier: a pending one is replaced, never
        // doubled. A quiet update has its own (GlanceCore MorningIDs).
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: notice.id,
                                                                     content: content, trigger: trigger))
    }

    private func loadRecord() -> MorningRecord? {
        guard let data = defaults.data(forKey: Self.morningKey) else { return nil }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .secondsSince1970
        return try? decoder.decode(MorningRecord.self, from: data)
    }

    private func saveRecord(_ record: MorningRecord) {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .secondsSince1970
        if let data = try? encoder.encode(record) { defaults.set(data, forKey: Self.morningKey) }
    }
}

/// Extra seconds from iOS while one glance fetch finishes. Ended exactly once:
/// by the fetch, or by iOS's expiry, which first cancels the fetch.
@MainActor
final class GlanceBackgroundTime {
    private var id = UIBackgroundTaskIdentifier.invalid

    static func begin(onExpiry: @escaping @Sendable () -> Void) -> GlanceBackgroundTime {
        let time = GlanceBackgroundTime()
        time.id = UIApplication.shared.beginBackgroundTask(withName: "helios.glance") {
            onExpiry()
            MainActor.assumeIsolated { time.end() }
        }
        return time
    }

    func end() {
        guard id != .invalid else { return }
        UIApplication.shared.endBackgroundTask(id)
        id = .invalid
    }
}
