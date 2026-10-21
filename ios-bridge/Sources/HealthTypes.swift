import Foundation
import HealthKit
import BridgeCore

// MARK: - Registry to HealthKit
// The stream list lives in BridgeCore (TypeRegistry) as plain identifiers, so it
// is tested on the Mac. This maps a stream to its HealthKit type; an identifier
// this iOS does not know gives nil and the stream is skipped.
enum HealthTypes {
    static func sampleType(_ def: StreamDef) -> HKSampleType? {
        switch def.cls {
        case .quantity:
            return HKObjectType.quantityType(forIdentifier: HKQuantityTypeIdentifier(rawValue: def.identifier))
        case .category:
            return HKObjectType.categoryType(forIdentifier: HKCategoryTypeIdentifier(rawValue: def.identifier))
        case .workout:
            return HKObjectType.workoutType()
        case .route:
            return HKSeriesType.workoutRoute()
        case .ecg:
            return HKObjectType.electrocardiogramType()
        }
    }

    /// Every type the Bridge asks to read: the streams plus activity summaries.
    static var readTypes: Set<HKObjectType> {
        var set = Set<HKObjectType>()
        for def in TypeRegistry.all {
            if let type = sampleType(def) { set.insert(type) }
        }
        set.insert(HKObjectType.activitySummaryType())
        return set
    }

    /// What the v1 Bridge asked for (an install that only has these must ask again).
    static var v1ReadTypes: Set<HKObjectType> {
        var set = Set<HKObjectType>()
        for def in TypeRegistry.registered {
            if let type = sampleType(def) { set.insert(type) }
        }
        set.insert(HKObjectType.workoutType())
        return set
    }
}

// MARK: - Sleep value mapping
// The Mac wants the HKCategoryValueSleepAnalysis case NAME for sleep stages, e.g.
// "HKCategoryValueSleepAnalysisAsleepDeep" (unchanged from v1).
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

// MARK: - Names as the Apple Health export prints them
// The raw value is always sent too; the name is for people and for matching the
// export's text ("HKWorkoutActivityTypeWalking").
enum HKNames {
    static func activity(_ type: HKWorkoutActivityType) -> String {
        "HKWorkoutActivityType" + activityShort(type)
    }

    // swiftlint:disable:next cyclomatic_complexity function_body_length
    static func activityShort(_ type: HKWorkoutActivityType) -> String {
        switch type {
        case .americanFootball: return "AmericanFootball"
        case .archery: return "Archery"
        case .australianFootball: return "AustralianFootball"
        case .badminton: return "Badminton"
        case .baseball: return "Baseball"
        case .basketball: return "Basketball"
        case .bowling: return "Bowling"
        case .boxing: return "Boxing"
        case .climbing: return "Climbing"
        case .cricket: return "Cricket"
        case .crossTraining: return "CrossTraining"
        case .curling: return "Curling"
        case .cycling: return "Cycling"
        case .elliptical: return "Elliptical"
        case .equestrianSports: return "EquestrianSports"
        case .fencing: return "Fencing"
        case .fishing: return "Fishing"
        case .functionalStrengthTraining: return "FunctionalStrengthTraining"
        case .golf: return "Golf"
        case .gymnastics: return "Gymnastics"
        case .handball: return "Handball"
        case .hiking: return "Hiking"
        case .hockey: return "Hockey"
        case .hunting: return "Hunting"
        case .lacrosse: return "Lacrosse"
        case .martialArts: return "MartialArts"
        case .mindAndBody: return "MindAndBody"
        case .paddleSports: return "PaddleSports"
        case .play: return "Play"
        case .preparationAndRecovery: return "PreparationAndRecovery"
        case .racquetball: return "Racquetball"
        case .rowing: return "Rowing"
        case .rugby: return "Rugby"
        case .running: return "Running"
        case .sailing: return "Sailing"
        case .skatingSports: return "SkatingSports"
        case .snowSports: return "SnowSports"
        case .soccer: return "Soccer"
        case .softball: return "Softball"
        case .squash: return "Squash"
        case .stairClimbing: return "StairClimbing"
        case .surfingSports: return "SurfingSports"
        case .swimming: return "Swimming"
        case .tableTennis: return "TableTennis"
        case .tennis: return "Tennis"
        case .trackAndField: return "TrackAndField"
        case .traditionalStrengthTraining: return "TraditionalStrengthTraining"
        case .volleyball: return "Volleyball"
        case .walking: return "Walking"
        case .waterFitness: return "WaterFitness"
        case .waterPolo: return "WaterPolo"
        case .waterSports: return "WaterSports"
        case .wrestling: return "Wrestling"
        case .yoga: return "Yoga"
        case .barre: return "Barre"
        case .coreTraining: return "CoreTraining"
        case .crossCountrySkiing: return "CrossCountrySkiing"
        case .downhillSkiing: return "DownhillSkiing"
        case .flexibility: return "Flexibility"
        case .highIntensityIntervalTraining: return "HighIntensityIntervalTraining"
        case .jumpRope: return "JumpRope"
        case .kickboxing: return "Kickboxing"
        case .pilates: return "Pilates"
        case .snowboarding: return "Snowboarding"
        case .stairs: return "Stairs"
        case .stepTraining: return "StepTraining"
        case .wheelchairWalkPace: return "WheelchairWalkPace"
        case .wheelchairRunPace: return "WheelchairRunPace"
        case .taiChi: return "TaiChi"
        case .mixedCardio: return "MixedCardio"
        case .handCycling: return "HandCycling"
        case .discSports: return "DiscSports"
        case .fitnessGaming: return "FitnessGaming"
        case .cardioDance: return "CardioDance"
        case .socialDance: return "SocialDance"
        case .pickleball: return "Pickleball"
        case .cooldown: return "Cooldown"
        case .swimBikeRun: return "SwimBikeRun"
        case .transition: return "Transition"
        case .underwaterDiving: return "UnderwaterDiving"
        case .other: return "Other"
        default: return "(\(type.rawValue))"
        }
    }

    static func event(_ type: HKWorkoutEventType) -> String {
        switch type {
        case .pause: return "HKWorkoutEventTypePause"
        case .resume: return "HKWorkoutEventTypeResume"
        case .lap: return "HKWorkoutEventTypeLap"
        case .marker: return "HKWorkoutEventTypeMarker"
        case .motionPaused: return "HKWorkoutEventTypeMotionPaused"
        case .motionResumed: return "HKWorkoutEventTypeMotionResumed"
        case .segment: return "HKWorkoutEventTypeSegment"
        case .pauseOrResumeRequest: return "HKWorkoutEventTypePauseOrResumeRequest"
        @unknown default: return "HKWorkoutEventType(\(type.rawValue))"
        }
    }

    static func classification(_ c: HKElectrocardiogram.Classification) -> String {
        switch c {
        case .notSet: return "notSet"
        case .sinusRhythm: return "sinusRhythm"
        case .atrialFibrillation: return "atrialFibrillation"
        case .inconclusiveLowHeartRate: return "inconclusiveLowHeartRate"
        case .inconclusiveHighHeartRate: return "inconclusiveHighHeartRate"
        case .inconclusivePoorReading: return "inconclusivePoorReading"
        case .inconclusiveOther: return "inconclusiveOther"
        case .unrecognized: return "unrecognized"
        @unknown default: return "classification(\(c.rawValue))"
        }
    }

    static func symptoms(_ s: HKElectrocardiogram.SymptomsStatus) -> String {
        switch s {
        case .notSet: return "notSet"
        case .none: return "none"
        case .present: return "present"
        @unknown default: return "symptoms(\(s.rawValue))"
        }
    }
}
