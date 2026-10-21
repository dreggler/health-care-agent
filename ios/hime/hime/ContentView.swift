import SwiftUI
import WebKit
import Combine


struct PersonalisedPage: Identifiable, Decodable {
    var id: String { page_id }
    let page_id: String
    let display_name: String
    let description: String?
    let backend_route: String
    let frontend_asset: String
    let created_at: String?
}

private struct PersonalisedPagesResponse: Decodable {
    let success: Bool
    let pages: [PersonalisedPage]
}


@MainActor
class PersonalisedPagesStore: ObservableObject {
    @Published var pages: [PersonalisedPage] = []
    @Published var isLoading: Bool = false

    private var refreshTimer: Timer?

    func fetchPages() async {
        isLoading = true
        defer { isLoading = false }
        let base = ServerConfig.load().apiBaseURL
        guard let url = URL(string: "\(base)/api/personalised-pages/list") else { return }
        do {
            let (data, _) = try await URLSession.shared.data(for: APIClient.request(url))
            let decoded = try JSONDecoder().decode(PersonalisedPagesResponse.self, from: data)
            pages = decoded.pages
        } catch {

        }
    }


    func startPeriodicRefresh() {
        stopPeriodicRefresh()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in
                await self.fetchPages()
            }
        }
    }

    func stopPeriodicRefresh() {
        refreshTimer?.invalidate()
        refreshTimer = nil
    }
}


struct PersonalisedPageWebView: UIViewRepresentable {
    let urlString: String

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        let webView = WKWebView(frame: .zero, configuration: config)
        webView.scrollView.bounces = true

        if let url = URL(string: urlString) {
            webView.load(APIClient.request(url))
        }
        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {


        guard let url = URL(string: urlString), webView.url != url else { return }
        webView.load(APIClient.request(url))
    }
}


struct PersonalisedPageScreen: View {
    let page: PersonalisedPage


    private var frontendURL: String {
        let base = "\(ServerConfig.load().apiBaseURL)/api/personalised-pages/\(page.page_id)/"
        let token = ServerConfig.authToken
        guard !token.isEmpty else { return base }
        let encoded = token.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? token
        return "\(base)?token=\(encoded)"
    }

    var body: some View {
        PersonalisedPageWebView(urlString: frontendURL)
    }
}


struct ContentView: View {
    @EnvironmentObject var hk: HealthKitManager
    @EnvironmentObject var ws: WebSocketClient

    @StateObject private var pagesStore = PersonalisedPagesStore()
    @ObservedObject private var router = AppRouter.shared


    @State private var selectedTab = 2


    @State private var path = NavigationPath()


    private var totalTabs: Int { 3 + pagesStore.pages.count + 1 }


    private func clampSelectedTab(_ candidate: Int) {
        let maxTab = totalTabs - 1
        if candidate > maxTab { selectedTab = maxTab }
        else if candidate < 0 { selectedTab = 0 }
    }

    var body: some View {
        mainContent
    }

