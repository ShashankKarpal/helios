import Foundation
import Observation

extension Notification.Name {
    /// The Host field changed: the old link's status says nothing about the new host (C7).
    static let heliosHostChanged = Notification.Name("helios.hostChanged")
}

// MARK: - Settings
// Mac host and shared token, persisted to UserDefaults. Observed by the status
// screen so edits take effect immediately; the uploader reads UserDefaults.
@Observable
final class Settings {
    static let shared = Settings()
    static let hostKey = "helios.host"
    static let tokenKey = "helios.token"
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
        didSet { defaults.set(token, forKey: Self.tokenKey) }
    }

    private init() {
        host = defaults.string(forKey: Self.hostKey) ?? Self.defaultHost
        token = defaults.string(forKey: Self.tokenKey) ?? ""
    }
}
