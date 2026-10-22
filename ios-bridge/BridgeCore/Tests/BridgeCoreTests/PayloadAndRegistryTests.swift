import Foundation
import HealthKit
import XCTest
@testable import BridgeCore

final class PayloadTests: XCTestCase {
    func batch(_ content: BatchContent) throws -> [String: Any] {
        let meta = BridgeMeta(appVersion: "1.1", build: "2", osVersion: "27.0.1", zone: "Pacific/Auckland",
                              reason: "observer", origin: .anchored, stream: "S", generation: 3)
        let body = try BatchV2(batchID: "B", sentAt: Date(timeIntervalSince1970: 0), bridge: meta, content: content).body()
        return try XCTUnwrap(try JSONSerialization.jsonObject(with: body) as? [String: Any])
    }

    func testTheV1FieldsOfASampleAreUnchanged() throws {
        var c = BatchContent()
        let src = c.sourceKey(SourceInfo(name: "Owner’s Ultra 1", bundleID: "com.apple.health.x", version: "11.0",
                                         productType: "Watch6,18", osVersion: "11.0.0"))
        let dev = c.deviceKey(DeviceInfo(desc: "<<HKDevice: 0x1>, name:Apple Watch>", name: "Apple Watch"))
        c.samples = [SampleV2(uuid: "U1", hkType: "HKQuantityTypeIdentifierHeartRate", kind: "quantity",
                              value: .number(61.5), unit: "count/min", start: "2026-10-21T01:02:03.456Z",
                              end: "2026-10-21T01:02:03.456Z", sourceName: "Owner’s Ultra 1", src: src, dev: dev,
                              metadata: [["HKMetadataKeyHeartRateMotionContext", "1"]],
                              syncIdentifier: nil, syncVersion: nil, writerID: "com.apple.health.x")]
        let obj = try batch(c)
        XCTAssertEqual(obj["schema"] as? Int, 2)
        XCTAssertEqual(obj["batch_id"] as? String, "B")
        XCTAssertEqual(obj["sync_path"] as? String, "bridge")
        XCTAssertEqual(obj["deleted"] as? [String], [])
        let s = try XCTUnwrap((obj["samples"] as? [[String: Any]])?.first)
        // Exactly what a v1 Bridge sent for the same sample, so the samples path is unchanged.
        let v1 = SampleDTOV1(uuid: "U1", hk_type: "HKQuantityTypeIdentifierHeartRate", value: .number(61.5),
                             unit: "count/min", start: "2026-10-21T01:02:03.456Z", end: "2026-10-21T01:02:03.456Z",
                             source_name: "Owner’s Ultra 1")
        let v1obj = try XCTUnwrap(try JSONSerialization.jsonObject(with: JSONEncoder().encode(v1)) as? [String: Any])
        for (k, v) in v1obj {
            XCTAssertEqual("\(s[k] ?? "absent")", "\(v)", "field \(k)")
        }
        XCTAssertEqual(s["src"] as? String, "s1")
        XCTAssertEqual(((obj["sources"] as? [String: Any])?["s1"] as? [String: Any])?["product_type"] as? String,
                       "Watch6,18")
        XCTAssertEqual(((obj["devices"] as? [String: Any])?["d1"] as? [String: Any])?["desc"] as? String,
                       "<<HKDevice: 0x1>, name:Apple Watch>")
        XCTAssertNil(obj["workouts"], "empty classes are left out")
    }

    func testOneSourceAndDeviceKeyPerDistinctDescriptor() {
        var c = BatchContent()
        let a = SourceInfo(name: "A", bundleID: "a", version: nil, productType: nil, osVersion: "1")
        let b = SourceInfo(name: "B", bundleID: "b", version: nil, productType: nil, osVersion: "1")
        XCTAssertEqual(c.sourceKey(a), "s1")
        XCTAssertEqual(c.sourceKey(b), "s2")
        XCTAssertEqual(c.sourceKey(a), "s1")
        XCTAssertEqual(c.sources.count, 2)
    }

