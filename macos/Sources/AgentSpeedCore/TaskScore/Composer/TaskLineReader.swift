import Foundation

/// The per-file half of the task composer (METRICS 6.9 *Composing the stream*): everything a line gives that depends
/// only on the line and on the same file's earlier lines — the working directory, copied history, prompt kinds, the
/// parsers' view of each call, and each result analysed against the file's call with that id. Its output (`TaskLine`)
/// is applied by `TaskComposer` right after the source's parser has read the same line.
///
/// A value type, so seeding and history can run it on decoding threads. Inputs and outputs are held in memory only:
/// a call's input until its first result, nothing else.
struct TaskLineReader: Sendable {
    /// A call waiting for its result.
    struct Pending: Sendable {
        var tool: String
        var input: JSONValue?
        var cwd: String?
        var info: TaskCommandInfo?
        var continues: String?
    }

    enum Entry: Sendable {
        case call(Pending, handle: Int)
        case script(calls: [Pending], prints: [Int], handle: Int)
        /// A call that has its result: only its id is kept, so a repeat of its line adds nothing.
        case answered
    }

    /// The source whose rules apply (both Claude sources read alike).
    let source: SourceKind
    private(set) var cwd: String?
    var headerSeen = false
    /// Codex: the first `session_meta`'s time; the Pi family: the header's (copied history, METRICS 3.5, 4.7).
    var createdMs: Int64?
    /// Codex: `subagent_history_start_ordinal` of the first `session_meta` (METRICS 3.5).
    var startOrdinal: Int64?
    /// OpenClaw: the window's turns are mirrors (METRICS 4.14).
    var mirrorWindow = false
    private var calls: [String: Entry] = [:]
    private var nextHandle = 0

    static let copyToleranceMs: Int64 = 2000
    /// A long-lived file forgets its answered calls beyond this many (their ids only block repeats of the same message).
    static let maxCalls = 8192

    /// The reader of a scored source's file, nil for any other source.
    init?(source: SourceKind) {
        guard TaskScoring.scores(source) else { return nil }
        self.source = source == .claudeDesktop ? .claudeCode : source
    }

    mutating func setCwd(_ value: String?) {
        guard let value, !TaskPythonText.isBlank(value) else { return }
        cwd = value
    }

    // MARK: - Calls

    /// Prepares a call (`id` and `tool` strings, an id the file did not have yet), or nil.
    mutating func call(id: String?, t: Int64, tool: String?, input: JSONValue?, digest: TaskDigest) -> TaskPreparedCall? {
        guard let id, let tool, calls[id] == nil else { return nil }
        let handle = takeHandle()
        let (prepared, pending) = analyse(handle: handle, t: t, tool: tool, input: input, digest: digest)
        if calls.count >= Self.maxCalls { forgetAnsweredCalls() }
        calls[id] = .call(pending, handle: handle)
        return prepared
    }

    /// Prepares a Codex code-mode script (TASK-PARSERS 4.4): its calls in source order.
    mutating func script(id: String?, t: Int64, script: TaskCodeScript) -> TaskPreparedScript? {
        guard let id, calls[id] == nil else { return nil }
        let handle = takeHandle()
        var prepared: [TaskPreparedCall] = []
        var pending: [Pending] = []
        for call in script.calls {
            let digest = TaskDigest(hex: call.digest) ?? .empty
            let (one, waiting) = analyse(handle: handle, t: t, tool: call.tool, input: call.input, digest: digest)
            prepared.append(one)
            pending.append(waiting)
        }
        calls[id] = .script(calls: pending, prints: script.prints.map(\.call), handle: handle)
        return TaskPreparedScript(handle: handle, calls: prepared)
    }

    private mutating func takeHandle() -> Int {
        defer { nextHandle += 1 }
        return nextHandle
    }

    private mutating func forgetAnsweredCalls() {
        calls = calls.filter { _, entry in
            if case .answered = entry { return false }
            return true
        }
    }

