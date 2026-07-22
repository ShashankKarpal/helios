import Foundation

// MARK: - Uploader
// POSTs a batch as JSON to https://<host>/ingest with the X-Helios-Token header.
// TLS is validated normally: the Mac serves an mkcert-issued certificate whose
// root is trusted on this device (see README). Returns true only when the Mac
// replies 2xx with { "ack": true }.
final class Uploader {
    static let shared = Uploader()

    private let session: URLSession

    private init() {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 20
        config.timeoutIntervalForResource = 45
        config.waitsForConnectivity = false
        config.allowsCellularAccess = true
        session = URLSession(configuration: config)
    }

    func upload(_ batch: Batch) async -> Bool {
        guard let url = Settings.shared.ingestURL else { return false }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(Settings.shared.token, forHTTPHeaderField: "X-Helios-Token")

        guard let body = try? JSONEncoder().encode(batch) else { return false }
        request.httpBody = body

        // Short exponential backoff. Background wakes are time-boxed, so keep this
        // modest; anything still failing lands in the outbox for the next sweep.
        let maxAttempts = 3
        var delayNanos: UInt64 = 500_000_000 // 0.5s

        for attempt in 1...maxAttempts {
            do {
                let (data, response) = try await session.data(for: request)
                if let http = response as? HTTPURLResponse,
                   (200..<300).contains(http.statusCode) {
                    if let ack = try? JSONDecoder().decode(AckResponse.self, from: data),
                       ack.ack {
                        return true
                    }
                    // 2xx but no positive ack: do not advance the anchor.
                    return false
                }
                // Non-2xx: retry unless it is a client error we cannot recover from.
                if let http = response as? HTTPURLResponse,
                   (400..<500).contains(http.statusCode) {
                    return false
                }
            } catch {
                // Network error (Mac asleep, off LAN, TLS not yet trusted): retry.
            }

            if attempt < maxAttempts {
                try? await Task.sleep(nanoseconds: delayNanos)
                delayNanos *= 2
            }
        }
        return false
    }
}
