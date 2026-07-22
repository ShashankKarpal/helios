import Foundation
import HealthKit

// MARK: - Anchor store
// Persists one HKQueryAnchor per Health type identifier to the app container
// using NSKeyedArchiver. The anchor is the resume point for HKAnchoredObjectQuery,
// so it MUST only be advanced after the Mac acknowledges the corresponding batch.
final class AnchorStore {
    static let shared = AnchorStore()

    private let directory: URL
    private let lock = NSLock()

    private init() {
        let base = FileManager.default.urls(for: .applicationSupportDirectory,
                                            in: .userDomainMask)[0]
        directory = base.appendingPathComponent("Helios/Anchors", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory,
                                                 withIntermediateDirectories: true)
    }

    private func url(for identifier: String) -> URL {
        // Identifiers are alphanumeric HealthKit tokens, safe as file names.
        directory.appendingPathComponent(identifier)
    }

    func anchor(for identifier: String) -> HKQueryAnchor? {
        lock.lock(); defer { lock.unlock() }
        guard let data = try? Data(contentsOf: url(for: identifier)) else { return nil }
        return try? NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: data)
    }

    func setAnchor(_ anchor: HKQueryAnchor, for identifier: String) {
        lock.lock(); defer { lock.unlock() }
        guard let data = try? NSKeyedArchiver.archivedData(withRootObject: anchor,
                                                           requiringSecureCoding: true) else {
            return
        }
        try? data.write(to: url(for: identifier), options: .atomic)
    }

    /// Used when the user wants to force a full re-backfill from scratch.
    func clearAll() {
        lock.lock(); defer { lock.unlock() }
        try? FileManager.default.removeItem(at: directory)
        try? FileManager.default.createDirectory(at: directory,
                                                 withIntermediateDirectories: true)
    }
}
