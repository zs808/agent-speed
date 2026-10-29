import Foundation

/// `command_info` and `shell_kind` over a parsed command (TASK-PARSERS 3.7, 3.9).
enum CommandAnalysis {
    /// The class of every segment and the chosen one: the first with the highest class rank (3.7).
    static func choose(_ segments: [ShellSegment], cwd: String?) -> ([Classification], Int?, Classification) {
        var bestIndex: Int?
        var best = Classification.other
        var classes: [Classification] = []
        classes.reserveCapacity(segments.count)
        for (index, segment) in segments.enumerated() {
            let result = segment.isNeutral ? Classification.other : ProgramRules.classify(segment.words, cwd: cwd)
            classes.append(result)
            if result.commandClass.rank > best.commandClass.rank {
                bestIndex = index
                best = result
            }
        }
        return (classes, bestIndex, best)
    }

    static func info(_ command: ShellCommand, cwd: String?) -> TaskCommandInfo {
        let segments = command.segments
        let (classes, bestIndex, best) = choose(segments, cwd: cwd)
        var kept = zip(segments, classes).filter { $0.1.commandClass != .other }.map { tokens($0.0) }
        if kept.isEmpty { kept = segments.filter { !$0.isNeutral }.map(tokens) }
        if kept.isEmpty { kept = segments.filter { !$0.words.isEmpty }.map(tokens) }
        let key = TaskText.sha16(Bytes.joined(kept.map { Bytes.joined($0, separator: [0x1F]) }, separator: [0x1E]))
        guard let bestIndex else {
            return TaskCommandInfo(commandClass: .other, suite: nil, targeted: false, key: key, masked: false,
                                   background: segments.contains { $0.background }, errorFilter: false)
        }
        let chosen = segments[bestIndex]
        let later = segments[(bestIndex + 1)...]
        let masked = !later.isEmpty && ((chosen.opAfter == .pipe && !command.pipefail)
            || later.contains { [.semicolon, .or, .background].contains($0.opBefore) })
        let errorFilter = (best.commandClass == .build || best.commandClass == .check)
            && MaskedRuns.isErrorFilter(segments, chosen: bestIndex)
        return TaskCommandInfo(commandClass: best.commandClass, suite: best.suite, targeted: best.targeted, key: key,
                               masked: masked, background: chosen.background, errorFilter: errorFilter,
                               mavenCompileKey: masked || command.compileScopeUnsafe ? nil : mavenCompileKey(segments, classes, bestIndex, cwd))
    }

