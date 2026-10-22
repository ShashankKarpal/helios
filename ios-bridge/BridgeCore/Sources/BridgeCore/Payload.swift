import Foundation

// MARK: - Time stamps on the wire
// Internet date-time in UTC with milliseconds ("2026-10-21T07:04:05.123Z"). The
// Mac parses the offset, keeps the string verbatim in the raw envelope and the
// instant to the second in `samples`.
public enum WireTime {
    nonisolated(unsafe) private static let formatter: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    private static let lock = NSLock()

    public static func string(_ date: Date) -> String {
        lock.lock(); defer { lock.unlock() }
        return formatter.string(from: date)
    }

    public static func date(_ string: String) -> Date? {
        lock.lock(); defer { lock.unlock() }
        return formatter.date(from: string)
    }
}

// MARK: - Sample value (v1 and v2)
// A bare JSON scalar: a number for quantities, a string for the sleep stage name.
public enum SampleValue: Codable, Equatable {
    case number(Double)
    case string(String)

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .number(let v): try c.encode(v)
        case .string(let v): try c.encode(v)
        }
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let v = try? c.decode(Double.self) {
            self = .number(v)
        } else {
            self = .string(try c.decode(String.self))
        }
    }
}

// MARK: - v1 (the shape the Bridge sent until Phase 3)
// Kept to read the v1 outbox file during the migration, and as the reference the
// tests compare the v2 sample fields against. Field names are the wire names.
public struct SampleDTOV1: Codable, Equatable {
    public let uuid: String
    public let hk_type: String
    public let value: SampleValue
    public let unit: String
    public let start: String
    public let end: String
    public let source_name: String

    public init(uuid: String, hk_type: String, value: SampleValue, unit: String, start: String, end: String,
                source_name: String) {
        self.uuid = uuid
        self.hk_type = hk_type
        self.value = value
        self.unit = unit
        self.start = start
        self.end = end
        self.source_name = source_name
    }
}

public struct BatchV1: Codable, Equatable {
    public let batch_id: String
    public let device: String
    public let sent_at: String
    public let sync_path: String
    public let samples: [SampleDTOV1]
    public let deleted: [String]

    public init(batch_id: String, device: String = "iphone", sent_at: String, sync_path: String = "bridge",
                samples: [SampleDTOV1], deleted: [String]) {
        self.batch_id = batch_id
        self.device = device
        self.sent_at = sent_at
        self.sync_path = sync_path
        self.samples = samples
        self.deleted = deleted
    }
}

/// One entry of the v1 outbox.json (Outbox.swift until Phase 3).
public struct OutboxItemV1: Codable, Equatable {
    public var batch: BatchV1
    public var attempts: Int = 0

    public init(batch: BatchV1, attempts: Int = 0) {
        self.batch = batch
        self.attempts = attempts
    }
}

// MARK: - v2 descriptors shared by every record of a batch
// A batch names each distinct source revision and device once; records point at
// them by a short key. Keeps a 2,000-sample page near its v1 size.
public struct SourceInfo: Codable, Equatable, Hashable {
    public var name: String
    public var bundleID: String
    public var version: String?
    public var productType: String?
    public var osVersion: String

    enum CodingKeys: String, CodingKey {
        case name
        case bundleID = "bundle_id"
        case version
        case productType = "product_type"
        case osVersion = "os_version"
    }

    public init(name: String, bundleID: String, version: String?, productType: String?, osVersion: String) {
        self.name = name
        self.bundleID = bundleID
        self.version = version
        self.productType = productType
        self.osVersion = osVersion
    }
}

public struct DeviceInfo: Codable, Equatable, Hashable {
    /// HKDevice's own description, the same text the Apple Health export prints
    /// in its device attribute (memory address included; the Mac blanks it).
    public var desc: String
    public var name: String?
    public var manufacturer: String?
    public var model: String?
    public var hardware: String?
    public var firmware: String?
    public var software: String?
    public var localID: String?
    public var udi: String?

    enum CodingKeys: String, CodingKey {
        case desc, name, manufacturer, model, hardware, firmware, software
        case localID = "local_id"
        case udi
    }

    public init(desc: String, name: String? = nil, manufacturer: String? = nil, model: String? = nil,
                hardware: String? = nil, firmware: String? = nil, software: String? = nil,
                localID: String? = nil, udi: String? = nil) {
        self.desc = desc
        self.name = name
        self.manufacturer = manufacturer
        self.model = model
        self.hardware = hardware
        self.firmware = firmware
        self.software = software
        self.localID = localID
        self.udi = udi
    }
}

