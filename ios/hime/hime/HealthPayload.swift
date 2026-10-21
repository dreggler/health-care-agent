import Foundation


nonisolated struct HealthPayload: Codable {
    let ts: Double
    let v:  Double
    let f:  String

    init(ts: Double, value: Double, feature: String) {
        self.ts = ts
        self.v = value
        self.f = feature
    }

    private enum CodingKeys: String, CodingKey {
        case ts, v, f
    }
}
