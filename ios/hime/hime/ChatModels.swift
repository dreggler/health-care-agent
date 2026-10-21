import Combine
import Foundation
import SwiftUI


struct ChatMessage: Identifiable, Equatable {
    enum Role: String { case user, assistant }


    enum Delivery: Equatable { case sent, sending, failed }

    let id: String
    var role: Role
    var text: String

    var imagePath: String?

    var localImage: Data?

    var messageHash: String?


    var reportId: Int?

    var isStreaming: Bool


    var serverId: Int?


    var turnId: String?


    var clientMsgId: String?
    var delivery: Delivery
    let timestamp: Date

    init(id: String = UUID().uuidString,
         role: Role,
         text: String = "",
         imagePath: String? = nil,
         localImage: Data? = nil,
         messageHash: String? = nil,
         reportId: Int? = nil,
         isStreaming: Bool = false,
         serverId: Int? = nil,
         turnId: String? = nil,
         clientMsgId: String? = nil,
         delivery: Delivery = .sent,
         timestamp: Date = Date()) {
        self.id = id
        self.role = role
        self.text = text
        self.imagePath = imagePath
        self.localImage = localImage
        self.messageHash = messageHash
        self.reportId = reportId
        self.isStreaming = isStreaming
        self.serverId = serverId
        self.turnId = turnId
        self.clientMsgId = clientMsgId
        self.delivery = delivery
        self.timestamp = timestamp
    }


    static func == (lhs: ChatMessage, rhs: ChatMessage) -> Bool {
        lhs.id == rhs.id
            && lhs.role == rhs.role
            && lhs.text == rhs.text
            && lhs.imagePath == rhs.imagePath
            && lhs.localImage?.count == rhs.localImage?.count
            && lhs.messageHash == rhs.messageHash
            && lhs.reportId == rhs.reportId
            && lhs.isStreaming == rhs.isStreaming
            && lhs.serverId == rhs.serverId
            && lhs.turnId == rhs.turnId
            && lhs.delivery == rhs.delivery
    }
}


struct RunStep: Identifiable, Equatable {
    enum Kind: Equatable { case tool, narration, notice }
    enum Status: Equatable { case running, ok, failed, stopped }

    let id: String
    let kind: Kind


    let tool: String


    let parent: String?

    var text: String

    var detail: String?
    var status: Status

    var preview: String?

    var callId: String?

    var nested: Bool { parent != nil }

    init(kind: Kind, tool: String = "", parent: String? = nil, text: String = "",
         detail: String? = nil, status: Status = .ok, preview: String? = nil,
         callId: String? = nil) {
        self.id = UUID().uuidString
        self.kind = kind
        self.tool = tool
        self.parent = parent
        self.text = text
        self.detail = detail
        self.status = status
        self.preview = preview
        self.callId = callId
    }
}


@MainActor
final class TurnState: ObservableObject {
    enum Phase: Equatable { case live, done, stopped }

    let id = UUID().uuidString

    let anchorId: String?
    let startedAt = Date()

    var runId: String?


    var closedByServer = false

    var lastVerificationDetail: String?

    @Published private(set) var phase: Phase = .live
    @Published private(set) var steps: [RunStep] = []
    @Published private(set) var endedAt: Date?

    @Published var streamText = ""

    @Published var replies: [ChatMessage] = []

    @Published var writingReply = false

    @Published var rechecking = false
    @Published var errorText: String?

    private var openNarrationId: String?
    private var narrationThinking = false
    private var noticeAt: Date?
    private static let narrationCap = 4000

    init(anchorId: String?) {
        self.anchorId = anchorId
    }


    var runningTool: String? {
        steps.last(where: { $0.kind == .tool && $0.status == .running })?.tool
    }

    var stepCount: Int { steps.filter { $0.kind == .tool }.count }


    var hasContent: Bool { !steps.isEmpty || !streamText.isEmpty || !replies.isEmpty }

    var avatarActivity: AvatarActivity {
        guard phase == .live else { return .idle }
        if writingReply || !streamText.isEmpty { return .replying }
        if runningTool != nil { return .working }
        return .thinking
    }