/// Metadata as [[key, value]] pairs, sorted by key, every value as text (the
/// export's own representation: a quantity reads "3.5 kcal/hr·kg").
public typealias MetadataPairs = [[String]]

// MARK: - v2 records
public struct SampleV2: Codable, Equatable {
    public var uuid: String
    public var hkType: String
    public var kind: String                 // quantity | category
    public var value: SampleValue?          // quantity: the number in `unit`; sleep: the stage name; other categories: the raw value
    public var unit: String
    public var start: String
    public var end: String
    public var sourceName: String
    public var src: String?                 // key into the batch's sources
    public var dev: String?                 // key into the batch's devices
    public var metadata: MetadataPairs?
    public var syncIdentifier: String?
    public var syncVersion: Int?
    public var writerID: String?            // the source bundle id: the scope of a sync identifier
    public var categoryValue: Int?
    public var note: String?                // why a value is missing (an incompatible unit)

    enum CodingKeys: String, CodingKey {
        case uuid
        case hkType = "hk_type"
        case kind, value, unit, start, end
        case sourceName = "source_name"
        case src, dev, metadata
        case syncIdentifier = "sync_identifier"
        case syncVersion = "sync_version"
        case writerID = "writer_id"
        case categoryValue = "category_value"
        case note
    }

    public init(uuid: String, hkType: String, kind: String, value: SampleValue?, unit: String, start: String,
                end: String, sourceName: String, src: String? = nil, dev: String? = nil,
                metadata: MetadataPairs? = nil, syncIdentifier: String? = nil, syncVersion: Int? = nil,
                writerID: String? = nil, categoryValue: Int? = nil, note: String? = nil) {
        self.uuid = uuid
        self.hkType = hkType
        self.kind = kind
        self.value = value
        self.unit = unit
        self.start = start
        self.end = end
        self.sourceName = sourceName
        self.src = src
        self.dev = dev
        self.metadata = metadata
        self.syncIdentifier = syncIdentifier
        self.syncVersion = syncVersion
        self.writerID = writerID
        self.categoryValue = categoryValue
        self.note = note
    }
}

public struct WorkoutStatV2: Codable, Equatable {
    public var hkType: String
    public var start: String
    public var end: String
    public var sum: Double?
    public var avg: Double?
    public var min: Double?
    public var max: Double?
    public var mostRecent: Double?
    public var unit: String

    enum CodingKeys: String, CodingKey {
        case hkType = "hk_type"
        case start, end, sum, avg, min, max
        case mostRecent = "most_recent"
        case unit
    }

    public init(hkType: String, start: String, end: String, sum: Double? = nil, avg: Double? = nil,
                min: Double? = nil, max: Double? = nil, mostRecent: Double? = nil, unit: String) {
        self.hkType = hkType
        self.start = start
        self.end = end
        self.sum = sum
        self.avg = avg
        self.min = min
        self.max = max
        self.mostRecent = mostRecent
        self.unit = unit
    }
}

public struct WorkoutEventV2: Codable, Equatable {
    public var type: Int
    public var typeName: String
    public var start: String
    public var end: String
    public var metadata: MetadataPairs?

    enum CodingKeys: String, CodingKey {
        case type
        case typeName = "type_name"
        case start, end, metadata
    }

    public init(type: Int, typeName: String, start: String, end: String, metadata: MetadataPairs? = nil) {
        self.type = type
        self.typeName = typeName
        self.start = start
        self.end = end
        self.metadata = metadata
    }
}

public struct WorkoutActivityV2: Codable, Equatable {
    public var uuid: String
    public var activityType: UInt
    public var activityName: String
    public var start: String
    public var end: String?
    public var durationS: Double
    public var metadata: MetadataPairs?
    public var statistics: [WorkoutStatV2]
    public var events: [WorkoutEventV2]

    enum CodingKeys: String, CodingKey {
        case uuid
        case activityType = "activity_type"
        case activityName = "activity_name"
        case start, end
        case durationS = "duration_s"
        case metadata, statistics, events
    }

    public init(uuid: String, activityType: UInt, activityName: String, start: String, end: String?,
                durationS: Double, metadata: MetadataPairs?, statistics: [WorkoutStatV2], events: [WorkoutEventV2]) {
        self.uuid = uuid
        self.activityType = activityType
        self.activityName = activityName
        self.start = start
        self.end = end
        self.durationS = durationS
        self.metadata = metadata
        self.statistics = statistics
        self.events = events
    }
}

