import SwiftUI
import PhotosUI


struct ChatView: View {
    @ObservedObject var vm: ChatViewModel


    @ObservedObject var connection: ChatConnectionState
    let title: String


    var onShowThreads: (() -> Void)?
    var onNewThread: (() -> Void)?

    var hasUnreadElsewhere = false
    @FocusState private var inputFocused: Bool


    @State private var nearBottom = true


    @State private var userScrolling = false

    private static let bottomID = "chat-bottom"

    var body: some View {
        VStack(spacing: 0) {
            if vm.agentStarting {
                Label("Waking up CareAgent…", systemImage: "moon.zzz")
                    .font(.caption)
                    .foregroundColor(.secondary)
                    .padding(.vertical, 6)
            }
            if let error = vm.errorBanner {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundColor(.red)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .transition(.opacity)
            }

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 14) {
                        ForEach(vm.rows) { row in
                            switch row.item {
                            case .message(let message):
                                ChatBubble(
                                    message: message,
                                    showAvatar: row.showAvatar,
                                    loadEvidence: { msg in await vm.evidence(for: msg) },
                                    onRetry: { id in vm.retry(messageId: id) }
                                )
                                .equatable()
                            case .turn(let turn):


                                TurnView(turn: turn,
                                         loadEvidence: { msg in await vm.evidence(for: msg) })
                            }
                        }
                        Color.clear.frame(height: 1).id(Self.bottomID)
                    }
                    .padding(.horizontal, 14)
                    .padding(.vertical, 14)
                }

                .defaultScrollAnchor(.bottom)
                .scrollDismissesKeyboard(.interactively)


                .simultaneousGesture(TapGesture().onEnded { inputFocused = false })
                .onScrollGeometryChange(for: ScrollMetrics.self) { geo in
                    ScrollMetrics(visibleMaxY: geo.visibleRect.maxY,
                                  content: geo.contentSize.height,
                                  container: geo.containerSize.height)
                } action: { old, new in


                    let resized = new.content > old.content + 0.5
                        || abs(new.container - old.container) > 0.5
                    if resized && nearBottom && !userScrolling {
                        scrollToBottom(proxy, animated: false)
                    } else if nearBottom != new.isNearBottom {


                        nearBottom = new.isNearBottom
                    }
                }
                .onScrollPhaseChange { _, phase in
                    let touching = phase == .tracking || phase == .interacting
                        || phase == .decelerating
                    if userScrolling != touching { userScrolling = touching }
                }


                .safeAreaInset(edge: .bottom, spacing: 0) {
                    ChatComposer(focus: $inputFocused,
                                 isBusy: vm.isBusy,
                                 showStop: vm.isBusy && vm.stopAvailable,
                                 onStop: { vm.stop() }) { text, image in
                        vm.send(text: text, image: image)
                        scrollToBottom(proxy, animated: true)
                    }
                }


