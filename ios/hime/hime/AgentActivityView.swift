import SwiftUI


enum ChatStepStyle {
    struct Info {
        let icon: String

        let running: LocalizedStringKey

        let done: LocalizedStringKey
    }

    static func info(_ tool: String) -> Info {
        switch tool {
        case "reply_user":
            return Info(icon: "paperplane.fill", running: "Writing a reply", done: "Wrote a reply")
        case "finish_chat":
            return Info(icon: "checkmark.bubble", running: "Wrapping up", done: "Wrapped up")
        case "analyze":
            return Info(icon: "chart.xyaxis.line", running: "Looking at your data", done: "Looked at your data")
        case "manage":
            return Info(icon: "slider.horizontal.3", running: "Making an update", done: "Made an update")
        case "sql":
            return Info(icon: "cylinder.split.1x2", running: "Checking your records", done: "Checked your records")
        case "code":
            return Info(icon: "function", running: "Running an analysis", done: "Ran an analysis")
        case "read_skill":
            return Info(icon: "book", running: "Reading a playbook", done: "Read a playbook")
        case "push_report":
            return Info(icon: "doc.text.fill", running: "Writing a report", done: "Wrote a report")
        case "update_md":
            return Info(icon: "note.text", running: "Saving a note", done: "Saved a note")
        case "create_page":
            return Info(icon: "rectangle.on.rectangle.angled", running: "Building a page", done: "Built a page")
        default:
            return Info(icon: "wrench.and.screwdriver.fill", running: "Working on it", done: "Worked on it")
        }
    }
}


func chatBubbleShape(isUser: Bool) -> UnevenRoundedRectangle {
    let r = HimeRadius.bubble
    let tail = HimeRadius.bubbleTail
    return UnevenRoundedRectangle(
        topLeadingRadius: r,
        bottomLeadingRadius: isUser ? r : tail,
        bottomTrailingRadius: isUser ? tail : r,
        topTrailingRadius: r,
        style: .continuous)
}


struct TurnActivityCard: View {
    @ObservedObject var turn: TurnState


    @State private var userExpanded: Bool?
    @State private var openSteps: Set<String> = []

    init(turn: TurnState) {
        _turn = ObservedObject(wrappedValue: turn)
    }

    private var canExpand: Bool { !turn.steps.isEmpty }

    private var expanded: Bool {
        guard canExpand else { return false }
        return userExpanded ?? (turn.phase == .live && turn.streamText.isEmpty && turn.replies.isEmpty)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            if expanded {
                Divider().padding(.horizontal, 12)
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(turn.steps) { step in
                        stepRow(step)
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
            }
        }
        .background(
            RoundedRectangle(cornerRadius: HimeRadius.card, style: .continuous)
                .fill(HimeColor.card)
        )
        .overlay(
            RoundedRectangle(cornerRadius: HimeRadius.card, style: .continuous)
                .stroke(HimeColor.line, lineWidth: 0.5)
        )
        .animation(.easeInOut(duration: 0.2), value: expanded)
    }

