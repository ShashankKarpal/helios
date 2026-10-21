import Foundation

// MARK: - Stream definitions
// Every HealthKit sample class the Bridge drains by anchor. Plain strings here,
// so the logic stays testable on the Mac; the app maps them to HealthKit types
// (HKObjectType.quantityType(forIdentifier:) and friends return nil for an
// identifier this iOS does not know, and that stream is then skipped).
public enum StreamClass: String, Codable, Sendable {
    case quantity, category, workout, route, ecg
}

public enum BackgroundFrequency: String, Codable, Sendable {
    case immediate, hourly
    /// No background delivery: drained when the app runs (foreground, BG tasks).
    case none
}

public struct StreamDef: Equatable, Hashable, Sendable, Identifiable {
    /// The anchor's file name. The 19 v1 types keep their identifier, so their
    /// saved anchors carry over. Workouts take a new key: the v1 Bridge drained
    /// them as bare durations the Mac skipped, so their history must come again.
    public let key: String
    public let identifier: String
    public let cls: StreamClass
    /// Quantities: the HKUnit string the value is read in. For the extra types it
    /// is the unit the Apple Health export prints for the same type, so a Bridge
    /// raw row and an export raw row compare without conversion.
    public let readUnit: String?
    /// The unit token sent on the wire (the v1 tokens for the v1 types).
    public let wireUnit: String
    /// The Mac's policy maps the type to a metric today (samples path); the
    /// others land in the raw envelope only.
    public let registered: Bool
    public let frequency: BackgroundFrequency
    /// The 48-hour safety sweep, for the registered sample types only.
    public let trailingSweep: Bool
    /// Records per page.
    public let pageLimit: Int
    /// How far back the safety sweep reads, when it differs from the default
    /// 48 hours (blood glucose: its writers are the ones with records the
    /// anchored read has missed, and its volume is small).
    public var sweepWindow: TimeInterval? = nil

    public var id: String { key }

    public var shortName: String {
        identifier
            .replacingOccurrences(of: "HKQuantityTypeIdentifier", with: "")
            .replacingOccurrences(of: "HKCategoryTypeIdentifier", with: "")
            .replacingOccurrences(of: "HKWorkoutTypeIdentifier", with: "Workouts")
            .replacingOccurrences(of: "HKWorkoutRouteTypeIdentifier", with: "Workout routes")
            .replacingOccurrences(of: "HKDataTypeIdentifierElectrocardiogram", with: "ECG")
    }
}

public enum TypeRegistry {
    static let qPrefix = "HKQuantityTypeIdentifier"
    static let cPrefix = "HKCategoryTypeIdentifier"

    private static func reg(_ name: String, _ read: String, _ wire: String? = nil,
                            _ freq: BackgroundFrequency = .hourly) -> StreamDef {
        StreamDef(key: qPrefix + name, identifier: qPrefix + name, cls: .quantity, readUnit: read,
                  wireUnit: wire ?? read, registered: true, frequency: freq, trailingSweep: true, pageLimit: 2000)
    }

    private static func extra(_ name: String, _ unit: String) -> StreamDef {
        StreamDef(key: qPrefix + name, identifier: qPrefix + name, cls: .quantity, readUnit: unit, wireUnit: unit,
                  registered: false, frequency: .none, trailingSweep: false, pageLimit: 2000)
    }

    private static func cat(_ name: String) -> StreamDef {
        StreamDef(key: cPrefix + name, identifier: cPrefix + name, cls: .category, readUnit: nil, wireUnit: "",
                  registered: false, frequency: .none, trailingSweep: false, pageLimit: 2000)
    }

    /// The 18 sample types the v1 Bridge sent and the Mac registers (same order,
    /// same wire units: fractions for SpO2 and body fat, the Mac scales them).
    public static let registered: [StreamDef] = [
        reg("HeartRate", "count/min", nil, .immediate),
        reg("RestingHeartRate", "count/min"),
        reg("HeartRateVariabilitySDNN", "ms", nil, .immediate),
        reg("RespiratoryRate", "count/min", nil, .immediate),
        reg("OxygenSaturation", "%", nil, .immediate),
        reg("BodyTemperature", "degC", nil, .immediate),
        reg("AppleSleepingWristTemperature", "degC"),
        reg("VO2Max", "mL/kg·min", "mL/kg*min"),
        reg("StepCount", "count"),
        reg("ActiveEnergyBurned", "kcal"),
        reg("BasalEnergyBurned", "kcal"),
        StreamDef(key: cPrefix + "SleepAnalysis", identifier: cPrefix + "SleepAnalysis", cls: .category,
                  readUnit: nil, wireUnit: "", registered: true, frequency: .immediate, trailingSweep: true,
                  pageLimit: 2000),
        { var g = reg("BloodGlucose", "mg/dL", nil, .immediate); g.sweepWindow = 14 * 86400; return g }(),
        reg("BodyMass", "kg"),
        reg("BodyMassIndex", "count"),
        reg("BodyFatPercentage", "%"),
        reg("LeanBodyMass", "kg"),
        reg("DietaryEnergyConsumed", "kcal"),
    ]

