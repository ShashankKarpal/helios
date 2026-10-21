import SwiftUI
import BackgroundTasks
import UserNotifications

// MARK: - App entry
@main
struct HeliosBridgeApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            StatusView()
                .onOpenURL { url in handle(url) }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                Task { await HealthKitManager.shared.runForeground(reason: "foreground", force: false) }
                BGTaskManager.shared.scheduleRefresh()
                BGTaskManager.shared.scheduleProcessing()
            }
        }
    }

    // helios-bridge://sync triggers an immediate sync. The PWA "Pull latest"
    // button opens this deep link.
    private func handle(_ url: URL) {
        guard url.scheme == "helios-bridge" else { return }
        if url.host == "sync" {
            Task { await HealthKitManager.shared.runForeground(reason: "deepLink", force: true) }
        }
    }
}

// MARK: - App delegate
// BGTaskScheduler registration MUST happen before application launch finishes,
// which is why it lives here rather than in a SwiftUI lifecycle hook.
final class AppDelegate: NSObject, UIApplicationDelegate {
    private var unlockObserver: NSObjectProtocol?

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        BGTaskManager.shared.register()
        NotificationManager.shared.requestAuthorization()
        Task { @MainActor in HealthKitManager.shared.bootstrap() }
        // Health data becomes readable at unlock: everything that waited for it
        // (observer wakes while locked, the overnight Watch samples) goes now.
        unlockObserver = NotificationCenter.default.addObserver(
            forName: UIApplication.protectedDataDidBecomeAvailableNotification, object: nil, queue: .main) { _ in
            Task { @MainActor in
                let time = BackgroundTime.begin("helios.unlock")
                _ = await HealthKitManager.shared.runBackground(reason: "unlock", budget: 25)
                time.end()
            }
        }
        return true
    }
}

// MARK: - Background tasks
// A refresh task (short, frequent) and a processing task (long, when iOS
// chooses: usually idle and on power). Both are rescheduled first, so the
// chain continues even when a run is cut short. What iOS actually grants is in
// the wake log on the status screen.
final class BGTaskManager {
    static let shared = BGTaskManager()

    let refreshID = "com.shanky.helios.bridge.refresh"
    let processingID = "com.shanky.helios.bridge.processing"

    func register() {
        BGTaskScheduler.shared.register(forTaskWithIdentifier: refreshID, using: nil) { task in
            guard let task = task as? BGAppRefreshTask else {
                task.setTaskCompleted(success: false)
                return
            }
            self.scheduleRefresh()
            let work = Task { @MainActor in
                _ = await HealthKitManager.shared.runBackground(reason: "refresh", budget: 25)
                task.setTaskCompleted(success: true)
            }
            task.expirationHandler = {
                work.cancel()
                Task { await HealthKitManager.shared.interrupt() }
                task.setTaskCompleted(success: false)
            }
        }
        BGTaskScheduler.shared.register(forTaskWithIdentifier: processingID, using: nil) { task in
            guard let task = task as? BGProcessingTask else {
                task.setTaskCompleted(success: false)
                return
            }
            self.scheduleProcessing()
            let work = Task { @MainActor in
                await HealthKitManager.shared.runProcessing()
                task.setTaskCompleted(success: true)
            }
            task.expirationHandler = {
                work.cancel()
                Task { await HealthKitManager.shared.interrupt() }
                task.setTaskCompleted(success: false)
            }
        }
    }

    func scheduleRefresh() {
        let request = BGAppRefreshTaskRequest(identifier: refreshID)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        try? BGTaskScheduler.shared.submit(request)
    }

    func scheduleProcessing() {
        let request = BGProcessingTaskRequest(identifier: processingID)
        request.requiresNetworkConnectivity = true
        request.requiresExternalPower = false
        request.earliestBeginDate = Date(timeIntervalSinceNow: 30 * 60)
        try? BGTaskScheduler.shared.submit(request)
    }
}

// MARK: - Local notifications
// Coalesced alerting with persisted state. The old version fired "cannot reach
// the Mac" on every background wake (its failure counter was in-memory, so each
// process relaunch re-armed it) and "stream quiet" on every sweep, producing a
// notification every 15 to 20 minutes whenever the Mac slept. This version keeps
// its state in UserDefaults so it survives relaunches, and notifies only on
// state CHANGES: once when the Mac becomes unreachable, escalations at 6h and
// 24h, one recovery note, and at most one quiet-stream alert per 24h. Resolved
// alerts are removed from Notification Center so stale banners never linger.
final class NotificationManager {
    static let shared = NotificationManager()

    private let defaults = UserDefaults.standard
    private enum Key {
        static let outageStart = "helios.alert.outageStart"
        static let outageStage = "helios.alert.outageStage" // 0 none, 1 first, 2 6h, 3 24h
        static let quietNotifiedAt = "helios.alert.quietNotifiedAt"
        // When the last audible outage alert fired, and whether the CURRENT
        // outage was opened silently. A Mac that sleeps and dark-wakes hourly
        // makes the link flap (fail, succeed, fail); without a cooldown that
        // produced an alert/recovery pair every hour. With it, a flapping link
        // gets at most one audible pair per 2h and is silent otherwise.
        static let lastOutageNotify = "helios.alert.lastOutageNotify"
        static let outageSilent = "helios.alert.outageSilent"
    }