    @ViewBuilder
    private var mainContent: some View {
        NavigationStack(path: $path) {
            VStack(spacing: 0) {

                HStack(spacing: 6) {
                    ForEach(0..<totalTabs, id: \.self) { idx in
                        Circle()
                            .fill(selectedTab == idx ? HimeColor.accent : HimeColor.idle)
                            .frame(width: selectedTab == idx ? 9 : 7, height: selectedTab == idx ? 9 : 7)
                            .animation(.spring(response: 0.3), value: selectedTab)
                    }
                }
                .padding(.top, 8)


                .padding(.bottom, 4)
                .animation(.spring(), value: totalTabs)

                TabView(selection: $selectedTab) {

                    DashboardView()
                        .tag(0)


                    DataListView(hk: hk)
                        .tag(1)


                    CatMainView()
                        .tag(2)


                    ForEach(Array(pagesStore.pages.enumerated()), id: \.element.id) { idx, page in
                        PersonalisedPageScreen(page: page)
                            .tag(3 + idx)
                    }


                    PagePlaceholderView()
                        .tag(3 + pagesStore.pages.count)
                }
                .tabViewStyle(.page(indexDisplayMode: .never))
                .ignoresSafeArea(edges: .bottom)
                .onChange(of: selectedTab) { _, newValue in
                    clampSelectedTab(newValue)
                }


                .onChange(of: totalTabs) { _, _ in
                    clampSelectedTab(selectedTab)
                }
            }
            .background(HimeColor.paper.ignoresSafeArea())
            .navigationTitle(navTitle)
            .navigationBarTitleDisplayMode(.inline)
            .toolbarBackground(.hidden, for: .navigationBar)
            .animation(.easeInOut(duration: 0.2), value: selectedTab)
            .toolbar {
                ToolbarItem(placement: .navigationBarLeading) {
                    Link(destination: URL(string: "https://github.com/thinkwee/HiMe")!) {
                        GitHubIcon()
                            .frame(width: 20, height: 20)
                    }
                }
                ToolbarItem(placement: .navigationBarTrailing) {
                    NavigationLink(destination: SettingsView()) {
                        Image(systemName: "gearshape")
                            .foregroundColor(HimeColor.ink)
                    }
                }
            }
            .navigationDestination(for: AppRoute.self) { route in
                switch route {
                case .chat: ChatScreen()
                case .thread(let id): ChatScreen(threadId: id)


                case .report: DashboardView()
                }
            }
            .task {
                await pagesStore.fetchPages()
                pagesStore.startPeriodicRefresh()


                await ServerConfig.flushPendingSurvey()
            }
            .onDisappear {
                pagesStore.stopPeriodicRefresh()
            }
            .refreshable {
                await pagesStore.fetchPages()
            }
        }


        .onReceive(router.$pendingRoute) { route in
            guard let route else { return }
            switch route {
            case .chat, .thread:
                selectedTab = 2
                path = NavigationPath()
                path.append(route)
            case .report:


                path = NavigationPath()
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
                    var tx = Transaction()
                    tx.disablesAnimations = true
                    withTransaction(tx) { selectedTab = 0 }
                }
            }
            router.pendingRoute = nil
        }
    }

    private var navTitle: String {
        switch selectedTab {
        case 0: return String(localized: "Dashboard")
        case 1: return String(localized: "Collected Data")
        case 2: return String(localized: "Hime Home")
        default:
            let pageIdx = selectedTab - 3
            if pageIdx >= 0 && pageIdx < pagesStore.pages.count {
                return pagesStore.pages[pageIdx].display_name
            }
            return String(localized: "Pages")
        }
    }
}


struct PagePlaceholderView: View {
    var body: some View {
        VStack(spacing: 24) {
            Spacer()

            Image(systemName: "sparkles.rectangle.stack")
                .font(.system(size: 56))
                .foregroundStyle(
                    LinearGradient(
                        colors: [HimeColor.accent, HimeColor.rose],
                        startPoint: .topLeading, endPoint: .bottomTrailing
                    )
                )

            VStack(spacing: 10) {
                Text("Your Next Page Lives Here")
                    .font(.system(.title3, design: .rounded).weight(.bold))
                    .foregroundColor(HimeColor.ink)

                Text("Infinite possibilities, one conversation away.")
                    .font(.system(.subheadline, design: .rounded).weight(.medium))
                    .foregroundColor(HimeColor.ink2)
                    .multilineTextAlignment(.center)
            }

            Text("Ask your agent to create a personalised page\nand it will appear right here.")
                .font(.footnote)
                .foregroundColor(HimeColor.ink2)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 40)

            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}


struct FeatureGroup: Identifiable {
    let id = UUID()
    let feature: String
    let samples: [RecentSample]

    var count: Int { samples.count }
    var syncedCount: Int { samples.filter { $0.isSynced }.count }
    var syncPercentage: Int {
        guard count > 0 else { return 0 }
        return Int(Double(syncedCount) / Double(count) * 100)
    }
    var emoji: String { samples.first?.emoji ?? "📡" }
    var displayName: String { samples.first?.displayName ?? feature }

