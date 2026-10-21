import HealthKit
import UIKit
import Combine
@preconcurrency import BackgroundTasks


struct RecentSample: Identifiable {
    let id = UUID()
    let feature: String
    let value: Double
    let timestamp: Date
    var isSynced: Bool = false

    var emoji: String {
        switch feature {
        case "heart_rate": return "💗"
        case "resting_heart_rate": return "❤️"
        case "walking_heart_rate_avg": return "🏃"
        case "heart_rate_variability": return "📈"
        case "blood_oxygen": return "🩸"
        case "respiratory_rate": return "🌬️"
        case "vo2max": return "🫁"
        case "steps": return "👟"
        case "distance": return "📍"
        case "active_energy": return "🔥"
        case "resting_energy": return "⚡️"
        case "exercise_time": return "🏋️"
        case "stand_time": return "🧍"
        case "flights_climbed": return "🪜"
        case "body_mass": return "⚖️"
        case "body_mass_index": return "📊"
        case "sleep_in_bed", "sleep_asleep", "sleep_core", "sleep_deep", "sleep_rem": return "😴"
        case "sleep_awake": return "👁️"
        case "walking_speed": return "🚶"
        case "running_speed": return "🏃‍♂️"
        case "environmental_audio": return "🔊"
        case "headphone_audio": return "🎧"
        case "mindful_session": return "🧘"
        case "water": return "💧"
        case "sleeping_wrist_temp": return "🌡️"
        case "time_in_daylight": return "☀️"
        case "walking_steadiness": return "⚖️"
        case "walking_asymmetry": return "👣"
        case "walking_step_length": return "📏"
        case "walking_double_support": return "👯"
        case "stair_ascent_speed", "stair_descent_speed": return "🪜"
        case "heart_rate_recovery": return "🔄"
        case "atrial_fibrillation_burden": return "💓"
        case "running_power", "cycling_power": return "⚡️"
        case "running_stride_length": return "📐"
        case "running_vertical_oscillation": return "🦘"
        case "running_ground_contact": return "⏱️"
        case "uv_index": return "☀️"
        case "low_heart_rate_event", "high_heart_rate_event", "irregular_heart_rhythm_event": return "⚠️"
        case _ where feature.hasPrefix("workout_"): return "🏋️"
        default: return "📡"
        }
    }

    var formattedValue: String {
        switch feature {
        case "heart_rate", "resting_heart_rate", "walking_heart_rate_avg", "respiratory_rate", "heart_rate_recovery":
            return "\(Int(value)) bpm"
        case "blood_oxygen", "walking_steadiness", "walking_asymmetry", "walking_double_support", "atrial_fibrillation_burden":
            return String(format: "%.1f%%", value * 100)
        case "steps", "flights_climbed":
            return "\(Int(value))"
        case "distance", "distance_cycling", "six_minute_walk", "walking_step_length", "running_stride_length":

            if value >= 1000 {
                return String(format: "%.2f km", value / 1000.0)
            } else {
                return String(format: "%.1f m", value)
            }
        case "active_energy", "resting_energy":
            return "\(Int(value)) kcal"
        case "exercise_time", "stand_time", "time_in_daylight", "mindful_session":

            return "\(Int(value / 60)) min"
        case _ where feature.hasPrefix("sleep_"):

            return "\(Int(value / 60)) min"
        case "heart_rate_variability":
            return String(format: "%.1f ms", value)
        case "body_mass":
            return String(format: "%.1f kg", value)
        case "body_mass_index":
            return String(format: "%.1f", value)
        case "vo2max":
            return String(format: "%.1f ml/kg·min", value)
        case "walking_speed", "stair_ascent_speed", "stair_descent_speed", "cycling_speed":
            return String(format: "%.2f m/s", value)
        case "running_speed":

            return String(format: "%.1f km/h", value * 3.6)
        case "environmental_audio", "headphone_audio":
            return String(format: "%.0f dB", value)
        case "water":
            return "\(Int(value)) mL"
        case "sleeping_wrist_temp":
            return String(format: "%.1f °C", value)
        case "running_power", "cycling_power":
            return "\(Int(value)) W"
        case "running_vertical_oscillation":

            return String(format: "%.1f cm", value)
        case "running_ground_contact":
            return "\(Int(value)) ms"
        case "uv_index":
            return String(format: "%.1f", value)
        case _ where feature.hasPrefix("workout_") && feature.hasSuffix("_duration"):
            return String(format: "%.1f min", value / 60.0)
        case _ where feature.hasPrefix("workout_") && feature.hasSuffix("_distance"):
            return String(format: "%.2f km", value / 1000.0)
        case _ where feature.hasPrefix("workout_") && feature.hasSuffix("_energy"):
            return "\(Int(value)) kcal"
        default:
            return String(format: "%.1f", value)
        }
    }

