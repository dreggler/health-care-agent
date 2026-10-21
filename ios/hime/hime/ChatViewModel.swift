import Combine
import Foundation
import SwiftUI

@MainActor
final class ChatViewModel: ObservableObject {
    @Published var messages: [ChatMessage] = [] {
        didSet {
            rows = Self.makeRows(messages, turns: turns)
            syncTurnReplies()
        }
    }


    private var turns: [TurnState] = [] {
        didSet {
            rows = Self.makeRows(messages, turns: turns)
            syncTurnReplies()
        }
    }


    @Published private(set) var rows: [ChatRow] = []


    let live = LiveState()

    @Published private(set) var isBusy = false

    @Published private(set) var stopAvailable = true
    @Published var agentStarting = false

    @Published var errorBanner: String?

    @Published private(set) var didLoadHistory = false


    let threadId: String


    private(set) var isVisible = false

    private var turnByRun: [String: TurnState] = [:]

    private var pendingTurns: [TurnState] = []


    private var sawRunDone = false
    private var settleTask: Task<Void, Never>?

    private var runSerial = 0
    private var lastEventAt = Date()
    private var watchdogTask: Task<Void, Never>?
    private static let maxRuns = 40


    private var pendingResend: [String] = []
    private var resendTask: Task<Void, Never>?
    private var bannerTask: Task<Void, Never>?
    private var isReconciling = false
    private var needsAnotherReconcile = false

    private var apiBase: String { ServerConfig.load().apiBaseURL }

    init(threadId: String) {
        self.threadId = threadId
    }


    func onAppear() {
        isVisible = true
        Task { await reconcile() }
    }

    func onDisappear() { isVisible = false }


    func foregrounded() {
        Task { await reconcile() }
    }


    func streamConnected() {
        if isVisible { Task { await reconcile() } }
        if !pendingResend.isEmpty { Task { await flushPending() } }
    }

