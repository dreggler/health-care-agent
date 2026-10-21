import Foundation
import Network

@MainActor
final class ChatStreamClient: NSObject {
    private var task: URLSessionWebSocketTask?
    private var session: URLSession?
    private var tickTimer: Timer?
    private var reconnectTask: Task<Void, Never>?
    private var pathMonitor: NWPathMonitor?
    private var lastPathKey: String?


    private var wantsConnection = false

    private var isOpen = false {
        didSet { if isOpen != oldValue { onLiveChange?(isOpen) } }
    }


    private var generation = 0
    private var backoff: TimeInterval = ChatStreamClient.minBackoff
    private var lastReceive = Date()
    private var lastPing = Date.distantPast

    private static let minBackoff: TimeInterval = 1
    private static let maxBackoff: TimeInterval = 30
    private static let pingInterval: TimeInterval = 20
    private static let silenceLimit: TimeInterval = 45
    private static let tickInterval: TimeInterval = 5


    var onEvent: (([String: Any]) -> Void)?

    var onConnected: (() -> Void)?


    var onLiveChange: ((Bool) -> Void)?

    var isLive: Bool { isOpen }


    private func streamURL() -> URL? {
        let base = ServerConfig.load().apiBaseURL
        var wsBase = base
        if base.hasPrefix("https://") {
            wsBase = "wss://" + base.dropFirst("https://".count)
        } else if base.hasPrefix("http://") {
            wsBase = "ws://" + base.dropFirst("http://".count)
        }


        var comps = URLComponents(string: "\(wsBase)/api/stream/agent/LiveUser")
        comps?.queryItems = [URLQueryItem(name: "client", value: "ios")]
        return comps?.url
    }


    func connect() {
        wantsConnection = true
        startPathMonitor()
        if task != nil {


            if Date().timeIntervalSince(lastReceive) > Self.silenceLimit { reconnectNow() }
            return
        }
        reconnectTask?.cancel()
        reconnectTask = nil
        open()
    }

    func disconnect() {
        wantsConnection = false
        reconnectTask?.cancel()
        reconnectTask = nil
        pathMonitor?.cancel()
        pathMonitor = nil
        lastPathKey = nil
        isOpen = false
        backoff = Self.minBackoff
        dropSocket()
    }


    private func open() {
        guard wantsConnection, task == nil, let url = streamURL() else { return }
        generation += 1
        let gen = generation
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 60
        let session = URLSession(configuration: cfg)


        var req = URLRequest(url: url)
        let token = ServerConfig.authToken
        if !token.isEmpty {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        let task = session.webSocketTask(with: req)
        self.session = session
        self.task = task
        isOpen = false
        lastReceive = Date()
        lastPing = .distantPast
        task.resume()
        receiveLoop(gen)
        startTimer(gen)
        sendPing(gen)
    }


    private func dropSocket() {
        generation += 1
        tickTimer?.invalidate()
        tickTimer = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        session?.invalidateAndCancel()
        session = nil
    }


    private func socketFailed(_ gen: Int) {
        guard gen == generation else { return }
        isOpen = false
        dropSocket()
        scheduleReconnect()
    }

    private func scheduleReconnect() {
        guard wantsConnection, reconnectTask == nil else { return }
        let delay = backoff
        backoff = min(backoff * 2, Self.maxBackoff)
        reconnectTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard !Task.isCancelled, let self else { return }
            self.reconnectTask = nil
            self.open()
        }
    }


    private func reconnectNow() {
        guard wantsConnection else { return }
        reconnectTask?.cancel()
        reconnectTask = nil
        isOpen = false
        backoff = Self.minBackoff
        dropSocket()
        open()
    }

    private func markOpen(_ gen: Int) {
        guard gen == generation, !isOpen else { return }
        isOpen = true
        backoff = Self.minBackoff
        onConnected?()
    }


    private func startTimer(_ gen: Int) {
        tickTimer?.invalidate()
        tickTimer = Timer.scheduledTimer(withTimeInterval: Self.tickInterval, repeats: true) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in self.tick(gen) }
        }
    }

    private func tick(_ gen: Int) {
        guard gen == generation else { return }
        let now = Date()
        if now.timeIntervalSince(lastReceive) > Self.silenceLimit {
            socketFailed(gen)
            return
        }
        if now.timeIntervalSince(lastPing) >= Self.pingInterval { sendPing(gen) }
    }


    private func sendPing(_ gen: Int) {
        guard gen == generation, let task else { return }
        lastPing = Date()
        task.send(.string("{\"type\":\"ping\"}")) { [weak self] error in
            guard error != nil, let self else { return }
            Task { @MainActor in self.socketFailed(gen) }
        }
        task.sendPing { [weak self] error in
            guard error != nil, let self else { return }
            Task { @MainActor in self.socketFailed(gen) }
        }
    }


    private func receiveLoop(_ gen: Int) {
        guard gen == generation, let task else { return }
        task.receive { [weak self] result in
            guard let self else { return }
            Task { @MainActor in
                guard gen == self.generation else { return }
                switch result {
                case .failure:
                    self.socketFailed(gen)
                case .success(let message):
                    self.lastReceive = Date()
                    var text: String?
                    switch message {
                    case .string(let s): text = s
                    case .data(let d): text = String(data: d, encoding: .utf8)
                    @unknown default: break
                    }
                    if let text, self.handle(text) { self.markOpen(gen) }


                    self.receiveLoop(gen)
                }
            }
        }
    }


    private func handle(_ text: String) -> Bool {


        let head = text.prefix(48)
        if head.contains("\"pong\"") || head.contains("\"status_update\"") { return true }
        guard let data = text.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return true }


        if (obj["type"] as? String) == "error", !isOpen {
            socketFailed(generation)
            return false
        }
        onEvent?(obj)
        return true
    }


    private func startPathMonitor() {
        guard pathMonitor == nil else { return }
        let monitor = NWPathMonitor()
        monitor.pathUpdateHandler = { [weak self] path in
            let satisfied = path.status == .satisfied
            let key = "\(satisfied)-" + path.availableInterfaces.map { "\($0.type)" }.joined(separator: ",")
            Task { @MainActor [weak self] in
                guard let self else { return }
                let changed = self.lastPathKey != nil && self.lastPathKey != key
                self.lastPathKey = key
                if changed && satisfied { self.reconnectNow() }
            }
        }
        monitor.start(queue: DispatchQueue(label: "hime.chat.path"))
        pathMonitor = monitor
    }
}