    var displayName: String {
        feature.replacingOccurrences(of: "_", with: " ").capitalized
    }
}


@MainActor
final class HealthKitManager: ObservableObject {
    static let shared = HealthKitManager()

    @Published var authStatus: String = "Not authorized"
    @Published var lastSync: String = "Never"
    @Published var recentSamples: [RecentSample] = []

    @Published var isBurstModeEnabled: Bool = UserDefaults.standard.bool(forKey: "burstModeEnabled") {
        didSet {
            UserDefaults.standard.set(isBurstModeEnabled, forKey: "burstModeEnabled")
            Task { @MainActor in self.toggleWorkoutSession() }
        }
    }

    nonisolated private let store = HKHealthStore()
    private var workoutSession: HKWorkoutSession?

    private var observerQueries: [HKObserverQuery] = []

    private var isForceFetching = false
    private let maxRecentSamples = 1000


    static let cumulativeFeatures: Set<String> = [
        "steps", "distance", "flights_climbed", "exercise_time",
        "stand_time", "active_energy", "resting_energy", "water"
    ]

    let quantityMetrics: [(String, HKQuantityTypeIdentifier, HKUnit)] = [
        ("steps", .stepCount, .count()),
        ("distance", .distanceWalkingRunning, .meter()),
        ("flights_climbed", .flightsClimbed, .count()),
        ("exercise_time", .appleExerciseTime, .second()),
        ("stand_time", .appleStandTime, .second()),
        ("active_energy", .activeEnergyBurned, .kilocalorie()),
        ("resting_energy", .basalEnergyBurned, .kilocalorie()),
        ("water", .dietaryWater, .literUnit(with: .milli)),
        ("heart_rate", .heartRate, HKUnit(from: "count/min")),
        ("resting_heart_rate", .restingHeartRate, HKUnit(from: "count/min")),
        ("walking_heart_rate_avg", .walkingHeartRateAverage, HKUnit(from: "count/min")),
        ("heart_rate_variability", .heartRateVariabilitySDNN, .secondUnit(with: .milli)),
        ("heart_rate_recovery", .heartRateRecoveryOneMinute, HKUnit(from: "count/min")),
        ("blood_oxygen", .oxygenSaturation, .percent()),
        ("vo2max", .vo2Max, HKUnit(from: "ml/kg*min")),
        ("respiratory_rate", .respiratoryRate, HKUnit(from: "count/min")),
        ("walking_speed", .walkingSpeed, HKUnit(from: "m/s")),
        ("walking_steadiness", .appleWalkingSteadiness, .percent()),
        ("walking_asymmetry", .walkingAsymmetryPercentage, .percent()),
        ("walking_step_length", .walkingStepLength, .meter()),
        ("walking_double_support", .walkingDoubleSupportPercentage, .percent()),
        ("six_minute_walk", .sixMinuteWalkTestDistance, .meter()),
        ("stair_ascent_speed", .stairAscentSpeed, HKUnit(from: "m/s")),
        ("stair_descent_speed", .stairDescentSpeed, HKUnit(from: "m/s")),
        ("body_mass", .bodyMass, .gramUnit(with: .kilo)),
        ("body_mass_index", .bodyMassIndex, .count()),
        ("sleeping_wrist_temp", .appleSleepingWristTemperature, .degreeCelsius()),
        ("time_in_daylight", .timeInDaylight, .second()),
        ("uv_index", .uvExposure, .count()),
        ("atrial_fibrillation_burden", .atrialFibrillationBurden, .percent()),
        ("running_power", .runningPower, .watt()),
        ("running_stride_length", .runningStrideLength, .meter()),
        ("running_vertical_oscillation", .runningVerticalOscillation, .meterUnit(with: .centi)),
        ("running_ground_contact", .runningGroundContactTime, .secondUnit(with: .milli)),
        ("cycling_speed", .cyclingSpeed, HKUnit(from: "m/s")),
        ("cycling_cadence", .cyclingCadence, HKUnit(from: "count/min")),
        ("cycling_power", .cyclingPower, .watt()),
    ]


    nonisolated static let sleepPlaceholder = "__sleep__"

