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
                Task { await HealthKitManager.shared.foregroundSweep(reason: .foreground) }
                BGTaskManager.shared.schedule()
            }
        }
    }

    // helios-bridge://sync triggers an immediate foreground sweep. The PWA
    // "Pull latest" button opens this deep link.
    private func handle(_ url: URL) {
        guard url.scheme == "helios-bridge" else { return }
        if url.host == "sync" {
            Task { await HealthKitManager.shared.foregroundSweep(reason: .deepLink) }
        }
    }
}

// MARK: - App delegate
// BGTaskScheduler registration MUST happen before application launch finishes,
// which is why it lives here rather than in a SwiftUI lifecycle hook.
final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        BGTaskManager.shared.register()
        NotificationManager.shared.requestAuthorization()
        Task { await HealthKitManager.shared.bootstrap() }
        return true
    }
}

// MARK: - Background task manager
final class BGTaskManager {
    static let shared = BGTaskManager()

    let identifier = "com.shanky.helios.bridge.refresh"

    func register() {
        BGTaskScheduler.shared.register(forTaskWithIdentifier: identifier, using: nil) { task in
            guard let refreshTask = task as? BGAppRefreshTask else {
                task.setTaskCompleted(success: false)
                return
            }
            self.handle(refreshTask)
        }
    }

    func schedule() {
        let request = BGAppRefreshTaskRequest(identifier: identifier)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        try? BGTaskScheduler.shared.submit(request)
    }

    private func handle(_ task: BGAppRefreshTask) {
        // Always schedule the next sweep first so the chain continues.
        schedule()

        let work = Task {
            await HealthKitManager.shared.performSweep(reason: .background)
            task.setTaskCompleted(success: true)
        }

        task.expirationHandler = {
            work.cancel()
            task.setTaskCompleted(success: false)
        }
    }
}

// MARK: - Local notifications
final class NotificationManager {
    static let shared = NotificationManager()

    func requestAuthorization() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    func notify(title: String, body: String, id: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        // nil trigger fires as soon as possible.
        let request = UNNotificationRequest(identifier: id, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }
}
