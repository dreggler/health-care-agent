import Foundation
import Security


private enum TokenKeychain {
    private static let service = "com.hime.serverAuth"
    private static let account = "serverAuthToken"

    private static var baseQuery: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    static func read() -> String? {
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func write(_ value: String) {
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


struct ServerConfig {

    static let defaultAddress = "192.168.1.100"


    let baseAddress: String


    var isLocal: Bool {
        let host = baseAddress
        if host == "localhost" { return true }

        if host.hasSuffix(".local") { return true }

        let parts = host.split(separator: ".")
        if parts.count == 4 && parts.allSatisfy({ $0.allSatisfy(\.isNumber) }) { return true }


        return !host.isEmpty && !host.contains(".") && !host.contains(":")
    }


    var watchURL: String {
        if isLocal {
            return "ws://\(baseAddress):8765"
        } else {
            return "wss://watch.\(baseAddress)"
        }
    }


    var apiBaseURL: String {
        if isLocal {
            return "http://\(baseAddress):8000"
        } else {
            return "https://api.\(baseAddress)"
        }
    }


    var watchHTTPBaseURL: String {
        if isLocal {
            return "http://\(baseAddress):8765"
        } else {
            return "https://watch.\(baseAddress)"
        }
    }


    private static let legacyTokenKey = "serverAuthToken"


    static var authToken: String {
        get {
            if let stored = TokenKeychain.read() { return stored }
            if let legacy = UserDefaults.standard.string(forKey: legacyTokenKey), !legacy.isEmpty {
                TokenKeychain.write(legacy)
                UserDefaults.standard.removeObject(forKey: legacyTokenKey)
                return legacy
            }
            return ""
        }
        set {
            TokenKeychain.write(newValue)
            UserDefaults.standard.removeObject(forKey: legacyTokenKey)
        }
    }


    private static let pendingSurveyKey = "hime.pendingGoalSurvey"


    @MainActor private static var isFlushingSurvey = false

    static var hasPendingSurvey: Bool {
        UserDefaults.standard.data(forKey: pendingSurveyKey) != nil
    }


    static func stashPendingSurvey(_ payload: [String: Any]) {
        if let data = try? JSONSerialization.data(withJSONObject: payload) {
            UserDefaults.standard.set(data, forKey: pendingSurveyKey)
        }
    }


    @MainActor
    static func flushPendingSurvey() async {
        guard !isFlushingSurvey else { return }
        guard let data = UserDefaults.standard.data(forKey: pendingSurveyKey) else { return }
        isFlushingSurvey = true
        defer { isFlushingSurvey = false }


        UserDefaults.standard.removeObject(forKey: pendingSurveyKey)

        let cfg = load()
        guard let url = URL(string: "\(cfg.apiBaseURL)/api/agent/onboarding-survey") else {
            UserDefaults.standard.set(data, forKey: pendingSurveyKey)
            return
        }
        var req = APIClient.request(url, method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = data
        guard let (_, resp) = try? await URLSession.shared.data(for: req),
              let http = resp as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
            UserDefaults.standard.set(data, forKey: pendingSurveyKey)
            return
        }

    }


    private static let key = "serverBaseAddress"


    static func load() -> ServerConfig {
        let raw = UserDefaults.standard.string(forKey: key)
            ?? migrateLegacy()
            ?? defaultAddress
        return ServerConfig(baseAddress: normalized(raw))
    }


    static func normalized(_ raw: String) -> String {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? defaultAddress : trimmed
    }


    func save() {
        UserDefaults.standard.set(baseAddress, forKey: ServerConfig.key)
    }


    private static func migrateLegacy() -> String? {
        if let old = UserDefaults.standard.string(forKey: "serverURL"), !old.isEmpty {
            let extracted = extractBase(from: old)
            UserDefaults.standard.set(extracted, forKey: key)
            UserDefaults.standard.removeObject(forKey: "serverURL")
            UserDefaults.standard.removeObject(forKey: "serverHost")
            return extracted
        }
        if let old = UserDefaults.standard.string(forKey: "serverHost"), !old.isEmpty {
            UserDefaults.standard.set(old, forKey: key)
            UserDefaults.standard.removeObject(forKey: "serverHost")
            return old
        }
        return nil
    }


    static func extractBase(from raw: String) -> String {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)

        for prefix in ["wss://", "ws://", "https://", "http://",
                        "wss:", "ws:", "https:", "http:"] {
            if s.hasPrefix(prefix) { s = String(s.dropFirst(prefix.count)); break }
        }
        if let idx = s.firstIndex(of: "/") { s = String(s[..<idx]) }


        if s.hasPrefix("[") {

            if let close = s.firstIndex(of: "]") {
                s = String(s[s.index(after: s.startIndex)..<close])
            }
        } else if s.filter({ $0 == ":" }).count <= 1, let idx = s.lastIndex(of: ":") {
            let after = String(s[s.index(after: idx)...])
            if after.allSatisfy(\.isNumber) { s = String(s[..<idx]) }
        }
        for sub in ["watch.", "api.", "dashboard."] {
            if s.hasPrefix(sub) {
                let stripped = String(s.dropFirst(sub.count))
                if stripped.contains(".") { s = stripped; break }
            }
        }
        return s
    }
}
