@preconcurrency import Dispatch
import Foundation
import Combine
import UIKit


private final class SessionDelegate: NSObject, URLSessionWebSocketDelegate, URLSessionTaskDelegate, URLSessionDelegate, @unchecked Sendable {
    weak var client: WebSocketClient?

    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        guard let identifier = session.configuration.identifier else { return }
        Task { @MainActor in
            client?.executeBackgroundCompletionHandler(for: identifier)
        }
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask, didOpenWithProtocol protocol: String?) {
        Task { @MainActor in
            await client?.onWSOpened()
        }
    }


    func urlSession(_ session: URLSession, taskIsWaitingForConnectivity task: URLSessionTask) {
        let url = task.currentRequest?.url?.absoluteString ?? "?"
        Task { @MainActor in
            HealthKitManager.bgLog("HTTP: Task waiting for connectivity — \(url)")
        }
    }


    static func taskKey(session: URLSession, task: URLSessionTask) -> String {
        "\(session.configuration.identifier ?? "fg")#\(task.taskIdentifier)"
    }


    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        let isWS = task is URLSessionWebSocketTask
        let fileURLString = task.taskDescription
        let key = SessionDelegate.taskKey(session: session, task: task)


        let statusCode = (task.response as? HTTPURLResponse)?.statusCode

        Task { @MainActor in
            if let error = error {
                if isWS {
                    client?.onWSTaskFailed(task: task, error: error)
                } else {
                    client?.onHTTPTaskFailed(key: key, error: error)
                }
            } else if !isWS {
                if let code = statusCode, (200...299).contains(code) {
                    client?.onHTTPTaskSucceeded(key: key)
                } else {
                    let code = statusCode ?? -1
                    let httpError = NSError(
                        domain: "hime.http", code: code,
                        userInfo: [NSLocalizedDescriptionKey: "HTTP status \(code)"]
                    )
                    client?.onHTTPTaskFailed(key: key, error: httpError)
                }
            }


            if let desc = fileURLString, let fileURL = URL(string: desc) {
                try? FileManager.default.removeItem(at: fileURL)
            }
        }
    }
}


@MainActor
private final class ContinuationGuard {
    private var continuation: CheckedContinuation<Bool, Never>?
    init(_ c: CheckedContinuation<Bool, Never>) { continuation = c }

    @discardableResult
    func resume(_ ok: Bool) -> Bool {
        guard let c = continuation else { return false }
        continuation = nil
        c.resume(returning: ok)
        return true
    }
}


private enum WSAckResult {
    case acked
    case nacked(String)

    case failed
}


@MainActor
private final class AckWaiter {
    let id: String
    private var continuation: CheckedContinuation<WSAckResult, Never>?
    init(id: String, continuation: CheckedContinuation<WSAckResult, Never>) {
        self.id = id
        self.continuation = continuation
    }
    @discardableResult
    func resolve(_ result: WSAckResult) -> Bool {
        guard let c = continuation else { return false }
        continuation = nil
        c.resume(returning: result)
        return true
    }
}

@MainActor
final class WebSocketClient: ObservableObject {
    static let shared = WebSocketClient()

    @Published private(set) var isConnected = false
    @Published var serverConfig: ServerConfig = ServerConfig.load() {
        didSet { serverConfig.save() }
    }

    @Published var isSyncActive: Bool = !UserDefaults.standard.bool(forKey: "userRequestedDisconnect") {
        didSet {
            UserDefaults.standard.set(!isSyncActive, forKey: "userRequestedDisconnect")
        }
    }


    var isSyncEnabled: Bool {
        isSyncActive
    }


    var serverURL: String { serverConfig.watchURL }


    var httpBaseURL: String { serverConfig.watchHTTPBaseURL }


    private var wsTask: URLSessionWebSocketTask?
    private var shouldReconnect = false
    private var reconnectDelay: TimeInterval = 1.0
    private let maxReconnectDelay: TimeInterval = 60.0


