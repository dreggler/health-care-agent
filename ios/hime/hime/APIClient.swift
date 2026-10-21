import Foundation


enum APIClient {
    static func request(_ url: URL, method: String = "GET") -> URLRequest {
        var req = URLRequest(url: url)
        req.httpMethod = method
        let token = ServerConfig.authToken
        if !token.isEmpty {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return req
    }
}


enum ServerProbeResult {
    case ok

    case unauthorized
    case serverError
    case unreachable
    case invalidAddress
}

extension APIClient {


    static func probe(_ cfg: ServerConfig) async -> ServerProbeResult {
        guard let healthURL = URL(string: "\(cfg.apiBaseURL)/health"),
              let authedURL = URL(string: "\(cfg.apiBaseURL)/api/agent/status") else {
            return .invalidAddress
        }
        var health = URLRequest(url: healthURL)
        health.timeoutInterval = 6
        do {
            let (_, response) = try await URLSession.shared.data(for: health)
            guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
                return .serverError
            }
        } catch {
            return .unreachable
        }

        var authed = request(authedURL)
        authed.timeoutInterval = 6
        do {
            let (_, response) = try await URLSession.shared.data(for: authed)
            guard let http = response as? HTTPURLResponse else { return .serverError }
            if http.statusCode == 401 { return .unauthorized }
            return (200...299).contains(http.statusCode) ? .ok : .serverError
        } catch {
            return .unreachable
        }
    }
}
