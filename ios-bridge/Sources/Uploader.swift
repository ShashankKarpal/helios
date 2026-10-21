import Foundation
import BridgeCore

// MARK: - Uploader
// POSTs a batch body to https://<host>/ingest with the X-Helios-Token header.
// TLS is validated normally (the host serves an mkcert certificate whose root is
// trusted on the phone). Only a 2xx with {"ack": true} is a delivery; the host is
// heliosd or the always-on relay, which acks once the batch is on its disk.
final class Uploader: BatchUploader {
    static let shared = Uploader()

    private let session: URLSession

    private init() {
        let config = URLSessionConfiguration.default
        // 45 s per request: during a heavy backfill the receiver can be briefly
        // busy, and a shorter cap stranded pages as "Queued (timeout)" in v1.
        config.timeoutIntervalForRequest = 45
        config.timeoutIntervalForResource = 90
        config.waitsForConnectivity = false
        config.allowsCellularAccess = true
        session = URLSession(configuration: config)
    }

    /// Read from UserDefaults (thread-safe), not from the observable Settings:
    /// uploads run off the main thread.
    private var endpoint: (url: URL, token: String)? {
        let host = (UserDefaults.standard.string(forKey: Settings.hostKey) ?? Settings.defaultHost)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !host.isEmpty else { return nil }
        let base = host.contains("://") ? host : "https://\(host)"
        guard let url = URL(string: base + "/ingest") else { return nil }
        return (url, UserDefaults.standard.string(forKey: Settings.tokenKey) ?? "")
    }

    func upload(_ body: Data) async -> UploadOutcome {
        guard let (url, token) = endpoint else { return .link("no host configured") }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(token, forHTTPHeaderField: "X-Helios-Token")
        request.httpBody = body

        // A short backoff inside one wake; anything still failing waits for the next.
        var delay: UInt64 = 500_000_000
        var last = "no answer"
        for attempt in 1...3 {
            do {
                let (data, response) = try await session.data(for: request)
                guard let http = response as? HTTPURLResponse else { return .link("not an HTTP reply") }
                switch http.statusCode {
                case 200..<300:
                    if let reply = try? JSONDecoder().decode(AckReply.self, from: data), reply.ack {
                        return .acked(reply)
                    }
                    return .link("no ack in reply")
                case 401, 403:
                    // The token or the client allowlist: nothing wrong with the batch.
                    return .link("HTTP \(http.statusCode) (token or allowlist)")
                case 400, 413, 422:
                    // The receiver read this batch and will not take it: park it.
                    return .refused(http.statusCode, Self.reason(data))
                case 408, 429, 500..<600:
                    last = "HTTP \(http.statusCode)"
                default:
                    // A wrong host or path (404, 405 ...) says nothing about the batch.
                    return .link("HTTP \(http.statusCode)")
                }
            } catch let error as URLError {
                switch error.code {
                case .timedOut: last = "timeout"
                case .cannotFindHost: last = "cannot find host"
                case .cannotConnectToHost: last = "cannot connect"
                case .notConnectedToInternet: last = "no network"
                case .serverCertificateUntrusted, .secureConnectionFailed: last = "TLS not trusted"
                default: last = error.localizedDescription
                }
            } catch {
                last = error.localizedDescription
            }
            if attempt < 3 {
                try? await Task.sleep(nanoseconds: delay)
                delay *= 2
            }
        }
        return .link(last)
    }

    private static func reason(_ data: Data) -> String {
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            if let e = obj["error"] as? String { return String(e.prefix(160)) }
            if let d = obj["detail"] { return String(String(describing: d).prefix(160)) }
        }
        return "refused"
    }
}
