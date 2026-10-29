import Foundation

/// pass or fail of a test, build or check command whose result is known (METRICS 6.9 *Runs*).
enum TaskRunResult: Sendable {
    case pass, fail
}

/// `suite:<suite>` for a non-targeted run of a known suite, else `key:<key>` (METRICS 6.9 *Checks*).
enum TaskCheckID: Hashable, Sendable {
    case key(TaskDigest)
    case suite(TaskOrdinalKey)
    case mavenCompile(TaskDigest)

    init(_ command: TaskCommand) {
        if command.suite == "mvn compile", let key = command.mavenCompileKey {
            self = .mavenCompile(key)
        } else if !command.targeted, let suite = command.suite {
            self = .suite(TaskOrdinalKey(suite))
        } else {
            self = .key(command.key)
        }
    }
}

/// One action of a task as the evaluation sees it (`Act` of `task_score.py`).
struct TaskAct: Sendable {
    /// Its event's position in the evaluated events.
    var event: Int
    var kind: TaskActionKind
    /// The interned `(tool, digest)` of a counted action; -1 for `other`.
    var key: Int
    /// 0 for `other`, 2 for a test, build or check command, else 1.
    var weight: Int
    /// `kind == .command` only.
    var command: TaskCommand?
    /// The outcome's `t` when the outcome is known at the evaluation's instant.
    var outcomeT: Int64?
    /// The known outcome says `failed: true` (a failed edit changes nothing).
    var outcomeFailed = false
    var result: TaskRunResult?
    /// `F`: 0 unless the run fails.
    var errors = 0
    /// Runs only: the distinct fingerprints, sorted.
    var fingerprints: [TaskDigest] = []
    /// Runs only.
    var failedCount: Int64?
    /// Runs only: the index of its check.
    var check = -1
    /// The number of effective edits before it.
    var block = 0
    var effective = false
    var oscillating = false
    /// `ΔI`.
    var gain = 0
    /// Failing runs only: the act index of the next element of its check's sequence (nil when there is none).
    var next: Int?

    var targeted: Bool { command?.targeted ?? false }
    var suite: String? { command?.suite }
    var runClass: TaskCommandClass? { command?.commandClass }

    init(event: Int, action: TaskAction, now: Int64?, key: Int) {
        self.event = event
        self.key = key
        kind = action.kind
        command = action.kind == .command ? action.command : nil
        var outcome = action.outcome
        if let known = outcome, let now, let t = known.t, t > now { outcome = nil }
        outcomeT = outcome?.t
        outcomeFailed = outcome?.failed == true
        let verifying = command.map { $0.commandClass != .other } ?? false
        weight = kind == .other ? 0 : verifying ? 2 : 1
        guard verifying, let outcome else { return }
        let failures = action.failures
        result = Self.runResult(outcome, failures)
        guard result != nil else { return }
        let listed = failures?.fingerprints ?? []
        fingerprints = Self.distinctSorted(listed)
        failedCount = failures?.failedCount
        if result == .fail { errors = max(1, listed.count, failedCount.map { Int($0) } ?? 0) }
    }

    /// `run_result`: fail on `failed` true or a positive `failedCount`; pass on `failed` false, or a zero
    /// `failedCount` unless `passedCount` is zero; otherwise not a run yet.
    static func runResult(_ outcome: TaskOutcome, _ failures: TaskRunFailures?) -> TaskRunResult? {
        let failedCount = failures?.failedCount
        let passedCount = failures?.passedCount
        if outcome.failed == true || (failedCount.map { $0 > 0 } ?? false) { return .fail }
        if outcome.failed == false || (failedCount == 0 && passedCount != 0) { return .pass }
        return nil
    }

    static func distinctSorted(_ digests: [TaskDigest]) -> [TaskDigest] {
        var ordered = true
        for index in digests.indices.dropFirst() where !(digests[index - 1] < digests[index]) {
            ordered = false
            break
        }
        return ordered ? digests : Array(Set(digests)).sorted()
    }
}

/// Interns `(tool, digest)` pairs so loops and repeats compare integers.
struct TaskActKeys {
    private struct Key: Hashable {
        var tool: TaskOrdinalKey
        var digest: TaskDigest
    }

    private var ids: [Key: Int] = [:]

    var count: Int { ids.count }

    mutating func id(_ tool: String, _ digest: TaskDigest) -> Int {
        let key = Key(tool: TaskOrdinalKey(tool), digest: digest)
        if let id = ids[key] { return id }
        let id = ids.count
        ids[key] = id
        return id
    }
}

/// What a task's own edits say about one file (METRICS 6.9 *Effective edits*).
struct TaskFileEdits {
    /// Contents of whole-file writes.
    var written: Set<TaskDigest> = []
    /// The file's content when known (after a write, before any hunk).
    var latest: TaskDigest?
    /// Texts the task's hunks produced.
    var current: Set<TaskDigest> = []
    /// Texts the task's hunks replaced.
    var superseded: Set<TaskDigest> = []

    /// Applies one hunk: whether it changed the file and whether it oscillated.
    mutating func apply(_ hunk: TaskHunk) -> (changed: Bool, oscillated: Bool) {
        if let old = hunk.old {
            if old == hunk.new { return (false, false) }
            let oscillated = current.contains(old) && superseded.contains(hunk.new)
            superseded.insert(old)
            current.remove(old)
            current.insert(hunk.new)
            latest = nil
            return (true, oscillated)
        }
        if let latest, hunk.new == latest { return (false, false) }
        let oscillated = written.contains(hunk.new)
        written.insert(hunk.new)
        latest = hunk.new
        current.removeAll()
        superseded.removeAll()
        return (true, oscillated)
    }

    /// `apply_edit`: (effective, oscillating) of an edit with these hunks; a failed edit changes nothing, an edit
    /// without hunks is effective and does not oscillate.
    static func apply(_ files: inout [TaskDigest: TaskFileEdits], hunks: [TaskHunk]?, failed: Bool)
        -> (effective: Bool, oscillating: Bool) {
        if failed { return (false, false) }
        guard let hunks, !hunks.isEmpty else { return (true, false) }
        var changed = false
        var oscillating = false
        for hunk in hunks {
            let step = files[hunk.file, default: TaskFileEdits()].apply(hunk)
            changed = changed || step.changed
            oscillating = oscillating || step.oscillated
        }
        return (changed, changed && oscillating)
    }
}
