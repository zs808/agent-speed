import Foundation

/// A 16-hex digest kept as its value (METRICS 6.9 *Event stream*): action digests, command keys, failure fingerprints
/// and hunk texts. Equal-length lowercase hex sorts like the number, so ordering matches the reference's string order.
public struct TaskDigest: Hashable, Comparable, Sendable {
    public let value: UInt64

    public init(value: UInt64) {
        self.value = value
    }

    /// Exactly 16 hex digits (either case).
    public init?(hex: String) {
        guard hex.utf8.count == 16, let value = UInt64(hex, radix: 16) else { return nil }
        self.value = value
    }

    public var hex: String {
        let digits = String(value, radix: 16)
        return String(repeating: "0", count: 16 - digits.count) + digits
    }

    public static func < (lhs: TaskDigest, rhs: TaskDigest) -> Bool { lhs.value < rhs.value }
}

/// The four `command_info` fields an action keeps (METRICS 6.9 *Event stream*).
public struct TaskCommand: Equatable, Hashable, Sendable {
    public var commandClass: TaskCommandClass
    /// nil or one of the fixed suite names (TASK-PARSERS 3.7).
    public var suite: String?
    public var targeted: Bool
    public var key: TaskDigest
    public var mavenCompileKey: TaskDigest?

    public init(commandClass: TaskCommandClass, suite: String?, targeted: Bool, key: TaskDigest, mavenCompileKey: TaskDigest? = nil) {
        self.commandClass = commandClass
        self.suite = suite
        self.targeted = targeted
        self.key = key
        self.mavenCompileKey = mavenCompileKey
    }
}

/// One changed region of an edit: digests of the file and of the old and new texts (`old` nil for a whole-file write).
public struct TaskHunk: Equatable, Hashable, Sendable {
    public var file: TaskDigest
    public var old: TaskDigest?
    public var new: TaskDigest

    public init(file: TaskDigest, old: TaskDigest?, new: TaskDigest) {
        self.file = file
        self.old = old
        self.new = new
    }
}

/// `failures` of a run (TASK-PARSERS 5) as the stream keeps it.
public struct TaskRunFailures: Equatable, Hashable, Sendable {
    /// Sorted, distinct, at most 256.
    public var fingerprints: [TaskDigest]
    /// nil: unknown.
    public var failedCount: Int64?
    public var passedCount: Int64?
    public var framework: String?

    public init(fingerprints: [TaskDigest], failedCount: Int64?, passedCount: Int64?, framework: String?) {
        self.fingerprints = fingerprints
        self.failedCount = failedCount
        self.passedCount = passedCount
        self.framework = framework
    }
}

/// One tool call of an agent (METRICS 6.9 *Event stream*): only classes, digests, counts, exit codes and times.
public struct TaskAction: Equatable, Hashable, Sendable {
    public var kind: TaskActionKind
    public var tool: String
    public var digest: TaskDigest
    /// `kind == .command` with command text only.
    public var command: TaskCommand?
    /// Once the result is read; `t` is the result line's time.
    public var outcome: TaskOutcome?
    /// With the outcome of an action that has `command`.
    public var failures: TaskRunFailures?
    /// `kind == .edit` only.
    public var hunks: [TaskHunk]?

    public init(kind: TaskActionKind, tool: String, digest: TaskDigest, command: TaskCommand? = nil,
                outcome: TaskOutcome? = nil, failures: TaskRunFailures? = nil, hunks: [TaskHunk]? = nil) {
        self.kind = kind
        self.tool = tool
        self.digest = digest
        self.command = command
        self.outcome = outcome
        self.failures = failures
        self.hunks = hunks
    }
}

/// A prompt or an action at time `t` (Unix ms), in file order.
public struct TaskEvent: Equatable, Hashable, Sendable {
    public enum Body: Equatable, Hashable, Sendable {
        case prompt(continuation: Bool)
        case action(TaskAction)
    }

    public var t: Int64
    public var body: Body

    public init(t: Int64, body: Body) {
        self.t = t
        self.body = body
    }

    public static func prompt(_ t: Int64, continuation: Bool) -> TaskEvent {
        TaskEvent(t: t, body: .prompt(continuation: continuation))
    }

    public static func action(_ t: Int64, _ action: TaskAction) -> TaskEvent {
        TaskEvent(t: t, body: .action(action))
    }

    public var action: TaskAction? {
        if case .action(let action) = body { return action }
        return nil
    }

    public var isPrompt: Bool {
        if case .prompt = body { return true }
        return false
    }
}

/// The agent's model and effort after a line (METRICS 6.9 *Model and effort*): records take the last note whose `t` is
/// at most the task's end.
public struct TaskModelNote: Equatable, Hashable, Sendable {
    public var t: Int64
    public var model: String?
    public var effort: String?

    public init(t: Int64, model: String?, effort: String?) {
        self.t = t
        self.model = model
        self.effort = effort
    }
}
