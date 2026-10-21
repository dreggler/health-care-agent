import Foundation


nonisolated final class PendingStore: @unchecked Sendable {
    static let shared = PendingStore()

    private nonisolated struct JournalLine: Codable {
        var ts: Double?
        var v: Double?
        var f: String?
        var t: Int?
        var pop: Int?
    }

    private nonisolated struct Key: Hashable {
        let ts: Double
        let f: String
    }

    private static let fileName = "hk_pending.jsonl"
    private static let consentKey = "hime.hasConsentedToAIDataSharing"


    private static let evictFirstFeatures: Set<String> = [
        "steps", "distance", "flights_climbed", "exercise_time",
        "stand_time", "active_energy", "resting_energy", "water"
    ]

    private let fileURL: URL
    private let lock = NSLock()
    private let maxSize = 100_000


    private var items: [HealthPayload] = []


    private var index: [Key: Int] = [:]
    private var baseSeq = 0


    private var inFlight = 0

    private var journalLines = 0
    private var loaded = false

    private init() {
        let fm = FileManager.default
        let support = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        try? fm.createDirectory(at: support, withIntermediateDirectories: true)
        self.fileURL = support.appendingPathComponent(Self.fileName)
        migrateLegacyCachesFile()
        markExcludedFromBackup()
    }


    private func migrateLegacyCachesFile() {
        let fm = FileManager.default
        let caches = fm.urls(for: .cachesDirectory, in: .userDomainMask)[0]
        let legacy = caches.appendingPathComponent("hk_pending.json")
        guard fm.fileExists(atPath: legacy.path) else { return }
        defer { try? fm.removeItem(at: legacy) }
        guard !fm.fileExists(atPath: fileURL.path),
              let data = try? Data(contentsOf: legacy),
              let old = try? JSONDecoder().decode([HealthPayload].self, from: data),
              !old.isEmpty else { return }
        lock.lock()
        defer { lock.unlock() }
        loadIfNeeded()
        for p in old { upsert(p) }
        writeSnapshot()
    }

    private func markExcludedFromBackup() {
        var url = fileURL
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try? url.setResourceValues(values)
    }


    @discardableResult
    func append(_ payloads: [HealthPayload]) -> Bool {
        guard !payloads.isEmpty else { return true }
        guard UserDefaults.standard.bool(forKey: Self.consentKey) else {
            HealthKitManager.bgLog("Store: consent not granted — dropping \(payloads.count) samples")
            return false
        }
        lock.lock()
        defer { lock.unlock() }
        loadIfNeeded()

        var lines: [JournalLine] = []
        for p in payloads {
            guard p.ts.isFinite, p.v.isFinite else { continue }
            let raw = upsert(p)
            lines.append(JournalLine(ts: p.ts, v: p.v, f: p.f, t: raw ? 1 : nil, pop: nil))
        }

        if items.count > maxSize {
            evictOverflow()
            return writeSnapshot()
        }
        if shouldCompact() { return writeSnapshot() }
        return appendToJournal(lines)
    }


    func peek(limit: Int) -> [HealthPayload] {
        lock.lock()
        defer { lock.unlock() }
        loadIfNeeded()
        let out = Array(items.prefix(limit))
        inFlight = out.count
        return out
    }

    func pop(count: Int) {
        guard count > 0 else { return }
        lock.lock()
        defer { lock.unlock() }
        loadIfNeeded()
        dropFront(min(count, items.count))
        inFlight = max(0, inFlight - count)
        if items.isEmpty {
            resetFile()
        } else if shouldCompact() {
            writeSnapshot()
        } else {
            appendToJournal([JournalLine(ts: nil, v: nil, f: nil, t: nil, pop: count)])
        }
    }


    func clear() {
        lock.lock()
        defer { lock.unlock() }
        items.removeAll()
        index.removeAll()
        baseSeq = 0
        inFlight = 0
        loaded = true
        resetFile()
    }

    var count: Int {
        lock.lock()
        defer { lock.unlock() }
        loadIfNeeded()
        return items.count
    }


    @discardableResult
    private func upsert(_ p: HealthPayload) -> Bool {
        let key = Key(ts: p.ts, f: p.f)
        if let seq = index[key] {
            let pos = seq - baseSeq
            if pos >= inFlight && pos < items.count {
                items[pos] = p
                return false
            }

            items.append(p)
            index[key] = baseSeq + items.count - 1
            return true
        }
        items.append(p)
        index[key] = baseSeq + items.count - 1
        return false
    }

    private func dropFront(_ n: Int) {
        guard n > 0 else { return }
        for i in 0..<n {
            let p = items[i]
            let key = Key(ts: p.ts, f: p.f)
            if index[key] == baseSeq + i { index.removeValue(forKey: key) }
        }
        items.removeFirst(n)
        baseSeq += n
    }


    private func evictOverflow() {
        var excess = items.count - maxSize
        guard excess > 0 else { return }
        var keep: [HealthPayload] = []
        keep.reserveCapacity(items.count)
        for (i, p) in items.enumerated() {
            if excess > 0, i >= inFlight, Self.evictFirstFeatures.contains(p.f) {
                excess -= 1
                continue
            }
            keep.append(p)
        }
        if excess > 0 {

            let start = min(inFlight, keep.count)
            keep.removeSubrange(start..<min(start + excess, keep.count))
        }
        HealthKitManager.bgLog("Store: overflow — evicted \(items.count - keep.count) entries")
        items = keep
        baseSeq = 0
        index.removeAll(keepingCapacity: true)
        for (i, p) in items.enumerated() { index[Key(ts: p.ts, f: p.f)] = i }
    }


    private func loadIfNeeded() {
        guard !loaded else { return }
        loaded = true
        items = []
        index = [:]
        baseSeq = 0
        journalLines = 0
        guard let data = try? Data(contentsOf: fileURL), !data.isEmpty else { return }

        let decoder = JSONDecoder()
        var corrupt = 0
        for slice in data.split(separator: UInt8(ascii: "\n"), omittingEmptySubsequences: true) {

            guard let line = try? decoder.decode(JournalLine.self, from: Data(slice)) else {
                corrupt += 1
                continue
            }
            journalLines += 1
            if let n = line.pop {
                dropFront(min(n, items.count))
            } else if let ts = line.ts, let v = line.v, let f = line.f {
                let p = HealthPayload(ts: ts, value: v, feature: f)
                if line.t == 1 {
                    items.append(p)
                    index[Key(ts: ts, f: f)] = baseSeq + items.count - 1
                } else {
                    upsert(p)
                }
            }
        }
        inFlight = 0
        if corrupt > 0 {
            HealthKitManager.bgLog("Store: skipped \(corrupt) unreadable journal line(s)")
            writeSnapshot()
        }
    }

    private func shouldCompact() -> Bool {
        journalLines > items.count * 2 + 2000
    }

    private func encode(_ lines: [JournalLine]) -> Data {
        let encoder = JSONEncoder()
        var out = Data()
        for line in lines {
            guard let d = try? encoder.encode(line) else { continue }
            out.append(d)
            out.append(0x0A)
        }
        return out
    }

    @discardableResult
    private func appendToJournal(_ lines: [JournalLine]) -> Bool {
        guard !lines.isEmpty else { return true }
        let data = encode(lines)
        guard !data.isEmpty else { return true }
        do {
            if !FileManager.default.fileExists(atPath: fileURL.path) {
                try data.write(to: fileURL, options: .atomic)
                markExcludedFromBackup()
            } else {
                let handle = try FileHandle(forWritingTo: fileURL)
                defer { try? handle.close() }
                try handle.seekToEnd()
                try handle.write(contentsOf: data)
            }
            journalLines += lines.count
            return true
        } catch {
            HealthKitManager.bgLog("PendingStore journal write failed: \(error.localizedDescription)")
            return false
        }
    }


    @discardableResult
    private func writeSnapshot() -> Bool {
        let lines = items.map { JournalLine(ts: $0.ts, v: $0.v, f: $0.f, t: 1, pop: nil) }
        do {
            try encode(lines).write(to: fileURL, options: .atomic)
            markExcludedFromBackup()
            journalLines = lines.count
            return true
        } catch {
            HealthKitManager.bgLog("PendingStore snapshot failed: \(error.localizedDescription)")
            return false
        }
    }

    private func resetFile() {
        try? FileManager.default.removeItem(at: fileURL)
        journalLines = 0
    }
}