public struct WorkoutV2: Codable, Equatable {
    public var uuid: String
    public var activityType: UInt
    public var activityName: String
    public var start: String
    public var end: String
    public var durationS: Double
    public var sourceName: String
    public var src: String?
    public var dev: String?
    public var metadata: MetadataPairs?
    public var syncIdentifier: String?
    public var syncVersion: Int?
    public var writerID: String?
    public var statistics: [WorkoutStatV2]
    public var events: [WorkoutEventV2]
    public var activities: [WorkoutActivityV2]
    /// Routes HealthKit associates with this workout (HKQuery.predicateForObjects(from:)).
    public var routeUUIDs: [String]
    /// The workout's own totals (the attributes the Apple Health export prints as
    /// totalEnergyBurned and totalDistance), kcal and metres.
    public var totalEnergyKcal: Double?
    public var totalDistanceM: Double?

    enum CodingKeys: String, CodingKey {
        case uuid
        case activityType = "activity_type"
        case activityName = "activity_name"
        case start, end
        case durationS = "duration_s"
        case sourceName = "source_name"
        case src, dev, metadata
        case syncIdentifier = "sync_identifier"
        case syncVersion = "sync_version"
        case writerID = "writer_id"
        case statistics, events, activities
        case routeUUIDs = "route_uuids"
        case totalEnergyKcal = "total_energy_kcal"
        case totalDistanceM = "total_distance_m"
    }

    public init(uuid: String, activityType: UInt, activityName: String, start: String, end: String,
                durationS: Double, sourceName: String, src: String? = nil, dev: String? = nil,
                metadata: MetadataPairs? = nil, syncIdentifier: String? = nil, syncVersion: Int? = nil,
                writerID: String? = nil, statistics: [WorkoutStatV2] = [], events: [WorkoutEventV2] = [],
                activities: [WorkoutActivityV2] = [], routeUUIDs: [String] = [], totalEnergyKcal: Double? = nil,
                totalDistanceM: Double? = nil) {
        self.uuid = uuid
        self.activityType = activityType
        self.activityName = activityName
        self.start = start
        self.end = end
        self.durationS = durationS
        self.sourceName = sourceName
        self.src = src
        self.dev = dev
        self.metadata = metadata
        self.syncIdentifier = syncIdentifier
        self.syncVersion = syncVersion
        self.writerID = writerID
        self.statistics = statistics
        self.events = events
        self.activities = activities
        self.routeUUIDs = routeUUIDs
        self.totalEnergyKcal = totalEnergyKcal
        self.totalDistanceM = totalDistanceM
    }
}

/// One location of a route, encoded as an array to keep long routes small:
/// [time, latitude, longitude, altitude m, horizontal accuracy m, vertical
/// accuracy m, speed m/s, course degrees, speed accuracy, course accuracy].
/// A negative accuracy or speed from CoreLocation means "invalid" and is sent as null.
public struct RoutePointV2: Codable, Equatable {
    public var t: String
    public var lat: Double
    public var lon: Double
    public var alt: Double?
    public var hAcc: Double?
    public var vAcc: Double?
    public var speed: Double?
    public var course: Double?
    public var speedAcc: Double?
    public var courseAcc: Double?

    public init(t: String, lat: Double, lon: Double, alt: Double?, hAcc: Double?, vAcc: Double?,
                speed: Double?, course: Double?, speedAcc: Double?, courseAcc: Double?) {
        self.t = t
        self.lat = lat
        self.lon = lon
        self.alt = alt
        self.hAcc = hAcc
        self.vAcc = vAcc
        self.speed = speed
        self.course = course
        self.speedAcc = speedAcc
        self.courseAcc = courseAcc
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.unkeyedContainer()
        try c.encode(t)
        try c.encode(lat)
        try c.encode(lon)
        for v in [alt, hAcc, vAcc, speed, course, speedAcc, courseAcc] {
            if let v { try c.encode(v) } else { try c.encodeNil() }
        }
    }

    public init(from decoder: Decoder) throws {
        var c = try decoder.unkeyedContainer()
        t = try c.decode(String.self)
        lat = try c.decode(Double.self)
        lon = try c.decode(Double.self)
        alt = try c.decodeIfPresent(Double.self)
        hAcc = try c.decodeIfPresent(Double.self)
        vAcc = try c.decodeIfPresent(Double.self)
        speed = try c.decodeIfPresent(Double.self)
        course = try c.decodeIfPresent(Double.self)
        speedAcc = try c.decodeIfPresent(Double.self)
        courseAcc = try c.decodeIfPresent(Double.self)
    }
}