                .background(HimeColor.paper)
                .onChange(of: vm.didLoadHistory) { _, loaded in
                    guard loaded else { return }
                    jumpToBottom(proxy)
                }
                .onChange(of: vm.rows.last?.id) { _, _ in
                    guard let last = vm.rows.last else { return }


                    var isUserRow = false
                    if case .message(let m) = last.item, m.role == .user { isUserRow = true }
                    if isUserRow || (nearBottom && !userScrolling) {
                        scrollToBottom(proxy, animated: vm.didLoadHistory)
                    }
                }
                .onChange(of: vm.isBusy) { _, _ in
                    if nearBottom && !userScrolling { scrollToBottom(proxy, animated: true) }
                }
                .onChange(of: inputFocused) { _, focused in
                    guard focused else { return }

                    Task {
                        try? await Task.sleep(nanoseconds: 300_000_000)
                        scrollToBottom(proxy, animated: false)
                    }
                }
                .onReceive(NotificationCenter.default.publisher(
                    for: UIResponder.keyboardDidShowNotification)) { _ in
                    if inputFocused { scrollToBottom(proxy, animated: false) }
                }
                .onAppear { if vm.didLoadHistory { jumpToBottom(proxy) } }
                .overlay {
                    if vm.rows.isEmpty && !vm.isBusy {
                        ChatEmptyState { text in
                            vm.send(text: text, image: nil)
                            scrollToBottom(proxy, animated: true)
                        }
                    }
                }
            }
        }
        .animation(.easeInOut(duration: 0.2), value: vm.errorBanner)
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .principal) {
                HStack(spacing: 8) {

                    ChatNavAvatar(live: vm.live,
                                  agentStarting: vm.agentStarting,
                                  reconnecting: connection.showReconnecting,
                                  hasError: vm.errorBanner != nil)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(title).font(.headline).lineLimit(1)
                        if connection.showReconnecting {
                            HStack(spacing: 4) {
                                PulsingDot()
                                Text("Reconnecting…")
                                    .font(.caption2)
                                    .foregroundColor(.secondary)
                            }
                            .transition(.opacity)
                        }
                    }
                    .animation(.easeInOut(duration: 0.2), value: connection.showReconnecting)
                }
            }
            if let onShowThreads {
                ToolbarItem(placement: .navigationBarLeading) {
                    Button(action: onShowThreads) {
                        Image(systemName: "list.bullet")
                            .overlay(alignment: .topTrailing) {
                                if hasUnreadElsewhere {
                                    Circle().fill(HimeColor.accent)
                                        .frame(width: 8, height: 8)
                                        .offset(x: 4, y: -3)
                                }
                            }
                    }
                    .accessibilityLabel(Text("Chats"))
                }
            }
            if let onNewThread {
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button(action: onNewThread) {
                        Image(systemName: "square.and.pencil")
                    }
                    .accessibilityLabel(Text("New chat"))
                }
            }
            ToolbarItem(placement: .navigationBarTrailing) {
                Menu {
                    Button(role: .destructive) { vm.clearConversation() } label: {
                        Label("Clear conversation", systemImage: "trash")
                    }
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
            }
        }
        .onAppear {
            ChatHub.shared.screenAppeared()
            ChatHub.shared.threadOpened(vm.threadId)
            vm.onAppear()
        }
        .onDisappear {
            vm.onDisappear()
            ChatHub.shared.threadClosed(vm.threadId)
            ChatHub.shared.screenDisappeared()
        }
    }

    private func scrollToBottom(_ proxy: ScrollViewProxy, animated: Bool) {
        if animated {
            withAnimation(.easeOut(duration: 0.2)) {
                proxy.scrollTo(Self.bottomID, anchor: .bottom)
            }
        } else {
            proxy.scrollTo(Self.bottomID, anchor: .bottom)
        }
    }


    private func jumpToBottom(_ proxy: ScrollViewProxy) {
        nearBottom = true
        Task {
            scrollToBottom(proxy, animated: false)
            try? await Task.sleep(nanoseconds: 250_000_000)

            if !userScrolling { scrollToBottom(proxy, animated: false) }
        }
    }
}


private struct ScrollMetrics: Equatable {
    var visibleMaxY: CGFloat
    var content: CGFloat
    var container: CGFloat

    var isNearBottom: Bool { content - visibleMaxY < 60 }
}


private struct ChatComposer: View {
    var focus: FocusState<Bool>.Binding

    let isBusy: Bool

    let showStop: Bool
    let onStop: () -> Void
    let onSend: (String, Data?) -> Void

    @State private var text = ""
    @State private var pendingImage: Data?
    @State private var pendingPreview: UIImage?
    @State private var photoItem: PhotosPickerItem?

