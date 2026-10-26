import Foundation
import Observation
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

    nonisolated static let morningID = "helios.morning"
    nonisolated static let morningCategory = "helios.morning"
    private static let morningKey = "helios.glance.morning"

    private init() {
        cache = GlanceShared.store()?.load() ?? GlanceCache()
    }

    var configured: Bool {
        !Settings.shared.summaryHost.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var state: GlanceState { glanceState(cache, configured: configured, now: Date()) }

    /// Fetch when due (or forced) and the address is set, then reload the
    /// widget and plan the morning notification. A cancelled fetch (iOS ended
    /// the task) records nothing.
    func refresh(reason: String, force: Bool = false) async {
        guard let store = GlanceShared.store() else { return }
        cache = store.load()        // the widget may have fetched since
        let host = Settings.shared.summaryHost.trimmingCharacters(in: .whitespacesAndNewlines)
        let now = Date()
        guard !host.isEmpty, !inFlight, force || cache.due(now: now) else {
            planMorning()
            return
        }
        inFlight = true
        defer { inFlight = false }
        guard let result = await GlanceClient.fetch(host: host, token: Settings.shared.token) else { return }
        if let next = try? store.update({ $0.merging(attemptStart: now, result: result) }) {
            cache = next
        }
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
        switch morningPlan(cache, record: record, now: Date(), calendar: .current) {
        case .nothing:
            return
        case .postNow(let notice):
            add(notice, at: nil)
            saveRecord(MorningRecord(day: notice.day, at: Date()))
        case .schedule(let date, let notice):
            add(notice, at: date)
            saveRecord(MorningRecord(day: notice.day, at: date))
        }
    }

    private func add(_ notice: GlanceNotice, at date: Date?) {
        let content = UNMutableNotificationContent()
        content.title = notice.title
        content.body = notice.body
        content.sound = nil                 // a silent banner: alarms are the 09:10 Guard's job
        content.interruptionLevel = .active
        content.categoryIdentifier = Self.morningCategory
        let trigger = date.map {
            UNCalendarNotificationTrigger(dateMatching: Calendar.current.dateComponents(
                [.year, .month, .day, .hour, .minute, .second], from: $0), repeats: false)
        }
        // One identifier: a pending morning notification is replaced, never doubled.
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: Self.morningID,
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
