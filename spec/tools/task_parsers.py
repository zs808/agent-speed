#!/usr/bin/env python3
"""Reference implementation of the task-score parsers (spec/TASK-PARSERS.md).

  python3 spec/tools/task_parsers.py           check spec/fixtures/task-parser-cases.json
  python3 spec/tools/task_parsers.py --write   regenerate it from the corpus (refuses when a hand expectation fails)

Six pure functions turn one tool call and its result into what the task score (METRICS 6.9) keeps: `action_kind`,
`command_info`, `outcome`, `failures`, `edit_hunks` and `continuation` / `running_session`. Inputs are read in memory
and never kept; the outputs hold only classes, flags, fixed-vocabulary suite names, 16-hex digests, counts, exit codes
and times. Every regular expression is written with explicit ASCII classes so the Swift and C# ports match it byte for
byte; `\\s`, `\\d`, `\\w` and `\\b` are never used.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys

sys.dont_write_bytecode = True

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fixtures", "task-parser-cases.json")


LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")


def scalars(text):
    """Text with every unpaired surrogate (possible only through JSON escapes) replaced by U+FFFD."""
    return LONE_SURROGATE.sub("\ufffd", text)


def sha16(text):
    return hashlib.sha256(scalars(text).encode("utf-8")).hexdigest()[:16]


EMPTY = sha16("")


def rx(pattern):
    return re.compile(pattern)


WS = " \t"                                   # trimming removes ASCII spaces and tabs only
ASCII_ALNUM = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
JSON_WS = " \t\r\n"
ASCII_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


# ============================================================================ sources and tool tables (1, 2)

DRIVE = rx(r"[A-Za-z]:/")
MAX_WRAPPER_DEPTH = 4
MAX_NUMBER = 999_999_999_999_999_999         # numbers read from text are capped at 18 digits


def norm_cwd(cwd):
    """The working directory with `/` separators and no trailing `/`; None when absent, not a string or the root."""
    if not isinstance(cwd, str):
        return None
    c = cwd.replace("\\", "/").rstrip("/")
    return c or None


def cwd_relative(path, cwd):
    """(path below cwd, True) when the `/`-separated path is cwd or inside it, else (path, False). A drive cwd
    compares ASCII case-insensitively."""
    c = norm_cwd(cwd)
    if c is None:
        return path, False
    a, b = (path.translate(ASCII_LOWER), c.translate(ASCII_LOWER)) if DRIVE.match(c + "/") else (path, c)
    if a == b:
        return "", True
    if a.startswith(b + "/"):
        return path[len(c) + 1:], True
    return path, False


def num(text):
    """A decimal number read from text, capped at MAX_NUMBER (and -MAX_NUMBER)."""
    sign = -1 if text.startswith("-") else 1
    digits = text.lstrip("+-").lstrip("0") or "0"
    return sign * (MAX_NUMBER if len(digits) > 18 else int(digits))


FAMILY = {"claudeCode": "claude", "claudeDesktop": "claude", "codex": "codex", "omp": "omp", "pi": "pi",
          "openclaw": "openclaw", "droid": "droid"}

TOOL_KINDS = {
    "claude": {
        "read": ("Read", "NotebookRead", "LS"),
        "search": ("Grep", "Glob", "LSP"),
        "edit": ("Edit", "MultiEdit", "Write", "NotebookEdit"),
        "command": ("Bash", "PowerShell"),
        "delegate": ("Task", "Agent", "SendMessage"),
        "web": ("WebFetch", "WebSearch"),
    },
    "codex": {
        "read": ("read_file", "list_dir", "view_image"),
        "search": ("grep_files",),
        "edit": ("apply_patch",),
        "command": ("shell", "container.exec", "local_shell", "shell_command", "exec_command", "exec"),
        "delegate": ("spawn_agent", "send_input", "resume_agent"),
        "web": ("web_search", "web_fetch"),
    },
    "omp": {
        "read": ("read",),
        "search": ("grep", "search", "find", "glob", "ast_grep", "lsp"),
        "edit": ("edit", "write", "ast_edit"),
        "command": ("bash",),
        "delegate": ("task",),
        "web": ("web_search", "fetch", "browser"),
    },
    "pi": {
        "read": ("read", "ls"),
        "search": ("grep", "find"),
        "edit": ("edit", "write"),
        "command": ("bash", "powershell"),
        "delegate": (),
        "web": (),
    },
    "openclaw": {
        "read": ("read", "ls"),
        "search": (),
        "edit": ("edit", "write", "apply_patch"),
        "command": ("exec", "bash"),
        "delegate": ("sessions_spawn", "sessions_send"),
        "web": ("web_search", "web_fetch", "x_search", "browser"),
    },
    "droid": {
        "read": ("Read", "LS"),
        "search": ("Grep", "Glob"),
        "edit": ("Create", "Edit", "MultiEdit"),
        "command": ("Execute",),
        "delegate": ("Task",),
        "web": ("WebSearch", "FetchUrl"),
    },
}
KIND_OF = {fam: {name: kind for kind, names in table.items() for name in names} for fam, table in TOOL_KINDS.items()}
CASE_FOLDED = ("omp", "pi", "openclaw")


def tool_name(family, tool):
    return tool.translate(ASCII_LOWER) if family in CASE_FOLDED else tool


def decode_args(value):
    """The input as an object: an object as is, a string holding a JSON object parsed, anything else None."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.lstrip(JSON_WS).startswith("{"):
        parsed = parse_json(value)
        return parsed if isinstance(parsed, dict) else None
    return None


ARGV_TOOLS = ("shell", "container.exec", "local_shell")
SAFE_ARG = rx(r"[A-Za-z0-9_@%+=:,./-]+")


def quote_arg(arg):
    if SAFE_ARG.fullmatch(arg):
        return arg
    return "'" + arg.replace("'", "'\"'\"'") + "'"


def command_text(source, tool, value):
    """The command line a shell tool runs, or None (3.1)."""
    family = FAMILY.get(source)
    if family is None or not isinstance(tool, str):
        return None
    name = tool_name(family, tool)
    if KIND_OF[family].get(name) != "command":
        return None
    args = decode_args(value)
    if family == "codex" and name == "exec":
        if args is not None:
            return next((args[f] for f in ("cmd", "command") if isinstance(args.get(f), str)), None)
        return value if isinstance(value, str) else None
    if args is None:
        return None
    field = "cmd" if (family == "codex" and name == "exec_command") else "command"
    command = args.get(field)
    if isinstance(command, str):
        return command
    if family == "codex" and name in ARGV_TOOLS and isinstance(command, list) and command \
            and all(isinstance(a, str) for a in command):
        return " ".join(quote_arg(a) for a in command)
    return None


def action_kind(source, tool, value, cwd=None):
    family = FAMILY.get(source)
    if family is None or not isinstance(tool, str):
        return "other"
    kind = KIND_OF[family].get(tool_name(family, tool), "other")
    if kind != "command":
        return kind
    text = command_text(source, tool, value)
    return "command" if text is None else shell_kind(parse_command(text, cwd))


# ============================================================================ shell tokenizer (3.2)

REDIR = rx(r"([0-9]*)(<<<|<<-|<<|>>|>&|<&|>\||<>|&>>|&>|>|<)")
NAME_ASSIGN = rx(r"[A-Za-z_][A-Za-z0-9_]*=")


def read_heredoc(text, i, delim, strip_tabs):
    lines = []
    n = len(text)
    while i < n:
        end = text.find("\n", i)
        line = text[i:] if end < 0 else text[i:end]
        i = n if end < 0 else end + 1
        if (line.lstrip("\t") if strip_tabs else line) == delim:
            return "".join(x + "\n" for x in lines), i
        lines.append(line)
    return "".join(x + "\n" for x in lines), n


def read_word(text, i):
    """One shell word from i: (value, next index). Quotes and escapes are removed; $(...), `...` and ${...} are
    kept verbatim."""
    n = len(text)
    out = []
    while i < n:
        c = text[i]
        if c in " \t\n;&|()<>":
            break
        if c == "\\":
            if i + 1 < n:
                if text[i + 1] != "\n":
                    out.append(text[i + 1])
                i += 2
            else:
                i += 1
            continue
        if c == "'":
            end = text.find("'", i + 1)
            end = n if end < 0 else end
            out.append(text[i + 1:end])
            i = end + 1
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "'":
            j = i + 2
            while j < n and text[j] != "'":
                if text[j] == "\\" and j + 1 < n:
                    out.append(text[j + 1])
                    j += 2
                    continue
                out.append(text[j])
                j += 1
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n and text[j + 1] in '"\\$`\n':
                    if text[j + 1] != "\n":
                        out.append(text[j + 1])
                    j += 2
                    continue
                out.append(text[j])
                j += 1
            i = j + 1
            continue
        if c == "$" and i + 1 < n and text[i + 1] in "({":
            close = ")" if text[i + 1] == "(" else "}"
            depth, j = 0, i + 1
            while j < n:
                if text[j] == text[i + 1]:
                    depth += 1
                elif text[j] == close:
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if c == "`":
            end = text.find("`", i + 1)
            end = n if end < 0 else end
            out.append(text[i:end + 1])
            i = end + 1
            continue
        out.append(c)
        i += 1
    return "".join(out), i


def tokenize(text):
    """Tokens: ("word", text), ("op", op), ("redir", op, target) and ("heredoc", body)."""
    items = []
    pending = []                     # (delimiter, strip tabs, index in items)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t":
            i += 1
            continue
        if c == "\\" and i + 1 < n and text[i + 1] == "\n":
            i += 2
            continue
        if c == "\n":
            items.append(("op", ";"))
            i += 1
            for delim, strip, index in pending:
                body, i = read_heredoc(text, i, delim, strip)
                items[index] = ("heredoc", body)
            pending = []
            continue
        if c == "#":
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        two = text[i:i + 2]
        if two in ("&&", "||", "|&", ";;"):
            items.append(("op", {"|&": "|", ";;": ";"}.get(two, two)))
            i += 2
            continue
        m = REDIR.match(text, i)
        if m:
            op = m.group(2)
            i = m.end()
            while i < n and text[i] in " \t":
                i += 1
            if op in ("<<", "<<-"):
                word, i = read_word(text, i)
                items.append(("heredoc", ""))
                pending.append((word, op == "<<-", len(items) - 1))
                continue
            word, i = read_word(text, i)
            items.append(("redir", op, word))
            continue
        if c in ";&|()":
            items.append(("op", c))
            i += 1
            continue
        word, i = read_word(text, i)
        items.append(("word", word))
    for delim, strip, index in pending:
        items[index] = ("heredoc", "")
    return items


# ============================================================================ segments, prefixes, runners (3.3, 3.4)

class Segment:
    __slots__ = ("words", "redirs", "heredocs", "ops", "op_before", "op_after", "background", "ctx")

    def __init__(self):
        self.words, self.redirs, self.heredocs, self.ops = [], [], [], []
        self.op_before = self.op_after = None
        self.background = False
        self.ctx = ""


SHELLS = ("bash", "sh", "zsh", "dash", "ksh", "fish")
POWERSHELLS = ("pwsh", "powershell")
LEADING_KEYWORDS = ("!", "if", "then", "else", "elif", "do", "while", "until", "{")
CLOSING_KEYWORDS = ("fi", "done", "esac", "}")
CONSTRUCTS = ("for", "case", "select", "function")
NEUTRAL = ("cd", "pushd", "popd", "export", "set", "unset", "source", ".", "true", ":", "echo", "printf", "sleep",
           "clear", "alias", "shopt", "ulimit", "umask", "wait", "trap", "exit", "return", "local", "declare",
           "typeset", "read", "false")
SHELL_C_FLAG = rx(r"-[A-Za-z]*c[A-Za-z]*")


def prog(word):
    """Normalised program name: basename, lower case, Windows extensions removed, aliases folded (3.4)."""
    base = re.split(r"[/\\]", word)[-1].translate(ASCII_LOWER)
    for ext in (".exe", ".cmd", ".bat", ".ps1"):
        if base.endswith(ext):
            base = base[:-len(ext)]
            break
    if re.fullmatch(r"python[0-9.]*|py|pypy[0-9.]*", base):
        return "python"
    if re.fullmatch(r"(gcc|g\+\+|cc|c\+\+|clang|clang\+\+)(-[0-9.]+)?", base):
        return "cc"
    return {"gradlew": "gradle", "mvnw": "mvn", "py.test": "pytest", "nodejs": "node", "gmake": "make",
            "bazelisk": "bazel", "msbuild": "msbuild", "pnpx": "pnpx"}.get(base, base)


def split_segments(items):
    """Segments with `ops`, the operators read since the previous segment; and the operators after the last one."""
    segments, current, ops = [], Segment(), []
    for item in items:
        kind = item[0]
        if kind == "word":
            current.words.append(item[1])
        elif kind == "redir":
            current.redirs.append((item[1], item[2]))
        elif kind == "heredoc":
            current.heredocs.append(item[1])
        else:
            if current.words or current.heredocs:
                current.ops = ops
                segments.append(current)
                ops = []
            current = Segment()
            ops.append(item[1])
    if current.words or current.heredocs:
        current.ops = ops
        segments.append(current)
        ops = []
    return segments, ops


def drop_options(words, i, with_value=(), stop_dashdash=True):
    """Skips option words from i; options in with_value consume the next word (unless written with '=')."""
    while i < len(words):
        w = words[i]
        if w == "--" and stop_dashdash:
            return i + 1
        if not w.startswith("-") or w == "-":
            return i
        i += 2 if (w in with_value) else 1
    return i


NICE_VALUE = ("-n", "--adjustment")
STDBUF_VALUE = ("-i", "-o", "-e")
CAFFEINATE_VALUE = ("-t", "-w")
XVFB_VALUE = ("-s", "--server-args", "-n", "--server-num", "-e", "--error-file", "-f", "--auth-file", "-p",
              "--xauth-protocol")
TIMEOUT_VALUE = ("-s", "--signal", "-k", "--kill-after")
UNITTEST_VALUE = ("-s", "-t", "-p", "-k", "--start-directory", "--top-level-directory", "--pattern")
SWIFT_TEST_VALUE = ("--filter", "--skip", "-s", "--specifier", "-c", "--configuration", "--package-path",
                    "--scratch-path", "-j", "--jobs", "--num-workers", "--xunit-output", "--test-product", "--sanitize",
                    "--triple", "--sdk")
SWIFT_TEST_FILTERS = ("--filter", "--skip", "-s", "--specifier")
SWIFT_BUILD_NARROW = ("--target", "--product")
SWIFT_BUILD_VALUE = ("--target", "--product", "-c", "--configuration", "--package-path", "--scratch-path", "-j",
                     "--jobs", "--triple", "--sdk", "--arch")
DOTNET_VALUE = ("--filter", "-c", "--configuration", "-f", "--framework", "-r", "--runtime", "-o", "--output", "-v",
                "--verbosity", "-s", "--settings", "-l", "--logger", "--results-directory", "-a", "--test-adapter-path",
                "--collect", "-p", "--property")
CMAKE_VALUE = ("--build", "--target", "-t", "-j", "--parallel", "--config")
TSC_NOT_CHECK = ("--watch", "-w", "--version", "-v", "--init", "--help", "-h", "--showConfig")
TSC_VALUE = ("-p", "--project", "--outDir", "--target", "--module", "--lib", "--jsx", "--moduleResolution", "--rootDir",
             "--declarationDir", "--tsBuildInfoFile")
BAZEL_VALUE = ("--config", "--test_filter", "--test_output", "--jobs", "-j")
MESON_VALUE = ("-C", "--suite", "--no-suite", "--num-processes", "--repeat", "--timeout-multiplier", "--setup",
               "--wrapper")
DJANGO_VALUE = ("-k", "--settings", "--parallel", "--tag", "--exclude-tag", "--pattern", "-p", "-v", "--verbosity")
NODE_TEST_VALUE = ("--test-name-pattern", "--test-reporter", "--test-reporter-destination", "--test-concurrency",
                   "--require", "-r", "--import", "--loader")
BUN_TEST_VALUE = ("-t", "--test-name-pattern", "--timeout", "--preload", "--coverage-reporter", "--reporter",
                  "--rerun-each")
PM_NARROW = ("-w", "--workspace", "--filter", "-F")
PM_VALUE = ("-w", "--workspace", "--filter", "-F", "--prefix", "-C", "--dir", "--cwd")
NX_VALUE = ("-t", "--target", "--targets", "-p", "--projects")
DENO_TEST_VALUE = ("--filter", "--allow-read", "--allow-net", "--allow-env", "--config", "-c", "--reporter",
                   "--coverage", "--jobs", "--shuffle", "--seed")
ENV_VALUE = ("-u", "--unset", "-C", "--chdir", "-S", "--split-string")
SUDO_VALUE = ("-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "--user", "--group")


def strip_prefixes(seg):
    """Removes env assignments and prefix commands from a segment's words (3.4)."""
    words = seg.words
    i = 0
    while i < len(words):
        w = words[i]
        if NAME_ASSIGN.match(w):
            i += 1
            continue
        p = prog(w)
        if p == "env":
            i = drop_options(words, i + 1, ENV_VALUE)
            continue
        if p in ("sudo", "doas"):
            i = drop_options(words, i + 1, SUDO_VALUE)
            continue
        if p == "time":
            i = drop_options(words, i + 1)
            continue
        if p == "nice":
            i = drop_options(words, i + 1, NICE_VALUE)
            continue
        if p in ("nohup", "setsid"):
            seg.background = True
            i = drop_options(words, i + 1)
            continue
        if p in ("exec", "command", "builtin", "noglob", "unbuffer", "chronic"):
            i = drop_options(words, i + 1)
            continue
        if p == "stdbuf":
            i = drop_options(words, i + 1, STDBUF_VALUE)
            continue
        if p == "caffeinate":
            i = drop_options(words, i + 1, CAFFEINATE_VALUE)
            continue
        if p == "xvfb-run":
            i = drop_options(words, i + 1, XVFB_VALUE)
            continue
        if p in ("timeout", "gtimeout"):
            i = drop_options(words, i + 1, TIMEOUT_VALUE)
            i += 1                                   # the duration
            continue
        break
    seg.words = words[i:]


def drop_keywords(seg):
    words = seg.words
    while words and words[0] in LEADING_KEYWORDS:
        words = words[1:]
    while words and words[-1] in ("}",):
        words = words[:-1]
    seg.words = words


def shell_script(seg):
    """The script of a shell wrapper (`bash -lc '…'`, `pwsh -Command …`, `cmd /c …`), or None."""
    if not seg.words:
        return None
    p = prog(seg.words[0])
    rest = seg.words[1:]
    if p in SHELLS:
        for k, w in enumerate(rest):
            if not w.startswith("-"):
                return None
            if SHELL_C_FLAG.fullmatch(w) and k + 1 < len(rest):
                return rest[k + 1]
        return None
    if p in POWERSHELLS:
        for k, w in enumerate(rest):
            if w.translate(ASCII_LOWER) in ("-command", "-c") and k + 1 < len(rest):
                return " ".join(rest[k + 1:])
        return None
    if p == "cmd":
        for k, w in enumerate(rest):
            if w.translate(ASCII_LOWER) == "/c" and k + 1 < len(rest):
                return " ".join(rest[k + 1:])
    return None


PY_VALUE = ("-W", "-X", "--check-hash-based-pycs")
UV_VALUE = ("--with", "--with-requirements", "--with-editable", "--python", "-p", "--extra", "--group", "--only-group",
            "--package", "--directory", "--project", "--env-file", "--index", "--index-url", "--default-index",
            "--extra-index-url", "-C", "--config-setting", "--no-group", "--script", "--gui-script")
CONDA_VALUE = ("-n", "--name", "-p", "--prefix", "--cwd")
NPX_VALUE = ("-p", "--package", "-c", "--call", "--cache", "--userconfig")
XCRUN_VALUE = ("--sdk", "-sdk", "--toolchain", "-toolchain")
COVERAGE_VALUE = ("--source", "--rcfile", "--data-file", "--concurrency", "--include", "--omit", "--context")


def skip_until_module(words, value_opts):
    """Index of the first word that is `-m` or not an option, skipping option values."""
    k = 0
    while k < len(words) and words[k].startswith("-") and words[k] not in ("-m", "-"):
        k += 2 if words[k] in value_opts else 1
    return k


def unwrap(words):
    """Removes runner wrappers until a program remains (3.4)."""
    for _ in range(8):
        if not words:
            return words
        p = prog(words[0])
        rest = words[1:]
        if p in ("npx", "pnpx", "bunx"):
            words = rest[drop_options(rest, 0, NPX_VALUE):]
            continue
        if p in ("npm", "pnpm", "yarn", "bun") and rest[:1] and rest[0] in ("exec", "dlx", "x"):
            words = rest[1:][drop_options(rest[1:], 0, NPX_VALUE):]
            continue
        if p == "mise" and rest[:1] and rest[0] in ("exec", "x"):
            k = rest.index("--") + 1 if "--" in rest else 1
            words = rest[k:]
            continue
        if p in ("uv", "poetry", "pipenv", "pdm", "rye", "pixi", "conda", "mamba", "micromamba", "hatch") \
                and rest[:1] == ["run"]:
            words = rest[1:][drop_options(rest[1:], 0, UV_VALUE + CONDA_VALUE):]
            continue
        if p == "python" or (p == "coverage" and rest[:1] == ["run"]):
            opts = rest if p == "python" else rest[1:]
            k = skip_until_module(opts, PY_VALUE if p == "python" else COVERAGE_VALUE)
            if k + 1 < len(opts) and opts[k] == "-m":
                words = opts[k + 1:]
                continue
            return words
        if p == "bundle" and rest[:1] == ["exec"]:
            words = rest[1:]
            continue
        if p == "xcrun":
            words = rest[drop_options(rest, 0, XCRUN_VALUE):]
            continue
        if p == "dotnet" and rest[:2] == ["tool", "run"]:
            words = rest[2:]
            continue
        if p == "php" and rest and not rest[0].startswith("-") and prog(rest[0]) in ("phpunit", "pest", "phpstan",
                                                                                        "psalm"):
            words = rest
            continue
        return words
    return words


# ============================================================================ classification (3.5)

CLASS_RANK = {"other": 0, "check": 1, "build": 2, "test": 3}
COSMETIC = ("-q", "-qq", "-v", "-vv", "-vvv", "--quiet", "--verbose", "--color", "--no-color", "--colors",
            "--no-colors", "--ansi", "--no-ansi", "--nocapture", "--no-capture", "--show-output", "--silent")
COSMETIC_PREFIX = ("--color=", "--colour=", "--colors=", "--tb=")


def has_extension(word):
    """The word ends with `.` and 1 to 10 ASCII letters or digits."""
    dot = word.rfind(".")
    ext = word[dot + 1:]
    return dot >= 0 and 1 <= len(ext) <= 10 and all(ch in ASCII_ALNUM for ch in ext)


def is_broad_path(arg, cwd=None):
    """`.`, `./`, or a directory path of at most one component (3.5)."""
    a, _ = cwd_relative(arg.replace("\\", "/"), cwd)
    while a.startswith("./"):
        a = a[2:]
    a = a.rstrip("/")
    if a in ("", "."):
        return True
    if "*" in a or "?" in a or "::" in a or a.startswith("/") or re.match(r"[A-Za-z]:", a):
        return False
    return "/" not in a and not has_extension(a)


def parse_opts(args, value_opts=(), stop_dashdash=True):
    """(positionals, options as {name: value or True}, words after `--`)."""
    pos, opts, after = [], {}, []
    k = 0
    while k < len(args):
        w = args[k]
        if w == "--" and stop_dashdash:
            after = args[k + 1:]
            break
        if w.startswith("-") and w != "-":
            if "=" in w and w.startswith("--"):
                name, val = w.split("=", 1)
                opts[name] = val
            elif w in value_opts and k + 1 < len(args):
                opts[w] = args[k + 1]
                k += 1
            else:
                matched = next((o for o in value_opts if len(o) == 2 and w.startswith(o) and len(w) > 2), None)
                if matched:
                    opts[matched] = w[2:]
                else:
                    opts[w] = True
        else:
            pos.append(w)
        k += 1
    return pos, opts, after


def any_opt(opts, names):
    return any(n in opts for n in names)


def path_targeted(pos, cwd):
    return any(not is_broad_path(p, cwd) for p in pos)


def name_targeted(pos, cwd):
    """Runners whose positionals are name or path patterns: any positional but `.` or cwd narrows the run (3.5)."""
    for p in pos:
        a, inside = cwd_relative(p.replace("\\", "/").rstrip("/"), cwd)
        if a not in ("", ".") and not (inside and a == ""):
            return True
    return False


RESULT_OTHER = ("other", None, False)

PYTEST_VALUE = ("-k", "-m", "-p", "-c", "-o", "-n", "-W", "-r", "--maxfail", "--tb", "--rootdir", "--basetemp",
                "--junitxml", "--junit-xml", "--durations", "--deselect", "--ignore", "--ignore-glob", "--log-level",
                "--log-cli-level", "--timeout", "--confcutdir", "--import-mode", "--dist", "--cov", "--cov-report",
                "--cov-config", "--cov-fail-under", "--reruns", "--reruns-delay", "--count", "-p", "--override-ini",
                "--capture", "--html", "--randomly-seed")
PYTEST_FILTERS = ("-k", "-m", "--lf", "--last-failed", "--sw", "--stepwise", "--deselect", "--stepwise-skip")


def c_pytest(args, cwd):
    pos, opts, after = parse_opts(args, PYTEST_VALUE)
    return "test", "pytest", any_opt(opts, PYTEST_FILTERS) or path_targeted(pos + after, cwd)


def c_unittest(args, cwd):
    pos, opts, _ = parse_opts(args, UNITTEST_VALUE)
    pos = [p for p in pos if p != "discover"]
    return "test", "unittest", bool(pos) or "-k" in opts


CARGO_GLOBAL_VALUE = ("--color", "--config", "-Z", "-C", "--manifest-path")
CARGO_VALUE = ("-p", "--package", "--test", "--bin", "--example", "--bench", "-F", "--features", "--target",
               "--target-dir", "--manifest-path", "-j", "--jobs", "--profile", "--exclude", "--color",
               "--message-format", "--config", "-Z", "--timings")
CARGO_NARROW = ("-p", "--package", "--test", "--lib", "--bin", "--bins", "--example", "--examples", "--doc", "--bench",
                "--benches")
LIBTEST_VALUE = ("--skip", "--test-threads", "--format", "--color", "-Z", "--logfile", "--report-time")
NEXTEST_VALUE = ("-E", "--filterset", "--filter-expr", "-p", "--package", "--test", "--bin", "--profile", "-P",
                 "-j", "--test-threads", "--retries", "--partition", "--archive-file", "--cargo-profile", "-F",
                 "--features", "--target-dir", "--manifest-path")


def c_cargo(args, cwd):
    k = 0
    while k < len(args) and (args[k].startswith("-") or args[k].startswith("+")):
        k += 2 if args[k] in CARGO_GLOBAL_VALUE else 1
    if k >= len(args):
        return RESULT_OTHER
    sub, rest = args[k], args[k + 1:]
    if sub in ("test", "t"):
        pos, opts, after = parse_opts(rest, CARGO_VALUE)
        apos, aopts, _ = parse_opts(after, LIBTEST_VALUE, stop_dashdash=False)
        targeted = any_opt(opts, CARGO_NARROW) or bool(pos) or bool(apos) or any_opt(aopts, ("--exact", "--skip"))
        return "test", "cargo test", targeted
    if sub == "nextest" and rest[:1] and rest[0] in ("run", "r"):
        pos, opts, _ = parse_opts(rest[1:], NEXTEST_VALUE)
        return "test", "cargo nextest", bool(pos) or any_opt(opts, ("-E", "--filterset", "--filter-expr", "-p",
                                                                     "--package", "--test", "--lib", "--bin"))
    pos, opts, _ = parse_opts(rest, CARGO_VALUE)
    narrow = any_opt(opts, CARGO_NARROW)
    if sub in ("build", "b", "rustc"):
        return "build", "cargo build", narrow
    if sub in ("check", "c"):
        return "check", "cargo check", narrow
    if sub == "clippy":
        return "check", "cargo clippy", narrow
    if sub == "fmt" and "--check" in rest:
        return "check", "cargo fmt", False
    return RESULT_OTHER


def c_swift(args, cwd):
    if not args:
        return RESULT_OTHER
    sub, rest = args[0], args[1:]
    if sub == "test":
        _, opts, _ = parse_opts(rest, SWIFT_TEST_VALUE)
        return "test", "swift test", any_opt(opts, SWIFT_TEST_FILTERS)
    if sub == "build":
        _, opts, _ = parse_opts(rest, SWIFT_BUILD_VALUE)
        return "build", "swift build", any_opt(opts, SWIFT_BUILD_NARROW)
    return RESULT_OTHER


XCODE_ACTIONS = ("build", "test", "build-for-testing", "test-without-building", "analyze", "archive", "clean",
                 "install", "installsrc", "docbuild")
XCODE_FLAGS = ("-quiet", "-verbose", "-json", "-allowProvisioningUpdates", "-allowProvisioningDeviceRegistration",
               "-skipUnavailableActions", "-hideShellScriptEnvironment", "-retry-tests-on-failure",
               "-skipPackagePluginValidation", "-skipMacroValidation", "-disableAutomaticPackageResolution",
               "-onlyUsePackageVersionsFromResolvedFile", "-showTestPlans", "-dry-run", "-UseModernBuildSystem",
               "-enableCodeCoverage", "-parallel-testing-enabled")
XCODE_INFO = ("-list", "-showBuildSettings", "-version", "-showsdks", "-showdestinations", "-usage", "-help",
              "-resolvePackageDependencies", "-showTestPlans", "-showComponent")


def c_xcodebuild(args, cwd):
    actions, narrow = [], False
    k = 0
    while k < len(args):
        w = args[k]
        if w in XCODE_INFO:
            return RESULT_OTHER
        if w.startswith("-only-testing") or w.startswith("-skip-testing"):
            narrow = True
            k += 1 if ":" in w else 2
            continue
        if w.startswith("-"):
            k += 1 if (w in XCODE_FLAGS or ":" in w or "=" in w) else 2
            continue
        if w in XCODE_ACTIONS:
            actions.append(w)
        k += 1
    if "test" in actions or "test-without-building" in actions:
        return "test", "xcodebuild test", narrow
    if "analyze" in actions:
        return "check", "xcodebuild analyze", False
    if actions and all(a == "clean" for a in actions):
        return RESULT_OTHER
    return "build", "xcodebuild build", False


GO_TEST_VALUE = ("-run", "-skip", "-bench", "-count", "-timeout", "-tags", "-p", "-parallel", "-cpu", "-coverprofile",
                 "-coverpkg", "-covermode", "-o", "-exec", "-benchtime", "-blockprofile", "-cpuprofile",
                 "-memprofile", "-mutexprofile", "-outputdir", "-trace", "-shuffle", "-fuzz", "-fuzztime", "-vet",
                 "-ldflags", "-gcflags", "-mod", "-modfile", "-C", "-asmflags", "-buildmode", "-compiler",
                 "-gccgoflags", "-installsuffix", "-pkgdir", "-toolexec", "-overlay", "-pgo")
GO_BROAD = ("./...", "...", "all", ".")


def go_opts(args):
    """Go flags may use one or two dashes and `=`; normalised to one dash."""
    norm = []
    for w in args:
        if w.startswith("--") and len(w) > 2:
            w = w[1:]
        norm.append(w)
    return parse_opts(norm, GO_TEST_VALUE, stop_dashdash=False)


def c_go(args, cwd):
    if not args:
        return RESULT_OTHER
    sub, rest = args[0], args[1:]
    if "-args" in rest:
        rest = rest[:rest.index("-args")]
    pos, opts, _ = go_opts(rest)
    opts = {k.split("=")[0]: v for k, v in opts.items()}
    narrow = any(p not in GO_BROAD for p in pos)
    if sub == "test":
        return "test", "go test", narrow or any_opt(opts, ("-run", "-skip", "-fuzz"))
    if sub in ("build", "install"):
        return "build", "go build", narrow
    if sub == "vet":
        return "check", "go vet", narrow
    return RESULT_OTHER


def c_dotnet(args, cwd):
    if not args:
        return RESULT_OTHER
    sub, rest = args[0], args[1:]
    _, opts, _ = parse_opts(rest, DOTNET_VALUE)
    if sub == "test":
        return "test", "dotnet test", "--filter" in opts
    if sub in ("build", "publish", "pack", "msbuild"):
        return "build", "dotnet " + sub, False
    if sub == "format" and any_opt(opts, ("--verify-no-changes",)):
        return "check", "dotnet format", False
    return RESULT_OTHER


GRADLE_VALUE = ("-p", "--project-dir", "-b", "--build-file", "-c", "--settings-file", "-x", "--exclude-task",
                "--tests", "--max-workers", "-I", "--init-script", "-g", "--gradle-user-home", "--console",
                "--warning-mode", "--priority", "-D", "-P", "--include-build")


def gradle_task_class(base):
    if base == "test" or re.fullmatch(r"test[A-Z0-9].*", base) or base.endswith("Test") or base.endswith("Tests"):
        return "test"
    if base in ("check", "build"):
        return "test"
    if base in ("assemble", "jar", "classes", "installDist", "bootJar", "shadowJar") or base.startswith("assemble") \
            or base.startswith("compile"):
        return "build"
    if base.startswith("lint") or base in ("detekt", "ktlintCheck", "spotlessCheck") or base.startswith("checkstyle") \
            or base.startswith("pmd") or base.startswith("spotbugs"):
        return "check"
    return "other"


GRADLE_SUITES = {"test": "gradle test", "check": "gradle check", "build": "gradle build", "assemble": "gradle assemble",
                 "lint": "gradle lint"}


def c_gradle(args, cwd):
    pos, opts, _ = parse_opts(args, GRADLE_VALUE)
    excluded = []
    for k, w in enumerate(args):
        if w in ("-x", "--exclude-task") and k + 1 < len(args):
            excluded.append(args[k + 1].split(":")[-1])
        elif w.startswith("--exclude-task="):
            excluded.append(w.split("=", 1)[1].split(":")[-1])
    best = RESULT_OTHER
    for task in pos:
        base = task.split(":")[-1]
        cls = gradle_task_class(base)
        if cls == "test" and base in ("check", "build") and "test" in excluded:
            cls = "build"
        if CLASS_RANK[cls] > CLASS_RANK[best[0]]:
            module = task.strip(":").count(":") > 0
            best = (cls, GRADLE_SUITES.get(base), module or "--tests" in opts)
    return best


MVN_TEST = ("test", "integration-test", "verify", "install", "deploy", "package")
MVN_BUILD = ("compile", "test-compile", "process-classes")
MVN_SKIP = ("-DskipTests", "-DskipTests=true", "-Dmaven.test.skip=true", "-Dmaven.test.skip")
MVN_VALUE = ("-pl", "--projects", "-f", "--file", "-s", "--settings", "-P", "--activate-profiles", "-T", "--threads",
             "-rf", "--resume-from", "-gs", "--global-settings", "-t", "--toolchains", "-l", "--log-file")


def c_mvn(args, cwd):
    pos, opts, _ = parse_opts(args, MVN_VALUE)
    skip = any(w in MVN_SKIP for w in args)
    narrow = any_opt(opts, ("-pl", "--projects")) or any(w.startswith("-Dtest=") or w.startswith("-Dit.test=")
                                                           for w in args)
    best = RESULT_OTHER
    for goal in pos:
        if goal in MVN_TEST or goal == "surefire:test":
            cls = "build" if skip else "test"
        elif goal in MVN_BUILD:
            cls = "build"
        elif goal.endswith(":check"):
            cls = "check"
        else:
            continue
        if CLASS_RANK[cls] > CLASS_RANK[best[0]]:
            suite = "mvn " + goal if goal in MVN_TEST + MVN_BUILD else None
            best = (cls, suite, narrow)
    return best


def c_cc(args, cwd):
    if "-E" in args or "--version" in args or "-v" == (args[0] if args else None):
        return RESULT_OTHER
    if "-fsyntax-only" in args:
        return "check", "cc", False
    return "build", "cc", False


def c_cmake(args, cwd):
    if "--build" in args:
        _, opts, _ = parse_opts(args, CMAKE_VALUE)
        target = opts.get("--target") or opts.get("-t")
        if target in ("test", "check", "RUN_TESTS"):
            return "test", "cmake --build", True
        return "build", "cmake --build", isinstance(target, str)
    return RESULT_OTHER


CTEST_VALUE = ("--test-dir", "-C", "--build-config", "-j", "--parallel", "--timeout", "--output-log", "-R",
               "--tests-regex", "-E", "--exclude-regex", "-L", "--label-regex", "-LE", "--label-exclude", "-I",
               "--tests-information", "--repeat", "--stop-time", "-O", "--resource-spec-file", "--repeat-until-fail")


CTEST_FILTERS = ("-R", "--tests-regex", "-E", "--exclude-regex", "-L", "--label-regex", "-LE", "--label-exclude", "-I",
                 "--tests-information", "--rerun-failed")


def c_ctest(args, cwd):
    _, opts, _ = parse_opts(args, CTEST_VALUE)
    return "test", "ctest", any_opt(opts, CTEST_FILTERS)


JEST_VALUE = ("-c", "--config", "-w", "--maxWorkers", "--testTimeout", "--reporters", "--coverageReporters",
              "--rootDir", "--roots", "--env", "--testEnvironment", "--outputFile", "--seed", "-t",
              "--testNamePattern", "--testPathPattern", "--testPathPatterns", "--testPathIgnorePatterns",
              "--changedSince", "--selectProjects", "--shard", "--coverageDirectory", "--collectCoverageFrom",
              "--json")
JEST_FILTERS = ("-t", "--testNamePattern", "--testPathPattern", "--testPathPatterns", "--findRelatedTests", "-o",
                "--onlyChanged", "--changedSince", "--lastCommit", "--selectProjects", "--shard", "--onlyFailures",
                "-f")


def c_jest(args, cwd):
    pos, opts, after = parse_opts(args, JEST_VALUE)
    return "test", "jest", any_opt(opts, JEST_FILTERS) or name_targeted(pos + after, cwd)


VITEST_VALUE = ("-c", "--config", "--root", "-r", "--dir", "--reporter", "--outputFile", "--environment", "--pool",
                "--project", "--shard", "-t", "--testNamePattern", "--maxWorkers", "--minWorkers", "--testTimeout",
                "--mode", "--exclude")
VITEST_FILTERS = ("-t", "--testNamePattern", "--changed", "--shard", "--project")


def c_vitest(args, cwd):
    sub = args[0] if args and args[0] in ("run", "watch", "dev", "related", "bench", "list", "init", "typecheck") \
        else None
    rest = args[1:] if sub else args
    if sub in ("bench", "list", "init"):
        return RESULT_OTHER
    pos, opts, after = parse_opts(rest, VITEST_VALUE)
    return "test", "vitest", sub == "related" or any_opt(opts, VITEST_FILTERS) or name_targeted(pos + after, cwd)


MOCHA_VALUE = ("-r", "--require", "-R", "--reporter", "-t", "--timeout", "-s", "--slow", "-u", "--ui", "--config",
               "--package", "--extension", "--ignore", "--file", "-g", "--grep", "-f", "--fgrep", "--spec",
               "--retries", "-j", "--jobs", "--reporter-option", "-O")


MOCHA_FILTERS = ("-g", "--grep", "-f", "--fgrep")


def c_mocha(args, cwd):
    pos, opts, after = parse_opts(args, MOCHA_VALUE)
    if isinstance(opts.get("--spec"), str):
        pos.append(opts["--spec"])
    return "test", "mocha", any_opt(opts, MOCHA_FILTERS) or path_targeted(pos + after, cwd)


def c_path_tool(cls, suite, value_opts=(), filters=()):
    def handler(args, cwd):
        pos, opts, after = parse_opts(args, value_opts)
        return cls, suite, any_opt(opts, filters) or path_targeted(pos + after, cwd)
    handler.rule = ("paths", cls, suite, filters, value_opts)
    return handler


MYPY_VALUE = ("--config-file", "-p", "--package", "-m", "--module", "--python-version", "--cache-dir", "--exclude",
              "--follow-imports", "--platform", "--junit-xml", "--python-executable")
PYRIGHT_VALUE = ("-p", "--project", "--pythonpath", "--pythonversion", "--level", "--threads", "--pythonplatform",
                 "-t", "--typeshedpath", "--venvpath")
RUFF_VALUE = ("--select", "--ignore", "--extend-select", "--extend-ignore", "--config", "--output-format",
              "--target-version", "--exclude", "--extend-exclude", "--line-length", "--per-file-ignores",
              "--cache-dir", "--stdin-filename", "--fixable", "--unfixable")
ESLINT_VALUE = ("-c", "--config", "--ext", "-f", "--format", "-o", "--output-file", "--rule", "--parser",
                "--resolve-plugins-relative-to", "--max-warnings", "--ignore-path", "--ignore-pattern",
                "--cache-location", "--env", "--global", "--plugin", "--rulesdir", "--parser-options",
                "--report-unused-disable-directives-severity", "--cache-strategy")
RSPEC_VALUE = ("-e", "--example", "-E", "--example-matches", "-t", "--tag", "-f", "--format", "-o", "--out", "-r",
               "--require", "-I", "--seed", "--order", "-P", "--pattern", "--exclude-pattern", "--default-path")
PHPUNIT_VALUE = ("--filter", "--group", "--exclude-group", "--testsuite", "-c", "--configuration", "--bootstrap",
                 "--log-junit", "--order-by", "--cache-directory", "--coverage-html", "--coverage-clover",
                 "--coverage-text", "--test-suffix", "--exclude-testsuite")


RUFF_SUBCOMMANDS = ("check", "format", "rule", "config", "linter", "clean", "server", "analyze", "version")


def c_ruff(args, cwd):
    sub = args[0] if args and args[0] in RUFF_SUBCOMMANDS else None
    rest = args[1:] if sub else args
    if sub in (None, "check"):
        pos, _, after = parse_opts(rest, RUFF_VALUE)
        return "check", "ruff", path_targeted(pos + after, cwd)
    if sub == "format" and ("--check" in rest or "--diff" in rest):
        pos, _, after = parse_opts(rest, RUFF_VALUE)
        return "check", "ruff format", path_targeted(pos + after, cwd)
    return RESULT_OTHER


def c_check_flag(suite, flags, value_opts=()):
    def handler(args, cwd):
        if not any(a in flags for a in args):
            return RESULT_OTHER
        pos, _, after = parse_opts(args, value_opts)
        return "check", suite, path_targeted(pos + after, cwd)
    handler.rule = ("flag", "check", suite, flags, value_opts)
    return handler


def c_tsc(name):
    def handler(args, cwd):
        if any(a in TSC_NOT_CHECK for a in args):
            return RESULT_OTHER
        pos, _, after = parse_opts(args, TSC_VALUE)
        if "-b" in args or "--build" in args:
            return "check", name, False
        return "check", name, path_targeted(pos + after, cwd)
    return handler


def c_sub(table, value_opts=()):
    """Programs whose first word picks the class: table maps subcommand -> (class, suite, filter options)."""
    def handler(args, cwd):
        if not args or args[0] not in table:
            return RESULT_OTHER
        cls, suite, filters, path_rule = table[args[0]]
        pos, opts, after = parse_opts(args[1:], value_opts)
        return cls, suite, any_opt(opts, filters) or (path_rule and path_targeted(pos + after, cwd))
    handler.rule = ("sub", table, value_opts)
    return handler


def c_bazel(args, cwd):
    if not args or args[0] not in ("test", "build"):
        return RESULT_OTHER
    pos, _, _ = parse_opts(args[1:], BAZEL_VALUE)
    narrow = any(p not in ("//...", "...", "//:all") for p in pos) or any(a.startswith("--test_filter") for a in args)
    return ("test", "bazel test", narrow) if args[0] == "test" else ("build", "bazel build", narrow)


def c_meson(args, cwd):
    if not args:
        return RESULT_OTHER
    if args[0] == "test":
        pos, opts, _ = parse_opts(args[1:], MESON_VALUE)
        return "test", "meson test", bool(pos) or any_opt(opts, ("--suite",))
    if args[0] == "compile":
        return "build", "meson compile", False
    return RESULT_OTHER


def c_python_script(args, cwd):
    """`python manage.py test`, `python setup.py test`; any other script is not a check."""
    args = args[skip_until_module(args, PY_VALUE):]
    if len(args) >= 2 and args[0].replace("\\", "/").split("/")[-1] == "manage.py" and args[1] == "test":
        pos, opts, _ = parse_opts(args[2:], DJANGO_VALUE)
        return "test", "django test", bool(pos) or any_opt(opts, ("-k", "--tag"))
    if len(args) >= 2 and args[0].replace("\\", "/").split("/")[-1] == "setup.py" and args[1] == "test":
        return "test", "setup.py test", False
    return RESULT_OTHER


def c_node(args, cwd):
    if "--test" in args:
        pos, opts, after = parse_opts([a for a in args if a != "--test"], NODE_TEST_VALUE)
        return "test", "node --test", any_opt(opts, ("--test-name-pattern", "--test-only")) or \
            path_targeted(pos + after, cwd)
    return RESULT_OTHER


def c_named(cls, suite):
    def handler(args, cwd):
        return cls, suite, False
    handler.rule = ("named", cls, suite, (), ())
    return handler


def c_posargs(name):
    """tox and nox: arguments after `--` go to the test runner and narrow the run."""
    def handler(args, cwd):
        return "test", name, "--" in args and args.index("--") < len(args) - 1
    handler.rule = ("posargs", "test", name, (), ())
    return handler


def c_swiftlint(args, cwd):
    if (args[:1] and args[0] in ("autocorrect", "analyze")) or "--fix" in args:
        return RESULT_OTHER
    return "check", "swiftlint", False


PROGRAMS = {
    "pytest": c_pytest,
    "unittest": c_unittest,
    "cargo": c_cargo,
    "swift": c_swift,
    "swiftc": c_named("build", "swiftc"),
    "xcodebuild": c_xcodebuild,
    "go": c_go,
    "dotnet": c_dotnet,
    "msbuild": c_named("build", "msbuild"),
    "vstest.console": c_named("test", "vstest"),
    "gradle": c_gradle,
    "mvn": c_mvn,
    "javac": c_named("build", "javac"),
    "kotlinc": c_named("build", "kotlinc"),
    "cc": c_cc,
    "cmake": c_cmake,
    "ctest": c_ctest,
    "jest": c_jest,
    "vitest": c_vitest,
    "mocha": c_mocha,
    "ava": c_path_tool("test", "ava", ("--match", "-m", "--timeout", "-T"), ("--match", "-m")),
    "tap": c_path_tool("test", "tap", ("--grep", "-g", "--reporter", "-R"), ("--grep", "-g")),
    "node": c_node,
    "playwright": c_sub({"test": ("test", "playwright test", ("-g", "--grep", "--last-failed", "--only-changed"),
                                  True)},
                        ("-g", "--grep", "--project", "-c", "--config", "--reporter", "--workers", "-j", "--shard",
                         "--retries", "--timeout", "--grep-invert")),
    "cypress": c_sub({"run": ("test", "cypress run", ("--spec", "-s"), False)},
                     ("--spec", "-s", "--browser", "-b", "--config", "-c", "--env", "-e", "--project", "-P")),
    "tsc": c_tsc("tsc"),
    "vue-tsc": c_tsc("vue-tsc"),
    "eslint": c_path_tool("check", "eslint", ESLINT_VALUE),
    "oxlint": c_path_tool("check", "oxlint", ("-c", "--config", "-A", "-D", "-W", "--format", "-f")),
    "biome": c_sub({"check": ("check", "biome", (), True), "lint": ("check", "biome", (), True),
                    "ci": ("check", "biome", (), True)}, ("--config-path", "--reporter", "--max-diagnostics")),
    "prettier": c_check_flag("prettier", ("--check", "-c", "--list-different", "-l"),
                             ("--config", "--ignore-path", "--plugin", "--parser")),
    "stylelint": c_path_tool("check", "stylelint", ("--config", "-c", "--formatter", "-f", "--ignore-path")),
    "svelte-check": c_named("check", "svelte-check"),
    "astro": c_sub({"check": ("check", "astro check", (), False)}),
    "ruff": c_ruff,
    "mypy": c_path_tool("check", "mypy", MYPY_VALUE),
    "dmypy": c_named("check", "mypy"),
    "pyright": c_path_tool("check", "pyright", PYRIGHT_VALUE),
    "basedpyright": c_path_tool("check", "basedpyright", PYRIGHT_VALUE),
    "flake8": c_path_tool("check", "flake8", ("--config", "--select", "--ignore", "--extend-ignore",
                                              "--max-line-length", "--exclude", "--format")),
    "pylint": c_path_tool("check", "pylint", ("--rcfile", "--disable", "--enable", "-j", "--jobs",
                                              "--output-format", "--load-plugins")),
    "black": c_check_flag("black", ("--check", "--diff"), ("--config", "-l", "--line-length", "-t",
                                                           "--target-version", "--include", "--exclude")),
    "isort": c_check_flag("isort", ("--check", "--check-only", "-c", "--diff"), ("--settings-path", "--sp",
                                                                                  "--profile")),
    "tox": c_posargs("tox"),
    "nox": c_posargs("nox"),
    "rspec": c_path_tool("test", "rspec", RSPEC_VALUE, ("-e", "--example", "-E", "--example-matches", "-t", "--tag",
                                                         "--only-failures", "--next-failure", "-P", "--pattern")),
    "rails": c_sub({"test": ("test", "rails test", ("-n", "--name"), True)}, ("-n", "--name", "-e",
                                                                                "--environment", "--seed")),
    "rubocop": c_path_tool("check", "rubocop", ("-c", "--config", "-f", "--format", "--only", "--except",
                                                "--require", "-r", "-o", "--out")),
    "phpunit": c_path_tool("test", "phpunit", PHPUNIT_VALUE, ("--filter", "--group", "--testsuite")),
    "pest": c_path_tool("test", "pest", PHPUNIT_VALUE, ("--filter", "--group", "--testsuite")),
    "phpstan": c_sub({"analyse": ("check", "phpstan", (), True), "analyze": ("check", "phpstan", (), True)},
                     ("-c", "--configuration", "-l", "--level", "--memory-limit", "--error-format")),
    "psalm": c_path_tool("check", "psalm", ("-c", "--config", "--threads", "--output-format")),
    "mix": c_sub({"test": ("test", "mix test", ("--only", "--failed", "--stale", "--include"), True),
                  "compile": ("build", "mix compile", (), False),
                  "credo": ("check", "mix credo", (), False),
                  "dialyzer": ("check", "mix dialyzer", (), False)},
                 ("--only", "--include", "--exclude", "--seed", "--max-failures", "--partitions")),
    "zig": c_sub({"test": ("test", "zig test", (), False), "build": ("build", "zig build", (), False)}),
    "stack": c_sub({"test": ("test", "stack test", ("--ta", "--test-arguments"), False),
                    "build": ("build", "stack build", (), False)}, ("--ta", "--test-arguments")),
    "cabal": c_sub({"test": ("test", "cabal test", ("--test-options", "--test-option"), False),
                    "build": ("build", "cabal build", (), False)}, ("--test-options", "--test-option")),
    "dart": c_sub({"test": ("test", "dart test", ("-n", "--name", "-N", "--plain-name", "-t", "--tags"), True),
                   "analyze": ("check", "dart analyze", (), False)},
                  ("-n", "--name", "-N", "--plain-name", "-t", "--tags", "-p", "--platform", "-j", "--concurrency",
                   "-r", "--reporter")),
    "flutter": c_sub({"test": ("test", "flutter test", ("-n", "--name", "--plain-name", "-t", "--tags"), True),
                      "analyze": ("check", "flutter analyze", (), False),
                      "build": ("build", "flutter build", (), False)},
                     ("-n", "--name", "--plain-name", "-t", "--tags", "-r", "--reporter", "--flavor")),
    "deno": None,                       # set below (scripts)
    "bun": None,
    "golangci-lint": c_sub({"run": ("check", "golangci-lint", (), True)},
                           ("-c", "--config", "--timeout", "-E", "--enable", "-D", "--disable", "--out-format")),
    "staticcheck": c_path_tool("check", "staticcheck", ("-checks", "-f", "-tags")),
    "swiftlint": c_swiftlint,
    "swift-format": c_sub({"lint": ("check", "swift-format", (), False)}),
    "clang-tidy": c_named("check", "clang-tidy"),
    "cppcheck": c_named("check", "cppcheck"),
    "shellcheck": c_named("check", "shellcheck"),
    "hadolint": c_named("check", "hadolint"),
    "terraform": c_sub({"validate": ("check", "terraform validate", (), False)}),
    "pre-commit": c_sub({"run": ("check", "pre-commit", ("--files",), False)}, ("--files", "--hook-stage",
                                                                                 "--from-ref", "--to-ref")),
    "bazel": c_bazel,
    "meson": c_meson,
    "vite": c_sub({"build": ("build", "vite build", (), False)}),
    "next": c_sub({"build": ("build", "next build", (), False), "lint": ("check", "next lint", (), False)}),
    "nuxt": c_sub({"build": ("build", "nuxt build", (), False)}),
    "ng": c_sub({"build": ("build", "ng build", (), False), "test": ("test", "ng test", ("--include",), False),
                 "lint": ("check", "ng lint", (), False)}, ("--include", "--configuration", "-c", "--project")),
    "webpack": c_named("build", "webpack"),
    "rollup": c_named("build", "rollup"),
    "esbuild": c_named("build", "esbuild"),
    "tsup": c_named("build", "tsup"),
    "rustc": c_named("build", "rustc"),
}

# ---------------------------------------------------------------------------- package scripts and task runners (3.6)

SCRIPT_ALIASES = {"t": "test", "tst": "test", "tests": "test", "type-check": "typecheck", "types": "typecheck",
                  "compile": "build"}
CANON_SCRIPTS = {"test": "test", "build": "build", "lint": "check", "typecheck": "check", "check": "check"}
TEST_WORDS = ("test", "tests", "spec", "specs", "unit", "e2e", "integration", "coverage", "cov", "jest", "vitest",
              "mocha", "pytest", "ctest", "playwright", "cypress")
BUILD_WORDS = ("build", "compile", "bundle", "dist", "all")
CHECK_WORDS = ("lint", "typecheck", "types", "tsc", "check", "eslint", "ruff", "mypy", "pyright", "clippy", "vet",
               "analyze", "analyse", "verify", "validate")
FORWARD_FILTERS = ("-t", "--testNamePattern", "-k", "--grep", "-g", "--filter", "-run", "--run", "--testPathPattern",
                   "--spec", "--tests", "-E", "-R", "--findRelatedTests", "--only-testing", "-only-testing", "--skip",
                   "--exact", "-p", "--package", "-m")
FORWARD_VALUE = ("--reporter", "-r", "--config", "-c", "--maxWorkers", "-w", "--timeout", "--testTimeout", "--env",
                 "--outputFile", "--coverageReporters", "--pool", "--environment", "--seed", "--tb", "-n",
                 "--workers", "-j", "--jobs")
SCRIPT_LABEL = {"npm": ("npm test", "npm run "), "pnpm": ("pnpm test", "pnpm run "), "yarn": ("yarn test",
                                                                                               "yarn run "),
                "bun": ("bun run test", "bun run "), "deno": ("deno task test", "deno task "),
                "composer": ("composer test", "composer "), "make": ("make test", "make "),
                "ninja": ("ninja test", "ninja "), "just": ("just test", "just "), "task": ("task test", "task "),
                "rake": ("rake test", "rake "), "mise": ("mise run test", "mise run "),
                "turbo": ("turbo test", "turbo "), "nx": ("nx test", "nx ")}
MAKE_FILTER_VAR = rx(r"TESTS?|T|TEST_?ARGS|TEST_?FILTER|FILTER|RUN|ONLY|K|PKG|PACKAGE|FILES?|SPEC|ARGS|PYTEST_?ARGS|"
                     r"TESTNAME|CASE|TESTOPTS")


def script_class(runner, name):
    n = name.translate(ASCII_LOWER)
    canon = SCRIPT_ALIASES.get(n, n)
    if runner in ("make", "ninja") and canon == "check":
        return "test", "make check" if runner == "make" else "ninja check"
    if runner in ("make", "ninja") and canon == "all":
        return "build", runner
    if canon in CANON_SCRIPTS:
        test_label, prefix = SCRIPT_LABEL[runner]
        return CANON_SCRIPTS[canon], test_label if canon == "test" else prefix + canon
    first = re.split(r"[:._/-]", n)[0]
    if first in TEST_WORDS:
        return "test", None
    if first in BUILD_WORDS:
        return "build", None
    if first in CHECK_WORDS:
        return "check", None
    return "other", None


def forwarded_targeted(args, cwd):
    words = [a for a in args if a != "--"]
    k = 0
    while k < len(words):
        w = words[k]
        if w in FORWARD_FILTERS or any(w.startswith(f + "=") for f in FORWARD_FILTERS if f.startswith("--")):
            return True
        if w.startswith("-"):
            k += 2 if w in FORWARD_VALUE else 1
            continue
        if name_targeted([w], cwd):
            return True
        k += 1
    return False


NPM_BUILTINS = ("install", "i", "ci", "add", "uninstall", "remove", "rm", "update", "up", "outdated", "ls", "list",
                "link", "publish", "pack", "init", "create", "audit", "config", "cache", "version", "view", "info",
                "why", "explain", "dedupe", "prune", "rebuild", "start", "restart", "stop", "store", "fetch", "patch",
                "import", "deploy", "licenses", "doctor", "setup", "env", "approve-builds", "self-update",
                "workspaces", "workspace", "dlx", "exec", "x", "set", "plugin", "constraints", "unplug", "stage",
                "npm", "node", "upgrade", "pm", "repl", "help", "login", "logout", "whoami", "search", "owner",
                "access", "token", "team", "org", "profile", "star", "stars", "unstar", "fund", "sbom", "query",
                "run-script", "run", "rum", "urn", "test", "t", "tst", "build", "global")


def c_package_manager(runner):
    def handler(args, cwd):
        k = 0
        narrow = False
        while k < len(args) and args[k].startswith("-"):
            w = args[k]
            if w in PM_VALUE:
                narrow = narrow or w in PM_NARROW
                k += 2
                continue
            if w.startswith("--workspace=") or w.startswith("--filter="):
                narrow = True
            k += 1
        rest = args[k:]
        if not rest:
            return RESULT_OTHER
        sub = rest[0]
        if runner == "yarn" and sub == "workspace" and len(rest) >= 3:
            cls, suite, _ = handler(rest[2:], cwd)
            return cls, suite, True
        if runner == "bun" and sub == "test":
            pos, opts, after = parse_opts(rest[1:], BUN_TEST_VALUE)
            return "test", "bun test", any_opt(opts, ("-t", "--test-name-pattern")) or name_targeted(pos + after,
                                                                                                       cwd)
        if runner == "bun" and sub == "build":
            return "build", "bun build", False
        before = rest[1:rest.index("--")] if "--" in rest else rest[1:]
        if any(w in PM_NARROW or w.startswith("--workspace=") or w.startswith("--filter=") for w in before):
            narrow = True
            rest = [w for k, w in enumerate(rest) if not (
                w in PM_NARROW or (k > 0 and rest[k - 1] in PM_NARROW and k <= len(before))
                or w.startswith("--workspace=") or w.startswith("--filter="))]
        if sub in ("test", "t", "tst"):
            name, fwd = "test", rest[1:]
        elif sub in ("run", "run-script", "rum", "urn"):
            if len(rest) < 2:
                return RESULT_OTHER
            j = drop_options(rest[1:], 0, PM_NARROW, stop_dashdash=False) + 1
            if j >= len(rest):
                return RESULT_OTHER
            name, fwd = rest[j], rest[j + 1:]
        elif runner in ("pnpm", "yarn", "bun") and sub not in NPM_BUILTINS and not has_extension(sub):
            if prog(sub) in PROGRAMS and prog(sub) not in SCRIPT_LABEL:
                return classify(rest, cwd)
            name, fwd = sub, rest[1:]
        else:
            return RESULT_OTHER
        cls, suite = script_class(runner, name)
        if cls == "other":
            return RESULT_OTHER
        return cls, suite, narrow or forwarded_targeted(fwd, cwd)
    return handler


RUNNER_SUBCOMMANDS = {"mise": ("run", "r"), "deno": ("task",), "turbo": ("run",), "composer": ("run-script", "run")}
COMPOSER_BUILTINS = ("install", "update", "require", "remove", "dump-autoload", "dumpautoload", "show", "outdated",
                     "validate", "create-project", "init", "config", "global", "exec", "search", "why", "why-not",
                     "licenses", "audit", "status", "archive", "diagnose", "self-update", "depends", "prohibits",
                     "fund", "reinstall", "bump", "check-platform-reqs", "clear-cache", "browse", "suggests")


def c_runner(runner, value_opts, default=None):
    """make, ninja, just, task, rake, mise run, turbo, nx, composer, deno task: target names map like scripts."""
    def handler(args, cwd):
        rest = args
        if rest[:1] and rest[0] in RUNNER_SUBCOMMANDS.get(runner, ()):
            rest = rest[1:]
        if runner == "nx" and rest[:1] and rest[0] in ("run-many", "affected"):
            _, opts, _ = parse_opts(rest[1:], NX_VALUE)
            target = opts.get("-t") or opts.get("--target") or opts.get("--targets")
            if not isinstance(target, str):
                return RESULT_OTHER
            cls, suite = script_class(runner, target.split(",")[0])
            return (cls, suite, isinstance(opts.get("-p") or opts.get("--projects"), str)) if cls != "other" \
                else RESULT_OTHER
        if runner == "nx" and rest[:1] == ["run"] and len(rest) >= 2 and ":" in rest[1]:
            cls, suite = script_class(runner, rest[1].split(":")[1])
            return (cls, suite, True) if cls != "other" else RESULT_OTHER
        pos, assigns, k = [], [], 0
        while k < len(rest):
            w = rest[k]
            if w.startswith("-"):
                if w in value_opts:
                    k += 3 if (runner == "just" and w == "--set") else 2
                    continue
                k += 1
                continue
            if NAME_ASSIGN.match(w):
                assigns.append(w.split("=", 1)[0])
            else:
                pos.append(w)
            k += 1
        if runner == "composer" and pos and pos[0] in COMPOSER_BUILTINS:
            return RESULT_OTHER
        if not pos:
            if default is None:
                return RESULT_OTHER
            return default[0], default[1], False
        if runner in ("make", "ninja", "rake"):
            best = None
            for target in pos:
                cls, suite = script_class(runner, target)
                if best is None or CLASS_RANK[cls] > CLASS_RANK[best[0]]:
                    best = (cls, suite)
            cls, suite = best
            narrow = any(MAKE_FILTER_VAR.fullmatch(a.translate(ASCII_UPPER)) for a in assigns)
        else:
            cls, suite = script_class(runner, pos[0])
            narrow = len(pos) > 1 and (runner == "just" or forwarded_targeted(pos[1:], cwd))
            if runner == "nx" and len(pos) > 1:
                narrow = True
        return (cls, suite, narrow) if cls != "other" else RESULT_OTHER
    handler.rule = ("runner", runner, value_opts, default)
    return handler


PROGRAMS.update({
    "npm": c_package_manager("npm"),
    "pnpm": c_package_manager("pnpm"),
    "yarn": c_package_manager("yarn"),
    "bun": c_package_manager("bun"),
    "make": c_runner("make", ("-C", "--directory", "-f", "--file", "--makefile", "-I", "--include-dir", "-o",
                              "--old-file", "-W", "--what-if", "-j", "-l"), ("build", "make")),
    "ninja": c_runner("ninja", ("-C", "-f", "-j", "-k", "-l", "-d", "-t", "-w"), ("build", "ninja")),
    "just": c_runner("just", ("-f", "--justfile", "-d", "--working-directory", "--set", "--dotenv-path",
                              "--dotenv-filename", "--shell", "--shell-arg", "--chooser", "--color", "--command",
                              "--list-heading", "--list-prefix")),
    "task": c_runner("task", ("-d", "--dir", "-t", "--taskfile", "-o", "--output", "-c", "--color")),
    "rake": c_runner("rake", ("-f", "--rakefile", "-I", "--libdir", "-r", "--require", "-C", "--directory")),
    "mise": c_runner("mise", ("-C", "--cd", "-E", "--env", "-j", "--jobs")),
    "turbo": c_runner("turbo", ("--filter", "-F", "--concurrency", "--output-logs", "--cache-dir")),
    "nx": c_runner("nx", ("-c", "--configuration", "--parallel", "--output-style")),
    "composer": c_runner("composer", ("-d", "--working-dir")),
})


def c_deno(args, cwd):
    if not args:
        return RESULT_OTHER
    sub, rest = args[0], args[1:]
    if sub == "test":
        pos, opts, after = parse_opts(rest, DENO_TEST_VALUE)
        return "test", "deno test", "--filter" in opts or path_targeted(pos + after, cwd)
    if sub == "check":
        return "check", "deno check", False
    if sub == "lint":
        return "check", "deno lint", False
    if sub == "fmt" and "--check" in rest:
        return "check", "deno fmt", False
    if sub == "task":
        return c_runner("deno", ())(args, cwd)
    return RESULT_OTHER


PROGRAMS["deno"] = c_deno


CUSTOM_SUITES = ("pytest", "unittest", "django test", "setup.py test", "cargo test", "cargo nextest", "cargo build",
                 "cargo check", "cargo clippy", "cargo fmt", "swift test", "swift build", "xcodebuild test",
                 "xcodebuild analyze", "xcodebuild build", "go test", "go build", "go vet", "dotnet test",
                 "dotnet build", "dotnet publish", "dotnet pack", "dotnet msbuild", "dotnet format", "cc",
                 "cmake --build", "ctest", "jest", "vitest", "mocha", "node --test", "tsc", "vue-tsc", "ruff",
                 "ruff format", "swiftlint", "bazel test", "bazel build", "meson test", "meson compile", "deno test",
                 "deno check", "deno lint", "deno fmt", "bun test", "bun build")


def all_suites():
    suites = set(CUSTOM_SUITES) | set(GRADLE_SUITES.values()) | {"mvn " + g for g in MVN_TEST + MVN_BUILD}
    for handler in PROGRAMS.values():
        rule = getattr(handler, "rule", None)
        if rule and rule[0] in ("named", "posargs", "paths", "flag"):
            suites.add(rule[2])
        elif rule and rule[0] == "sub":
            suites.update(entry[1] for entry in rule[1].values())
    for runner, (test_label, prefix) in SCRIPT_LABEL.items():
        suites.add(test_label)
        suites.update(prefix + canon for canon in CANON_SCRIPTS if canon != "test")
    suites.update(("make", "make check", "ninja", "ninja check"))
    return tuple(sorted(suites))


SUITES = all_suites()
SUITE_SET = frozenset(SUITES)


def classify(words, cwd=None):
    """(class, suite, targeted) of one segment's words after prefixes and runners are removed."""
    if not words:
        return RESULT_OTHER
    p = prog(words[0])
    args = words[1:]
    handler = c_python_script if p == "python" else PROGRAMS.get(p)
    if handler is None:
        return RESULT_OTHER
    result = handler(args, cwd)
    assert result[1] is None or result[1] in SUITE_SET, result
    return result


# ============================================================================ command parsing and command_info (3)

READ_PROGS = ("cat", "head", "tail", "less", "more", "nl", "wc", "ls", "tree", "stat", "file", "pwd", "bat", "batcat",
              "sed", "awk", "jq", "yq", "realpath", "readlink", "du", "basename", "dirname", "od", "xxd", "hexdump",
              "strings", "diff", "cmp", "sort", "uniq", "cut", "tr", "column", "which", "type", "whereis", "printenv",
              "date", "env", "exa", "eza", "lsd")
SEARCH_PROGS = ("rg", "grep", "egrep", "fgrep", "ag", "ack", "find", "fd", "fdfind", "locate", "mdfind")
GIT_READ = ("status", "diff", "log", "show", "blame", "rev-parse", "ls-tree", "cat-file", "describe", "shortlog",
            "reflog", "branch", "remote", "tag", "config")
GIT_SEARCH = ("grep", "ls-files")
GIT_VALUE = ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env")
FIND_ACTIONS = ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls")
PATCH_LINE = rx(r"[ \t]*\*\*\* Begin Patch[ \t]*")
FD_TARGET = rx(r"[0-9-]+")
WRITE_REDIRS = (">", ">>", ">|", "&>", "&>>", "<>")


class Parsed:
    def __init__(self):
        self.segments = []         # Segment, words normalised (prefixes, keywords, runners removed)
        self.patches = []          # patch texts (heredoc bodies or words holding a `*** Begin Patch` line)
        self.apply_patch = False
        self.pipefail = False
        self.compile_scope_unsafe = False


def has_patch_line(text):
    return any(PATCH_LINE.fullmatch(line) for line in text.split("\n"))


def norm_dir(target, ctx, cwd):
    """The directory context after `cd target` (3.3)."""
    t = target.replace("\\", "/")
    if t == "":
        return ctx
    rel, inside = cwd_relative(t, cwd)
    if inside:
        joined = rel
    elif t == "~" or t.startswith("~/") or t.startswith("/") or DRIVE.match(t) or t == "-":
        joined = t
    else:
        joined = (ctx + "/" + t) if ctx else t
    absolute = joined.startswith("/")
    parts = []
    for part in joined.split("/"):
        if part in ("", "."):
            continue
        if part == ".." and parts and parts[-1] != "..":
            parts.pop()
            continue
        parts.append(part)
    result = ("/" if absolute else "") + "/".join(parts)
    return "" if result in ("", ".") else result


class ParseState:
    def __init__(self, cwd):
        self.cwd = cwd
        self.ctx = ""
        self.parens = []           # contexts saved by `(`
        self.dirs = []             # contexts saved by `pushd`
        self.carry = []            # operators of dropped segments, merged into the next kept one


def apply_parens(ops, st):
    for op in ops:
        if op == "(":
            st.parens.append(st.ctx)
        elif op == ")" and st.parens:
            st.ctx = st.parens.pop()


PIPEFAIL_FLAG = rx(r"-[A-Za-z]*o")


def parse_into(text, parsed, st, depth):
    if depth > MAX_WRAPPER_DEPTH:
        return
    segments, tail = split_segments(tokenize(lf(text)))
    for seg in segments:
        apply_parens(seg.ops, st)
        ops = st.carry + seg.ops
        st.carry = []
        # Preserve evidence of environment wrappers before prefix removal. JAVA_HOME selects the compiler;
        # a successful lifecycle still compiles, while arbitrary variables may suppress Maven compilation.
        if any(re.match(r"[A-Za-z_][A-Za-z0-9_]*=", w) and not w.startswith("JAVA_HOME=") for w in seg.words):
            parsed.compile_scope_unsafe = True
        drop_keywords(seg)
        strip_prefixes(seg)
        drop_keywords(seg)
        if seg.words and (seg.words[0] in CLOSING_KEYWORDS or seg.words[0] in CONSTRUCTS):
            seg.words = []
        script = shell_script(seg)
        parsed.patches.extend(h for h in seg.heredocs if has_patch_line(h))
        if script is not None:
            st.carry = ops
            before = len(parsed.segments)
            parse_into(script, parsed, st, depth + 1)
            if seg.background and len(parsed.segments) > before:
                parsed.segments[-1].background = True
            continue
        parsed.patches.extend(w for w in seg.words if "\n" in w and has_patch_line(w))
        seg.words = unwrap(seg.words)
        seg.ctx = st.ctx
        if seg.words:
            p = prog(seg.words[0])
            if p in ("apply_patch", "applypatch"):
                parsed.apply_patch = True
            if p == "set" and any(PIPEFAIL_FLAG.fullmatch(w) and seg.words[k + 1:k + 2] == ["pipefail"]
                                  for k, w in enumerate(seg.words)):
                parsed.pipefail = True
            if p in ("cd", "pushd"):
                if p == "pushd":
                    st.dirs.append(st.ctx)
                target = next((w for w in seg.words[1:] if not w.startswith("-") or w == "-"), "~")
                st.ctx = norm_dir(target, st.ctx, st.cwd)
            elif p == "popd":
                st.ctx = st.dirs.pop() if st.dirs else st.ctx
        if seg.words or seg.heredocs:
            seg.ops = ops
            parsed.segments.append(seg)
        else:
            st.carry = ops
    apply_parens(tail, st)
    st.carry = st.carry + tail


def last_operator(ops):
    sig = [op for op in ops if op not in ("(", ")")]
    return sig[-1] if sig else None


def first_operator(ops):
    sig = [op for op in ops if op not in ("(", ")")]
    return sig[0] if sig else None


def parse_command(text, cwd=None):
    parsed = Parsed()
    st = ParseState(cwd)
    parse_into(scalars(text) if isinstance(text, str) else "", parsed, st, 0)
    segs = parsed.segments
    for k, seg in enumerate(segs):
        seg.op_before = last_operator(seg.ops) if k > 0 else None
        following = segs[k + 1].ops if k + 1 < len(segs) else st.carry
        seg.op_after = last_operator(following)
        if first_operator(following) == "&":
            seg.background = True
    return parsed


def is_neutral(seg):
    return not seg.words or prog(seg.words[0]) in NEUTRAL


def segment_tokens(seg, cwd):
    words = seg.words
    p = prog(words[0])
    tokens = [p]
    for w in words[1:]:
        if w in COSMETIC or w.startswith(COSMETIC_PREFIX):
            continue
        if p == "pytest" and (w == "-s" or re.fullmatch(r"-r[A-Za-z]+", w) or w == "--capture=no"):
            continue
        tokens.append(w)
    if p in ("npm", "pnpm", "yarn") and len(tokens) > 1 and tokens[1] in ("test", "t", "tst"):
        tokens = [p, "run", "test"] + tokens[2:]
    elif p in ("npm", "pnpm", "yarn", "bun") and len(tokens) > 1 and tokens[1] in ("run-script", "rum", "urn"):
        tokens = [p, "run"] + tokens[2:]
    elif p in ("pnpm", "yarn") and len(tokens) > 1 and tokens[1] not in NPM_BUILTINS and not tokens[1].startswith("-"):
        tokens = [p, "run"] + tokens[1:]
    if seg.ctx:
        tokens = ["@" + seg.ctx] + tokens
    return tokens


def choose(segs, cwd):
    """The class of every segment, and the chosen one: the first with the highest class rank (3.7)."""
    best_index, best, classes = None, RESULT_OTHER, []
    for k, seg in enumerate(segs):
        result = classify(seg.words, cwd) if not is_neutral(seg) else RESULT_OTHER
        classes.append(result)
        if CLASS_RANK[result[0]] > CLASS_RANK[best[0]]:
            best_index, best = k, result
    return classes, best_index, best


def maven_compile_cwd(source, tool, value, cwd):
    """Only coverage uses the explicit Codex exec_command working directory; other parser facts stay stable."""
    fam = FAMILY.get(source)
    args = decode_args(value)
    if fam == "codex" and isinstance(tool, str) and tool_name(fam, tool) == "exec_command" \
            and args is not None and "workdir" in args:
        return args["workdir"] if isinstance(args["workdir"], str) else None
    return cwd


def maven_compile_key(segs, classes, chosen, cwd):
    """Conservative main-compile coverage, not equivalence of tests or arbitrary Maven goals.

    Only a single verifier and one standard lifecycle phase (optional leading clean) qualify. Keep every
    build/configuration token and the starting/relative directories, removing only cosmetic output, test selectors
    and skipTests. Unknown/dynamic directories, direct plugins and compilation suppression give no evidence.
    """
    base = norm_cwd(cwd)
    if base is None or not (cwd.replace("\\", "/").startswith("/") or DRIVE.match(cwd.replace("\\", "/"))):
        return None
    if sum(c[0] != "other" for c in classes) != 1:
        return None
    seg = segs[chosen]
    if prog(seg.words[0]) != "mvn" or seg.background:
        return None
    for prior in segs[:chosen]:
        if prior.words and prog(prior.words[0]) in ("cd", "pushd", "popd"):
            if prior.op_after != "&&" or len(prior.words) != 2 or prior.words[1] in ("-", "~") \
                    or prior.words[1].startswith("~") or any(any(c in w for c in "$`*?[") for w in prior.words):
                return None
    args = seg.words[1:]
    if any(any(c in w for c in "$`*?[") for w in [base, seg.ctx] + args):
        return None
    phases = MVN_BUILD + MVN_TEST
    kept, goals = [], []
    k = 0
    while k < len(args):
        w = args[k]
        if w in ("-h", "--help", "-v", "--version", "-V", "--show-version", "-fn", "--fail-never"):
            return None
        if w in MVN_VALUE:
            if k + 1 >= len(args):
                return None
            kept.extend((w, args[k + 1]))
            k += 2
            continue
        if w in COSMETIC or w.startswith(COSMETIC_PREFIX):
            k += 1
            continue
        if w.startswith("-D"):
            prop = w[2:].split("=", 1)[0]
            if prop in ("test", "it.test", "skipTests", "surefire.failIfNoSpecifiedTests"):
                k += 1
                continue
            if ("skip" in prop.lower() and prop != "maven.test.skip") or "failonerror" in prop.lower():
                return None
        if not w.startswith("-"):
            goals.append(w)
        else:
            kept.append(w)
        k += 1
    if goals and goals[0] == "clean":
        goals = goals[1:]
    if len(goals) != 1 or goals[0] not in phases:
        return None
    return sha16("\x1f".join(["maven-main-compile-v1", base, seg.ctx] + kept))


def command_info(text, cwd=None, parsed=None):
    parsed = parsed or parse_command(text, cwd)
    cwd = cwd if isinstance(cwd, str) else None
    segs = parsed.segments
    classes, best_index, best = choose(segs, cwd)
    kept = [segment_tokens(seg, cwd) for seg, c in zip(segs, classes) if c[0] != "other"]
    if not kept:
        kept = [segment_tokens(seg, cwd) for seg in segs if not is_neutral(seg)]
    if not kept:
        kept = [segment_tokens(seg, cwd) for seg in segs if seg.words]
    key = sha16("\x1e".join("\x1f".join(t) for t in kept))
    if best_index is None:
        background = any(s.background for s in segs)
        return {"class": "other", "suite": None, "targeted": False, "key": key, "masked": False,
                "background": background, "errorFilter": False}
    chosen = segs[best_index]
    later = segs[best_index + 1:]
    masked = bool(later) and ((chosen.op_after == "|" and not parsed.pipefail)
                              or any(x.op_before in (";", "||", "&") for x in later))
    info = {"class": best[0], "suite": best[1], "targeted": bool(best[2]), "key": key, "masked": masked,
            "background": chosen.background,
            "errorFilter": best[0] in ("build", "check") and error_filter(segs, best_index)}
    coverage = None if masked or parsed.compile_scope_unsafe else maven_compile_key(segs, classes, best_index, cwd)
    if coverage is not None:
        info["mavenCompileKey"] = coverage
    return info


# ---------------------------------------------------------------------------- masked runs (3.10)

STATUS_REF = rx(r"\$\?|\$\{\?\}|\$\{PIPESTATUS\[([0-9]+)\]\}|\$PIPESTATUS")
STATUS_VALUE = rx(r"-?[0-9]+")
GREP_PROGRAMS = ("grep", "egrep")
GREP_FLAGS = "iEnwFPashHo"
GREP_LONG_FLAGS = ("--ignore-case", "--extended-regexp", "--line-number", "--word-regexp", "--fixed-strings",
                   "--perl-regexp", "--text", "--no-messages", "--no-filename", "--with-filename", "--only-matching")
GREP_VALUE_OPTIONS = ("-e", "-A", "-B", "-C", "-m", "--regexp", "--after-context", "--before-context", "--context",
                      "--max-count")
LIMITERS = ("head", "tail")
NO_OUTPUT = "(Bash completed with no output)"


def pipeline_end(segs, k):
    """Index of the last segment of the pipeline that holds segs[k]."""
    while k + 1 < len(segs) and segs[k].op_after == "|":
        k += 1
    return k


def pipeline_start(segs, k):
    while k > 0 and segs[k].op_before == "|":
        k -= 1
    return k


def grep_patterns(words):
    """The patterns of a grep that reads only its input (no file operands, no inverting or counting option), else
    None."""
    patterns, positional, i = [], [], 1
    while i < len(words):
        w = words[i]
        if w == "--":
            positional += words[i + 1:]
            break
        name, eq, value = w.partition("=")
        if w in GREP_VALUE_OPTIONS or (w.startswith("--") and eq and name in GREP_VALUE_OPTIONS):
            if eq:
                arg = value
            elif i + 1 < len(words):
                arg, i = words[i + 1], i + 1
            else:
                return None
            if name in ("-e", "--regexp"):
                patterns.append(arg)
        elif len(w) > 2 and w[:2] in ("-e", "-A", "-B", "-C", "-m") and not w.startswith("--"):
            if w[:2] == "-e":
                patterns.append(w[2:])
        elif w in GREP_LONG_FLAGS or name == "--color" or name == "--colour":
            pass
        elif w.startswith("-") and len(w) > 1 and not w.startswith("--"):
            if any(c not in GREP_FLAGS for c in w[1:]):
                return None
        elif w.startswith("--"):
            return None
        else:
            positional.append(w)
        i += 1
    if not patterns:
        if not positional:
            return None
        patterns, positional = [positional[0]], positional[1:]
    return None if positional else patterns


def error_filter(segs, k):
    """The chosen segment pipes into a grep for error lines, then at most `head` / `tail`, ending the command (3.10)."""
    end = pipeline_end(segs, k)
    if end == k or end != len(segs) - 1 or segs[k].op_after != "|":
        return False
    words = segs[k + 1].words
    if not words or prog(words[0]) not in GREP_PROGRAMS:
        return False
    patterns = grep_patterns(words)
    if not patterns or not any("error" in pat.translate(ASCII_LOWER) for pat in patterns):
        return False
    return all(segs[j].words and prog(segs[j].words[0]) in LIMITERS for j in range(k + 2, end + 1))


def echo_text(seg):
    """What an `echo` segment prints to the output (its words joined by spaces), or None."""
    w = seg.words
    if len(w) < 2 or prog(w[0]) != "echo" or seg.redirs or seg.heredocs or w[1].startswith("-"):
        return None
    return " ".join(w[1:])


def status_text(text):
    """(prefix, suffix, PIPESTATUS index or None) of an echo text with exactly one status reference and no other
    expansion, (text, None, None) for a fixed text, or None."""
    refs = list(STATUS_REF.finditer(text))
    if len(refs) > 1:
        return None
    if not refs:
        return None if "$" in text or "`" in text or not text.strip(WS) else (text, None, None)
    m = refs[0]
    rest = text[:m.start()] + text[m.end():]
    if "$" in rest or "`" in rest:
        return None
    index = 0 if m.group(0) == "$PIPESTATUS" else (int(m.group(1)) if m.group(1) is not None else None)
    return text[:m.start()], text[m.end():], index


def status_echo(parsed, k):
    """The status echo that reports the exit status of the chosen segment segs[k] (3.10) as {form, …}, or None."""
    segs = parsed.segments
    end = pipeline_end(segs, k)
    own = end == k or parsed.pipefail
    after = segs[end + 1:]
    texts = [status_text(echo_text(seg) or "") for seg in after]
    if None in texts:
        return None
    if len(after) == 1 and after[0].op_before == ";":
        prefix, suffix, index = texts[0]
        if suffix is None or (own if index is None else index == k - pipeline_start(segs, k)) is False:
            return None
        return {"form": "printed", "prefix": prefix, "suffix": suffix}
    if not own or any(index is not None for _, _, index in texts):
        return None
    if len(after) == 2 and after[0].op_before == "&&" and after[1].op_before == "||":
        ok, fail = texts
        if ok[1] is not None or (fail[1] is None and fail[0].strip(WS) == ok[0].strip(WS)):
            return None
        return {"form": "branches", "ok": ok[0], "fail": [fail[0], fail[1]]}
    if len(after) == 1 and after[0].op_before == "||":
        return {"form": "else", "fail": [texts[0][0], texts[0][1]]}
    return None


def printed_value(line, prefix, suffix):
    """The integer that `line` holds between `prefix` and `suffix` (both trimmed at their outer edge), or None."""
    prefix, suffix = prefix.lstrip(WS), suffix.rstrip(WS)
    if len(line) < len(prefix) + len(suffix) or not line.startswith(prefix) or not line.endswith(suffix):
        return None
    middle = line[len(prefix):len(line) - len(suffix)]
    return num(middle) if STATUS_VALUE.fullmatch(middle) else None


def silent(text):
    """No output at all: nothing but whitespace, or Claude Code's placeholder for it."""
    stripped = text.strip(JSON_WS)
    return stripped == "" or stripped == NO_OUTPUT


def masked_status(text, output, cwd=None):
    """{exitCode, failed} that a masked command shows in its output (3.10), or None. `exitCode` null means kept."""
    parsed = parse_command(text, cwd)
    info = command_info(text, cwd, parsed)
    if not info["masked"]:
        return None
    k = choose(parsed.segments, cwd if isinstance(cwd, str) else None)[1]
    lines = [x.strip(WS) for x in lf(output).split("\n") if x.strip(WS)]
    last = lines[-1] if lines else None
    echo = status_echo(parsed, k) if k is not None else None
    if echo is not None and last is not None:
        if echo["form"] == "printed":
            value = printed_value(last, echo["prefix"], echo["suffix"])
            if value is not None:
                return {"exitCode": value, "failed": value != 0}
        else:
            if echo["form"] == "branches" and last == echo["ok"].strip(WS):
                return {"exitCode": 0, "failed": False}
            prefix, suffix = echo["fail"]
            if suffix is None:
                if last == prefix.strip(WS):
                    return {"exitCode": None, "failed": True}
            else:
                value = printed_value(last, prefix, suffix)
                if value is not None and value != 0:
                    return {"exitCode": value, "failed": True}
    if info["errorFilter"] and silent(output):
        return {"exitCode": None, "failed": False}
    return None





def shell_kind(parsed, cwd=None):
    if parsed.apply_patch or parsed.patches:
        return "edit"
    info = command_info("", cwd, parsed)
    if info["class"] != "other":
        return "command"
    if any(op in WRITE_REDIRS and target not in ("/dev/null", "NUL", "nul") and not FD_TARGET.fullmatch(target)
           for seg in parsed.segments for op, target in seg.redirs):
        return "command"
    segs = [s for s in parsed.segments if not is_neutral(s)]
    if not segs:
        return "command"
    kinds = set()
    for seg in segs:
        p = prog(seg.words[0])
        args = seg.words[1:]
        if p == "git":
            k = drop_options(args, 0, GIT_VALUE)
            sub = args[k] if k < len(args) else None
            if sub in GIT_READ:
                kinds.add("read")
            elif sub in GIT_SEARCH:
                kinds.add("search")
            else:
                return "command"
        elif p == "sed" and any(a.startswith("-i") or a.startswith("--in-place") for a in args):
            return "command"
        elif p in READ_PROGS:
            kinds.add("read")
        elif p in SEARCH_PROGS:
            if p == "find" and any(a in FIND_ACTIONS for a in args):
                return "command"
            kinds.add("search")
        else:
            return "command"
    return "search" if "search" in kinds else "read"


# ============================================================================ results: text, outcome, sessions (4)

CLAUDE_EXIT = rx(r"Exit code (-?[0-9]+)")
CLAUDE_ABORT = ("[Request interrupted by user", "The user doesn't want to proceed with this tool use",
                "The user doesn't want to take this action", "Permission to use ",
                "Claude requested permissions to use", "User rejected", "The tool use was rejected")
CLAUDE_TIMEOUT = rx(r"Command timed out after .*")
CLAUDE_BACKGROUND = rx(r"Command running in background with ID: ?([A-Za-z0-9_:-]+(?:\.[A-Za-z0-9_:-]+)*).*")
CODEX_EXIT = rx(r"Exit code: (-?[0-9]+)")
CODEX_EXITED = rx(r"Process exited with code (-?[0-9]+)")
CODEX_RUNNING = rx(r"Process running with session ID ([0-9]+)")
CODEX_TIMEOUT = rx(r"command timed out after [0-9]+ milliseconds")
CODEX_HEADER = rx(r"(Chunk ID|Wall time|Exit code|Process exited with code|Process running with session ID|"
                  r"Original token count|Total output lines)[: ].*")
PI_EXIT = rx(r"\(?Command exited with code (-?[0-9]+)\)?")
PI_TIMEOUT = rx(r"\[?Command timed out.*")
PI_ABORT = rx(r"(Command aborted.*|Command was terminated.*|Operation aborted.*|Command cancelled.*)")
PI_RUNNING = rx(r"Command still running \(session .*")
BASHOUTPUT_STATUS = rx(r"<status>([a-z_]+)</status>")
BASHOUTPUT_EXIT = rx(r"<exit_code>(-?[0-9]+)</exit_code>")


def is_int(value):
    """A JSON integer: a number without fraction or exponent, in the signed 64-bit range; never a boolean."""
    return isinstance(value, int) and not isinstance(value, bool) and -2 ** 63 <= value < 2 ** 63


def blocks_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text") for b in content
                         if isinstance(b, dict) and b.get("type") in ("text", "input_text", "output_text")
                         and isinstance(b.get("text"), str))
    return ""


def codex_status(result):
    """Text, body, exit code, state, session and patch verdict of a Codex `*_output` payload or its `output` value."""
    out = result.get("output") if isinstance(result, dict) and "type" in result and "output" in result else result
    if isinstance(out, dict) and isinstance(out.get("content"), (list, str)):
        out = out["content"]
    if isinstance(out, str) and out.lstrip(JSON_WS).startswith("["):
        decoded = parse_json(out)
        if isinstance(decoded, list) and decoded and all(isinstance(x, dict) and isinstance(x.get("type"), str)
                                                         for x in decoded):
            out = decoded
    text = lf(blocks_text(out))
    status = {"text": text, "body": text, "exit": None, "state": "done", "session": None, "patch": None}
    lines = text.split("\n")
    head = leading_object(text)
    if head is not None and isinstance(head.get("output"), str):
        status["body"] = lf(head["output"])
        meta = head.get("metadata") if isinstance(head.get("metadata"), dict) else {}
        if is_int(head.get("exit_code")):
            status["exit"] = head["exit_code"]
        elif is_int(meta.get("exit_code")):
            status["exit"] = meta["exit_code"]
        elif is_int(head.get("session_id")):
            status["state"], status["session"] = "running", "codex:" + str(head["session_id"])
    else:
        k = next((k for k, x in enumerate(lines[:8]) if x == "Output:"), None)
        if k is not None and all(CODEX_HEADER.fullmatch(x) for x in lines[:k]):
            status["body"] = "\n".join(lines[k + 1:])
            for line in lines[:k]:
                r = CODEX_EXIT.fullmatch(line) or CODEX_EXITED.fullmatch(line)
                if r:
                    status["exit"] = num(r.group(1))
                r = CODEX_RUNNING.fullmatch(line)
                if r:
                    status["state"], status["session"] = "running", "codex:" + r.group(1)
    body_first = status["body"].split("\n", 1)[0]
    first = lines[0].strip(WS)
    if first == "aborted" or first.startswith("aborted by user") or "rejected by user" in first:
        status["state"] = "aborted"
    elif CODEX_TIMEOUT.match(body_first):
        status["state"] = "timeout"
    if status["exit"] is None and status["state"] == "done":
        if body_first.startswith("Success. Updated the following files"):
            status["patch"] = False
        elif body_first.startswith(("apply_patch verification failed", "Failed to apply patch")):
            status["patch"] = True
    return status


def parse_json(text):
    """An RFC 8259 JSON value, or None: NaN and Infinity are rejected, and so is nesting too deep to read."""
    try:
        return json.loads(text, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        return None


def reject_constant(name):
    raise ValueError(name)


def leading_object(text):
    """The JSON object that starts the text (after JSON whitespace), ignoring what follows it; else None."""
    stripped = text.lstrip(JSON_WS)
    if not stripped.startswith("{"):
        return None
    try:
        obj, _ = json.JSONDecoder(parse_constant=reject_constant).raw_decode(stripped)
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def lf(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def result_text(source, result):
    """The output text that `failures` reads (4.1)."""
    family = FAMILY.get(source)
    if family == "claude":
        if not isinstance(result, dict):
            return ""
        text = lf(blocks_text(result.get("content")))
        lines = text.split("\n")
        if lines and CLAUDE_EXIT.fullmatch(lines[0]):
            text = "\n".join(lines[1:])
        return text
    if family == "codex":
        return codex_status(result)["body"]
    if family == "droid":
        return lf(blocks_text(result.get("content"))) if isinstance(result, dict) else ""
    if family in ("omp", "pi", "openclaw"):
        if not isinstance(result, dict):
            return ""
        lines = lf(blocks_text(result.get("content"))).split("\n")
        while lines and lines[-1].strip(WS) == "":
            lines.pop()
        if lines and PI_EXIT.fullmatch(lines[-1].strip(WS)):
            lines.pop()
        return "\n".join(lines)
    return ""


def shell_call_state(source, tool, value, cwd=None):
    """(background, masked) of a shell call: the input's own background flag or the command's (4.2)."""
    family = FAMILY.get(source)
    text = command_text(source, tool, value)
    if text is None:
        return False, False
    args = decode_args(value) or {}
    background = (family == "claude" and args.get("run_in_background") is True) or \
                 (family == "openclaw" and args.get("background") is True)
    info = command_info(text, cwd)
    return background or info["background"], info["masked"]


def masked_outcome(source, tool, value, result, cwd=None):
    """What a masked shell call's output says about its chosen segment (3.10), or None."""
    text = command_text(source, tool, value)
    return masked_status(text, result_text(source, result), cwd) if text is not None else None


def outcome(source, result, t=None, tool=None, value=None, cwd=None):
    """{exitCode, failed, t} of a tool result (4.2)."""
    family = FAMILY.get(source)
    exit_code, failed, state = None, None, "done"
    if family == "claude" and isinstance(result, dict):
        text = lf(blocks_text(result.get("content")))
        first = text.split("\n", 1)[0]
        m = CLAUDE_EXIT.fullmatch(first)
        if m:
            exit_code = num(m.group(1))
        if text.startswith(CLAUDE_ABORT):
            state = "aborted"
        elif CLAUDE_TIMEOUT.fullmatch(first):
            state = "timeout"
        elif CLAUDE_BACKGROUND.fullmatch(first):
            state = "running"
        elif tool in ("BashOutput", "TaskOutput"):
            status = BASHOUTPUT_STATUS.search(text)
            if status and status.group(1) == "running":
                state = "running"
            mexit = BASHOUTPUT_EXIT.search(text)
            if mexit and exit_code is None and status:
                exit_code = num(mexit.group(1))
        failed = (exit_code != 0) if exit_code is not None else (result.get("is_error") is True)
    elif family == "codex":
        status = codex_status(result)
        exit_code, state = status["exit"], status["state"]
        failed = (exit_code != 0) if exit_code is not None else status["patch"]
    elif family in ("omp", "pi", "openclaw") and isinstance(result, dict):
        lines = [x.strip(WS) for x in lf(blocks_text(result.get("content"))).split("\n")]
        nonempty = [x for x in lines if x]
        details = result.get("details") if isinstance(result.get("details"), dict) else {}
        if is_int(details.get("exitCode")):
            exit_code = details["exitCode"]
        elif is_int(details.get("exit_code")):
            exit_code = details["exit_code"]
        elif nonempty and PI_EXIT.fullmatch(nonempty[-1]):
            exit_code = num(PI_EXIT.fullmatch(nonempty[-1]).group(1))
        if details.get("status") == "running" or (nonempty and PI_RUNNING.match(nonempty[0])):
            state = "running"
        elif details.get("timedOut") is True or any(PI_TIMEOUT.fullmatch(x) for x in nonempty[-2:]):
            state = "timeout"
        elif nonempty and (PI_ABORT.fullmatch(nonempty[-1]) or PI_ABORT.fullmatch(nonempty[0])):
            state = "aborted"
        failed = (exit_code != 0) if exit_code is not None else (result.get("isError") is True)
    elif family == "droid" and isinstance(result, dict):
        failed = result.get("is_error") is True
    else:
        return {"exitCode": None, "failed": None, "t": t}
    if tool is not None and state == "done":
        background, masked = shell_call_state(source, tool, value, cwd)
        if background:
            state = "running"
        elif masked:
            shown = masked_outcome(source, tool, value, result, cwd)
            if shown is None:
                failed = None
            else:
                failed = shown["failed"]
                if shown["exitCode"] is not None or failed:
                    exit_code = shown["exitCode"]
    if state != "done":
        failed = None
        if state in ("running", "aborted"):
            exit_code = None
    return {"exitCode": exit_code, "failed": failed, "t": t}


def continuation(source, tool, value):
    """The session a call continues (Codex `write_stdin`, Claude `BashOutput` / `TaskOutput`), or None (4.3)."""
    family = FAMILY.get(source)
    args = decode_args(value) or {}
    if family == "codex" and tool == "write_stdin" and is_int(args.get("session_id")):
        return "codex:" + str(args["session_id"])
    if family == "claude" and tool in ("BashOutput", "TaskOutput"):
        for field in ("bash_id", "shell_id", "task_id"):
            if isinstance(args.get(field), str) and args[field]:
                return "claude:" + args[field]
    return None


def running_session(source, result):
    """The session a result leaves running, or None (4.3)."""
    family = FAMILY.get(source)
    if family == "codex":
        return codex_status(result)["session"]
    if family == "claude" and isinstance(result, dict):
        first = lf(blocks_text(result.get("content"))).split("\n", 1)[0]
        m = CLAUDE_BACKGROUND.fullmatch(first)
        return "claude:" + m.group(1) if m else None
    return None


NOTIFY_TASK = rx(r"<task-id>([^<]+)</task-id>")
NOTIFY_SUMMARY = rx(r"<summary>([^\n]*?)</summary>")
NOTIFY_EXIT = rx(r"Background command [^\n]*(?:\(exit code (-?[0-9]+)\)|with exit code (-?[0-9]+))")


def notification(source, line):
    """{session, exitCode} of a Claude background-task notice that reports an exit code (4.3), else None."""
    if FAMILY.get(source) != "claude" or not isinstance(line, dict) or line.get("type") != "attachment":
        return None
    att = line.get("attachment")
    if not isinstance(att, dict) or att.get("type") != "queued_command" or att.get("commandMode") != "task-notification":
        return None
    text = att.get("prompt")
    if not isinstance(text, str):
        return None
    text = lf(scalars(text))
    task, summary = NOTIFY_TASK.search(text), NOTIFY_SUMMARY.search(text)
    if not task or not summary:
        return None
    m = NOTIFY_EXIT.fullmatch(summary.group(1).strip(WS))
    if not m:
        return None
    return {"session": "claude:" + task.group(1), "exitCode": num(m.group(1) or m.group(2))}


# ============================================================================ Codex code mode (4.4)

JS_WS = " \t\r\n\f\v"
JS_IDENT_START = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_$")
JS_IDENT = JS_IDENT_START | frozenset("0123456789")
JS_DIGITS = frozenset("0123456789")
JS_NUMBER_TOKEN = rx(r"[0-9](?:[0-9A-Za-z_.]|(?<=[eE])[+-])*")
JS_NUMBER = rx(r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
JS_HEX2 = rx(r"[0-9A-Fa-f]{2}")
JS_HEX4 = rx(r"[0-9A-Fa-f]{4}")
JS_CODEPOINT = rx(r"\{([0-9A-Fa-f]{1,6})\}")
JS_SIMPLE_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v"}
JS_LINE_BREAKS = ["\r\n", "\n", "\r", "\u2028", "\u2029"]  # a list: the appendix lists word tuples only
JS_REGEX_AFTER = ("return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case", "do", "else",
                  "yield", "await")
JS_OPEN, JS_CLOSE = "([{", ")]}"
CODE_COMPLETED = "Script completed"


class JsError(Exception):
    pass


class Tok:
    __slots__ = ("kind", "start", "end", "value", "text")

    def __init__(self, kind, start, end, value, text):
        self.kind, self.start, self.end, self.value, self.text = kind, start, end, value, text


def js_escape(text, i, out):
    """One escape after the backslash at text[i - 1]; returns the next index."""
    for brk in JS_LINE_BREAKS:
        if text.startswith(brk, i):
            return i + len(brk)
    c = text[i]
    if c in JS_SIMPLE_ESCAPES:
        out.append(JS_SIMPLE_ESCAPES[c])
        return i + 1
    if c == "0" and not (i + 1 < len(text) and text[i + 1] in JS_DIGITS):
        out.append("\0")
        return i + 1
    if c == "x" and JS_HEX2.match(text, i + 1):
        out.append(chr(int(text[i + 1:i + 3], 16)))
        return i + 3
    if c == "u":
        m = JS_CODEPOINT.match(text, i + 1)
        if m and int(m.group(1), 16) <= 0x10FFFF:
            out.append(chr(int(m.group(1), 16)))
            return m.end()
        if JS_HEX4.match(text, i + 1):
            out.append(chr(int(text[i + 1:i + 5], 16)))
            return i + 5
    out.append(c)
    return i + 1


def js_join(units):
    """Decoded characters, with surrogate pairs from \\u escapes combined and lone surrogates read as U+FFFD."""
    s = "".join(units)
    return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace") if LONE_SURROGATE.search(s) else s


def js_string(text, i):
    quote, out, j, n = text[i], [], i + 1, len(text)
    while j < n:
        c = text[j]
        if c == quote:
            return js_join(out), j + 1
        if c == "\\" and j + 1 < n:
            j = js_escape(text, j + 1, out)
            continue
        if c in "\n\r":
            break
        out.append(c)
        j += 1
    raise JsError("unterminated string")


def js_template(text, i):
    """A template literal: its value, or None when it holds `${…}`; and its end."""
    out, j, n, opaque = [], i + 1, len(text), False
    while j < n:
        c = text[j]
        if c == "`":
            return (None if opaque else js_join(out)), j + 1
        if c == "\\" and j + 1 < n:
            j = js_escape(text, j + 1, out)
            continue
        if text.startswith("${", j):
            opaque = True
            j = js_lex(text, j + 2, until_brace=True)[1]
            continue
        if c == "\r":
            out.append("\n")
            j += 2 if text.startswith("\r\n", j) else 1
            continue
        out.append(c)
        j += 1
    raise JsError("unterminated template")


def js_regex_end(text, i):
    """The end of a regular expression literal starting at text[i] == '/', or None."""
    j, n, in_class = i + 1, len(text), False
    while j < n:
        c = text[j]
        if c in "\n\r  ":
            return None
        if c == "\\":
            j += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            j += 1
            while j < n and text[j] in JS_IDENT:
                j += 1
            return j
        j += 1
    return None


def regex_allowed(toks):
    if not toks:
        return True
    last = toks[-1]
    if last.kind in ("num", "str", "regex"):
        return False
    if last.kind == "id":
        return last.text in JS_REGEX_AFTER
    return last.text not in JS_CLOSE


def js_lex(text, i=0, until_brace=False):
    """Tokens of a script (or of a template's `${…}` up to its closing brace) and the index after them."""
    toks, n, depth = [], len(text), 0
    while i < n:
        c = text[i]
        if c in JS_WS:
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c in "'\"":
            value, j = js_string(text, i)
            toks.append(Tok("str", i, j, value, text[i:j]))
            i = j
        elif c == "`":
            value, j = js_template(text, i)
            toks.append(Tok("str", i, j, value, text[i:j]))
            i = j
        elif c == "/" and regex_allowed(toks) and js_regex_end(text, i) is not None:
            j = js_regex_end(text, i)
            toks.append(Tok("regex", i, j, None, text[i:j]))
            i = j
        elif c in JS_IDENT_START:
            j = i + 1
            while j < n and text[j] in JS_IDENT:
                j += 1
            toks.append(Tok("id", i, j, None, text[i:j]))
            i = j
        elif c in JS_DIGITS:
            j = JS_NUMBER_TOKEN.match(text, i).end()
            raw = text[i:j]
            value = None
            if JS_NUMBER.fullmatch(raw):
                value = int(raw) if raw.isdigit() else float(raw)
            toks.append(Tok("num", i, j, value, raw))
            i = j
        else:
            if until_brace:
                if c == "{":
                    depth += 1
                elif c == "}":
                    if depth == 0:
                        return toks, i + 1
                    depth -= 1
            toks.append(Tok("punct", i, i + 1, None, c))
            i += 1
    if until_brace:
        raise JsError("unterminated template expression")
    return toks, i


def is_punct(tok, text):
    return tok is not None and tok.kind == "punct" and tok.text == text


def js_close(toks, k):
    """Index of the token that closes the bracket at toks[k], or None."""
    depth = 0
    for j in range(k, len(toks)):
        t = toks[j]
        if t.kind != "punct":
            continue
        if t.text in JS_OPEN:
            depth += 1
        elif t.text in JS_CLOSE:
            depth -= 1
            if depth == 0:
                return j
    return None


MISSING = object()


def js_literal(toks, p, end, env=None):
    """(value, next index) of the literal at toks[p] before `end`, or (MISSING, p) when it is not a literal. `env` maps
    the names a plain script bound to literals (4.4)."""
    if p >= end:
        return MISSING, p
    t = toks[p]
    if t.kind == "id" and env and t.text in env:
        return env[t.text], p + 1
    if t.kind == "str":
        return (MISSING, p) if t.value is None else (t.value, p + 1)
    if t.kind == "num":
        return (MISSING, p) if t.value is None else (t.value, p + 1)
    if is_punct(t, "-") and p + 1 < end and toks[p + 1].kind == "num" and toks[p + 1].value is not None:
        return -toks[p + 1].value, p + 2
    if t.kind == "id" and t.text in ("true", "false", "null"):
        return {"true": True, "false": False, "null": None}[t.text], p + 1
    if is_punct(t, "{"):
        close = js_close(toks, p)
        if close is None or close >= end:
            return MISSING, p
        obj, q = {}, p + 1
        while q < close:
            stop = q
            depth = 0
            while stop < close and not (depth == 0 and is_punct(toks[stop], ",")):
                if toks[stop].kind == "punct" and toks[stop].text in JS_OPEN:
                    depth += 1
                elif toks[stop].kind == "punct" and toks[stop].text in JS_CLOSE:
                    depth -= 1
                stop += 1
            key_tok = toks[q]
            key = key_tok.text if key_tok.kind == "id" else key_tok.value if key_tok.kind == "str" else \
                str(key_tok.value) if key_tok.kind == "num" and isinstance(key_tok.value, int) else None
            if isinstance(key, str) and q + 1 < stop and is_punct(toks[q + 1], ":"):
                value, after = js_literal(toks, q + 2, stop, env)
                if value is not MISSING and after == stop:
                    obj[key] = value
            q = stop + 1
        return obj, close + 1
    if is_punct(t, "["):
        close = js_close(toks, p)
        if close is None or close >= end:
            return MISSING, p
        items, q = [], p + 1
        while q < close:
            value, after = js_literal(toks, q, close, env)
            if value is MISSING or (after < close and not is_punct(toks[after], ",")):
                return MISSING, p
            items.append(value)
            q = after + 1
        return items, close + 1
    return MISSING, p


def code_script(source, tool, value):
    """A Codex code-mode script (4.4): {calls: [{tool, input, digest}], plain, prints: [[call, mode]]} or None."""
    if FAMILY.get(source) != "codex" or tool != "exec" or not isinstance(value, str) or decode_args(value) is not None:
        return None
    text = scalars(value)
    try:
        toks = js_lex(text)[0]
    except JsError:
        return None
    calls, spans = [], {}
    for k, t in enumerate(toks):
        if t.kind != "id" or t.text != "tools" or (k > 0 and is_punct(toks[k - 1], ".")):
            continue
        if not (k + 3 < len(toks) and is_punct(toks[k + 1], ".") and toks[k + 2].kind == "id"
                and is_punct(toks[k + 3], "(")):
            continue
        close = js_close(toks, k + 3)
        if close is None:
            continue
        spans[k] = (len(calls), close)
        calls.append({"tool": toks[k + 2].text, "input": call_input(toks, k, close, None),
                      "digest": sha16(text[t.start:toks[close].end])})
    if not calls:
        return None
    plain, prints, inputs = plain_statements(toks, spans)
    if plain:
        for index, value in inputs.items():
            calls[index]["input"] = value
    return {"calls": calls, "plain": plain, "prints": prints if plain else []}


def call_input(toks, k, close, env):
    if close == k + 4:
        return None
    arg, after = js_literal(toks, k + 4, close, env)
    return None if arg is MISSING or after != close else arg


def plain_statements(toks, spans):
    """Whether the script is plain (4.4), its printing statements as [call index, "result" | "output"], and the calls'
    inputs read with the literals bound before them."""
    env, bound, prints, inputs, p, n = {}, {}, [], {}, 0, len(toks)

    def at(i, text, kind="punct"):
        return i < n and toks[i].kind == kind and toks[i].text == text

    def awaited(i):
        """The call of `await tools.NAME(…)` at toks[i], refreshed with the bindings so far, and the index after it."""
        if at(i, "await", "id") and i + 1 in spans:
            index, close = spans[i + 1]
            inputs[index] = call_input(toks, i + 1, close, env)
            return index, close + 1
        return None, i

    while p < n:
        if at(p, ";"):
            p += 1
            continue
        if toks[p].kind == "id" and toks[p].text in ("const", "let", "var") and p + 2 < n \
                and toks[p + 1].kind == "id" and at(p + 2, "="):
            name = toks[p + 1].text
            index, q = awaited(p + 3)
            if index is not None:
                bound[name] = index
                env.pop(name, None)
            else:
                literal, q = js_literal(toks, p + 3, n, env)
                if literal is MISSING:
                    return False, [], {}
                env[name] = literal
                bound.pop(name, None)
            p = q
            continue
        if at(p, "text", "id") and at(p + 1, "("):
            q, index, mode = p + 2, None, "result"
            index, after = awaited(q)
            if index is None and at(q, "("):
                index, after = awaited(q + 1)
                if index is not None and at(after, ")") and at(after + 1, ".") and at(after + 2, "output", "id"):
                    mode, after = "output", after + 3
                else:
                    index = None
            if index is None and q < n and toks[q].kind == "id" and toks[q].text in bound:
                index, after = bound[toks[q].text], q + 1
                if at(after, ".") and at(after + 1, "output", "id"):
                    mode, after = "output", after + 2
            if index is None or not at(after, ")"):
                return False, [], {}
            prints.append([index, mode])
            p = after + 1
            continue
        index, q = awaited(p)
        if index is None:
            return False, [], {}
        p = q
    return True, prints, inputs


def code_items(source, result):
    """The printed results of a code-mode script that completed (4.4), or None."""
    if FAMILY.get(source) != "codex" or not isinstance(result, dict):
        return None
    out = result.get("output") if "type" in result and "output" in result else result
    if not isinstance(out, list) or not out:
        return None
    texts = [lf(item["text"]) if isinstance(item, dict) and isinstance(item.get("text"), str) else None for item in out]
    if texts[0] is None or texts[0].split("\n", 1)[0] != CODE_COMPLETED:
        return None
    return texts[1:]


# ============================================================================ failures: preprocessing (5.1, 5.2)

HEAD_BYTES = 65_536
TAIL_BYTES = 196_608
MAX_LINE = 2_000
MAX_MESSAGE = 200
MAX_FINGERPRINTS = 256

ANSI_OSC = rx(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
ANSI_CSI = rx(r"\x1b\[[0-?]*[ -/]*[@-~]")
ANSI_FE = rx(r"\x1b[@-Z\\-_]")
CONTROL = rx(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def budget(text):
    """Segments of the output: all of it, or a head and a tail cut on character and line boundaries (5.1)."""
    text = scalars(text)
    data = text.encode("utf-8")
    if len(data) <= HEAD_BYTES + TAIL_BYTES:
        return [text]
    cut = HEAD_BYTES
    while cut > 0 and (data[cut] & 0xC0) == 0x80:
        cut -= 1
    head = data[:cut].decode("utf-8")
    start = len(data) - TAIL_BYTES
    while start < len(data) and (data[start] & 0xC0) == 0x80:
        start += 1
    tail = data[start:].decode("utf-8")
    nl = head.rfind("\n")
    head = head[:nl + 1] if nl >= 0 else head
    nl = tail.find("\n")
    tail = tail[nl + 1:] if nl >= 0 else tail
    return [head, tail]


WORKSPACE_PREFIX = rx(r"((?:@[A-Za-z0-9._-]+/|[A-Za-z0-9._-]*/)[A-Za-z0-9._/-]* [A-Za-z][A-Za-z0-9:._-]*|"
                      r"@?[A-Za-z0-9._/-]+:[A-Za-z][A-Za-z0-9._-]*): ?")
WORKSPACE_MIN = 3


def strip_workspace_prefixes(lines):
    """Removes the `<package> <script>: ` prefixes that pnpm, yarn and turbo put on interleaved lines (5.1)."""
    counts = {}
    for line in lines:
        r = WORKSPACE_PREFIX.match(line)
        if r and (r.end() == len(line) or line[len(r.group(1)) + 1:len(r.group(1)) + 2] in (" ", "")):
            counts[r.group(1)] = counts.get(r.group(1), 0) + 1
    common = {p for p, n in counts.items() if n >= WORKSPACE_MIN}
    if not common:
        return lines
    out = []
    for line in lines:
        r = WORKSPACE_PREFIX.match(line)
        if r and r.group(1) in common and line[len(r.group(1)) + 1:len(r.group(1)) + 2] in (" ", ""):
            rest = line[len(r.group(1)) + 1:]
            out.append(rest[1:] if rest.startswith(" ") else rest)
        else:
            out.append(line)
    return out


def clean_lines(segment):
    text = ANSI_OSC.sub("", segment)
    text = ANSI_CSI.sub("", text)
    text = ANSI_FE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = CONTROL.sub("", text)
    for sep in ("\u0085", "\u2028", "\u2029"):
        text = text.replace(sep, " ")
    lines = []
    for line in text.split("\n"):
        line = line.rstrip(" \t")
        lines.append(line[:MAX_LINE])
    return strip_workspace_prefixes(lines)


NOT_IN_PATH = r"(?<![A-Za-z0-9._~/\\-])"
TMP_PATH = rx(NOT_IN_PATH + r"(?:(?:/private)?/var/folders/|(?:/private)?/tmp/|[A-Za-z]:[\\/]Users[\\/][^\\/ \t]+[\\/]"
              r"AppData[\\/]Local[\\/]Temp[\\/])[^ \t'\"()\[\]\{\}<>,;|`]*")
HOME_PATH = rx(NOT_IN_PATH + r"(?:/Users/[^/ \t'\"]+/|/home/[^/ \t'\"]+/|[A-Za-z]:[\\/]Users[\\/][^\\/ \t'\"]+[\\/])")
UUID = rx(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
ISO_TIME = rx(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:[.,][0-9]+)?)?(?:Z|[+-][0-9]{2}:?[0-9]{2}"
              r")?")
TIME_OF_DAY = rx(r"(?<![0-9])[0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:[.,][0-9]+)?(?![0-9])")
DATE = rx(r"(?<![0-9])[0-9]{4}-[0-9]{2}-[0-9]{2}(?![0-9])")
HEX_ADDR = rx(r"0x[0-9A-Fa-f]+")
FILE_POS = rx(r"(\.[A-Za-z][A-Za-z0-9+]{0,9}):[0-9]+(?::[0-9]+)?")
FILE_POS_PAREN = rx(r"(\.[A-Za-z][A-Za-z0-9+]{0,9})\([0-9]+(?:,[0-9]+)?\)")
FILE_POS_BRACKET = rx(r"(\.[A-Za-z][A-Za-z0-9+]{0,9}):\[[0-9]+,[0-9]+\]")
LINE_WORD = rx(r"(?<![A-Za-z0-9_])(line|Line|LINE|lines|col|column|Column|col\.)( ?)[0-9]+(?![0-9])")
DURATION = rx(r"(?<![A-Za-z0-9_.])[0-9]+(?:\.[0-9]+)? ?(?:ns|us|\u00b5s|ms|s|sec|secs|seconds|second|m|min|mins|"
              r"minutes|minute|h|hr|hrs|hours|hour)(?![A-Za-z0-9_])")
LONG_HEX = rx(r"(?<![A-Za-z0-9_])[0-9A-Fa-f]{8,}(?![A-Za-z0-9_])")
PID_PORT = rx(r"(?<![A-Za-z0-9_])(pid|PID|port)([ =:#]+)[0-9]+")
SPACES = rx(r"[ \t]+")


def strip_cwd(text, cwd):
    """Removes every `cwd/` that does not follow a path character (5.2)."""
    c = norm_cwd(cwd)
    if not c:
        return text
    body = re.escape(c).replace("/", r"[\\/]") + r"[\\/]"
    flags = re.IGNORECASE | re.ASCII if DRIVE.match(c + "/") else 0
    return re.compile(NOT_IN_PATH + body, flags).sub("", text)


def mask(text, cwd=None):
    """Masked message (5.2): paths relative, volatile parts replaced, spaces collapsed, at most 200 characters."""
    t = TMP_PATH.sub("<tmp>", text)
    t = strip_cwd(t, cwd)
    t = HOME_PATH.sub("~/", t)
    t = UUID.sub("<uuid>", t)
    t = ISO_TIME.sub("<time>", t)
    t = TIME_OF_DAY.sub("<time>", t)
    t = DATE.sub("<date>", t)
    t = HEX_ADDR.sub("0x?", t)
    t = FILE_POS_BRACKET.sub(r"\1:[N]", t)
    t = FILE_POS.sub(r"\1:N", t)
    t = FILE_POS_PAREN.sub(r"\1(N)", t)
    t = LINE_WORD.sub(r"\1\2N", t)
    t = DURATION.sub("<dur>", t)
    t = LONG_HEX.sub("<hex>", t)
    t = PID_PORT.sub(r"\1 N", t)
    t = SPACES.sub(" ", t).strip(" ")
    return t[:MAX_MESSAGE]


def mask_name(text, cwd=None):
    """A test name keeps its text: only paths are made relative and spaces collapsed (5.2)."""
    t = TMP_PATH.sub("<tmp>", text)
    t = strip_cwd(t, cwd)
    t = HOME_PATH.sub("~/", t)
    return SPACES.sub(" ", t).strip(" ")[:MAX_MESSAGE]


def norm_file(path, cwd=None):
    p = path.strip(WS).strip("'\"`")
    if p.startswith("file://"):
        p = p[len("file://"):]
    p = p.replace("\\", "/")
    p = TMP_PATH.sub("<tmp>", p)
    p, _ = cwd_relative(p, cwd)
    p = HOME_PATH.sub("~/", p)
    while "//" in p:
        p = p.replace("//", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


# ============================================================================ failures: recognisers (5.3)

class Found:
    def __init__(self, cwd):
        self.cwd = cwd
        self.fps = []          # canonical strings, in output order
        self.summaries = []    # (framework, family, failed, passed, mode)

    def fp(self, framework, kind, file, code, message=""):
        file = norm_file(file, self.cwd) if file else ""
        message = mask(message, self.cwd) if message else ""
        code = code or ""
        self.fps.append("|".join((framework, kind, file, code, message)))

    def test(self, framework, name, file=""):
        self.fp(framework, "test", file, mask_name(name, self.cwd))

    def error(self, framework, file, code, message):
        self.fp(framework, "error", file, code, message)

    def summary(self, framework, family, failed, passed, mode="last"):
        self.summaries.append((framework, family, failed, passed, mode))


def m(pattern):
    compiled = rx(pattern)
    return compiled.fullmatch


def parse_counts(text, sep=","):
    counts = {}
    for part in text.split(sep):
        mm = re.fullmatch(r"[ \t]*([0-9]+) ([A-Za-z][A-Za-z ]*?)[ \t]*", part)
        if mm:
            counts[mm.group(2)] = counts.get(mm.group(2), 0) + num(mm.group(1))
    return counts


class Recognizer:
    def __init__(self, found):
        self.f = found

    def step(self, lines, i):
        return 0

    def finish(self):
        pass


# ---- pytest
PYTEST_FAIL = m(r"(?:\[gw[0-9]+\] \[ *[0-9]+%\] )?(FAILED|ERROR) (?!\()([^ ]+?)(?: - (.*))?")
PYTEST_VERBOSE = m(r"([^ ]+::.+?) (FAILED|ERROR)(?: +\[ *[0-9]+%\])?")
PYTEST_SUMMARY = m(r"(?:=+ )?((?:[0-9]+ (?:failed|passed|errors?|skipped|xfailed|xpassed|deselected|warnings?|"
                   r"rerun|reruns|subtests passed|subtests failed)(?:, )?)+) in [0-9]+(?:\.[0-9]+)?s"
                   r"(?: \([0-9:.]+\))?(?: =+)?")
PYTEST_NONE = m(r"(?:=+ )?no tests ran in [0-9]+(?:\.[0-9]+)?s(?: =+)?")


class Pytest(Recognizer):
    def step(self, lines, i):
        line = lines[i]
        r = PYTEST_FAIL(line) or PYTEST_VERBOSE(line)
        if r:
            nodeid = r.group(2) if r.re.pattern.startswith("(?:\\[gw") else r.group(1)
            file, _, name = nodeid.partition("::")
            self.f.test("pytest", name, file)
            return 1
        r = PYTEST_SUMMARY(line)
        if r:
            c = parse_counts(r.group(1))
            failed = c.get("failed", 0) + c.get("error", 0) + c.get("errors", 0)
            self.f.summary("pytest", "test", failed, c.get("passed", 0))
            return 1
        if PYTEST_NONE(line):
            self.f.summary("pytest", "test", 0, 0)
            return 1
        return 0


# ---- unittest
UNITTEST_FAIL = m(r"(FAIL|ERROR|UNEXPECTED SUCCESS): ([A-Za-z_][A-Za-z0-9_]*) \(([A-Za-z0-9_.]+)\)(?: .*)?")
UNITTEST_RAN = m(r"Ran ([0-9]+) tests? in [0-9]+(?:\.[0-9]+)?s")
UNITTEST_OK = m(r"OK(?: \((.*)\))?")
UNITTEST_FAILED = m(r"FAILED \((.*)\)")


class Unittest(Recognizer):
    def __init__(self, found):
        super().__init__(found)
        self.ran = None

    def step(self, lines, i):
        line = lines[i]
        r = UNITTEST_FAIL(line)
        if r:
            name, where = r.group(2), r.group(3)
            self.f.test("unittest", where if where.endswith("." + name) else where + "." + name)
            return 1
        r = UNITTEST_RAN(line)
        if r:
            self.ran = num(r.group(1))
            return 1
        if self.ran is not None:
            r = UNITTEST_OK(line) or UNITTEST_FAILED(line)
            if r:
                c = {}
                for part in (r.group(1) or "").split(","):
                    kv = re.fullmatch(r"[ \t]*([a-z ]+)=([0-9]+)[ \t]*", part)
                    if kv:
                        c[kv.group(1)] = num(kv.group(2))
                failed = c.get("failures", 0) + c.get("errors", 0) + c.get("unexpected successes", 0)
                passed = max(0, self.ran - failed - c.get("skipped", 0) - c.get("expected failures", 0))
                self.f.summary("unittest", "test", failed, passed)
                self.ran = None
                return 1
        return 0


# ---- cargo test (libtest) and nextest
LIBTEST_LINE = m(r"test (.+) \.\.\. (FAILED|ok|ignored(?:, .*)?|bench: .*)")
LIBTEST_STDOUT = m(r"---- (.+) (?:stdout|stderr) ----")
LIBTEST_RESULT = m(r"test result: (?:ok|FAILED)\. ([0-9]+) passed; ([0-9]+) failed; ([0-9]+) ignored; ([0-9]+) measured"
                   r"; "
                   r"([0-9]+) filtered out(?:; finished in .*)?")
LIBTEST_FAILURES = m(r"failures:")
LIBTEST_LISTED = m(r"    ([^ ].*)")
NEXTEST_FAIL = m(r" *(?:FAIL|SIGSEGV|SIGABRT|SIGKILL|SIGBUS|TIMEOUT|LEAK-FAIL|ABORT) \[ *[0-9.]+m?s\] (?:\( *[0-9]+/[0-"
                 r"9]+\) )?"
                 r"([^ ]+) (.+)")
NEXTEST_OTHER = m(r" *(?:PASS|SKIP|LEAK|SLOW|FLAKY|START|RETRY|TRY [0-9]+ [A-Z-]+) \[.*")
NEXTEST_SUMMARY = m(r" *Summary \[[^\]]*\] +([0-9]+) tests? run: (.*)")
DOCTEST_LINE = rx(r"\(line [0-9]+\)")


def libtest_name(name):
    return DOCTEST_LINE.sub("(line N)", name)


class Cargo(Recognizer):
    def __init__(self, found):
        super().__init__(found)
        self.listing = False

    def step(self, lines, i):
        line = lines[i]
        if self.listing:
            r = LIBTEST_LISTED(line)
            if r and " " not in r.group(1).split(" - ")[0]:
                self.f.test("cargo", libtest_name(r.group(1)))
                return 1
            self.listing = False
        r = LIBTEST_LINE(line)
        if r:
            if r.group(2) == "FAILED":
                self.f.test("cargo", libtest_name(r.group(1)))
            return 1
        r = LIBTEST_STDOUT(line)
        if r:
            self.f.test("cargo", libtest_name(r.group(1)))
            return 1
        r = LIBTEST_RESULT(line)
        if r:
            self.f.summary("cargo", "test", num(r.group(2)), num(r.group(1)), "sum")
            return 1
        if LIBTEST_FAILURES(line) and i + 1 < len(lines) and LIBTEST_LISTED(lines[i + 1]):
            self.listing = True
            return 1
        r = NEXTEST_FAIL(line)
        if r:
            self.f.test("cargo", libtest_name(r.group(2)))
            return 1
        if NEXTEST_OTHER(line):
            return 1
        r = NEXTEST_SUMMARY(line)
        if r:
            c = parse_counts(re.sub(r" \([^)]*\)", "", r.group(2)))
            failed = c.get("failed", 0) + c.get("timed out", 0) + c.get("exec failed", 0) + c.get("leaky failed", 0)
            self.f.summary("nextest", "test", failed, c.get("passed", 0))
            return 1
        return 0


# ---- rustc / cargo build
RUST_HEADER = m(r"error(?:\[(E[0-9]{4})\])?: (.+)")
RUST_WARN = m(r"warning(?:\[[A-Za-z0-9_:]+\])?: .+")
RUST_LOC = m(r" *--> (.+?):([0-9]+):([0-9]+)")
RUST_COULD_NOT = m(r"error: could not compile `[^`]+`(?: \([^)]*\))?(?:;.*)?(?: due to ([0-9]+) previous errors?|"
                   r" due to previous error)?(?:;.*)?")
RUST_ABORTING = m(r"error: aborting due to (?:([0-9]+) )?previous errors?(?:;.*)?")
RUST_STATUS = m(r"error: (?:test failed|build failed|failed to run|[0-9]+ targets? failed|process didn't exit "
                r"successfully|Recipe ).*")


def rust_location(lines, i):
    """Index of the `-->` line within the 3 lines after a header, when no other header comes first."""
    for k in range(i + 1, min(i + 4, len(lines))):
        if RUST_LOC(lines[k]):
            return k
        if RUST_HEADER(lines[k]) or RUST_WARN(lines[k]):
            return None
    return None


class Rustc(Recognizer):
    def step(self, lines, i):
        line = lines[i]
        r = RUST_COULD_NOT(line)
        if r:
            self.f.summary("rustc", "error", num(r.group(1) or "1"), None, "sum")
            return 1
        r = RUST_ABORTING(line)
        if r:
            self.f.summary("rustc", "error", num(r.group(1) or "1"), None, "sum")
            return 1
        if RUST_STATUS(line):
            return 1
        r = RUST_HEADER(line)
        if r:
            k = rust_location(lines, i)
            if k is not None:
                self.f.error("rustc", RUST_LOC(lines[k]).group(1), r.group(1), r.group(2))
                return k - i + 1
            return 0
        if RUST_WARN(line):
            k = rust_location(lines, i)
            return 0 if k is None else k - i + 1
        return 0


# ---- swift-testing, XCTest, xcodebuild, swiftc
ST_MARK = r"[^ \tA-Za-z0-9]+ "
ST_ISSUE = m(ST_MARK + r"Test (.+?) recorded an issue.*")
ST_FAILED = m(ST_MARK + r"Test (.+?) failed after [0-9]+(?:\.[0-9]+)? seconds(?: with [0-9]+ issues?)?"
              r"(?: \(including [0-9]+ known issues?\))?\.")
ST_OTHER = m(ST_MARK + r"(?:Test .+? (?:passed after .*|started\.|skipped.*)|Suite .+? (?:passed|failed|started).*|"
             r"Test run started\.|Testing Library Version: .*|Target Platform: .*)")
ST_RUN = m(ST_MARK + r"Test run with ([0-9]+) tests?(?: in [0-9]+ suites?)? (passed|failed) after [0-9]+(?:\.[0-9]+)? "
           r"seconds(?: with ([0-9]+) issues?)?(?: \(including [0-9]+ known issues?\))?\.")
ST_CASES = rx(r" with [0-9]+ test cases?$")
XC_CASE = m(r"Test [Cc]ase '(-\[[^\]]+\]|[^']+)' (failed|passed|started|skipped)(?: on '[^']*')?(?: \([0-9.]+ seconds\)"
            r")?\.?")
XC_ERROR = m(r"(.+?):([0-9]+): error: (-\[[^\]]+\]|[A-Za-z0-9_.()/]+) : .*")
XC_SUITE = m(r"Test [Ss]uite '.+' (?:started|passed|failed).*")
XC_EXECUTED = m(r"[ \t]*Executed ([0-9]+) tests?, with (?:([0-9]+) tests? skipped and )?([0-9]+) failures? \(([0-9]+) "
                r"unexpected\) in [0-9.]+ \([0-9.]+\) seconds")
XB_FAILING = m(r"Failing tests:")
XB_LISTED = m(r"[ \t]+(.+?)(?: \(.*\))?")
XB_RESULT = m(r"\*\* (?:TEST|BUILD|ANALYZE|ARCHIVE|CLEAN|TEST BUILD|INSTALL) (?:FAILED|SUCCEEDED|INTERRUPTED) \*\*")
XB_COMMANDS = m(r"The following build commands failed:")
XB_COUNT = m(r"\(([0-9]+) failures?\)")
SWIFTC_ERROR = m(r"(.+\.swift):([0-9]+):([0-9]+): error: (.+)")
SWIFTC_WARN = m(r"(.+\.swift):([0-9]+):([0-9]+): (?:warning|note): .+")
SWIFT_NOISE = m(r"error: (?:emit-module command failed|fatalError|compile command failed|link command failed).*")


def xctest_id(name):
    s = name
    if s.startswith("-[") and s.endswith("]"):
        s = s[2:-1]
    s = s.replace(" ", ".").replace("/", ".").replace("()", "")
    parts = [p for p in s.split(".") if p]
    return ".".join(parts[-2:])


class Swift(Recognizer):
    def __init__(self, found):
        super().__init__(found)
        self.st_failed = set()
        self.xc_failed = set()
        self.listing = False
        self.commands = False

    def step(self, lines, i):
        line = lines[i]
        if self.listing:
            r = XB_LISTED(line)
            if r:
                if not r.group(1).endswith(":"):
                    test = xctest_id(r.group(1))
                    self.xc_failed.add(test)
                    self.f.test("xctest", test)
                return 1
            self.listing = False
        if self.commands:
            r = XB_COUNT(line)
            if r:
                self.f.summary("xcodebuild", "error", num(r.group(1)), None)
                self.commands = False
                return 1
            if line.startswith((" ", "\t")):
                return 1
            self.commands = False
        r = ST_FAILED(line)
        if r and not r.group(1).startswith("run with "):
            name = ST_CASES.sub("", r.group(1))
            self.st_failed.add(name)
            self.f.test("swift-testing", name)
            return 1
        r = ST_RUN(line)
        if r:
            total = num(r.group(1))
            if r.group(2) == "passed":
                failed = 0
            else:
                failed = len(self.st_failed) or num(r.group(3) or "1")
            self.f.summary("swift-testing", "test", failed, max(0, total - failed))
            return 1
        if ST_ISSUE(line) or ST_OTHER(line):
            return 1
        r = XC_CASE(line)
        if r:
            if r.group(2) == "failed":
                test = xctest_id(r.group(1))
                self.xc_failed.add(test)
                self.f.test("xctest", test)
            return 1
        r = XC_ERROR(line)
        if r:
            test = xctest_id(r.group(3))
            self.xc_failed.add(test)
            self.f.test("xctest", test)
            return 1
        if XC_SUITE(line):
            return 1
        r = XC_EXECUTED(line)
        if r:
            total, skipped, failures = num(r.group(1)), num(r.group(2) or "0"), num(r.group(3))
            failed = len(self.xc_failed) if self.xc_failed else failures
            self.f.summary("xctest", "test", failed, max(0, total - failed - skipped))
            return 1
        if XB_FAILING(line):
            self.listing = True
            return 1
        if XB_RESULT(line):
            return 1
        if XB_COMMANDS(line):
            self.commands = True
            return 1
        r = SWIFTC_ERROR(line)
        if r:
            self.f.error("swiftc", r.group(1), "", r.group(4))
            return 1
        if SWIFTC_WARN(line) or SWIFT_NOISE(line):
            return 1
        return 0


# ---- go
GO_FAIL = m(r"[ \t]*--- FAIL: ([^ ]+) \([0-9.]+s\)")
GO_PASS = m(r"[ \t]*--- (PASS|SKIP): ([^ ]+) \([0-9.]+s\)")
GO_RUN = m(r"=== (?:RUN|PAUSE|CONT|NAME) .*")
GO_PKG = m(r"(FAIL|ok)[ \t]+([^ \t]+)[ \t]+(?:[0-9.]+s|\(cached\)|\[([^\]]+)\])(?:[ \t].*)?")
GO_BARE = m(r"(?:FAIL|PASS|\?[ \t]+[^ \t]+[ \t]+\[no test files\])")
GO_ERROR = m(r"([^ \t][^ \t:]*\.go):([0-9]+)(?::([0-9]+))?: (.+)")
GO_HEADER = m(r"# [^ ]+(?: \[[^\]]+\])?")


class Go(Recognizer):
    def __init__(self, found):
        super().__init__(found)
        self.failed, self.passed, self.ok, self.pkgs, self.broken = set(), set(), 0, 0, 0

    def step(self, lines, i):
        line = lines[i]
        r = GO_FAIL(line)
        if r:
            self.failed.add(r.group(1))
            self.f.test("go", r.group(1))
            return 1
        r = GO_PASS(line)
        if r:
            if r.group(1) == "PASS":
                self.passed.add(r.group(2))
            return 1
        if GO_RUN(line) or GO_BARE(line) or GO_HEADER(line):
            return 1
        r = GO_PKG(line)
        if r:
            self.pkgs += 1
            if r.group(1) == "ok":
                self.ok += 1
            elif r.group(3) in ("build failed", "setup failed"):
                self.broken += 1
            return 1
        r = GO_ERROR(line)
        if r:
            self.f.error("go", r.group(1), "", r.group(4))
            return 1
        return 0

    def finish(self):
        if self.pkgs or self.failed or self.passed:
            passed = len(self.passed) if self.passed else (self.ok if self.ok else None)
            self.f.summary("go", "test", len(self.failed) + self.broken, passed)


# ---- jest, vitest, mocha, bun, deno
JEST_FILE = m(r" ?(FAIL|PASS) +([^ ]+)(?: \([0-9.]+ ?m?s\))?")
JEST_BULLET = m(r"  ● (.+)")
JEST_TESTS = m(r"Tests:[ \t]+(.*[0-9]+ total)")
JEST_SUITES = m(r"Test Suites:[ \t]+(.*[0-9]+ total)")
JEST_NOISE = m(r"(?:Snapshots:|Time:|Ran all test suites).*|[ \t]+[✓✕○✎] .*|Summary of all failing tests")
VITEST_FAIL = m(r" *FAIL +(.+?) > (.+?)(?: \[[^\]]*\])?")
VITEST_LOAD = m(r" *FAIL +([^ ]+) \[ (.+) \]")
VITEST_FILE = m(r" *[❯>✓] ([^ ]+) \([0-9]+ tests?(?: \|.*)?\)(?: [0-9.]+m?s)?")
VITEST_X = m(r" +[×✗] (.+?)(?: [0-9.]+m?s)?")
VITEST_TESTS = m(r" *Tests +(.+?) \(([0-9]+)\)")
VITEST_FILES = m(r" *Test Files +(.+?) \(([0-9]+)\)")
MOCHA_PASSING = m(r"[ \t]*([0-9]+) passing \([0-9a-z.]+\)")
MOCHA_FAILING = m(r"[ \t]*([0-9]+) failing")
MOCHA_PENDING = m(r"[ \t]*([0-9]+) pending")
MOCHA_ITEM = m(r"([ \t]*)([0-9]+)\) (.*)")
BUN_FAIL = m(r"\(fail\) (.+?)(?: \[[0-9.]+m?s\])?")
BUN_COUNT = m(r" *([0-9]+) (pass|fail)")
DENO_SUMMARY = m(r"(?:ok|FAILED) \| ([0-9]+) passed(?: \([0-9]+ steps?\))? \| ([0-9]+) failed(?: \([0-9]+ steps?\))?.*")
DENO_FAILURE = m(r"(.+?) => (.+?):[0-9]+:[0-9]+")


class Js(Recognizer):
    def __init__(self, found):
        super().__init__(found)
        self.jest_file = ""
        self.jest_tests = self.jest_suites = None
        self.vitest_file = ""
        self.vitest_tests = self.vitest_files = None
        self.mocha_passing = self.mocha_failing = None
        self.mocha_details = False
        self.bun = {}

    def step(self, lines, i):
        line = lines[i]
        if self.mocha_details:
            r = MOCHA_ITEM(line)
            if r:
                parts, k = [r.group(3)], i
                while not parts[-1].endswith(":") and k + 1 < len(lines) and lines[k + 1].strip(WS) \
                        and len(lines[k + 1]) - len(lines[k + 1].lstrip(" ")) > len(r.group(1)):
                    k += 1
                    parts.append(lines[k].strip(WS))
                title = " ".join(p.strip(WS) for p in parts)
                if title.endswith(":"):
                    self.f.test("mocha", title[:-1])
                    return k - i + 1
        r = VITEST_LOAD(line)
        if r:
            self.f.test("vitest", "", r.group(1))
            return 1
        r = VITEST_FAIL(line)
        if r:
            self.f.test("vitest", r.group(2), r.group(1))
            return 1
        r = JEST_FILE(line)
        if r:
            self.jest_file = r.group(2) if r.group(1) == "FAIL" else ""
            return 1
        r = JEST_BULLET(line)
        if r:
            if r.group(1) != "Console":
                self.f.test("jest", r.group(1), self.jest_file)
            return 1
        r = JEST_SUITES(line)
        if r:
            self.flush_jest()
            self.jest_suites = parse_counts(r.group(1))
            return 1
        r = JEST_TESTS(line)
        if r:
            self.jest_tests = parse_counts(r.group(1))
            self.flush_jest()
            return 1
        if JEST_NOISE(line):
            return 1
        r = VITEST_FILE(line)
        if r:
            self.vitest_file = r.group(1)
            return 1
        r = VITEST_X(line)
        if r and self.vitest_file:
            self.f.test("vitest", r.group(1), self.vitest_file)
            return 1
        r = VITEST_FILES(line)
        if r:
            self.flush_vitest()
            self.vitest_files = parse_counts(r.group(1), "|")
            return 1
        r = VITEST_TESTS(line)
        if r:
            self.vitest_tests = parse_counts(r.group(1), "|")
            self.flush_vitest()
            return 1
        r = MOCHA_PASSING(line)
        if r:
            self.mocha_passing = num(r.group(1))
            return 1
        r = MOCHA_FAILING(line)
        if r:
            self.mocha_failing = num(r.group(1))
            self.mocha_details = True
            return 1
        if MOCHA_PENDING(line):
            return 1
        r = BUN_FAIL(line)
        if r:
            self.f.test("bun", r.group(1))
            return 1
        r = BUN_COUNT(line)
        if r:
            self.bun[r.group(2)] = num(r.group(1))
            return 1
        r = DENO_SUMMARY(line)
        if r:
            self.f.summary("deno", "test", num(r.group(2)), num(r.group(1)))
            return 1
        r = DENO_FAILURE(line)
        if r:
            self.f.test("deno", r.group(1), r.group(2))
            return 1
        return 0

    def flush(self, framework, tests, files):
        if tests is None and files is None:
            return
        failed = (tests or {}).get("failed", 0)
        if failed == 0 and files:
            failed = files.get("failed", 0)
        self.f.summary(framework, "test", failed, (tests or {}).get("passed", 0), "sum")

    def flush_jest(self):
        self.flush("jest", self.jest_tests, self.jest_suites)
        self.jest_tests = self.jest_suites = None

    def flush_vitest(self):
        self.flush("vitest", self.vitest_tests, self.vitest_files)
        self.vitest_tests = self.vitest_files = None

    def finish(self):
        self.flush_jest()
        self.flush_vitest()
        if self.mocha_passing is not None or self.mocha_failing is not None:
            self.f.summary("mocha", "test", self.mocha_failing or 0, self.mocha_passing or 0)
        if self.bun:
            self.f.summary("bun", "test", self.bun.get("fail", 0), self.bun.get("pass", 0))


# ---- tsc, eslint, ruff, black, mypy, pyright
TSC_PAREN = m(r"(.+?)\(([0-9]+),([0-9]+)\): error (TS[0-9]+): (.*)")
TSC_PRETTY = m(r"(.+?):([0-9]+):([0-9]+) - error (TS[0-9]+): (.*)")
TSC_FOUND = m(r"Found ([0-9]+) errors? in .*")
FOUND_ERRORS = m(r"Found ([0-9]+) errors?(?: \(([0-9]+) fixed, ([0-9]+) remaining\))?\.")
TSC_TABLE = m(r"Errors +Files")
TSC_ROW = m(r"[ \t]+[0-9]+[ \t]+.+:[0-9]+")
ESLINT_FILE = m(r"(/[^:]*|[A-Za-z]:[\\/][^:]*)")
ESLINT_MSG = m(r"[ \t]+([0-9]+):([0-9]+)[ \t]+(error|warning)[ \t]+(.+?)(?:[ \t]{2,}([^ \t]+))?")
ESLINT_UNIX = m(r"(.+?):([0-9]+):([0-9]+): (.+) \[(Error|Warning)/(.+)\]")
ESLINT_SUMMARY = m(r"✖ ([0-9]+) problems? \(([0-9]+) errors?, ([0-9]+) warnings?\)")
ESLINT_FIXABLE = m(r"[ \t]+[0-9]+ errors? and [0-9]+ warnings? potentially fixable.*")
RUFF_CONCISE = m(r"(.+?\.(?:py|pyi|ipynb)):([0-9]+):([0-9]+): ([A-Z]+[0-9]+|SyntaxError|invalid-syntax):? (?:\[\*\] )?("
                 r".*)")
RUFF_HEADER = m(r"([A-Z]{1,4}[0-9]{1,4}|invalid-syntax|SyntaxError):? (?:\[\*\] )?(.+)")
RUFF_LOC = m(r" *--> (.+?):([0-9]+):([0-9]+)")
RUFF_PASSED = m(r"All checks passed!")
RUFF_WOULD = m(r"Would reformat: (.+)")
BLACK_WOULD = m(r"would reformat (.+)")
FORMAT_SUMMARY = m(r"(?:Oh no! .*)?([0-9]+) files? would be reformatted(?:, [0-9]+ files? (?:already formatted|would be"
                   r" "
                   r"left unchanged))?\.?")
MYPY_ERROR = m(r"(.+?\.pyi?):([0-9]+)(?::([0-9]+))?: error: (.+?)(?:  \[([a-z0-9-]+)\])?")
MYPY_NOTE = m(r"(.+?\.pyi?):([0-9]+)(?::([0-9]+))?: note: .*")
MYPY_FOUND = m(r"Found ([0-9]+) errors? in [0-9]+ files? \((?:checked [0-9]+ source files?|errors prevented further "
               r"checking)\)")
MYPY_SUCCESS = m(r"Success: no issues found in [0-9]+ source files?")
PYRIGHT_ERROR = m(r"[ \t]*(.+?):([0-9]+):([0-9]+) - error: (.+?)(?: \((report[A-Za-z]+)\))?")
PYRIGHT_RULE = rx(r" \((report[A-Za-z]+)\)$")
PYRIGHT_OTHER = m(r"[ \t]*(.+?):([0-9]+):([0-9]+) - (?:warning|information|info|note): .*")
PYRIGHT_SUMMARY = m(r"([0-9]+) errors?, ([0-9]+) warnings?, ([0-9]+) (?:informations?|infos?|notes?) ?")


class Checkers(Recognizer):
    def __init__(self, found):
        super().__init__(found)
        self.eslint_file = None
        self.tsc_table = False

    def step(self, lines, i):
        line = lines[i]
        if self.tsc_table:
            if TSC_ROW(line):
                return 1
            self.tsc_table = False
        r = TSC_PAREN(line) or TSC_PRETTY(line)
        if r:
            self.f.error("tsc", r.group(1), r.group(4), r.group(5))
            return 1
        r = MYPY_FOUND(line)
        if r:
            self.f.summary("mypy", "error", num(r.group(1)), None)
            return 1
        r = TSC_FOUND(line)
        if r:
            self.f.summary("tsc", "error", num(r.group(1)), None)
            return 1
        r = FOUND_ERRORS(line)
        if r:
            failed = num(r.group(3)) if r.group(3) is not None else num(r.group(1))
            self.f.summary("found-errors", "error", failed, None)
            return 1
        if TSC_TABLE(line):
            self.tsc_table = True
            return 1
        r = ESLINT_UNIX(line)
        if r:
            if r.group(5) == "Error":
                self.f.error("eslint", r.group(1), r.group(6), r.group(4))
            return 1
        if ESLINT_FILE(line) and i + 1 < len(lines) and ESLINT_MSG(lines[i + 1]):
            self.eslint_file = line
            return 1
        if self.eslint_file is not None:
            r = ESLINT_MSG(line)
            if r:
                if r.group(3) == "error":
                    self.f.error("eslint", self.eslint_file, r.group(5) or "", r.group(4))
                return 1
        r = ESLINT_SUMMARY(line)
        if r:
            self.f.summary("eslint", "error", num(r.group(2)), None)
            return 1
        if ESLINT_FIXABLE(line):
            return 1
        r = RUFF_CONCISE(line)
        if r:
            self.f.error("ruff", r.group(1), r.group(4), r.group(5))
            return 1
        r = RUFF_HEADER(line)
        if r:
            for k in range(i + 1, min(i + 4, len(lines))):
                loc = RUFF_LOC(lines[k])
                if loc:
                    self.f.error("ruff", loc.group(1), r.group(1), r.group(2))
                    return k - i + 1
        if RUFF_PASSED(line):
            self.f.summary("ruff", "error", 0, None)
            return 1
        r = RUFF_WOULD(line)
        if r:
            self.f.error("ruff", r.group(1), "format", "")
            return 1
        r = BLACK_WOULD(line)
        if r:
            self.f.error("black", r.group(1), "format", "")
            return 1
        r = FORMAT_SUMMARY(line)
        if r:
            self.f.summary("format", "error", num(r.group(1)), None)
            return 1
        r = MYPY_ERROR(line)
        if r:
            self.f.error("mypy", r.group(1), r.group(5) or "", r.group(4))
            return 1
        if MYPY_NOTE(line):
            return 1
        if MYPY_SUCCESS(line):
            self.f.summary("mypy", "error", 0, None)
            return 1
        r = PYRIGHT_ERROR(line)
        if r:
            indent = len(line) - len(line.lstrip(" \t"))
            k, code = i, r.group(5) or ""
            while k + 1 < len(lines) and len(lines[k + 1]) - len(lines[k + 1].lstrip(" \t")) > indent \
                    and not PYRIGHT_ERROR(lines[k + 1]) and not PYRIGHT_OTHER(lines[k + 1]):
                k += 1
            if k > i and not code:
                tail = PYRIGHT_RULE.search(lines[k])
                code = tail.group(1) if tail else ""
            self.f.error("pyright", r.group(1), code, PYRIGHT_RULE.sub("", r.group(4)))
            return k - i + 1
        if PYRIGHT_OTHER(line):
            return 1
        r = PYRIGHT_SUMMARY(line)
        if r:
            self.f.summary("pyright", "error", num(r.group(1)), None)
            return 1
        return 0


# ---- .NET
MSB_ERROR = m(r"[ \t]*(.+?)\(([0-9]+)(?:,([0-9]+))?(?:,[0-9]+,[0-9]+)?\): error ([A-Za-z]+[0-9]+): (.*?)(?: \[([^\]]+)"
              r"\])?")
MSB_TOOL_ERROR = m(r"[ \t]*([A-Za-z][A-Za-z0-9_.-]*) : error ([A-Za-z]+[0-9]+): (.*?)(?: \[([^\]]+)\])?")
MSB_WARNING = m(r"[ \t]*.+?(?:\([0-9,]+\))? : warning [A-Za-z]+[0-9]+: .*|[ \t]*.+?\([0-9,]+\): warning [A-Za-z]+[0-9]+"
                r": .*")
MSB_COUNT = m(r"[ \t]*([0-9]+) Error\(s\)")
MSB_NOISE = m(r"[ \t]*[0-9]+ Warning\(s\)|Build FAILED\.|Build succeeded\.|Time Elapsed .*")
MSB_TERMINAL = m(r"Build (?:failed|succeeded)(?: with ([0-9]+) error\(s\)(?: and [0-9]+ warning\(s\))?)?"
                 r"(?: and [0-9]+ warning\(s\))? in [0-9.]+s")
VSTEST_FAILED = m(r"[ \t]*Failed ([^ ]+) \[[^\]]*\]")
VSTEST_PASSED = m(r"[ \t]*(?:Passed|Skipped) ([^ ]+) \[[^\]]*\]")
VSTEST_SUMMARY = m(r"(?:Failed|Passed)![ \t]*-[ \t]*Failed:[ \t]*([0-9]+),[ \t]*Passed:[ \t]*([0-9]+),[ \t]*Skipped:[ "
                   r"\t]*"
                   r"([0-9]+),[ \t]*Total:[ \t]*([0-9]+).*")
MTP_SUMMARY = m(r"[ \t]*Test summary: total: ([0-9]+), failed: ([0-9]+), succeeded: ([0-9]+), skipped: ([0-9]+).*")
MTP_FAILED = m(r"[ \t]*failed (.+?) \([0-9.]+m?s\)")


class Dotnet(Recognizer):
    def step(self, lines, i):
        line = lines[i]
        r = MSB_ERROR(line)
        if r:
            self.f.error("msbuild", r.group(1), r.group(4), r.group(5))
            return 1
        r = MSB_TOOL_ERROR(line)
        if r:
            self.f.error("msbuild", "", r.group(2), r.group(3))
            return 1
        if MSB_WARNING(line):
            return 1
        r = MSB_COUNT(line)
        if r:
            self.f.summary("msbuild", "error", num(r.group(1)), None)
            return 1
        r = MSB_TERMINAL(line)
        if r:
            if r.group(1):
                self.f.summary("msbuild", "error", num(r.group(1)), None)
            return 1
        if MSB_NOISE(line):
            return 1
        r = VSTEST_FAILED(line)
        if r:
            self.f.test("dotnet", r.group(1))
            return 1
        if VSTEST_PASSED(line):
            return 1
        r = VSTEST_SUMMARY(line)
        if r:
            self.f.summary("dotnet", "test", num(r.group(1)), num(r.group(2)), "sum")
            return 1
        r = MTP_SUMMARY(line)
        if r:
            self.f.summary("dotnet-mtp", "test", num(r.group(2)), num(r.group(3)))
            return 1
        r = MTP_FAILED(line)
        if r:
            self.f.test("dotnet", r.group(1))
            return 1
        return 0


# ---- JVM: JUnit via Gradle and Surefire, javac, kotlinc
GRADLE_TEST = m(r"([^ ]+) > (.+?) (FAILED|PASSED|SKIPPED)")
GRADLE_SUMMARY = m(r"([0-9]+) tests? completed, ([0-9]+) failed(?:, ([0-9]+) skipped)?")
GRADLE_NOISE = m(r"> Task [^ ]+(?: [A-Z-]+)?|FAILURE: Build failed with an exception\.|BUILD (?:SUCCESSFUL|FAILED) in ."
                 r"*|"
                 r"\* What went wrong:|\* Try:")
SUREFIRE_TEST = m(r"\[ERROR\] ([A-Za-z0-9_.$]+)\.([A-Za-z0-9_$\[\]]+)(?:\(.*\))? -- Time elapsed: .* <<< (?:FAILURE|ERR"
                  r"OR)!")
SUREFIRE_OLD = m(r"(?:\[ERROR\] )?([A-Za-z0-9_$]+)\(([A-Za-z0-9_.$]+)\)[ \t]+Time elapsed: .* <<< (?:FAILURE|ERROR)!")
SUREFIRE_HEADER = m(r"\[ERROR\] (?:Failures|Errors): ?")
SUREFIRE_LISTED = m(r"\[ERROR\][ \t]+([A-Za-z0-9_$]+)\.([A-Za-z0-9_$\[\]]+)(?::[0-9]+)?(?:[ \t].*)?")
SUREFIRE_RUN = m(r"\[(?:ERROR|WARNING|INFO)\] Tests run: ([0-9]+), Failures: ([0-9]+), Errors: ([0-9]+), Skipped: "
                 r"([0-9]+)(?:, Flakes: [0-9]+)?(.*)")
MAVEN_JAVAC = m(r"\[ERROR\] (.+?\.(?:java|kt|scala)):\[([0-9]+),([0-9]+)\] (.+)")
MAVEN_ERRORS = m(r"\[INFO\] ([0-9]+) errors?[ \t]*")
MAVEN_NOISE = m(r"\[ERROR\] COMPILATION ERROR :?|\[INFO\] BUILD (?:FAILURE|SUCCESS)")
JAVAC_ERROR = m(r"(.+?\.java):([0-9]+): error: (.+)")
JAVAC_WARNING = m(r"(.+?\.java):([0-9]+): warning: .*")
JAVAC_COUNT = m(r"([0-9]+) errors?")
KOTLIN_ERROR = m(r"e: (?:file://)?(.+?\.kts?):([0-9]+):([0-9]+) (.+)")
KOTLIN_ERROR_OLD = m(r"e: (.+?\.kts?): \(([0-9]+), ([0-9]+)\): (.+)")
KOTLIN_WARNING = m(r"w: .*")


def junit_id(cls, method):
    simple = cls.split(".")[-1]
    method = method[:-2] if method.endswith("()") else method
    return simple + "." + method


class Jvm(Recognizer):
    def __init__(self, found):
        super().__init__(found)
        self.listing = False

    def step(self, lines, i):
        line = lines[i]
        if self.listing:
            if SUREFIRE_RUN(line) or line.startswith("[INFO]") or line in ("[ERROR] ", "[ERROR]"):
                self.listing = False
            else:
                r = SUREFIRE_LISTED(line)
                if r and not line.startswith("[ERROR]   Run "):
                    self.f.test("junit", junit_id(r.group(1), r.group(2)))
                    return 1
                if line.startswith("[ERROR]") or line.startswith("[WARNING]"):
                    return 1
                self.listing = False
        r = GRADLE_TEST(line)
        if r:
            if r.group(3) == "FAILED":
                self.f.test("junit", junit_id(r.group(1), r.group(2)))
            return 1
        r = GRADLE_SUMMARY(line)
        if r:
            total, failed, skipped = num(r.group(1)), num(r.group(2)), num(r.group(3) or "0")
            self.f.summary("gradle", "test", failed, max(0, total - failed - skipped), "sum")
            return 1
        if GRADLE_NOISE(line):
            return 1
        r = SUREFIRE_TEST(line)
        if r:
            self.f.test("junit", junit_id(r.group(1), r.group(2)))
            return 1
        r = SUREFIRE_OLD(line)
        if r:
            self.f.test("junit", junit_id(r.group(2), r.group(1)))
            return 1
        if SUREFIRE_HEADER(line):
            self.listing = True
            return 1
        r = SUREFIRE_RUN(line)
        if r:
            if r.group(5).strip(WS) == "":
                run, failures, errors, skipped = (num(r.group(k)) for k in (1, 2, 3, 4))
                self.f.summary("maven", "test", failures + errors, max(0, run - failures - errors - skipped))
            return 1
        r = MAVEN_JAVAC(line)
        if r:
            self.f.error("javac", r.group(1), "", r.group(4))
            return 1
        r = MAVEN_ERRORS(line)
        if r:
            self.f.summary("javac", "error", num(r.group(1)), None)
            return 1
        if MAVEN_NOISE(line):
            return 1
        r = JAVAC_ERROR(line)
        if r:
            self.f.error("javac", r.group(1), "", r.group(3))
            return 1
        if JAVAC_WARNING(line):
            return 1
        r = JAVAC_COUNT(line)
        if r:
            self.f.summary("javac", "error", num(r.group(1)), None)
            return 1
        r = KOTLIN_ERROR(line) or KOTLIN_ERROR_OLD(line)
        if r:
            self.f.error("kotlinc", r.group(1), "", r.group(4))
            return 1
        if KOTLIN_WARNING(line):
            return 1
        return 0


# ---- rspec, phpunit, ctest
RSPEC_FAILED = m(r"rspec (?:\./)?(.+?)(?::[0-9]+|\[[0-9:]+\]) # (.*)")
RSPEC_SUMMARY = m(r"([0-9]+) examples?, ([0-9]+) failures?(?:, ([0-9]+) pending)?(?:, ([0-9]+) errors? occurred outside"
                  r" of "
                  r"examples)?")
RSPEC_LOAD = m(r"An error occurred while loading (?:\./)?(.+?)\.?")
PHPUNIT_SECTION = m(r"There (?:was|were) [0-9]+ ([a-z][a-z ]*?):")
PHPUNIT_ITEM = m(r"[0-9]+\) ([A-Za-z0-9_\\]+)::([A-Za-z0-9_]+)(?: with data set (#[0-9]+|\"[^\"]*\"))?(?: \(.*\))?")
PHPUNIT_TESTS = m(r"Tests: ([0-9]+), Assertions: [0-9]+((?:, [A-Za-z ]+: [0-9]+)*)\.")
PHPUNIT_OK = m(r"OK \(([0-9]+) tests?, [0-9]+ assertions?\)")
PHPUNIT_NOISE = m(r"FAILURES!|ERRORS!|OK, but (?:there were issues|incomplete, skipped, or risky tests)!")
CTEST_LINE = m(r" *[0-9]+/[0-9]+ Test +#[0-9]+: (.+?) \.*(?: +|\*\*\*)(Passed|Failed|Exception|Timeout|Not Run|Skipped)"
               r"(?:[: ].*)?")
CTEST_LIST_HEADER = m(r"The following tests (?:FAILED|did not run):")
CTEST_LISTED = m(r"[ \t]+[0-9]+ - (.+?) \((.+)\)")
CTEST_SUMMARY = m(r"([0-9]+)% tests passed, ([0-9]+) tests? failed out of ([0-9]+)")
CTEST_NOISE = m(r" *Start +[0-9]+: .*|Test project .*|Errors while running CTest|Total Test time .*")


class Misc(Recognizer):
    def __init__(self, found):
        super().__init__(found)
        self.php_section = None
        self.ctest_list = None

    def step(self, lines, i):
        line = lines[i]
        if self.ctest_list is not None:
            r = CTEST_LISTED(line)
            if r:
                if self.ctest_list == "FAILED":
                    self.f.test("ctest", r.group(1))
                return 1
            self.ctest_list = None
        r = RSPEC_FAILED(line)
        if r:
            self.f.test("rspec", r.group(2), r.group(1))
            return 1
        r = RSPEC_SUMMARY(line)
        if r:
            examples, failures = num(r.group(1)), num(r.group(2))
            pending, outside = num(r.group(3) or "0"), num(r.group(4) or "0")
            self.f.summary("rspec", "test", failures + outside, max(0, examples - failures - pending))
            return 1
        r = RSPEC_LOAD(line)
        if r:
            self.f.error("rspec", r.group(1), "load", "")
            return 1
        r = PHPUNIT_SECTION(line)
        if r:
            self.php_section = r.group(1)
            return 1
        r = PHPUNIT_ITEM(line)
        if r:
            if self.php_section in ("failure", "failures", "error", "errors"):
                name = r.group(1) + "::" + r.group(2) + (" " + r.group(3) if r.group(3) else "")
                self.f.test("phpunit", name)
            return 1
        r = PHPUNIT_TESTS(line)
        if r:
            c = {}
            for part in r.group(2).split(", "):
                kv = re.fullmatch(r"([A-Za-z ]+): ([0-9]+)", part)
                if kv:
                    c[kv.group(1)] = num(kv.group(2))
            failed = c.get("Errors", 0) + c.get("Failures", 0)
            passed = max(0, num(r.group(1)) - failed - c.get("Skipped", 0) - c.get("Incomplete", 0))
            self.f.summary("phpunit", "test", failed, passed)
            return 1
        r = PHPUNIT_OK(line)
        if r:
            self.f.summary("phpunit", "test", 0, num(r.group(1)))
            return 1
        if PHPUNIT_NOISE(line):
            return 1
        r = CTEST_LINE(line)
        if r:
            if r.group(2) in ("Failed", "Exception", "Timeout"):
                self.f.test("ctest", r.group(1))
            return 1
        r = CTEST_LIST_HEADER(line)
        if r:
            self.ctest_list = "FAILED" if "FAILED" in line else "notrun"
            return 1
        r = CTEST_SUMMARY(line)
        if r:
            failed, total = num(r.group(2)), num(r.group(3))
            self.f.summary("ctest", "test", failed, max(0, total - failed))
            return 1
        if CTEST_NOISE(line):
            return 1
        return 0


# ---- C and C++ compilers, linkers, make
CC_EXT = r"(?:c|cc|cpp|cxx|c\+\+|C|h|hh|hpp|hxx|h\+\+|m|mm|cu|cuh|ino|S|s|ipp|tpp|inl)"
CC_ERROR = m(r"(.+?\." + CC_EXT + r"):([0-9]+)(?::([0-9]+))?: (?:fatal )?error: (.+?)(?: \[([^\]]+)\])?")
CC_WARNING = m(r"(.+?\." + CC_EXT + r"):([0-9]+)(?::([0-9]+))?: (?:warning|note): .*")
CC_CONTEXT = m(r"In file included from .*|[ \t]+from .*|.+?: In (?:function|member function|constructor|destructor|"
               r"instantiation of|static member function) .*:")
CC_GENERATED = m(r"([0-9]+) errors? generated\.")
LD_UNDEFINED_ARCH = m(r"Undefined symbols for architecture [^ ]+:")
LD_SYMBOL = m(r"[ \t]+\"(.+)\", referenced from:")
LD_REFERENCE = rx(r"undefined reference to [`'\u2018](.+?)['\u2019]")
LD_NOISE = m(r"(?:[^ ]*/)?ld(?:\.[a-z]+)?: .*|collect2: error: ld returned [0-9]+ exit status|clang(?:\+\+)?: error: li"
             r"nker "
             r"command failed.*|[ \t]+(?:_?[A-Za-z0-9_]+(?:\(.*\))? in .+\.o|in function .*)")
MAKE_NOISE = m(r"g?make(?:\[[0-9]+\])?: \*\*\* .*|g?make(?:\[[0-9]+\])?: (?:Entering|Leaving) directory .*")


def cc_code(flag):
    if not flag:
        return ""
    if flag.startswith("-Werror,"):
        flag = flag[len("-Werror,"):]
    if flag.startswith("-Werror="):
        flag = "-W" + flag[len("-Werror="):]
    return flag


class Cc(Recognizer):
    def __init__(self, found):
        super().__init__(found)
        self.symbols = False

    def step(self, lines, i):
        line = lines[i]
        if self.symbols:
            r = LD_SYMBOL(line)
            if r:
                self.f.error("cc", "", "ld", "undefined symbol " + r.group(1))
                return 1
            if line.startswith((" ", "\t")):
                return 1
            self.symbols = False
        r = CC_ERROR(line)
        if r:
            self.f.error("cc", r.group(1), cc_code(r.group(5)), r.group(4))
            return 1
        if CC_WARNING(line) or CC_CONTEXT(line):
            return 1
        r = CC_GENERATED(line)
        if r:
            self.f.summary("cc", "error", num(r.group(1)), None, "sum")
            return 1
        if LD_UNDEFINED_ARCH(line):
            self.symbols = True
            return 1
        r = LD_REFERENCE.search(line)
        if r:
            self.f.error("cc", "", "ld", "undefined reference to " + r.group(1))
            return 1
        if LD_NOISE(line) or MAKE_NOISE(line):
            return 1
        return 0


RECOGNIZERS = (Pytest, Unittest, Cargo, Rustc, Swift, Go, Js, Checkers, Dotnet, Jvm, Misc, Cc)

# ---- generic fallback (5.6)
GENERIC_FILE = m(r"(.+?\.[A-Za-z0-9]+):([0-9]+)(?::([0-9]+))?:? (?:fatal )?error(?:\[([^\]]+)\])?:? (.+)")
GENERIC_PREFIX = m(r"(?:error|ERROR|Error|fatal|FATAL)(?:\[([^\]]+)\])?: (.+)")
GENERIC_EXCEPTION = m(r"(?:Uncaught )?([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)): (.*)")


def generic(found, lines, claimed):
    for i, line in enumerate(lines):
        if i in claimed:
            continue
        r = GENERIC_FILE(line)
        if r:
            found.error("generic", r.group(1), r.group(4) or "", r.group(5))
            continue
        r = GENERIC_PREFIX(line)
        if r:
            found.error("generic", "", r.group(1) or "", r.group(2))
            continue
        r = GENERIC_EXCEPTION(line)
        if r:
            found.error("generic", "", r.group(1), r.group(2))


SUPERSEDES = {"nextest": ("cargo",)}

COMPLETE_CARGO = rx(r"Finished .* target\(s\) in .+")
COMPLETE_SWIFTPM = rx(r"Build complete!(?: \(.*\))?")
COMPLETE_XCODEBUILD = rx(r"\*\* [A-Z]+(?: [A-Z]+)* SUCCEEDED \*\*(?: \[.*\])?")
COMPLETE_GRADLE = rx(r"BUILD SUCCESSFUL(?: in .+)?")
COMPLETE_MAVEN = rx(r"\[INFO\] BUILD SUCCESS")
COMPLETE_PRETTIER = rx(r"All matched files use Prettier code style!")
COMPLETE_VITE = rx(r"\u2713 built in [0-9]+(?:\.[0-9]+)?(?:ms|s)")
COMPLETION_FRAMEWORKS = ("cargo", "swiftpm", "xcodebuild", "gradle", "maven", "prettier", "vite")
COMPLETIONS = (COMPLETE_CARGO, COMPLETE_SWIFTPM, COMPLETE_XCODEBUILD, COMPLETE_GRADLE, COMPLETE_MAVEN,
               COMPLETE_PRETTIER, COMPLETE_VITE)


def completion(segments):
    """The framework of the first line that says a tool finished cleanly (5.4), or None."""
    for lines, _ in segments:
        for line in lines:
            trimmed = line.strip(WS)
            for name, pattern in zip(COMPLETION_FRAMEWORKS, COMPLETIONS):
                if pattern.fullmatch(trimmed):
                    return name
    return None


def failures_detail(info, output_text, cwd=None, failed=None):
    """failures() plus the canonical strings behind the fingerprints (fixtures only)."""
    found = Found(cwd if isinstance(cwd, str) else None)
    info = info if isinstance(info, dict) else {}
    output_text = output_text if isinstance(output_text, str) else ""
    segments = []
    for segment in budget(output_text):
        lines = clean_lines(segment)
        claimed = set()
        recognizers = [cls(found) for cls in RECOGNIZERS]
        i = 0
        while i < len(lines):
            used = 0
            for rec in recognizers:
                used = rec.step(lines, i)
                if used:
                    break
            if used:
                claimed.update(range(i, i + used))
                i += used
            else:
                i += 1
        for rec in recognizers:
            rec.finish()
        segments.append((lines, claimed))
    if not found.fps and failed is True:
        for lines, claimed in segments:
            generic(found, lines, claimed)
    suite = info.get("suite") if isinstance(info.get("suite"), str) else ""
    has_tsc = any(fp.startswith("tsc|") for fp in found.fps)
    summaries = []
    for fw, family, f_count, p_count, mode in found.summaries:
        if fw == "found-errors":
            fw = "tsc" if (has_tsc or suite in ("tsc", "vue-tsc")) else "ruff"
        summaries.append((fw, family, f_count, p_count, mode))
    present = {s[0] for s in summaries}
    summaries = [s for s in summaries if not any(s[0] in SUPERSEDES.get(p, ()) for p in present)]
    combined = {}
    order = []
    for fw, family, f_count, p_count, mode in summaries:
        if fw not in combined:
            order.append(fw)
            combined[fw] = [family, f_count, p_count]
        elif mode == "sum":
            c = combined[fw]
            c[1] = (c[1] or 0) + (f_count or 0)
            c[2] = None if (c[2] is None and p_count is None) else (c[2] or 0) + (p_count or 0)
        else:
            combined[fw] = [family, f_count, p_count]
    canonical = sorted(set(found.fps))
    tests = [fw for fw in order if combined[fw][0] == "test"]
    errors = [fw for fw in order if combined[fw][0] == "error"]
    chosen = tests or errors
    if chosen:
        failed_count = sum(combined[fw][1] or 0 for fw in chosen)
        passes = [combined[fw][2] for fw in chosen if combined[fw][2] is not None]
        passed_count = sum(passes) if (tests and passes) else None
        framework = next((fw for fw in chosen if combined[fw][1]), None) or \
            next((fw for fw in chosen if combined[fw][2]), None) or chosen[0]
    elif canonical:
        failed_count, passed_count = len(canonical), None
        framework = found.fps[0].split("|", 1)[0]
    else:
        framework = completion(segments)
        failed_count, passed_count = (0 if framework else None), None
        if failed_count is None and failed is False and info.get("errorFilter") and silent(output_text):
            failed_count = 0
    fingerprints = sorted({sha16(c) for c in canonical})[:MAX_FINGERPRINTS]
    return {"fingerprints": fingerprints, "failedCount": failed_count, "passedCount": passed_count,
            "framework": framework}, canonical


def failures(info, output_text, cwd=None, failed=None):
    return failures_detail(info, output_text, cwd, failed)[0]


# ============================================================================ edit hunks (6)

def text_lines(text):
    lines = [x.rstrip(" \t") for x in lf(text if isinstance(text, str) else "").split("\n")]
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def hunk_texts(old_lines, new_lines):
    a, b = list(old_lines), list(new_lines)
    while a and b and a[0] == b[0]:
        a.pop(0)
        b.pop(0)
    while a and b and a[-1] == b[-1]:
        a.pop()
        b.pop()
    return "\n".join(a), "\n".join(b)


def norm_edit_path(path, cwd=None):
    p = path.replace("\\", "/")
    if p.startswith("file://"):
        p = p[len("file://"):]
    p, _ = cwd_relative(p, cwd)
    while "//" in p:
        p = p.replace("//", "/")
    while p.startswith("./"):
        p = p[2:]
    while "/./" in p:
        p = p.replace("/./", "/")
    return p


def hunk(path, old, new, cwd):
    o, n = hunk_texts(text_lines(old), text_lines(new))
    return {"file": sha16(norm_edit_path(path, cwd)), "old": sha16(o), "new": sha16(n)}


def hunk_lines(path, old_lines, new_lines, cwd):
    o, n = hunk_texts(text_lines("\n".join(old_lines)), text_lines("\n".join(new_lines)))
    return {"file": sha16(norm_edit_path(path, cwd)), "old": sha16(o), "new": sha16(n)}


def whole(path, content, cwd):
    return {"file": sha16(norm_edit_path(path, cwd)), "old": None, "new": sha16("\n".join(text_lines(content)))}


def deleted(path, cwd):
    return {"file": sha16(norm_edit_path(path, cwd)), "old": None, "new": EMPTY}


PATCH_HEADER = rx(r"\*\*\* (Add File|Delete File|Update File|Move to): (.+)")


def update_chunks(lines):
    """[(old lines, new lines)] of the `@@` chunks of an Update File section or a diff."""
    chunks, old, new, changed = [], [], [], False
    for line in lines:
        if line.startswith("@@"):
            if changed:
                chunks.append((old, new))
            old, new, changed = [], [], False
        elif line == "*** End of File":
            continue
        elif line.startswith("-"):
            old.append(line[1:])
            changed = True
        elif line.startswith("+"):
            new.append(line[1:])
            changed = True
        else:
            text = line[1:] if line.startswith(" ") else line
            old.append(text)
            new.append(text)
    if changed:
        chunks.append((old, new))
    return chunks


def codex_patch(text, cwd):
    lines = lf(text).split("\n")
    start = next((k + 1 for k, x in enumerate(lines) if x.strip(WS) == "*** Begin Patch"), 0)
    hunks = []
    sections = []          # (op, path, move_to, body lines)
    for line in lines[start:]:
        if line.strip(WS) == "*** End Patch":
            break
        r = PATCH_HEADER.fullmatch(line.rstrip(WS))
        if r and r.group(1) == "Move to" and sections and sections[-1][0] == "Update File":
            sections[-1][2] = r.group(2).strip(WS)
            continue
        if r and r.group(1) != "Move to":
            sections.append([r.group(1), r.group(2).strip(WS), None, []])
            continue
        if sections:
            sections[-1][3].append(line)
    for op, path, move_to, body in sections:
        if op == "Add File":
            hunks.append(whole(path, "\n".join(x[1:] for x in body if x.startswith("+")), cwd))
        elif op == "Delete File":
            hunks.append(deleted(path, cwd))
        else:
            target = path
            if move_to:
                hunks.append(deleted(path, cwd))
                target = move_to
            for old, new in update_chunks(body):
                hunks.append(hunk_lines(target, old, new, cwd))
    return hunks


HASHLINE_HEADER = rx(r"\[(.+)#([0-9A-Za-z]{4})\]")
HASHLINE_INSERT = rx(r"PUT (?:<[0-9]+|>[0-9]+\*?|>\$):")
HASHLINE_MOVE = rx(r"MV (.+)")


def hashline_patch(text, cwd):
    """omp hashline edits: only insertions (known old: empty) and file deletions carry both sides (6.4)."""
    lines = lf(text).split("\n")
    hunks, path, k = [], None, 0
    while k < len(lines):
        line = lines[k].rstrip(WS)
        k += 1
        r = HASHLINE_HEADER.fullmatch(line)
        if r:
            path = r.group(1).strip(WS)
            continue
        if path is None:
            continue
        if HASHLINE_INSERT.fullmatch(line):
            body = []
            while k < len(lines) and lines[k].startswith("+"):
                body.append(lines[k][1:])
                k += 1
            if body:
                hunks.append(hunk_lines(path, [], body, cwd))
        elif line == "REM":
            hunks.append(deleted(path, cwd))
        elif HASHLINE_MOVE.fullmatch(line):
            hunks.append(deleted(path, cwd))
            path = HASHLINE_MOVE.fullmatch(line).group(1).strip(WS)
    return hunks


def is_codex_patch(text):
    return any(PATCH_HEADER.fullmatch(x.rstrip(WS)) for x in lf(text).split("\n"))


def is_hashline(text):
    return any(HASHLINE_HEADER.fullmatch(x.rstrip(WS)) for x in lf(text).split("\n"))


def pi_edits(args):
    """Pi and OpenClaw `edit`: edits[] (an array, a JSON string or one object) plus legacy oldText / newText."""
    edits = args.get("edits")
    if isinstance(edits, str):
        edits = parse_json(edits)
    if isinstance(edits, dict):
        edits = [edits]
    pairs = []
    for e in edits if isinstance(edits, list) else []:
        if isinstance(e, dict) and isinstance(e.get("oldText"), str) and isinstance(e.get("newText"), str):
            pairs.append((e["oldText"], e["newText"]))
    if isinstance(args.get("oldText"), str) and isinstance(args.get("newText"), str):
        legacy = (args["oldText"], args["newText"])
        if legacy not in pairs:
            pairs.append(legacy)
    return pairs


def omp_patch_mode(args, cwd):
    path = args.get("path")
    hunks = []
    for entry in args.get("edits") or []:
        if not isinstance(entry, dict):
            continue
        op = entry.get("op") or "update"
        diff = entry.get("diff") if isinstance(entry.get("diff"), str) else ""
        if op == "delete":
            hunks.append(deleted(path, cwd))
        elif op == "create":
            body = lf(diff).split("\n")
            if all(x.startswith("+") for x in body if x):
                body = [x[1:] for x in body]
            hunks.append(whole(path, "\n".join(body), cwd))
        else:
            target = path
            if isinstance(entry.get("rename"), str) and entry["rename"]:
                hunks.append(deleted(path, cwd))
                target = entry["rename"]
            for old, new in update_chunks(lf(diff).split("\n")):
                hunks.append(hunk_lines(target, old, new, cwd))
    return hunks


def patch_input(text, cwd):
    if is_codex_patch(text):
        return codex_patch(text, cwd)
    if is_hashline(text):
        return hashline_patch(text, cwd)
    return []


def droid_pair(entry):
    """(old, new) of a Factory Droid replacement: `old_str` / `new_str`, else `old_string` / `new_string` (6.2)."""
    for old_key, new_key in (("old_str", "new_str"), ("old_string", "new_string")):
        if isinstance(entry.get(old_key), str) and isinstance(entry.get(new_key), str):
            return entry[old_key], entry[new_key]
    return None


def droid_hunks(name, args, cwd):
    path = args.get("file_path") if isinstance(args.get("file_path"), str) else None
    if path is None:
        return []
    if name == "Create":
        return [whole(path, args["content"], cwd)] if isinstance(args.get("content"), str) else []
    if name == "Edit":
        pair = droid_pair(args)
        return [hunk(path, pair[0], pair[1], cwd)] if pair else []
    edits = args.get("edits") if isinstance(args.get("edits"), list) else []
    pairs = [droid_pair(e) for e in edits if isinstance(e, dict)]
    return [hunk(path, old, new, cwd) for old, new in (p for p in pairs if p)]


def edit_hunks(source, tool, value, cwd=None):
    family = FAMILY.get(source)
    if family is None or not isinstance(tool, str):
        return []
    name = tool_name(family, tool)
    kind = KIND_OF[family].get(name)
    if kind == "command":
        text = command_text(source, tool, value)
        if text is None:
            return []
        parsed = parse_command(text, cwd)
        return [h for p in parsed.patches for h in codex_patch(p, cwd)]
    if kind != "edit":
        return []
    args = decode_args(value)
    if family == "codex":
        if isinstance(value, str) and args is None:
            return codex_patch(value, cwd)
        args = args or {}
        text = args.get("input") if isinstance(args.get("input"), str) else args.get("patch")
        return codex_patch(text, cwd) if isinstance(text, str) else []
    if args is None:
        return []
    if family == "droid":
        return droid_hunks(name, args, cwd)
    if family == "claude":
        path = args.get("file_path") if isinstance(args.get("file_path"), str) else None
        if name == "Edit" and path and isinstance(args.get("old_string"), str) and isinstance(args.get("new_string"),
                                                                                               str):
            return [hunk(path, args["old_string"], args["new_string"], cwd)]
        if name == "MultiEdit" and path:
            edits = args.get("edits") if isinstance(args.get("edits"), list) else []
            return [hunk(path, e["old_string"], e["new_string"], cwd) for e in edits
                    if isinstance(e, dict) and isinstance(e.get("old_string"), str)
                    and isinstance(e.get("new_string"), str)]
        if name == "Write" and path and isinstance(args.get("content"), str):
            return [whole(path, args["content"], cwd)]
        if name == "NotebookEdit" and isinstance(args.get("notebook_path"), str):
            nb = args["notebook_path"]
            cell = args.get("cell_id") if isinstance(args.get("cell_id"), str) else (
                str(args["cell_number"]) if is_int(args.get("cell_number")) else "")
            mode = args.get("edit_mode") or "replace"
            source_text = args.get("new_source") if isinstance(args.get("new_source"), str) else ""
            if mode == "insert":
                return [hunk(nb, "", source_text, cwd)]
            if mode == "delete":
                return [deleted(nb + "#" + cell, cwd)]
            return [whole(nb + "#" + cell, source_text, cwd)]
        return []
    path = args.get("path") if isinstance(args.get("path"), str) else None
    if name == "write":
        return [whole(path, args["content"], cwd)] if path and isinstance(args.get("content"), str) else []
    if name == "apply_patch":
        return codex_patch(args["input"], cwd) if isinstance(args.get("input"), str) else []
    if name == "edit":
        if isinstance(args.get("input"), str):
            return patch_input(args["input"], cwd)
        if family == "omp" and path and isinstance(args.get("old_string"), str) \
                and isinstance(args.get("new_string"), str):
            return [hunk(path, args["old_string"], args["new_string"], cwd)]
        if family == "omp" and path and isinstance(args.get("edits"), list) and any(
                isinstance(e, dict) and ("diff" in e or "op" in e) for e in args["edits"]):
            return omp_patch_mode(args, cwd)
        if path:
            return [hunk(path, o, n, cwd) for o, n in pi_edits(args)]
    return []


# ============================================================================ spec appendix (A)

SPEC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "TASK-PARSERS.md")
APPENDIX_BEGIN = "<!-- appendix: generated by spec/tools/task_parsers.py --write; checked by default -->"
APPENDIX_END = "<!-- appendix end -->"


def code_list(values):
    return ", ".join("`" + v + "`" if "`" not in v else "``" + v + "``" for v in values) or "none"


# Strings and maps the text names, listed in A.6 (a list, so A.2 does not list it as a word tuple).
APPENDIX_CONSTANTS = ["GREP_FLAGS", "NO_OUTPUT", "CODE_COMPLETED", "CC_EXT", "NOT_IN_PATH", "RUNNER_SUBCOMMANDS",
                      "SCRIPT_LABEL"]


def spec_appendix():
    """Appendix A of spec/TASK-PARSERS.md: every table, list and pattern of this reference, verbatim."""
    out = [APPENDIX_BEGIN, "", "### A.1 Tool kinds", ""]
    for family, table in TOOL_KINDS.items():
        out.append("- **%s**: %s." % (family, "; ".join("%s %s" % (kind, code_list(names))
                                                       for kind, names in table.items() if names)))
    out += ["", "### A.2 Lists", ""]
    for name, value in vars(sys.modules[__name__]).items():
        if name.isupper() and isinstance(value, tuple) and value and all(isinstance(x, str) for x in value):
            out.append("- `%s`: %s" % (name, code_list(value)))
    out += ["", "### A.3 Table-driven programs", ""]
    for program, handler in PROGRAMS.items():
        rule = getattr(handler, "rule", None)
        if rule is None:
            continue
        kind = rule[0]
        if kind == "named":
            text = "class %s, suite `%s`, never targeted" % (rule[1], rule[2])
        elif kind == "posargs":
            text = "class %s, suite `%s`; targeted when words follow `--`" % (rule[1], rule[2])
        elif kind == "paths":
            text = "class %s, suite `%s`; targeted by the path rule%s; value options %s" % (
                rule[1], rule[2], " or by " + code_list(rule[3]) if rule[3] else "", code_list(rule[4]))
        elif kind == "flag":
            text = "class check, suite `%s` only with one of %s (else other); targeted by the path rule; value " \
                   "options %s" % (rule[2], code_list(rule[3]), code_list(rule[4]))
        elif kind == "sub":
            subs = []
            for sub, (cls, suite, filters, path_rule) in rule[1].items():
                how = [code_list(filters)] if filters else []
                how += ["the path rule"] if path_rule else []
                subs.append("`%s` → %s `%s`%s" % (sub, cls, suite, ", targeted by " + " or ".join(how) if how else
                                                  ", never targeted"))
            text = "first word: %s; any other first word → other; value options %s" % ("; ".join(subs),
                                                                                       code_list(rule[2]))
        else:
            text = "task runner `%s` (3.6); value options %s; no target → %s" % (
                rule[1], code_list(rule[2]), "%s `%s`" % rule[3] if rule[3] else "other")
        out.append("- `%s`: %s." % (program, text))
    out += ["", "### A.4 Patterns", "", "Full-line patterns unless the text says *search*.", "", "```text"]
    for name, value in vars(sys.modules[__name__]).items():
        pattern = None
        if isinstance(value, re.Pattern):
            pattern = value.pattern
        elif callable(value) and isinstance(getattr(value, "__self__", None), re.Pattern):
            pattern = value.__self__.pattern
        if pattern is not None and name.isupper():
            out.append("%-18s %s" % (name, pattern))
    out += ["```", "", "### A.5 Rule order within each recogniser", "",
            "The patterns each recogniser tries, in the order its code first uses them (5.3).", ""]
    import inspect
    for cls in RECOGNIZERS:
        names = []
        for name in re.findall(r"\b([A-Z][A-Z0-9_]{2,})\b", inspect.getsource(cls)):
            value = getattr(sys.modules[__name__], name, None)
            is_pattern = isinstance(value, re.Pattern) or isinstance(getattr(value, "__self__", None), re.Pattern)
            if is_pattern and name not in names:
                names.append(name)
        out.append("- %s: %s" % (cls.__name__, ", ".join("`%s`" % n for n in names)))
    out += ["", "### A.6 Named strings and maps", "",
            "Strings as JSON string literals (escapes as in JSON; regular-expression fragments that the patterns of "
            "A.4 already contain expanded); maps as `key → values`.", "", "```text"]
    for name in APPENDIX_CONSTANTS:
        value = getattr(sys.modules[__name__], name)
        if isinstance(value, str):
            shown = json.dumps(value, ensure_ascii=False)
        else:
            shown = "; ".join("%s → %s" % (k, ", ".join(json.dumps(x, ensure_ascii=False) for x in v))
                              for k, v in value.items())
        out.append("%-18s %s" % (name, shown))
    out += ["```", "", APPENDIX_END]
    return "\n".join(out) + "\n"


def spec_with_appendix(text):
    begin, end = text.find(APPENDIX_BEGIN), text.find(APPENDIX_END)
    if begin < 0 or end < 0:
        return None
    return text[:begin] + spec_appendix() + text[end + len(APPENDIX_END) + 1:]


# ============================================================================ fixtures

def compute(case):
    """The reference output of one fixture case."""
    group = case["group"]
    cwd = case.get("cwd")
    if group == "actionKind":
        return action_kind(case["source"], case["tool"], case["input"], cwd)
    if group == "command":
        info = command_info(case["text"], cwd)
        return {k: info[k] for k in ("class", "suite", "targeted", "key", "masked", "background", "errorFilter")}
    if group == "outcome":
        return outcome(case["source"], case["result"], case.get("t"), case.get("tool"), case.get("input"), cwd)
    if group == "resultText":
        return result_text(case["source"], case["result"])
    if group == "session":
        return {"continuation": continuation(case["source"], case.get("tool"), case.get("input")),
                "running": running_session(case["source"], case["result"]) if "result" in case else None}
    if group == "failures":
        info = case.get("info") or command_info(case.get("command") or "", cwd)
        result, canonical = failures_detail(info, case["output"], cwd, case.get("failed"))
        return dict(result, canonical=canonical)
    if group == "edits":
        return edit_hunks(case["source"], case["tool"], case["input"], cwd)
    if group == "notification":
        return notification(case["source"], case["line"])
    if group == "codeScript":
        return code_script(case["source"], case["tool"], case["input"])
    if group == "codeItems":
        return code_items(case["source"], case["result"])
    if group == "maskedStatus":
        return masked_status(case["text"], case["output"], cwd)
    raise ValueError(group)


def build():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import task_parser_corpus as corpus  # noqa: E402
    cases, problems, outputs = [], [], {}
    corpus_cases = corpus.cases()
    for case in corpus_cases:
        got = compute(case)
        outputs[case["id"]] = got
        for problem in corpus.hand_check(case, got):
            problems.append("%s: %s" % (case["id"], problem))
        stored = {k: v for k, v in case.items() if k not in ("want", "author")}
        cases.append(dict(stored, expect=got))
    problems.extend(corpus.check_relations(corpus_cases, outputs))
    return {"_comment": "spec/TASK-PARSERS.md. Each case names its function (`group`), its inputs and `expect`, the "
                        "reference output (`failures` also lists the `canonical` strings behind its fingerprints, "
                        "sorted, to help ports debug; `command` also has the `masked` and `background` flags that "
                        "`outcome` uses). Generated by spec/tools/task_parsers.py --write from "
                        "spec/tools/task_parser_corpus.py and spec/tools/task_parser_outputs.py; the hand "
                        "expectations there are checked first.",
            "cases": cases}, problems


def main():
    data, problems = build()
    if problems:
        print("\n".join(problems))
        print("%d hand expectations failed" % len(problems))
        return 1
    counts = {}
    for case in data["cases"]:
        counts[case["group"]] = counts.get(case["group"], 0) + 1
    summary = ", ".join("%s %d" % kv for kv in counts.items())
    with open(SPEC, encoding="utf-8") as fh:
        spec_text = fh.read()
    spec_new = spec_with_appendix(spec_text)
    if spec_new is None:
        print("spec/TASK-PARSERS.md has no appendix markers")
        return 1
    if "--write" in sys.argv:
        fixture_bytes = (json.dumps(data, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
        spec_bytes = spec_new.encode("utf-8")
        with open(FIXTURE, "wb") as fh:
            fh.write(fixture_bytes)
        with open(SPEC, "wb") as fh:
            fh.write(spec_bytes)
        print("wrote %s: %d cases (%s) and the appendix of %s" % (
            os.path.normpath(FIXTURE), len(data["cases"]), summary, os.path.normpath(SPEC)))
        return 0
    if spec_new != spec_text:
        print("the appendix of spec/TASK-PARSERS.md differs from the reference; run with --write")
        return 1
    with open(FIXTURE, encoding="utf-8") as fh:
        stored = json.load(fh)
    mismatches = []
    by_id = {c["id"]: c for c in stored["cases"]}
    for case in data["cases"]:
        old = by_id.get(case["id"])
        if old is None:
            mismatches.append("%s: missing from the fixture" % case["id"])
        elif old != case:
            mismatches.append("%s: expected %r got %r" % (case["id"], old.get("expect"), case["expect"]))
    if len(stored["cases"]) != len(data["cases"]):
        mismatches.append("fixture has %d cases, corpus %d" % (len(stored["cases"]), len(data["cases"])))
    if mismatches:
        print("\n".join(mismatches[:40]))
        return 1
    print("task parsers reference matches %d cases (%s)" % (len(data["cases"]), summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