    let categoryMetrics: [(String, HKCategoryTypeIdentifier)] = [
        ("mindful_session", .mindfulSession),
        ("audio_exposure_event", .environmentalAudioExposureEvent),
        ("high_heart_rate_event", .highHeartRateEvent),
        ("low_heart_rate_event", .lowHeartRateEvent),
        ("irregular_heart_rhythm_event", .irregularHeartRhythmEvent),
        (HealthKitManager.sleepPlaceholder, .sleepAnalysis),
    ]


    private let workoutTypes: [HKWorkoutActivityType] = [
        .running, .cycling, .swimming, .walking, .hiking,
        .yoga, .functionalStrengthTraining, .traditionalStrengthTraining,
        .highIntensityIntervalTraining, .elliptical, .rowing,
        .coreTraining, .flexibility, .cooldown,
    ]

    nonisolated private static func workoutTypeName(_ type: HKWorkoutActivityType) -> String {
        switch type {
        case .running: return "running"
        case .cycling: return "cycling"
        case .swimming: return "swimming"
        case .walking: return "walking"
        case .hiking: return "hiking"
        case .yoga: return "yoga"
        case .functionalStrengthTraining: return "strength"
        case .traditionalStrengthTraining: return "strength"
        case .highIntensityIntervalTraining: return "hiit"
        case .elliptical: return "elliptical"
        case .rowing: return "rowing"
        case .coreTraining: return "core"
        case .flexibility: return "flexibility"
        case .cooldown: return "cooldown"
        default: return "other"
        }
    }

    private init() {}


    private var didRegisterObservers = false

    func setup() async {
        guard HKHealthStore.isHealthDataAvailable() else {
            await MainActor.run { authStatus = "HealthKit not available on this device" }
            HealthKitManager.bgLog("HealthKit not available (iPad/Simulator)")
            return
        }

        var readTypes = Set<HKObjectType>()
        for (_, id, _) in quantityMetrics {
            if let type = HKQuantityType.quantityType(forIdentifier: id) {
                readTypes.insert(type)
            }
        }
        for (_, id) in categoryMetrics {
            if let type = HKObjectType.categoryType(forIdentifier: id) {
                readTypes.insert(type)
            }
        }

        readTypes.insert(HKWorkoutType.workoutType())

        do {
            try await store.requestAuthorization(toShare: [], read: readTypes)


            await MainActor.run { authStatus = "Permission request completed" }
        } catch {
            let msg = "Auth failed: \(error.localizedDescription)"
            await MainActor.run { authStatus = msg }
            HealthKitManager.bgLog("HealthKit auth failed: \(error.localizedDescription)")
            return
        }


        if isBurstModeEnabled { startWorkoutSession() }

        if didRegisterObservers {
            HealthKitManager.bgLog("HealthKit observers already registered — skipping duplicate setup()")
            return
        }
        didRegisterObservers = true


        for (f, id, unit) in quantityMetrics {
            if HealthKitManager.cumulativeFeatures.contains(f) {
                registerCumulativeObserver(feature: f, quantityType: HKQuantityType(id), unit: unit)
            } else {
                registerObserver(feature: f, sampleType: HKQuantityType(id), unit: unit)
            }
        }
        for (f, id) in categoryMetrics {
            registerObserver(feature: f, sampleType: HKCategoryType(id), unit: nil)
        }


        registerWorkoutObserver()
    }

    private func registerObserver(feature: String, sampleType: HKSampleType, unit: HKUnit?) {
        store.enableBackgroundDelivery(for: sampleType, frequency: .immediate) { _, _ in }

        let observer = HKObserverQuery(sampleType: sampleType, predicate: nil) { [weak self] _, completion, error in
            if let error {
                HealthKitManager.bgLog("📱 HK-OBSERVER: \(feature) fired with error: \(error.localizedDescription)")
                completion()
                return
            }
            guard let self else { completion(); return }


            Task {
                let stateStr = await BackgroundTaskHandle.appStateDescription()
                HealthKitManager.bgLog("📱 HK-OBSERVER: \(feature) fired (appState=\(stateStr))")

                guard let handle = await BackgroundTaskHandle.begin(name: "HKFetch-\(feature)") else {
                    HealthKitManager.bgLog("📱 HK-OBSERVER: \(feature) — beginBackgroundTask returned .invalid, aborting")
                    completion()
                    return
                }


                await self.fetchAndStore(feature: feature, sampleType: sampleType, unit: unit, appState: stateStr)
                completion()
                await handle.end()
            }
        }
        observerQueries.append(observer)
        store.execute(observer)
    }