    private var heartbeatTimer: DispatchSourceTimer?
    private let heartbeatInterval: TimeInterval = 30.0
    private let heartbeatTimeout: TimeInterval = 10.0

    private let chunkSize = 500
    private var _isFlushing = false


    private var wsServerAcks = false


    private var wsHelloSettled = false
    private var wsAckWaiter: AckWaiter?
    private let wsAckTimeout: TimeInterval = 15.0

    private var wsPausedUntil: Date = .distantPast
    private var wsResumeTask: Task<Void, Never>?
    private let wsNackPause: TimeInterval = 60.0


    private var httpConsecutiveClientErrors = 0
    private let maxHttpClientErrors = 5


    private var httpRetryDelay: TimeInterval = 2.0
    private let maxHttpRetryDelay: TimeInterval = 60.0

    private let queue = DispatchQueue(label: "hime.websocket", qos: .utility)
    private let sessionDelegate = SessionDelegate()


    private static let kPendingHTTPKey = "hime.pendingHTTPTasks"
    private var pendingHTTPTasks: [String: Int] = [:] {
        didSet { _persistPendingHTTPTasks() }
    }

    private lazy var fgSession: URLSession = {
        let cfg = URLSessionConfiguration.default


        cfg.waitsForConnectivity = false
        cfg.timeoutIntervalForRequest = 30


        sessionDelegate.client = self
        return URLSession(configuration: cfg, delegate: sessionDelegate, delegateQueue: .main)
    }()

    private lazy var bgSession: URLSession = {
        let cfg = URLSessionConfiguration.background(withIdentifier: "com.hime.healthkit.upload")
        cfg.isDiscretionary = false
        cfg.sessionSendsLaunchEvents = true
        sessionDelegate.client = self
        return URLSession(configuration: cfg, delegate: sessionDelegate, delegateQueue: .main)
    }()

    private init() {


        UserDefaults.standard.removeObject(forKey: Self.kPendingHTTPKey)
    }

    private func _persistPendingHTTPTasks() {
        UserDefaults.standard.set(pendingHTTPTasks, forKey: Self.kPendingHTTPKey)
    }


    func connect(userInitiated: Bool = true) {
        if userInitiated {
            self.isSyncActive = true
        } else if !isSyncActive {
            return
        }
        queue.async { [weak self] in
            guard let self else { return }
            Task { @MainActor in

                self.wsTask?.cancel(with: .normalClosure, reason: nil)
                self.wsTask = nil
                self._resetWSProtocolState()
                self.isConnected = false
                self._openWS()
            }
        }
    }


    func serverSettingsDidChange() {
        connect(userInitiated: false)
        PhoneConnectivityManager.shared.syncServerConfigToWatch()
        DeviceTokenUploader.shared.uploadIfNeeded()
    }

    func reconnectIfNeeded() {
        queue.async { [weak self] in
            guard let self else { return }
            Task { @MainActor in
                guard self.isSyncActive else { return }
                self._openWS()
            }
        }
    }

    func disconnect(userInitiated: Bool = true) {
        if userInitiated {
            self.isSyncActive = false
        }
        queue.async { [weak self] in
            guard let self else { return }
            Task { @MainActor in
                self.shouldReconnect = false
                self._stopHeartbeat()
                self.wsTask?.cancel(with: .normalClosure, reason: nil)
                self.wsTask = nil
                self._resetWSProtocolState()
                self.isConnected = false
                HealthKitManager.bgLog("WS: User disconnected")
            }
        }
    }

    func send(_ payload: HealthPayload) {
        PendingStore.shared.append([payload])
        if isSyncEnabled {
            flushPending()
        }
    }

    func flushPending(appState: String = "foreground") {
        Task {
            guard self.isSyncEnabled else { return }
            await self._flush(appState: appState)
        }
    }

    func flushPendingAndWait(appState: String = "foreground") async {
        guard self.isSyncEnabled else { return }
        await self._flush(appState: appState)
    }


