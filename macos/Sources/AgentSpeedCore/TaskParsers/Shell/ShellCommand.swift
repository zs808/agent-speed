import Foundation

/// One simple command of a command line after keywords, prefixes and runners are removed (TASK-PARSERS 3.3, 3.4).
final class ShellSegment {
    var words: [Bytes] = []
    var redirections: [(operator: Bytes, target: Bytes)] = []
    var heredocs: [Bytes] = []
    var ops: [ShellOperator] = []
    var opBefore: ShellOperator?
    var opAfter: ShellOperator?
    var background = false
    /// The directory context it runs in, relative to the starting directory (empty there).
    var context = Bytes()

    var isNeutral: Bool {
        guard let first = words.first else { return true }
        return ShellCommand.neutral.contains(ShellCommand.program(first))
    }
}

/// A parsed command line (TASK-PARSERS 3.3): its segments, the patch texts it holds and whether `pipefail` is set.
final class ShellCommand {
    private(set) var segments: [ShellSegment] = []
    private(set) var patches: [Bytes] = []
    private(set) var appliesPatch = false
    private(set) var pipefail = false
    private(set) var compileScopeUnsafe = false

    static let maxWrapperDepth = 4

    private let cwd: String?
    private var context = Bytes()
    private var parens: [Bytes] = []
    private var dirs: [Bytes] = []
    private var carry: [ShellOperator] = []

    init(_ text: String, cwd: String?) {
        self.cwd = cwd
        parse(Bytes(utf8: text), depth: 0)
        for (index, segment) in segments.enumerated() {
            segment.opBefore = index > 0 ? Self.lastOperator(segment.ops) : nil
            let following = index + 1 < segments.count ? segments[index + 1].ops : carry
            segment.opAfter = Self.lastOperator(following)
            if Self.firstOperator(following) == .background { segment.background = true }
        }
    }