    /// The classes Phase 3 adds. New keys, so they drain from the beginning.
    public static let classes: [StreamDef] = [
        StreamDef(key: "v2.HKWorkoutTypeIdentifier", identifier: "HKWorkoutTypeIdentifier", cls: .workout,
                  readUnit: nil, wireUnit: "s", registered: false, frequency: .immediate, trailingSweep: false,
                  pageLimit: 25),
        StreamDef(key: "v2.HKWorkoutRouteTypeIdentifier", identifier: "HKWorkoutRouteTypeIdentifier", cls: .route,
                  readUnit: nil, wireUnit: "", registered: false, frequency: .immediate, trailingSweep: false,
                  pageLimit: 2),
        StreamDef(key: "v2.HKDataTypeIdentifierElectrocardiogram", identifier: "HKDataTypeIdentifierElectrocardiogram",
                  cls: .ecg, readUnit: nil, wireUnit: "mcV", registered: false, frequency: .immediate,
                  trailingSweep: false, pageLimit: 4),
    ]

    /// Every other sample type the owner's devices write (the June export's 56
    /// unregistered types and the ones the Watch Ultra, the iPhone and the
    /// owner's apps can write from now on). Raw envelope only on the Mac.
    public static let extraQuantities: [StreamDef] = [
        extra("PhysicalEffort", "kcal/hr·kg"),
        extra("DistanceWalkingRunning", "km"),
        extra("AppleStandTime", "min"),
        extra("WalkingSpeed", "km/hr"),
        extra("WalkingStepLength", "cm"),
        extra("EnvironmentalAudioExposure", "dBASPL"),
        extra("AppleExerciseTime", "min"),
        extra("WalkingDoubleSupportPercentage", "%"),
        extra("WalkingAsymmetryPercentage", "%"),
        extra("HeadphoneAudioExposure", "dBASPL"),
        extra("TimeInDaylight", "min"),
        extra("FlightsClimbed", "count"),
        extra("DistanceCycling", "km"),
        extra("StairDescentSpeed", "m/s"),
        extra("StairAscentSpeed", "m/s"),
        extra("WalkingHeartRateAverage", "count/min"),
        extra("EnvironmentalSoundReduction", "dBASPL"),
        extra("AppleWalkingSteadiness", "%"),
        extra("SixMinuteWalkTestDistance", "m"),
        extra("DistanceSwimming", "m"),
        extra("SwimmingStrokeCount", "count"),
        extra("HeartRateRecoveryOneMinute", "count/min"),
        extra("AtrialFibrillationBurden", "%"),
        extra("Height", "cm"),
        extra("NumberOfAlcoholicBeverages", "count"),
        extra("AppleMoveTime", "min"),
        extra("RunningSpeed", "m/s"),
        extra("RunningPower", "W"),
        extra("RunningStrideLength", "m"),
        extra("RunningVerticalOscillation", "cm"),
        extra("RunningGroundContactTime", "ms"),
        extra("UnderwaterDepth", "m"),
        extra("WaterTemperature", "degC"),
        extra("CyclingSpeed", "m/s"),
        extra("CyclingPower", "W"),
        extra("CyclingCadence", "count/min"),
        extra("NumberOfTimesFallen", "count"),
        extra("AppleSleepingBreathingDisturbances", "count"),
        extra("BloodPressureSystolic", "mmHg"),
        extra("BloodPressureDiastolic", "mmHg"),
        extra("WaistCircumference", "cm"),
        extra("BasalBodyTemperature", "degC"),
        extra("DistanceDownhillSnowSports", "m"),
        extra("WorkoutEffortScore", "appleEffortScore"),
        extra("EstimatedWorkoutEffortScore", "appleEffortScore"),
        // Nutrition (units as the export prints them).
        extra("DietaryFatTotal", "g"),
        extra("DietaryCarbohydrates", "g"),
        extra("DietaryProtein", "g"),
        extra("DietaryFiber", "g"),
        extra("DietarySugar", "g"),
        extra("DietaryFatSaturated", "g"),
        extra("DietaryFatPolyunsaturated", "g"),
        extra("DietaryFatMonounsaturated", "g"),
        extra("DietaryCholesterol", "mg"),
        extra("DietarySodium", "mg"),
        extra("DietaryPotassium", "mg"),
        extra("DietaryVitaminC", "mg"),
        extra("DietaryCalcium", "mg"),
        extra("DietaryIron", "mg"),
        extra("DietaryVitaminA", "mcg"),
        extra("DietaryVitaminD", "mcg"),
        extra("DietaryVitaminB6", "mg"),
        extra("DietaryVitaminB12", "mcg"),
        extra("DietaryVitaminE", "mg"),
        extra("DietaryVitaminK", "mcg"),
        extra("DietaryThiamin", "mg"),
        extra("DietaryRiboflavin", "mg"),
        extra("DietaryNiacin", "mg"),
        extra("DietaryFolate", "mcg"),
        extra("DietaryPhosphorus", "mg"),
        extra("DietaryMagnesium", "mg"),
        extra("DietaryZinc", "mg"),
        extra("DietarySelenium", "mcg"),
        extra("DietaryCopper", "mg"),
        extra("DietaryManganese", "mg"),
        extra("DietaryCaffeine", "mg"),
        extra("DietaryWater", "mL"),
        extra("DietaryBiotin", "mcg"),
        extra("DietaryChromium", "mcg"),
        extra("DietaryIodine", "mcg"),
        extra("DietaryMolybdenum", "mcg"),
        extra("DietaryPantothenicAcid", "mg"),
        extra("DietaryChloride", "mg"),
    ]