    var latestTimestamp: Date { samples.first?.timestamp ?? .distantPast }
}

func groupSamplesByFeature(_ samples: [RecentSample]) -> [FeatureGroup] {
    let grouped = Dictionary(grouping: samples) { $0.feature }
    return grouped.map { feature, arr in
        FeatureGroup(
            feature: feature,
            samples: arr.sorted { $0.timestamp > $1.timestamp }
        )
    }.sorted { $0.latestTimestamp > $1.latestTimestamp }
}


struct RecentSamplesSection: View {
    @ObservedObject var hk: HealthKitManager

    private var featureGroups: [FeatureGroup] {
        groupSamplesByFeature(hk.recentSamples)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Recent Collected Data")
                    .font(.headline)
                    .foregroundColor(.primary)
                Spacer()
                if !hk.recentSamples.isEmpty {
                    Text("\(hk.recentSamples.count) readings")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
            }
            .padding(.horizontal)

            if hk.recentSamples.isEmpty {
                EmptyRecentView()
            } else {
                ScrollView {
                    LazyVStack(spacing: 8) {
                        ForEach(featureGroups) { group in
                            NavigationLink(destination: FeatureDetailView(group: group)) {
                                FeatureGroupRow(group: group)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.horizontal)
                }
            }
        }
    }
}

struct FeatureGroupRow: View {
    let group: FeatureGroup

    var body: some View {
        HStack(spacing: 12) {
            Text(group.emoji)
                .font(.title2)
                .frame(width: 36, height: 36)
                .background(HimeColor.cream)
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))

            VStack(alignment: .leading, spacing: 2) {
                Text(group.displayName)
                    .font(.subheadline)
                    .foregroundColor(.primary)
                Text(group.latestTimestamp.formatted(date: .abbreviated, time: .shortened))
                    .font(.caption)
                    .foregroundColor(.secondary)
            }

            Spacer()

            VStack(alignment: .trailing, spacing: 2) {
                Text("\(group.count) readings")
                    .font(.subheadline.weight(.medium))
                    .foregroundColor(.primary)

                HStack(spacing: 4) {
                    if group.syncPercentage == 100 {
                        Image(systemName: "checkmark.circle.fill")
                            .font(.caption2)
                            .foregroundColor(HimeColor.ok)
                    }
                    Text("\(group.syncPercentage)% synced")
                        .font(.caption2.weight(.bold))
                        .foregroundColor(group.syncPercentage == 100 ? HimeColor.ok : HimeColor.warn)
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(HimeColor.card)
        .clipShape(RoundedRectangle(cornerRadius: HimeRadius.row, style: .continuous))
    }
}

struct FeatureDetailView: View {
    let group: FeatureGroup

    var body: some View {
        List {
            Section {
                ForEach(group.samples) { sample in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(sample.timestamp, style: .time)
                                .font(.subheadline.monospacedDigit())
                                .foregroundColor(.primary)
                            Text(sample.timestamp, style: .date)
                                .font(.caption2)
                                .foregroundColor(.secondary)
                        }
                        Spacer()
                        VStack(alignment: .trailing, spacing: 2) {
                            Text(sample.formattedValue)
                                .font(.subheadline.weight(.medium).monospacedDigit())
                                .foregroundColor(.primary)
                            if sample.isSynced {
                                Image(systemName: "checkmark.circle.fill")
                                    .font(.caption2)
                                    .foregroundColor(HimeColor.ok)
                            } else {
                                Image(systemName: "arrow.up.circle")
                                    .font(.caption2)
                                    .foregroundColor(HimeColor.warn)
                            }
                        }
                    }
                }
            } header: {
                Text("\(group.displayName) · \(group.count) readings")
            }
        }
        .navigationTitle(group.displayName)
        .navigationBarTitleDisplayMode(.inline)
    }
}

struct EmptyRecentView: View {
    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "waveform.path.ecg")
                .font(.system(size: 40))
                .foregroundColor(HimeColor.ink2.opacity(0.6))
            Text("No data collected yet")
                .font(.subheadline)
                .foregroundColor(HimeColor.ink2)
            Text("Connect and wear your Apple Watch to start collecting health data.")
                .font(.caption)
                .foregroundColor(HimeColor.ink2)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 32)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 48)
    }
}
