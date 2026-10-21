import WatchConnectivity
import Combine
import Foundation
import os
import Security
import WidgetKit


struct HimeWatchWidgetSnapshot: Codable, Equatable {
    var catStateRaw: String
    var catMessage: String
    var heartRate: Double?
    var steps: Double?
}

enum HimeWatchWidgetStore {
    static let appGroup: String = {
        guard let id = Bundle.main.bundleIdentifier,
              let range = id.range(of: ".hime", options: .backwards) else { return "" }
        return "group.\(id[id.startIndex..<range.upperBound]).watch"
    }()
    static let fileName = "watch_widget_snapshot.json"

    static var fileURL: URL? {
        FileManager.default
            .containerURL(forSecurityApplicationGroupIdentifier: appGroup)?
            .appendingPathComponent(fileName)
    }

    static func read() -> HimeWatchWidgetSnapshot {
        guard let url = fileURL,
              let data = try? Data(contentsOf: url),
              let snap = try? JSONDecoder().decode(HimeWatchWidgetSnapshot.self, from: data) else {
            return HimeWatchWidgetSnapshot(catStateRaw: "relaxed", catMessage: "")
        }
        return snap
    }


    private static let minReloadInterval: TimeInterval = 15 * 60
    private static var lastReload: Date = .distantPast

    static func write(_ snap: HimeWatchWidgetSnapshot, reload: Bool) {
        guard let url = fileURL else { return }
        guard let data = try? JSONEncoder().encode(snap) else { return }
        try? data.write(to: url, options: .atomic)
        guard reload else { return }
        lastReload = Date()
        WidgetCenter.shared.reloadAllTimelines()
    }

    static func update(_ mutate: (inout HimeWatchWidgetSnapshot) -> Void) {
        var s = read()
        let before = s
        mutate(&s)


        guard s != before else { return }


        let stateChanged = s.catStateRaw != before.catStateRaw || s.catMessage != before.catMessage
        let staleEnough = Date().timeIntervalSince(lastReload) >= minReloadInterval
        write(s, reload: stateChanged || staleEnough)
    }
}


private enum WatchTokenKeychain {
    private nonisolated static let service = "com.hime.watch.serverAuth"
    private nonisolated static let account = "serverAuthToken"

    private nonisolated static var baseQuery: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    nonisolated static func read() -> String {
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data,
              let str = String(data: data, encoding: .utf8) else { return "" }
        return str
    }

    nonisolated static func write(_ value: String) {
        let query = baseQuery
        guard !value.isEmpty else {
            SecItemDelete(query as CFDictionary)
            return
        }
        let attributes: [String: Any] = [
            kSecValueData as String: Data(value.utf8),
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock,
        ]
        if SecItemUpdate(query as CFDictionary, attributes as CFDictionary) == errSecSuccess { return }
        var insert = query
        insert.merge(attributes) { _, new in new }
        SecItemAdd(insert as CFDictionary, nil)
    }
}

private nonisolated let wcLog = Logger(subsystem: "com.hime.watch", category: "WCSync")


nonisolated func watchSyncLog(_ msg: String) {
    wcLog.info("\(msg)")
    Task { @MainActor in
        WatchConnectivityManager.shared.bufferLog(msg)
    }
}

@MainActor
class WatchConnectivityManager: NSObject, ObservableObject {
    static let shared = WatchConnectivityManager()

    @Published var isPhoneReachable: Bool = false
    @Published var catState: String = "relaxed"
    @Published var catMessage: String = ""
    @Published var lastNotification: String = ""


    private var pendingBatches: [[[String: Any]]] = WatchConnectivityManager.loadPendingBatches() {
        didSet { WatchConnectivityManager.savePendingBatches(pendingBatches) }
    }
    private let maxPendingBatches = 200