    var body: some View {
        VStack(spacing: 6) {
            if let ui = pendingPreview {
                HStack {
                    Image(uiImage: ui)
                        .resizable().scaledToFill()
                        .frame(width: 44, height: 44)
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    Button {
                        pendingImage = nil
                        pendingPreview = nil
                    } label: {
                        Image(systemName: "xmark.circle.fill").foregroundColor(.secondary)
                    }
                    Spacer()
                }
                .padding(.horizontal, 12)
            }
            HStack(spacing: 8) {
                PhotosPicker(selection: $photoItem, matching: .images) {
                    Image(systemName: "photo.on.rectangle")
                        .font(.system(size: 22))
                        .foregroundColor(.secondary)
                }
                .onChange(of: photoItem) { _, item in
                    guard let item else { return }
                    Task {
                        if let data = try? await item.loadTransferable(type: Data.self) {


                            let scaled = await Task.detached(priority: .userInitiated) {
                                downscaleJPEG(data)
                            }.value
                            pendingImage = scaled
                            pendingPreview = scaled.flatMap { UIImage(data: $0) }
                        }
                        photoItem = nil
                    }
                }

                TextField(isBusy ? String(localized: "Working on it… you can add something anytime")
                                 : String(localized: "Message CareAgent"),
                          text: $text, axis: .vertical)
                    .textFieldStyle(.plain)
                    .focused(focus)
                    .lineLimit(1...5)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .background(Color(.systemGray6))
                    .clipShape(RoundedRectangle(cornerRadius: HimeRadius.pill))

                if showStop {
                    Button(action: onStop) {
                        Image(systemName: "stop.fill")
                            .font(.system(size: 12, weight: .bold))
                            .foregroundColor(.white)
                            .frame(width: 28, height: 28)
                            .background(
                                RoundedRectangle(cornerRadius: 8, style: .continuous)
                                    .fill(HimeColor.ink2))
                    }
                    .accessibilityLabel(Text("Stop"))
                }

                Button(action: send) {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.system(size: 28))
                        .foregroundColor(canSend ? HimeColor.accent : .gray)
                }
                .disabled(!canSend)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
        }
        .background(.bar)
        .overlay(alignment: .top) {

            Rectangle()
                .fill(Color.primary.opacity(0.08))
                .frame(height: 0.5)
        }
    }

    private var canSend: Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || pendingImage != nil
    }

    private func send() {
        guard canSend else { return }
        onSend(text, pendingImage)
        text = ""
        pendingImage = nil
        pendingPreview = nil
    }
}


private struct ChatEmptyState: View {
    let onPick: (String) -> Void

    private var starters: [String] {
        [String(localized: "How did I sleep last night?"),
         String(localized: "Any unusual heart rate this week?"),
         String(localized: "Make me a weekly summary")]
    }

    var body: some View {
        VStack(spacing: 10) {
            HimeAvatar(size: 104, activity: .idle)
            Text("Hi, I'm CareAgent")
                .font(.title3.weight(.semibold))
                .foregroundColor(HimeColor.ink)
            Text("Ask me anything about your sleep, heart, activity, or recovery.")
                .font(.footnote)
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 40)
            VStack(spacing: 8) {
                ForEach(starters, id: \.self) { prompt in
                    Button { onPick(prompt) } label: {
                        Text(prompt)
                            .font(.subheadline)
                            .foregroundColor(HimeColor.ink)
                            .padding(.horizontal, 14)
                            .padding(.vertical, 9)
                            .frame(maxWidth: .infinity)
                            .background(
                                RoundedRectangle(cornerRadius: HimeRadius.row, style: .continuous)
                                    .fill(HimeColor.card)
                            )
                            .overlay(
                                RoundedRectangle(cornerRadius: HimeRadius.row, style: .continuous)
                                    .stroke(HimeColor.line, lineWidth: 0.5)
                            )
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 28)
            .padding(.top, 8)
        }
        .padding(.bottom, 40)
    }
}


private struct PulsingDot: View {
    @State private var on = false

    var body: some View {
        Circle()
            .fill(HimeColor.warn)
            .frame(width: 6, height: 6)
            .opacity(on ? 1 : 0.3)
            .animation(.easeInOut(duration: 0.8).repeatForever(autoreverses: true), value: on)
            .onAppear { on = true }
    }
}


private struct ChatBubble: View, Equatable {
    let message: ChatMessage


    let showAvatar: Bool

    var embedded = false
    let loadEvidence: (ChatMessage) async -> String?
    let onRetry: (String) -> Void

    @State private var evidence: String?
    @State private var showEvidence = false
    @State private var loadingEvidence = false

    static func == (lhs: ChatBubble, rhs: ChatBubble) -> Bool {
        lhs.message == rhs.message && lhs.showAvatar == rhs.showAvatar
    }