    private func analyse(handle: Int, t: Int64, tool: String, input: JSONValue?,
                         digest: TaskDigest) -> (TaskPreparedCall, Pending) {
        let kind = TaskParsers.actionKind(source, tool: tool, input: input, cwd: cwd)
        var action = TaskAction(kind: kind, tool: tool, digest: digest)
        var info: TaskCommandInfo?
        if kind == .command, let text = TaskParsers.commandText(source, tool: tool, input: input) {
            let found = TaskParsers.commandInfo(text, cwd: cwd)
            info = found
            var compileKey = found.mavenCompileKey
            if found.suite?.hasPrefix("mvn ") == true, source == .codex, TaskFamily(source)?.toolName(tool) == "exec_command" {
                let object = input?.object ?? input?.string.flatMap { (try? JSONParser.parse($0))?.object }
                if let workdir = object?["workdir"] {
                    if let directory = workdir.string {
                        if directory != cwd { compileKey = TaskParsers.commandInfo(text, cwd: directory).mavenCompileKey }
                    } else {
                        compileKey = nil
                    }
                }
            }
            action.command = TaskCommand(commandClass: found.commandClass, suite: found.suite, targeted: found.targeted,
                                         key: TaskDigest(hex: found.key) ?? .empty,
                                         mavenCompileKey: compileKey.flatMap(TaskDigest.init(hex:)))
        } else if kind == .edit {
            action.hunks = TaskParsers.editHunks(source, tool: tool, input: input, cwd: cwd).map(TaskHunk.init)
        }
        let continues = TaskParsers.continuation(source, tool: tool, input: input)
        let prepared = TaskPreparedCall(handle: handle, t: t, action: action, info: info, continues: continues, cwd: cwd)
        return (prepared, Pending(tool: tool, input: input, cwd: cwd, info: info, continues: continues))
    }

    // MARK: - Results

    /// A result for call `id` at `t`; nil when it can change nothing: no call of the file has that id (yet), or the
    /// call already has its result (only the first counts).
    mutating func result(id: String?, result: JSONValue, t: Int64) -> TaskPreparedResult? {
        guard let id else { return nil }
        switch calls[id] {
        case nil:
            return nil
        case .script(let pending, let prints, let handle)?:
            calls[id] = nil
            var mapped: [(index: Int, answer: TaskPreparedAnswer)] = []
            if let items = TaskParsers.codeItems(source, result: result), !prints.isEmpty, items.count == prints.count {
                var done = Set<Int>()
                for (index, item) in zip(prints, items) where index < pending.count && done.insert(index).inserted {
                    mapped.append((index, answer(pending[index], result: .string(item ?? ""), t: t)))
                }
            }
            return TaskPreparedResult(handle: handle, t: t, answer: .script(mapped))
        case .call(let pending, let handle)?:
            let prepared = answer(pending, result: result, t: t)
            calls[id] = .answered
            return TaskPreparedResult(handle: handle, t: t, answer: .call(prepared))
        case .answered?:
            return nil
        }
    }

    private func answer(_ call: Pending, result: JSONValue, t: Int64) -> TaskPreparedAnswer {
        let outcome = TaskParsers.outcome(source, result: result, t: t, tool: call.tool, input: call.input, cwd: call.cwd)
        let session = call.continues == nil ? TaskParsers.runningSession(source, result: result) : nil
        let needsText = session != nil || call.continues != nil || call.info != nil
        let text = needsText ? TaskParsers.resultText(source, result: result) : nil
        var failures: TaskRunFailures?
        var masked = false
        if session != nil {
            masked = isMasked(call)
        } else if let info = call.info {
            failures = TaskRunFailures(TaskParsers.failures(info, output: text ?? "", cwd: call.cwd, failed: outcome.failed))
        }
        let kept = session != nil || call.continues != nil ? text : nil
        return TaskPreparedAnswer(outcome: outcome, failures: failures, session: session, masked: masked, text: kept)
    }

    /// `shell_call_state(...)[1]`: the command is masked (TASK-PARSERS 3.10).
    private func isMasked(_ call: Pending) -> Bool {
        if let info = call.info { return info.masked }
        guard let text = TaskParsers.commandText(source, tool: call.tool, input: call.input) else { return false }
        return TaskParsers.commandInfo(text, cwd: call.cwd).masked
    }
}

extension TaskHunk {
    init(_ hunk: TaskEditHunk) {
        self.init(file: TaskDigest(hex: hunk.file) ?? .empty, old: hunk.old.flatMap { TaskDigest(hex: $0) },
                  new: TaskDigest(hex: hunk.new) ?? .empty)
    }
}

extension TaskRunFailures {
    init(_ failures: TaskFailures) {
        self.init(fingerprints: failures.fingerprints.compactMap { TaskDigest(hex: $0) }, failedCount: failures.failedCount,
                  passedCount: failures.passedCount, framework: failures.framework)
    }
}

/// Which sources have a task score (METRICS 6.9).
public enum TaskScoring {
    public static let sources: Set<SourceKind> = [.codex, .claudeCode, .claudeDesktop, .omp, .pi, .openclaw, .droid]

    public static func scores(_ source: SourceKind) -> Bool { sources.contains(source) }
}