    private nonisolated static var pendingBatchesURL: URL? {
        FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask).first?
            .appendingPathComponent("watch_pending_batches.json")
    }

    private nonisolated static func loadPendingBatches() -> [[[String: Any]]] {
        guard let url = pendingBatchesURL,
              let data = try? Data(contentsOf: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [[[String: Any]]] else { return [] }
        return obj
    }

    private nonisolated static func savePendingBatches(_ batches: [[[String: Any]]]) {
        guard let url = pendingBatchesURL else { return }
        if batches.isEmpty {
            try? FileManager.default.removeItem(at: url)
            return
        }
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        if let data = try? JSONSerialization.data(withJSONObject: batches) {
            try? data.write(to: url, options: .atomic)
        }
    }


    @Published var serverIngestURL: String? = UserDefaults.standard.string(forKey: "serverIngestURL")


    nonisolated static var serverAuthToken: String {
        get { WatchTokenKeychain.read() }
        set { WatchTokenKeychain.write(newValue) }
    }


    private var logBuffer: [String] = []
    private let maxLogBuffer = 200

    private override init() {
        super.init()
        if WCSession.isSupported() {
            let session = WCSession.default
            session.delegate = self
            session.activate()
        }
    }


    func bufferLog(_ message: String) {
        let ts = Date().formatted(date: .omitted, time: .standard)
        logBuffer.append("[\(ts)] \(message)")
        if logBuffer.count > maxLogBuffer {
            logBuffer.removeFirst()
        }
    }


    private var lastLogFlush: Date = .distantPast
    private let logFlushInterval: TimeInterval = 300


    nonisolated func flushLogs() {
        Task { @MainActor in
            guard !self.logBuffer.isEmpty else { return }
            let now = Date()
            guard now.timeIntervalSince(self.lastLogFlush) >= self.logFlushInterval else { return }


            guard WCSession.default.activationState == .activated else { return }

            let lines = self.logBuffer
            self.logBuffer.removeAll()
            self.lastLogFlush = now
            let message: [String: Any] = ["type": "watch_logs", "lines": lines]
            WCSession.default.transferUserInfo(message)
        }
    }


    @discardableResult
    nonisolated func sendHealthData(_ payloads: [[String: Any]]) async -> Bool {
        guard WCSession.default.activationState == .activated else {
            watchSyncLog("⌚ WC-SEND: session not activated, queuing \(payloads.count) samples to pendingBatches")
            await MainActor.run {
                self.pendingBatches.append(payloads)
                if self.pendingBatches.count > self.maxPendingBatches {
                    self.pendingBatches.removeFirst()
                }
            }
            return true
        }

        let message: [String: Any] = ["type": "health_data", "payloads": payloads, "source": "watch"]

        if WCSession.default.isReachable {
            watchSyncLog("⌚ WC-SEND: phone reachable, sendMessage \(payloads.count) samples")
            WCSession.default.sendMessage(
                message,
                replyHandler: { (_: [String: Any]) in
                    watchSyncLog("⌚ WC-SEND: sendMessage succeeded (\(payloads.count) samples)")
                },
                errorHandler: { (error: Error) in
                    watchSyncLog("⌚ WC-SEND: sendMessage failed (\(error.localizedDescription)), falling back to transferUserInfo")
                    WCSession.default.transferUserInfo(message)
                }
            )
        } else {
            watchSyncLog("⌚ WC-SEND: phone NOT reachable, using transferUserInfo for \(payloads.count) samples")
            WCSession.default.transferUserInfo(message)
        }
        return true
    }

    nonisolated func flushPending() async {
        guard WCSession.default.activationState == .activated else { return }
        let batches = await MainActor.run { () -> [[[String: Any]]] in
            let b = self.pendingBatches
            self.pendingBatches.removeAll()
            return b
        }
        for batch in batches {
            await sendHealthData(batch)
        }
    }


    nonisolated func sendHealthDataHTTP(_ payloads: [[String: Any]]) async {
        guard let urlString = await MainActor.run(body: { self.serverIngestURL }),
              let url = URL(string: urlString) else {
            watchSyncLog("⌚ HTTP-SEND: no serverIngestURL configured, skipping direct upload of \(payloads.count) samples")
            return
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("watch-background", forHTTPHeaderField: "X-Sync-Mode")
        let token = WatchConnectivityManager.serverAuthToken
        if !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        request.timeoutInterval = 30

        let wireData = payloads.map { p -> [String: Any] in
            ["ts": p["ts"] as Any, "f": p["f"] as Any, "v": p["v"] as Any]
        }
        guard let data = try? JSONSerialization.data(withJSONObject: wireData) else { return }
        request.httpBody = data

        watchSyncLog("⌚ HTTP-SEND: POSTing \(payloads.count) samples to \(urlString)")
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            if let http = response as? HTTPURLResponse {
                if http.statusCode == 200 {
                    watchSyncLog("⌚ HTTP-SEND: success (\(payloads.count) samples, status 200)")
                } else {
                    watchSyncLog("⌚ HTTP-SEND: server returned status \(http.statusCode)")
                }
            }
        } catch {
            watchSyncLog("⌚ HTTP-SEND: failed — \(error.localizedDescription)")
        }
    }
}