    private func registerCumulativeObserver(feature: String, quantityType: HKQuantityType, unit: HKUnit) {
        store.enableBackgroundDelivery(for: quantityType, frequency: .immediate) { _, _ in }

        let observer = HKObserverQuery(sampleType: quantityType, predicate: nil) { [weak self] _, completion, error in
            if let error {
                HealthKitManager.bgLog("📱 HK-OBSERVER: \(feature) (cumulative) fired with error: \(error.localizedDescription)")
                completion()
                return
            }
            guard let self else { completion(); return }


            Task {
                let stateStr = await BackgroundTaskHandle.appStateDescription()
                HealthKitManager.bgLog("📱 HK-OBSERVER: \(feature) (cumulative) fired (appState=\(stateStr))")

                guard let handle = await BackgroundTaskHandle.begin(name: "HKStats-\(feature)") else {
                    HealthKitManager.bgLog("📱 HK-OBSERVER: \(feature) (cumulative) — beginBackgroundTask returned .invalid, aborting")
                    completion()
                    return
                }
                await self.fetchAndStoreCumulative(
                    feature: feature, quantityType: quantityType, unit: unit, appState: stateStr
                )
                completion()
                await handle.end()
            }
        }
        observerQueries.append(observer)
        store.execute(observer)
    }

    private func registerWorkoutObserver() {
        let workoutType = HKWorkoutType.workoutType()
        store.enableBackgroundDelivery(for: workoutType, frequency: .immediate) { _, _ in }

        let observer = HKObserverQuery(sampleType: workoutType, predicate: nil) { [weak self] _, completion, error in
            guard error == nil, let self else { completion(); return }


            Task {
                let stateStr = await BackgroundTaskHandle.appStateDescription()
                guard let handle = await BackgroundTaskHandle.begin(name: "HKFetch-workouts") else {
                    completion()
                    return
                }
                await self.fetchWorkouts(appState: stateStr)
                completion()
                await handle.end()
            }
        }
        observerQueries.append(observer)
        store.execute(observer)
    }


    func stopCollection() {
        for q in observerQueries { store.stop(q) }
        observerQueries.removeAll()
        store.disableAllBackgroundDelivery { _, _ in }
        didRegisterObservers = false
        if workoutSession != nil { stopWorkoutSession() }
        PendingStore.shared.clear()
        HealthKitManager.resetSyncCursors()
        recentSamples = []
        HealthKitManager.bgLog("HK: collection stopped (consent revoked) — queue cleared")
    }


    nonisolated func fetchWorkouts(appState: String = "foreground") async {
        let anchorKey = "anchor_workouts"
        let anchor = HealthKitManager.loadAnchorStatic(anchorKey)

        let backfillStart = Calendar.current.date(byAdding: .day, value: -14, to: Date())!
        let predicate = HKQuery.predicateForSamples(withStart: backfillStart, end: nil, options: .strictStartDate)

        let healthStore = await MainActor.run { HealthKitManager.shared.store }


        let (payloads, newAnchor): ([HealthPayload], HKQueryAnchor?) = await withCheckedContinuation { continuation in
            let query = HKAnchoredObjectQuery(
                type: HKWorkoutType.workoutType(), predicate: predicate, anchor: anchor,
                limit: HKObjectQueryNoLimit
            ) { _, rawSamples, _, newAnchor, _ in
                var results: [HealthPayload] = []

                if let workouts = rawSamples as? [HKWorkout] {
                    for w in workouts {
                        let typeName = HealthKitManager.workoutTypeName(w.workoutActivityType)
                        let ts = w.startDate.timeIntervalSince1970
                        let duration = w.duration

                        results.append(HealthPayload(ts: ts, value: duration, feature: "workout_\(typeName)_duration"))

                        if let distance = w.totalDistance?.doubleValue(for: .meter()), distance > 0 {
                            results.append(HealthPayload(ts: ts, value: distance, feature: "workout_\(typeName)_distance"))
                        }
                        if let energy = w.statistics(for: HKQuantityType(.activeEnergyBurned))?.sumQuantity()?.doubleValue(for: .kilocalorie()), energy > 0 {
                            results.append(HealthPayload(ts: ts, value: energy, feature: "workout_\(typeName)_energy"))
                        }
                    }
                }

                continuation.resume(returning: (results, newAnchor))
            }
            healthStore.execute(query)
        }

        guard !payloads.isEmpty else {
            if let newAnchor { HealthKitManager.saveAnchorStatic(newAnchor, key: anchorKey) }
            return
        }

        let queued = PendingStore.shared.append(payloads)
        if queued, let newAnchor {
            HealthKitManager.saveAnchorStatic(newAnchor, key: anchorKey)
        }

        let ts = Date().formatted(date: .omitted, time: .standard)
        let recents = payloads.map {
            RecentSample(feature: $0.f, value: $0.v, timestamp: Date(timeIntervalSince1970: $0.ts), isSynced: false)
        }

        await MainActor.run {
            let m = HealthKitManager.shared
            m.lastSync = ts
            m.mergeRecentSamples(recents)
        }

        await WebSocketClient.shared.flushPendingAndWait(appState: appState)
    }


