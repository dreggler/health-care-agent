import Combine
import SwiftUI


enum AppRoute: Hashable {
    case chat

    case thread(id: String)


    case report(id: Int)
}

@MainActor
final class AppRouter: ObservableObject {
    static let shared = AppRouter()


    @Published var pendingRoute: AppRoute?


    @Published var pendingReportId: Int?

    private init() {}


    func requestChat(threadId: String? = nil) {
        if let threadId, !threadId.isEmpty, threadId != chatMainThreadId {
            pendingRoute = .thread(id: threadId)
        } else {
            pendingRoute = .chat
        }
    }

    func requestReport(_ id: Int) {
        pendingReportId = id
        pendingRoute = .report(id: id)
    }
}