extension WatchConnectivityManager: WCSessionDelegate {
    nonisolated func session(_ session: WCSession, activationDidCompleteWith activationState: WCSessionActivationState, error: Error?) {
        watchSyncLog("⌚ WC-SESSION: activated (state=\(activationState.rawValue), reachable=\(session.isReachable))")
        Task { @MainActor in
            self.isPhoneReachable = session.isReachable
            let ctx = session.receivedApplicationContext
            if !ctx.isEmpty {
                self.handleIncoming(ctx)
            }
            await self.flushPending()
        }
    }

    nonisolated func sessionReachabilityDidChange(_ session: WCSession) {
        watchSyncLog("⌚ WC-SESSION: reachability changed → \(session.isReachable)")
        Task { @MainActor in
            self.isPhoneReachable = session.isReachable
            if session.isReachable {
                watchSyncLog("⌚ WC-SESSION: phone reachable, flushing pending batches")
                await self.flushPending()
            }
        }
    }

    nonisolated func session(_ session: WCSession, didReceiveMessage message: [String: Any]) {
        Task { @MainActor in
            self.handleIncoming(message)
        }
    }

    nonisolated func session(_ session: WCSession, didReceiveUserInfo userInfo: [String: Any] = [:]) {
        Task { @MainActor in
            self.handleIncoming(userInfo)
        }
    }

    nonisolated func session(_ session: WCSession, didReceiveApplicationContext applicationContext: [String: Any]) {
        Task { @MainActor in
            self.handleIncoming(applicationContext)
        }
    }

    @MainActor
    private func handleIncoming(_ data: [String: Any]) {
        guard let type = data["type"] as? String else { return }
        switch type {
        case "composite":


            var catChanged = false
            if let state = data["cat_state"] as? String, state != self.catState {
                self.catState = state
                catChanged = true
            }
            if let message = data["cat_message"] as? String, message != self.catMessage {
                self.catMessage = message
                catChanged = true
            }
            if let ingestURL = data["ingest_url"] as? String, ingestURL != self.serverIngestURL {
                self.serverIngestURL = ingestURL
                UserDefaults.standard.set(ingestURL, forKey: "serverIngestURL")
            }

            if let token = data["auth_token"] as? String, token != WatchConnectivityManager.serverAuthToken {
                WatchConnectivityManager.serverAuthToken = token
            }
            if catChanged { self.publishWatchSnapshot() }
        case "cat_state":
            self.catState = data["state"] as? String ?? "relaxed"
            self.catMessage = data["message"] as? String ?? ""
            self.publishWatchSnapshot()
        case "notification":
            self.lastNotification = data["text"] as? String ?? ""
        case "server_config":
            if let ingestURL = data["ingest_url"] as? String {
                self.serverIngestURL = ingestURL
                UserDefaults.standard.set(ingestURL, forKey: "serverIngestURL")
            }
            if let token = data["auth_token"] as? String {
                WatchConnectivityManager.serverAuthToken = token
            }
        default:
            break
        }
    }


    @MainActor
    func publishWatchSnapshot(heartRate: Double? = nil, steps: Double? = nil) {
        let state = self.catState
        let message = self.catMessage
        HimeWatchWidgetStore.update { snap in
            snap.catStateRaw = state
            snap.catMessage = message
            if let hr = heartRate { snap.heartRate = hr }
            if let s = steps { snap.steps = s }
        }
    }
}
