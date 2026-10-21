import SwiftUI
import BackgroundTasks
import UserNotifications

private let kBGRefreshID = "com.hime.healthkit.refresh"


class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(
        _ application: UIApplication,
        handleEventsForBackgroundURLSession identifier: String,
        completionHandler: @escaping () -> Void
    ) {
        WebSocketClient.shared.addBackgroundCompletionHandler(identifier: identifier, completion: completionHandler)
    }

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        BGTaskScheduler.shared.register(forTaskWithIdentifier: kBGRefreshID, using: nil) { task in
            guard let refreshTask = task as? BGAppRefreshTask else { return }
            HealthKitManager.shared.handleBackgroundRefresh(task: refreshTask)
        }

        UNUserNotificationCenter.current().delegate = self
        registerForPushIfConsented(application)


        DeviceTokenUploader.shared.uploadIfNeeded()
        return true
    }


    func registerForPushIfConsented(_ application: UIApplication) {
        let onboarded = UserDefaults.standard.bool(forKey: "hime.hasOnboarded")
        let consented = UserDefaults.standard.bool(forKey: "hime.hasConsentedToAIDataSharing")
        guard onboarded && consented else { return }
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
            guard granted else { return }
            DispatchQueue.main.async { application.registerForRemoteNotifications() }
        }
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        let hex = deviceToken.map { String(format: "%02x", $0) }.joined()
        DeviceTokenUploader.shared.setToken(hex)
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        print("APNs registration failed: \(error.localizedDescription)")
    }


    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {

        completionHandler([.banner, .sound])
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {


        let threadId = response.notification.request.content.userInfo["thread_id"] as? String
        Task { @MainActor in AppRouter.shared.requestChat(threadId: threadId) }
        completionHandler()
    }
}


@main
struct himeApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    @StateObject private var hk = HealthKitManager.shared
    @StateObject private var ws = WebSocketClient.shared

    @AppStorage("hime.hasOnboarded") private var hasOnboarded: Bool = false
    @AppStorage("hime.hasConsentedToAIDataSharing") private var hasConsentedToAI: Bool = false

    private var isReady: Bool { hasOnboarded && hasConsentedToAI }

    init() {

        _ = PhoneConnectivityManager.shared


        if hasOnboarded && hasConsentedToAI {


            WebSocketClient.shared.connect(userInitiated: false)
            Task {
                await HealthKitManager.shared.setup()
            }
        }
    }

    var body: some Scene {
        WindowGroup {
            Group {
                if isReady {
                    ContentView()
                } else {
                    OnboardingView(hasOnboarded: $hasOnboarded)
                }
            }
                .environmentObject(hk)
                .environmentObject(ws)
                .onChange(of: hasOnboarded) { _, onboarded in
                    if onboarded {


                        appDelegate.registerForPushIfConsented(UIApplication.shared)
                    }
                }
                .onReceive(NotificationCenter.default.publisher(for: UIApplication.didEnterBackgroundNotification)) { _ in
                    let pending = PendingStore.shared.count
                    let bgTimeRemaining = UIApplication.shared.backgroundTimeRemaining
                    let timeStr = bgTimeRemaining > 999999 ? "unlimited" : String(format: "%.1fs", bgTimeRemaining)
                    HealthKitManager.bgLog("📱 LIFECYCLE: → BACKGROUND (pending=\(pending), bgTimeRemaining=\(timeStr), burst=\(HealthKitManager.shared.isBurstModeEnabled))")


                    var taskID: UIBackgroundTaskIdentifier = .invalid
                    var flushTask: Task<Void, Never>?
                    taskID = UIApplication.shared.beginBackgroundTask(withName: "HimeBackgroundFlush") {
                        flushTask?.cancel()
                        if taskID != .invalid {
                            UIApplication.shared.endBackgroundTask(taskID)
                            taskID = .invalid
                        }
                    }

                    if !HealthKitManager.shared.isBurstModeEnabled {
                        WebSocketClient.shared.disconnect(userInitiated: false)
                    }

                    flushTask = Task {
                        await WebSocketClient.shared.flushPendingAndWait(appState: "background")
                        HealthKitManager.bgLog("📱 LIFECYCLE: background flush done (remaining=\(PendingStore.shared.count))")
                        if taskID != .invalid {
                            UIApplication.shared.endBackgroundTask(taskID)
                            taskID = .invalid
                        }
                    }

                    HealthKitManager.scheduleBackgroundRefresh()
                }
                .onReceive(NotificationCenter.default.publisher(for: UIApplication.didBecomeActiveNotification)) { _ in
                    let pending = PendingStore.shared.count
                    HealthKitManager.bgLog("📱 LIFECYCLE: → FOREGROUND (pending=\(pending))")
                    WebSocketClient.shared.reconnectIfNeeded()
                    WebSocketClient.shared.flushPending(appState: "foreground")
                    DeviceTokenUploader.shared.uploadIfNeeded()
                }
        }
    }
}


@MainActor
final class DeviceTokenUploader {
    static let shared = DeviceTokenUploader()

    private let tokenKey = "hime.apnsDeviceToken"


    private let uploadedKey = "hime.apnsUploadedTo"
    private var isUploading = false

    private init() {}

    private var consented: Bool {
        UserDefaults.standard.bool(forKey: "hime.hasConsentedToAIDataSharing")
    }

    func setToken(_ token: String) {
        UserDefaults.standard.set(token, forKey: tokenKey)
        uploadIfNeeded()
    }

    func uploadIfNeeded() {
        Task { await upload() }
    }

    private func upload() async {
        guard !isUploading, consented,
              let token = UserDefaults.standard.string(forKey: tokenKey), !token.isEmpty else { return }
        let base = ServerConfig.load().apiBaseURL
        let marker = "\(token)|\(base)"
        guard UserDefaults.standard.string(forKey: uploadedKey) != marker else { return }
        guard let url = URL(string: "\(base)/api/devices/register") else { return }
        isUploading = true
        defer { isUploading = false }

        var req = APIClient.request(url, method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 15
        #if DEBUG
        let env = "sandbox"
        #else
        let env = "production"
        #endif
        let body: [String: Any] = [
            "device_token": token,
            "bundle_id": Bundle.main.bundleIdentifier ?? "",
            "environment": env,
        ]
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        guard let (_, resp) = try? await URLSession.shared.data(for: req),
              let http = resp as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
            HealthKitManager.bgLog("APNs: device token upload failed — will retry")
            return
        }
        UserDefaults.standard.set(marker, forKey: uploadedKey)
        HealthKitManager.bgLog("APNs: device token registered")
    }


    func unregister() {
        let token = UserDefaults.standard.string(forKey: tokenKey) ?? ""
        UserDefaults.standard.removeObject(forKey: tokenKey)
        UserDefaults.standard.removeObject(forKey: uploadedKey)
        UIApplication.shared.unregisterForRemoteNotifications()
        guard !token.isEmpty,
              let url = URL(string: "\(ServerConfig.load().apiBaseURL)/api/devices/unregister") else { return }
        var req = APIClient.request(url, method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 15
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["device_token": token])
        let request = req
        Task { _ = try? await URLSession.shared.data(for: request) }
    }
}