    nonisolated func fetchAndStoreCumulative(
        feature: String,
        quantityType: HKQuantityType,
        unit: HKUnit,
        appState: String = "foreground"
    ) async {
        let hwmKey = "stats_hwm_\(feature)"
        let lastSent = UserDefaults.standard.double(forKey: hwmKey)


        let lookbackSeconds: TimeInterval = 48 * 3600

        let backfillStart = Calendar.current.date(byAdding: .day, value: -14, to: Date())!
        let startDate: Date
        if lastSent > 0 {
            let hwmDate = Date(timeIntervalSince1970: lastSent)
            let lookbackDate = hwmDate.addingTimeInterval(-lookbackSeconds)
            startDate = max(lookbackDate, backfillStart)
        } else {
            startDate = backfillStart
        }


        let calendar = Calendar.current
        let anchorDate = calendar.startOfDay(for: startDate)
        let interval = DateComponents(hour: 1)
        let now = Date()

        let predicate = HKQuery.predicateForSamples(
            withStart: startDate, end: now, options: .strictStartDate
        )

        let healthStore = await MainActor.run { HealthKitManager.shared.store }
        let (localPayloads, latestEndTs): ([HealthPayload], Double) = await withCheckedContinuation { continuation in
            let query = HKStatisticsCollectionQuery(
                quantityType: quantityType,
                quantitySamplePredicate: predicate,
                options: .cumulativeSum,
                anchorDate: anchorDate,
                intervalComponents: interval
            )

            query.initialResultsHandler = { _, collection, error in
                guard let collection = collection, error == nil else {
                    let errMsg = error?.localizedDescription ?? "nil"
                    let isProtected = errMsg.contains("Protected") || errMsg.contains("inaccessible")
                    HealthKitManager.bgLog("📱 HK-STATS: \(feature) query FAILED — \(errMsg)\(isProtected ? " [DEVICE LOCKED]" : "")")
                    continuation.resume(returning: ([], 0))
                    return
                }

                var payloads: [HealthPayload] = []
                var maxEndTs: Double = 0
                collection.enumerateStatistics(from: startDate, to: now) { stats, _ in


                    guard let sum = stats.sumQuantity() else { return }
                    let value = sum.doubleValue(for: unit)
                    payloads.append(HealthPayload(
                        ts: stats.startDate.timeIntervalSince1970,
                        value: value,
                        feature: feature
                    ))
                    let endTs = stats.endDate.timeIntervalSince1970
                    if endTs > maxEndTs { maxEndTs = endTs }
                }
                continuation.resume(returning: (payloads, maxEndTs))
            }

            healthStore.execute(query)
        }

        guard !localPayloads.isEmpty else {
            HealthKitManager.bgLog("📱 HK-STATS: \(feature) — 0 new buckets (appState=\(appState))")
            return
        }

        HealthKitManager.bgLog("📱 HK-STATS: \(feature) — \(localPayloads.count) new buckets → PendingStore (appState=\(appState))")

        let queued = PendingStore.shared.append(localPayloads)


        if queued, latestEndTs > 0 {
            UserDefaults.standard.set(latestEndTs, forKey: hwmKey)
        }

        let ts = Date().formatted(date: .omitted, time: .standard)
        let recentSlice = localPayloads.suffix(1000)
        let recents = recentSlice.map {
            RecentSample(
                feature: $0.f,
                value: $0.v,
                timestamp: Date(timeIntervalSince1970: $0.ts),
                isSynced: false
            )
        }

        await MainActor.run {
            let m = HealthKitManager.shared
            m.lastSync = ts
            m.mergeRecentSamples(recents)
        }

        await WebSocketClient.shared.flushPendingAndWait(appState: appState)
    }