    private func _flush(appState: String = "foreground") async {


        guard isSyncEnabled else { return }
        guard !_isFlushing else { return }
        _isFlushing = true
        defer { _isFlushing = false }


        let isActive = (appState == "foreground" || appState == "active")
        while true {


            guard !Task.isCancelled else {
                HealthKitManager.bgLog("📤 FLUSH: cancelled — \(PendingStore.shared.count) records kept")
                return
            }
            guard isSyncEnabled else { return }
            let storeCount = PendingStore.shared.count
            guard storeCount > 0 else { return }

            if appState != "background" && isConnected && wsHelloSettled {


                guard pendingHTTPTasks.isEmpty else {
                    HealthKitManager.bgLog("📤 FLUSH: deferred — \(pendingHTTPTasks.count) HTTP upload(s) still in flight")
                    return
                }
                guard Date() >= wsPausedUntil else {
                    HealthKitManager.bgLog("📤 FLUSH: paused — server refused data, \(storeCount) records kept")
                    return
                }
                let payloads = PendingStore.shared.peek(limit: chunkSize)
                guard !payloads.isEmpty else { return }
                HealthKitManager.bgLog("📤 FLUSH: \(payloads.count)/\(storeCount) records via WS (appState=\(appState))")
                switch await _sendWS(payloads) {
                case .sent:

                    continue
                case .paused:
                    _scheduleWSResume()
                    return
                case .failed:


                    return
                }
            }

            if isActive {
                HealthKitManager.bgLog("📤 FLUSH: deferred — WS not ready, \(storeCount) queued (will drain on onWSOpened)")
                return
            }


            if !pendingHTTPTasks.isEmpty { return }
            let payloads = PendingStore.shared.peek(limit: chunkSize)
            guard !payloads.isEmpty else { return }
            HealthKitManager.bgLog("📤 FLUSH: \(payloads.count)/\(storeCount) records via HTTP (appState=\(appState))")
            _sendHTTP(payloads, appState: appState)


            return
        }
    }

    private enum WSSendOutcome {
        case sent
        case paused
        case failed
    }

    private func _sendWS(_ payloads: [HealthPayload]) async -> WSSendOutcome {
        let wireItems = payloads.map { ["ts": $0.ts, "f": $0.f, "v": $0.v] }


        let useAck = wsServerAcks
        let batchID = UUID().uuidString
        let body: Any
        if useAck {
            body = ["id": batchID, "items": wireItems] as [String: Any]
        } else {
            body = wireItems
        }
        guard let data = try? JSONSerialization.data(withJSONObject: body) else {
            HealthKitManager.bgLog("WS: payload serialization failed — batch kept")
            return .failed
        }

        let sent: Bool = await withCheckedContinuation { (continuation: CheckedContinuation<Bool, Never>) in
            guard let task = wsTask else {


                isConnected = false
                continuation.resume(returning: false)
                return
            }

            let guardBox = ContinuationGuard(continuation)


            let timeoutTask = Task { @MainActor in
                try? await Task.sleep(nanoseconds: 60 * 1_000_000_000)
                guard !Task.isCancelled else { return }
                if guardBox.resume(false) {
                    HealthKitManager.bgLog("WS: Send timeout — forcing resume")
                    self.isConnected = false
                    self.wsTask?.cancel(with: .abnormalClosure, reason: nil)
                    self.wsTask = nil
                    self._resetWSProtocolState()
                    if self.shouldReconnect { self._scheduleReconnect() }
                }
            }

            task.send(.data(data)) { [weak self] error in
                guard let self else {
                    Task { @MainActor in
                        timeoutTask.cancel()
                        guardBox.resume(false)
                    }
                    return
                }
                Task { @MainActor in
                    timeoutTask.cancel()
                    if let error = error {
                        HealthKitManager.bgLog("WS: Send ERR — \(error.localizedDescription)")
                        self.isConnected = false
                        guardBox.resume(false)
                    } else {
                        guardBox.resume(true)
                    }
                }
            }
        }
        guard sent else { return .failed }

        guard useAck else {

            _commitWSBatch(payloads.count)
            return .sent
        }

        switch await _awaitAck(id: batchID) {
        case .acked:
            _commitWSBatch(payloads.count)
            return .sent
        case .nacked(let reason):
            HealthKitManager.bgLog("WS: server NACK (\(reason)) — \(payloads.count) records kept, pausing \(Int(wsNackPause))s")
            wsPausedUntil = Date().addingTimeInterval(wsNackPause)
            return .paused
        case .failed:
            HealthKitManager.bgLog("WS: no ack within \(Int(wsAckTimeout))s — \(payloads.count) records kept, reconnecting")
            if let ws = wsTask {
                _stopHeartbeat()
                ws.cancel(with: .abnormalClosure, reason: nil)
                wsTask = nil
                isConnected = false
                _resetWSProtocolState()
                if shouldReconnect { _scheduleReconnect() }
            }
            return .failed
        }
    }

