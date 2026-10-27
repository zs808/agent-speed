import AgentSpeedCore
import Foundation

/// Reads only the local title index, never transcript messages. The cache exists only while the dashboard is open.
actor CodexSessionTitleIndex {
    private struct CacheKey: Equatable {
        let path: String
        let size: UInt64
        let modified: Date
        let fileNumber: UInt64
        let ids: Set<String>
    }

    private struct Entry: Decodable {
        let id: String
        let thread_name: String?
        let updated_at: String?
    }

    private struct Choice {
        let title: String?
        let updatedAt: Date?
    }

    private let maximumBytes: Int
    private var cachedKey: CacheKey?
    private var cachedTitles: [String: String] = [:]
    private let fractionalDates: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()
    private let wholeSecondDates = ISO8601DateFormatter()

    init(maximumBytes: Int = 8 * 1024 * 1024) {
        self.maximumBytes = max(1, maximumBytes)
    }

    static func indexURL(sessionsRoot: URL?, codexHome: String?, home: URL) -> URL {
        if let sessionsRoot {
            return sessionsRoot.deletingLastPathComponent().appendingPathComponent("session_index.jsonl")
        }
        let root = codexHome.flatMap { $0.isEmpty ? nil : URL(fileURLWithPath: $0, isDirectory: true) }
            ?? home.appendingPathComponent(".codex", isDirectory: true)
        return root.appendingPathComponent("session_index.jsonl")
    }

    func titles(indexURL: URL, sessionIDs: Set<String>) -> [String: String] {
        guard !sessionIDs.isEmpty else { return [:] }
        do {
            let attributes = try FileManager.default.attributesOfItem(atPath: indexURL.path)
            guard attributes[.type] as? FileAttributeType == .typeRegular,
                  let size = (attributes[.size] as? NSNumber)?.uint64Value,
                  let modified = attributes[.modificationDate] as? Date else { return [:] }
            let key = CacheKey(path: indexURL.path, size: size, modified: modified,
                               fileNumber: (attributes[.systemFileNumber] as? NSNumber)?.uint64Value ?? 0, ids: sessionIDs)
            if key == cachedKey { return cachedTitles }

            let handle = try FileHandle(forReadingFrom: indexURL)
            defer { try? handle.close() }
            let bytes = min(size, UInt64(maximumBytes))
            let start = size - bytes
            var startsOnLineBoundary = true
            if start > 0 {
                try handle.seek(toOffset: start - 1)
                startsOnLineBoundary = try handle.read(upToCount: 1)?.first == 10
            }
            try handle.seek(toOffset: start)
            var data = try handle.read(upToCount: Int(bytes)) ?? Data()
            if !startsOnLineBoundary {
                guard let newline = data.firstIndex(of: 10) else { return [:] }
                data.removeSubrange(...newline)
            }

            let decoder = JSONDecoder()
            var choices: [String: Choice] = [:]
            for line in data.split(separator: 10) {
                guard let entry = try? decoder.decode(Entry.self, from: Data(line)), sessionIDs.contains(entry.id) else { continue }
                let date = entry.updated_at.flatMap { fractionalDates.date(from: $0) ?? wholeSecondDates.date(from: $0) }
                if let old = choices[entry.id], let oldDate = old.updatedAt, let date, date < oldDate { continue }
                let clean = entry.thread_name?.split(whereSeparator: \.isWhitespace).joined(separator: " ")
                choices[entry.id] = Choice(title: clean.flatMap { $0.isEmpty ? nil : $0 }, updatedAt: date)
            }
            let titles = choices.compactMapValues(\.title)
            cachedKey = key
            cachedTitles = titles
            return titles
        } catch {
            cachedKey = nil
            cachedTitles = [:]
            return [:]
        }
    }
}

enum AgentSessionTitle {
    /// Child sessions without a title use their nearest named Codex ancestor; unavailable data keeps the project name.
    static func resolve(for agent: AgentStatus, among agents: [AgentStatus], titles: [String: String]) -> String? {
        guard agent.source == .codex else { return nil }
        var current = agent
        var visited: Set<String> = []
        while visited.insert(current.id).inserted {
            if let id = current.sessionId, let title = titles[id], !title.isEmpty { return title }
            guard let parentID = current.parentId,
                  let parent = agents.first(where: { $0.id == parentID && $0.source == .codex }) else { return nil }
            current = parent
        }
        return nil
    }
}
