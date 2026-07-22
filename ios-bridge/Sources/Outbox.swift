import Foundation
import Observation

// MARK: - Outbox
// A durable, on-disk queue of batches that could not be delivered. When the Mac
// is unreachable we append the batch here and leave the per-type anchor untouched,
// so nothing is lost. Flushing is idempotent: a batch is removed only after the
// Mac acknowledges it, and the Mac dedupes by sample uuid, so re-sending is safe.
@Observable
final class Outbox {
    static let shared = Outbox()

    @ObservationIgnored private let fileURL: URL
    @ObservationIgnored private let lock = NSLock()
    @ObservationIgnored private var batches: [Batch] = []

    /// Observed by the status screen.
    private(set) var depth: Int = 0

    private init() {
        let base = FileManager.default.urls(for: .applicationSupportDirectory,
                                            in: .userDomainMask)[0]
        let directory = base.appendingPathComponent("Helios", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory,
                                                 withIntermediateDirectories: true)
        fileURL = directory.appendingPathComponent("outbox.json")
        load()
    }

    private func load() {
        if let data = try? Data(contentsOf: fileURL),
           let stored = try? JSONDecoder().decode([Batch].self, from: data) {
            batches = stored
        }
        setDepth(batches.count)
    }

    private func persist() {
        if let data = try? JSONEncoder().encode(batches) {
            try? data.write(to: fileURL, options: .atomic)
        }
        setDepth(batches.count)
    }

    private func setDepth(_ value: Int) {
        if Thread.isMainThread {
            depth = value
        } else {
            DispatchQueue.main.async { self.depth = value }
        }
    }

    func enqueue(_ batch: Batch) {
        guard !batch.isEmpty else { return }
        lock.lock(); defer { lock.unlock() }
        batches.append(batch)
        persist()
    }

    func snapshot() -> [Batch] {
        lock.lock(); defer { lock.unlock() }
        return batches
    }

    func remove(batchID: String) {
        lock.lock(); defer { lock.unlock() }
        batches.removeAll { $0.batch_id == batchID }
        persist()
    }

    /// Attempts to deliver every queued batch in order. Stops at the first failure
    /// so we do not hammer an unreachable Mac. Returns true if the outbox is empty
    /// afterwards. Idempotent: a batch is removed only on a true ack.
    @discardableResult
    func flush() async -> Bool {
        for batch in snapshot() {
            let acked = await Uploader.shared.upload(batch)
            if acked {
                remove(batchID: batch.batch_id)
            } else {
                return false
            }
        }
        return snapshot().isEmpty
    }
}
