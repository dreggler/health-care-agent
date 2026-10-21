import Foundation
import Combine

struct LogEntry: Identifiable {
    let id = UUID()
    let text: String
}


private final class LogFileWriter: @unchecked Sendable {
    let queue = DispatchQueue(label: "hime.logmanager", qos: .utility)


    private let maxFileSize: UInt64 = 1_024 * 1_024

    private let logFileURL: URL

    private var fileHandle: FileHandle?

    init(url: URL) {
        logFileURL = url
    }

    deinit {
        try? fileHandle?.close()
    }

    func append(_ line: String) {
        let data = (line + "\n").data(using: .utf8) ?? Data()


        if !FileManager.default.fileExists(atPath: logFileURL.path) {
            FileManager.default.createFile(atPath: logFileURL.path, contents: nil)
            fileHandle = nil
        }

        if fileHandle == nil {
            do {
                fileHandle = try FileHandle(forWritingTo: logFileURL)
                fileHandle?.seekToEndOfFile()
            } catch {
                print("[LogManager] Failed to open log file: \(error.localizedDescription)")
                return
            }
        }

        guard let handle = fileHandle else { return }
        handle.seekToEndOfFile()
        handle.write(data)


        if handle.offsetInFile > maxFileSize {
            truncateLogFile()
        }
    }


    func loadRecent(limit: Int) -> [String] {
        guard let data = try? Data(contentsOf: logFileURL),
              let content = String(data: data, encoding: .utf8) else { return [] }

        let lines = content.components(separatedBy: "\n")
            .filter { !$0.isEmpty }
        return Array(lines.suffix(limit))
    }


    private func truncateLogFile() {
        try? fileHandle?.close()
        fileHandle = nil

        guard let data = try? Data(contentsOf: logFileURL),
              let content = String(data: data, encoding: .utf8) else { return }

        let lines = content.components(separatedBy: "\n")

        let keepCount = lines.count / 2
        let kept = lines.suffix(keepCount).joined(separator: "\n")

        try? kept.data(using: .utf8)?.write(to: logFileURL, options: .atomic)


        fileHandle = try? FileHandle(forWritingTo: logFileURL)
        fileHandle?.seekToEndOfFile()
    }
}

@MainActor
final class LogManager: ObservableObject {
    static let shared = LogManager()

    @Published var logs: [LogEntry] = []


    private let maxMemoryLogs = 500

    private let writer: LogFileWriter

    private init() {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        writer = LogFileWriter(url: docs.appendingPathComponent("hime_activity.log"))


        loadFromDisk()
    }

    func log(_ message: String) {
        let timestamp = Date().formatted(date: .omitted, time: .standard)
        let logLine = "[\(timestamp)] \(message)"
        logs.insert(LogEntry(text: logLine), at: 0)

        if logs.count > maxMemoryLogs {
            logs.removeLast()
        }

        print(logLine)


        let writer = self.writer
        writer.queue.async { writer.append(logLine) }
    }


    private func loadFromDisk() {
        let writer = self.writer
        let limit = maxMemoryLogs
        writer.queue.async {
            let lines = writer.loadRecent(limit: limit)
            guard !lines.isEmpty else { return }
            Task { @MainActor in


                self.logs.append(contentsOf: lines.reversed().map { LogEntry(text: $0) })
                if self.logs.count > limit {
                    self.logs.removeLast(self.logs.count - limit)
                }
            }
        }
    }
}