public struct RouteV2: Codable, Equatable {
    public var uuid: String
    /// The workout HealthKit associates the route with, proven by asking HealthKit
    /// for that workout's routes (never by time overlap); nil when none was found.
    public var workoutUUID: String?
    public var start: String
    public var end: String
    public var sourceName: String
    public var src: String?
    public var dev: String?
    public var metadata: MetadataPairs?
    public var points: [RoutePointV2]

    enum CodingKeys: String, CodingKey {
        case uuid
        case workoutUUID = "workout_uuid"
        case start, end
        case sourceName = "source_name"
        case src, dev, metadata, points
    }

    public init(uuid: String, workoutUUID: String?, start: String, end: String, sourceName: String,
                src: String? = nil, dev: String? = nil, metadata: MetadataPairs? = nil, points: [RoutePointV2]) {
        self.uuid = uuid
        self.workoutUUID = workoutUUID
        self.start = start
        self.end = end
        self.sourceName = sourceName
        self.src = src
        self.dev = dev
        self.metadata = metadata
        self.points = points
    }
}

public struct ECGV2: Codable, Equatable {
    public var uuid: String
    public var start: String
    public var end: String
    public var classification: Int
    public var classificationName: String
    public var symptomsStatus: Int
    public var symptomsName: String
    public var averageHR: Double?
    public var samplingHz: Double?
    public var nVoltages: Int
    public var sourceName: String
    public var src: String?
    public var dev: String?
    public var metadata: MetadataPairs?
    public var lead: String
    public var unit: String
    public var microvolts: [Double?]

    enum CodingKeys: String, CodingKey {
        case uuid, start, end, classification
        case classificationName = "classification_name"
        case symptomsStatus = "symptoms_status"
        case symptomsName = "symptoms_name"
        case averageHR = "average_hr"
        case samplingHz = "sampling_hz"
        case nVoltages = "n_voltages"
        case sourceName = "source_name"
        case src, dev, metadata, lead, unit, microvolts
    }

    public init(uuid: String, start: String, end: String, classification: Int, classificationName: String,
                symptomsStatus: Int, symptomsName: String, averageHR: Double?, samplingHz: Double?, nVoltages: Int,
                sourceName: String, src: String? = nil, dev: String? = nil, metadata: MetadataPairs? = nil,
                lead: String, unit: String = "mcV", microvolts: [Double?]) {
        self.uuid = uuid
        self.start = start
        self.end = end
        self.classification = classification
        self.classificationName = classificationName
        self.symptomsStatus = symptomsStatus
        self.symptomsName = symptomsName
        self.averageHR = averageHR
        self.samplingHz = samplingHz
        self.nVoltages = nVoltages
        self.sourceName = sourceName
        self.src = src
        self.dev = dev
        self.metadata = metadata
        self.lead = lead
        self.unit = unit
        self.microvolts = microvolts
    }
}

/// One day of Activity rings as HealthKit states it (native date components and goals).
public struct ActivitySummaryV2: Codable, Equatable {
    public var date: String                 // yyyy-MM-dd from the summary's own date components
    public var era: Int?
    public var activeEnergy: Double?
    public var activeEnergyGoal: Double?
    public var activeEnergyUnit: String
    public var moveTime: Double?
    public var moveTimeGoal: Double?
    public var exerciseTime: Double?
    public var exerciseTimeGoal: Double?
    public var standHours: Double?
    public var standHoursGoal: Double?
    public var moveMode: Int?

    enum CodingKeys: String, CodingKey {
        case date, era
        case activeEnergy = "active_energy"
        case activeEnergyGoal = "active_energy_goal"
        case activeEnergyUnit = "active_energy_unit"
        case moveTime = "move_time"
        case moveTimeGoal = "move_time_goal"
        case exerciseTime = "exercise_time"
        case exerciseTimeGoal = "exercise_time_goal"
        case standHours = "stand_hours"
        case standHoursGoal = "stand_hours_goal"
        case moveMode = "move_mode"
    }