    private static func mavenCompileKey(_ segments: [ShellSegment], _ classes: [Classification],
                                        _ chosen: Int, _ cwd: String?) -> String? {
        guard let cwd else { return nil }
        let rawBase = cwd.replacingOccurrences(of: "\\", with: "/")
        var base = rawBase
        while base.hasSuffix("/") { base.removeLast() }
        let scalars = Array(rawBase.utf8)
        let drive = scalars.count >= 3 && scalars[2] == 47 && ((65...90).contains(scalars[0]) || (97...122).contains(scalars[0])) && scalars[1] == 58
        guard !base.isEmpty, base.hasPrefix("/") || drive,
              classes.filter({ $0.commandClass != .other }).count == 1 else { return nil }
        let segment = segments[chosen]
        guard ShellCommand.program(segment.words[0]) == Bytes(utf8: "mvn"), !segment.background else { return nil }
        for prior in segments[..<chosen] {
            guard let first = prior.words.first else { continue }
            if ["cd", "pushd", "popd"].contains(ShellCommand.program(first).string) {
                let words = prior.words.map(\.string)
                guard prior.opAfter == .and, words.count == 2, words[1] != "-", !words[1].hasPrefix("~"),
                      !words.contains(where: { word in "$`*?[".contains(where: { word.contains($0) }) }) else { return nil }
            }
        }
        let args = segment.words.dropFirst().map(\.string)
        let context = segment.context.string
        guard !([base, context] + args).contains(where: { word in "$`*?[".contains(where: { word.contains($0) }) }) else { return nil }
        let values: Set<String> = ["-pl", "--projects", "-f", "--file", "-s", "--settings", "-P", "--activate-profiles", "-T", "--threads", "-rf", "--resume-from", "-gs", "--global-settings", "-t", "--toolchains", "-l", "--log-file"]
        let reject: Set<String> = ["-h", "--help", "-v", "--version", "-V", "--show-version", "-fn", "--fail-never"]
        var kept: [String] = []
        var goals: [String] = []
        var i = 0
        while i < args.count {
            let word = args[i]
            if reject.contains(word) { return nil }
            if values.contains(word) {
                guard i + 1 < args.count else { return nil }
                kept += [word, args[i + 1]]
                i += 2
                continue
            }
            let bytes = Bytes(utf8: word)
            if cosmetic.contains(bytes) || cosmeticPrefixes.contains(where: { bytes.hasPrefix($0) }) { i += 1; continue }
            if word.hasPrefix("-D") {
                let prop = String(word.dropFirst(2).split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)[0])
                if ["test", "it.test", "skipTests", "surefire.failIfNoSpecifiedTests"].contains(prop) { i += 1; continue }
                if (prop.lowercased().contains("skip") && prop != "maven.test.skip") || prop.lowercased().contains("failonerror") { return nil }
            }
            if word.hasPrefix("-") { kept.append(word) } else { goals.append(word) }
            i += 1
        }
        if goals.first == "clean" { goals.removeFirst() }
        guard goals.count == 1, ProgramRules.mvnBuild.contains(Bytes(utf8: goals[0])) || ProgramRules.mvnTest.contains(Bytes(utf8: goals[0])) else { return nil }
        return TaskText.sha16(Bytes(utf8: (["maven-main-compile-v1", base, context] + kept).joined(separator: "\u{1f}")))
    }

    private static let cosmetic = TaskText.words([
        "-q", "-qq", "-v", "-vv", "-vvv", "--quiet", "--verbose", "--color", "--no-color", "--colors", "--no-colors",
        "--ansi", "--no-ansi", "--nocapture", "--no-capture", "--show-output", "--silent",
    ])
    private static let cosmeticPrefixes = TaskText.list(["--color=", "--colour=", "--colors=", "--tb="])
    private static let pytestReport = Pattern(#"-r[A-Za-z]+"#)
    private static let npmLike = TaskText.words(["npm", "pnpm", "yarn"])
    private static let scriptRunners = TaskText.words(["npm", "pnpm", "yarn", "bun"])
    private static let workspaceRunners = TaskText.words(["pnpm", "yarn"])
    private static let testAliases = TaskText.words(["test", "t", "tst"])
    private static let runAliases = TaskText.words(["run-script", "rum", "urn"])

    /// The key tokens of one kept segment (TASK-PARSERS 3.7, *key*).
    private static func tokens(_ segment: ShellSegment) -> [Bytes] {
        let words = segment.words
        let p = ShellCommand.program(words[0])
        var tokens = [p]
        for word in words.dropFirst() {
            if cosmetic.contains(word) || cosmeticPrefixes.contains(where: { word.hasPrefix($0) }) { continue }
            if p == Bytes(utf8: "pytest"),
               word == Bytes(utf8: "-s") || pytestReport.fullMatches(word) || word == Bytes(utf8: "--capture=no") {
                continue
            }
            tokens.append(word)
        }
        if npmLike.contains(p), tokens.count > 1, testAliases.contains(tokens[1]) {
            tokens = [p, Bytes(utf8: "run"), Bytes(utf8: "test")] + tokens.dropFirst(2)
        } else if scriptRunners.contains(p), tokens.count > 1, runAliases.contains(tokens[1]) {
            tokens = [p, Bytes(utf8: "run")] + tokens.dropFirst(2)
        } else if workspaceRunners.contains(p), tokens.count > 1, !ScriptRunners.npmBuiltins.contains(tokens[1]),
                  tokens[1].first != Byte.dash {
            tokens = [p, Bytes(utf8: "run")] + tokens.dropFirst()
        }
        if !segment.context.isEmpty { tokens.insert([0x40] + segment.context, at: 0) }
        return tokens
    }

    private static let writeRedirections = TaskText.words([">", ">>", ">|", "&>", "&>>", "<>"])
    private static let nullTargets = TaskText.words(["/dev/null", "NUL", "nul"])
    private static let fdTarget = Pattern(#"[0-9-]+"#)
    static let readPrograms = TaskText.words([
        "cat", "head", "tail", "less", "more", "nl", "wc", "ls", "tree", "stat", "file", "pwd", "bat", "batcat", "sed",
        "awk", "jq", "yq", "realpath", "readlink", "du", "basename", "dirname", "od", "xxd", "hexdump", "strings",
        "diff", "cmp", "sort", "uniq", "cut", "tr", "column", "which", "type", "whereis", "printenv", "date", "env",
        "exa", "eza", "lsd",
    ])
    static let searchPrograms = TaskText.words([
        "rg", "grep", "egrep", "fgrep", "ag", "ack", "find", "fd", "fdfind", "locate", "mdfind",
    ])
    private static let gitRead = TaskText.words([
        "status", "diff", "log", "show", "blame", "rev-parse", "ls-tree", "cat-file", "describe", "shortlog", "reflog",
        "branch", "remote", "tag", "config",
    ])
    private static let gitSearch = TaskText.words(["grep", "ls-files"])
    private static let gitValue = OptionList([
        "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env",
    ])
    private static let findActions = TaskText.words([
        "-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls",
    ])

    /// `shell_kind` (TASK-PARSERS 3.9).
    static func shellKind(_ command: ShellCommand) -> TaskActionKind {
        if command.appliesPatch || !command.patches.isEmpty { return .edit }
        if info(command, cwd: nil).commandClass != .other { return .command }
        for segment in command.segments {
            for (op, target) in segment.redirections where writeRedirections.contains(op) {
                if !nullTargets.contains(target), !fdTarget.fullMatches(target) { return .command }
            }
        }
        let segments = command.segments.filter { !$0.isNeutral }
        if segments.isEmpty { return .command }
        var search = false
        for segment in segments {
            let p = ShellCommand.program(segment.words[0])
            let args = Array(segment.words.dropFirst())
            if p == Bytes(utf8: "git") {
                let k = CommandOptions.dropOptions(args, from: 0, values: gitValue)
                let sub = k < args.count ? args[k] : nil
                if let sub, gitRead.contains(sub) {
                    continue
                } else if let sub, gitSearch.contains(sub) {
                    search = true
                } else {
                    return .command
                }
            } else if p == Bytes(utf8: "sed"), args.contains(where: { $0.hasPrefix(Bytes(utf8: "-i")) || $0.hasPrefix(Bytes(utf8: "--in-place")) }) {
                return .command
            } else if readPrograms.contains(p) {
                continue
            } else if searchPrograms.contains(p) {
                if p == Bytes(utf8: "find"), args.contains(where: { findActions.contains($0) }) { return .command }
                search = true
            } else {
                return .command
            }
        }
        return search ? .search : .read
    }
}