    nonisolated func fetchAndStore(
        feature: String,
        sampleType: HKSampleType,
        unit: HKUnit?,
        appState: String = "foreground"
    ) async {
        let anchorKey = "anchor_\(sampleType.identifier)"
        let anchor = HealthKitManager.loadAnchorStatic(anchorKey)


        let backfillStart = Calendar.current.date(byAdding: .day, value: -14, to: Date())!
        let predicate = HKQuery.predicateForSamples(withStart: backfillStart, end: nil, options: .strictStartDate)

        let healthStore = await MainActor.run { HealthKitManager.shared.store }


        let (localPayloads, newAnchor): ([HealthPayload], HKQueryAnchor?) = await withCheckedContinuation { continuation in
            let query = HKAnchoredObjectQuery(
                type: sampleType, predicate: predicate, anchor: anchor,
                limit: HKObjectQueryNoLimit
            ) { _, rawSamples, _, newAnchor, _ in
                var payloads: [HealthPayload] = []

                if let samples = rawSamples as? [HKQuantitySample], let unit {
                    for s in samples {
                        payloads.append(HealthPayload(
                            ts: s.startDate.timeIntervalSince1970,
                            value: s.quantity.doubleValue(for: unit),
                            feature: feature
                        ))
                    }
                } else if let samples = rawSamples as? [HKCategorySample] {
                    HealthKitManager.bgLog("Category \(feature): got \(samples.count) samples")
                    for s in samples {
                        let (feat, val) = HealthKitManager.parseCategorySample(s, baseFeature: feature)


                        let isDuration = feature == HealthKitManager.sleepPlaceholder || feature == "mindful_session"
                        let sampleTs = isDuration ? s.endDate.timeIntervalSince1970 : s.startDate.timeIntervalSince1970
                        payloads.append(HealthPayload(
                            ts: sampleTs,
                            value: val,
                            feature: feat
                        ))
                    }
                }

                continuation.resume(returning: (payloads, newAnchor))
            }
            healthStore.execute(query)
        }

        guard !localPayloads.isEmpty else {
            if let newAnchor { HealthKitManager.saveAnchorStatic(newAnchor, key: anchorKey) }
            HealthKitManager.bgLog("📱 HK-FETCH: \(feature) — 0 new samples (appState=\(appState))")
            return
        }

        HealthKitManager.bgLog("📱 HK-FETCH: \(feature) — \(localPayloads.count) new samples → PendingStore (appState=\(appState))")

        let queued = PendingStore.shared.append(localPayloads)
        if queued, let newAnchor {
            HealthKitManager.saveAnchorStatic(newAnchor, key: anchorKey)
        }

        let ts = Date().formatted(date: .omitted, time: .standard)
        let recentSlice = localPayloads.suffix(1000)
        let recents = recentSlice.map {
            RecentSample(
                feature: $0.f,
                value: $0.v,
                timestamp: Date(timeIntervalSince1970: $0.ts),
                isSynced: false
            )
        }

        await MainActor.run {
            let m = HealthKitManager.shared
            m.lastSync = ts
            m.mergeRecentSamples(recents)
        }

        await WebSocketClient.shared.flushPendingAndWait(appState: appState)
    }


    func mergeRecentSamples(_ incoming: [RecentSample]) {
        guard !incoming.isEmpty else { return }
        let keys = Set(incoming.map { "\($0.feature)|\($0.timestamp.timeIntervalSince1970)" })
        recentSamples.removeAll { keys.contains("\($0.feature)|\($0.timestamp.timeIntervalSince1970)") }
        recentSamples.insert(contentsOf: incoming.reversed(), at: 0)
        if recentSamples.count > 1000 {
            recentSamples = Array(recentSamples.prefix(1000))
        }
    }


    func markOldestAsSynced(count: Int) {
        guard count > 0, !recentSamples.isEmpty else { return }
        var remaining = count


        let upperBound = recentSamples.count
        for i in stride(from: upperBound - 1, through: 0, by: -1) {
            if remaining <= 0 { break }
            guard i < recentSamples.count else { continue }
            if !recentSamples[i].isSynced {
                recentSamples[i].isSynced = true
                remaining -= 1
            }
        }
    }


