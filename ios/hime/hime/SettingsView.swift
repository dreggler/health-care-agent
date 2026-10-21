import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var hk: HealthKitManager
    @EnvironmentObject var ws: WebSocketClient
    @StateObject private var lm = LogManager.shared

    @State private var showClearLogsConfirm = false
    @State private var showCopiedToast = false
    @State private var serverAddress: String = ServerConfig.load().baseAddress
    @State private var authToken: String = ServerConfig.authToken
    @State private var saveTask: Task<Void, Never>? = nil
    @State private var tokenSaveTask: Task<Void, Never>? = nil

    @AppStorage("hime.hasOnboarded") private var hasOnboarded: Bool = false
    @AppStorage("hime.hasConsentedToAIDataSharing") private var hasConsentedToAI: Bool = false
    @State private var connectionTestResult: String? = nil
    @State private var connectionTestSuccess: Bool = false
    @State private var isTestingConnection: Bool = false
    @State private var showAIDisclosureSheet = false
    @State private var showRevokeConsentConfirm = false
    @State private var showPlanSurvey = false

    var body: some View {
        Form {

            Section {
                VStack(spacing: 12) {
                    Image("AppLogo")
                        .resizable()
                        .aspectRatio(contentMode: .fit)
                        .frame(height: 80)
                        .padding(.vertical, 10)

                    VStack(spacing: 4) {
                        Text("CareAgent")
                            .font(.system(size: 24, weight: .bold, design: .rounded))
                            .tracking(4)

                        Text("Personal Health AI Agent")
                            .font(.caption2)
                            .foregroundColor(HimeColor.ink2)
                            .tracking(1)
                    }
                }
                .frame(maxWidth: .infinity)
                .listRowBackground(Color.clear)
            }


            Section {
                Link(destination: URL(string: "https://github.com/dreggler/health-care-agent")!) {
                    HStack {
                        GitHubIcon()
                            .frame(width: 20, height: 20)
                        Text("Deploy Your Own Server")
                        Spacer()
                        Text("GitHub")
                            .foregroundColor(HimeColor.ink2)
                            .font(.subheadline)
                        Image(systemName: "arrow.up.right.square")
                            .font(.caption)
                            .foregroundColor(HimeColor.ink2)
                    }
                }
            } footer: {
                Text("CareAgent requires a self-hosted server. Follow the setup guide on GitHub to deploy your backend and connect this app.")
            }


            Section {
                HStack {
                    Label("Server Address", systemImage: "network")
                    Spacer()
                    TextField("192.168.1.100 or example.com", text: $serverAddress)
                        .multilineTextAlignment(.trailing)
                        .keyboardType(.URL)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .foregroundColor(HimeColor.ink2)
                        .onSubmit {
                            saveTask?.cancel()
                            serverAddress = applyServerAddress()
                        }
                        .onChange(of: serverAddress) {
                            saveTask?.cancel()
                            saveTask = Task { @MainActor in
                                try? await Task.sleep(for: .milliseconds(500))
                                guard !Task.isCancelled else { return }
                                applyServerAddress()
                            }
                        }
                }

                HStack {
                    Label("Auth Token", systemImage: "lock")
                    Spacer()
                    SecureField("Optional", text: $authToken)
                        .multilineTextAlignment(.trailing)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .foregroundColor(HimeColor.ink2)
                        .onSubmit {
                            tokenSaveTask?.cancel()
                            Task { await applyAuthToken(authToken) }
                        }
                        .onChange(of: authToken) { _, newValue in


                            tokenSaveTask?.cancel()
                            tokenSaveTask = Task {
                                try? await Task.sleep(nanoseconds: 500_000_000)
                                guard !Task.isCancelled else { return }
                                await applyAuthToken(newValue)
                            }
                        }
                }

                HStack {
                    Label {
                        Text("Status")
                    } icon: {
                        Image(systemName: ws.isConnected ? "checkmark.circle.fill" : "xmark.circle.fill")
                            .foregroundColor(ws.isConnected ? HimeColor.ok : HimeColor.bad)
                    }
                    Spacer()
                    Text(ws.isConnected ? LocalizedStringKey("Connected") : LocalizedStringKey("Disconnected"))
                        .foregroundColor(HimeColor.ink2)
                }

                Button {
                    ws.isSyncActive ? ws.disconnect() : ws.connect()
                } label: {
                    Label(ws.isSyncActive ? LocalizedStringKey("Disconnect") : LocalizedStringKey("Connect"), systemImage: ws.isSyncActive ? "wifi.slash" : "wifi")
                }
                .foregroundColor(ws.isSyncActive ? HimeColor.bad : HimeColor.accentStrong)

                Button {
                    Task { await testServerConnection() }
                } label: {
                    HStack {
                        Label("Test Server Connection", systemImage: "bolt.horizontal.circle")
                        Spacer()
                        if isTestingConnection {
                            ProgressView().scaleEffect(0.8)
                        }
                    }
                }
                .disabled(isTestingConnection)

                if let result = connectionTestResult {
                    Text(result)
                        .font(.footnote)
                        .foregroundColor(connectionTestSuccess ? HimeColor.ok : HimeColor.bad)
                }

            } header: {
                Text("Server")
            } footer: {
                Text("Enter your server address. Auth Token must match the API_AUTH_TOKEN in your server's .env file (leave empty for local use).")
            }


            Section {
                HStack {
                    Label {
                        Text("AI Data Sharing Consent")
                    } icon: {
                        Image(systemName: hasConsentedToAI ? "checkmark.shield.fill" : "xmark.shield.fill")
                            .foregroundColor(hasConsentedToAI ? HimeColor.ok : HimeColor.bad)
                    }
                    Spacer()
                    Text(hasConsentedToAI ? LocalizedStringKey("Granted") : LocalizedStringKey("Not granted"))
                        .foregroundColor(HimeColor.ink2)
                        .font(.subheadline)
                }

                Button {
                    showAIDisclosureSheet = true
                } label: {
                    Label("Review Disclosure", systemImage: "doc.text.magnifyingglass")
                }
                .foregroundColor(HimeColor.accentStrong)

                if hasConsentedToAI {
                    Button(role: .destructive) {
                        showRevokeConsentConfirm = true
                    } label: {
                        Label("Revoke Consent", systemImage: "hand.raised.slash")
                    }
                    .confirmationDialog(
                        "Revoke consent to share data with third-party AI?",
                        isPresented: $showRevokeConsentConfirm,
                        titleVisibility: .visible
                    ) {
                        Button("Revoke", role: .destructive) {
                            hasConsentedToAI = false


                            hk.stopCollection()
                            ws.disconnect(userInitiated: false)
                            DeviceTokenUploader.shared.unregister()
                            hasOnboarded = false
                        }
                    } message: {
                        Text("The app will disconnect from your server and return to onboarding. You can re-grant consent there.")
                    }
                }
            } header: {
                Text("Data & Privacy")
            } footer: {
                Text("CareAgent sends Apple Health data and chat messages to the third-party AI provider configured on your server. Your explicit consent is required before any data is shared.")
            }


            Section {
                Button {
                    showPlanSurvey = true
                } label: {
                    Label("Redesign My Plan", systemImage: "wand.and.stars")
                }
                .foregroundColor(HimeColor.accentStrong)

                Button {
                    hasOnboarded = false
                } label: {
                    Label("Replay Onboarding", systemImage: "arrow.counterclockwise")
                }
                .foregroundColor(HimeColor.accentStrong)
            } header: {
                Text("Plan & Onboarding")
            } footer: {
                Text("Retake the goal survey and let CareAgent redesign your scheduled check-ins and plan.")
            }


            Section {
                HStack {
                    Label("Authorization", systemImage: "heart.text.square")
                    Spacer()


                    Text(hk.recentSamples.isEmpty ? hk.authStatus : "Receiving data")
                        .foregroundColor(hk.recentSamples.isEmpty ? HimeColor.warn : HimeColor.ok)
                        .font(.subheadline)
                }

                HStack {
                    Label("Last Sync", systemImage: "clock.arrow.circlepath")
                    Spacer()
                    Text(hk.lastSync)
                        .foregroundColor(HimeColor.ink2)
                }

                Toggle(isOn: $hk.isBurstModeEnabled) {
                    Label("Burst Mode (High Priority)", systemImage: "bolt.fill")
                        .foregroundColor(hk.isBurstModeEnabled ? HimeColor.warn : HimeColor.ink2)
                }

                Button {
                    hk.forceFetch(fullResync: true)
                } label: {
                    Label("Force Fetch All Data Now", systemImage: "arrow.clockwise")
                }
                .foregroundColor(HimeColor.accentStrong)

            } header: {
                Text("HealthKit")
            } footer: {
                Text("CareAgent monitors 50+ health metrics from your Apple Watch in the background using HealthKit observer queries.")
            }


            Section {
                if lm.logs.isEmpty {
                    Text("No recent activity.")
                        .foregroundColor(HimeColor.ink2)
                        .font(.subheadline)
                } else {
                    ScrollViewReader { proxy in
                        ScrollView {
                            LazyVStack(alignment: .leading, spacing: 2) {
                                ForEach(lm.logs) { entry in
                                    Text(entry.text)
                                        .font(.system(.caption2, design: .monospaced))
                                        .foregroundColor(HimeColor.ink2)
                                        .padding(.horizontal, 16)
                                        .padding(.vertical, 1)
                                        .id(entry.id)
                                }
                            }
                        }
                        .frame(height: 260)


                        .onChange(of: lm.logs.first?.id) {
                            if let first = lm.logs.first {
                                withAnimation {
                                    proxy.scrollTo(first.id, anchor: .top)
                                }
                            }
                        }
                    }
                    .listRowInsets(EdgeInsets(top: 4, leading: 0, bottom: 4, trailing: 0))
                }

                if !lm.logs.isEmpty {
                    Button {

                        UIPasteboard.general.string = lm.logs.reversed().map(\.text).joined(separator: "\n")
                        showCopiedToast = true
                        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                            showCopiedToast = false
                        }
                    } label: {
                        Label("Copy Logs", systemImage: "doc.on.doc")
                    }
                    .foregroundColor(HimeColor.accentStrong)

                    Button(role: .destructive) {
                        showClearLogsConfirm = true
                    } label: {
                        Label("Clear Logs", systemImage: "trash")
                    }
                    .confirmationDialog("Clear all activity logs?", isPresented: $showClearLogsConfirm, titleVisibility: .visible) {
                        Button("Clear", role: .destructive) { lm.logs.removeAll() }
                    }
                }
            } header: {
                Text("Activity Log")
            }


            Section {
                HStack {
                    Text("Version")
                    Spacer()
                    Text(Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "—")
                        .foregroundColor(HimeColor.ink2)
                }
                HStack {
                    Text("Build")
                    Spacer()
                    Text(Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "—")
                        .foregroundColor(HimeColor.ink2)
                }
            } header: {
                Text("About")
            }
        }
        .navigationTitle("Settings")
        .navigationBarTitleDisplayMode(.inline)
        .overlay {
            if showCopiedToast {
                Text("Copied!")
                    .font(.subheadline)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 8)
                    .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 8))
                    .transition(.opacity)
            }
        }
        .animation(.easeInOut(duration: 0.2), value: showCopiedToast)
        .sheet(isPresented: $showAIDisclosureSheet) {
            AIDisclosureSheet()
        }
        .sheet(isPresented: $showPlanSurvey) {
            PlanSurveySheet()
        }
    }


    private func testServerConnection() async {
        isTestingConnection = true
        connectionTestResult = nil
        connectionTestSuccess = false
        defer { isTestingConnection = false }


        tokenSaveTask?.cancel()
        saveTask?.cancel()
        let cfg = ServerConfig(baseAddress: applyServerAddress())
        await applyAuthToken(authToken)

        switch await APIClient.probe(cfg) {
        case .ok:
            let fmt = String(localized: "OK — connected to %@")
            connectionTestResult = String(format: fmt, cfg.baseAddress)
            connectionTestSuccess = true
        case .unauthorized:
            connectionTestResult = String(localized: "Server rejected the auth token (401)")
        case .invalidAddress:
            connectionTestResult = String(localized: "Invalid address")
        case .serverError:
            connectionTestResult = String(localized: "Server returned an error")
        case .unreachable:
            connectionTestResult = String(localized: "Could not reach server")
        }
    }


    @discardableResult
    private func applyServerAddress() -> String {
        let cleaned = ServerConfig.normalized(ServerConfig.extractBase(from: serverAddress))
        guard cleaned != ws.serverConfig.baseAddress else { return cleaned }
        let cfg = ServerConfig(baseAddress: cleaned)
        cfg.save()
        ws.serverConfig = cfg
        ws.serverSettingsDidChange()
        return cleaned
    }


    private func applyAuthToken(_ raw: String) async {
        let token = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if token != ServerConfig.authToken {
            ServerConfig.authToken = token
            ws.serverSettingsDidChange()
        }


        await ServerConfig.flushPendingSurvey()
    }
}


struct AIDisclosureSheet: View {
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    HStack(spacing: 12) {
                        Image(systemName: "brain.head.profile")
                            .font(.system(size: 32))
                            .foregroundColor(HimeColor.accentStrong)
                        VStack(alignment: .leading, spacing: 2) {
                            Text("AI Data Sharing")
                                .font(.title2.bold())
                            Text("How CareAgent shares your data with third-party AI")
                                .font(.footnote)
                                .foregroundColor(HimeColor.ink2)
                        }
                    }

                    Text(AIDisclosureContent.bodyText)
                        .font(.callout)
                        .foregroundColor(HimeColor.ink)
                        .fixedSize(horizontal: false, vertical: true)

                    AIDisclosureDetails()
                }
                .padding(20)
            }
            .navigationTitle("Data & Privacy")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }
}
