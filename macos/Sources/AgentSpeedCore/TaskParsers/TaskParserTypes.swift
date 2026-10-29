import Foundation

/// What a tool call did (TASK-PARSERS 2).
public enum TaskActionKind: String, Sendable, CaseIterable {
    case read, search, edit, command, delegate, web, other
}

/// The strongest check a command runs (TASK-PARSERS 3): test > build > check > other.
public enum TaskCommandClass: String, Sendable, CaseIterable {
    case test, build, check, other

    /// other 0, check 1, build 2, test 3.
    public var rank: Int {
        switch self {
        case .other: 0
        case .check: 1
        case .build: 2
        case .test: 3
        }
    }
}

/// `command_info` (TASK-PARSERS 3). METRICS 6.9 keeps `commandClass`, `suite`, `targeted` and `key`; `masked` and
/// `background` serve `outcome`.
public struct TaskCommandInfo: Equatable, Hashable, Sendable {
    public var commandClass: TaskCommandClass
    /// nil or one of `TaskSuites.all`.
    public var suite: String?
    public var targeted: Bool
    /// 16 hex digits identifying the normalised command.
    public var key: String
    public var masked: Bool
    public var background: Bool
    /// Class build or check, and the chosen segment's output goes only through a grep for error lines (3.10).
    public var errorFilter: Bool
    public var mavenCompileKey: String?

    public init(commandClass: TaskCommandClass, suite: String?, targeted: Bool, key: String, masked: Bool,
                background: Bool, errorFilter: Bool, mavenCompileKey: String? = nil) {
        self.commandClass = commandClass
        self.suite = suite
        self.targeted = targeted
        self.key = key
        self.masked = masked
        self.background = background
        self.errorFilter = errorFilter
        self.mavenCompileKey = mavenCompileKey
    }
}

/// `outcome` (TASK-PARSERS 4.2). A nil `failed` means unknown, never a failure.
public struct TaskOutcome: Equatable, Hashable, Sendable {
    public var exitCode: Int64?
    public var failed: Bool?
    /// The time of the result's line (Unix ms), passed through.
    public var t: Int64?

    public init(exitCode: Int64?, failed: Bool?, t: Int64?) {
        self.exitCode = exitCode
        self.failed = failed
        self.t = t
    }
}

/// The status a masked command shows in its own output (TASK-PARSERS 3.10). A nil `exitCode` means the pipeline's
/// own code is kept.
public struct TaskMaskedStatus: Equatable, Hashable, Sendable {
    public var exitCode: Int64?
    public var failed: Bool
}

/// `failures` (TASK-PARSERS 5): fingerprints of the distinct failures and the counts the output states.
public struct TaskFailures: Equatable, Hashable, Sendable {
    /// Sorted distinct 16-hex digests, at most 256 (the smallest).
    public var fingerprints: [String]
    /// nil: unknown, never zero.
    public var failedCount: Int64?
    public var passedCount: Int64?
    public var framework: String?
}

/// One changed region of an edit (TASK-PARSERS 6): digests of the file, the old lines and the new lines.
public struct TaskEditHunk: Equatable, Hashable, Sendable {
    public var file: String
    /// nil for a whole-file write or a deletion.
    public var old: String?
    public var new: String
}

/// A Claude background-task notice that reports an exit code (TASK-PARSERS 4.3).
public struct TaskNotice: Equatable, Hashable, Sendable {
    public var session: String
    public var exitCode: Int64
}

/// A Codex code-mode script (TASK-PARSERS 4.4).
public struct TaskCodeScript: Equatable, Sendable {
    public struct Call: Equatable, Sendable {
        /// The tool name after `tools.`.
        public var tool: String
        /// The call's single literal argument, or `.null` when it has none or it is not a literal.
        public var input: JSONValue
        /// `sha16` of the script text from `tools` through the call's closing parenthesis.
        public var digest: String
    }

    public struct Print: Equatable, Sendable {
        public enum Mode: String, Sendable {
            /// `text(CALL)`: the item is the call's whole result.
            case result
            /// `text((CALL).output)`: the item is the command's output alone.
            case output
        }

        /// The index of the printed call in `calls`.
        public var call: Int
        public var mode: Mode
    }

    public var calls: [Call]
    /// Only `;`, bindings, awaited calls and printing statements; `prints` is empty otherwise.
    public var plain: Bool
    public var prints: [Print]
}
