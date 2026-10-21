import Foundation

// MARK: - Anchor store
// One file per stream in the v1 folder (Helios/Anchors/<stream key>), so the
// anchors the v1 Bridge saved for the 19 existing types carry over and those
// types do not drain their history again.
//
// v2 file: "HXA2" + generation (8 bytes, big endian) + the archived HKQueryAnchor.
// v1 file: the archived anchor alone (read as generation 0).
// The generation moves on every save and on every clear, in the same atomic
// write as the anchor, so "the anchor moved since this page was read" is one
// comparison and can never be half written.
public final class AnchorStore: @unchecked Sendable {   // guarded by `lock`
    static let magic = Data("HXA2".utf8)

    public let dir: URL
    private let lock = NSLock()

    public init(dir: URL) throws {
        self.dir = dir
        try DurableFile.makeDirectory(dir)
        DurableFile.removeStaleTemps(in: dir)
    }

    private func url(_ stream: String) -> URL {
        // Stream keys are HealthKit identifiers or "v2." plus one: safe file names.
        dir.appendingPathComponent(stream)
    }

    private func read(_ stream: String) -> (generation: Int, anchor: Data?) {
        guard let data = try? Data(contentsOf: url(stream)), !data.isEmpty else { return (0, nil) }
        if data.count >= 12, data.prefix(4) == Self.magic {
            let gen = data.subdata(in: 4..<12).reduce(0) { ($0 << 8) | Int($1) }
            let anchor = data.count > 12 ? data.subdata(in: 12..<data.count) : nil
            return (gen, anchor)
        }
        return (0, data)
    }

    /// The archived anchor, nil when the stream starts from the beginning.
    public func anchor(_ stream: String) -> Data? {
        lock.lock(); defer { lock.unlock() }
        return read(stream).anchor
    }

    public func generation(_ stream: String) -> Int {
        lock.lock(); defer { lock.unlock() }
        return read(stream).generation
    }

    /// Saves the anchor after an ack. Throws when the disk did not take it; the
    /// page is then read and sent again (harmless: the Mac dedupes).
    public func save(_ anchor: Data, for stream: String) throws {
        lock.lock(); defer { lock.unlock() }
        try write(stream, generation: read(stream).generation + 1, anchor: anchor)
    }

    /// Saves the anchor only if no other save happened since `generation` (an
    /// ack of a queued copy of the page read at that generation).
    @discardableResult
    public func save(_ anchor: Data, for stream: String, ifGeneration generation: Int) throws -> Bool {
        lock.lock(); defer { lock.unlock() }
        let current = read(stream).generation
        guard current == generation else { return false }
        try write(stream, generation: current + 1, anchor: anchor)
        return true
    }

    /// Clears the stream so its next drain starts from the beginning (Reset and
    /// re-pull). Keeps the generation counter moving, so every queued page of the
    /// old drain counts as superseded.
    public func clear(_ stream: String) throws {
        lock.lock(); defer { lock.unlock() }
        try write(stream, generation: read(stream).generation + 1, anchor: nil)
    }

    private func write(_ stream: String, generation: Int, anchor: Data?) throws {
        var data = Self.magic
        var g = UInt64(generation).bigEndian
        withUnsafeBytes(of: &g) { data.append(contentsOf: $0) }
        if let anchor { data.append(anchor) }
        try DurableFile.write(data, to: url(stream), point: "anchor.save")
    }
}