    private func showBanner(_ text: String) {
        errorBanner = text
        bannerTask?.cancel()
        bannerTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 4_000_000_000)
            guard !Task.isCancelled, let self else { return }
            self.errorBanner = nil
        }
    }


    func send(text rawText: String, image: Data?) {
        let text = rawText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty || image != nil else { return }
        let msg = ChatMessage(role: .user, text: text, localImage: image,
                              clientMsgId: UUID().uuidString, delivery: .sending)
        messages.append(msg)
        beginTurn(anchor: msg.id)
        ChatHub.shared.noteOutgoing(threadId: threadId, text: text.isEmpty ? String(localized: "Image") : text)
        Task { await deliver(messageId: msg.id) }
    }


    func retry(messageId: String) {
        guard let i = messages.firstIndex(where: { $0.id == messageId }),
              messages[i].delivery == .failed else { return }
        messages[i].delivery = .sending
        beginTurn(anchor: messageId)
        Task { await deliver(messageId: messageId) }
    }

    private enum PostOutcome {
        case queued
        case starting
        case failed(String)
    }


    private func deliver(messageId: String) async {
        guard let msg = messages.first(where: { $0.id == messageId }) else { return }
        let outcome = await post(text: msg.text, image: msg.localImage,
                                 clientMsgId: msg.clientMsgId ?? UUID().uuidString)
        switch outcome {
        case .queued:
            setDelivery(messageId, .sent)
            pendingResend.removeAll { $0 == messageId }
            if pendingResend.isEmpty { agentStarting = false }


            await reconcile()
        case .starting:

            if !pendingResend.contains(messageId) { pendingResend.append(messageId) }
            agentStarting = true
            armResendWatchdog()
        case .failed(let reason):


            if messages.first(where: { $0.id == messageId })?.serverId != nil { return }
            setDelivery(messageId, .failed)
            pendingResend.removeAll { $0 == messageId }
            if pendingResend.isEmpty { agentStarting = false }
            if let t = pendingTurns.first(where: { $0.anchorId == messageId }) { finish(t) }
            showBanner(reason)
        }
    }

    private func setDelivery(_ id: String, _ state: ChatMessage.Delivery) {
        guard let i = messages.firstIndex(where: { $0.id == id }),
              messages[i].delivery != state else { return }
        messages[i].delivery = state
    }

    private func post(text: String, image: Data?, clientMsgId: String) async -> PostOutcome {
        guard let url = URL(string: "\(apiBase)/api/agent/chat") else {
            return .failed(String(localized: "Server address is invalid."))
        }
        var req = APIClient.request(url, method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = ["text": text, "client_msg_id": clientMsgId,
                                   "thread_id": threadId]
        if let image {
            body["image_base64"] = image.base64EncodedString()
            body["image_mime"] = "image/jpeg"
        }
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            guard let http = resp as? HTTPURLResponse else {
                return .failed(String(localized: "Couldn't send. Tap the message to retry."))
            }
            if http.statusCode == 429 {
                return .failed(String(localized: "You're sending too fast. Wait a moment, then tap to retry."))
            }
            guard (200..<300).contains(http.statusCode) else {
                return .failed(String(localized: "Couldn't send (error \(http.statusCode)). Tap the message to retry."))
            }
            if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               (obj["queued"] as? Bool) == false || (obj["status"] as? String) == "starting" {
                return .starting
            }
            return .queued
        } catch {
            return .failed(String(localized: "Couldn't reach Hime. Tap the message to retry."))
        }
    }


    private func flushPending() async {
        let ids = pendingResend
        pendingResend = []
        for id in ids { await deliver(messageId: id) }
    }


    private func armResendWatchdog() {
        guard resendTask == nil else { return }
        resendTask = Task { [weak self] in
            var tries = 0
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 15_000_000_000)
                guard !Task.isCancelled, let self else { return }
                if self.pendingResend.isEmpty { break }
                tries += 1
                if tries > 6 {
                    let ids = self.pendingResend
                    self.pendingResend = []
                    for id in ids { self.setDelivery(id, .failed) }
                    self.agentStarting = false
                    self.finishAll()
                    self.showBanner(String(localized: "Hime didn't wake up. Tap the message to retry."))
                    break
                }
                await self.flushPending()
            }
            self?.resendTask = nil
        }
    }


    private func fetchHistory() async -> [ChatHistoryRow]? {
        let tid = threadId.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? threadId
        guard let url = URL(string: "\(apiBase)/api/agent/chat-history?limit=100&thread_id=\(tid)") else { return nil }
        do {
            let (data, resp) = try await URLSession.shared.data(for: APIClient.request(url))
            if let http = resp as? HTTPURLResponse, !(200..<300).contains(http.statusCode) { return nil }
            return try JSONDecoder().decode(ChatHistoryResponse.self, from: data).messages
        } catch { return nil }
    }


    func reconcile() async {
        if isReconciling { needsAnotherReconcile = true; return }
        isReconciling = true
        defer { isReconciling = false }
        repeat {
            needsAnotherReconcile = false
            guard let history = await fetchHistory() else { return }
            merge(history)
            didLoadHistory = true
        } while needsAnotherReconcile
    }


    private func merge(_ history: [ChatHistoryRow]) {
        guard !history.isEmpty else { return }
        let locals = messages
        var byServerId: [Int: Int] = [:]
        var byClientId: [String: Int] = [:]
        var unsyncedByHash: [String: [Int]] = [:]
        var unsyncedByText: [String: [Int]] = [:]
        for (i, m) in locals.enumerated() {
            if let sid = m.serverId { byServerId[sid] = i }
            if let cid = m.clientMsgId { byClientId[cid] = i }
            if m.serverId == nil {
                if let h = m.messageHash { unsyncedByHash[h, default: []].append(i) }
                unsyncedByText[Self.textKey(role: m.role, text: m.text, hasImage: m.imagePath != nil),
                               default: []].append(i)
            }
        }

        var used = Set<Int>()
        var merged: [ChatMessage] = []
        var addedAssistant = false
        for row in history {
            let role: ChatMessage.Role = row.role == "assistant" ? .assistant : .user
            var match: Int?
            if let sid = row.id, let i = byServerId[sid], !used.contains(i) {
                match = i
            } else if let cid = row.client_msg_id, let i = byClientId[cid], !used.contains(i) {
                match = i
            } else if let h = row.message_hash,
                      let i = unsyncedByHash[h]?.first(where: { !used.contains($0) && locals[$0].role == role }) {
                match = i
            } else {
                let key = Self.textKey(role: role, text: row.content, hasImage: row.image_id != nil)
                match = unsyncedByText[key]?.first(where: { !used.contains($0) })
            }
            if let i = match {
                used.insert(i)
                var m = locals[i]
                m.serverId = row.id ?? m.serverId
                if m.messageHash == nil { m.messageHash = row.message_hash }
                if m.reportId == nil { m.reportId = row.report_id }
                if m.imagePath == nil, let img = row.image_id { m.imagePath = Self.imagePath(img) }
                if m.clientMsgId == nil { m.clientMsgId = row.client_msg_id }
                if m.role == .user { m.delivery = .sent }
                merged.append(m)
            } else {
                if role == .assistant { addedAssistant = true }
                merged.append(Self.message(from: row))
            }
        }


        let ids = history.compactMap { $0.id }
        let lo = ids.min(), hi = ids.max()
        var prefix: [ChatMessage] = []
        var tail: [ChatMessage] = []
        for (i, m) in locals.enumerated() where !used.contains(i) {
            if let sid = m.serverId {
                if let lo, sid < lo { prefix.append(m) }
                else if let hi, sid > hi { tail.append(m) }
            } else {
                tail.append(m)
            }
        }
        let result = prefix + merged + tail
        if result != messages { messages = result }

        if addedAssistant, result.last?.role == .assistant { finishAll() }
    }

    private static func textKey(role: ChatMessage.Role, text: String, hasImage: Bool) -> String {
        "\(role.rawValue)|\(hasImage ? 1 : 0)|\(text.trimmingCharacters(in: .whitespacesAndNewlines))"
    }

    private static func imagePath(_ imageId: String) -> String { "/api/agent/chat-image/\(imageId)" }

    private static func message(from row: ChatHistoryRow) -> ChatMessage {
        ChatMessage(role: row.role == "assistant" ? .assistant : .user,
                    text: row.content,
                    imagePath: row.image_id.map { Self.imagePath($0) },
                    messageHash: row.message_hash,
                    reportId: row.report_id,
                    serverId: row.id,
                    clientMsgId: row.client_msg_id,
                    timestamp: row.date ?? Date())
    }

    private static func makeRows(_ messages: [ChatMessage], turns: [TurnState]) -> [ChatRow] {
        var byAnchor: [String: [TurnState]] = [:]
        var orphans: [TurnState] = []
        let ids = Set(messages.map { $0.id })
        for t in turns {
            if let a = t.anchorId, ids.contains(a) {
                byAnchor[a, default: []].append(t)
            } else {
                orphans.append(t)
            }
        }
        let turnIds = Set(turns.map { $0.id })
        var out: [ChatRow] = []
        out.reserveCapacity(messages.count + turns.count)
        var prevRole: ChatMessage.Role?
        for m in messages {

            let inTurn = m.role == .assistant && m.turnId.map { turnIds.contains($0) } == true
            if !inTurn {
                out.append(ChatRow(item: .message(m), showAvatar: prevRole != m.role))
                prevRole = m.role
            }
            if let ts = byAnchor[m.id] {
                for t in ts { out.append(ChatRow(item: .turn(t), showAvatar: false)) }
                prevRole = nil
            }
        }
        for t in orphans { out.append(ChatRow(item: .turn(t), showAvatar: false)) }
        return out
    }


    private func syncTurnReplies() {
        guard !turns.isEmpty else { return }
        var byTurn: [String: [ChatMessage]] = [:]
        for m in messages where m.role == .assistant {
            if let id = m.turnId { byTurn[id, default: []].append(m) }
        }
        for t in turns {
            let next = byTurn[t.id] ?? []
            if t.replies != next { t.replies = next }
        }
    }

    func evidence(for message: ChatMessage) async -> String? {
        guard let hash = message.messageHash,
              let url = URL(string: "\(apiBase)/api/agent/evidence/\(hash)") else { return nil }
        do {
            let (data, _) = try await URLSession.shared.data(for: APIClient.request(url))
            let resp = try JSONDecoder().decode(EvidenceResponse.self, from: data)
            return resp.found ? resp.formatted : nil
        } catch { return nil }
    }


    func clearConversation() {
        Task {
            switch await post(text: "/clear", image: nil, clientMsgId: UUID().uuidString) {
            case .queued:
                clearLocal()
            case .starting:
                showBanner(String(localized: "Hime is waking up. Try clearing again in a moment."))
            case .failed:
                showBanner(String(localized: "Couldn't clear the conversation. Please try again."))
            }
        }
    }

    private func clearLocal() {
        finishAll()
        settleTask?.cancel()
        settleTask = nil
        pendingTurns.removeAll()
        turnByRun.removeAll()
        turns.removeAll()
        messages.removeAll()
        pendingResend.removeAll()
        agentStarting = false
    }


    private func beginTurn(anchor: String?) {
        let t = TurnState(anchorId: anchor)
        pendingTurns.append(t)
        addTurn(t)
        runSerial += 1
        touch()
        syncLive()
        startWatchdog()
    }

    private func addTurn(_ t: TurnState) {
        var next = turns
        next.append(t)
        if next.count > Self.maxRuns {

            var excess = next.count - Self.maxRuns
            next.removeAll { old in
                guard excess > 0, old.phase != .live else { return false }
                excess -= 1
                if let rid = old.runId { turnByRun[rid] = nil }
                return true
            }
        }
        turns = next
    }

    private func removeTurn(_ t: TurnState) {
        pendingTurns.removeAll { $0 === t }
        if let rid = t.runId { turnByRun[rid] = nil }
        turns.removeAll { $0 === t }
    }

    private static func runId(of event: [String: Any]) -> String? {
        guard let r = event["run_id"] as? String, !r.isEmpty else { return nil }
        return r
    }


    private func turn(for event: [String: Any], create: Bool) -> TurnState? {
        if let rid = Self.runId(of: event) {
            if let t = turnByRun[rid] { return t }
            guard create else { return nil }
            let t: TurnState
            if !pendingTurns.isEmpty {
                t = pendingTurns.removeFirst()
            } else {
                t = TurnState(anchorId: messages.last?.id)
                addTurn(t)
            }
            t.runId = rid
            turnByRun[rid] = t
            return t
        }
        if let t = turns.first(where: { $0.phase == .live }) { return t }
        guard create else { return nil }
        let t = TurnState(anchorId: messages.last?.id)
        addTurn(t)
        return t
    }


    private func activeTurn(for event: [String: Any]) -> TurnState? {
        guard let t = turn(for: event, create: true) else { return nil }
        if t.phase == .stopped { return nil }
        if t.phase == .done {
            if t.closedByServer { return nil }
            t.reopen()
        }
        touch()
        syncLive()
        startWatchdog()
        return t
    }

    private func touch() {
        lastEventAt = Date()
        settleTask?.cancel()
        settleTask = nil
    }


    private func syncLive() {
        let lt = turns.first(where: { $0.phase == .live })
        let active = lt != nil
        if live.active != active { live.active = active }
        let tool = lt.flatMap { $0.runningTool ?? ($0.writingReply ? "reply_user" : nil) }
        if live.tool != tool { live.tool = tool }
        let replying = !(lt?.streamText.isEmpty ?? true)
        if live.replying != replying { live.replying = replying }
        if isBusy != active { isBusy = active }
        if !active {
            watchdogTask?.cancel()
            watchdogTask = nil
        }
    }


    private func finish(_ t: TurnState, stopped: Bool = false, byServer: Bool = false) {
        if byServer { t.closedByServer = true }
        if !t.hasContent && t.runId == nil && t.errorText == nil && t.phase == .live {
            removeTurn(t)
        } else {
            pendingTurns.removeAll { $0 === t }
            t.settle(stopped: stopped)
        }
        syncLive()
    }

    private func finishAll(stopped: Bool = false, error: String? = nil) {
        for t in turns where t.phase == .live {
            if let error { t.errorText = error }
            finish(t, stopped: stopped)
        }
        syncLive()
    }


    private func armSettle(_ t: TurnState) {
        guard !sawRunDone else { return }
        settleTask?.cancel()
        settleTask = Task { [weak self, weak t] in
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            guard !Task.isCancelled, let self, let t else { return }
            self.settleTask = nil
            if t.phase == .live { self.finish(t) }
        }
    }


    private func startWatchdog() {
        guard watchdogTask == nil else { return }
        watchdogTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 15_000_000_000)
                guard !Task.isCancelled, let self else { return }
                if !self.live.active { break }
                if Date().timeIntervalSince(self.lastEventAt) > 150 {
                    self.finishAll(error: String(localized: "Hime stopped responding."))
                    return
                }
            }
            self?.watchdogTask = nil
        }
    }


    func stop() {
        guard live.active else { return }
        let serial = runSerial
        Task {
            guard let url = URL(string: "\(apiBase)/api/agent/chat/stop") else { return }
            var req = APIClient.request(url, method: "POST")
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: ["thread_id": threadId])
            do {
                let (data, resp) = try await URLSession.shared.data(for: req)
                if let http = resp as? HTTPURLResponse {
                    if http.statusCode == 404 || http.statusCode == 405 {
                        stopAvailable = false
                        return
                    }
                    guard (200..<300).contains(http.statusCode) else {
                        showBanner(String(localized: "Couldn't stop. Please try again."))
                        return
                    }
                }
                let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
                if (obj?["stopped"] as? Bool) == false {

                    finishAll()
                    return
                }
                try? await Task.sleep(nanoseconds: 4_000_000_000)
                if live.active && runSerial == serial { finishAll(stopped: true) }
            } catch {
                showBanner(String(localized: "Couldn't stop. Please try again."))
            }
        }
    }


    func handle(_ event: [String: Any]) {
        guard let type = event["type"] as? String else { return }
        switch type {
        case "status_update", "pong", "monitor_connected":
            break
        case "agent_waiting":


            if !pendingResend.isEmpty || isBusy { agentStarting = true }
        case "agent_started":
            agentStarting = false
            if !pendingResend.isEmpty { Task { await flushPending() } }
        case "chat_thinking", "chat_content":


            let delta = (event["content"] as? String) ?? ""
            guard !delta.isEmpty, let t = activeTurn(for: event) else { break }
            t.appendNarration(delta, thinking: type == "chat_thinking")
        case "chat_reply_delta":
            guard let t = activeTurn(for: event) else { break }
            if (event["reset"] as? Bool) == true {

                t.holdBack(reason: (event["reason"] as? String) ?? "rejected",
                           detail: event["detail"] as? String)
            } else {
                let text = (event["text"] as? String) ?? ""
                guard !text.isEmpty else { break }
                t.rechecking = false
                if t.streamText != text { t.streamText = text }
            }
            syncLive()
        case "chat_verification":
            if let t = turn(for: event, create: false),
               let status = event["status"] as? String, status != "verified",
               let detail = event["detail"] as? String, !detail.isEmpty {
                t.lastVerificationDetail = detail
            }
        case "chat_tool_call":
            handleToolCall(event, forceNested: false)
        case "chat_tool_result":
            handleToolResult(event, forceNested: false)
        case "chat_reply":
            attachReply(text: (event["content"] as? String) ?? "",
                        hash: event["message_hash"] as? String,
                        reportId: event["report_id"] as? Int,
                        imagePath: nil, event: event)
        case "chat_image":
            attachReply(text: (event["caption"] as? String) ?? "",
                        hash: event["message_hash"] as? String,
                        reportId: nil,
                        imagePath: event["url"] as? String, event: event)
        case "chat_run_done":
            sawRunDone = true
            if let t = turn(for: event, create: false) { finish(t, byServer: true) }
        case "chat_stopped":
            if let t = turn(for: event, create: false) {
                finish(t, stopped: true, byServer: true)
            } else {
                finishAll(stopped: true)
            }
        case "chat_cleared":
            clearLocal()
        default:


            if turns.contains(where: { $0.phase == .live }) {
                if type.hasSuffix("_tool_call") {
                    handleToolCall(event, forceNested: true)
                } else if type.hasSuffix("_tool_result") {
                    handleToolResult(event, forceNested: true)
                }
            }
        }
    }

    private func handleToolCall(_ event: [String: Any], forceNested: Bool) {
        let tool = (event["tool"] as? String) ?? ""
        if tool == "finish_chat" {

            if let t = turn(for: event, create: false) { finish(t, byServer: true) }
            return
        }
        guard let t = activeTurn(for: event) else { return }
        guard !tool.isEmpty else { return }

        if tool == "reply_user" {
            t.writingReply = true
            syncLive()
            return
        }
        var parent = event["parent"] as? String
        if parent == nil, forceNested || event["source"] != nil {
            parent = (event["source"] as? String) ?? "analyze"
        }
        let args = event["arguments"] as? [String: Any]
        var detail = Self.detail(tool: tool, arguments: args)
        if detail == nil, let summary = event["summary"] as? String {
            let c = Self.clip(summary.replacingOccurrences(of: "\n", with: " "), 60)
            if !c.isEmpty { detail = c }
        }
        t.addTool(callId: event["call_id"] as? String, tool: tool, parent: parent, detail: detail)
        syncLive()
    }

    private func handleToolResult(_ event: [String: Any], forceNested: Bool) {
        let tool = (event["tool"] as? String) ?? ""
        guard !tool.isEmpty, tool != "finish_chat",
              let t = turn(for: event, create: false) else { return }
        touch()
        let ok = (event["success"] as? Bool) ?? false
        if tool == "reply_user" {
            t.writingReply = false
            if !ok {

                let result = event["result"] as? [String: Any]
                let reason = (result?["blocked"] as? String) == "verification" ? "verification" : "rejected"
                t.holdBack(reason: reason, detail: result?["verification_detail"] as? String)
            }
            syncLive()
            return
        }
        let nested = forceNested || event["source"] != nil || event["parent"] != nil
        t.completeTool(callId: event["call_id"] as? String, tool: tool, nested: nested, success: ok,
                       preview: Self.preview(of: event["result_preview"] ?? event["result"]))
        syncLive()
    }


    private func attachReply(text: String, hash: String?, reportId: Int?,
                             imagePath: String?, event: [String: Any]) {


        let t: TurnState?
        if Self.runId(of: event) != nil {
            t = turn(for: event, create: true)
        } else {
            t = turns.first(where: { $0.phase == .live && $0.runId == nil })
        }
        if let t, !t.closedByServer { t.reopen() }
        touch()


        var dup: Int?
        if let imagePath {
            dup = messages.lastIndex(where: { $0.imagePath == imagePath })
        } else if let last = messages.lastIndex(where: { $0.role == .assistant }) {
            let m = messages[last]
            if let hash, m.messageHash == hash, m.text == text { dup = last }
            else if hash == nil, m.text == text, Date().timeIntervalSince(m.timestamp) < 5,
                    messages.last?.role == .assistant { dup = last }
        }
        if dup == nil, let hash {
            dup = messages.lastIndex(where: {
                $0.role == .assistant && $0.messageHash == hash && $0.text == text
            })
        }
        if let i = dup {
            if let t, messages[i].turnId == nil { messages[i].turnId = t.id }
        } else {
            messages.append(ChatMessage(role: .assistant, text: text, imagePath: imagePath,
                                        messageHash: hash, reportId: reportId, turnId: t?.id))
        }
        if let t {
            t.writingReply = false
            t.rechecking = false
            if !t.streamText.isEmpty { t.streamText = "" }
            syncLive()
            armSettle(t)
        }
    }


    private static func tail(_ s: String, limit: Int = 200) -> String {
        let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: "\n", with: " ")
        guard t.count > limit else { return t }
        return "…" + String(t.suffix(limit))
    }

    private static func clip(_ s: String, _ limit: Int) -> String {
        let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
        return t.count > limit ? String(t.prefix(limit)) + "…" : t
    }


    private static func detail(tool: String, arguments: [String: Any]?) -> String? {
        guard let args = arguments else { return nil }
        let key: String
        switch tool {
        case "analyze", "manage": key = "goal"
        case "sql": key = "query"
        case "read_skill": key = "name"
        case "create_page": key = "title"
        case "update_md": key = "file"
        default: return nil
        }
        guard let v = args[key] as? String else { return nil }
        let c = clip(v.replacingOccurrences(of: "\n", with: " "), 60)
        return c.isEmpty ? nil : c
    }


    private static func preview(of result: Any?) -> String? {
        if let s = result as? String {
            let c = clip(s, 280)
            return c.isEmpty ? nil : c
        }
        guard let dict = result as? [String: Any] else { return nil }
        for key in ["error", "findings", "result", "message", "summary", "output", "stdout"] {
            if let v = dict[key] as? String {
                let c = clip(v, 280)
                if !c.isEmpty { return c }
            }
        }
        if let data = try? JSONSerialization.data(withJSONObject: dict),
           let json = String(data: data, encoding: .utf8) {
            return clip(json, 160)
        }
        return nil
    }
}