    func forceFetch(fullResync: Bool = false) {


        guard !isForceFetching else {
            HealthKitManager.bgLog("HK: Force sweep already running — ignoring")
            return
        }
        var bgTaskID: UIBackgroundTaskIdentifier = .invalid
        var sweep: Task<Void, Never>?
        bgTaskID = UIApplication.shared.beginBackgroundTask(withName: "HKForceFetch") { [weak self] in


            sweep?.cancel()
            self?.isForceFetching = false
            if bgTaskID != .invalid {
                UIApplication.shared.endBackgroundTask(bgTaskID)
                bgTaskID = .invalid
            }
        }
        guard bgTaskID != .invalid else { return }
        isForceFetching = true


        if fullResync {
            HealthKitManager.resetSyncCursors()
        }
        HealthKitManager.bgLog("HK: Force sweep started\(fullResync ? " (full 14-day resync)" : "")...")

        sweep = Task {
            for (f, id, unit) in self.quantityMetrics {
                if HealthKitManager.cumulativeFeatures.contains(f) {
                    await self.fetchAndStoreCumulative(
                        feature: f, quantityType: HKQuantityType(id), unit: unit, appState: "foreground"
                    )
                } else if let type = HKQuantityType.quantityType(forIdentifier: id) {
                    await self.fetchAndStore(feature: f, sampleType: type, unit: unit, appState: "foreground")
                }
            }
            for (f, id) in self.categoryMetrics {
                if let type = HKObjectType.categoryType(forIdentifier: id) {
                    await self.fetchAndStore(feature: f, sampleType: type, unit: nil, appState: "foreground")
                }
            }
            await self.fetchWorkouts(appState: "foreground")
            HealthKitManager.bgLog("HK: Force sweep completed.")
            self.isForceFetching = false
            if bgTaskID != .invalid {
                UIApplication.shared.endBackgroundTask(bgTaskID)
                bgTaskID = .invalid
            }
        }
    }


    private func toggleWorkoutSession() {
        if isBurstModeEnabled {
            startWorkoutSession()
        } else {
            stopWorkoutSession()
        }
    }