    public init(date: String, era: Int? = nil, activeEnergy: Double? = nil, activeEnergyGoal: Double? = nil,
                activeEnergyUnit: String = "kcal", moveTime: Double? = nil, moveTimeGoal: Double? = nil,
                exerciseTime: Double? = nil, exerciseTimeGoal: Double? = nil, standHours: Double? = nil,
                standHoursGoal: Double? = nil, moveMode: Int? = nil) {
        self.date = date
        self.era = era
        self.activeEnergy = activeEnergy
        self.activeEnergyGoal = activeEnergyGoal
        self.activeEnergyUnit = activeEnergyUnit
        self.moveTime = moveTime
        self.moveTimeGoal = moveTimeGoal
        self.exerciseTime = exerciseTime
        self.exerciseTimeGoal = exerciseTimeGoal
        self.standHours = standHours
        self.standHoursGoal = standHoursGoal
        self.moveMode = moveMode
    }
}

/// Apple's own daily total for one type (HKStatisticsCollectionQuery, cumulative
/// sum, Apple's de-duplication across sources) on a reporting-zone day: the check
/// value for Helios's steps and energy (C9). `sources` is the per-source split.
public struct DailyStatisticV2: Codable, Equatable {
    public var hkType: String
    public var day: String
    public var zone: String
    public var value: Double?
    public var unit: String
    public var complete: Bool
    public var sources: [[String]]?          // [[source name, bundle id, value as text], ...]

    enum CodingKeys: String, CodingKey {
        case hkType = "hk_type"
        case day, zone, value, unit, complete, sources
    }

    public init(hkType: String, day: String, zone: String, value: Double?, unit: String, complete: Bool,
                sources: [[String]]? = nil) {
        self.hkType = hkType
        self.day = day
        self.zone = zone
        self.value = value
        self.unit = unit
        self.complete = complete
        self.sources = sources
    }
}

// MARK: - The batch
/// Where a page came from: the Mac logs it, and the Bridge uses it to decide
/// what may be queued (only anchored pages ever enter the outbox).
public enum PageOrigin: String, Codable {
    case anchored, sweep, reread, summaries, dailystats, legacy
}

public struct BridgeMeta: Codable, Equatable {
    public var appVersion: String
    public var build: String
    public var osVersion: String
    public var zone: String
    public var reason: String
    public var origin: PageOrigin
    public var stream: String?
    public var generation: Int?
    public var windowStart: String?
    public var windowEnd: String?

    enum CodingKeys: String, CodingKey {
        case appVersion = "app_version"
        case build
        case osVersion = "os_version"
        case zone, reason, origin, stream, generation
        case windowStart = "window_start"
        case windowEnd = "window_end"
    }

    public init(appVersion: String, build: String, osVersion: String, zone: String, reason: String,
                origin: PageOrigin, stream: String? = nil, generation: Int? = nil, windowStart: String? = nil,
                windowEnd: String? = nil) {
        self.appVersion = appVersion
        self.build = build
        self.osVersion = osVersion
        self.zone = zone
        self.reason = reason
        self.origin = origin
        self.stream = stream
        self.generation = generation
        self.windowStart = windowStart
        self.windowEnd = windowEnd
    }
}

/// The records of one page, before the batch header is added.
public struct BatchContent: Equatable {
    public var sources: [String: SourceInfo] = [:]
    public var devices: [String: DeviceInfo] = [:]
    public var samples: [SampleV2] = []
    public var deleted: [String] = []
    public var workouts: [WorkoutV2] = []
    public var routes: [RouteV2] = []
    public var ecgs: [ECGV2] = []
    public var activitySummaries: [ActivitySummaryV2] = []
    public var dailyStatistics: [DailyStatisticV2] = []
    /// The earliest start and latest end among the page's records (status and logs).
    public var windowStart: Date?
    public var windowEnd: Date?

    public init() {}

    public var recordCount: Int {
        samples.count + workouts.count + routes.count + ecgs.count + activitySummaries.count + dailyStatistics.count
    }

    public var isEmpty: Bool { recordCount == 0 && deleted.isEmpty }

    /// The uuids of the page's HealthKit objects (the read-again list's keys).
    public var recordUUIDs: Set<String> {
        Set(samples.map(\.uuid) + workouts.map(\.uuid) + routes.map(\.uuid) + ecgs.map(\.uuid))
    }

    /// Distinct HealthKit identifiers in the page (outbox header, logs).
    public var types: [String] {
        var set = Set<String>()
        samples.forEach { set.insert($0.hkType) }
        if !workouts.isEmpty { set.insert("HKWorkoutTypeIdentifier") }
        if !routes.isEmpty { set.insert("HKWorkoutRouteTypeIdentifier") }
        if !ecgs.isEmpty { set.insert("HKDataTypeIdentifierElectrocardiogram") }
        if !activitySummaries.isEmpty { set.insert("HKActivitySummaryTypeIdentifier") }
        dailyStatistics.forEach { set.insert($0.hkType) }
        return set.sorted()
    }