    public static let extraCategories: [StreamDef] = [
        cat("AppleStandHour"),
        cat("AudioExposureEvent"),
        cat("HeadphoneAudioExposureEvent"),
        cat("HighHeartRateEvent"),
        cat("LowHeartRateEvent"),
        cat("IrregularHeartRhythmEvent"),
        cat("LowCardioFitnessEvent"),
        cat("MindfulSession"),
        cat("AppleWalkingSteadinessEvent"),
        cat("SleepApneaEvent"),
        cat("HandwashingEvent"),
        cat("ToothbrushingEvent"),
    ]

    /// Everything drained by anchor, in drain priority order.
    public static let all: [StreamDef] = registered + classes + extraQuantities + extraCategories

    /// The streams the v1 Bridge already asked permission for.
    public static let v1Keys: Set<String> = Set(registered.map(\.key) + ["HKWorkoutTypeIdentifier"])

    public static let byKey: [String: StreamDef] = Dictionary(uniqueKeysWithValues: all.map { ($0.key, $0) })

    /// Read-unit lookup for workout statistics (HKStatistics of any quantity type).
    public static func readUnit(forQuantity identifier: String) -> String? {
        (registered + extraQuantities).first { $0.identifier == identifier }?.readUnit
    }
}

// MARK: - Dated windows (not anchored)
public enum WindowKind: Equatable, Hashable, Sendable {
    /// One sample stream over a time window (the trailing sweep, a re-read job).
    case samples(StreamDef)
    /// Activity summaries over a day range.
    case activitySummaries
    /// Apple's daily totals (steps, active and basal energy) over a day range.
    case dailyStatistics

    public var cursorKey: String {
        switch self {
        case .samples(let def): return "sweep:" + def.key
        case .activitySummaries: return "summaries"
        case .dailyStatistics: return "dailystats"
        }
    }

    public var origin: PageOrigin {
        switch self {
        case .samples: return .sweep
        case .activitySummaries: return .summaries
        case .dailyStatistics: return .dailystats
        }
    }
}

/// The types Apple's daily totals are fetched for (C9).
public enum DailyStatisticTypes {
    public static let identifiers: [(identifier: String, unit: String)] = [
        ("HKQuantityTypeIdentifierStepCount", "count"),
        ("HKQuantityTypeIdentifierActiveEnergyBurned", "kcal"),
        ("HKQuantityTypeIdentifierBasalEnergyBurned", "kcal"),
    ]
    /// Days are reporting-zone days, like Helios's own daily values (owner
    /// decision 2026-10-18: Dubai stays the reporting zone while traveling).
    public static let zone = "Asia/Dubai"
}