    private var header: some View {
        Button {
            guard canExpand else { return }
            userExpanded = !expanded
        } label: {
            HStack(spacing: 8) {
                switch turn.phase {
                case .live:
                    PulsingIcon(systemName: "sparkles")
                case .done:
                    Image(systemName: "checklist")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(HimeColor.ink2)
                case .stopped:
                    Image(systemName: "stop.circle")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(HimeColor.ink2)
                }
                Text(turn.statusText)
                    .font(.footnote.weight(.medium))
                    .foregroundColor(HimeColor.ink2)
                    .lineLimit(1)
                Spacer(minLength: 8)
                if canExpand {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundColor(HimeColor.ink2.opacity(0.7))
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder
    private func stepRow(_ step: RunStep) -> some View {
        switch step.kind {
        case .narration:
            let t = step.text.trimmingCharacters(in: .whitespacesAndNewlines)
            if !t.isEmpty {
                Text(t)
                    .font(.footnote)
                    .italic()
                    .foregroundColor(HimeColor.ink2.opacity(0.85))
                    .lineLimit(8)
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 3)
            }
        case .notice:
            noticeRow(step)
        case .tool:
            toolRow(step)
        }
    }

    private func noticeRow(_ step: RunStep) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 12))
                    .foregroundColor(HimeColor.warn)
                    .frame(width: 16)
                Text(step.text)
                    .font(.footnote)
                    .foregroundColor(HimeColor.ink)
                    .multilineTextAlignment(.leading)
                Spacer(minLength: 0)
            }
            if let detail = step.detail, !detail.isEmpty {
                Text(detail)
                    .font(.caption)
                    .foregroundColor(HimeColor.ink2)
                    .lineLimit(3)
                    .padding(.leading, 24)
            }
            if let draft = step.preview, !draft.isEmpty {
                Text(draft)
                    .font(.caption)
                    .italic()
                    .foregroundColor(HimeColor.ink2.opacity(0.75))
                    .lineLimit(4)
                    .padding(.leading, 24)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 3)
    }

    private func toolRow(_ step: RunStep) -> some View {
        let info = ChatStepStyle.info(step.tool)
        let isOpen = openSteps.contains(step.id)
        let tappable = !(step.preview ?? "").isEmpty
        return Button {
            guard tappable else { return }
            if isOpen { openSteps.remove(step.id) } else { openSteps.insert(step.id) }
        } label: {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    statusGlyph(step.status)
                    Image(systemName: info.icon)
                        .font(.system(size: 12))
                        .foregroundColor(HimeColor.ink2)
                        .frame(width: 16)
                    Text(step.status == .running ? info.running : info.done)
                        .font(.footnote)
                        .foregroundColor(HimeColor.ink)
                    if let detail = step.detail, !detail.isEmpty {
                        Text(detail)
                            .font(.caption)
                            .foregroundColor(HimeColor.ink2)
                            .lineLimit(1)
                            .truncationMode(.tail)
                    }
                    Spacer(minLength: 0)
                }
                if isOpen, let preview = step.preview {
                    Text(preview)
                        .font(.caption)
                        .foregroundColor(HimeColor.ink2)
                        .lineLimit(8)
                        .multilineTextAlignment(.leading)
                        .padding(.leading, 24)
                }
            }
            .padding(.leading, step.nested ? 16 : 0)
            .padding(.vertical, 3)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder
    private func statusGlyph(_ status: RunStep.Status) -> some View {
        switch status {
        case .running:
            ProgressView().scaleEffect(0.55).frame(width: 14, height: 14)
        case .ok:
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 13)).foregroundColor(HimeColor.ok).frame(width: 14, height: 14)
        case .failed:
            Image(systemName: "exclamationmark.circle.fill")
                .font(.system(size: 13)).foregroundColor(HimeColor.bad).frame(width: 14, height: 14)
        case .stopped:
            Image(systemName: "stop.circle.fill")
                .font(.system(size: 13)).foregroundColor(HimeColor.ink2).frame(width: 14, height: 14)
        }
    }
}


struct PulsingIcon: View {
    let systemName: String
    @State private var animate = false

    var body: some View {
        Image(systemName: systemName)
            .font(.system(size: 15, weight: .semibold))
            .foregroundStyle(HimeColor.accent)
            .scaleEffect(animate ? 1.12 : 0.9)
            .opacity(animate ? 1.0 : 0.65)
            .animation(.easeInOut(duration: 0.7).repeatForever(autoreverses: true), value: animate)
            .onAppear { animate = true }

            .id(systemName)
    }
}


struct PulsingCaret: View {
    @State private var on = false

    var body: some View {
        Capsule()
            .fill(HimeColor.accent)
            .frame(width: 3, height: 14)
            .opacity(on ? 1 : 0.2)
            .animation(.easeInOut(duration: 0.6).repeatForever(autoreverses: true), value: on)
            .onAppear { on = true }
    }
}
