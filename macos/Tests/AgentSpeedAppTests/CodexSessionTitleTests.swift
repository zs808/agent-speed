import AgentSpeedCore
import Foundation
import Testing
@testable import AgentSpeed

@Suite("Codex session titles")
struct CodexSessionTitleTests {
    private func agent(_ id: String, source: SourceKind = .codex, session: String?, parent: String? = nil) -> AgentStatus {
        AgentStatus(id: id, source: source, project: "workspace", model: nil, phase: .idle,
                    isSubagent: parent != nil, live: Rate(output: 0, total: 0), recentSpeed: nil,
                    lastActivity: Date(timeIntervalSince1970: 0), parentId: parent, sessionId: session)
    }

    @Test func usesTheNamedSessionAndFallsBackToItsParent() {
        let root = agent("root-file", session: "root-session")
        let child = agent("child-file", session: "child-session", parent: root.id)
        let grandchild = agent("grandchild-file", session: "grandchild-session", parent: child.id)
        let agents = [root, child, grandchild]
        let names = ["root-session": "会话名称", "child-session": "子任务"]
        #expect(AgentSessionTitle.resolve(for: root, among: agents, titles: names) == "会话名称")
        #expect(AgentSessionTitle.resolve(for: child, among: agents, titles: names) == "子任务")
        #expect(AgentSessionTitle.resolve(for: grandchild, among: agents, titles: names) == "子任务")
        #expect(AgentSessionTitle.resolve(for: child, among: agents, titles: ["root-session": "会话名称"]) == "会话名称")
    }

    @Test func unmatchedOtherSourcesAndCyclesFallBackToProject() {
        let first = agent("first", session: "one", parent: "second")
        let second = agent("second", session: "two", parent: "first")
        let other = agent("other", source: .claudeCode, session: "one")
        #expect(AgentSessionTitle.resolve(for: first, among: [first, second], titles: [:]) == nil)
        #expect(AgentSessionTitle.resolve(for: other, among: [other], titles: ["one": "Codex title"]) == nil)
    }

    @Test func readsTheMostRecentlyUpdatedMatchingName() async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let file = folder.appendingPathComponent("session_index.jsonl")
        let lines = [
            #"{"id":"first","thread_name":"新的 会话","updated_at":"2026-10-27T09:00:00.500Z"}"#,
            #"{"id":"first","thread_name":"旧的会话","updated_at":"2026-10-26T09:00:00Z"}"#,
            #"{"id":"second","thread_name":"另一个","updated_at":"2026-10-27T09:00:00Z"}"#,
            "{invalid json",
        ]
        try (lines.joined(separator: "\n") + "\n").write(to: file, atomically: true, encoding: .utf8)
        let index = CodexSessionTitleIndex()
        let names = await index.titles(indexURL: file, sessionIDs: ["first"])
        #expect(names == ["first": "新的 会话"])
    }

    @Test func reflectsRenamesAndMissingIndexWithoutReadingOtherTitles() async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let file = folder.appendingPathComponent("session_index.jsonl")
        let index = CodexSessionTitleIndex()
        #expect(await index.titles(indexURL: file, sessionIDs: ["first"]).isEmpty)
        try "{\"id\":\"first\",\"thread_name\":\"原名称\"}\n".write(to: file, atomically: true, encoding: .utf8)
        #expect(await index.titles(indexURL: file, sessionIDs: ["first"]) == ["first": "原名称"])
        try "{\"id\":\"first\",\"thread_name\":\"更新后的名称\"}\n{\"id\":\"other\",\"thread_name\":\"不需要\"}\n".write(to: file, atomically: true, encoding: .utf8)
        #expect(await index.titles(indexURL: file, sessionIDs: ["first"]) == ["first": "更新后的名称"])
    }

    @Test func resolvesTheIndexBesideTheConfiguredSessionsFolder() {
        let home = URL(fileURLWithPath: "/example/home", isDirectory: true)
        let custom = URL(fileURLWithPath: "/example/custom/sessions", isDirectory: true)
        #expect(CodexSessionTitleIndex.indexURL(sessionsRoot: custom, codexHome: nil, home: home).path == "/example/custom/session_index.jsonl")
        #expect(CodexSessionTitleIndex.indexURL(sessionsRoot: nil, codexHome: "/example/codex", home: home).path == "/example/codex/session_index.jsonl")
        #expect(CodexSessionTitleIndex.indexURL(sessionsRoot: nil, codexHome: nil, home: home).path == "/example/home/.codex/session_index.jsonl")
    }

    @Test func boundedReadsSkipPartialLinesButRetainCompleteTailEntries() async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let file = folder.appendingPathComponent("session_index.jsonl")
        let last = "{\"id\":\"recent\",\"thread_name\":\"近期会话\"}\n"
        let text = String(repeating: "x", count: 200) + "\n" + last
        try text.write(to: file, atomically: true, encoding: .utf8)
        let atBoundary = CodexSessionTitleIndex(maximumBytes: last.utf8.count)
        #expect(await atBoundary.titles(indexURL: file, sessionIDs: ["recent"]) == ["recent": "近期会话"])
        let insideEarlierLine = CodexSessionTitleIndex(maximumBytes: last.utf8.count + 10)
        #expect(await insideEarlierLine.titles(indexURL: file, sessionIDs: ["recent"]) == ["recent": "近期会话"])
    }
}
