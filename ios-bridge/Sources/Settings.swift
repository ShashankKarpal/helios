import Foundation
import Observation

// MARK: - Settings
// Mac host and shared token, persisted to UserDefaults. Observed by the status
// screen so edits take effect immediately.
@Observable
final class Settings {
    static let shared = Settings()

    @ObservationIgnored private let defaults = UserDefaults.standard
    private enum Key {
        static let host = "helios.host"
        static let token = "helios.token"
    }

    var host: String {
        didSet { defaults.set(host, forKey: Key.host) }
    }

    var token: String {
        didSet { defaults.set(token, forKey: Key.token) }
    }

    private init() {
        host = defaults.string(forKey: Key.host) ?? "helios.local:8420"
        token = defaults.string(forKey: Key.token) ?? ""
    }

    /// Builds the ingest endpoint. Accepts "helios.local:8420", a bare host, or a
    /// full "https://..." string. Defaults to HTTPS when no scheme is present.
    var ingestURL: URL? {
        let trimmed = host.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        let base = trimmed.contains("://") ? trimmed : "https://\(trimmed)"
        return URL(string: base + "/ingest")
    }
}