    private func startWorkoutSession() {
        guard workoutSession == nil else { return }

        if #available(iOS 26.0, *) {
            let config = HKWorkoutConfiguration()
            config.activityType = .other

            do {
                let session = try HKWorkoutSession(healthStore: store, configuration: config)
                self.workoutSession = session
                session.startActivity(with: Date())
                HealthKitManager.bgLog("Burst Mode: ON")
            } catch {
                HealthKitManager.bgLog("Burst Mode ERR: \(error.localizedDescription)")
                isBurstModeEnabled = false
            }
        } else {
            HealthKitManager.bgLog("Burst Mode: not available on this iOS version")
            isBurstModeEnabled = false
        }
    }

    private func stopWorkoutSession() {
        workoutSession?.end()
        workoutSession = nil
        HealthKitManager.bgLog("Burst Mode: OFF")
    }


    nonisolated private static func parseCategorySample(_ s: HKCategorySample, baseFeature: String) -> (String, Double) {
        if baseFeature == sleepPlaceholder {
            var feat = "sleep_unknown"
            switch HKCategoryValueSleepAnalysis(rawValue: s.value) {
            case .inBed:             feat = "sleep_in_bed"
            case .asleepUnspecified: feat = "sleep_asleep"
            case .asleepCore:        feat = "sleep_core"
            case .asleepDeep:        feat = "sleep_deep"
            case .asleepREM:         feat = "sleep_rem"
            case .awake:             feat = "sleep_awake"
            default: break
            }
            return (feat, s.endDate.timeIntervalSince(s.startDate))
        }
        if baseFeature == "mindful_session" {
            return (baseFeature, s.endDate.timeIntervalSince(s.startDate))
        }


        let duration = s.endDate.timeIntervalSince(s.startDate)
        return (baseFeature, duration > 0 ? duration : 1.0)
    }

    nonisolated private static func loadAnchorStatic(_ key: String) -> HKQueryAnchor? {
        UserDefaults.standard.data(forKey: key).flatMap {
            try? NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: $0)
        }
    }


    nonisolated static func resetSyncCursors() {
        let defaults = UserDefaults.standard
        let keys = defaults.dictionaryRepresentation().keys.filter {
            $0.hasPrefix("anchor_") || $0.hasPrefix("stats_hwm_")
        }
        keys.forEach { defaults.removeObject(forKey: $0) }
        bgLog("HK: Reset \(keys.count) sync cursors — next sweep re-reads 14 days")
    }

    nonisolated private static func saveAnchorStatic(_ anchor: HKQueryAnchor, key: String) {
        if let data = try? NSKeyedArchiver.archivedData(withRootObject: anchor, requiringSecureCoding: true) {
            UserDefaults.standard.set(data, forKey: key)
        }
    }

    nonisolated static func bgLog(_ msg: String) {
        Task { @MainActor in
            LogManager.shared.log(msg)
        }
    }


    nonisolated static func scheduleBackgroundRefresh() {
        let request = BGAppRefreshTaskRequest(identifier: "com.hime.healthkit.refresh")
        request.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        do {
            try BGTaskScheduler.shared.submit(request)
            bgLog("📱 BG-REFRESH: scheduled next refresh in 15 min")
        } catch {
            bgLog("📱 BG-REFRESH: submit ERR: \(error.localizedDescription)")
        }
    }

    nonisolated func handleBackgroundRefresh(task: BGAppRefreshTask) {
        HealthKitManager.bgLog("📱 BG-REFRESH: handleBackgroundRefresh started (pending=\(PendingStore.shared.count))")
        HealthKitManager.scheduleBackgroundRefresh()


        let completion = BGCompletionGuard()
        var work: Task<Void, Never>?

        task.expirationHandler = {
            work?.cancel()
            Task { @MainActor in
                HealthKitManager.bgLog("📱 BG-REFRESH: task EXPIRED before completion")
                completion.finish(task: task, success: false)
            }
        }

        work = Task { @MainActor in
            let m = HealthKitManager.shared

            let priorityFeatures: Set<String> = [
                "heart_rate", "blood_oxygen", "heart_rate_variability",
                "resting_heart_rate", "respiratory_rate", "steps",
                "active_energy", "exercise_time", HealthKitManager.sleepPlaceholder
            ]


            for (f, id, unit) in m.quantityMetrics where priorityFeatures.contains(f) {
                if Task.isCancelled { break }
                if HealthKitManager.cumulativeFeatures.contains(f) {
                    await m.fetchAndStoreCumulative(
                        feature: f, quantityType: HKQuantityType(id), unit: unit, appState: "background"
                    )
                } else if let type = HKQuantityType.quantityType(forIdentifier: id) {
                    await m.fetchAndStore(feature: f, sampleType: type, unit: unit, appState: "background")
                }
            }
            for (f, id) in m.categoryMetrics where priorityFeatures.contains(f) {
                if Task.isCancelled { break }
                if let type = HKObjectType.categoryType(forIdentifier: id) {
                    await m.fetchAndStore(feature: f, sampleType: type, unit: nil, appState: "background")
                }
            }


            if !Task.isCancelled {
                await m.fetchWorkouts(appState: "background")
            }


            for (f, id, unit) in m.quantityMetrics where !priorityFeatures.contains(f) {
                if Task.isCancelled { break }
                if HealthKitManager.cumulativeFeatures.contains(f) {
                    await m.fetchAndStoreCumulative(
                        feature: f, quantityType: HKQuantityType(id), unit: unit, appState: "background"
                    )
                } else if let type = HKQuantityType.quantityType(forIdentifier: id) {
                    await m.fetchAndStore(feature: f, sampleType: type, unit: unit, appState: "background")
                }
            }
            for (f, id) in m.categoryMetrics where !priorityFeatures.contains(f) {
                if Task.isCancelled { break }
                if let type = HKObjectType.categoryType(forIdentifier: id) {
                    await m.fetchAndStore(feature: f, sampleType: type, unit: nil, appState: "background")
                }
            }

            HealthKitManager.bgLog("📱 BG-REFRESH: completed (pending=\(PendingStore.shared.count))")
            completion.finish(task: task, success: !Task.isCancelled)
        }
    }
}


@MainActor
final class BackgroundTaskHandle {
    private var id: UIBackgroundTaskIdentifier = .invalid


    static func begin(name: String) -> BackgroundTaskHandle? {
        let handle = BackgroundTaskHandle()
        handle.id = UIApplication.shared.beginBackgroundTask(withName: name) { [weak handle] in
            HealthKitManager.bgLog("📱 BG-TASK: \(name) expired — releasing assertion")
            handle?.end()
        }
        return handle.id == .invalid ? nil : handle
    }

    func end() {
        guard id != .invalid else { return }
        UIApplication.shared.endBackgroundTask(id)
        id = .invalid
    }


    static func appStateDescription() -> String {
        switch UIApplication.shared.applicationState {
        case .active:     return "active"
        case .background: return "background"
        default:          return "inactive"
        }
    }
}


@MainActor
final class BGCompletionGuard {
    private var finished = false


    nonisolated init() {}

    func finish(task: BGTask, success: Bool) {
        guard !finished else { return }
        finished = true
        task.setTaskCompleted(success: success)
    }
}