    var statusText: String {
        let n = stepCount
        var suffix = ""
        if n == 1 {
            suffix = " · " + String(localized: "1 step")
        } else if n > 1 {
            suffix = " · " + String(localized: "\(n) steps")
        }
        switch phase {
        case .live:
            let base: String
            if writingReply || !streamText.isEmpty {
                base = String(localized: "Writing the reply…")
            } else if let t = runningTool {
                base = Self.liveVerb(t)
            } else if rechecking {
                base = String(localized: "Re-checking facts…")
            } else {
                base = String(localized: "Thinking…")
            }
            return base + suffix
        case .done:
            return String(localized: "Thought for \(durationText)") + suffix
        case .stopped:
            return String(localized: "Stopped") + suffix
        }
    }

    private var durationText: String {
        let s = max(1, Int((endedAt ?? Date()).timeIntervalSince(startedAt).rounded()))
        return s < 60 ? "\(s)s" : "\(s / 60)m \(s % 60)s"
    }

    private static func liveVerb(_ tool: String) -> String {
        switch tool {
        case "analyze", "sql", "code", "read_skill": return String(localized: "Analyzing data")
        case "manage", "update_md", "create_page": return String(localized: "Making an update")
        default: return String(localized: "Working")
        }
    }


    func appendNarration(_ delta: String, thinking: Bool) {
        if let id = openNarrationId, let i = steps.lastIndex(where: { $0.id == id }) {
            var t = steps[i].text
            if thinking != narrationThinking, !t.isEmpty { t += "\n\n" }
            t += delta
            if t.count > Self.narrationCap { t = "…" + String(t.suffix(Self.narrationCap)) }
            steps[i].text = t
        } else {
            let step = RunStep(kind: .narration, text: delta)
            openNarrationId = step.id
            steps.append(step)
        }
        narrationThinking = thinking
    }

    func addTool(callId: String?, tool: String, parent: String?, detail: String?) {
        openNarrationId = nil
        rechecking = false
        steps.append(RunStep(kind: .tool, tool: tool, parent: parent, detail: detail,
                             status: .running, callId: callId))
    }

    func completeTool(callId: String?, tool: String, nested: Bool, success: Bool, preview: String?) {
        var idx: Int?
        if let callId {
            idx = steps.lastIndex(where: { $0.kind == .tool && $0.callId == callId })
        }
        if idx == nil {
            idx = steps.lastIndex(where: {
                $0.kind == .tool && $0.status == .running && $0.tool == tool && $0.nested == nested
            })
        }
        guard let i = idx else { return }
        steps[i].status = success ? .ok : .failed
        steps[i].preview = preview
    }


    func holdBack(reason: String, detail: String?) {
        let draft = streamText.trimmingCharacters(in: .whitespacesAndNewlines)
        let now = Date()
        let reasonDetail = (detail?.isEmpty == false) ? detail : lastVerificationDetail
        let verification = reason == "verification"
        withAnimation(.easeOut(duration: 0.25)) {
            if let at = noticeAt, now.timeIntervalSince(at) < 2,
               let i = steps.lastIndex(where: { $0.kind == .notice }) {
                if verification {
                    steps[i].text = Self.noticeText(reason)
                    if let reasonDetail, !reasonDetail.isEmpty { steps[i].detail = reasonDetail }
                }
                if steps[i].preview == nil, !draft.isEmpty { steps[i].preview = draft }
            } else if !draft.isEmpty {
                openNarrationId = nil
                steps.append(RunStep(kind: .notice, text: Self.noticeText(reason),
                                     detail: verification ? reasonDetail : nil,
                                     status: .failed, preview: draft))
                noticeAt = now
            }
            if !streamText.isEmpty { streamText = "" }
            if verification { rechecking = true }
        }
    }

    private static func noticeText(_ reason: String) -> String {
        switch reason {
        case "verification": return String(localized: "Draft held back by fact check — re-verifying")
        case "retry": return String(localized: "Draft discarded — retrying")
        default: return String(localized: "Draft set aside — rewriting")
        }
    }