    private func _commitWSBatch(_ count: Int) {
        PendingStore.shared.pop(count: count)
        HealthKitManager.shared.markOldestAsSynced(count: count)
        HealthKitManager.bgLog("WS: Sent \(count) records")
    }


    private func _awaitAck(id: String) async -> WSAckResult {
        await withCheckedContinuation { (continuation: CheckedContinuation<WSAckResult, Never>) in
            let waiter = AckWaiter(id: id, continuation: continuation)
            wsAckWaiter = waiter
            let timeout = wsAckTimeout
            Task { @MainActor [weak self, weak waiter] in
                try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
                guard let waiter, waiter.resolve(.failed) else { return }
                if self?.wsAckWaiter === waiter { self?.wsAckWaiter = nil }
            }
        }
    }


    private func _handleWSMessage(_ message: URLSessionWebSocketTask.Message, from ws: URLSessionWebSocketTask) {
        guard wsTask === ws else { return }
        let raw: Data?
        switch message {
        case .string(let text): raw = text.data(using: .utf8)
        case .data(let d):      raw = d
        @unknown default:       raw = nil
        }
        guard let raw,
              let obj = try? JSONSerialization.jsonObject(with: raw) as? [String: Any],
              let type = obj["type"] as? String else { return }

        switch type {
        case "hello":
            if let level = obj["ack"] as? Int, level >= 1 { wsServerAcks = true }
            wsHelloSettled = true
            HealthKitManager.bgLog("WS: server hello (ack=\(wsServerAcks))")
        case "ack":
            _deliverAck(.acked, id: obj["id"] as? String)
        case "nack":
            _deliverAck(.nacked(obj["reason"] as? String ?? "unknown"), id: obj["id"] as? String)
        default:
            break
        }
    }

    private func _deliverAck(_ result: WSAckResult, id: String?) {
        guard let waiter = wsAckWaiter else { return }


        if let id, id != waiter.id { return }
        wsAckWaiter = nil
        waiter.resolve(result)
    }


    private func _resetWSProtocolState() {
        wsServerAcks = false
        wsHelloSettled = false
        if let waiter = wsAckWaiter {
            wsAckWaiter = nil
            waiter.resolve(.failed)
        }
    }


