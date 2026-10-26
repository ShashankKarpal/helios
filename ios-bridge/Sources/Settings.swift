import Foundation
import Observation
import GlanceCore

extension Notification.Name {
    /// The Host field changed: the old link's status says nothing about the new host (C7).
    static let heliosHostChanged = Notification.Name("helios.hostChanged")
}

// MARK: - Settings
// Mac host and shared token, persisted to UserDefaults. Observed by the status
// screen so edits take effect immediately; the uploader reads UserDefaults.
// The summary address (rank 3) is the Mac itself, for the widget and the
// morning notification; the Host may stay the relay. The summary address and
// the token are mirrored into the App Group so the widget can ask the Mac.
@Observable
final class Settings {
    static let shared = Settings()
    static let hostKey = "helios.host"
    static let tokenKey = "helios.token"
    static let summaryHostKey = GlanceShared.summaryHostKey
    static let defaultHost = "shanky-m4.local:8420"

    @ObservationIgnored private let defaults = UserDefaults.standard

    var host: String {
        didSet {
            guard host != oldValue else { return }
            defaults.set(host, forKey: Self.hostKey)
            NotificationCenter.default.post(name: .heliosHostChanged, object: nil)
        }
    }

    var token: String {
        didSet {
            defaults.set(token, forKey: Self.tokenKey)
            mirror()
        }
    }

    var summaryHost: String {
        didSet {
            guard summaryHost != oldValue else { return }
            defaults.set(summaryHost, forKey: Self.summaryHostKey)
            mirror()
        }
    }

    private init() {
        host = defaults.string(forKey: Self.hostKey) ?? Self.defaultHost
        token = defaults.string(forKey: Self.tokenKey) ?? ""
        summaryHost = defaults.string(forKey: Self.summaryHostKey) ?? ""
        mirror()
    }

    /// The widget's copy, in the App Group's defaults.
    private func mirror() {
        guard let shared = UserDefaults(suiteName: GlanceShared.appGroup) else { return }
        shared.set(summaryHost.trimmingCharacters(in: .whitespacesAndNewlines), forKey: GlanceShared.summaryHostKey)
        shared.set(token, forKey: GlanceShared.tokenKey)
    }
}
