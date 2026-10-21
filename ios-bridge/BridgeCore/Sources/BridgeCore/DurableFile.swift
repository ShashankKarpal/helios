import Foundation
#if canImport(Darwin)
import Darwin
#endif

// MARK: - Fault points (tests only)
// Production code calls Faults.hit(name) between durable steps. The hook is nil
// in the app. A test installs one that throws SimulatedCrash at the step it wants
// to model a crash at; the crash harness installs one that kills its own process.
// Either way the files on disk are exactly what a crash at that step leaves.
public enum Faults {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var _hook: ((String) throws -> Void)?

    public static var hook: ((String) throws -> Void)? {
        get { lock.lock(); defer { lock.unlock() }; return _hook }
        set { lock.lock(); _hook = newValue; lock.unlock() }
    }

    @inline(__always)
    public static func hit(_ point: String) throws {
        if let hook = hook { try hook(point) }
    }
}

public struct SimulatedCrash: Error, Equatable, CustomStringConvertible {
    public let point: String
    public init(point: String) { self.point = point }
    public var description: String { "simulated crash at \(point)" }
}

// MARK: - Errors
public struct DurableError: Error, Equatable, CustomStringConvertible {
    public let step: String
    public let path: String
    public let code: Int32
    public init(step: String, path: String, code: Int32) {
        self.step = step
        self.path = path
        self.code = code
    }
    public var description: String {
        "\(step) failed for \((path as NSString).lastPathComponent): \(String(cString: strerror(code)))"
    }
}

// MARK: - Durable file
// Every write goes to a temp file in the same folder, is flushed to stable
// storage, then renamed over the target, so after a crash the target holds the
// old bytes or the new bytes, never a torn mix. Every failure throws: nothing in
// the Bridge may report data as safe when the disk did not take it (C3).
public enum DurableFile {
    static let tempSuffix = ".tmp"

    /// Writes `data` to `url` atomically and durably. `point` names the fault
    /// points: "<point>.written" (temp file on disk, target untouched) and
    /// "<point>.renamed" (target replaced).
    public static func write(_ data: Data, to url: URL, point: String = "file") throws {
        let dir = url.deletingLastPathComponent()
        let tmp = dir.appendingPathComponent(".\(url.lastPathComponent).\(UUID().uuidString)\(tempSuffix)")
        let fd = open(tmp.path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw DurableError(step: "open", path: tmp.path, code: errno) }
        do {
            try writeAll(fd, data, path: tmp.path)
            try flush(fd, path: tmp.path)
        } catch {
            close(fd)
            unlink(tmp.path)
            throw error
        }
        close(fd)
        try Faults.hit("\(point).written")
        if rename(tmp.path, url.path) != 0 {
            let code = errno
            unlink(tmp.path)
            throw DurableError(step: "rename", path: url.path, code: code)
        }
        try Faults.hit("\(point).renamed")
        syncDirectory(dir)
    }

    /// Removes a file durably. A file that is already gone is not an error.
    public static func remove(_ url: URL, point: String = "remove") throws {
        try Faults.hit("\(point).before")
        if unlink(url.path) != 0 && errno != ENOENT {
            throw DurableError(step: "remove", path: url.path, code: errno)
        }
        try Faults.hit("\(point).removed")
        syncDirectory(url.deletingLastPathComponent())
    }

    /// Moves a file inside one volume. The destination's folder must exist.
    public static func move(_ from: URL, to: URL, point: String = "move") throws {
        try Faults.hit("\(point).before")
        if rename(from.path, to.path) != 0 {
            throw DurableError(step: "move", path: from.path, code: errno)
        }
        try Faults.hit("\(point).moved")
        syncDirectory(to.deletingLastPathComponent())
        syncDirectory(from.deletingLastPathComponent())
    }

    /// Temp files a crash left between write and rename. They never hold the
    /// only copy of anything (the target was not replaced yet), so they go.
    public static func removeStaleTemps(in dir: URL) {
        guard let names = try? FileManager.default.contentsOfDirectory(atPath: dir.path) else { return }
        for name in names where name.hasPrefix(".") && name.hasSuffix(tempSuffix) {
            unlink(dir.appendingPathComponent(name).path)
        }
    }

    /// The file's bytes; nil when the file does not exist. Any other failure
    /// (no permission, the phone locked before its first unlock, an I/O error)
    /// throws: a file that exists but cannot be read is never treated as absent.
    public static func read(_ url: URL) throws -> Data? {
        do {
            return try Data(contentsOf: url)
        } catch let error as CocoaError where error.code == .fileReadNoSuchFile {
            return nil
        } catch let error as NSError where error.domain == NSPOSIXErrorDomain && error.code == Int(ENOENT) {
            return nil
        }
    }

    /// Creates a folder (and its parents) or throws.
    public static func makeDirectory(_ url: URL) throws {
        do {
            try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        } catch {
            throw DurableError(step: "mkdir", path: url.path, code: Int32((error as NSError).code))
        }
    }

    private static func writeAll(_ fd: Int32, _ data: Data, path: String) throws {
        try data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            guard let base = raw.baseAddress, raw.count > 0 else { return }
            var offset = 0
            while offset < raw.count {
                let n = Darwin.write(fd, base + offset, raw.count - offset)
                if n < 0 {
                    if errno == EINTR { continue }
                    throw DurableError(step: "write", path: path, code: errno)
                }
                offset += n
            }
        }
    }

    private static func flush(_ fd: Int32, path: String) throws {
        // F_FULLFSYNC asks the drive to flush its own cache too; fall back to
        // fsync where the file system does not support it.
        if fcntl(fd, F_FULLFSYNC) == 0 { return }
        if fsync(fd) != 0 { throw DurableError(step: "fsync", path: path, code: errno) }
    }

    /// Makes a rename or unlink itself durable. Best effort: the data is
    /// already safe, and a failure here is not a reason to report it lost.
    static func syncDirectory(_ dir: URL) {
        let fd = open(dir.path, O_RDONLY | O_CLOEXEC)
        guard fd >= 0 else { return }
        _ = fsync(fd)
        close(fd)
    }
}
