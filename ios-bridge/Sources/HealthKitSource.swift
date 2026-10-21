import CoreLocation
import Foundation
import HealthKit
import BridgeCore

// MARK: - HealthKit as a record source
// The per-class adapters of the lossless envelope (plan v2 Phase 3): every
// sample with its true type (categories too, not only sleep), metadata, device,
// source revision, sync identifier and version, millisecond instants; workouts
// with statistics, events, activities and their routes; routes with every
// location and the workout HealthKit associates them with; ECGs with their
// voltages; activity summaries; Apple's own daily totals. Queries run on
// HealthKit's queues, never on the main thread.
final class HealthKitSource: RecordSource {
    let store: HKHealthStore

    init(store: HKHealthStore) {
        self.store = store
    }

    // MARK: Anchored pages

    func anchoredPage(_ def: StreamDef, anchor: Data?, limit: Int) async throws -> AnchoredPage {
        guard let type = HealthTypes.sampleType(def) else {
            throw SourceError.unavailable("\(def.shortName) is not known to this iOS")
        }
        let hkAnchor = anchor.flatMap { try? NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: $0) }
        let (added, deleted, newAnchor) = try await runAnchored(type: type, anchor: hkAnchor, limit: limit)
        var content = BatchContent()
        let retry = try await convert(added, def, into: &content)
        for sample in added { content.widen(start: sample.startDate, end: sample.endDate) }
        content.deleted = deleted.map { $0.uuid.uuidString }
        let anchorData = try NSKeyedArchiver.archivedData(withRootObject: newAnchor, requiringSecureCoding: true)
        return AnchoredPage(content: content, newAnchor: anchorData, retry: retry)
    }

    // MARK: Records by uuid (the read-again list)

    func records(_ def: StreamDef, uuids: [String]) async throws -> BatchContent {
        guard let type = HealthTypes.sampleType(def) else {
            throw SourceError.unavailable("\(def.shortName) is not known to this iOS")
        }
        let ids = Set(uuids.compactMap(UUID.init(uuidString:)))
        guard !ids.isEmpty else { return BatchContent() }
        let found = try await runSampleQuery(type: type, predicate: HKQuery.predicateForObjects(with: ids))
        var content = BatchContent()
        _ = try await convert(found, def, into: &content)   // parts still unreadable: left out, still held
        for sample in found { content.widen(start: sample.startDate, end: sample.endDate) }
        return content
    }

    /// Converts records into the page. A record whose parts (a workout's routes,
    /// a route's points, an ECG's voltages) cannot be read is left out and its
    /// uuid returned, to be read again by uuid later; one bad record never stops
    /// the stream. A locked phone stops the whole page: nothing moves, and the
    /// page is read again after the unlock.
    private func convert(_ added: [HKSample], _ def: StreamDef, into content: inout BatchContent) async throws
        -> [String] {
        var retry: [String] = []
        switch def.cls {
        case .quantity, .category:
            for sample in added {
                if let dto = sampleDTO(sample, def, &content) { content.samples.append(dto) }
            }
        case .workout:
            for workout in added.compactMap({ $0 as? HKWorkout }) {
                do {
                    content.workouts.append(try await workoutDTO(workout, &content))
                } catch SourceError.locked {
                    throw SourceError.locked
                } catch {
                    retry.append(workout.uuid.uuidString)
                }
            }
        case .route:
            for route in added.compactMap({ $0 as? HKWorkoutRoute }) {
                do {
                    content.routes.append(try await routeDTO(route, &content))
                } catch SourceError.locked {
                    throw SourceError.locked
                } catch {
                    retry.append(route.uuid.uuidString)
                }
            }
        case .ecg:
            for ecg in added.compactMap({ $0 as? HKElectrocardiogram }) {
                do {
                    content.ecgs.append(try await ecgDTO(ecg, &content))
                } catch SourceError.locked {
                    throw SourceError.locked
                } catch {
                    retry.append(ecg.uuid.uuidString)
                }
            }
        }
        return retry
    }

    // MARK: Dated windows

    func windowPages(_ kind: WindowKind, start: Date, end: Date, limit: Int) async throws -> [BatchContent] {
        switch kind {
        case .samples(let def):
            guard let type = HealthTypes.sampleType(def) else { return [] }
            let predicate = HKQuery.predicateForSamples(withStart: start, end: end, options: .strictStartDate)
            let samples = try await runSampleQuery(type: type, predicate: predicate)
            var pages: [BatchContent] = []
            var i = 0
            while i < samples.count {
                var content = BatchContent()
                for sample in samples[i..<min(i + limit, samples.count)] {
                    if let dto = sampleDTO(sample, def, &content) { content.samples.append(dto) }
                    content.widen(start: sample.startDate, end: sample.endDate)
                }
                pages.append(content)
                i += limit
            }
            return pages
        case .activitySummaries:
            let summaries = try await activitySummaries(start: start, end: end)
            return summaries.isEmpty ? [] : stride(from: 0, to: summaries.count, by: 400).map { i in
                var content = BatchContent()
                content.activitySummaries = Array(summaries[i..<min(i + 400, summaries.count)])
                return content
            }
        case .dailyStatistics:
            var content = BatchContent()
            for (identifier, unit) in DailyStatisticTypes.identifiers {
                content.dailyStatistics += try await dailyTotals(identifier: identifier, unit: unit, start: start, end: end)
            }
            return content.isEmpty ? [] : [content]
        }
    }

    // MARK: Sample conversion

    private func describe(_ source: HKSourceRevision, _ device: HKDevice?, into content: inout BatchContent)
        -> (src: String, dev: String?) {
        let os = source.operatingSystemVersion
        let src = content.sourceKey(SourceInfo(
            name: source.source.name, bundleID: source.source.bundleIdentifier, version: source.version,
            productType: source.productType, osVersion: "\(os.majorVersion).\(os.minorVersion).\(os.patchVersion)"))
        let dev = device.map {
            content.deviceKey(DeviceInfo(desc: String(describing: $0), name: $0.name, manufacturer: $0.manufacturer,
                                         model: $0.model, hardware: $0.hardwareVersion, firmware: $0.firmwareVersion,
                                         software: $0.softwareVersion, localID: $0.localIdentifier,
                                         udi: $0.udiDeviceIdentifier))
        }
        return (src, dev)
    }

    static func pairs(_ metadata: [String: Any]?) -> MetadataPairs? {
        guard let metadata, !metadata.isEmpty else { return nil }
        return metadata.keys.sorted().map { key in [key, text(metadata[key] as Any)] }
    }

    static func text(_ value: Any) -> String {
        switch value {
        case let s as String: return s
        case let q as HKQuantity: return q.description
        case let d as Date: return WireTime.string(d)
        case let n as NSNumber: return n.stringValue
        default: return String(describing: value)
        }
    }

    static func syncFields(_ metadata: [String: Any]?) -> (String?, Int?) {
        let id = metadata?[HKMetadataKeySyncIdentifier] as? String
        let version = (metadata?[HKMetadataKeySyncVersion] as? NSNumber)?.intValue
        return (id, version)
    }

    private func sampleDTO(_ sample: HKSample, _ def: StreamDef, _ content: inout BatchContent) -> SampleV2? {
        let (src, dev) = describe(sample.sourceRevision, sample.device, into: &content)
        let (syncID, syncVersion) = Self.syncFields(sample.metadata)
        var dto = SampleV2(uuid: sample.uuid.uuidString, hkType: sample.sampleType.identifier, kind: "quantity",
                           value: nil, unit: def.wireUnit, start: WireTime.string(sample.startDate),
                           end: WireTime.string(sample.endDate), sourceName: sample.sourceRevision.source.name,
                           src: src, dev: dev, metadata: Self.pairs(sample.metadata), syncIdentifier: syncID,
                           syncVersion: syncVersion, writerID: sample.sourceRevision.source.bundleIdentifier)
        if let quantity = sample as? HKQuantitySample {
            let unit = HKUnit(from: def.readUnit ?? "count")
            if quantity.quantity.is(compatibleWith: unit) {
                dto.value = .number(quantity.quantity.doubleValue(for: unit))
            } else {
                dto.note = "unit \(def.readUnit ?? "?") does not fit \(quantity.quantity)"
            }
            return dto
        }
        if let category = sample as? HKCategorySample {
            dto.kind = "category"
            dto.categoryValue = category.value
            dto.unit = ""
            if category.categoryType.identifier == HKCategoryTypeIdentifier.sleepAnalysis.rawValue {
                dto.value = .string(SleepValue.name(for: category.value))
            } else {
                dto.value = .number(Double(category.value))
            }
            return dto
        }
        return nil
    }

    // MARK: Workouts

    private func statDTO(_ type: HKQuantityType, _ stat: HKStatistics) -> WorkoutStatV2? {
        let unit = Self.unit(for: type)
        guard let unit else { return nil }
        func v(_ q: HKQuantity?) -> Double? { q.map { $0.doubleValue(for: unit) } }
        return WorkoutStatV2(hkType: type.identifier, start: WireTime.string(stat.startDate),
                             end: WireTime.string(stat.endDate), sum: v(stat.sumQuantity()),
                             avg: v(stat.averageQuantity()), min: v(stat.minimumQuantity()),
                             max: v(stat.maximumQuantity()), mostRecent: v(stat.mostRecentQuantity()),
                             unit: unit.unitString)
    }

    /// The registry's unit for the type, else the first common unit it fits.
    static func unit(for type: HKQuantityType) -> HKUnit? {
        if let s = TypeRegistry.readUnit(forQuantity: type.identifier) {
            let u = HKUnit(from: s)
            if type.is(compatibleWith: u) { return u }
        }
        let candidates = ["count/min", "kcal", "m", "count", "s", "W", "m/s", "%", "degC", "kg", "mL/kg·min", "dBASPL"]
        return candidates.map { HKUnit(from: $0) }.first { type.is(compatibleWith: $0) }
    }

    private func eventDTO(_ event: HKWorkoutEvent) -> WorkoutEventV2 {
        WorkoutEventV2(type: event.type.rawValue, typeName: HKNames.event(event.type),
                       start: WireTime.string(event.dateInterval.start), end: WireTime.string(event.dateInterval.end),
                       metadata: Self.pairs(event.metadata))
    }

    private func workoutDTO(_ workout: HKWorkout, _ content: inout BatchContent) async throws -> WorkoutV2 {
        let (src, dev) = describe(workout.sourceRevision, workout.device, into: &content)
        let (syncID, syncVersion) = Self.syncFields(workout.metadata)
        let stats = workout.allStatistics.compactMap { statDTO($0.key, $0.value) }.sorted { $0.hkType < $1.hkType }
        let activities = workout.workoutActivities.map { a in
            WorkoutActivityV2(uuid: a.uuid.uuidString, activityType: a.workoutConfiguration.activityType.rawValue,
                              activityName: HKNames.activity(a.workoutConfiguration.activityType),
                              start: WireTime.string(a.startDate), end: a.endDate.map(WireTime.string),
                              durationS: a.duration, metadata: Self.pairs(a.metadata),
                              statistics: a.allStatistics.compactMap { statDTO($0.key, $0.value) }
                                  .sorted { $0.hkType < $1.hkType },
                              events: a.workoutEvents.map(eventDTO))
        }
        let routes = try await routes(of: workout)
        let totals = Self.totals(workout)
        return WorkoutV2(uuid: workout.uuid.uuidString, activityType: workout.workoutActivityType.rawValue,
                         activityName: HKNames.activity(workout.workoutActivityType),
                         start: WireTime.string(workout.startDate), end: WireTime.string(workout.endDate),
                         durationS: workout.duration, sourceName: workout.sourceRevision.source.name, src: src,
                         dev: dev, metadata: Self.pairs(workout.metadata), syncIdentifier: syncID,
                         syncVersion: syncVersion, writerID: workout.sourceRevision.source.bundleIdentifier,
                         statistics: stats, events: (workout.workoutEvents ?? []).map(eventDTO),
                         activities: activities, routeUUIDs: routes.map(\.uuid.uuidString),
                         totalEnergyKcal: totals.kcal, totalDistanceM: totals.metres)
    }

    /// The workout's own totals. Deprecated in iOS 18 in favour of statistics,
    /// but they are what older workouts carry and what the export prints.
    @available(iOS, deprecated: 18.0)
    private static func totals(_ workout: HKWorkout) -> (kcal: Double?, metres: Double?) {
        (workout.totalEnergyBurned?.doubleValue(for: .kilocalorie()), workout.totalDistance?.doubleValue(for: .meter()))
    }

    /// The routes HealthKit itself associates with a workout.
    private func routes(of workout: HKWorkout) async throws -> [HKSample] {
        try await runSampleQuery(type: HKSeriesType.workoutRoute(), predicate: HKQuery.predicateForObjects(from: workout))
    }

    // MARK: Routes

    private func routeDTO(_ route: HKWorkoutRoute, _ content: inout BatchContent) async throws -> RouteV2 {
        let (src, dev) = describe(route.sourceRevision, route.device, into: &content)
        let locations = try await locations(of: route)
        let points = locations.map { l in
            RoutePointV2(t: WireTime.string(l.timestamp), lat: l.coordinate.latitude, lon: l.coordinate.longitude,
                         alt: l.verticalAccuracy >= 0 ? l.altitude : nil,
                         hAcc: l.horizontalAccuracy >= 0 ? l.horizontalAccuracy : nil,
                         vAcc: l.verticalAccuracy >= 0 ? l.verticalAccuracy : nil,
                         speed: l.speed >= 0 ? l.speed : nil, course: l.course >= 0 ? l.course : nil,
                         speedAcc: l.speedAccuracy >= 0 ? l.speedAccuracy : nil,
                         courseAcc: l.courseAccuracy >= 0 ? l.courseAccuracy : nil)
        }
        return RouteV2(uuid: route.uuid.uuidString, workoutUUID: try await owningWorkout(of: route),
                       start: WireTime.string(route.startDate), end: WireTime.string(route.endDate),
                       sourceName: route.sourceRevision.source.name, src: src, dev: dev,
                       metadata: Self.pairs(route.metadata), points: points)
    }

    private func locations(of route: HKWorkoutRoute) async throws -> [CLLocation] {
        try await withCheckedThrowingContinuation { continuation in
            var all: [CLLocation] = []
            var finished = false
            let query = HKWorkoutRouteQuery(route: route) { _, locations, done, error in
                guard !finished else { return }
                if let error {
                    finished = true
                    continuation.resume(throwing: Self.map(error))
                    return
                }
                all.append(contentsOf: locations ?? [])
                if done {
                    finished = true
                    continuation.resume(returning: all)
                }
            }
            store.execute(query)
        }
    }

    /// The workout this route belongs to, proven by asking HealthKit for each
    /// nearby workout's routes; nil when no workout claims it. Never by time alone.
    private func owningWorkout(of route: HKWorkoutRoute) async throws -> String? {
        let window = HKQuery.predicateForSamples(withStart: route.startDate.addingTimeInterval(-6 * 3600),
                                                 end: route.endDate.addingTimeInterval(6 * 3600), options: [])
        let candidates = try await runSampleQuery(type: HKObjectType.workoutType(), predicate: window)
            .compactMap { $0 as? HKWorkout }
            .sorted { abs($0.startDate.timeIntervalSince(route.startDate)) < abs($1.startDate.timeIntervalSince(route.startDate)) }
        for workout in candidates.prefix(5) {
            if try await routes(of: workout).contains(where: { $0.uuid == route.uuid }) {
                return workout.uuid.uuidString
            }
        }
        return nil
    }

    // MARK: ECGs

    private func ecgDTO(_ ecg: HKElectrocardiogram, _ content: inout BatchContent) async throws -> ECGV2 {
        let (src, dev) = describe(ecg.sourceRevision, ecg.device, into: &content)
        let microvolts = try await voltages(of: ecg)
        let bpm = HKUnit.count().unitDivided(by: .minute())
        return ECGV2(uuid: ecg.uuid.uuidString, start: WireTime.string(ecg.startDate), end: WireTime.string(ecg.endDate),
                     classification: ecg.classification.rawValue, classificationName: HKNames.classification(ecg.classification),
                     symptomsStatus: ecg.symptomsStatus.rawValue, symptomsName: HKNames.symptoms(ecg.symptomsStatus),
                     averageHR: ecg.averageHeartRate?.doubleValue(for: bpm),
                     samplingHz: ecg.samplingFrequency?.doubleValue(for: .hertz()),
                     nVoltages: ecg.numberOfVoltageMeasurements, sourceName: ecg.sourceRevision.source.name,
                     src: src, dev: dev, metadata: Self.pairs(ecg.metadata), lead: "appleWatchSimilarToLeadI",
                     unit: HKUnit.voltUnit(with: .micro).unitString, microvolts: microvolts)
    }

    private func voltages(of ecg: HKElectrocardiogram) async throws -> [Double?] {
        try await withCheckedThrowingContinuation { continuation in
            var values: [Double?] = []
            values.reserveCapacity(ecg.numberOfVoltageMeasurements)
            var finished = false
            let micro = HKUnit.voltUnit(with: .micro)
            let query = HKElectrocardiogramQuery(ecg) { _, result in
                guard !finished else { return }
                switch result {
                case .measurement(let m):
                    values.append(m.quantity(for: .appleWatchSimilarToLeadI)?.doubleValue(for: micro))
                case .done:
                    finished = true
                    continuation.resume(returning: values)
                case .error(let error):
                    finished = true
                    continuation.resume(throwing: Self.map(error))
                @unknown default:
                    finished = true
                    continuation.resume(throwing: SourceError.unavailable("unknown ECG query result"))
                }
            }
            store.execute(query)
        }
    }

    // MARK: Activity summaries

    private func activitySummaries(start: Date, end: Date) async throws -> [ActivitySummaryV2] {
        let cal = Calendar(identifier: .gregorian)
        var from = cal.dateComponents([.era, .year, .month, .day], from: start)
        from.calendar = cal
        var to = cal.dateComponents([.era, .year, .month, .day], from: end)
        to.calendar = cal
        let predicate = HKQuery.predicate(forActivitySummariesBetweenStart: from, end: to)
        let summaries: [HKActivitySummary] = try await withCheckedThrowingContinuation { continuation in
            let query = HKActivitySummaryQuery(predicate: predicate) { _, summaries, error in
                if let error {
                    continuation.resume(throwing: Self.map(error))
                } else {
                    continuation.resume(returning: summaries ?? [])
                }
            }
            store.execute(query)
        }
        let kcal = HKUnit.kilocalorie(), minute = HKUnit.minute(), count = HKUnit.count()
        return summaries.compactMap { s in
            let c = s.dateComponents(for: cal)
            guard let y = c.year, let m = c.month, let d = c.day else { return nil }
            return ActivitySummaryV2(
                date: String(format: "%04d-%02d-%02d", y, m, d), era: c.era,
                activeEnergy: s.activeEnergyBurned.doubleValue(for: kcal),
                activeEnergyGoal: s.activeEnergyBurnedGoal.doubleValue(for: kcal),
                moveTime: s.appleMoveTime.doubleValue(for: minute),
                moveTimeGoal: s.appleMoveTimeGoal.doubleValue(for: minute),
                exerciseTime: s.appleExerciseTime.doubleValue(for: minute),
                exerciseTimeGoal: s.exerciseTimeGoal?.doubleValue(for: minute),
                standHours: s.appleStandHours.doubleValue(for: count),
                standHoursGoal: s.standHoursGoal?.doubleValue(for: count),
                moveMode: s.activityMoveMode.rawValue)
        }
    }

    // MARK: Apple's daily totals (C9)

    private func dailyTotals(identifier: String, unit unitString: String, start: Date, end: Date) async throws
        -> [DailyStatisticV2] {
        guard let type = HKObjectType.quantityType(forIdentifier: HKQuantityTypeIdentifier(rawValue: identifier)),
              let zone = TimeZone(identifier: DailyStatisticTypes.zone) else { return [] }
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = zone
        let from = cal.startOfDay(for: start)
        let unit = HKUnit(from: unitString)
        let predicate = HKQuery.predicateForSamples(withStart: from, end: end, options: [])
        let collection: HKStatisticsCollection = try await withCheckedThrowingContinuation { continuation in
            let query = HKStatisticsCollectionQuery(quantityType: type, quantitySamplePredicate: predicate,
                                                    options: [.cumulativeSum, .separateBySource], anchorDate: from,
                                                    intervalComponents: DateComponents(day: 1))
            query.initialResultsHandler = { _, collection, error in
                if let collection {
                    continuation.resume(returning: collection)
                } else {
                    continuation.resume(throwing: error.map(Self.map) ?? SourceError.unavailable("no statistics"))
                }
            }
            store.execute(query)
        }
        let dayFormat = DateFormatter()
        dayFormat.calendar = cal
        dayFormat.timeZone = zone
        dayFormat.locale = Locale(identifier: "en_US_POSIX")
        dayFormat.dateFormat = "yyyy-MM-dd"
        let now = Date()
        var out: [DailyStatisticV2] = []
        collection.enumerateStatistics(from: from, to: end) { stat, _ in
            let sources = stat.sources?.map { source -> [String] in
                [source.name, source.bundleIdentifier,
                 stat.sumQuantity(for: source).map { Self.number($0.doubleValue(for: unit)) } ?? ""]
            }
            out.append(DailyStatisticV2(hkType: identifier, day: dayFormat.string(from: stat.startDate),
                                        zone: DailyStatisticTypes.zone,
                                        value: stat.sumQuantity()?.doubleValue(for: unit), unit: unitString,
                                        complete: stat.endDate <= now, sources: sources))
        }
        return out
    }

    static func number(_ v: Double) -> String {
        v == v.rounded() && abs(v) < 1e15 ? String(Int64(v)) : String(v)
    }

    // MARK: Query plumbing

    /// Health data is unreadable while the phone is locked: say so plainly, so the
    /// engine waits for the unlock instead of counting an error (C1).
    static func map(_ error: Error) -> Error {
        if let hk = error as? HKError, hk.code == .errorDatabaseInaccessible { return SourceError.locked }
        return error
    }

    private func runAnchored(type: HKSampleType, anchor: HKQueryAnchor?, limit: Int) async throws
        -> (added: [HKSample], deleted: [HKDeletedObject], newAnchor: HKQueryAnchor) {
        try await withCheckedThrowingContinuation { continuation in
            let query = HKAnchoredObjectQuery(type: type, predicate: nil, anchor: anchor, limit: limit) {
                _, samples, deleted, newAnchor, error in
                if let error {
                    continuation.resume(throwing: Self.map(error))
                    return
                }
                // Never fall back to a zero anchor: that would reset the stream.
                guard let next = newAnchor ?? anchor else {
                    continuation.resume(throwing: SourceError.unavailable("HealthKit returned no anchor"))
                    return
                }
                continuation.resume(returning: (samples ?? [], deleted ?? [], next))
            }
            store.execute(query)
        }
    }

    private func runSampleQuery(type: HKSampleType, predicate: NSPredicate) async throws -> [HKSample] {
        try await withCheckedThrowingContinuation { continuation in
            let sort = NSSortDescriptor(key: HKSampleSortIdentifierStartDate, ascending: true)
            let query = HKSampleQuery(sampleType: type, predicate: predicate, limit: HKObjectQueryNoLimit,
                                      sortDescriptors: [sort]) { _, samples, error in
                if let error {
                    continuation.resume(throwing: Self.map(error))
                } else {
                    continuation.resume(returning: samples ?? [])
                }
            }
            store.execute(query)
        }
    }
}