    private var isUser: Bool { message.role == .user }


    private var bubbleShape: UnevenRoundedRectangle { chatBubbleShape(isUser: isUser) }

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            if isUser {
                Spacer(minLength: 44)
            } else if embedded {
                EmptyView()
            } else if showAvatar {
                HimeAvatar(size: 36)
            } else {
                Color.clear.frame(width: 36, height: 1)
            }
            VStack(alignment: isUser ? .trailing : .leading, spacing: 6) {
                if let data = message.localImage {
                    LocalChatImage(key: "local:\(message.id)", data: data)
                }
                if let path = message.imagePath {
                    AuthedAsyncImage(path: path)
                }
                if !message.text.isEmpty {
                    bubbleText
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                        .background(


                            bubbleShape
                                .fill(isUser ? HimeColor.userBubble : HimeColor.assistantBubble)
                                .shadow(color: .black.opacity(isUser ? 0.08 : 0.05),
                                        radius: 2, x: 0, y: 1)
                        )
                        .overlay {
                            if !isUser {
                                bubbleShape.stroke(Color.primary.opacity(0.05), lineWidth: 0.5)
                            }
                        }
                        .opacity(isUser && message.delivery == .sending ? 0.75 : 1)
                        .textSelection(.enabled)
                }
                if isUser && message.delivery == .failed {
                    retryControl
                }
                if let reportId = message.reportId, !isUser {
                    reportLink(reportId)
                }
                if message.messageHash != nil && !isUser && !message.isStreaming {
                    evidenceControl
                }
                if showEvidence, let evidence {
                    MarkdownView(text: evidence, foreground: Color(.secondaryLabel))
                        .font(.footnote)
                        .padding(10)
                        .background(Color(.systemGray6))
                        .clipShape(RoundedRectangle(cornerRadius: 12))
                }
            }
            if !isUser { Spacer(minLength: embedded ? 0 : 40) }
        }
    }


    @ViewBuilder
    private var bubbleText: some View {
        if isUser {
            Text(message.text)
                .font(.body)
                .foregroundColor(HimeColor.userBubbleText)
        } else if message.isStreaming {
            VStack(alignment: .leading, spacing: 4) {
                MarkdownView(text: message.text, foreground: .primary)
                PulsingCaret()
            }
        } else {
            MarkdownView(text: message.text, foreground: .primary)
        }
    }


    private var retryControl: some View {
        Button {
            onRetry(message.id)
        } label: {
            HStack(spacing: 4) {
                Image(systemName: "exclamationmark.circle.fill")
                Text("Not sent. Tap to retry")
                Image(systemName: "arrow.clockwise")
                    .font(.system(size: 10, weight: .semibold))
            }
            .font(.caption)
            .foregroundColor(.red)
        }
        .buttonStyle(.plain)
    }


    private func reportLink(_ reportId: Int) -> some View {
        Button {
            AppRouter.shared.requestReport(reportId)
        } label: {
            HStack(spacing: 4) {
                Image(systemName: "doc.text.magnifyingglass")
                Text("View full report")
                Image(systemName: "chevron.right")
                    .font(.system(size: 9, weight: .semibold))
            }
            .font(.caption.weight(.medium))
            .foregroundColor(HimeColor.accent)
        }
        .buttonStyle(.plain)
    }

    private var evidenceControl: some View {
        Button {
            if showEvidence { showEvidence = false; return }
            if let evidence, !evidence.isEmpty { showEvidence = true; return }
            loadingEvidence = true
            Task {
                let result = await loadEvidence(message)
                evidence = result ?? String(localized: "No evidence recorded.")
                showEvidence = true
                loadingEvidence = false
            }
        } label: {
            HStack(spacing: 4) {
                if loadingEvidence {
                    ProgressView().scaleEffect(0.7)
                } else {
                    Image(systemName: "chart.bar.doc.horizontal")
                }
                Text(showEvidence ? "Hide evidence" : "Show evidence")
            }
            .font(.caption)
            .foregroundColor(.secondary)
        }
    }
}