    private static let sinceFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "MMM d, HH:mm"
        return f
    }()

    func requestAuthorization() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    private func notify(title: String, body: String, id: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        // nil trigger fires as soon as possible. Reusing the same id replaces
        // the previous banner instead of stacking a new one.
        let request = UNNotificationRequest(identifier: id, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    private func clearDelivered(_ ids: [String]) {
        UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: ids)
    }

    // MARK: Mac reachability (delivery outages)

    /// Called while uploads are failing. Notifies once per outage, then escalates
    /// at 6h and 24h. Everything in between stays silent: pages wait safely,
    /// and nagging about every failed sweep helps nobody.
    func reportDeliveryFailure(reason: String, queued: Int) {
        let now = Date()
        let start: Date
        if let stored = defaults.object(forKey: Key.outageStart) as? Date {
            start = stored
        } else {
            start = now
            defaults.set(now, forKey: Key.outageStart)
        }
        let stage = defaults.integer(forKey: Key.outageStage)
        let hours = now.timeIntervalSince(start) / 3600

        if stage < 1 {
            // Cooldown: if an audible outage alert fired within the last 2h,
            // open this outage silently (the 6h/24h escalations still apply).
            let lastNotify = defaults.object(forKey: Key.lastOutageNotify) as? Date
            if let lastNotify, now.timeIntervalSince(lastNotify) < 2 * 3600 {
                defaults.set(true, forKey: Key.outageSilent)
            } else {
                notify(title: "Mac unreachable, waiting safely",
                       body: "Cannot reach the Mac (\(reason)). Your data stays in Health and on the phone and delivers automatically once the Mac or the relay is reachable. No action needed.",
                       id: "helios.outage")
                defaults.set(now, forKey: Key.lastOutageNotify)
                defaults.set(false, forKey: Key.outageSilent)
            }
            defaults.set(1, forKey: Key.outageStage)
        } else if stage < 2, hours >= 6 {
            notify(title: "Mac unreachable for 6+ hours",
                   body: "Waiting since \(Self.sinceFormatter.string(from: start)), \(queued) pages queued. If the Mac is awake on this network, open Helios Bridge once. Otherwise this drains by itself on reconnect.",
                   id: "helios.outage")
            defaults.set(now, forKey: Key.lastOutageNotify)
            defaults.set(false, forKey: Key.outageSilent)
            defaults.set(2, forKey: Key.outageStage)
        } else if stage < 3, hours >= 24 {
            notify(title: "Mac unreachable for 24+ hours",
                   body: "Still waiting since \(Self.sinceFormatter.string(from: start)) (\(queued) pages queued). Nothing is lost, but the dashboard is a full day behind.",
                   id: "helios.outage")
            defaults.set(now, forKey: Key.lastOutageNotify)
            defaults.set(false, forKey: Key.outageSilent)
            defaults.set(3, forKey: Key.outageStage)
        }
    }

    /// Called after a delivery. Closes an open outage. Short blips close
    /// silently (the stale banner is simply removed from Notification Center);
    /// an audible "back online" fires only after an outage long enough to have
    /// escalated (6h+), which is when the owner was actually worried.
    func reportDeliverySuccess() {
        let stage = defaults.integer(forKey: Key.outageStage)
        let hadStart = defaults.object(forKey: Key.outageStart) != nil
        guard stage != 0 || hadStart else { return }
        let wasSilent = defaults.bool(forKey: Key.outageSilent)
        if stage >= 2, !wasSilent {
            notify(title: "Back online",
                   body: "The Mac is reachable again. Queued data is delivering now.",
                   id: "helios.outage.recovered")
        }
        defaults.removeObject(forKey: Key.outageStart)
        defaults.set(0, forKey: Key.outageStage)
        defaults.set(false, forKey: Key.outageSilent)
        clearDelivered(["helios.outage"])
    }

    // MARK: Quiet streams (writer apps, not the bridge)

    /// Heart rate has stopped ARRIVING IN HEALTH. That is a writer-app stall
    /// (Whoop or Zepp not syncing, Watch not worn), not a bridge fault, and the
    /// copy says so. At most one alert per 24h; cleared silently on recovery.
    func reportQuietStream(lastSample: Date) {
        if let lastNotified = defaults.object(forKey: Key.quietNotifiedAt) as? Date,
           Date().timeIntervalSince(lastNotified) < 24 * 3600 {
            return
        }
        notify(title: "Heart rate has stopped arriving in Health",
               body: "Nothing new since \(Self.sinceFormatter.string(from: lastSample)). The bridge is fine; a source app has stopped writing. Open Whoop or Zepp (or wear the Watch) and it backfills, then syncs on its own.",
               id: "helios.stream.quiet")
        defaults.set(Date(), forKey: Key.quietNotifiedAt)
    }

    /// Fresh samples arrived: clear the quiet alert without a sound.
    func reportStreamRecovered() {
        guard defaults.object(forKey: Key.quietNotifiedAt) != nil else { return }
        defaults.removeObject(forKey: Key.quietNotifiedAt)
        clearDelivered(["helios.stream.quiet"])
    }
}