    private static let assignmentPrefix = Pattern(#"[A-Za-z_][A-Za-z0-9_]*="#)

    private static func lastOperator(_ ops: [ShellOperator]) -> ShellOperator? {
        ops.last { !$0.isParenthesis }
    }

    private static func firstOperator(_ ops: [ShellOperator]) -> ShellOperator? {
        ops.first { !$0.isParenthesis }
    }

    private func parse(_ text: Bytes, depth: Int) {
        guard depth <= Self.maxWrapperDepth else { return }
        let (split, tail) = Self.splitSegments(ShellTokenizer.tokenize(text.lfNormalized()))
        for segment in split {
            applyParentheses(segment.ops)
            let ops = carry + segment.ops
            carry = []
            if segment.words.contains(where: { Self.assignmentPrefix.matches($0) && !$0.hasPrefix(Bytes(utf8: "JAVA_HOME=")) }) {
                compileScopeUnsafe = true
            }
            Self.dropKeywords(segment)
            Self.stripPrefixes(segment)
            Self.dropKeywords(segment)
            if let first = segment.words.first, Self.closingKeywords.contains(first) || Self.constructs.contains(first) {
                segment.words = []
            }
            let script = Self.shellScript(segment)
            patches.append(contentsOf: segment.heredocs.filter(Self.hasPatchLine))
            if let script {
                carry = ops
                let before = segments.count
                parse(script, depth: depth + 1)
                if segment.background, segments.count > before { segments[segments.count - 1].background = true }
                continue
            }
            patches.append(contentsOf: segment.words.filter { $0.contains(Byte.lf) && Self.hasPatchLine($0) })
            segment.words = Self.unwrap(segment.words)
            segment.context = context
            if let first = segment.words.first {
                let program = Self.program(first)
                if program == Bytes(utf8: "apply_patch") || program == Bytes(utf8: "applypatch") { appliesPatch = true }
                if program == Bytes(utf8: "set"), setsPipefail(segment.words) { pipefail = true }
                if program == Bytes(utf8: "cd") || program == Bytes(utf8: "pushd") {
                    if program == Bytes(utf8: "pushd") { dirs.append(context) }
                    let target = segment.words.dropFirst().first { !$0.hasPrefix([Byte.dash]) || $0 == [Byte.dash] }
                    context = normalizedDirectory(target ?? Bytes(utf8: "~"))
                } else if program == Bytes(utf8: "popd") {
                    context = dirs.popLast() ?? context
                }
            }
            if !segment.words.isEmpty || !segment.heredocs.isEmpty {
                segment.ops = ops
                segments.append(segment)
            } else {
                carry = ops
            }
        }
        applyParentheses(tail)
        carry += tail
    }

    private func setsPipefail(_ words: [Bytes]) -> Bool {
        for (index, word) in words.enumerated() where Self.pipefailFlag.fullMatches(word) {
            if index + 1 < words.count, words[index + 1] == Bytes(utf8: "pipefail") { return true }
        }
        return false
    }

    private func applyParentheses(_ ops: [ShellOperator]) {
        for op in ops {
            if op == .open {
                parens.append(context)
            } else if op == .close, let saved = parens.popLast() {
                context = saved
            }
        }
    }

    /// The directory context after `cd target` (TASK-PARSERS 3.3).
    private func normalizedDirectory(_ target: Bytes) -> Bytes {
        let t = target.replacing(byte: Byte.backslash, with: Byte.slash)
        if t.isEmpty { return context }
        let (relative, inside) = TaskPaths.cwdRelative(t, cwd: cwd)
        let joined: Bytes
        if inside {
            joined = relative
        } else if t == Bytes(utf8: "~") || t.hasPrefix(Bytes(utf8: "~/")) || t.hasPrefix([Byte.slash]) || TaskPaths.startsWithDrive(t)
                    || t == [Byte.dash] {
            joined = t
        } else {
            joined = context.isEmpty ? t : context + [Byte.slash] + t
        }
        var parts: [Bytes] = []
        for part in joined.split(byte: Byte.slash) {
            if part.isEmpty || part == [Byte.dot] { continue }
            if part == Bytes(utf8: ".."), let last = parts.last, last != Bytes(utf8: "..") {
                parts.removeLast()
                continue
            }
            parts.append(part)
        }
        let result = (joined.first == Byte.slash ? [Byte.slash] : []) + Bytes.joined(parts, separator: [Byte.slash])
        return result == [Byte.dot] ? [] : result
    }

    private static func splitSegments(_ items: [ShellItem]) -> ([ShellSegment], [ShellOperator]) {
        var segments: [ShellSegment] = []
        var current = ShellSegment()
        var ops: [ShellOperator] = []
        for item in items {
            switch item {
            case .word(let word):
                current.words.append(word)
            case .redirection(let op, let target):
                current.redirections.append((op, target))
            case .heredoc(let body):
                current.heredocs.append(body)
            case .op(let op):
                if !current.words.isEmpty || !current.heredocs.isEmpty {
                    current.ops = ops
                    segments.append(current)
                    ops = []
                }
                current = ShellSegment()
                ops.append(op)
            }
        }
        if !current.words.isEmpty || !current.heredocs.isEmpty {
            current.ops = ops
            segments.append(current)
            ops = []
        }
        return (segments, ops)
    }

    static func hasPatchLine(_ text: Bytes) -> Bool {
        text.split(byte: Byte.lf).contains { patchLine.fullMatches($0) }
    }

    // MARK: Words

    static let shells = TaskText.words(["bash", "sh", "zsh", "dash", "ksh", "fish"])
    static let powershells = TaskText.words(["pwsh", "powershell"])
    static let leadingKeywords = TaskText.words(["!", "if", "then", "else", "elif", "do", "while", "until", "{"])
    static let closingKeywords = TaskText.words(["fi", "done", "esac", "}"])
    static let constructs = TaskText.words(["for", "case", "select", "function"])
    static let neutral = TaskText.words([
        "cd", "pushd", "popd", "export", "set", "unset", "source", ".", "true", ":", "echo", "printf", "sleep", "clear",
        "alias", "shopt", "ulimit", "umask", "wait", "trap", "exit", "return", "local", "declare", "typeset", "read",
        "false",
    ])
    static let shellCFlag = Pattern(#"-[A-Za-z]*c[A-Za-z]*"#)
    static let nameAssign = Pattern(#"[A-Za-z_][A-Za-z0-9_]*="#)
    static let pipefailFlag = Pattern(#"-[A-Za-z]*o"#)
    static let patchLine = Pattern(#"[ \t]*\*\*\* Begin Patch[ \t]*"#)
    private static let pythonName = Pattern(#"python[0-9.]*|py|pypy[0-9.]*"#)
    private static let compilerName = Pattern(#"(gcc|g\+\+|cc|c\+\+|clang|clang\+\+)(-[0-9.]+)?"#)
    private static let programAliases: [Bytes: Bytes] = [
        "gradlew": "gradle", "mvnw": "mvn", "py.test": "pytest", "nodejs": "node", "gmake": "make", "bazelisk": "bazel",
    ].reduce(into: [:]) { $0[Bytes(utf8: $1.key)] = Bytes(utf8: $1.value) }
    private static let windowsExtensions = TaskText.list([".exe", ".cmd", ".bat", ".ps1"])

    /// The normalised program name of a word (TASK-PARSERS 3.4).
    static func program(_ word: Bytes) -> Bytes {
        var start = word.count
        while start > 0, word[start - 1] != Byte.slash, word[start - 1] != Byte.backslash { start -= 1 }
        var base = Bytes(word[start...]).lowercasedASCII()
        if let ext = windowsExtensions.first(where: { base.hasSuffix($0) }) { base.removeLast(ext.count) }
        if pythonName.fullMatches(base) { return Bytes(utf8: "python") }
        if compilerName.fullMatches(base) { return Bytes(utf8: "cc") }
        return programAliases[base] ?? base
    }

    // MARK: Keywords, prefixes and wrappers (3.3, 3.4)

    private static func dropKeywords(_ segment: ShellSegment) {
        var words = segment.words[...]
        while let first = words.first, leadingKeywords.contains(first) { words = words.dropFirst() }
        while let last = words.last, last == Bytes(utf8: "}") { words = words.dropLast() }
        if words.count != segment.words.count { segment.words = Array(words) }
    }

    private static let niceValue = OptionList(["-n", "--adjustment"])
    private static let stdbufValue = OptionList(["-i", "-o", "-e"])
    private static let caffeinateValue = OptionList(["-t", "-w"])
    private static let xvfbValue = OptionList([
        "-s", "--server-args", "-n", "--server-num", "-e", "--error-file", "-f", "--auth-file", "-p", "--xauth-protocol",
    ])
    private static let timeoutValue = OptionList(["-s", "--signal", "-k", "--kill-after"])
    private static let envValue = OptionList(["-u", "--unset", "-C", "--chdir", "-S", "--split-string"])
    private static let sudoValue = OptionList(["-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "--user", "--group"])
    private static let plainPrefixes = TaskText.words(["exec", "command", "builtin", "noglob", "unbuffer", "chronic"])

    private static func stripPrefixes(_ segment: ShellSegment) {
        let words = segment.words
        var i = 0
        while i < words.count {
            let word = words[i]
            if nameAssign.matches(word) {
                i += 1
                continue
            }
            let p = program(word)
            switch p {
            case Bytes(utf8: "env"):
                i = CommandOptions.dropOptions(words, from: i + 1, values: envValue)
            case Bytes(utf8: "sudo"), Bytes(utf8: "doas"):
                i = CommandOptions.dropOptions(words, from: i + 1, values: sudoValue)
            case Bytes(utf8: "time"):
                i = CommandOptions.dropOptions(words, from: i + 1)
            case Bytes(utf8: "nice"):
                i = CommandOptions.dropOptions(words, from: i + 1, values: niceValue)
            case Bytes(utf8: "nohup"), Bytes(utf8: "setsid"):
                segment.background = true
                i = CommandOptions.dropOptions(words, from: i + 1)
            case _ where plainPrefixes.contains(p):
                i = CommandOptions.dropOptions(words, from: i + 1)
            case Bytes(utf8: "stdbuf"):
                i = CommandOptions.dropOptions(words, from: i + 1, values: stdbufValue)
            case Bytes(utf8: "caffeinate"):
                i = CommandOptions.dropOptions(words, from: i + 1, values: caffeinateValue)
            case Bytes(utf8: "xvfb-run"):
                i = CommandOptions.dropOptions(words, from: i + 1, values: xvfbValue)
            case Bytes(utf8: "timeout"), Bytes(utf8: "gtimeout"):
                i = CommandOptions.dropOptions(words, from: i + 1, values: timeoutValue) + 1
            default:
                segment.words = i >= words.count ? [] : Array(words[i...])
                return
            }
        }
        segment.words = []
    }

    /// The script of a shell wrapper (`bash -lc '…'`, `pwsh -Command …`, `cmd /c …`), or nil.
    private static func shellScript(_ segment: ShellSegment) -> Bytes? {
        guard let first = segment.words.first else { return nil }
        let p = program(first)
        let rest = Array(segment.words.dropFirst())
        if shells.contains(p) {
            for (k, word) in rest.enumerated() {
                if !word.hasPrefix([Byte.dash]) { return nil }
                if shellCFlag.fullMatches(word), k + 1 < rest.count { return rest[k + 1] }
            }
            return nil
        }
        if powershells.contains(p) {
            for (k, word) in rest.enumerated() {
                let folded = word.lowercasedASCII()
                if folded == Bytes(utf8: "-command") || folded == Bytes(utf8: "-c"), k + 1 < rest.count {
                    return Bytes.joined(Array(rest[(k + 1)...]), separator: [Byte.space])
                }
            }
            return nil
        }
        if p == Bytes(utf8: "cmd") {
            for (k, word) in rest.enumerated() where word.lowercasedASCII() == Bytes(utf8: "/c") && k + 1 < rest.count {
                return Bytes.joined(Array(rest[(k + 1)...]), separator: [Byte.space])
            }
        }
        return nil
    }

    // MARK: Runners (3.4)

    private static let pyValue = OptionList(["-W", "-X", "--check-hash-based-pycs"])
    private static let uvCondaValue = OptionList([
        "--with", "--with-requirements", "--with-editable", "--python", "-p", "--extra", "--group", "--only-group",
        "--package", "--directory", "--project", "--env-file", "--index", "--index-url", "--default-index",
        "--extra-index-url", "-C", "--config-setting", "--no-group", "--script", "--gui-script",
        "-n", "--name", "-p", "--prefix", "--cwd",
    ])
    private static let npxValue = OptionList(["-p", "--package", "-c", "--call", "--cache", "--userconfig"])
    private static let xcrunValue = OptionList(["--sdk", "-sdk", "--toolchain", "-toolchain"])
    private static let coverageValue = OptionList([
        "--source", "--rcfile", "--data-file", "--concurrency", "--include", "--omit", "--context",
    ])
    private static let npxRunners = TaskText.words(["npx", "pnpx", "bunx"])
    private static let packageManagers = TaskText.words(["npm", "pnpm", "yarn", "bun"])
    private static let execWords = TaskText.words(["exec", "dlx", "x"])
    private static let envRunners = TaskText.words([
        "uv", "poetry", "pipenv", "pdm", "rye", "pixi", "conda", "mamba", "micromamba", "hatch",
    ])
    private static let phpTools = TaskText.words(["phpunit", "pest", "phpstan", "psalm"])

    /// The index of the first word that is `-m` or not an option, skipping option values.
    static func skipUntilModule(_ words: [Bytes], values: OptionList) -> Int {
        var k = 0
        while k < words.count, words[k].hasPrefix([Byte.dash]), words[k] != Bytes(utf8: "-m"), words[k] != [Byte.dash] {
            k += values.takesValue(words[k]) ? 2 : 1
        }
        return k
    }

    private static func tail(_ words: [Bytes], from index: Int) -> [Bytes] {
        index >= words.count ? [] : Array(words[index...])
    }

    /// Runner wrappers removed until a program remains (TASK-PARSERS 3.4).
    static func unwrap(_ input: [Bytes]) -> [Bytes] {
        var words = input
        for _ in 0..<8 {
            guard let first = words.first else { return words }
            let p = program(first)
            let rest = Array(words.dropFirst())
            if npxRunners.contains(p) {
                words = tail(rest, from: CommandOptions.dropOptions(rest, from: 0, values: npxValue))
                continue
            }
            if packageManagers.contains(p), let sub = rest.first, execWords.contains(sub) {
                let after = Array(rest.dropFirst())
                words = tail(after, from: CommandOptions.dropOptions(after, from: 0, values: npxValue))
                continue
            }
            if p == Bytes(utf8: "mise"), let sub = rest.first, sub == Bytes(utf8: "exec") || sub == Bytes(utf8: "x") {
                let k = rest.firstIndex(of: Bytes(utf8: "--")).map { $0 + 1 } ?? 1
                words = tail(rest, from: k)
                continue
            }
            if envRunners.contains(p), rest.first == Bytes(utf8: "run") {
                let after = Array(rest.dropFirst())
                words = tail(after, from: CommandOptions.dropOptions(after, from: 0, values: uvCondaValue))
                continue
            }
            if p == Bytes(utf8: "python") || (p == Bytes(utf8: "coverage") && rest.first == Bytes(utf8: "run")) {
                let opts = p == Bytes(utf8: "python") ? rest : Array(rest.dropFirst())
                let k = skipUntilModule(opts, values: p == Bytes(utf8: "python") ? pyValue : coverageValue)
                if k + 1 < opts.count, opts[k] == Bytes(utf8: "-m") {
                    words = Array(opts[(k + 1)...])
                    continue
                }
                return words
            }
            if p == Bytes(utf8: "bundle"), rest.first == Bytes(utf8: "exec") {
                words = Array(rest.dropFirst())
                continue
            }
            if p == Bytes(utf8: "xcrun") {
                words = tail(rest, from: CommandOptions.dropOptions(rest, from: 0, values: xcrunValue))
                continue
            }
            if p == Bytes(utf8: "dotnet"), rest.count >= 2, rest[0] == Bytes(utf8: "tool"), rest[1] == Bytes(utf8: "run") {
                words = Array(rest.dropFirst(2))
                continue
            }
            if p == Bytes(utf8: "php"), let script = rest.first, !script.hasPrefix([Byte.dash]),
               phpTools.contains(program(script)) {
                words = rest
                continue
            }
            return words
        }
        return words
    }
}

/// Paths relative to the agent's working directory (TASK-PARSERS 3.3, *cwd-relative*).
enum TaskPaths {
    /// `cwd` with `/` separators and no trailing `/`; nil when absent or the root.
    static func normalizedCwd(_ cwd: String?) -> Bytes? {
        guard let cwd else { return nil }
        let c = Bytes(utf8: cwd).replacing(byte: Byte.backslash, with: Byte.slash).trimmedRight(Byte.slash)
        return c.isEmpty ? nil : c
    }

    /// `[A-Za-z]:/` at the start.
    static func startsWithDrive(_ path: Bytes) -> Bool {
        path.count >= 3 && Byte.isLetter(path[0]) && path[1] == Byte.colon && path[2] == Byte.slash
    }

    /// A drive cwd (`C:` or `C:/…`) compares ASCII case-insensitively.
    static func isDrive(_ cwd: Bytes) -> Bool {
        startsWithDrive(cwd + [Byte.slash])
    }

    /// (the path below cwd, true) when the `/`-separated path is cwd or inside it; else (path, false).
    static func cwdRelative(_ path: Bytes, cwd: String?) -> (Bytes, Bool) {
        guard let c = normalizedCwd(cwd) else { return (path, false) }
        let drive = isDrive(c)
        let a = drive ? path.lowercasedASCII() : path
        let b = drive ? c.lowercasedASCII() : c
        if a == b { return ([], true) }
        if a.count > b.count, a[b.count] == Byte.slash, a.hasPrefix(b) {
            return (Bytes(path[(c.count + 1)...]), true)
        }
        return (path, false)
    }
}