private struct TurnView: View {
    @ObservedObject var turn: TurnState
    let loadEvidence: (ChatMessage) async -> String?


    private var bodies: [ChatMessage] {
        var items = turn.replies
        if !turn.streamText.isEmpty {
            items.append(ChatMessage(id: "stream-" + turn.id, role: .assistant,
                                     text: turn.streamText, isStreaming: true))
        }
        return items
    }

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            if turn.phase == .live {
                HimeAvatar(size: 36, activity: turn.avatarActivity)
            } else {
                HimeAvatar(size: 36, state: turn.phase == .stopped ? "tired" : "relaxed")
            }
            VStack(alignment: .leading, spacing: 8) {
                TurnActivityCard(turn: turn)
                ForEach(Array(bodies.enumerated()), id: \.offset) { _, message in
                    ChatBubble(message: message,
                               showAvatar: false,
                               embedded: true,
                               loadEvidence: loadEvidence,
                               onRetry: { _ in })
                        .equatable()
                        .transition(.opacity)
                }
                if let error = turn.errorText {
                    Label(error, systemImage: "exclamationmark.triangle.fill")
                        .font(.caption)
                        .foregroundColor(HimeColor.bad)
                }
            }
            Spacer(minLength: 40)
        }
    }
}


private struct LocalChatImage: View {
    let key: String
    let data: Data
    @State private var image: UIImage?

    init(key: String, data: Data) {
        self.key = key
        self.data = data
        _image = State(initialValue: ChatImageCache.image(for: key))
    }

    var body: some View {
        Group {
            if let image {
                ChatImageThumbnail(image: image, maxWidth: 220, maxHeight: 220)
            } else {
                RoundedRectangle(cornerRadius: HimeRadius.card)
                    .fill(Color(.systemGray6))
                    .frame(width: 120, height: 120)
            }
        }
        .task(id: key) {
            guard image == nil else { return }
            if let ui = await ChatImageCache.decode(data) {
                ChatImageCache.store(ui, for: key)
                image = ui
            }
        }
    }
}


private struct AuthedAsyncImage: View {
    let path: String
    @State private var image: UIImage?
    @State private var failed = false

    init(path: String) {
        self.path = path
        _image = State(initialValue: ChatImageCache.image(for: "path:\(path)"))
    }

    var body: some View {
        Group {
            if let image {
                ChatImageThumbnail(image: image, maxWidth: 240, maxHeight: 240)
            } else {
                RoundedRectangle(cornerRadius: HimeRadius.card)
                    .fill(Color(.systemGray6))
                    .frame(width: 200, height: 150)
                    .overlay {
                        if failed {
                            VStack(spacing: 6) {
                                Image(systemName: "photo.badge.exclamationmark")
                                    .font(.system(size: 22))
                                Text("Image unavailable")
                                    .font(.caption)
                            }
                            .foregroundColor(.secondary)
                        } else {
                            ProgressView()
                        }
                    }
            }
        }
        .task(id: path) { await load() }
    }

    private func load() async {
        guard image == nil,
              let url = URL(string: "\(ServerConfig.load().apiBaseURL)\(path)") else { return }
        failed = false


        guard let ui = await ChatImageCache.fetch(url, authed: true) else {
            failed = true
            return
        }
        ChatImageCache.store(ui, for: "path:\(path)")
        image = ui
    }
}


private nonisolated func downscaleJPEG(_ data: Data, maxDimension: CGFloat = 1280, quality: CGFloat = 0.7) -> Data? {
    guard let ui = UIImage(data: data) else { return nil }
    let scale = min(1, maxDimension / max(ui.size.width, ui.size.height))
    if scale >= 1 { return ui.jpegData(compressionQuality: quality) }
    let newSize = CGSize(width: ui.size.width * scale, height: ui.size.height * scale)


    let format = UIGraphicsImageRendererFormat()
    format.scale = 1
    let renderer = UIGraphicsImageRenderer(size: newSize, format: format)
    let resized = renderer.image { _ in ui.draw(in: CGRect(origin: .zero, size: newSize)) }
    return resized.jpegData(compressionQuality: quality)
}
