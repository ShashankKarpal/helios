import Foundation

// MARK: - ISO8601 helper
// The Mac keys on ISO8601 timestamps. We emit internet date-time with fractional
// seconds so the ingest side can parse a stable, unambiguous format.
enum ISO8601 {
    static let formatter: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    static func string(from date: Date) -> String {
        formatter.string(from: date)
    }
}

// MARK: - Sample value
// A single sample value is either numeric (quantity samples) or a string
// (category samples, e.g. the sleep-stage case name). We encode it as a bare
// JSON scalar so the Mac sees a number for quantities and a string for sleep.
enum SampleValue: Codable, Equatable {
    case number(Double)
    case string(String)

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .number(let value):
            try container.encode(value)
        case .string(let value):
            try container.encode(value)
        }
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else {
            self = .string(try container.decode(String.self))
        }
    }
}

// MARK: - Sample DTO
// Matches the JSON the Mac expects exactly. Field names use snake_case on the
// wire, which is why the property names are snake_case here (no CodingKeys map
// needed and nothing to drift out of sync).
struct SampleDTO: Codable, Equatable {
    let uuid: String
    let hk_type: String
    let value: SampleValue
    let unit: String
    let start: String
    let end: String
    let source_name: String
}

// MARK: - Batch
// { "batch_id": "uuid", "device": "iphone", "sent_at": ISO8601, "sync_path": "bridge",
//   "samples": [ ... ], "deleted": [ "<uuid>", ... ] }
struct Batch: Codable, Equatable {
    let batch_id: String
    let device: String
    let sent_at: String
    let sync_path: String
    let samples: [SampleDTO]
    let deleted: [String]

    static func make(samples: [SampleDTO], deleted: [String]) -> Batch {
        Batch(
            batch_id: UUID().uuidString,
            device: "iphone",
            sent_at: ISO8601.string(from: Date()),
            sync_path: "bridge",
            samples: samples,
            deleted: deleted
        )
    }

    var isEmpty: Bool { samples.isEmpty && deleted.isEmpty }
}

// MARK: - Ack response
// The Mac replies { "ack": true } once it has durably persisted the batch.
// We only advance the per-type anchor after seeing ack == true.
struct AckResponse: Codable {
    let ack: Bool
}