    func reopen() {
        guard phase == .done else { return }
        phase = .live
        endedAt = nil
    }


    func settle(stopped: Bool = false) {
        guard phase == .live else { return }
        withAnimation(.easeInOut(duration: 0.2)) {
            for i in steps.indices where steps[i].status == .running {
                steps[i].status = stopped ? .stopped : .ok
            }
            openNarrationId = nil
            writingReply = false
            rechecking = false
            if !streamText.isEmpty { streamText = "" }
            endedAt = Date()
            phase = stopped ? .stopped : .done
        }
    }
}


struct ChatRow: Identifiable, Equatable {
    enum Item: Equatable {
        case message(ChatMessage)
        case turn(TurnState)

        static func == (lhs: Item, rhs: Item) -> Bool {
            switch (lhs, rhs) {
            case let (.message(a), .message(b)): return a == b
            case let (.turn(a), .turn(b)): return a === b
            default: return false
            }
        }
    }

    let item: Item

    let showAvatar: Bool

    var id: String {
        switch item {
        case .message(let m): return m.id
        case .turn(let t): return "turn-" + t.id
        }
    }


    var isAssistantSide: Bool {
        switch item {
        case .message(let m): return m.role == .assistant
        case .turn: return true
        }
    }
}


@MainActor
final class LiveState: ObservableObject {
    @Published var active = false

    @Published var tool: String?

    @Published var replying = false

    func reset() {
        if active { active = false }
        if tool != nil { tool = nil }
        if replying { replying = false }
    }
}


struct ChatHistoryRow: Decodable {
    let id: Int?
    let created_at: String?
    let role: String
    let content: String
    let message_hash: String?
    let client_msg_id: String?
    let report_id: Int?
    let image_id: String?


    var date: Date? {
        guard let created_at else { return nil }
        return Self.utcFormatter.date(from: String(created_at.prefix(19)))
    }

    private static let utcFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return f
    }()
}

struct ChatHistoryResponse: Decodable {
    let success: Bool
    let messages: [ChatHistoryRow]
}


struct EvidenceResponse: Decodable {
    let success: Bool
    let found: Bool
    let formatted: String
}


let chatMainThreadId = "main"


struct ChatThreadPreview: Equatable {
    var role: String
    var content: String
    var date: Date?
}


struct ChatThread: Identifiable, Equatable {
    let id: String

    var title: String
    var createdAt: Date?
    var updatedAt: Date?
    var pinned: Bool
    var archived: Bool
    var last: ChatThreadPreview?

    var isMain: Bool { id == chatMainThreadId }


    static let main = ChatThread(id: chatMainThreadId, title: "", createdAt: nil, updatedAt: nil,
                                 pinned: true, archived: false, last: nil)


    static func from(_ any: Any?) -> ChatThread? {
        guard let d = any as? [String: Any], let id = d["id"] as? String, !id.isEmpty else { return nil }
        var preview: ChatThreadPreview?
        if let lm = d["last_message"] as? [String: Any] {
            preview = ChatThreadPreview(role: (lm["role"] as? String) ?? "",
                                        content: (lm["content"] as? String) ?? "",
                                        date: parseServerDate(lm["created_at"] as? String))
        }
        return ChatThread(id: id,
                          title: (d["title"] as? String) ?? "",
                          createdAt: parseServerDate(d["created_at"] as? String),
                          updatedAt: parseServerDate(d["updated_at"] as? String),
                          pinned: (d["pinned"] as? Bool) ?? (id == chatMainThreadId),
                          archived: (d["archived"] as? Bool) ?? false,
                          last: preview)
    }


    var activityDate: Date { last?.date ?? updatedAt ?? createdAt ?? .distantPast }
}


func parseServerDate(_ s: String?) -> Date? {
    guard let s, s.count >= 19 else { return nil }
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.timeZone = TimeZone(identifier: "UTC")
    f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
    return f.date(from: String(s.replacingOccurrences(of: " ", with: "T").prefix(19)))
}