    func testRoutePointsAreCompactArraysAndRoundTrip() throws {
        let p = RoutePointV2(t: "2026-10-21T01:00:00.000Z", lat: 25.2, lon: 55.3, alt: 4.5, hAcc: 3, vAcc: nil,
                             speed: 1.2, course: nil, speedAcc: nil, courseAcc: nil)
        let data = try JSONEncoder().encode(p)
        XCTAssertEqual(String(decoding: data, as: UTF8.self),
                       #"["2026-10-21T01:00:00.000Z",25.2,55.3,4.5,3,null,1.2,null,null,null]"#)
        XCTAssertEqual(try JSONDecoder().decode(RoutePointV2.self, from: data), p)
    }

    func testEveryClassEncodesWithItsWireKeys() throws {
        var c = BatchContent()
        c.workouts = [WorkoutV2(uuid: "W", activityType: 52, activityName: "HKWorkoutActivityTypeWalking",
                                start: "s", end: "e", durationS: 600, sourceName: "Watch",
                                statistics: [WorkoutStatV2(hkType: "HKQuantityTypeIdentifierHeartRate", start: "s",
                                                           end: "e", avg: 110, min: 90, max: 140, unit: "count/min")],
                                events: [WorkoutEventV2(type: 1, typeName: "HKWorkoutEventTypePause", start: "s", end: "s")],
                                routeUUIDs: ["R"])]
        c.routes = [RouteV2(uuid: "R", workoutUUID: "W", start: "s", end: "e", sourceName: "Watch", points: [])]
        c.ecgs = [ECGV2(uuid: "E", start: "s", end: "e", classification: 1, classificationName: "sinusRhythm",
                        symptomsStatus: 1, symptomsName: "none", averageHR: 62, samplingHz: 512, nVoltages: 2,
                        sourceName: "Watch", lead: "appleWatchSimilarToLeadI", microvolts: [1.5, nil])]
        c.activitySummaries = [ActivitySummaryV2(date: "2026-10-21", activeEnergy: 400, activeEnergyGoal: 600)]
        c.dailyStatistics = [DailyStatisticV2(hkType: "HKQuantityTypeIdentifierStepCount", day: "2026-10-21",
                                              zone: "Asia/Dubai", value: 5230, unit: "count", complete: true)]
        let obj = try batch(c)
        let w = try XCTUnwrap((obj["workouts"] as? [[String: Any]])?.first)
        XCTAssertEqual(w["activity_type"] as? Int, 52)
        XCTAssertEqual(w["route_uuids"] as? [String], ["R"])
        XCTAssertEqual(((w["statistics"] as? [[String: Any]])?.first)?["hk_type"] as? String,
                       "HKQuantityTypeIdentifierHeartRate")
        XCTAssertEqual(((obj["routes"] as? [[String: Any]])?.first)?["workout_uuid"] as? String, "W")
        let e = try XCTUnwrap((obj["ecgs"] as? [[String: Any]])?.first)
        XCTAssertEqual(e["sampling_hz"] as? Double, 512)
        XCTAssertEqual((e["microvolts"] as? [Any])?.count, 2)
        XCTAssertEqual(((obj["activity_summaries"] as? [[String: Any]])?.first)?["active_energy_goal"] as? Double, 600)
        XCTAssertEqual(((obj["daily_statistics"] as? [[String: Any]])?.first)?["zone"] as? String, "Asia/Dubai")
        XCTAssertEqual((obj["bridge"] as? [String: Any])?["origin"] as? String, "anchored")
        XCTAssertEqual(c.types, ["HKActivitySummaryTypeIdentifier", "HKDataTypeIdentifierElectrocardiogram",
                                 "HKQuantityTypeIdentifierStepCount", "HKWorkoutRouteTypeIdentifier",
                                 "HKWorkoutTypeIdentifier"])
    }

    func testNonFiniteNumbersAreWrittenAsTextNeverAThrow() throws {
        var content = BatchContent()
        content.samples = [Double.nan, .infinity, -.infinity].enumerated().map { i, v in
            SampleV2(uuid: "u\(i)", hkType: "HKQuantityTypeIdentifierHeartRate", kind: "quantity", value: .number(v),
                     unit: "count/min", start: "2026-10-21T01:02:03.000Z", end: "2026-10-21T01:02:03.000Z",
                     sourceName: "Synthetic Watch")
        }
        let meta = BridgeMeta(appVersion: "t", build: "0", osVersion: "t", zone: "UTC", reason: "t", origin: .anchored)
        let body = try BatchV2(sentAt: Date(timeIntervalSince1970: 0), bridge: meta, content: content).body()
        let obj = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: Any])
        let values = (obj["samples"] as? [[String: Any]])?.map { $0["value"] as? String }
        XCTAssertEqual(values, ["NaN", "Infinity", "-Infinity"])
    }

    func testTheSpoolAckAndTheDaemonAckBothDecode() throws {
        let spool = try JSONDecoder().decode(AckReply.self, from: Data(#"{"ack": true, "batch_id": "b", "spooled": "x.json", "queued": 3}"#.utf8))
        XCTAssertTrue(spool.ack && spool.viaRelay)
        XCTAssertEqual(spool.queued, 3)
        let daemon = try JSONDecoder().decode(AckReply.self, from: Data(#"{"ack": true, "accepted": 5, "guarded": 2, "raw": {"written": 7, "existing": 0}}"#.utf8))
        XCTAssertFalse(daemon.viaRelay)
        XCTAssertEqual(daemon.raw?.written, 7)
    }
}

final class RegistryTests: XCTestCase {
    func testKeysAreUniqueAndTheV1TypesKeepTheirAnchors() {
        XCTAssertEqual(Set(TypeRegistry.all.map(\.key)).count, TypeRegistry.all.count)
        XCTAssertEqual(TypeRegistry.registered.count, 18)
        for def in TypeRegistry.registered {
            XCTAssertEqual(def.key, def.identifier, "a v1 type keeps its anchor file name")
            XCTAssertTrue(def.trailingSweep)
        }
        for def in TypeRegistry.classes {
            XCTAssertTrue(def.key.hasPrefix("v2."), "a new class drains from the beginning")
        }
        XCTAssertEqual(TypeRegistry.classes.first?.identifier, HKObjectType.workoutType().identifier)
        XCTAssertEqual(TypeRegistry.classes[1].identifier, HKSeriesType.workoutRoute().identifier)
        XCTAssertEqual(TypeRegistry.classes[2].identifier, HKObjectType.electrocardiogramType().identifier)
    }

    func testTheV1WireUnitsAreKept() {
        let wire = Dictionary(uniqueKeysWithValues: TypeRegistry.registered.map { ($0.identifier, $0.wireUnit) })
        XCTAssertEqual(wire["HKQuantityTypeIdentifierVO2Max"], "mL/kg*min")
        XCTAssertEqual(wire["HKQuantityTypeIdentifierOxygenSaturation"], "%")
        XCTAssertEqual(wire["HKCategoryTypeIdentifierSleepAnalysis"], "")
        XCTAssertEqual(wire["HKQuantityTypeIdentifierBodyMassIndex"], "count")
    }

    /// HealthKit on the Mac knows every identifier, and every read unit fits its
    /// type (an incompatible unit would make doubleValue(for:) raise on the phone).
    func testEveryQuantityUnitFitsItsType() {
        for def in TypeRegistry.registered + TypeRegistry.extraQuantities where def.cls == .quantity {
            guard let type = HKObjectType.quantityType(forIdentifier: HKQuantityTypeIdentifier(rawValue: def.identifier)) else {
                XCTFail("unknown identifier \(def.identifier)")
                continue
            }
            let unit = HKUnit(from: def.readUnit ?? "")
            XCTAssertTrue(type.is(compatibleWith: unit), "\(def.identifier) cannot be read in \(def.readUnit ?? "nil")")
        }
    }

    func testEveryCategoryIdentifierExists() {
        for def in TypeRegistry.extraCategories + TypeRegistry.registered where def.cls == .category {
            XCTAssertNotNil(HKObjectType.categoryType(forIdentifier: HKCategoryTypeIdentifier(rawValue: def.identifier)),
                            def.identifier)
        }
    }

    func testTheDailyTotalUnitsFit() {
        for (id, unit) in DailyStatisticTypes.identifiers {
            let type = HKObjectType.quantityType(forIdentifier: HKQuantityTypeIdentifier(rawValue: id))!
            XCTAssertTrue(type.is(compatibleWith: HKUnit(from: unit)))
            XCTAssertEqual(type.aggregationStyle, .cumulative)
        }
    }
}
