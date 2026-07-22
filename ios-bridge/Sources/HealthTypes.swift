import Foundation
import HealthKit

// MARK: - Type registry
// One definition per Health type we ingest. The canonical identifier string is
// what the Mac stores as hk_type. unitString is the exact unit token sent on the
// wire; unit is the HKUnit we read the sample's doubleValue in.
struct HealthTypeDef {
    let identifier: String        // e.g. "HKQuantityTypeIdentifierHeartRate"
    let sampleType: HKSampleType  // used for queries, observers, background delivery
    let unit: HKUnit?             // nil for category and workout types
    let unitString: String        // wire token, e.g. "count/min"
    let isCategory: Bool
    let isWorkout: Bool
    let frequency: HKUpdateFrequency // background delivery cadence
}

enum HealthTypes {

    // MARK: Canonical units
    private static let countPerMinute = HKUnit.count().unitDivided(by: .minute())
    private static let millisecond = HKUnit.secondUnit(with: .milli)
    private static let percent = HKUnit.percent()
    private static let degreesC = HKUnit.degreeCelsius()
    private static let mlPerKgMin = HKUnit.literUnit(with: .milli)
        .unitDivided(by: HKUnit.gramUnit(with: .kilo).unitMultiplied(by: HKUnit.minute()))
    private static let count = HKUnit.count()
    private static let kcal = HKUnit.kilocalorie()
    private static let mgPerDL = HKUnit.gramUnit(with: .milli)
        .unitDivided(by: HKUnit.literUnit(with: .deci))
    private static let kilogram = HKUnit.gramUnit(with: .kilo)

    // MARK: Builders
    private static func q(_ id: HKQuantityTypeIdentifier,
                          _ unit: HKUnit,
                          _ unitString: String,
                          _ frequency: HKUpdateFrequency = .hourly) -> HealthTypeDef {
        let type = HKQuantityType.quantityType(forIdentifier: id)!
        return HealthTypeDef(identifier: id.rawValue,
                             sampleType: type,
                             unit: unit,
                             unitString: unitString,
                             isCategory: false,
                             isWorkout: false,
                             frequency: frequency)
    }

    private static func c(_ id: HKCategoryTypeIdentifier,
                          _ frequency: HKUpdateFrequency = .hourly) -> HealthTypeDef {
        let type = HKCategoryType.categoryType(forIdentifier: id)!
        return HealthTypeDef(identifier: id.rawValue,
                             sampleType: type,
                             unit: nil,
                             unitString: "",
                             isCategory: true,
                             isWorkout: false,
                             frequency: frequency)
    }

    private static func workout(_ frequency: HKUpdateFrequency = .immediate) -> HealthTypeDef {
        HealthTypeDef(identifier: "HKWorkoutTypeIdentifier",
                      sampleType: HKObjectType.workoutType(),
                      unit: nil,
                      unitString: "s",
                      isCategory: false,
                      isWorkout: true,
                      frequency: frequency)
    }

    // MARK: Registry
    // Note on oxygen saturation and body fat percentage: HealthKit stores these as
    // a fraction in the range 0...1. Reading doubleValue(for: .percent()) returns
    // that fraction (0.98 means 98%). We send the raw fraction and the unit "%",
    // and the Mac is responsible for scaling to a human percentage.
    static let all: [HealthTypeDef] = [
        q(.heartRate, countPerMinute, "count/min", .immediate),
        q(.restingHeartRate, countPerMinute, "count/min"),
        q(.heartRateVariabilitySDNN, millisecond, "ms", .immediate),
        q(.respiratoryRate, countPerMinute, "count/min", .immediate),
        q(.oxygenSaturation, percent, "%", .immediate),          // fraction, Mac scales
        q(.bodyTemperature, degreesC, "degC", .immediate),
        q(.appleSleepingWristTemperature, degreesC, "degC"),
        q(.vo2Max, mlPerKgMin, "mL/kg*min"),
        q(.stepCount, count, "count"),
        q(.activeEnergyBurned, kcal, "kcal"),
        q(.basalEnergyBurned, kcal, "kcal"),
        c(.sleepAnalysis, .immediate),
        q(.bloodGlucose, mgPerDL, "mg/dL", .immediate),
        q(.bodyMass, kilogram, "kg"),
        q(.bodyMassIndex, count, "count"),
        q(.bodyFatPercentage, percent, "%"),                     // fraction, Mac scales
        q(.leanBodyMass, kilogram, "kg"),
        q(.dietaryEnergyConsumed, kcal, "kcal"),
        workout()
    ]

    /// The set of object types requested for read authorization.
    static let readTypes: Set<HKObjectType> = Set(all.map { $0.sampleType })

    /// Fast lookup from a sample's type identifier to its definition.
    static let byIdentifier: [String: HealthTypeDef] = {
        var map: [String: HealthTypeDef] = [:]
        for def in all { map[def.identifier] = def }
        return map
    }()

    /// Human-friendly short name for the status screen.
    static func shortName(_ identifier: String) -> String {
        identifier
            .replacingOccurrences(of: "HKQuantityTypeIdentifier", with: "")
            .replacingOccurrences(of: "HKCategoryTypeIdentifier", with: "")
            .replacingOccurrences(of: "HKWorkoutTypeIdentifier", with: "Workouts")
    }
}

// MARK: - Sleep value mapping
// Category sleep samples carry an integer raw value. The Mac wants the
// HKCategoryValueSleepAnalysis case NAME as a string, e.g.
// "HKCategoryValueSleepAnalysisAsleepDeep".
enum SleepValue {
    static func name(for rawValue: Int) -> String {
        guard let value = HKCategoryValueSleepAnalysis(rawValue: rawValue) else {
            return "HKCategoryValueSleepAnalysisUnknown"
        }
        switch value {
        case .inBed:              return "HKCategoryValueSleepAnalysisInBed"
        case .asleepUnspecified:  return "HKCategoryValueSleepAnalysisAsleepUnspecified"
        case .awake:              return "HKCategoryValueSleepAnalysisAwake"
        case .asleepCore:         return "HKCategoryValueSleepAnalysisAsleepCore"
        case .asleepDeep:         return "HKCategoryValueSleepAnalysisAsleepDeep"
        case .asleepREM:          return "HKCategoryValueSleepAnalysisAsleepREM"
        @unknown default:         return "HKCategoryValueSleepAnalysisUnknown"
        }
    }
}