    /// Registers a source revision once per page and returns its key.
    public mutating func sourceKey(_ info: SourceInfo) -> String {
        if let (k, _) = sources.first(where: { $0.value == info }) { return k }
        let k = "s\(sources.count + 1)"
        sources[k] = info
        return k
    }

    /// Registers a device once per page and returns its key.
    public mutating func deviceKey(_ info: DeviceInfo) -> String {
        if let (k, _) = devices.first(where: { $0.value == info }) { return k }
        let k = "d\(devices.count + 1)"
        devices[k] = info
        return k
    }

    public mutating func widen(start: Date, end: Date) {
        windowStart = windowStart.map { Swift.min($0, start) } ?? start
        windowEnd = windowEnd.map { Swift.max($0, end) } ?? end
    }
}

public struct BatchV2: Encodable {
    public static let schemaVersion = 2

    public var batchID: String
    public var device: String = "iphone"
    public var sentAt: String
    public var syncPath: String = "bridge"
    public var schema: Int = BatchV2.schemaVersion
    public var bridge: BridgeMeta
    public var content: BatchContent

    enum CodingKeys: String, CodingKey {
        case batchID = "batch_id"
        case device
        case sentAt = "sent_at"
        case syncPath = "sync_path"
        case schema, bridge, sources, devices, samples, deleted, workouts, routes, ecgs
        case activitySummaries = "activity_summaries"
        case dailyStatistics = "daily_statistics"
    }

    public init(batchID: String = UUID().uuidString, sentAt: Date, bridge: BridgeMeta, content: BatchContent) {
        self.batchID = batchID
        self.sentAt = WireTime.string(sentAt)
        self.bridge = bridge
        self.content = content
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(batchID, forKey: .batchID)
        try c.encode(device, forKey: .device)
        try c.encode(sentAt, forKey: .sentAt)
        try c.encode(syncPath, forKey: .syncPath)
        try c.encode(schema, forKey: .schema)
        try c.encode(bridge, forKey: .bridge)
        // v1 servers read samples and deleted: always present, possibly empty.
        try c.encode(content.samples, forKey: .samples)
        try c.encode(content.deleted, forKey: .deleted)
        if !content.sources.isEmpty { try c.encode(content.sources, forKey: .sources) }
        if !content.devices.isEmpty { try c.encode(content.devices, forKey: .devices) }
        if !content.workouts.isEmpty { try c.encode(content.workouts, forKey: .workouts) }
        if !content.routes.isEmpty { try c.encode(content.routes, forKey: .routes) }
        if !content.ecgs.isEmpty { try c.encode(content.ecgs, forKey: .ecgs) }
        if !content.activitySummaries.isEmpty { try c.encode(content.activitySummaries, forKey: .activitySummaries) }
        if !content.dailyStatistics.isEmpty { try c.encode(content.dailyStatistics, forKey: .dailyStatistics) }
    }

    public func body() throws -> Data {
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        // JSON has no NaN or infinity. Throwing would stop the whole stream on
        // one odd value from another app, page after page; the Mac stores null
        // in the number column and keeps the text.
        enc.nonConformingFloatEncodingStrategy = .convertToString(positiveInfinity: "Infinity",
                                                                  negativeInfinity: "-Infinity", nan: "NaN")
        return try enc.encode(self)
    }
}

// MARK: - The receiver's reply
/// heliosd replies {"ack": true, ...counts}; the M1 spool replies {"ack": true,
/// "spooled": name, "queued": n}. Only ack == true counts as delivered.
public struct AckReply: Decodable, Equatable {
    public var ack: Bool
    public var spooled: String?
    public var queued: Int?
    public var accepted: Int?
    public var guarded: Int?
    public var raw: RawCounts?

    public struct RawCounts: Decodable, Equatable {
        public var written: Int?
        public var existing: Int?

        public init(written: Int? = nil, existing: Int? = nil) {
            self.written = written
            self.existing = existing
        }
    }

    public init(ack: Bool, spooled: String? = nil, queued: Int? = nil, accepted: Int? = nil, guarded: Int? = nil,
                raw: RawCounts? = nil) {
        self.ack = ack
        self.spooled = spooled
        self.queued = queued
        self.accepted = accepted
        self.guarded = guarded
        self.raw = raw
    }

    public var viaRelay: Bool { spooled != nil }
}
