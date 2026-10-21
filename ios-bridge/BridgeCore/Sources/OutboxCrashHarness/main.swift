import Foundation
import BridgeCore

// The crash harness: a child process the tests kill with SIGKILL.
//
//   OutboxCrashHarness run    --dir D --seed S [--kill-at K] [--slow-us N]
//   OutboxCrashHarness verify --dir D --seed S
//
// run: migrates D/outbox.json if present (the v1 queue), then drains the
// synthetic workload against a fake Mac whose state is an fsynced log in D, so it
// survives the kill. --kill-at K kills the process at the K-th durable step
// (every DurableFile write, rename, remove and move is a step); --slow-us sleeps
// at every step so a timed kill from outside can land inside one. Prints
// "hits=<n>" when it finishes.
// verify: prints one JSON line comparing the fake Mac with the fake HealthKit.

final class Counter {
    private let lock = NSLock()
    private var n = 0
    func bump() -> Int {
        lock.lock(); defer { lock.unlock() }
        n += 1
        return n
    }
    var value: Int {
        lock.lock(); defer { lock.unlock() }
        return n
    }
}

func option(_ name: String) -> String? {
    let args = CommandLine.arguments
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    return args[i + 1]
}

let mode = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "run"
guard let dirPath = option("--dir") else {
    FileHandle.standardError.write(Data("missing --dir\n".utf8))
    exit(2)
}
let dir = URL(fileURLWithPath: dirPath, isDirectory: true)
let seed = Int(option("--seed") ?? "1") ?? 1
let health = FakeHealth()
Workload.populate(health, seed: seed)
let server = FakeServer(logURL: dir.appendingPathComponent("server.log"))
server.fallback = Workload.serverMode

/// The v1 queue the parent wrote before the first run: its uuids exist only in the outbox.
func legacyUUIDs() -> Set<String> {
    let url = dir.appendingPathComponent("legacy-uuids.txt")
    guard let text = try? String(contentsOf: url, encoding: .utf8) else { return [] }
    return Set(text.split(separator: "\n").map(String.init))
}

switch mode {
case "run":
    let hits = Counter()
    let killAt = Int(option("--kill-at") ?? "0") ?? 0
    let slow = UInt32(option("--slow-us") ?? "0") ?? 0
    Faults.hook = { _ in
        let n = hits.bump()
        if slow > 0 { usleep(slow) }
        if killAt > 0 && n == killAt { kill(getpid(), SIGKILL) }
    }
    do {
        let engine = try Workload.makeEngine(dir: dir, health: health, server: server)
        _ = try engine.outbox.migrateLegacy(at: dir.appendingPathComponent("outbox.json"), now: Date())
        await Workload.run(engine)
        print("hits=\(hits.value)")
        exit(0)
    } catch {
        print("error=\(error)")
        exit(3)
    }

case "verify":
    do {
        let outbox = try OutboxStore(root: dir.appendingPathComponent("Outbox", isDirectory: true))
        let expected = Set(health.alive().map(\.uuid)).union(legacyUUIDs())
        let counts = outbox.counts
        let report: [String: Int] = [
            "missing": expected.subtracting(server.alive).count,
            "phantom": Set(server.received.keys).subtracting(health.everAdded).subtracting(legacyUUIDs()).count,
            "resurrected": server.alive.intersection(health.everDeleted).count,
            "queued": counts.queued,
            "parked": counts.parked,
            "quarantined": counts.quarantined,
            "duplicates": server.received.values.reduce(0) { $0 + max(0, $1 - 1) },
            "v1_left": FileManager.default.fileExists(atPath: dir.appendingPathComponent("outbox.json").path) ? 1 : 0,
        ]
        let data = try JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])
        print(String(decoding: data, as: UTF8.self))
        exit(0)
    } catch {
        print("error=\(error)")
        exit(3)
    }

default:
    FileHandle.standardError.write(Data("unknown mode \(mode)\n".utf8))
    exit(2)
}
