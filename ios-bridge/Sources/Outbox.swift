import Foundation
import Observation

// MARK: - Outbox item
// A queued batch plus its delivery attempt count. Attempts are persisted so a
// batch that can never be delivered (for example one that always times out) is
// eventually dropped instead of blocking the queue forever. Dropping is safe:
// anchors only advance on ack, so any dropped window is re-fetched by the next
// anchored query and re-sent; the Mac dedupes by content hash.
struct OutboxItem: Codable {
    var batch: Batch
    var attempts: Int = 0
}

// MARK: - Outbox
// A durable, on-disk queue of batches that could not be delivered. When the Mac
// is unreachable we append the batch here and leave the per-type anchor untouched,
// so nothing is lost. Flushing is idempotent: a batch is removed only after the
// Mac acknowledges it, and the Mac dedupes by sample uuid, so re-sending is safe.
//
// No head-of-line blocking: a batch that fails to upload is rotated to the BACK
// of the queue (and dropped after maxAttempts), so one poisoned or oversized
// batch can never block the rest. The flush stops after 2 consecutive failures,
// which distinguishes "this batch is bad" from "the Mac is actually down".
@Observable
final class Outbox {
    static let shared = Outbox()

    @ObservationIgnored private let fileURL: URL
    @ObservationIgnored private let lock = NSLock()
    @ObservationIgnored private var items: [OutboxItem] = []
    @ObservationIgnored private let maxAttempts = 8

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
        if let data = try? Data(contentsOf: fileURL) {
            if let stored = try? JSONDecoder().decode([OutboxItem].self, from: data) {
                items = stored
            } else if let legacy = try? JSONDecoder().decode([Batch].self, from: data) {
                // Migrate the pre-attempts on-disk format.
                items = legacy.map { OutboxItem(batch: $0) }
            }
        }
        setDepth(items.count)
    }

    private func persist() {
        if let data = try? JSONEncoder().encode(items) {
            try? data.write(to: fileURL, options: .atomic)
        }
        setDepth(items.count)
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
        items.append(OutboxItem(batch: batch))
        persist()
    }

    private func head() -> OutboxItem? {
        lock.lock(); defer { lock.unlock() }
        return items.first
    }

    private func count() -> Int {
        lock.lock(); defer { lock.unlock() }
        return items.count
    }

    func remove(batchID: String) {
        lock.lock(); defer { lock.unlock() }
        items.removeAll { $0.batch.batch_id == batchID }
        persist()
    }

    /// Records a failed delivery: bump attempts, rotate the batch to the back of
    /// the queue, and drop it entirely once it has exhausted maxAttempts.
    private func recordFailure(batchID: String) {
        lock.lock(); defer { lock.unlock() }
        guard let idx = items.firstIndex(where: { $0.batch.batch_id == batchID }) else { return }
        var item = items.remove(at: idx)
        item.attempts += 1
        if item.attempts < maxAttempts {
            items.append(item)   // rotate: the next flush tries a different head
        }
        persist()
    }

    /// Attempts to deliver queued batches oldest-first. A failed batch rotates to
    /// the back instead of blocking the queue; flushing stops after 2 consecutive
    /// failures so an unreachable Mac is not hammered. Returns true if the outbox
    /// is empty afterwards. Idempotent: a batch is removed only on a true ack.
    @discardableResult
    func flush() async -> Bool {
        var consecutiveFailures = 0
        var visited = 0
        let budget = count() + 2
        while consecutiveFailures < 2, visited < budget, let item = head() {
            visited += 1
            let acked = await Uploader.shared.upload(item.batch)
            if acked {
                remove(batchID: item.batch.batch_id)
                consecutiveFailures = 0
            } else {
                recordFailure(batchID: item.batch.batch_id)
                consecutiveFailures += 1
            }
        }
        return count() == 0
    }
}