    private func _scheduleWSResume() {
        wsResumeTask?.cancel()
        let delay = max(1.0, wsPausedUntil.timeIntervalSinceNow)
        wsResumeTask = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard !Task.isCancelled, let self else { return }
            await self._flush()
        }
    }

    private func _sendHTTP(_ payloads: [HealthPayload], appState: String) {
        guard let url = URL(string: httpBaseURL + "/ingest") else {
            HealthKitManager.bgLog("HTTP: Invalid ingest URL from httpBaseURL: \(httpBaseURL)")
            return
        }
        var request = APIClient.request(url, method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(appState, forHTTPHeaderField: "X-Sync-Mode")

        let wireData = payloads.map { ["ts": $0.ts, "f": $0.f, "v": $0.v] }
        guard let data = try? JSONSerialization.data(withJSONObject: wireData) else { return }

        let tmp = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".json")
        try? data.write(to: tmp)

        let useFG = (appState == "foreground" || appState == "active")
        let session = useFG ? fgSession : bgSession
        let task = session.uploadTask(with: request, fromFile: tmp)
        task.taskDescription = tmp.absoluteString
        pendingHTTPTasks[SessionDelegate.taskKey(session: session, task: task)] = payloads.count
        task.resume()
        HealthKitManager.bgLog("HTTP: Resume \(appState) upload (\(payloads.count)) — Backlog: \(PendingStore.shared.count)")
    }


    func onWSTaskFailed(task: URLSessionTask, error: Error) {
        queue.async { [weak self] in
            guard let self else { return }
            Task { @MainActor in
                guard self.wsTask?.taskIdentifier == task.taskIdentifier else { return }
                self._stopHeartbeat()
                self.wsTask = nil
                self._resetWSProtocolState()
                self.isConnected = false
                HealthKitManager.bgLog("WS: Error — \(error.localizedDescription)")
                if self.shouldReconnect { self._scheduleReconnect() }
            }
        }
    }

    func onWSOpened() async {
        self.isConnected = true
        self.reconnectDelay = 1.0
        HealthKitManager.bgLog("WS: Connected")
        guard let ws = wsTask else { return }
        _startHeartbeat(ws)


        if !wsHelloSettled {
            for _ in 0..<20 {
                if wsHelloSettled || wsTask !== ws { break }
                try? await Task.sleep(nanoseconds: 100_000_000)
            }
        }
        guard wsTask === ws else { return }
        wsHelloSettled = true
        await _flush()
    }

    func onHTTPTaskSucceeded(key: String) {
        queue.async { [weak self] in
            guard let self else { return }
            Task { @MainActor in
                guard let count = self.pendingHTTPTasks.removeValue(forKey: key) else { return }
                PendingStore.shared.pop(count: count)
                HealthKitManager.shared.markOldestAsSynced(count: count)
                HealthKitManager.bgLog("HTTP: Success (\(count) records)")
                self.httpRetryDelay = 2.0
                self.httpConsecutiveClientErrors = 0


                if PendingStore.shared.count > 0 { await self._flush(appState: "background") }
            }
        }
    }

    func onHTTPTaskFailed(key: String, error: Error) {
        queue.async { [weak self] in
            guard let self else { return }
            Task { @MainActor in


                if let count = self.pendingHTTPTasks.removeValue(forKey: key) {
                    HealthKitManager.bgLog("HTTP: Task failed (\(count) records kept for retry) — \(error.localizedDescription)")
                    self._noteHTTPFailure(error, batchCount: count)
                } else {
                    HealthKitManager.bgLog("HTTP: Task failed — \(error.localizedDescription)")
                }


                guard PendingStore.shared.count > 0 else { return }
                let delay = self.httpRetryDelay
                self.httpRetryDelay = min(self.httpRetryDelay * 2, self.maxHttpRetryDelay)
                try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
                await self._flush(appState: "background")
            }
        }
    }


    private func _noteHTTPFailure(_ error: Error, batchCount: Int) {
        let ns = error as NSError
        let isPoisonStatus = ns.domain == "hime.http"
            && (400..<500).contains(ns.code)
            && ![401, 403, 408, 429].contains(ns.code)
        guard isPoisonStatus else {
            httpConsecutiveClientErrors = 0
            return
        }
        httpConsecutiveClientErrors += 1
        guard httpConsecutiveClientErrors >= maxHttpClientErrors else { return }
        httpConsecutiveClientErrors = 0

        let batch = PendingStore.shared.peek(limit: batchCount)
        guard !batch.isEmpty else { return }
        let fm = FileManager.default
        if let dir = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask).first {
            let file = dir.appendingPathComponent("quarantine_\(Int(Date().timeIntervalSince1970)).json")
            if let data = try? JSONEncoder().encode(batch) {
                try? data.write(to: file, options: .atomic)
            }
        }
        PendingStore.shared.pop(count: batch.count)
        HealthKitManager.bgLog("HTTP: quarantined \(batch.count) records after \(maxHttpClientErrors) consecutive rejections (HTTP \(ns.code))")
    }


    private func _openWS() {
        guard wsTask == nil else { return }

        guard var components = URLComponents(string: serverURL) else { return }

        let path = components.path.hasSuffix("/") ? components.path.dropLast() : components.path[...]
        if !path.hasSuffix("/ws") {
            components.path = String(path) + "/ws"
        }
        guard let url = components.url else { return }


        var wsRequest = URLRequest(url: url)
        let token = ServerConfig.authToken
        if !token.isEmpty {
            wsRequest.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }

        shouldReconnect = true
        _resetWSProtocolState()


        let ws = fgSession.webSocketTask(with: wsRequest)
        wsTask = ws
        ws.resume()

        ws.sendPing { _ in }
        _receiveLoop(ws)
    }

    private func _receiveLoop(_ ws: URLSessionWebSocketTask) {
        ws.receive { [weak self] result in
            guard let self else { return }
            self.queue.async {
                Task { @MainActor in
                    switch result {
                    case .success(let message):
                        self._handleWSMessage(message, from: ws)
                        self._receiveLoop(ws)
                    case .failure:
                        guard self.wsTask === ws else { return }
                        self._stopHeartbeat()
                        self.wsTask = nil
                        self._resetWSProtocolState()
                        self.isConnected = false
                        if self.shouldReconnect { self._scheduleReconnect() }
                    }
                }
            }
        }
    }


    private func _failHeartbeat(_ ws: URLSessionWebSocketTask, reason: String) {
        guard self.wsTask === ws else { return }
        HealthKitManager.bgLog("WS: \(reason)")
        _stopHeartbeat()
        wsTask?.cancel(with: .abnormalClosure, reason: nil)
        wsTask = nil
        _resetWSProtocolState()
        isConnected = false
        if shouldReconnect { _scheduleReconnect() }
    }

    private func _startHeartbeat(_ ws: URLSessionWebSocketTask) {
        _stopHeartbeat()
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + heartbeatInterval, repeating: heartbeatInterval)


        let workQueue = queue
        let pongTimeout = heartbeatTimeout
        timer.setEventHandler { [weak self, weak ws] in
            guard let self, let ws else { return }


            let timeoutItem = DispatchWorkItem { [weak self, weak ws] in
                guard let self, let ws else { return }
                Task { @MainActor in
                    self._failHeartbeat(ws, reason: "Heartbeat pong timeout (\(Int(pongTimeout))s)")
                }
            }
            workQueue.asyncAfter(deadline: .now() + pongTimeout, execute: timeoutItem)
            ws.sendPing { [weak self] error in
                timeoutItem.cancel()
                guard let self else { return }
                if let error = error {

                    workQueue.async {
                        Task { @MainActor in
                            self._failHeartbeat(ws, reason: "Heartbeat ping failed — \(error.localizedDescription)")
                        }
                    }
                }

            }
        }
        timer.resume()
        heartbeatTimer = timer
    }

    private func _stopHeartbeat() {
        heartbeatTimer?.cancel()
        heartbeatTimer = nil
    }

    private func _scheduleReconnect() {
        let delay = reconnectDelay
        reconnectDelay = min(reconnectDelay * 2, maxReconnectDelay)
        queue.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self else { return }
            Task { @MainActor in
                guard self.shouldReconnect else { return }
                self._openWS()
            }
        }
    }


    private var backgroundCompletionHandlers: [String: () -> Void] = [:]

    func addBackgroundCompletionHandler(identifier: String, completion: @escaping () -> Void) {
        backgroundCompletionHandlers[identifier] = completion
    }

    func executeBackgroundCompletionHandler(for identifier: String) {
        if let completion = backgroundCompletionHandlers.removeValue(forKey: identifier) {
            completion()
        }
    }
}
