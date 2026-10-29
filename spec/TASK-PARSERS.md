# Task-score parsers

The task score (METRICS 6.9) judges whether a coding agent is making progress from what its tool calls did. This
document defines the pure functions that turn one tool call and its result into the fields the score keeps. Their
inputs (command lines, tool output, edit text) are read in memory while a line is parsed and are never stored; only the
outputs listed here are kept (section 7).

| Function | Output |
| --- | --- |
| `action_kind(source, tool, input, cwd)` | `read`, `search`, `edit`, `command`, `delegate`, `web` or `other` (2) |
| `command_info(text, cwd)` | `{class, suite, targeted, key}` plus `masked`, `background` and `errorFilter` (3) |
| `masked_status(text, outputText, cwd)` | `{exitCode, failed}` that a masked command shows in its own output, or null (3.10) |
| `outcome(source, result, t, tool, input, cwd)` | `{exitCode, failed, t}` (4.2) |
| `result_text(source, result)` | the output text that `failures` reads (4.1) |
| `continuation(source, tool, input)`, `running_session(source, result)` | a session key or null (4.3) |
| `notification(source, line)` | `{session, exitCode}` of a Claude background-task notice, or null (4.3) |
| `code_script(source, tool, input)`, `code_items(source, result)` | a Codex code-mode script's tool calls and printing statements, and its printed results (4.4) |
| `failures(info, outputText, cwd, failed)` | `{fingerprints, failedCount, passedCount, framework}` (5) |
| `edit_hunks(source, tool, input, cwd)` | `[{file, old, new}]` (6) |

`spec/tools/task_parsers.py` implements every function and checks `spec/fixtures/task-parser-cases.json`; with `--write`
it regenerates the fixture from `spec/tools/task_parser_corpus.py` and `spec/tools/task_parser_outputs.py` (a
hand-written corpus, never copied from real logs) and rewrites Appendix A of this file. Appendix A lists every word
list, option table and regular expression verbatim and is normative; the checker fails when it differs from the
reference.

## 1. Conventions

- **Sources.** The functions apply to `claudeCode` and `claudeDesktop` (family `claude`), `codex`, `omp`, `pi`,
  `openclaw` and `droid` (Factory Droid), the sources that record tool calls (METRICS 6.10). For any other source: kind `other`, the outcome
  `{exitCode: null, failed: null, t}`, empty text, no session, no hunks.
- **Inputs as stored.** `tool` is the call's name; `input` is the call's input as the line stores it (METRICS 6.10): an
  object for Claude, omp, Pi and OpenClaw; a string for Codex (`arguments` of a `function_call`, `input` of a
  `custom_tool_call`). `result` is the Claude or Factory Droid `tool_result` block, the Codex `…_output` payload, or
  the omp, Pi and OpenClaw `toolResult` message. `cwd` is the agent's working directory (METRICS 4: `cwdPath`), or null. Any input of
  an unexpected type is treated as absent; no function ever fails.
- **JSON.** *Parsing* follows RFC 8259: `NaN`, `Infinity` and `-Infinity` are rejected, duplicate keys keep the last
  value, and a document nested too deeply for the parser fails; a failed parse means *none*. *The input as an object*:
  an object as is; a string whose first character after JSON whitespace (space, tab, CR, LF) is `{` and that parses
  as a JSON object; otherwise none. *The leading JSON object* of a text: the first JSON value after JSON whitespace,
  when it is an object, ignoring any text after it. A *JSON integer* is a number written without fraction or exponent,
  in the signed 64-bit range; `true` and `false` are never numbers, and *`== true`* means the literal `true`.
- **Text.** Strings are sequences of Unicode scalar values; an unpaired surrogate (possible only through a JSON escape)
  is read as U+FFFD. A *character* is one scalar value: every length and cut counts scalars, never UTF-16 units or
  grapheme clusters, and byte budgets count UTF-8 bytes. Equality, prefixes, searches, sets and sorting are ordinal
  over scalar values (no normalisation, no culture rules). *Trim* removes ASCII spaces and tabs only. Case folding is
  ASCII only. *LF-normalised*: CR LF, then any remaining CR, replaced by LF. A *number read from text* is a run of
  digits (with the pattern's sign); more than 18 significant digits read as 999 999 999 999 999 999.
- **Patterns.** Regular expressions use explicit ASCII classes (`[0-9]`, `[A-Za-z]`, `[ \t]`); `\s`, `\d`, `\w` and
  `\b` are never used, and ports must not add them. Unless the text says otherwise a pattern must match the **whole**
  string, as `\A(?:P)\z` (never `^P$`: `$` also matches before a final line break in .NET and ICU, and several
  patterns have a top-level `|`). A *prefix match* is anchored at the start only (`\A(?:P)`); a *search* finds the
  first match anywhere; a match *at* a position is anchored there (`\G` in .NET, `anchored` in NSRegularExpression).
  `.` matches any character except LF (ICU needs `dotMatchesLineSeparators` and `.` written as `[^\n]`). Lookbehind is
  one character wide. Matching is case-sensitive unless stated.
- **Digests.** `sha16(text)` = the first 16 lowercase hex digits of SHA-256 over the UTF-8 bytes of `text`.
  `sha16("") = e3b0c44298fc1c14`.
- **Null.** A null `failed`, count or suite means *unknown*, never a failure or zero.

## 2. `action_kind`

`action_kind(source, tool, input, cwd)`:

1. The tool name is compared exactly; for omp, Pi and OpenClaw after ASCII case folding (omp folds built-in names,
   METRICS 4.7). A name not in the source's table (A.1) is `other`; so are MCP tools (`mcp__…`), todo lists, plans,
   questions to the user, `BashOutput`, `write_stdin`, `wait_agent` and every other tool.
2. A name of kind `command` (a shell tool) is refined by its command text (3.1): no readable text → `command`;
   otherwise `shell_kind` (3.9) → `edit` (an `apply_patch` in the shell), `command`, `read` or `search`.

| Source | read | search | edit | command | delegate | web |
| --- | --- | --- | --- | --- | --- | --- |
| Claude | `Read`, `NotebookRead`, `LS` | `Grep`, `Glob`, `LSP` | `Edit`, `MultiEdit`, `Write`, `NotebookEdit` | `Bash`, `PowerShell` | `Task`, `Agent`, `SendMessage` | `WebFetch`, `WebSearch` |
| Codex | `read_file`, `list_dir`, `view_image` | `grep_files` | `apply_patch` | `shell`, `container.exec`, `local_shell`, `shell_command`, `exec_command`, `exec` | `spawn_agent`, `send_input`, `resume_agent` | `web_search`, `web_fetch` |
| omp | `read` | `grep`, `search`, `find`, `glob`, `ast_grep`, `lsp` | `edit`, `write`, `ast_edit` | `bash` | `task` | `web_search`, `fetch`, `browser` |
| Pi | `read`, `ls` | `grep`, `find` | `edit`, `write` | `bash`, `powershell` | — | — |
| OpenClaw | `read`, `ls` | — | `edit`, `write`, `apply_patch` | `exec`, `bash` | `sessions_spawn`, `sessions_send` | `web_search`, `web_fetch`, `x_search`, `browser` |
| Factory Droid | `Read`, `LS` | `Grep`, `Glob` | `Create`, `Edit`, `MultiEdit` | `Execute` | `Task` | `WebSearch`, `FetchUrl` |

Evidence for the omp, Pi and OpenClaw names: their published packages (omp 18.13.3 `BUILTIN_TOOL_NAMES`, Pi 1.4.0
`core/tools`, OpenClaw 2026.10.8 tool catalog), read on 2026-10-28; Codex: `codex-rs` `tools` (main, same day).
Factory Droid (closed source): the names seen in public recordings of droid (`spec/research/droid.md`: Read, LS, Glob,
Grep, Execute, Create, Edit, MultiEdit, TodoWrite, Task, Skill, WebSearch, FetchUrl, AskUser, GenerateDroid,
ExitSpecMode); `TodoWrite`, `Skill`, `AskUser`, `GenerateDroid` and `ExitSpecMode` are `other`. Names compare exactly.
Not seen and therefore `other`: an `ApplyPatch` tool, which droid may offer to some models. No Droid session was read on
this machine.

## 3. `command_info`

`command_info(text, cwd)` → `{class, suite, targeted, key, masked, background, errorFilter}`:

| Field | Value |
| --- | --- |
| `class` | `test`, `build`, `check` or `other`: the strongest class in the command, **test > build > check > other** |
| `suite` | the runner of the chosen segment from the fixed vocabulary `SUITES` (A.2), or null |
| `targeted` | the chosen segment's run is narrowed by a filter, a test name or a path (3.5) |
| `key` | 16-hex digest of the normalised command (3.7) |
| `masked` | the command's exit status may hide the chosen segment's (3.7); used by `outcome` |
| `background` | the chosen segment runs in the background (3.7); used by `outcome` |
| `errorFilter` | class build or check, and the chosen segment's output goes only through a grep for error lines (3.10); used by `outcome` and `failures` |

The order puts tests first because a passing test run proves a build too (runners such as `cargo test`, `swift test`,
`go test` and `dotnet test` build first), a build proves more than a lint or type check, and anything else proves
nothing. METRICS 6.9 consumes `class`, `suite`, `targeted` and `key`.

### 3.1 Command text

`command_text(source, tool, input)` is the text a shell tool runs, from the input as an object (1):

| Source and tool | Text |
| --- | --- |
| Claude `Bash`, `PowerShell`; omp, Pi `bash`, `powershell`; OpenClaw `exec`, `bash`; Codex `shell_command`; Factory Droid `Execute` | `command` (a string) |
| Codex `shell`, `container.exec`, `local_shell` (`ARGV_TOOLS`) | `command`: a string as is; a non-empty array of strings joined (below) |
| Codex `exec_command` | `cmd` (a string) |
| Codex `exec` (custom tool) | when the input is an object: its string `cmd`, else its string `command`, else no text; otherwise the raw input string |

Anything else (a missing field, another type, an empty array or one with a non-string) has no text. An array is joined
with single spaces after quoting each element: an element matching `SAFE_ARG` stays as is; any other element (the empty
string included) becomes `'` + the element with every `'` replaced by `'"'"'` + `'`. Tokenising the joined text (3.2)
gives back the array exactly. Before tokenising, the text is LF-normalised.

### 3.2 Tokens

The text is read left to right into words, operators, redirections and here-documents. At each token start the
checks run in this order: space or tab, backslash-LF, LF, `#`, a two-character operator, a redirection, a
one-character operator, else a word.

- Spaces and tabs separate tokens. A backslash followed by LF is removed (line continuation). An unquoted LF is the
  operator `;`; right after it, the bodies of the here-documents opened on that line are read (below).
- `#` at the start of a token begins a comment that runs to the end of the line.
- Two-character operators: `&&`, `||`, `|&` (read as `|`), `;;` (read as `;`). One-character operators: `;`, `&`,
  `|`, `(`, `)`. So `&>log` is a redirection and `&&` is never two `&`.
- A redirection is `REDIR` matched at the token start: optional digits, then one of `<<<`, `<<-`, `<<`, `>>`, `>&`,
  `<&`, `>|`, `<>`, `&>>`, `&>`, `>`, `<`. Spaces and tabs after it are skipped and the next word is its target.
  `<<` and `<<-` open a here-document whose delimiter is that word (quotes removed); every other redirection keeps
  `(operator, target)` for the segment and takes no further part in the command.
- A here-document body is the lines after the operator's line up to, not including, the first line equal to the
  delimiter (for `<<-`, after removing its leading tabs); each body line keeps its LF. Without that line the body runs
  to the end of the text. A here-document never opened by an LF has an empty body.
- A word runs until an unquoted space, tab, LF, `;`, `&`, `|`, `(`, `)`, `<` or `>`. Inside it: `'…'` is literal (an
  unterminated quote runs to the end); `"…"` is literal except that a backslash before `"`, `\`, `$`, `` ` `` or LF
  escapes it (backslash-LF is removed); `$'…'` is literal except that a backslash makes the next character literal;
  `$(…)` and `${…}` are kept verbatim up to the matching bracket (only that bracket's own nesting is counted, quotes
  are not); `` `…` `` is kept verbatim; outside quotes a backslash makes the next character literal. Every
  unterminated form (`'`, `"`, `$'`, `$(`, `${`, `` ` ``) runs to the end of the text.

### 3.3 Segments

Operators split the tokens into segments: a segment is the words, redirections and here-document bodies between
operators, and it carries `ops`, every operator read since the previous segment (in order, parentheses included). A
segment with neither words nor bodies does not exist; its operators stay in the next segment's `ops`. The operators
after the last segment are the *tail*. Then, for each segment in order:

1. Its own `(` and `)` in `ops` are applied to the directory context, in order: `(` saves it, `)` restores the last
   saved one. Its `ops` are prefixed with the operators carried from dropped segments (step 7).
2. Leading words in `LEADING_KEYWORDS` are dropped, and a trailing `}`.
3. Prefixes are removed (3.4), then step 2 again.
4. A segment whose first word is in `CLOSING_KEYWORDS` or `CONSTRUCTS` loses all its words. (Function definitions are
   not recognised: `t() { cargo test; }` classifies its body as if it ran.)
5. A shell wrapper (3.4) is replaced by the segments of its script, parsed the same way with the same state; its `ops`
   are carried into the script's first segment, and a `nohup` / `setsid` prefix on the wrapper marks the script's last
   segment background. Scripts nest at most `MAX_WRAPPER_DEPTH` = 4 levels; a deeper script contributes nothing (no
   segments, no patch texts).
6. Runners are removed (3.4). The segment records the current directory context. Then `cd` and `pushd` set the context
   to their first argument that does not start with `-` (or is `-`; none → `~`), normalised (below); `pushd` first
   saves the current context; `popd` restores the last one `pushd` saved. `set` with a word matching `PIPEFAIL_FLAG`
   followed by `pipefail` (`set -o pipefail`, `set -eo pipefail`; not `set +o`) marks the whole command `pipefail`,
   wherever it appears, wrapper scripts included.
7. A segment left with neither words nor bodies is dropped and its `ops` carry to the next segment.

After the last segment the tail's parentheses are applied and the tail joins the carried operators. Then, for each
kept segment `k`: the *operator between* it and the next kept segment is the last operator other than `(` and `)` in
the next segment's `ops` (for the last segment, in the remaining carried operators); `op_after(k)` is that operator,
`op_before(k+1)` the same one. The segment is background when the first operator other than `(` and `)` after it is
`&`, or a `nohup` / `setsid` prefix marked it. So `(cargo test) | tee log` has `|` after `cargo test`, `{ cargo test; }
| tee log` too (the `;` and `|` merge, the last wins), and in `(cd sub; cargo build;) && cargo test` the subshell's
context ends before `cargo test`.

Context normalisation of a target `t` with the current context `ctx`: `\` → `/`; an empty `t` leaves `ctx` unchanged;
when `t` is `cwd` or inside it (*cwd-relative*, below) the result is the part below `cwd`, counted from the starting
directory; else `~`, a target starting with `~/`, `/`, a drive (`DRIVE`) or `-` stands alone, and any other is joined
as `ctx/t` (or `t` when `ctx` is empty); then `.` and empty components are removed and `x/..` pairs collapse (a
leading `/` is kept). A result of `.` or empty is the empty context (the starting directory).

*cwd-relative*: `cwd` with `\` → `/` and trailing `/` removed (an empty result — the root — counts as no `cwd`); a
`/`-separated path equal to it is the empty path inside it, and one starting with `cwd/` is the rest inside it; for a
drive `cwd` both compare ASCII case-insensitively. The same rule serves 3.5, 5.2 and 6.1.

A segment is *neutral* when it has no words or its program (3.4) is in `NEUTRAL` (navigation, variables, `echo`,
`printf`, `true`, `sleep`, …). Neutral segments never classify.

### 3.4 Programs, prefixes, shell wrappers and runners

*Program name* of a word: the part after the last `/` or `\`, ASCII-folded, with a trailing `.exe`, `.cmd`, `.bat` or
`.ps1` removed; then `python`, `python3`, `python3.12`, `py`, `pypy3` (`python[0-9.]*|py|pypy[0-9.]*`) → `python`;
`gcc`, `g++`, `cc`, `c++`, `clang`, `clang++` with an optional `-<version>` → `cc`; `gradlew` → `gradle`; `mvnw` →
`mvn`; `py.test` → `pytest`; `nodejs` → `node`; `gmake` → `make`; `bazelisk` → `bazel`.

*Options*: a word starting with `-` (but not `-` alone). A list of *value options* (A.2, named per program) says which
options take the next word as their value. When reading positionals and options (`parse_opts`): `--` ends the options
(the rest are *words after `--`*); `--name=value` sets `--name` without taking the next word; a word in the value list
takes the next word as its value (a value option that is the last word is still present, without a value); a word
that starts with a two-character value option and is longer carries its value attached (`-kpush`, `-n4`); any other
option is a flag; single-dash `-x=v` is not split (except for `go`, 3.5).

**Prefixes**, removed from the front repeatedly: environment assignments (a *prefix match* of `NAME_ASSIGN`, so a
value may hold any text, line breaks included); `env` with its options
(`ENV_VALUE`) and assignments; `sudo` / `doas` (`SUDO_VALUE`); `time`; `nice` (`NICE_VALUE`); `nohup`, `setsid`
(both mark the segment background); `exec`, `command`, `builtin`, `noglob`, `unbuffer`, `chronic`; `stdbuf`
(`STDBUF_VALUE`); `caffeinate` (`CAFFEINATE_VALUE`); `xvfb-run` (`XVFB_VALUE`); `timeout`, `gtimeout`
(`TIMEOUT_VALUE`, then the duration word). Each prefix's options stop at `--` (removed) or at the first non-option.

**Shell wrappers**: `bash`, `sh`, `zsh`, `dash`, `ksh`, `fish` whose leading options include one matching
`SHELL_C_FLAG` (`-c`, `-lc`, `-ec`, …): the word after it is the script; a non-option before that flag means no
wrapper (`bash script.sh` is a program). `pwsh`, `powershell` with `-Command` or `-c` (ASCII case-insensitive): the
remaining words joined by spaces. `cmd` with `/c` (any case): the remaining words joined by spaces.

**Runners**, removed repeatedly (at most 8 times) until a program remains:

| Runner | Removed |
| --- | --- |
| `npx`, `pnpx`, `bunx` | the runner and its options (`NPX_VALUE`) |
| `npm`, `pnpm`, `yarn`, `bun` followed by `exec`, `dlx` or `x` | both words and the options after them (`NPX_VALUE`) |
| `mise exec` / `mise x` | everything up to and including `--` (or just the two words) |
| `uv`, `poetry`, `pipenv`, `pdm`, `rye`, `pixi`, `conda`, `mamba`, `micromamba`, `hatch` followed by `run` | both words and the options after them (`UV_VALUE` + `CONDA_VALUE`) |
| `python` with `-m MODULE` after its options (`PY_VALUE`) | everything up to `MODULE`, which becomes the program |
| `coverage run` with `-m MODULE` after its options (`COVERAGE_VALUE`) | everything up to `MODULE` |
| `bundle exec` | both words |
| `xcrun` | the runner and its options (`XCRUN_VALUE`) |
| `dotnet tool run` | the three words |
| `php` followed by a script whose program name is `phpunit`, `pest`, `phpstan` or `psalm` | `php` |

For `python` and `coverage run`, the options are skipped one by one (a value option takes the next word) until `-m`,
`-` or a non-option word; `-m` is then followed by the module.

`python` without `-m` stays a program (3.5: after its options, `manage.py test`, `setup.py test`, else other).

### 3.5 Classification

A non-neutral segment's words after 3.3–3.4 classify by their program name into `(class, suite, targeted)`; a program
without a rule is `other`. *Positionals* are the words that are not options or option values (3.4). Two rules decide
`targeted`; wherever this section applies them, they read the positionals **and** the words after `--`:

- **Path rule** (runners given files or directories): targeted when some word is not *broad*. Broad: after `\` → `/`,
  made cwd-relative (3.3) when inside `cwd`, then without leading `./` pairs and trailing `/`: empty, `.`, or one path
  component containing none of `*`, `?`, `::`, not starting with `/` or with a letter and `:`, and without a file
  extension. A word *has a file extension* when it ends with `.` followed by 1 to 10 ASCII letters or digits. So
  `tests/` and `src` are broad; `tests/unit`, `tests/test_q.py` and `tests/test_q.py::test_a` narrow the run.
- **Name rule** (runners whose positionals are name or path patterns): targeted when some word, without trailing `/`,
  is not empty, not `.` and not `cwd` itself.

Programs with their own rule:

| Program | Class and suite | Targeted |
| --- | --- | --- |
| `pytest` | test `pytest` | an option in `PYTEST_FILTERS`, or the path rule (`PYTEST_VALUE`) |
| `unittest` (`python -m unittest`) | test `unittest` | a positional other than `discover`, or `-k` (`UNITTEST_VALUE`) |
| `python manage.py test` (after python's options, skipped as in 3.4) | test `django test` | a positional label, `-k` or `--tag` (`DJANGO_VALUE`) |
| `python setup.py test` | test `setup.py test` | never |
| `cargo` | after global options (`CARGO_GLOBAL_VALUE`; words starting with `+` are toolchains): `test`/`t` → test `cargo test`; `nextest run`/`nextest r` → test `cargo nextest`; `build`/`b`/`rustc` → build `cargo build`; `check`/`c` → check `cargo check`; `clippy` → check `cargo clippy`; `fmt` with `--check` → check `cargo fmt`; else other | `test`: an option in `CARGO_NARROW`, a positional (a test filter), or after `--` a positional or `--exact` / `--skip` (`CARGO_VALUE`, `LIBTEST_VALUE`); `nextest`: a positional, or `-E`, `--filterset`, `--filter-expr`, `-p`, `--package`, `--test`, `--lib`, `--bin` (`NEXTEST_VALUE`); `build`, `check`, `clippy`: an option in `CARGO_NARROW`; `fmt`: never |
| `swift` | `test` → test `swift test`; `build` → build `swift build`; else other | `test`: an option in `SWIFT_TEST_FILTERS` (`SWIFT_TEST_VALUE`); `build`: in `SWIFT_BUILD_NARROW` (`SWIFT_BUILD_VALUE`) |
| `xcodebuild` | an option in `XCODE_INFO` → other. Actions are the positionals in `XCODE_ACTIONS` (options take a value unless in `XCODE_FLAGS` or written with `:` or `=`; `NAME=value` build settings are positionals that are not actions): `test` or `test-without-building` → test `xcodebuild test`; else `analyze` → check `xcodebuild analyze`; only `clean` → other; otherwise (no action, `build`, `archive`, …) → build `xcodebuild build` | `-only-testing…` or `-skip-testing…` (with `:` attached or a separate value), test only |
| `go` | `test` → test `go test`; `build`, `install` → build `go build`; `vet` → check `go vet`; else other. Words from `-args` on are ignored; `--` does not end the options; a leading `--` is read as `-`, and `-name=value` sets `-name` (`GO_TEST_VALUE`) | a package positional not in `GO_BROAD`; for `test` also `-run`, `-skip` or `-fuzz` |
| `dotnet` | `test` → test `dotnet test`; `build`, `publish`, `pack`, `msbuild` → build `dotnet <sub>`; `format --verify-no-changes` → check `dotnet format`; else other (`DOTNET_VALUE`) | `test`: `--filter` |
| `gradle` | each task positional (`GRADLE_VALUE`) classifies by its name after the last `:` (below); the strongest task wins, the first on ties; suite `gradle <name>` when the name is `test`, `check`, `build`, `assemble` or `lint`, else null | `--tests`, or the winning task has a module prefix (`:app:test`; `:test` alone has none) |
| `mvn` | each goal positional (`MVN_VALUE`): in `MVN_TEST` or `surefire:test` → test (build with an option in `MVN_SKIP`); in `MVN_BUILD` → build; ending `:check` → check; the strongest wins, the first on ties; suite `mvn <goal>` for goals in `MVN_TEST` or `MVN_BUILD`, else null | `-pl` / `--projects`, or a word starting with `-Dtest=` or `-Dit.test=` |
| `cc` | `-E` or `--version` anywhere, or `-v` first → other; `-fsyntax-only` → check `cc`; else build `cc` | never |
| `cmake` | with `--build` (`CMAKE_VALUE`): `--target`/`-t` `test`, `check` or `RUN_TESTS` → test `cmake --build` (targeted); else build `cmake --build`; without `--build` → other | test: always; build: a `--target` / `-t` is given |
| `ctest` | test `ctest` (`CTEST_VALUE`) | an option in `CTEST_FILTERS` |
| `jest` | test `jest` (`JEST_VALUE`) | an option in `JEST_FILTERS`, or the name rule over positionals and words after `--` |
| `vitest` | first word `run`, `watch`, `dev`, `related` or `typecheck` is a subcommand; `bench`, `list`, `init` → other; else test `vitest` (`VITEST_VALUE`) | `related`, an option in `VITEST_FILTERS`, or the name rule |
| `mocha` | test `mocha` (`MOCHA_VALUE`; a `--spec` value counts as a positional) | an option in `MOCHA_FILTERS`, or the path rule |
| `node` | with `--test` → test `node --test` (`NODE_TEST_VALUE`); else other | `--test-name-pattern`, `--test-only`, or the path rule |
| `tsc`, `vue-tsc` | an option in `TSC_NOT_CHECK` → other; else check `tsc` / `vue-tsc` (`TSC_VALUE`) | the path rule, except with `-b` / `--build` (never) |
| `ruff` | a first word in `RUFF_SUBCOMMANDS` is the subcommand: `check`, or no subcommand (legacy), → check `ruff`; `format` with `--check` or `--diff` → check `ruff format`; any other subcommand → other (`RUFF_VALUE`) | the path rule |
| `swiftlint` | first word `autocorrect` or `analyze`, or `--fix` anywhere → other; else check `swiftlint` | never |
| `bazel` | `test` → test `bazel test`; `build` → build `bazel build`; else other (`BAZEL_VALUE`) | a positional other than `//...`, `...`, `//:all`, or a `--test_filter…` word |
| `meson` | `test` → test `meson test` (`MESON_VALUE`); `compile` → build `meson compile`; else other | test: a positional or `--suite` |
| `deno` | `test` → test `deno test` (`DENO_TEST_VALUE`); `check`, `lint` → check `deno check` / `deno lint`; `fmt --check` → check `deno fmt`; `task NAME` → script (3.6, runner `deno task`); else other | test: `--filter` or the path rule |
| `npm`, `pnpm`, `yarn`, `bun` | package scripts (3.6) | 3.6 |
| `make`, `ninja`, `just`, `task`, `rake`, `mise`, `turbo`, `nx`, `composer` | task runners (3.6) | 3.6 |

Every other program with a rule is table-driven (A.3): a fixed class and suite (`swiftc`, `msbuild`, `javac`, …), a
path runner (`eslint`, `mypy`, `rspec`, `phpunit`, …), a check flag (`prettier --check`, `black --check`, …), or a
subcommand table (`dart test`, `mix test`, `ng build`, …).

Gradle task names: `test`, a name matching `test[A-Z0-9].*`, or ending in `Test` or `Tests` → test; `check` or `build`
→ test (they run the tests), or build when `-x test`, `--exclude-task test` or `--exclude-task=test` excludes the
`test` task (by its name after the last `:`); `assemble…`, `compile…`, `jar`,
`classes`, `installDist`, `bootJar`, `shadowJar` → build; `lint…`, `detekt`, `ktlintCheck`, `spotlessCheck`,
`checkstyle…`, `pmd…`, `spotbugs…` → check; anything else → other.

**Maven main-compilation coverage.** Following the [standard Maven lifecycle](https://maven.apache.org/guides/introduction/introduction-to-the-lifecycle.html), `command_info` may also return `mavenCompileKey`, a 16-hex digest
of `maven-main-compile-v1`, the known absolute starting directory, the relative shell directory context, and the
remaining Maven arguments, joined by U+001F. It is evidence only of the standard main-code `compile` phase:
a successful later lifecycle phase can close a failed `mvn compile` with the same digest. It does not equate tests.

Coverage requires one verifying shell segment and one phase from `MVN_BUILD` or `MVN_TEST`, optionally preceded
by `clean`. Keep module/reactor selectors, POM paths, profiles, settings and other configuration tokens in order;
remove cosmetic output options and `-Dtest=…`, `-Dit.test=…`, `-DskipTests[=…]` and
`-Dsurefire.failIfNoSpecifiedTests=…`. Direct plugin goals, multiple phases/verifiers, masked/background runs,
help/version/fail-never options, dynamic directories/arguments and compilation-suppression properties are excluded.
A preceding directory change must be static, explicit and followed by `&&`; `cd -` is unknown. Environment
assignments other than `JAVA_HOME` exclude coverage before shell-prefix removal, including on shell wrappers.
`JAVA_HOME` may select a different compiler, but a successful lifecycle still provides main-compilation evidence.
For Codex `exec_command`, use an explicit `workdir` for this digest; invalid explicit values give no digest rather
than falling back to the session directory. Other existing parser fields and check rules keep their meaning.
Only the digest is kept, never the source directory or command text. Maven plugins/POMs can override the standard
lifecycle; this rule does not inspect project configuration or establish full-suite/business acceptance.

### 3.6 Package scripts and task runners

**Script names.** A script or target name `n` (ASCII-folded) maps through the aliases `t`, `tst`, `tests` → `test`;
`type-check`, `types` → `typecheck`; `compile` → `build`. A canonical name gets a class and a suite label:

| Canonical | Class | Suite |
| --- | --- | --- |
| `test` | test | the runner's test label |
| `build` | build | runner prefix + `build` |
| `lint`, `typecheck`, `check` | check | runner prefix + the name |

| Runner | Test label | Prefix |
| --- | --- | --- |
| npm, pnpm, yarn | `npm test`, `pnpm test`, `yarn test` | `npm run `, `pnpm run `, `yarn run ` |
| bun (a script, not `bun test`) | `bun run test` | `bun run ` |
| deno task, composer, just, task, rake, turbo, nx | `deno task test`, `composer test`, `just test`, `task test`, `rake test`, `turbo test`, `nx test` | the runner and a space (`deno task `, …) |
| mise | `mise run test` | `mise run ` |
| make, ninja | `make test`, `ninja test` | `make `, `ninja ` |

For `make` and `ninja`, `check` is a **test** target (`make check` runs the tests in autotools projects; suite `make
check` / `ninja check`), and `all` is build with suite `make` / `ninja`. A name that is not canonical gets its class
from its first part (split at `:`, `.`, `_`, `/`, `-`): in `TEST_WORDS` → test, `BUILD_WORDS` → build,
`CHECK_WORDS` → check, else other; its suite is null (so `npm run test:unit` is test with no suite, and each such
command is a check of its own in METRICS 6.9).

**npm, pnpm, yarn, bun.** Leading options are skipped (`PM_VALUE` take a value; those in `PM_NARROW` narrow the run).
Then, among the words after the first one and before any `--`, the options in `PM_NARROW` with their values, and
`--workspace=…` / `--filter=…`, are removed and narrow the run (`npm run -w web test` runs the script `test` of one
workspace). Then the first word:

- `yarn workspace NAME …`: the words after `NAME` classify the same way and are always targeted.
- `bun test` → test `bun test` (`BUN_TEST_VALUE`), targeted by `-t`, `--test-name-pattern` or the name rule;
  `bun build` → build `bun build`.
- `test`, `t`, `tst` → the script `test`; `run`, `run-script`, `rum`, `urn` → the next word after any options is the
  script (none → other).
- For pnpm, yarn and bun only, a first word that is not in `NPM_BUILTINS` and has no file extension: when its program
  name has a rule of its own (3.5) and is not one of the runners of `SCRIPT_LABEL`, the words classify as that
  program (`pnpm vitest run` is `vitest`; `python` has no such rule here); otherwise it is the script name.
- Anything else → other.

A script's class and suite come from its name; a script of class other makes the segment other. *Forwarded words* are
the words after the script name with `--` removed: the run is targeted when narrowed above or when a forwarded word is
in `FORWARD_FILTERS` (or written `--name=…` for one of its `--` options), or is a non-option word not preceded by an
option in `FORWARD_VALUE` that the name rule counts.

**make, ninja, just, task, rake, mise, turbo, nx, composer, deno task.** A first word in the runner's
`RUNNER_SUBCOMMANDS` (`mise run` / `mise r`, `deno task`, `turbo run`, `composer run-script` / `composer run`) is
dropped. `nx run-many` / `nx affected` take their target from `-t` / `--target` / `--targets` (the first of a comma
list; targeted when `-p` / `--projects` is given); `nx run PROJECT:TARGET` uses `TARGET` and is targeted. Otherwise,
options are skipped (A.3 lists each runner's value options; `just --set` takes two values), words that are a prefix
match of `NAME_ASSIGN` are assignments and the remaining words are positionals (`--` is skipped like
an option).
A composer whose first positional is in `COMPOSER_BUILTINS` → other. No positional: `make` → build `make`, `ninja` →
build `ninja`, other runners → other. `make`, `ninja`, `rake`: every positional is a target, the strongest wins (the
first on ties), targeted when an assignment's name, ASCII upper-cased, matches `MAKE_FILTER_VAR` (`TEST=queue`,
`args=…`). Other runners: the first positional is the task; with further positionals the run is targeted for `just`
(recipe parameters) and `nx` always, and for the others when the further positionals pass the forwarded-words rule
(options having been skipped, only its name-rule part can apply).

### 3.7 Result fields

Every non-neutral segment classifies; the **chosen segment** is the first with the highest class rank. `class`,
`suite` and `targeted` are the chosen segment's; with no classifying segment the class is `other`, the suite null and
`targeted` false.

**masked** (chosen segment only, with the operators between segments of 3.3): true when a segment follows it and
either the operator after it is `|` (and the command is not `pipefail`), or some later segment's operator before it is
`;`, `||` or `&` (an LF counts as `;`). `&&` never masks: a failure stops the list and becomes its status. So `cargo
test | tail`, `cargo test || true`, `cargo test; echo $?`, `(cargo test) | tee log`, `{ cargo test; } | tee log` and
`if cargo test; then …; fi` are masked; `cargo build && cargo test` and `set -o pipefail; cargo test | tee log` are
not. `!` is dropped as a keyword and a loop's status is not seen (approximations). For class other, `masked` is false.

**background**: the chosen segment is background (3.3); for class other, when any segment is.

**key**: the *kept* segments are the classifying segments of class test, build or check; when there are none, the
non-neutral segments; when there are none, every segment with words. Each kept segment gives a token list:

1. its program name, then its other words in order except those in `COSMETIC` or starting with `COSMETIC_PREFIX`,
   and for `pytest` also `-s`, `--capture=no` and words matching `-r[A-Za-z]+`;
2. `npm|pnpm|yarn test|t|tst …` → `<pm> run test …`; `npm|pnpm|yarn|bun run-script|rum|urn …` → `<pm> run …`;
   `pnpm|yarn NAME …` with `NAME` not in `NPM_BUILTINS` and not starting with `-` → `<pm> run NAME …`;
3. preceded by `@<context>` when the context it records (3.3 step 6, before its own `cd`) is not empty.

`key = sha16(lists joined by U+001E, each list's tokens joined by U+001F)`; no kept segment → `sha16("")`. Output
filters (`| tail`), redirections, environment, prefixes, runners, colour and verbosity flags and a `cd` into `cwd`
therefore leave the key unchanged; a filter, a path or another directory changes it.

### 3.8 Suite vocabulary

`suite` is always null or one of `SUITES` (A.2, 200 names); the reference asserts it. Equal runners give equal
strings: `python -m pytest`, `uv run pytest` and `pytest -q` are all `pytest`; `npm test` and `npm run test` are both
`npm test`; `pnpm lint` and `pnpm run lint` are `pnpm run lint`.

### 3.9 Shell kind

`shell_kind(text)` for `action_kind` (2):

1. `edit` when a segment's program is `apply_patch` or `applypatch`, or the command holds a patch text: a
   here-document body, or a word containing LF, with a line matching `PATCH_LINE` (the words of a shell wrapper are
   not searched; its script is, as segments).
2. `command` when `command_info` gives a class other than `other`.
3. `command` when any segment, neutral ones included, has an output redirection (`WRITE_REDIRS`) whose target is not
   `/dev/null`, `NUL`, `nul` or a file descriptor (`FD_TARGET`).
4. Over the non-neutral segments (none → `command`): `command` as soon as one is `sed` with an option starting `-i`
   or `--in-place`, or `find` with an option in `FIND_ACTIONS`, or `git` whose subcommand (its first word after git's
   own options, `GIT_VALUE` taking a value) is outside `GIT_READ` and `GIT_SEARCH`, or has a program outside
   `READ_PROGS` and `SEARCH_PROGS`. Otherwise `search` when some segment is a search (`SEARCH_PROGS`, or `git` with a
   subcommand in `GIT_SEARCH`), else `read`.

So Codex reading files through `sed -n` or `rg` counts like Claude's `Read` and `Grep`, which keeps the two comparable
in METRICS 6.11. Shell edits other than `apply_patch` (`sed -i`, `cat > file`) stay `command`, as METRICS 6.9 assumes.

### 3.10 Masked runs

Agents often pipe a check through `tail` or `grep`, or print its status themselves (real logs, 2026-10-28: about nine in
ten Claude Code build and check commands are masked). A masked command's exit status is the pipeline's or the last
list element's, not the chosen segment's, so `outcome` leaves `failed` null (4.2) — unless the command's own output
states the chosen segment's status in one of the forms below (user's decision, 2026-10-28). Nothing else is read:
every other masked command stays unknown, and the counts of 5 decide.

Terms, over the segments of 3.3 (an LF counts as `;`): the *pipeline* of the chosen segment C is C with the segments
joined to it by `|`; C's *index* is its position in that pipeline from 0; C *owns* the pipeline's status when C is
its last segment or the command is `pipefail` (3.7; then a failing C makes the status non-zero). An *echo* is a segment
whose program is `echo`, with at least one word after it, no redirection and no here-document, and whose first word
after `echo` does not start with `-`; it prints its words joined by single spaces. A *status reference* is `$?`,
`${?}`, `${PIPESTATUS[N]}` (N decimal) or `$PIPESTATUS` (N = 0) (*search* `STATUS_REF`); an echo text is *fixed* when it
holds no `$`, no backquote and is not blank after trimming, and a *status text* when it holds exactly one status
reference and, around it, no other `$` or backquote — its *prefix* and *suffix* are the text before and after the
reference. (`${PIPESTATUS[N]}` is bash's; zsh's `$pipestatus` is not read. Single quotes are not distinguished: a
literal `'$?'` prints `$?`, which never matches below.)

**Printed status.** The output's *last line* is its last line that is not empty after trimming, trimmed; no other line
is read. It *shows* a status text when it starts with the prefix with its leading whitespace removed, ends with the
suffix with its trailing whitespace removed, and what lies between matches `STATUS_VALUE` (an optionally negative
integer, read as in 1). The forms, each with the echo segments ending the command:

| Form | Command after C's pipeline | Counts when | Last line → result |
| --- | --- | --- | --- |
| printed | `;` then one echo with a status text (`; echo $?`, `; echo "exit: $?"`, `; echo EXIT=$?`, `; echo "${PIPESTATUS[0]}"`) | a `$?` reference: C owns the status; a `PIPESTATUS` reference: N is C's index | shows the status text with value V → `{exitCode: V, failed: V ≠ 0}` |
| branches | `&&` one echo with a fixed text, then `\|\|` one echo with a fixed text (different) or a status text with `$?` (`&& echo ok \|\| echo fail`, `&& echo PASS \|\| echo "FAIL $?"`) | C owns the status | equals the first text → `{exitCode: 0, failed: false}`; equals the second (fixed) → `{exitCode: null, failed: true}`; shows the second (status text) with V ≠ 0 → `{exitCode: V, failed: true}` |
| else | `\|\|` then one echo with a fixed text or a status text with `$?` (`\|\| echo FAILED`, `\|\| echo "failed $?"`) | C owns the status | as the second text of *branches*; anything else (the echo did not run) stays unknown |

So `cargo build 2>&1 | tail -3; echo $?` stays unknown (`$?` is `tail`'s), while `… | tail -3; echo
"${PIPESTATUS[0]}"` and `set -o pipefail; … | tail -3; echo $?` count. A `$?` after `&&` or `||` echoes is the status
of the list so far, which these forms read only where it is C's pipeline's. A list before C (`cd x && cargo build;
echo $?`) makes `$?` the list's status: a failing `cd` reads as a failed run (an approximation). The forms apply to every
class: a printed exit code is the chosen segment's own.

**Error filters** (`errorFilter`, build and check only). C's class is `build` or `check`, C is followed by `|` and a
segment whose program is `grep` or `egrep` (`GREP_PROGRAMS`), then only `|`-joined segments whose program is `head` or
`tail` (`LIMITERS`), and that pipeline ends the command. The grep must read only its input and keep matching lines:
its options are short flags whose letters are all in `GREP_FLAGS` (A.6: case-insensitive, extended or Perl patterns, line
numbers, whole words, fixed strings, text, no messages, file names, only matching), the long flags `GREP_LONG_FLAGS`,
`--color` / `--colour` with or without a value, and `GREP_VALUE_OPTIONS` with their value (`-e PATTERN`, context and
`-m` counts; also written `-ePATTERN`, `-A3` or `--name=value`); anything else (`-v`, `-c`, `-l`, `-q`, `-r`, `-f`, …)
makes it no filter. Its patterns are the `-e` values, else its first operand; any further operand is a file, so no
filter. Some pattern must contain `error` (ASCII case-insensitive): `grep -i error`, `grep -E "error|warning"`,
`grep -E "error:|FAIL"`, `grep -e warning -e error`. When such a command produced **no output** — the output text,
stripped of JSON whitespace (space, tab, CR, LF), is empty or is Claude Code's placeholder `(Bash completed with no
output)` (`NO_OUTPUT`, A.6; real logs) — the check found no error line: `{failed: false}` (the exit code is kept) and
`failures` reports `failedCount` 0 (5.4). A filter with output, `| tail` or `| head` alone, and every test-class
command (a test must show its summary, 5) stay unknown.

`masked_status(text, outputText, cwd)`: null when `command_info` is not masked; otherwise the first of a printed
status (when the command has a form above and its last line gives a result) and an error filter with no output
(`{exitCode: null, failed: false}`), else null.

## 4. Results

### 4.1 `result_text`

The text `failures` reads, LF-normalised:

*Blocks text* of a content value: a string as is; an array gives the `text` of its items of type `text`,
`input_text` or `output_text` that have a string `text`, joined by LF; anything else gives the empty text.

- **Claude**: the blocks text of the block's `content`; a first line matching `CLAUDE_EXIT` is removed.
- **Codex**: the *body* of the Codex status (below).
- **omp, Pi, OpenClaw**: the blocks text of the message's `content`; trailing lines that are empty after trimming are
  dropped, then a last line that, trimmed, matches `PI_EXIT`.
- **Factory Droid**: the blocks text of the block's `content` (a string in every recording).

**Codex status** of a result:

1. *Output value*: the result's `output` when the result is an object with both `type` and `output`; else the result
   itself. An object with a `content` array or string gives that `content`. A string that starts, after JSON
   whitespace, with `[` and parses as a non-empty array whose items are all objects with a string `type` is read as
   that array (so a plain `[1,2]` printed by a command stays text).
2. *Text*: the blocks text of the output value, LF-normalised. *Body*: the text, unless step 3 or 4 sets it.
3. When the text has a leading JSON object (1) with a string `output` (Codex's JSON forms always carry one): the body
   is that `output`, LF-normalised; the exit code is its JSON-integer `exit_code`, else its `metadata.exit_code`;
   with neither, a JSON-integer `session_id` means *running* in session `codex:<id>`.
4. Otherwise, when one of the first 8 lines is exactly `Output:` and every line before it matches `CODEX_HEADER`
   (`Exit code: …`, `Wall time: …`, `Chunk ID: …`, …), those lines are the *header* and the body is everything after
   the `Output:` line. A header line matching `CODEX_EXIT` or `CODEX_EXITED` gives the exit code; one matching
   `CODEX_RUNNING` means *running* in session `codex:<N>`. Without such a header there is no exit code.
5. State, the first that applies: *aborted* when the text's first line, trimmed, is `aborted`, starts with `aborted by
   user` or contains `rejected by user`; *timeout* when the body's first line is a prefix match of `CODEX_TIMEOUT`;
   *running* as found above; else *done*.
6. *Patch verdict*, only without an exit code in state done: the body's first line starting with `Success. Updated the
   following files` → not failed; starting with `apply_patch verification failed` or `Failed to apply patch` → failed.

### 4.2 `outcome`

`outcome(source, result, t, tool, input, cwd)` → `{exitCode, failed, t}`, where `t` is the time of the result's line
(Unix ms), passed through. Each source gives an exit code (or null), a `failed` value and a *state*: `done`, `running`
(backgrounded or still running), `aborted` (interrupted, cancelled or rejected by the user) or `timeout`.

**Claude** (`text` = the content as in 4.1 before removing the exit line; `first` = its first line):

- `exitCode` = the number of `first` when it matches `CLAUDE_EXIT` (`text` here is the blocks text, LF-normalised).
- State: `aborted` when `text` starts with one of `CLAUDE_ABORT`; else `timeout` when `first` matches
  `CLAUDE_TIMEOUT`; else `running` when `first` matches `CLAUDE_BACKGROUND`; else, when `tool` is `BashOutput` or
  `TaskOutput`, `running` when `<status>running</status>` occurs (*search* `BASHOUTPUT_STATUS`), and an exit
  code from `<exit_code>N</exit_code>` (*search* `BASHOUTPUT_EXIT`) when the status tag is present and no exit line
  was found.
- `failed` = `exitCode ≠ 0` when an exit code is known, else `is_error == true` (absent → false, as METRICS 6.10).

**Factory Droid**: `exitCode` null, state `done`, `failed` = `is_error == true` (absent → false). The recordings show no
exit-code line, background run or interruption marker in a result, so none is read (TASK-PARSERS 10).

**Codex**: the Codex status (4.1) gives the exit code and the state; `failed` = `exitCode ≠ 0` when an exit code is
known, else the patch verdict, else null.

**omp, Pi, OpenClaw** (lines of the text, each trimmed; `last` / `first` = the last / first non-empty one;
`details` = the message's `details` object):

- `exitCode` = integer `details.exitCode`, else integer `details.exit_code`, else the number of `last` when it matches
  `PI_EXIT` (Pi and omp write `Command exited with code N`; OpenClaw writes `(Command exited with code N)`, with
  `isError` false for a normal non-zero exit).
- State: `running` when `details.status == "running"` or `first` is a prefix match of `PI_RUNNING`; else `timeout` when
  `details.timedOut == true` or one of the last two non-empty lines matches `PI_TIMEOUT`; else `aborted` when `last`
  or `first` matches `PI_ABORT`.
- `failed` = `exitCode ≠ 0` when known, else `isError == true`.

**The call.** When `tool` is given, its command text exists (3.1) and the state is `done`: the state becomes
`running` when the input says so (Claude `run_in_background == true`, OpenClaw `background == true`) or `command_info`
says `background`; otherwise, when `command_info` says `masked`, `masked_status(text, result_text(source, result), cwd)`
(3.10) decides: null → `failed` becomes null (the exit code is kept: it is the pipeline's, not the check's); a result →
its `failed`, and its `exitCode` when that is not null, or null when `failed` is true (a failure word: the exit code
shown is the echo's).

**Final.** In any state other than `done`, `failed` is null; in `running` and `aborted`, `exitCode` is null too.
Unknown sources give `{exitCode: null, failed: null, t}`.

A null `failed` is never a failure: a backgrounded `cargo test`, a run still in progress, a timeout, a user
interruption, and a masked pipeline all leave the run to its counts (METRICS 6.9 *Runs*: a masked `cargo test | tail`
with `1 failed` in its output still fails; with neither a status nor counts it is not a run).

### 4.3 Sessions that outlive the call

Codex's unified exec returns after `yield_time_ms` (10 s by default) while the command keeps running, and a later
`write_stdin` call (often with empty `chars`) receives the rest of the output and the exit code; Claude's background
`Bash` is read later with `BashOutput` / `TaskOutput`. To give the original command its outcome:

- `running_session(source, result)`: Codex — the session of the Codex status (4.1 steps 3–4), whatever its final
  state. Claude — a first line matching `CLAUDE_BACKGROUND` → `claude:<id>`. Otherwise null.
- `continuation(source, tool, input)`: Codex `write_stdin` with an integer `session_id` → `codex:<id>`; Claude
  `BashOutput` / `TaskOutput` with a non-empty string `bash_id`, `shell_id` or `task_id` (in that order) →
  `claude:<id>`. Otherwise null.
- `notification(source, line)`: Claude Code reports the end of a background command not through a tool call but with a
  line of type `attachment` whose `attachment.type` is `queued_command`, `attachment.commandMode` is
  `task-notification` and `attachment.prompt` is a string (checked against real logs, counts and markers only,
  2026-10-28). Its text, LF-normalised, holds `<task-id>…</task-id>` (*search* `NOTIFY_TASK`) and a summary (*search*
  `NOTIFY_SUMMARY`); when the summary, trimmed, matches `NOTIFY_EXIT` (`Background command "…" completed (exit code
  N)` or `… failed with exit code N`) the result is `{session: "claude:<task id>", exitCode: N}`. A stopped command,
  an agent or monitor task, any other mode and a notice without a task id give null. The task id is the id of
  `CLAUDE_BACKGROUND`, which excludes the period that ends that sentence (`… with ID: bk3x9a. Output is being written
  to: …`).

The event layer (METRICS 6.9 *Composing the stream*, *Sessions*) keeps, per agent, a map from session key to the action
whose result left that session running (a call that is not itself a continuation): the action keeps that result's
outcome (state `running`) and gets no `failures` yet. A result of a continuation call whose session is in the map
appends its `result_text` to that action's output (Codex parts are slices of one output stream and are concatenated
as they are; Claude parts are joined with LF); when its own `outcome` has a non-null
`exitCode` and a non-null `failed` (state `done` with an exit code), that outcome (with its `t`) becomes the original
action's outcome — with `failed` null when the original command is `masked` (3.7) — `failures` is computed over the
accumulated output with the original command's `info` and `cwd`, and the key leaves the map. The continuation call
itself stays kind `other`, with its own outcome. A result that leaves the same session running again replaces the
entry. Only digests, counts and the session key are kept; the accumulated text is held in memory only until the
outcome is set, and `failures` applies its budget (5.1) to the whole of it. The live engine may drop the entries of
tasks other than the agent's current one: nothing shows them, and the history analysis reads whole files.

### 4.4 Codex code mode

Recent Codex versions (on this Mac, every rollout since 2026-07) let the model run a JavaScript cell instead of calling
tools one by one: a `custom_tool_call` named `exec` whose input is the script, such as
`text(await tools.exec_command({cmd: "cargo test", yield_time_ms: 10000}));`, and whose output lists a header item
`Script completed` / `Wall time … seconds` / `Output:` and then one `input_text` item per `text(…)` printed. The tools a
script calls are the actions (METRICS 6.9 *Composing the stream*); the script itself is none. Verified against real
logs through counts and structural skeletons only (2026-10-28).

`code_script(source, tool, input)`: for Codex `exec` with a string input that is not a JSON object (1), the script is
read as below; a script that cannot be tokenised, or that calls no tool, gives null (and the call keeps the rule of
3.1). Otherwise `{calls, plain, prints}`:

- **Tokens.** Whitespace (space, tab, CR, LF, FF, VT), `//` and `/* */` comments are skipped. A string is `'…'` or
  `"…"` (a raw line break ends it unterminated: the script cannot be tokenised); a template is `` `…` ``, whose
  `${…}` parts are tokenised recursively up to their closing brace and make its value unknown. Escapes in both: `\n`
  `\r` `\t` `\b` `\f` `\v`, `\0` not followed by a digit, `\xHH`, `\uHHHH` (surrogate pairs combine; a lone
  surrogate is U+FFFD), `\u{H…}` up to U+10FFFF, a backslash before a line break (CR LF, LF, CR, U+2028, U+2029) is
  removed with it, and any other escaped character stands for itself. A template's raw CR LF and CR are LF. A `/`
  starts a regular expression (up to an unescaped `/` outside `[…]`, then letters, digits, `_` and `$`; a line break
  first makes it a plain `/`) unless the previous token is a number, string, regular expression, `)`, `]`, `}` or an
  identifier other than the keywords `JS_REGEX_AFTER`. An identifier is `[A-Za-z_$][A-Za-z0-9_$]*`; a number token is
  `JS_NUMBER_TOKEN`, valued only when it matches `JS_NUMBER` (an integer, else a double). Any other character is a
  one-character punctuator.
- **Calls.** Every `tools` `.` NAME `(` … `)` whose `tools` does not follow a `.`, the closing parenthesis found by
  bracket depth over `(` `[` `{` and `)` `]` `}`, is a call in source order: `tool` = NAME, `digest` = `sha16` of the
  script text from `tools` through that `)`, and `input` = its single argument when that is a literal (else null; no
  argument → null). Literals: a string with a known value, a valued number (with an optional leading `-`), `true`,
  `false`, `null`, an array of literals, and an object whose keys are identifiers, strings or integers — a member whose
  value is not a literal (a variable, an expression, a template with `${…}`, a shorthand or a spread) is left out, and
  a repeated key keeps its last value.
- **Plain.** The script is *plain* when its tokens are only `;` and these statements, read in order, each CALL being
  `await` followed by a call above:
  - `const`, `let` or `var` NAME `=` CALL — binds NAME to the call; `const`, `let` or `var` NAME `=` LITERAL — binds
    NAME to the literal, which the arguments of later calls may use in place of a literal (a name bound again keeps
    its latest binding);
  - CALL alone;
  - *printing*: `text ( CALL )`, `text ( NAME )` with NAME bound to a call — mode `result`; `text ( ( CALL ) .
    output )`, `text ( NAME . output )` — mode `output`.

  `prints` lists the printing statements in order as `[call index, mode]` (empty when the script is not plain). In a
  plain script the calls' inputs are read with the bindings made before them; otherwise without any binding.

`code_items(source, result)`: Codex only, and `result` must be an object. Its *output* is `result.output` when the
object has both a `type` and an `output` member, else the object itself; unlike 4.1 step 1, a `content` member is not
unwrapped and a string is not parsed as JSON. When the output is a non-empty array whose first item is an object with a
string `text` whose first line, LF-normalised, is `Script completed` (`CODE_COMPLETED`, A.6), the result is the list of
the other items in order, each item's string `text` LF-normalised, or null for an item that is not an object with a
string `text` (whatever its `type`). Anything else gives null: a string output (a failed tool call, a script still
running), an object, an empty array, a first item that says `Script failed`.

The event layer matches the printing statements of a plain script with the items in order — the k-th statement with
the k-th item — only when `code_items` is not null and has exactly as many items as `prints`; a call printed more than
once takes its first item. The item is the call's Codex result, a string read like an output value: in mode `result`
usually a leading JSON object with `exit_code` or `session_id` (4.1 step 3); in mode `output` the command's output
alone, so the run is left to its counts (5). Every other call of a script gets no result. Through `session_id` results
and `write_stdin` calls, scripts take part in 4.3 like direct calls.

## 5. `failures`

`failures(info, outputText, cwd, failed)` → `{fingerprints, failedCount, passedCount, framework}`: `info` is the
command's `command_info` (only its suite is read), `outputText` its `result_text`, `failed` its `outcome.failed`.

### 5.1 Budget and cleaning

- **Budget**: when the text's UTF-8 form is at most `HEAD_BYTES + TAIL_BYTES` = 64 KiB + 192 KiB = 262 144 bytes, it
  is one segment. Otherwise there are two: the *head* is the first 65 536 bytes, the cut moved back to the start of a
  character (never inside a UTF-8 sequence), cut again after its last LF (kept whole when it has none); the *tail* is
  the last 196 608 bytes, the cut moved forward to the start of a character, starting after its first LF (whole when
  none). Compile errors print first and summaries last, so both survive; the middle is dropped.
- **Cleaning**, per segment: remove OSC sequences (`ANSI_OSC`, hyperlinks included), then CSI (`ANSI_CSI`), then
  two-character escapes (`ANSI_FE`); LF-normalise (so a progress bar's bare CRs become line breaks); remove the
  remaining C0 controls except tab and LF, and DEL (`CONTROL`); replace U+0085, U+2028 and U+2029 by a space; split at
  LF; right-trim each line and keep at most its first `MAX_LINE` = 2 000 characters.
- **Workspace prefixes**: pnpm, yarn and turbo prefix interleaved lines with `<package> <script>: ` or
  `<package>:<script>: `. A line *has* the prefix `p` when `WORKSPACE_PREFIX` matches at its start, `p` is the match's
  first group, and the character after `p:` is a space or the end of the line. Prefixes that lines have at least
  `WORKSPACE_MIN` = 3 times in the segment are removed from the lines that have them (with the colon and one
  following space).

### 5.2 Masking

A fingerprint never carries a volatile part, so the same failure gives the same digest across runs.

- **Message** (`mask`), in this order: temporary paths (`TMP_PATH`) → `<tmp>`; every `cwd/` removed (`cwd` as in 3.3;
  `/` and `\` both match its separators, letters compare ASCII case-insensitively for a drive `cwd`); home prefixes
  (`HOME_PATH`) → `~/`; `UUID` → `<uuid>`; `ISO_TIME` → `<time>`; `TIME_OF_DAY` →
  `<time>`; `DATE` → `<date>`; `HEX_ADDR` → `0x?`; `FILE_POS_BRACKET` → `\1:[N]`; `FILE_POS` → `\1:N`;
  `FILE_POS_PAREN` → `\1(N)`; `LINE_WORD` → `\1\2N`; `DURATION` → `<dur>`; `LONG_HEX` → `<hex>`; `PID_PORT` → `\1 N`;
  runs of spaces and tabs → one space; trimmed; cut to `MAX_MESSAGE` = 200 characters. File names and error codes
  survive; line and column numbers, durations, times, addresses and temporary directories do not. `TMP_PATH`,
  `HOME_PATH` and the `cwd/` removal only match where a path starts: not right after a letter, digit, `.`, `_`, `~`,
  `/`, `\` or `-` (`NOT_IN_PATH`), so `/opt/mirror/Users/dev/x` and `/usr/tmp/x` keep their text.
- **Test name** (`mask_name`): temporary paths, the `cwd/` prefix and home prefixes as above; space runs collapsed;
  trimmed; cut to 200 characters. Nothing else changes: a test's name is its identity (cargo doc-test names get
  `(line N)` from their recogniser).
- **File** (`norm_file`): trimmed, then every `'`, `"` and `` ` `` at either end removed (paired or not); a `file://`
  prefix removed; `\` → `/`; `TMP_PATH` → `<tmp>`; made cwd-relative (3.3) when inside `cwd`; `HOME_PATH` → `~/`;
  `//` → `/` repeatedly; leading `./` removed repeatedly.

### 5.3 Recognisers

Each segment is read line by line by a fresh set of recognisers, tried in this order: pytest, unittest, cargo
(libtest and nextest), rustc, Swift (swift-testing, XCTest, xcodebuild, swiftc), Go, JavaScript (jest, vitest, mocha,
bun, deno), checkers (tsc, eslint, ruff, black, mypy, pyright), .NET (MSBuild, VSTest, Microsoft Testing Platform),
JVM (JUnit through Gradle and Surefire, javac, kotlinc, Maven), misc (rspec, phpunit, ctest), C and C++ (gcc, clang,
linkers, make). At line `i` the first recogniser that *claims* it consumes one or more lines (lookahead included) and
the reader continues after them; a line no one claims is left for the fallback (5.6). Claiming a line (warnings, notes,
progress, noise) without emitting anything keeps it from other recognisers, which never see it: a state that waits for
such a line (a listing mode) simply continues. Within a recogniser, rules are tried in the order of A.5; a rule guarded
by a state (a listing that is open) is tried first while the state holds, and a state change made while trying rules
persists even when the line ends up unclaimed. When a segment ends, the recognisers emit what their state still holds,
in recogniser order, after everything emitted while reading.

A recogniser emits **failures** — `test(framework, name, file)` or `error(framework, file, code, message)` — and
**summaries** `(framework, family, failed, passed, mode)` with family `test` or `error` and mode `last` (only the last
one counts) or `sum` (all of them add up). Failures describe failing tests and errors only: passing tests, warnings,
notes and skipped tests are never failures. The framework names below are part of the output (`framework`) and of
every canonical string (5.7); synthetic texts are given in quotes.

| Recogniser | Failure frameworks | Summary frameworks |
| --- | --- | --- |
| pytest, unittest | `pytest`, `unittest` | `pytest`, `unittest` |
| cargo, rustc | `cargo` (libtest and nextest failures alike), `rustc` | `cargo` (sum), `nextest`, `rustc` (sum) |
| Swift | `swift-testing`, `xctest`, `swiftc` | `swift-testing`, `xctest`, `xcodebuild` |
| Go | `go` | `go` |
| JavaScript | `jest`, `vitest`, `mocha`, `bun`, `deno` | `jest` (sum), `vitest` (sum), `mocha`, `bun`, `deno` |
| checkers | `tsc`, `eslint`, `ruff`, `black`, `mypy`, `pyright` | `tsc`, `found-errors` (5.4), `eslint`, `ruff`, `format`, `mypy`, `pyright` |
| .NET | `msbuild`, `dotnet` (VSTest and MTP failures) | `msbuild`, `dotnet` (VSTest, sum), `dotnet-mtp` |
| JVM | `junit`, `javac`, `kotlinc` | `gradle` (sum), `maven`, `javac` |
| misc | `rspec`, `phpunit`, `ctest` | `rspec`, `phpunit`, `ctest` |
| C, C++ | `cc` (linker errors: code `ld`, message "undefined symbol " + symbol or "undefined reference to " + symbol) | `cc` (sum) |

*Counts in a summary line* (`parse_counts`): the text is split at `,` (vitest: at `|`); each part that matches
`[ \t]*([0-9]+) ([A-Za-z][A-Za-z ]*?)[ \t]*` adds its number to its label (equal labels add up); a missing label is
0. unittest reads `name=N` pairs (a later pair replaces an earlier one), phpunit `Label: N` pairs; nextest first
removes parenthesised notes ` (…)`. Passed counts computed by subtraction never go below 0.

| Recogniser | Failures | Summaries |
| --- | --- | --- |
| **pytest** | `PYTEST_FAIL` (`FAILED` / `ERROR` lines, xdist prefix allowed) and `PYTEST_VERBOSE`: node id → file = the part before the first `::`, name = the rest (a collection error has an empty name) | `PYTEST_SUMMARY`: failed = `failed` + `error(s)`, passed = `passed`; `PYTEST_NONE` (`no tests ran`): 0 / 0 |
| **unittest** (also Django) | `UNITTEST_FAIL`: name = the dotted path in parentheses, plus `.<method>` unless it already ends with it | `UNITTEST_RAN` sets the total; the next `UNITTEST_OK` / `UNITTEST_FAILED`: failed = `failures` + `errors` + `unexpected successes`, passed = total − failed − `skipped` − `expected failures` (≥ 0) |
| **cargo** | `LIBTEST_LINE` with `FAILED`, `LIBTEST_STDOUT`, the names listed under a `failures:` line whose next line matches `LIBTEST_LISTED` (until a line that does not, a listed name being one word before ` - `), `NEXTEST_FAIL` (test name only, binary id dropped); doc-test names get `(line N)` | `LIBTEST_RESULT`, mode sum (one line per test binary); `NEXTEST_SUMMARY` as framework `nextest`: failed = `failed` + `timed out` + `exec failed` + `leaky failed`, passed = `passed` (parenthesised notes ignored). `NEXTEST_OTHER` lines (`PASS`, `TRY n …`, `FLAKY`, …) are claimed only, so a flaky test that passed on retry is not a failure |
| **rustc** | `RUST_HEADER` (`error[E…]: message`) whose `RUST_LOC` (` --> file:line:col`) comes within the next 3 lines before another header: file, code `E…` (or empty), message; header to location are consumed. A warning header with its location is claimed only. A header without a location is left unclaimed | `RUST_COULD_NOT` (`could not compile … due to N previous errors`, 1 without a number) and `RUST_ABORTING`, family error, mode sum. `RUST_STATUS` lines are claimed only |
| **swift-testing** | `ST_FAILED` (a symbol, then `Test NAME failed after …`), except `Test run with …`; ` with N test cases` is removed from the name. `ST_ISSUE` and `ST_OTHER` are claimed only | `ST_RUN`: total N; failed = 0 when it passed, else the number of distinct failed names seen in the segment (none → the issue count, else 1); passed = N − failed |
| **XCTest** | `XC_CASE` with `failed`, `XC_ERROR`, and the indented lines after `Failing tests:` (`XB_LISTED`, lines ending `:` are group headers), all with the name normalised (`xctest_id`: strip `-[` … `]`, spaces and `/` → `.`, drop `()`, keep the last two dot components: `Class.method`) | `XC_EXECUTED` (`Executed N tests, with [S tests skipped and] M failures …`), mode last: failed = distinct failed tests seen, or M when none; passed = N − failed − S |
| **xcodebuild** | — (its compiler errors are swiftc's or clang's) | `XB_COUNT` (`(N failures)` after `The following build commands failed:`), family error. `XB_RESULT` (`** TEST FAILED **` …) is claimed only |
| **swiftc** | `SWIFTC_ERROR` (`file.swift:line:col: error: message`) | — (`SWIFTC_WARN`, `SWIFT_NOISE` claimed only) |
| **Go** | `GO_FAIL` (`--- FAIL: name (…)`, parents and subtests alike); `GO_ERROR` (an unindented `file.go:line[:col]: message`: compiler and vet) | at the segment's end, when a package line (`GO_PKG`) or a `--- FAIL` / `--- PASS` line was seen: failed = distinct failed tests + packages marked `[build failed]` or `[setup failed]`; passed = distinct `--- PASS` names, else the number of `ok` packages, else null |
| **jest** | `JEST_BULLET` (`  ● title`, except `Console`) with file = the last `FAIL` header (`JEST_FILE`) | a `JEST_TESTS` line (with `Test Suites:` before it): failed = tests failed, or failed suites when no test failed; passed = tests passed; mode sum (pnpm runs one jest per package) |
| **vitest** | `VITEST_FAIL` (` FAIL  file > name`), `VITEST_LOAD` (` FAIL  file [ file ]`: empty name), `VITEST_X` (`× name`) under the last `VITEST_FILE` header | `VITEST_TESTS` (with `VITEST_FILES` before it), as jest, mode sum |
| **mocha** | after `N failing`: each numbered block `N) title` with its more-indented continuation lines, up to the part ending `:`, joined by spaces without the colon | at the end: failed = `N failing` (0 if absent), passed = `N passing` |
| **bun**, **deno** | `BUN_FAIL` (`(fail) name`); `DENO_FAILURE` (`name => file:line:col`) | bun: `N pass` / `N fail` lines; deno: `DENO_SUMMARY` |
| **tsc** | `TSC_PAREN` and `TSC_PRETTY`: file, code `TS…`, message. The pretty `Errors  Files` table is claimed | `TSC_FOUND` (`Found N errors in …`), family error |
| **eslint** | a message line (`ESLINT_MSG`) of severity `error` under the last file header — an absolute path line (`ESLINT_FILE`) directly followed by a message line: rule = the last column after two or more spaces (empty for parse errors); `ESLINT_UNIX` with `Error` | `ESLINT_SUMMARY`: failed = errors |
| **ruff**, **black** | `RUFF_CONCISE` (`file:line:col: CODE message`, `SyntaxError` / `invalid-syntax` included); `RUFF_HEADER` (`CODE message`) with a `RUFF_LOC` line within 3 lines (the full format); `RUFF_WOULD` / `BLACK_WOULD` (code `format`) | `RUFF_PASSED` (`All checks passed!`): 0; `FORMAT_SUMMARY`: N files |
| **mypy** | `MYPY_ERROR`: file, code in `  [code]`, message (notes claimed only) | `MYPY_FOUND`: N; `MYPY_SUCCESS`: 0 |
| **pyright** | `PYRIGHT_ERROR`, consuming the more-indented continuation lines; the rule is the message's own `(reportX)`, else the one ending the last continuation line (`PYRIGHT_RULE`), and is removed from the message | `PYRIGHT_SUMMARY`: N errors |
| **MSBuild** | `MSB_ERROR` (`file(line[,col]): error CODE: message [project]`; the project is dropped); `MSB_TOOL_ERROR` (`CSC : error CS…`, file empty) | `MSB_COUNT` (`N Error(s)`) and the terminal logger's `MSB_TERMINAL`, family error |
| **VSTest**, **MTP** | `VSTEST_FAILED` (`Failed Name [time]`), `MTP_FAILED` (`failed Name (time)`) | `VSTEST_SUMMARY` (`Failed!` / `Passed!` per test assembly), mode sum; `MTP_SUMMARY` |
| **JUnit** | `GRADLE_TEST` with `FAILED`, `SUREFIRE_TEST`, `SUREFIRE_OLD`, and the list under `[ERROR] Failures:` / `[ERROR] Errors:` (`SUREFIRE_LISTED`, except `Run N:` lines), all as `junit_id`: the class's last dot component, `.`, the method without a trailing `()` | `GRADLE_SUMMARY` (`N tests completed, F failed[, S skipped]`), mode sum: passed = N − F − S; `SUREFIRE_RUN` lines with nothing after the counts (the aggregate), mode last: failed = failures + errors, passed = run − failures − errors − skipped |
| **javac**, **kotlinc** | `JAVAC_ERROR`, `MAVEN_JAVAC` (`[ERROR] file:[line,col] message`), `KOTLIN_ERROR`, `KOTLIN_ERROR_OLD` | `JAVAC_COUNT` (`N errors`), `MAVEN_ERRORS` (`[INFO] N errors`), family error |
| **rspec** | `RSPEC_FAILED` (`rspec ./file:line # description`): file, name = description; `RSPEC_LOAD` (error, code `load`) | `RSPEC_SUMMARY`: failed = failures + errors outside examples, passed = examples − failures − pending |
| **phpunit** | `PHPUNIT_ITEM` (`N) Class::method[ with data set …]`) only inside a `There was/were N failure(s)/error(s):` section (`PHPUNIT_SECTION`; skipped, risky and other sections list tests that did not fail) | `PHPUNIT_TESTS`: failed = Errors + Failures, passed = Tests − failed − Skipped − Incomplete; `PHPUNIT_OK` |
| **ctest** | `CTEST_LINE` with `Failed`, `Exception` or `Timeout`; the list after `The following tests FAILED:` (`CTEST_LISTED`) | `CTEST_SUMMARY`: failed, passed = total − failed |
| **C, C++** | `CC_ERROR` for C-family files (`CC_EXT`): code = the bracketed flag with `-Werror,` removed and `-Werror=` → `-W`; linker: symbols after `LD_UNDEFINED_ARCH` (`LD_SYMBOL`) and *search* `LD_REFERENCE`, code `ld` | `CC_GENERATED` (`N errors generated.`), family error, mode sum |

Details the table leaves out: a jest bullet takes the file of the last `FAIL` header, and a `PASS` header empties it;
a `Tests:` line emits a summary even without a `Test Suites:` line, and a `Test Suites:` line followed by no `Tests:`
line emits one at the next `Test Suites:` line or the segment's end, with failed = failed suites and passed = 0 (vitest
alike with `Test Files`). bun's `N pass` / `N fail` lines replace earlier ones. mocha's passed count is 0 without a
`passing` line, and its block indentation counts spaces only. A pyright message is its first line only (continuation
lines only supply the rule). The Surefire list skips lines starting exactly `[ERROR]   Run ` and ends at the aggregate
`Tests run:` line, an `[INFO]` line, or a bare `[ERROR]`. `XB_COUNT` counts only right after `The following build
commands failed:` and its indented lines.

### 5.4 Counts and framework

Summaries named `found-errors` (`FOUND_ERRORS`: `Found N errors.`, written alike by older tsc and by ruff; `(n fixed,
m remaining)` counts `m`) belong to `tsc` when the result holds a tsc failure or the suite is `tsc` / `vue-tsc`, else to
`ruff`. A `nextest` summary drops every `cargo` summary of the same result (nextest's captured output repeats libtest
lines). Then each framework's summaries combine by mode (sum adds counts; a null passed stays null only when all are
null; last keeps the last), in order of first appearance.

- When some framework has a test-family summary: `failedCount` = the sum of their failed counts, `passedCount` = the
  sum of their non-null passed counts (null when all are null).
- Else, when some has an error-family summary: `failedCount` = their sum, `passedCount` = null.
- Else, when there are failures: `failedCount` = the number of distinct canonical strings (5.7), `passedCount` =
  null — a compiler that prints no total still yields a count, so a masked `swift build | tail` with errors fails.
- Else, when a line, trimmed, fully matches a *completion marker* — `COMPLETE_CARGO` (`Finished … target(s) in …`),
  `COMPLETE_SWIFTPM` (`Build complete!`), `COMPLETE_XCODEBUILD` (`** BUILD SUCCEEDED **` and the other actions),
  `COMPLETE_GRADLE` (`BUILD SUCCESSFUL`), `COMPLETE_MAVEN` (`[INFO] BUILD SUCCESS`), `COMPLETE_PRETTIER`, `COMPLETE_VITE`
  (`✓ built in …`), tried in that order on each line: `failedCount` = 0, `passedCount` = null, and `framework` = the
  marker's name in `COMPLETION_FRAMEWORKS`, from the first such line in output order. These tools print the line only
  when they finish without errors, so a build piped through `tail` still reads as passed (real logs: nine in ten
  Claude Code build and check commands end in a pipe, which hides the exit code).
- Else, when `failed` is false, `info.errorFilter` is true and the output shows no output (3.10): `failedCount` = 0,
  `passedCount` = null, `framework` = null.
- Else both are null.

`framework` = among the chosen frameworks, the first (in order of appearance) with a non-zero failed count, else the
first with a non-zero passed count, else the first; with no summary, the framework of the first failure in output
order; else null.

These counts fit METRICS 6.9: a run fails when `failed` is true or `failedCount > 0`, and passes when `failed` is false
or `failedCount == 0` with a `passedCount` that is not 0 (null for checkers and builds, which count no passes). A retried flaky failure that the summary still counts (Gradle's
test-retry: `12 tests completed, 1 failed` with `BUILD SUCCESSFUL`) is a failure, as the tool states it.

### 5.5 Deviations from the corpus authors' reading

The corpus outputs were written with their authors' own expectations before the reference existed. Where these rules
deliberately differ, `task_parser_corpus.OVERRIDES` records it: pytest errors count as failures; a tool without a
summary counts its distinct failures; Go counts unbuilt packages as failures and `ok` packages as passes; a jest or
vitest suite that fails to load counts once; rspec errors outside examples count as failures.

### 5.6 Generic fallback

Only when no recogniser emitted a failure **and** `failed` is true, every unclaimed line is tried against
`GENERIC_FILE` (`file.ext:line[:col]: [fatal] error[CODE]: message`), else `GENERIC_PREFIX` (`error: …`, `ERROR: …`,
`fatal: …`), else `GENERIC_EXCEPTION` (`TypeError: …`, `ModuleNotFoundError: …`; the type is the code), each an error
of framework `generic`. With `failed` false or null the fallback never runs, so a passing run that logs the word
`error` has no fingerprints.

### 5.7 Fingerprints

Each failure gives the canonical string `framework | kind | file | code | message` (fields joined by `|`, kind `test`
or `error`): a test has `code` = its masked name and an empty message (a failing test is the same failure whatever its
assertion says, so convergence follows tests, not messages); an error has its normalised file, its code (or empty) and
its masked message. `fingerprints` = the sorted distinct `sha16` of the distinct canonical strings, at most
`MAX_FINGERPRINTS` = 256 (the smallest digests). The fixture lists the canonical strings too (`canonical`) so ports
can compare them before hashing.

## 6. `edit_hunks`

`edit_hunks(source, tool, input, cwd)` → a list of `{file, old, new}` in input order; `[]` for a call that is not an
edit or whose input cannot be split.

### 6.1 Normalisation

- **file** = `sha16` of the path with `\` → `/`, a `file://` prefix removed, made cwd-relative (3.3) when inside
  `cwd`, then `//` → `/` repeatedly, leading `./` removed repeatedly and `/./` → `/` repeatedly, in that order.
  Absolute paths outside `cwd` stay absolute.
- **Text lines** of a string: LF-normalised, split at LF, each line right-trimmed, empty lines removed from both ends.
- **Hunk** of an old and a new text: their text lines, minus the lines common to the start of both and then the lines
  common to the end of both; `old` and `new` = `sha16` of the remaining lines joined by LF. Context lines therefore
  never matter: a revert gives exactly swapped digests even when it quotes more or less context, an `apply_patch`
  chunk and an `Edit` of the same change give the same digests, and a change of blank lines or trailing spaces only
  gives `old == new` (no change, METRICS 6.9).
- **Whole-file write**: `old` = null, `new` = `sha16` of the content's text lines joined by LF.
- **Deletion**: `old` = null, `new` = `sha16("")` (an empty file).
- **Rename**: a deletion of the old path, then the moved file's hunks under the new path (its whole new content is not
  in the input, so no whole-file write is emitted for it).

### 6.2 Per tool

| Source, tool | Hunks |
| --- | --- |
| Claude `Edit` | one hunk of `old_string` → `new_string` in `file_path` (`replace_all` changes nothing) |
| Claude `MultiEdit` | one hunk per entry of `edits` with string `old_string` and `new_string`, same file |
| Claude `Write` | whole-file write of `content` |
| Claude `NotebookEdit` | the cell is a file of its own, `notebook_path#cell` (`cell` = `cell_id`, else the decimal `cell_number`, else empty): `edit_mode` `replace` (default) → whole-file write of `new_source`; `delete` → deletion; `insert` → a hunk `""` → `new_source` in `notebook_path` |
| Codex `apply_patch` | the patch (6.3): a custom tool's input string (one that is not a JSON object); a function's string `input`, else its string `patch` |
| Codex shell tools; any shell tool | every patch text in the command (3.9): heredoc bodies of `apply_patch <<'EOF'`, `bash -lc` scripts and argv elements (`["apply_patch", "*** Begin Patch…"]`) |
| Pi, OpenClaw, omp `edit` | an `input` string is read by its shape (6.4) for every family. Otherwise (omp's other shapes first, 6.4): one hunk per `{oldText, newText}` with string values in `edits` (an array; a single object; a string holding a JSON array or object), then the legacy top-level `oldText` / `newText` when that pair is not already listed, in `path` |
| Pi, omp, OpenClaw `write` | whole-file write of `content` at `path` |
| OpenClaw `apply_patch` | the patch in `input` |
| omp `ast_edit` | `[]` (a structural rewrite; its effect is not in the input) |
| Factory Droid `Create` | whole-file write of `content` at `file_path` |
| Factory Droid `Edit` | one hunk of `old_str` → `new_str` (else `old_string` → `new_string`) in `file_path` |
| Factory Droid `MultiEdit` | one hunk per entry of `edits` with such a pair, in `file_path` |

### 6.3 Codex patches

The text is LF-normalised. The envelope starts after the first line equal to `*** Begin Patch` after trimming (from the
first line when there is none) and ends at a line equal to `*** End Patch` after trimming. Lines that, right-trimmed,
match `PATCH_HEADER` open sections: `*** Add File: P`, `*** Delete File: P`, `*** Update File: P`; paths are
trimmed. A `*** Move to: Q` line inside an Update section (anywhere; the last one wins) sets its destination; outside
one it is an ordinary line. Lines before the first section are ignored; other lines belong to the open section.

- **Add**: a whole-file write of the section's lines that start with `+`, without the `+`.
- **Delete**: a deletion.
- **Update**: chunks start at lines beginning `@@` (the first may have none); a line exactly `*** End of File` is
  skipped; a line
  starting `-` is old only, `+` new only, ` ` or anything else (an empty line included) context for both (one leading
  space removed). Each chunk with at least one `-` or `+` line gives a hunk of its old lines → new lines. With `Move
  to`, a deletion of `P` first and the hunks under `Q`.

### 6.4 omp edit modes

omp's `edit` tool takes one of five shapes, chosen by a setting (default `hashline`):

| Shape | Mode | Hunks |
| --- | --- | --- |
| `input` string with a `PATCH_HEADER` line | `apply_patch` | as 6.3 |
| `input` string with a `HASHLINE_HEADER` line (`[PATH#TAG]`) | `hashline` | per file section: an insertion (`HASHLINE_INSERT`: `PUT <N:`, `PUT >N:`, `PUT >N*:`, `PUT >$:`) with its following `+` lines → a hunk `""` → the body (each line without its `+`); `REM` → deletion; `MV DEST` → deletion of the current path, which becomes `DEST`. Replacements (`PUT N.=M:`, `PUT N*:`) and `CUT` name line numbers, not the old text, so they give no hunk |
| any other `input` string (`*** Edit File:`, the `sloppy` mode) | `sloppy` | `[]` |
| `old_string` and `new_string` with `path` | `replace` | one hunk |
| `edits` entries with `diff` or `op`, with `path` | `patch` | per entry: `op` `delete` → deletion; `create` → whole-file write of `diff` (the leading `+` removed when every non-empty line has one); otherwise the `diff` chunks as 6.3 Update, under `rename` when given (after a deletion of `path`) |

The hashline, sloppy and patch shapes are read from omp 18.13.3's schemas (`edit/schemas.ts`, `edit/hashline-compact.md`)
and are not verified against real sessions.

## 7. Privacy

User-approved (2026-10-28, METRICS 6.9 *Privacy*). The functions read command lines, tool output and edit text in
memory, while the line that holds them is parsed; the 4.3 accumulation holds a running command's output only until its
outcome is known. Nothing of the input is stored, displayed, logged or reported: only kinds, classes, flags, suite
names from the fixed vocabulary, 16-hex digests, counts, exit codes and times leave these functions. Canonical strings
(5.7) exist only to be hashed. The fixture's `canonical` lists come from the fictional corpus.

## 8. Notes for the event layer

- `kind`, `command`, `outcome`, `failures` and `hunks` of METRICS 6.9 are `action_kind`, `command_info` (its four
  contract fields), `outcome`, `failures(info, result_text(…), cwd, outcome.failed)` and `edit_hunks`. METRICS 6.9
  *Composing the stream* says which lines give calls and results per source, and passes every function of one action
  the agent's `cwd` at the call's line (METRICS 4 `cwdPath`).
- `command_info` and `failures` apply to actions of kind `command` with command text (3.1); a `read` or `search` shell
  command has no `command` field, and neither has a shell call without readable text.
- An edit whose outcome has `failed` true (Claude `<tool_use_error>`, a rejected patch) did not apply; METRICS 6.9
  already treats it as not effective.
- Continuations (4.3) attach late outcomes to the original command.

## 9. Reference and fixture

`python3 spec/tools/task_parsers.py` checks every case of `spec/fixtures/task-parser-cases.json` and Appendix A;
`--write` regenerates both after checking the hand expectations of the corpus (the digests there are computed with
`hashlib` from hand-normalised text, independently of the reference). Each case has `group` (`actionKind`, `command`,
`outcome`, `resultText`, `session`, `notification`, `codeScript`, `codeItems`, `maskedStatus`, `failures`, `edits`),
its inputs and `expect`. `command` cases also expect `masked`, `background` and `errorFilter`; `failures` cases list
`canonical`. Ports run every case through their own functions and
compare the outputs exactly.

## 10. Known approximations

- Shell parsing is a deterministic subset of POSIX shell: no aliases, functions, variable expansion or globbing, and
  `$(…)` is opaque. A command assembled at run time (`eval`, scripts) is classified by what is written.
- The class of a package script, `make` target or task comes from its name, not its body; a script named `ci` that
  runs the tests is `other`.
- `masked` follows the operators, not the commands: `cargo test | tee log` is masked unless `pipefail` is set, and a
  loop's status is not recognised as masking. Function definitions are not recognised (their bodies classify as if
  they ran), and wrapper scripts nested more than 4 deep are ignored.
- A recogniser that misses a format leaves the fallback to a generic pattern only for failed runs; an unrecognised
  passing run has no counts and relies on its exit code.
- Fingerprints of compiler errors drop line numbers, so two identical messages in one file count once.
- A Codex command whose own output starts with a JSON object holding a string `output` is read as Codex's JSON
  form (4.1).
- Codex code mode (4.4): a script that is not plain (`Promise.all`, loops, variables between calls; about a fifth of
  scripts on this Mac) gives its calls no results, so their commands are not runs; a script still running after its
  yield (`Script running with cell ID N`, finished by a later `wait` call) gets no results either.
- A Claude background command's notice carries its exit code but not its output: the run is decided by the exit code
  alone (and is unknown when the command was `masked`).
- Factory Droid: the tool names come from public recordings, the input fields (`command`, `file_path`, `content`,
  `old_str` / `new_str`, `edits`) from the recorded `Read`, `Edit`, `LS`, `Grep` and `Execute` inputs and from the shape
  of Claude's tools (both spellings of the replacement pair are read); no result format beyond `is_error` is known, so
  a failed command has no exit code and a background command reads as finished.
- Not verified against real logs (none were read for them): Claude `BashOutput` / `TaskOutput` tags, omp's hashline,
  sloppy and patch shapes, Codex's `exec` custom tool with an object input, the exact wording of every runner across
  versions (the corpus notes the uncertain details per case). Codex code mode, Claude task notices and the background
  id were checked against real logs by counts and markers only (2026-10-28, METRICS 6.9 *Composing the stream*).

## Appendix A — tables, lists and patterns

The reference writes this appendix; do not edit it by hand.

<!-- appendix: generated by spec/tools/task_parsers.py --write; checked by default -->

### A.1 Tool kinds

- **claude**: read `Read`, `NotebookRead`, `LS`; search `Grep`, `Glob`, `LSP`; edit `Edit`, `MultiEdit`, `Write`, `NotebookEdit`; command `Bash`, `PowerShell`; delegate `Task`, `Agent`, `SendMessage`; web `WebFetch`, `WebSearch`.
- **codex**: read `read_file`, `list_dir`, `view_image`; search `grep_files`; edit `apply_patch`; command `shell`, `container.exec`, `local_shell`, `shell_command`, `exec_command`, `exec`; delegate `spawn_agent`, `send_input`, `resume_agent`; web `web_search`, `web_fetch`.
- **omp**: read `read`; search `grep`, `search`, `find`, `glob`, `ast_grep`, `lsp`; edit `edit`, `write`, `ast_edit`; command `bash`; delegate `task`; web `web_search`, `fetch`, `browser`.
- **pi**: read `read`, `ls`; search `grep`, `find`; edit `edit`, `write`; command `bash`, `powershell`.
- **openclaw**: read `read`, `ls`; edit `edit`, `write`, `apply_patch`; command `exec`, `bash`; delegate `sessions_spawn`, `sessions_send`; web `web_search`, `web_fetch`, `x_search`, `browser`.
- **droid**: read `Read`, `LS`; search `Grep`, `Glob`; edit `Create`, `Edit`, `MultiEdit`; command `Execute`; delegate `Task`; web `WebSearch`, `FetchUrl`.

### A.2 Lists

- `CASE_FOLDED`: `omp`, `pi`, `openclaw`
- `ARGV_TOOLS`: `shell`, `container.exec`, `local_shell`
- `SHELLS`: `bash`, `sh`, `zsh`, `dash`, `ksh`, `fish`
- `POWERSHELLS`: `pwsh`, `powershell`
- `LEADING_KEYWORDS`: `!`, `if`, `then`, `else`, `elif`, `do`, `while`, `until`, `{`
- `CLOSING_KEYWORDS`: `fi`, `done`, `esac`, `}`
- `CONSTRUCTS`: `for`, `case`, `select`, `function`
- `NEUTRAL`: `cd`, `pushd`, `popd`, `export`, `set`, `unset`, `source`, `.`, `true`, `:`, `echo`, `printf`, `sleep`, `clear`, `alias`, `shopt`, `ulimit`, `umask`, `wait`, `trap`, `exit`, `return`, `local`, `declare`, `typeset`, `read`, `false`
- `NICE_VALUE`: `-n`, `--adjustment`
- `STDBUF_VALUE`: `-i`, `-o`, `-e`
- `CAFFEINATE_VALUE`: `-t`, `-w`
- `XVFB_VALUE`: `-s`, `--server-args`, `-n`, `--server-num`, `-e`, `--error-file`, `-f`, `--auth-file`, `-p`, `--xauth-protocol`
- `TIMEOUT_VALUE`: `-s`, `--signal`, `-k`, `--kill-after`
- `UNITTEST_VALUE`: `-s`, `-t`, `-p`, `-k`, `--start-directory`, `--top-level-directory`, `--pattern`
- `SWIFT_TEST_VALUE`: `--filter`, `--skip`, `-s`, `--specifier`, `-c`, `--configuration`, `--package-path`, `--scratch-path`, `-j`, `--jobs`, `--num-workers`, `--xunit-output`, `--test-product`, `--sanitize`, `--triple`, `--sdk`
- `SWIFT_TEST_FILTERS`: `--filter`, `--skip`, `-s`, `--specifier`
- `SWIFT_BUILD_NARROW`: `--target`, `--product`
- `SWIFT_BUILD_VALUE`: `--target`, `--product`, `-c`, `--configuration`, `--package-path`, `--scratch-path`, `-j`, `--jobs`, `--triple`, `--sdk`, `--arch`
- `DOTNET_VALUE`: `--filter`, `-c`, `--configuration`, `-f`, `--framework`, `-r`, `--runtime`, `-o`, `--output`, `-v`, `--verbosity`, `-s`, `--settings`, `-l`, `--logger`, `--results-directory`, `-a`, `--test-adapter-path`, `--collect`, `-p`, `--property`
- `CMAKE_VALUE`: `--build`, `--target`, `-t`, `-j`, `--parallel`, `--config`
- `TSC_NOT_CHECK`: `--watch`, `-w`, `--version`, `-v`, `--init`, `--help`, `-h`, `--showConfig`
- `TSC_VALUE`: `-p`, `--project`, `--outDir`, `--target`, `--module`, `--lib`, `--jsx`, `--moduleResolution`, `--rootDir`, `--declarationDir`, `--tsBuildInfoFile`
- `BAZEL_VALUE`: `--config`, `--test_filter`, `--test_output`, `--jobs`, `-j`
- `MESON_VALUE`: `-C`, `--suite`, `--no-suite`, `--num-processes`, `--repeat`, `--timeout-multiplier`, `--setup`, `--wrapper`
- `DJANGO_VALUE`: `-k`, `--settings`, `--parallel`, `--tag`, `--exclude-tag`, `--pattern`, `-p`, `-v`, `--verbosity`
- `NODE_TEST_VALUE`: `--test-name-pattern`, `--test-reporter`, `--test-reporter-destination`, `--test-concurrency`, `--require`, `-r`, `--import`, `--loader`
- `BUN_TEST_VALUE`: `-t`, `--test-name-pattern`, `--timeout`, `--preload`, `--coverage-reporter`, `--reporter`, `--rerun-each`
- `PM_NARROW`: `-w`, `--workspace`, `--filter`, `-F`
- `PM_VALUE`: `-w`, `--workspace`, `--filter`, `-F`, `--prefix`, `-C`, `--dir`, `--cwd`
- `NX_VALUE`: `-t`, `--target`, `--targets`, `-p`, `--projects`
- `DENO_TEST_VALUE`: `--filter`, `--allow-read`, `--allow-net`, `--allow-env`, `--config`, `-c`, `--reporter`, `--coverage`, `--jobs`, `--shuffle`, `--seed`
- `ENV_VALUE`: `-u`, `--unset`, `-C`, `--chdir`, `-S`, `--split-string`
- `SUDO_VALUE`: `-u`, `-g`, `-h`, `-p`, `-C`, `-D`, `-r`, `-t`, `-U`, `--user`, `--group`
- `PY_VALUE`: `-W`, `-X`, `--check-hash-based-pycs`
- `UV_VALUE`: `--with`, `--with-requirements`, `--with-editable`, `--python`, `-p`, `--extra`, `--group`, `--only-group`, `--package`, `--directory`, `--project`, `--env-file`, `--index`, `--index-url`, `--default-index`, `--extra-index-url`, `-C`, `--config-setting`, `--no-group`, `--script`, `--gui-script`
- `CONDA_VALUE`: `-n`, `--name`, `-p`, `--prefix`, `--cwd`
- `NPX_VALUE`: `-p`, `--package`, `-c`, `--call`, `--cache`, `--userconfig`
- `XCRUN_VALUE`: `--sdk`, `-sdk`, `--toolchain`, `-toolchain`
- `COVERAGE_VALUE`: `--source`, `--rcfile`, `--data-file`, `--concurrency`, `--include`, `--omit`, `--context`
- `COSMETIC`: `-q`, `-qq`, `-v`, `-vv`, `-vvv`, `--quiet`, `--verbose`, `--color`, `--no-color`, `--colors`, `--no-colors`, `--ansi`, `--no-ansi`, `--nocapture`, `--no-capture`, `--show-output`, `--silent`
- `COSMETIC_PREFIX`: `--color=`, `--colour=`, `--colors=`, `--tb=`
- `PYTEST_VALUE`: `-k`, `-m`, `-p`, `-c`, `-o`, `-n`, `-W`, `-r`, `--maxfail`, `--tb`, `--rootdir`, `--basetemp`, `--junitxml`, `--junit-xml`, `--durations`, `--deselect`, `--ignore`, `--ignore-glob`, `--log-level`, `--log-cli-level`, `--timeout`, `--confcutdir`, `--import-mode`, `--dist`, `--cov`, `--cov-report`, `--cov-config`, `--cov-fail-under`, `--reruns`, `--reruns-delay`, `--count`, `-p`, `--override-ini`, `--capture`, `--html`, `--randomly-seed`
- `PYTEST_FILTERS`: `-k`, `-m`, `--lf`, `--last-failed`, `--sw`, `--stepwise`, `--deselect`, `--stepwise-skip`
- `CARGO_GLOBAL_VALUE`: `--color`, `--config`, `-Z`, `-C`, `--manifest-path`
- `CARGO_VALUE`: `-p`, `--package`, `--test`, `--bin`, `--example`, `--bench`, `-F`, `--features`, `--target`, `--target-dir`, `--manifest-path`, `-j`, `--jobs`, `--profile`, `--exclude`, `--color`, `--message-format`, `--config`, `-Z`, `--timings`
- `CARGO_NARROW`: `-p`, `--package`, `--test`, `--lib`, `--bin`, `--bins`, `--example`, `--examples`, `--doc`, `--bench`, `--benches`
- `LIBTEST_VALUE`: `--skip`, `--test-threads`, `--format`, `--color`, `-Z`, `--logfile`, `--report-time`
- `NEXTEST_VALUE`: `-E`, `--filterset`, `--filter-expr`, `-p`, `--package`, `--test`, `--bin`, `--profile`, `-P`, `-j`, `--test-threads`, `--retries`, `--partition`, `--archive-file`, `--cargo-profile`, `-F`, `--features`, `--target-dir`, `--manifest-path`
- `XCODE_ACTIONS`: `build`, `test`, `build-for-testing`, `test-without-building`, `analyze`, `archive`, `clean`, `install`, `installsrc`, `docbuild`
- `XCODE_FLAGS`: `-quiet`, `-verbose`, `-json`, `-allowProvisioningUpdates`, `-allowProvisioningDeviceRegistration`, `-skipUnavailableActions`, `-hideShellScriptEnvironment`, `-retry-tests-on-failure`, `-skipPackagePluginValidation`, `-skipMacroValidation`, `-disableAutomaticPackageResolution`, `-onlyUsePackageVersionsFromResolvedFile`, `-showTestPlans`, `-dry-run`, `-UseModernBuildSystem`, `-enableCodeCoverage`, `-parallel-testing-enabled`
- `XCODE_INFO`: `-list`, `-showBuildSettings`, `-version`, `-showsdks`, `-showdestinations`, `-usage`, `-help`, `-resolvePackageDependencies`, `-showTestPlans`, `-showComponent`
- `GO_TEST_VALUE`: `-run`, `-skip`, `-bench`, `-count`, `-timeout`, `-tags`, `-p`, `-parallel`, `-cpu`, `-coverprofile`, `-coverpkg`, `-covermode`, `-o`, `-exec`, `-benchtime`, `-blockprofile`, `-cpuprofile`, `-memprofile`, `-mutexprofile`, `-outputdir`, `-trace`, `-shuffle`, `-fuzz`, `-fuzztime`, `-vet`, `-ldflags`, `-gcflags`, `-mod`, `-modfile`, `-C`, `-asmflags`, `-buildmode`, `-compiler`, `-gccgoflags`, `-installsuffix`, `-pkgdir`, `-toolexec`, `-overlay`, `-pgo`
- `GO_BROAD`: `./...`, `...`, `all`, `.`
- `GRADLE_VALUE`: `-p`, `--project-dir`, `-b`, `--build-file`, `-c`, `--settings-file`, `-x`, `--exclude-task`, `--tests`, `--max-workers`, `-I`, `--init-script`, `-g`, `--gradle-user-home`, `--console`, `--warning-mode`, `--priority`, `-D`, `-P`, `--include-build`
- `MVN_TEST`: `test`, `integration-test`, `verify`, `install`, `deploy`, `package`
- `MVN_BUILD`: `compile`, `test-compile`, `process-classes`
- `MVN_SKIP`: `-DskipTests`, `-DskipTests=true`, `-Dmaven.test.skip=true`, `-Dmaven.test.skip`
- `MVN_VALUE`: `-pl`, `--projects`, `-f`, `--file`, `-s`, `--settings`, `-P`, `--activate-profiles`, `-T`, `--threads`, `-rf`, `--resume-from`, `-gs`, `--global-settings`, `-t`, `--toolchains`, `-l`, `--log-file`
- `CTEST_VALUE`: `--test-dir`, `-C`, `--build-config`, `-j`, `--parallel`, `--timeout`, `--output-log`, `-R`, `--tests-regex`, `-E`, `--exclude-regex`, `-L`, `--label-regex`, `-LE`, `--label-exclude`, `-I`, `--tests-information`, `--repeat`, `--stop-time`, `-O`, `--resource-spec-file`, `--repeat-until-fail`
- `CTEST_FILTERS`: `-R`, `--tests-regex`, `-E`, `--exclude-regex`, `-L`, `--label-regex`, `-LE`, `--label-exclude`, `-I`, `--tests-information`, `--rerun-failed`
- `JEST_VALUE`: `-c`, `--config`, `-w`, `--maxWorkers`, `--testTimeout`, `--reporters`, `--coverageReporters`, `--rootDir`, `--roots`, `--env`, `--testEnvironment`, `--outputFile`, `--seed`, `-t`, `--testNamePattern`, `--testPathPattern`, `--testPathPatterns`, `--testPathIgnorePatterns`, `--changedSince`, `--selectProjects`, `--shard`, `--coverageDirectory`, `--collectCoverageFrom`, `--json`
- `JEST_FILTERS`: `-t`, `--testNamePattern`, `--testPathPattern`, `--testPathPatterns`, `--findRelatedTests`, `-o`, `--onlyChanged`, `--changedSince`, `--lastCommit`, `--selectProjects`, `--shard`, `--onlyFailures`, `-f`
- `VITEST_VALUE`: `-c`, `--config`, `--root`, `-r`, `--dir`, `--reporter`, `--outputFile`, `--environment`, `--pool`, `--project`, `--shard`, `-t`, `--testNamePattern`, `--maxWorkers`, `--minWorkers`, `--testTimeout`, `--mode`, `--exclude`
- `VITEST_FILTERS`: `-t`, `--testNamePattern`, `--changed`, `--shard`, `--project`
- `MOCHA_VALUE`: `-r`, `--require`, `-R`, `--reporter`, `-t`, `--timeout`, `-s`, `--slow`, `-u`, `--ui`, `--config`, `--package`, `--extension`, `--ignore`, `--file`, `-g`, `--grep`, `-f`, `--fgrep`, `--spec`, `--retries`, `-j`, `--jobs`, `--reporter-option`, `-O`
- `MOCHA_FILTERS`: `-g`, `--grep`, `-f`, `--fgrep`
- `MYPY_VALUE`: `--config-file`, `-p`, `--package`, `-m`, `--module`, `--python-version`, `--cache-dir`, `--exclude`, `--follow-imports`, `--platform`, `--junit-xml`, `--python-executable`
- `PYRIGHT_VALUE`: `-p`, `--project`, `--pythonpath`, `--pythonversion`, `--level`, `--threads`, `--pythonplatform`, `-t`, `--typeshedpath`, `--venvpath`
- `RUFF_VALUE`: `--select`, `--ignore`, `--extend-select`, `--extend-ignore`, `--config`, `--output-format`, `--target-version`, `--exclude`, `--extend-exclude`, `--line-length`, `--per-file-ignores`, `--cache-dir`, `--stdin-filename`, `--fixable`, `--unfixable`
- `ESLINT_VALUE`: `-c`, `--config`, `--ext`, `-f`, `--format`, `-o`, `--output-file`, `--rule`, `--parser`, `--resolve-plugins-relative-to`, `--max-warnings`, `--ignore-path`, `--ignore-pattern`, `--cache-location`, `--env`, `--global`, `--plugin`, `--rulesdir`, `--parser-options`, `--report-unused-disable-directives-severity`, `--cache-strategy`
- `RSPEC_VALUE`: `-e`, `--example`, `-E`, `--example-matches`, `-t`, `--tag`, `-f`, `--format`, `-o`, `--out`, `-r`, `--require`, `-I`, `--seed`, `--order`, `-P`, `--pattern`, `--exclude-pattern`, `--default-path`
- `PHPUNIT_VALUE`: `--filter`, `--group`, `--exclude-group`, `--testsuite`, `-c`, `--configuration`, `--bootstrap`, `--log-junit`, `--order-by`, `--cache-directory`, `--coverage-html`, `--coverage-clover`, `--coverage-text`, `--test-suffix`, `--exclude-testsuite`
- `RUFF_SUBCOMMANDS`: `check`, `format`, `rule`, `config`, `linter`, `clean`, `server`, `analyze`, `version`
- `TEST_WORDS`: `test`, `tests`, `spec`, `specs`, `unit`, `e2e`, `integration`, `coverage`, `cov`, `jest`, `vitest`, `mocha`, `pytest`, `ctest`, `playwright`, `cypress`
- `BUILD_WORDS`: `build`, `compile`, `bundle`, `dist`, `all`
- `CHECK_WORDS`: `lint`, `typecheck`, `types`, `tsc`, `check`, `eslint`, `ruff`, `mypy`, `pyright`, `clippy`, `vet`, `analyze`, `analyse`, `verify`, `validate`
- `FORWARD_FILTERS`: `-t`, `--testNamePattern`, `-k`, `--grep`, `-g`, `--filter`, `-run`, `--run`, `--testPathPattern`, `--spec`, `--tests`, `-E`, `-R`, `--findRelatedTests`, `--only-testing`, `-only-testing`, `--skip`, `--exact`, `-p`, `--package`, `-m`
- `FORWARD_VALUE`: `--reporter`, `-r`, `--config`, `-c`, `--maxWorkers`, `-w`, `--timeout`, `--testTimeout`, `--env`, `--outputFile`, `--coverageReporters`, `--pool`, `--environment`, `--seed`, `--tb`, `-n`, `--workers`, `-j`, `--jobs`
- `NPM_BUILTINS`: `install`, `i`, `ci`, `add`, `uninstall`, `remove`, `rm`, `update`, `up`, `outdated`, `ls`, `list`, `link`, `publish`, `pack`, `init`, `create`, `audit`, `config`, `cache`, `version`, `view`, `info`, `why`, `explain`, `dedupe`, `prune`, `rebuild`, `start`, `restart`, `stop`, `store`, `fetch`, `patch`, `import`, `deploy`, `licenses`, `doctor`, `setup`, `env`, `approve-builds`, `self-update`, `workspaces`, `workspace`, `dlx`, `exec`, `x`, `set`, `plugin`, `constraints`, `unplug`, `stage`, `npm`, `node`, `upgrade`, `pm`, `repl`, `help`, `login`, `logout`, `whoami`, `search`, `owner`, `access`, `token`, `team`, `org`, `profile`, `star`, `stars`, `unstar`, `fund`, `sbom`, `query`, `run-script`, `run`, `rum`, `urn`, `test`, `t`, `tst`, `build`, `global`
- `COMPOSER_BUILTINS`: `install`, `update`, `require`, `remove`, `dump-autoload`, `dumpautoload`, `show`, `outdated`, `validate`, `create-project`, `init`, `config`, `global`, `exec`, `search`, `why`, `why-not`, `licenses`, `audit`, `status`, `archive`, `diagnose`, `self-update`, `depends`, `prohibits`, `fund`, `reinstall`, `bump`, `check-platform-reqs`, `clear-cache`, `browse`, `suggests`
- `CUSTOM_SUITES`: `pytest`, `unittest`, `django test`, `setup.py test`, `cargo test`, `cargo nextest`, `cargo build`, `cargo check`, `cargo clippy`, `cargo fmt`, `swift test`, `swift build`, `xcodebuild test`, `xcodebuild analyze`, `xcodebuild build`, `go test`, `go build`, `go vet`, `dotnet test`, `dotnet build`, `dotnet publish`, `dotnet pack`, `dotnet msbuild`, `dotnet format`, `cc`, `cmake --build`, `ctest`, `jest`, `vitest`, `mocha`, `node --test`, `tsc`, `vue-tsc`, `ruff`, `ruff format`, `swiftlint`, `bazel test`, `bazel build`, `meson test`, `meson compile`, `deno test`, `deno check`, `deno lint`, `deno fmt`, `bun test`, `bun build`
- `SUITES`: `astro check`, `ava`, `basedpyright`, `bazel build`, `bazel test`, `biome`, `black`, `bun build`, `bun run build`, `bun run check`, `bun run lint`, `bun run test`, `bun run typecheck`, `bun test`, `cabal build`, `cabal test`, `cargo build`, `cargo check`, `cargo clippy`, `cargo fmt`, `cargo nextest`, `cargo test`, `cc`, `clang-tidy`, `cmake --build`, `composer build`, `composer check`, `composer lint`, `composer test`, `composer typecheck`, `cppcheck`, `ctest`, `cypress run`, `dart analyze`, `dart test`, `deno check`, `deno fmt`, `deno lint`, `deno task build`, `deno task check`, `deno task lint`, `deno task test`, `deno task typecheck`, `deno test`, `django test`, `dotnet build`, `dotnet format`, `dotnet msbuild`, `dotnet pack`, `dotnet publish`, `dotnet test`, `esbuild`, `eslint`, `flake8`, `flutter analyze`, `flutter build`, `flutter test`, `go build`, `go test`, `go vet`, `golangci-lint`, `gradle assemble`, `gradle build`, `gradle check`, `gradle lint`, `gradle test`, `hadolint`, `isort`, `javac`, `jest`, `just build`, `just check`, `just lint`, `just test`, `just typecheck`, `kotlinc`, `make`, `make build`, `make check`, `make lint`, `make test`, `make typecheck`, `meson compile`, `meson test`, `mise run build`, `mise run check`, `mise run lint`, `mise run test`, `mise run typecheck`, `mix compile`, `mix credo`, `mix dialyzer`, `mix test`, `mocha`, `msbuild`, `mvn compile`, `mvn deploy`, `mvn install`, `mvn integration-test`, `mvn package`, `mvn process-classes`, `mvn test`, `mvn test-compile`, `mvn verify`, `mypy`, `next build`, `next lint`, `ng build`, `ng lint`, `ng test`, `ninja`, `ninja build`, `ninja check`, `ninja lint`, `ninja test`, `ninja typecheck`, `node --test`, `nox`, `npm run build`, `npm run check`, `npm run lint`, `npm run typecheck`, `npm test`, `nuxt build`, `nx build`, `nx check`, `nx lint`, `nx test`, `nx typecheck`, `oxlint`, `pest`, `phpstan`, `phpunit`, `playwright test`, `pnpm run build`, `pnpm run check`, `pnpm run lint`, `pnpm run typecheck`, `pnpm test`, `pre-commit`, `prettier`, `psalm`, `pylint`, `pyright`, `pytest`, `rails test`, `rake build`, `rake check`, `rake lint`, `rake test`, `rake typecheck`, `rollup`, `rspec`, `rubocop`, `ruff`, `ruff format`, `rustc`, `setup.py test`, `shellcheck`, `stack build`, `stack test`, `staticcheck`, `stylelint`, `svelte-check`, `swift build`, `swift test`, `swift-format`, `swiftc`, `swiftlint`, `tap`, `task build`, `task check`, `task lint`, `task test`, `task typecheck`, `terraform validate`, `tox`, `tsc`, `tsup`, `turbo build`, `turbo check`, `turbo lint`, `turbo test`, `turbo typecheck`, `unittest`, `vite build`, `vitest`, `vstest`, `vue-tsc`, `webpack`, `xcodebuild analyze`, `xcodebuild build`, `xcodebuild test`, `yarn run build`, `yarn run check`, `yarn run lint`, `yarn run typecheck`, `yarn test`, `zig build`, `zig test`
- `READ_PROGS`: `cat`, `head`, `tail`, `less`, `more`, `nl`, `wc`, `ls`, `tree`, `stat`, `file`, `pwd`, `bat`, `batcat`, `sed`, `awk`, `jq`, `yq`, `realpath`, `readlink`, `du`, `basename`, `dirname`, `od`, `xxd`, `hexdump`, `strings`, `diff`, `cmp`, `sort`, `uniq`, `cut`, `tr`, `column`, `which`, `type`, `whereis`, `printenv`, `date`, `env`, `exa`, `eza`, `lsd`
- `SEARCH_PROGS`: `rg`, `grep`, `egrep`, `fgrep`, `ag`, `ack`, `find`, `fd`, `fdfind`, `locate`, `mdfind`
- `GIT_READ`: `status`, `diff`, `log`, `show`, `blame`, `rev-parse`, `ls-tree`, `cat-file`, `describe`, `shortlog`, `reflog`, `branch`, `remote`, `tag`, `config`
- `GIT_SEARCH`: `grep`, `ls-files`
- `GIT_VALUE`: `-C`, `-c`, `--git-dir`, `--work-tree`, `--namespace`, `--exec-path`, `--config-env`
- `FIND_ACTIONS`: `-delete`, `-exec`, `-execdir`, `-ok`, `-okdir`, `-fprint`, `-fprintf`, `-fls`
- `WRITE_REDIRS`: `>`, `>>`, `>|`, `&>`, `&>>`, `<>`
- `GREP_PROGRAMS`: `grep`, `egrep`
- `GREP_LONG_FLAGS`: `--ignore-case`, `--extended-regexp`, `--line-number`, `--word-regexp`, `--fixed-strings`, `--perl-regexp`, `--text`, `--no-messages`, `--no-filename`, `--with-filename`, `--only-matching`
- `GREP_VALUE_OPTIONS`: `-e`, `-A`, `-B`, `-C`, `-m`, `--regexp`, `--after-context`, `--before-context`, `--context`, `--max-count`
- `LIMITERS`: `head`, `tail`
- `CLAUDE_ABORT`: `[Request interrupted by user`, `The user doesn't want to proceed with this tool use`, `The user doesn't want to take this action`, `Permission to use `, `Claude requested permissions to use`, `User rejected`, `The tool use was rejected`
- `JS_REGEX_AFTER`: `return`, `typeof`, `instanceof`, `in`, `of`, `new`, `delete`, `void`, `throw`, `case`, `do`, `else`, `yield`, `await`
- `COMPLETION_FRAMEWORKS`: `cargo`, `swiftpm`, `xcodebuild`, `gradle`, `maven`, `prettier`, `vite`

### A.3 Table-driven programs

- `swiftc`: class build, suite `swiftc`, never targeted.
- `msbuild`: class build, suite `msbuild`, never targeted.
- `vstest.console`: class test, suite `vstest`, never targeted.
- `javac`: class build, suite `javac`, never targeted.
- `kotlinc`: class build, suite `kotlinc`, never targeted.
- `ava`: class test, suite `ava`; targeted by the path rule or by `--match`, `-m`; value options `--match`, `-m`, `--timeout`, `-T`.
- `tap`: class test, suite `tap`; targeted by the path rule or by `--grep`, `-g`; value options `--grep`, `-g`, `--reporter`, `-R`.
- `playwright`: first word: `test` → test `playwright test`, targeted by `-g`, `--grep`, `--last-failed`, `--only-changed` or the path rule; any other first word → other; value options `-g`, `--grep`, `--project`, `-c`, `--config`, `--reporter`, `--workers`, `-j`, `--shard`, `--retries`, `--timeout`, `--grep-invert`.
- `cypress`: first word: `run` → test `cypress run`, targeted by `--spec`, `-s`; any other first word → other; value options `--spec`, `-s`, `--browser`, `-b`, `--config`, `-c`, `--env`, `-e`, `--project`, `-P`.
- `eslint`: class check, suite `eslint`; targeted by the path rule; value options `-c`, `--config`, `--ext`, `-f`, `--format`, `-o`, `--output-file`, `--rule`, `--parser`, `--resolve-plugins-relative-to`, `--max-warnings`, `--ignore-path`, `--ignore-pattern`, `--cache-location`, `--env`, `--global`, `--plugin`, `--rulesdir`, `--parser-options`, `--report-unused-disable-directives-severity`, `--cache-strategy`.
- `oxlint`: class check, suite `oxlint`; targeted by the path rule; value options `-c`, `--config`, `-A`, `-D`, `-W`, `--format`, `-f`.
- `biome`: first word: `check` → check `biome`, targeted by the path rule; `lint` → check `biome`, targeted by the path rule; `ci` → check `biome`, targeted by the path rule; any other first word → other; value options `--config-path`, `--reporter`, `--max-diagnostics`.
- `prettier`: class check, suite `prettier` only with one of `--check`, `-c`, `--list-different`, `-l` (else other); targeted by the path rule; value options `--config`, `--ignore-path`, `--plugin`, `--parser`.
- `stylelint`: class check, suite `stylelint`; targeted by the path rule; value options `--config`, `-c`, `--formatter`, `-f`, `--ignore-path`.
- `svelte-check`: class check, suite `svelte-check`, never targeted.
- `astro`: first word: `check` → check `astro check`, never targeted; any other first word → other; value options none.
- `mypy`: class check, suite `mypy`; targeted by the path rule; value options `--config-file`, `-p`, `--package`, `-m`, `--module`, `--python-version`, `--cache-dir`, `--exclude`, `--follow-imports`, `--platform`, `--junit-xml`, `--python-executable`.
- `dmypy`: class check, suite `mypy`, never targeted.
- `pyright`: class check, suite `pyright`; targeted by the path rule; value options `-p`, `--project`, `--pythonpath`, `--pythonversion`, `--level`, `--threads`, `--pythonplatform`, `-t`, `--typeshedpath`, `--venvpath`.
- `basedpyright`: class check, suite `basedpyright`; targeted by the path rule; value options `-p`, `--project`, `--pythonpath`, `--pythonversion`, `--level`, `--threads`, `--pythonplatform`, `-t`, `--typeshedpath`, `--venvpath`.
- `flake8`: class check, suite `flake8`; targeted by the path rule; value options `--config`, `--select`, `--ignore`, `--extend-ignore`, `--max-line-length`, `--exclude`, `--format`.
- `pylint`: class check, suite `pylint`; targeted by the path rule; value options `--rcfile`, `--disable`, `--enable`, `-j`, `--jobs`, `--output-format`, `--load-plugins`.
- `black`: class check, suite `black` only with one of `--check`, `--diff` (else other); targeted by the path rule; value options `--config`, `-l`, `--line-length`, `-t`, `--target-version`, `--include`, `--exclude`.
- `isort`: class check, suite `isort` only with one of `--check`, `--check-only`, `-c`, `--diff` (else other); targeted by the path rule; value options `--settings-path`, `--sp`, `--profile`.
- `tox`: class test, suite `tox`; targeted when words follow `--`.
- `nox`: class test, suite `nox`; targeted when words follow `--`.
- `rspec`: class test, suite `rspec`; targeted by the path rule or by `-e`, `--example`, `-E`, `--example-matches`, `-t`, `--tag`, `--only-failures`, `--next-failure`, `-P`, `--pattern`; value options `-e`, `--example`, `-E`, `--example-matches`, `-t`, `--tag`, `-f`, `--format`, `-o`, `--out`, `-r`, `--require`, `-I`, `--seed`, `--order`, `-P`, `--pattern`, `--exclude-pattern`, `--default-path`.
- `rails`: first word: `test` → test `rails test`, targeted by `-n`, `--name` or the path rule; any other first word → other; value options `-n`, `--name`, `-e`, `--environment`, `--seed`.
- `rubocop`: class check, suite `rubocop`; targeted by the path rule; value options `-c`, `--config`, `-f`, `--format`, `--only`, `--except`, `--require`, `-r`, `-o`, `--out`.
- `phpunit`: class test, suite `phpunit`; targeted by the path rule or by `--filter`, `--group`, `--testsuite`; value options `--filter`, `--group`, `--exclude-group`, `--testsuite`, `-c`, `--configuration`, `--bootstrap`, `--log-junit`, `--order-by`, `--cache-directory`, `--coverage-html`, `--coverage-clover`, `--coverage-text`, `--test-suffix`, `--exclude-testsuite`.
- `pest`: class test, suite `pest`; targeted by the path rule or by `--filter`, `--group`, `--testsuite`; value options `--filter`, `--group`, `--exclude-group`, `--testsuite`, `-c`, `--configuration`, `--bootstrap`, `--log-junit`, `--order-by`, `--cache-directory`, `--coverage-html`, `--coverage-clover`, `--coverage-text`, `--test-suffix`, `--exclude-testsuite`.
- `phpstan`: first word: `analyse` → check `phpstan`, targeted by the path rule; `analyze` → check `phpstan`, targeted by the path rule; any other first word → other; value options `-c`, `--configuration`, `-l`, `--level`, `--memory-limit`, `--error-format`.
- `psalm`: class check, suite `psalm`; targeted by the path rule; value options `-c`, `--config`, `--threads`, `--output-format`.
- `mix`: first word: `test` → test `mix test`, targeted by `--only`, `--failed`, `--stale`, `--include` or the path rule; `compile` → build `mix compile`, never targeted; `credo` → check `mix credo`, never targeted; `dialyzer` → check `mix dialyzer`, never targeted; any other first word → other; value options `--only`, `--include`, `--exclude`, `--seed`, `--max-failures`, `--partitions`.
- `zig`: first word: `test` → test `zig test`, never targeted; `build` → build `zig build`, never targeted; any other first word → other; value options none.
- `stack`: first word: `test` → test `stack test`, targeted by `--ta`, `--test-arguments`; `build` → build `stack build`, never targeted; any other first word → other; value options `--ta`, `--test-arguments`.
- `cabal`: first word: `test` → test `cabal test`, targeted by `--test-options`, `--test-option`; `build` → build `cabal build`, never targeted; any other first word → other; value options `--test-options`, `--test-option`.
- `dart`: first word: `test` → test `dart test`, targeted by `-n`, `--name`, `-N`, `--plain-name`, `-t`, `--tags` or the path rule; `analyze` → check `dart analyze`, never targeted; any other first word → other; value options `-n`, `--name`, `-N`, `--plain-name`, `-t`, `--tags`, `-p`, `--platform`, `-j`, `--concurrency`, `-r`, `--reporter`.
- `flutter`: first word: `test` → test `flutter test`, targeted by `-n`, `--name`, `--plain-name`, `-t`, `--tags` or the path rule; `analyze` → check `flutter analyze`, never targeted; `build` → build `flutter build`, never targeted; any other first word → other; value options `-n`, `--name`, `--plain-name`, `-t`, `--tags`, `-r`, `--reporter`, `--flavor`.
- `golangci-lint`: first word: `run` → check `golangci-lint`, targeted by the path rule; any other first word → other; value options `-c`, `--config`, `--timeout`, `-E`, `--enable`, `-D`, `--disable`, `--out-format`.
- `staticcheck`: class check, suite `staticcheck`; targeted by the path rule; value options `-checks`, `-f`, `-tags`.
- `swift-format`: first word: `lint` → check `swift-format`, never targeted; any other first word → other; value options none.
- `clang-tidy`: class check, suite `clang-tidy`, never targeted.
- `cppcheck`: class check, suite `cppcheck`, never targeted.
- `shellcheck`: class check, suite `shellcheck`, never targeted.
- `hadolint`: class check, suite `hadolint`, never targeted.
- `terraform`: first word: `validate` → check `terraform validate`, never targeted; any other first word → other; value options none.
- `pre-commit`: first word: `run` → check `pre-commit`, targeted by `--files`; any other first word → other; value options `--files`, `--hook-stage`, `--from-ref`, `--to-ref`.
- `vite`: first word: `build` → build `vite build`, never targeted; any other first word → other; value options none.
- `next`: first word: `build` → build `next build`, never targeted; `lint` → check `next lint`, never targeted; any other first word → other; value options none.
- `nuxt`: first word: `build` → build `nuxt build`, never targeted; any other first word → other; value options none.
- `ng`: first word: `build` → build `ng build`, never targeted; `test` → test `ng test`, targeted by `--include`; `lint` → check `ng lint`, never targeted; any other first word → other; value options `--include`, `--configuration`, `-c`, `--project`.
- `webpack`: class build, suite `webpack`, never targeted.
- `rollup`: class build, suite `rollup`, never targeted.
- `esbuild`: class build, suite `esbuild`, never targeted.
- `tsup`: class build, suite `tsup`, never targeted.
- `rustc`: class build, suite `rustc`, never targeted.
- `make`: task runner `make` (3.6); value options `-C`, `--directory`, `-f`, `--file`, `--makefile`, `-I`, `--include-dir`, `-o`, `--old-file`, `-W`, `--what-if`, `-j`, `-l`; no target → build `make`.
- `ninja`: task runner `ninja` (3.6); value options `-C`, `-f`, `-j`, `-k`, `-l`, `-d`, `-t`, `-w`; no target → build `ninja`.
- `just`: task runner `just` (3.6); value options `-f`, `--justfile`, `-d`, `--working-directory`, `--set`, `--dotenv-path`, `--dotenv-filename`, `--shell`, `--shell-arg`, `--chooser`, `--color`, `--command`, `--list-heading`, `--list-prefix`; no target → other.
- `task`: task runner `task` (3.6); value options `-d`, `--dir`, `-t`, `--taskfile`, `-o`, `--output`, `-c`, `--color`; no target → other.
- `rake`: task runner `rake` (3.6); value options `-f`, `--rakefile`, `-I`, `--libdir`, `-r`, `--require`, `-C`, `--directory`; no target → other.
- `mise`: task runner `mise` (3.6); value options `-C`, `--cd`, `-E`, `--env`, `-j`, `--jobs`; no target → other.
- `turbo`: task runner `turbo` (3.6); value options `--filter`, `-F`, `--concurrency`, `--output-logs`, `--cache-dir`; no target → other.
- `nx`: task runner `nx` (3.6); value options `-c`, `--configuration`, `--parallel`, `--output-style`; no target → other.
- `composer`: task runner `composer` (3.6); value options `-d`, `--working-dir`; no target → other.

### A.4 Patterns

Full-line patterns unless the text says *search*.

```text
LONE_SURROGATE     [\ud800-\udfff]
DRIVE              [A-Za-z]:/
SAFE_ARG           [A-Za-z0-9_@%+=:,./-]+
REDIR              ([0-9]*)(<<<|<<-|<<|>>|>&|<&|>\||<>|&>>|&>|>|<)
NAME_ASSIGN        [A-Za-z_][A-Za-z0-9_]*=
SHELL_C_FLAG       -[A-Za-z]*c[A-Za-z]*
MAKE_FILTER_VAR    TESTS?|T|TEST_?ARGS|TEST_?FILTER|FILTER|RUN|ONLY|K|PKG|PACKAGE|FILES?|SPEC|ARGS|PYTEST_?ARGS|TESTNAME|CASE|TESTOPTS
PATCH_LINE         [ \t]*\*\*\* Begin Patch[ \t]*
FD_TARGET          [0-9-]+
PIPEFAIL_FLAG      -[A-Za-z]*o
STATUS_REF         \$\?|\$\{\?\}|\$\{PIPESTATUS\[([0-9]+)\]\}|\$PIPESTATUS
STATUS_VALUE       -?[0-9]+
CLAUDE_EXIT        Exit code (-?[0-9]+)
CLAUDE_TIMEOUT     Command timed out after .*
CLAUDE_BACKGROUND  Command running in background with ID: ?([A-Za-z0-9_:-]+(?:\.[A-Za-z0-9_:-]+)*).*
CODEX_EXIT         Exit code: (-?[0-9]+)
CODEX_EXITED       Process exited with code (-?[0-9]+)
CODEX_RUNNING      Process running with session ID ([0-9]+)
CODEX_TIMEOUT      command timed out after [0-9]+ milliseconds
CODEX_HEADER       (Chunk ID|Wall time|Exit code|Process exited with code|Process running with session ID|Original token count|Total output lines)[: ].*
PI_EXIT            \(?Command exited with code (-?[0-9]+)\)?
PI_TIMEOUT         \[?Command timed out.*
PI_ABORT           (Command aborted.*|Command was terminated.*|Operation aborted.*|Command cancelled.*)
PI_RUNNING         Command still running \(session .*
BASHOUTPUT_STATUS  <status>([a-z_]+)</status>
BASHOUTPUT_EXIT    <exit_code>(-?[0-9]+)</exit_code>
NOTIFY_TASK        <task-id>([^<]+)</task-id>
NOTIFY_SUMMARY     <summary>([^\n]*?)</summary>
NOTIFY_EXIT        Background command [^\n]*(?:\(exit code (-?[0-9]+)\)|with exit code (-?[0-9]+))
JS_NUMBER_TOKEN    [0-9](?:[0-9A-Za-z_.]|(?<=[eE])[+-])*
JS_NUMBER          [0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?
JS_HEX2            [0-9A-Fa-f]{2}
JS_HEX4            [0-9A-Fa-f]{4}
JS_CODEPOINT       \{([0-9A-Fa-f]{1,6})\}
ANSI_OSC           \x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?
ANSI_CSI           \x1b\[[0-?]*[ -/]*[@-~]
ANSI_FE            \x1b[@-Z\\-_]
CONTROL            [\x00-\x08\x0b\x0c\x0e-\x1f\x7f]
WORKSPACE_PREFIX   ((?:@[A-Za-z0-9._-]+/|[A-Za-z0-9._-]*/)[A-Za-z0-9._/-]* [A-Za-z][A-Za-z0-9:._-]*|@?[A-Za-z0-9._/-]+:[A-Za-z][A-Za-z0-9._-]*): ?
TMP_PATH           (?<![A-Za-z0-9._~/\\-])(?:(?:/private)?/var/folders/|(?:/private)?/tmp/|[A-Za-z]:[\\/]Users[\\/][^\\/ \t]+[\\/]AppData[\\/]Local[\\/]Temp[\\/])[^ \t'\"()\[\]\{\}<>,;|`]*
HOME_PATH          (?<![A-Za-z0-9._~/\\-])(?:/Users/[^/ \t'\"]+/|/home/[^/ \t'\"]+/|[A-Za-z]:[\\/]Users[\\/][^\\/ \t'\"]+[\\/])
UUID               [0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}
ISO_TIME           [0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:[.,][0-9]+)?)?(?:Z|[+-][0-9]{2}:?[0-9]{2})?
TIME_OF_DAY        (?<![0-9])[0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:[.,][0-9]+)?(?![0-9])
DATE               (?<![0-9])[0-9]{4}-[0-9]{2}-[0-9]{2}(?![0-9])
HEX_ADDR           0x[0-9A-Fa-f]+
FILE_POS           (\.[A-Za-z][A-Za-z0-9+]{0,9}):[0-9]+(?::[0-9]+)?
FILE_POS_PAREN     (\.[A-Za-z][A-Za-z0-9+]{0,9})\([0-9]+(?:,[0-9]+)?\)
FILE_POS_BRACKET   (\.[A-Za-z][A-Za-z0-9+]{0,9}):\[[0-9]+,[0-9]+\]
LINE_WORD          (?<![A-Za-z0-9_])(line|Line|LINE|lines|col|column|Column|col\.)( ?)[0-9]+(?![0-9])
DURATION           (?<![A-Za-z0-9_.])[0-9]+(?:\.[0-9]+)? ?(?:ns|us|\u00b5s|ms|s|sec|secs|seconds|second|m|min|mins|minutes|minute|h|hr|hrs|hours|hour)(?![A-Za-z0-9_])
LONG_HEX           (?<![A-Za-z0-9_])[0-9A-Fa-f]{8,}(?![A-Za-z0-9_])
PID_PORT           (?<![A-Za-z0-9_])(pid|PID|port)([ =:#]+)[0-9]+
SPACES             [ \t]+
PYTEST_FAIL        (?:\[gw[0-9]+\] \[ *[0-9]+%\] )?(FAILED|ERROR) (?!\()([^ ]+?)(?: - (.*))?
PYTEST_VERBOSE     ([^ ]+::.+?) (FAILED|ERROR)(?: +\[ *[0-9]+%\])?
PYTEST_SUMMARY     (?:=+ )?((?:[0-9]+ (?:failed|passed|errors?|skipped|xfailed|xpassed|deselected|warnings?|rerun|reruns|subtests passed|subtests failed)(?:, )?)+) in [0-9]+(?:\.[0-9]+)?s(?: \([0-9:.]+\))?(?: =+)?
PYTEST_NONE        (?:=+ )?no tests ran in [0-9]+(?:\.[0-9]+)?s(?: =+)?
UNITTEST_FAIL      (FAIL|ERROR|UNEXPECTED SUCCESS): ([A-Za-z_][A-Za-z0-9_]*) \(([A-Za-z0-9_.]+)\)(?: .*)?
UNITTEST_RAN       Ran ([0-9]+) tests? in [0-9]+(?:\.[0-9]+)?s
UNITTEST_OK        OK(?: \((.*)\))?
UNITTEST_FAILED    FAILED \((.*)\)
LIBTEST_LINE       test (.+) \.\.\. (FAILED|ok|ignored(?:, .*)?|bench: .*)
LIBTEST_STDOUT     ---- (.+) (?:stdout|stderr) ----
LIBTEST_RESULT     test result: (?:ok|FAILED)\. ([0-9]+) passed; ([0-9]+) failed; ([0-9]+) ignored; ([0-9]+) measured; ([0-9]+) filtered out(?:; finished in .*)?
LIBTEST_FAILURES   failures:
LIBTEST_LISTED         ([^ ].*)
NEXTEST_FAIL        *(?:FAIL|SIGSEGV|SIGABRT|SIGKILL|SIGBUS|TIMEOUT|LEAK-FAIL|ABORT) \[ *[0-9.]+m?s\] (?:\( *[0-9]+/[0-9]+\) )?([^ ]+) (.+)
NEXTEST_OTHER       *(?:PASS|SKIP|LEAK|SLOW|FLAKY|START|RETRY|TRY [0-9]+ [A-Z-]+) \[.*
NEXTEST_SUMMARY     *Summary \[[^\]]*\] +([0-9]+) tests? run: (.*)
DOCTEST_LINE       \(line [0-9]+\)
RUST_HEADER        error(?:\[(E[0-9]{4})\])?: (.+)
RUST_WARN          warning(?:\[[A-Za-z0-9_:]+\])?: .+
RUST_LOC            *--> (.+?):([0-9]+):([0-9]+)
RUST_COULD_NOT     error: could not compile `[^`]+`(?: \([^)]*\))?(?:;.*)?(?: due to ([0-9]+) previous errors?| due to previous error)?(?:;.*)?
RUST_ABORTING      error: aborting due to (?:([0-9]+) )?previous errors?(?:;.*)?
RUST_STATUS        error: (?:test failed|build failed|failed to run|[0-9]+ targets? failed|process didn't exit successfully|Recipe ).*
ST_ISSUE           [^ \tA-Za-z0-9]+ Test (.+?) recorded an issue.*
ST_FAILED          [^ \tA-Za-z0-9]+ Test (.+?) failed after [0-9]+(?:\.[0-9]+)? seconds(?: with [0-9]+ issues?)?(?: \(including [0-9]+ known issues?\))?\.
ST_OTHER           [^ \tA-Za-z0-9]+ (?:Test .+? (?:passed after .*|started\.|skipped.*)|Suite .+? (?:passed|failed|started).*|Test run started\.|Testing Library Version: .*|Target Platform: .*)
ST_RUN             [^ \tA-Za-z0-9]+ Test run with ([0-9]+) tests?(?: in [0-9]+ suites?)? (passed|failed) after [0-9]+(?:\.[0-9]+)? seconds(?: with ([0-9]+) issues?)?(?: \(including [0-9]+ known issues?\))?\.
ST_CASES            with [0-9]+ test cases?$
XC_CASE            Test [Cc]ase '(-\[[^\]]+\]|[^']+)' (failed|passed|started|skipped)(?: on '[^']*')?(?: \([0-9.]+ seconds\))?\.?
XC_ERROR           (.+?):([0-9]+): error: (-\[[^\]]+\]|[A-Za-z0-9_.()/]+) : .*
XC_SUITE           Test [Ss]uite '.+' (?:started|passed|failed).*
XC_EXECUTED        [ \t]*Executed ([0-9]+) tests?, with (?:([0-9]+) tests? skipped and )?([0-9]+) failures? \(([0-9]+) unexpected\) in [0-9.]+ \([0-9.]+\) seconds
XB_FAILING         Failing tests:
XB_LISTED          [ \t]+(.+?)(?: \(.*\))?
XB_RESULT          \*\* (?:TEST|BUILD|ANALYZE|ARCHIVE|CLEAN|TEST BUILD|INSTALL) (?:FAILED|SUCCEEDED|INTERRUPTED) \*\*
XB_COMMANDS        The following build commands failed:
XB_COUNT           \(([0-9]+) failures?\)
SWIFTC_ERROR       (.+\.swift):([0-9]+):([0-9]+): error: (.+)
SWIFTC_WARN        (.+\.swift):([0-9]+):([0-9]+): (?:warning|note): .+
SWIFT_NOISE        error: (?:emit-module command failed|fatalError|compile command failed|link command failed).*
GO_FAIL            [ \t]*--- FAIL: ([^ ]+) \([0-9.]+s\)
GO_PASS            [ \t]*--- (PASS|SKIP): ([^ ]+) \([0-9.]+s\)
GO_RUN             === (?:RUN|PAUSE|CONT|NAME) .*
GO_PKG             (FAIL|ok)[ \t]+([^ \t]+)[ \t]+(?:[0-9.]+s|\(cached\)|\[([^\]]+)\])(?:[ \t].*)?
GO_BARE            (?:FAIL|PASS|\?[ \t]+[^ \t]+[ \t]+\[no test files\])
GO_ERROR           ([^ \t][^ \t:]*\.go):([0-9]+)(?::([0-9]+))?: (.+)
GO_HEADER          # [^ ]+(?: \[[^\]]+\])?
JEST_FILE           ?(FAIL|PASS) +([^ ]+)(?: \([0-9.]+ ?m?s\))?
JEST_BULLET          ● (.+)
JEST_TESTS         Tests:[ \t]+(.*[0-9]+ total)
JEST_SUITES        Test Suites:[ \t]+(.*[0-9]+ total)
JEST_NOISE         (?:Snapshots:|Time:|Ran all test suites).*|[ \t]+[✓✕○✎] .*|Summary of all failing tests
VITEST_FAIL         *FAIL +(.+?) > (.+?)(?: \[[^\]]*\])?
VITEST_LOAD         *FAIL +([^ ]+) \[ (.+) \]
VITEST_FILE         *[❯>✓] ([^ ]+) \([0-9]+ tests?(?: \|.*)?\)(?: [0-9.]+m?s)?
VITEST_X            +[×✗] (.+?)(?: [0-9.]+m?s)?
VITEST_TESTS        *Tests +(.+?) \(([0-9]+)\)
VITEST_FILES        *Test Files +(.+?) \(([0-9]+)\)
MOCHA_PASSING      [ \t]*([0-9]+) passing \([0-9a-z.]+\)
MOCHA_FAILING      [ \t]*([0-9]+) failing
MOCHA_PENDING      [ \t]*([0-9]+) pending
MOCHA_ITEM         ([ \t]*)([0-9]+)\) (.*)
BUN_FAIL           \(fail\) (.+?)(?: \[[0-9.]+m?s\])?
BUN_COUNT           *([0-9]+) (pass|fail)
DENO_SUMMARY       (?:ok|FAILED) \| ([0-9]+) passed(?: \([0-9]+ steps?\))? \| ([0-9]+) failed(?: \([0-9]+ steps?\))?.*
DENO_FAILURE       (.+?) => (.+?):[0-9]+:[0-9]+
TSC_PAREN          (.+?)\(([0-9]+),([0-9]+)\): error (TS[0-9]+): (.*)
TSC_PRETTY         (.+?):([0-9]+):([0-9]+) - error (TS[0-9]+): (.*)
TSC_FOUND          Found ([0-9]+) errors? in .*
FOUND_ERRORS       Found ([0-9]+) errors?(?: \(([0-9]+) fixed, ([0-9]+) remaining\))?\.
TSC_TABLE          Errors +Files
TSC_ROW            [ \t]+[0-9]+[ \t]+.+:[0-9]+
ESLINT_FILE        (/[^:]*|[A-Za-z]:[\\/][^:]*)
ESLINT_MSG         [ \t]+([0-9]+):([0-9]+)[ \t]+(error|warning)[ \t]+(.+?)(?:[ \t]{2,}([^ \t]+))?
ESLINT_UNIX        (.+?):([0-9]+):([0-9]+): (.+) \[(Error|Warning)/(.+)\]
ESLINT_SUMMARY     ✖ ([0-9]+) problems? \(([0-9]+) errors?, ([0-9]+) warnings?\)
ESLINT_FIXABLE     [ \t]+[0-9]+ errors? and [0-9]+ warnings? potentially fixable.*
RUFF_CONCISE       (.+?\.(?:py|pyi|ipynb)):([0-9]+):([0-9]+): ([A-Z]+[0-9]+|SyntaxError|invalid-syntax):? (?:\[\*\] )?(.*)
RUFF_HEADER        ([A-Z]{1,4}[0-9]{1,4}|invalid-syntax|SyntaxError):? (?:\[\*\] )?(.+)
RUFF_LOC            *--> (.+?):([0-9]+):([0-9]+)
RUFF_PASSED        All checks passed!
RUFF_WOULD         Would reformat: (.+)
BLACK_WOULD        would reformat (.+)
FORMAT_SUMMARY     (?:Oh no! .*)?([0-9]+) files? would be reformatted(?:, [0-9]+ files? (?:already formatted|would be left unchanged))?\.?
MYPY_ERROR         (.+?\.pyi?):([0-9]+)(?::([0-9]+))?: error: (.+?)(?:  \[([a-z0-9-]+)\])?
MYPY_NOTE          (.+?\.pyi?):([0-9]+)(?::([0-9]+))?: note: .*
MYPY_FOUND         Found ([0-9]+) errors? in [0-9]+ files? \((?:checked [0-9]+ source files?|errors prevented further checking)\)
MYPY_SUCCESS       Success: no issues found in [0-9]+ source files?
PYRIGHT_ERROR      [ \t]*(.+?):([0-9]+):([0-9]+) - error: (.+?)(?: \((report[A-Za-z]+)\))?
PYRIGHT_RULE        \((report[A-Za-z]+)\)$
PYRIGHT_OTHER      [ \t]*(.+?):([0-9]+):([0-9]+) - (?:warning|information|info|note): .*
PYRIGHT_SUMMARY    ([0-9]+) errors?, ([0-9]+) warnings?, ([0-9]+) (?:informations?|infos?|notes?) ?
MSB_ERROR          [ \t]*(.+?)\(([0-9]+)(?:,([0-9]+))?(?:,[0-9]+,[0-9]+)?\): error ([A-Za-z]+[0-9]+): (.*?)(?: \[([^\]]+)\])?
MSB_TOOL_ERROR     [ \t]*([A-Za-z][A-Za-z0-9_.-]*) : error ([A-Za-z]+[0-9]+): (.*?)(?: \[([^\]]+)\])?
MSB_WARNING        [ \t]*.+?(?:\([0-9,]+\))? : warning [A-Za-z]+[0-9]+: .*|[ \t]*.+?\([0-9,]+\): warning [A-Za-z]+[0-9]+: .*
MSB_COUNT          [ \t]*([0-9]+) Error\(s\)
MSB_NOISE          [ \t]*[0-9]+ Warning\(s\)|Build FAILED\.|Build succeeded\.|Time Elapsed .*
MSB_TERMINAL       Build (?:failed|succeeded)(?: with ([0-9]+) error\(s\)(?: and [0-9]+ warning\(s\))?)?(?: and [0-9]+ warning\(s\))? in [0-9.]+s
VSTEST_FAILED      [ \t]*Failed ([^ ]+) \[[^\]]*\]
VSTEST_PASSED      [ \t]*(?:Passed|Skipped) ([^ ]+) \[[^\]]*\]
VSTEST_SUMMARY     (?:Failed|Passed)![ \t]*-[ \t]*Failed:[ \t]*([0-9]+),[ \t]*Passed:[ \t]*([0-9]+),[ \t]*Skipped:[ \t]*([0-9]+),[ \t]*Total:[ \t]*([0-9]+).*
MTP_SUMMARY        [ \t]*Test summary: total: ([0-9]+), failed: ([0-9]+), succeeded: ([0-9]+), skipped: ([0-9]+).*
MTP_FAILED         [ \t]*failed (.+?) \([0-9.]+m?s\)
GRADLE_TEST        ([^ ]+) > (.+?) (FAILED|PASSED|SKIPPED)
GRADLE_SUMMARY     ([0-9]+) tests? completed, ([0-9]+) failed(?:, ([0-9]+) skipped)?
GRADLE_NOISE       > Task [^ ]+(?: [A-Z-]+)?|FAILURE: Build failed with an exception\.|BUILD (?:SUCCESSFUL|FAILED) in .*|\* What went wrong:|\* Try:
SUREFIRE_TEST      \[ERROR\] ([A-Za-z0-9_.$]+)\.([A-Za-z0-9_$\[\]]+)(?:\(.*\))? -- Time elapsed: .* <<< (?:FAILURE|ERROR)!
SUREFIRE_OLD       (?:\[ERROR\] )?([A-Za-z0-9_$]+)\(([A-Za-z0-9_.$]+)\)[ \t]+Time elapsed: .* <<< (?:FAILURE|ERROR)!
SUREFIRE_HEADER    \[ERROR\] (?:Failures|Errors): ?
SUREFIRE_LISTED    \[ERROR\][ \t]+([A-Za-z0-9_$]+)\.([A-Za-z0-9_$\[\]]+)(?::[0-9]+)?(?:[ \t].*)?
SUREFIRE_RUN       \[(?:ERROR|WARNING|INFO)\] Tests run: ([0-9]+), Failures: ([0-9]+), Errors: ([0-9]+), Skipped: ([0-9]+)(?:, Flakes: [0-9]+)?(.*)
MAVEN_JAVAC        \[ERROR\] (.+?\.(?:java|kt|scala)):\[([0-9]+),([0-9]+)\] (.+)
MAVEN_ERRORS       \[INFO\] ([0-9]+) errors?[ \t]*
MAVEN_NOISE        \[ERROR\] COMPILATION ERROR :?|\[INFO\] BUILD (?:FAILURE|SUCCESS)
JAVAC_ERROR        (.+?\.java):([0-9]+): error: (.+)
JAVAC_WARNING      (.+?\.java):([0-9]+): warning: .*
JAVAC_COUNT        ([0-9]+) errors?
KOTLIN_ERROR       e: (?:file://)?(.+?\.kts?):([0-9]+):([0-9]+) (.+)
KOTLIN_ERROR_OLD   e: (.+?\.kts?): \(([0-9]+), ([0-9]+)\): (.+)
KOTLIN_WARNING     w: .*
RSPEC_FAILED       rspec (?:\./)?(.+?)(?::[0-9]+|\[[0-9:]+\]) # (.*)
RSPEC_SUMMARY      ([0-9]+) examples?, ([0-9]+) failures?(?:, ([0-9]+) pending)?(?:, ([0-9]+) errors? occurred outside of examples)?
RSPEC_LOAD         An error occurred while loading (?:\./)?(.+?)\.?
PHPUNIT_SECTION    There (?:was|were) [0-9]+ ([a-z][a-z ]*?):
PHPUNIT_ITEM       [0-9]+\) ([A-Za-z0-9_\\]+)::([A-Za-z0-9_]+)(?: with data set (#[0-9]+|\"[^\"]*\"))?(?: \(.*\))?
PHPUNIT_TESTS      Tests: ([0-9]+), Assertions: [0-9]+((?:, [A-Za-z ]+: [0-9]+)*)\.
PHPUNIT_OK         OK \(([0-9]+) tests?, [0-9]+ assertions?\)
PHPUNIT_NOISE      FAILURES!|ERRORS!|OK, but (?:there were issues|incomplete, skipped, or risky tests)!
CTEST_LINE          *[0-9]+/[0-9]+ Test +#[0-9]+: (.+?) \.*(?: +|\*\*\*)(Passed|Failed|Exception|Timeout|Not Run|Skipped)(?:[: ].*)?
CTEST_LIST_HEADER  The following tests (?:FAILED|did not run):
CTEST_LISTED       [ \t]+[0-9]+ - (.+?) \((.+)\)
CTEST_SUMMARY      ([0-9]+)% tests passed, ([0-9]+) tests? failed out of ([0-9]+)
CTEST_NOISE         *Start +[0-9]+: .*|Test project .*|Errors while running CTest|Total Test time .*
CC_ERROR           (.+?\.(?:c|cc|cpp|cxx|c\+\+|C|h|hh|hpp|hxx|h\+\+|m|mm|cu|cuh|ino|S|s|ipp|tpp|inl)):([0-9]+)(?::([0-9]+))?: (?:fatal )?error: (.+?)(?: \[([^\]]+)\])?
CC_WARNING         (.+?\.(?:c|cc|cpp|cxx|c\+\+|C|h|hh|hpp|hxx|h\+\+|m|mm|cu|cuh|ino|S|s|ipp|tpp|inl)):([0-9]+)(?::([0-9]+))?: (?:warning|note): .*
CC_CONTEXT         In file included from .*|[ \t]+from .*|.+?: In (?:function|member function|constructor|destructor|instantiation of|static member function) .*:
CC_GENERATED       ([0-9]+) errors? generated\.
LD_UNDEFINED_ARCH  Undefined symbols for architecture [^ ]+:
LD_SYMBOL          [ \t]+\"(.+)\", referenced from:
LD_REFERENCE       undefined reference to [`'\u2018](.+?)['\u2019]
LD_NOISE           (?:[^ ]*/)?ld(?:\.[a-z]+)?: .*|collect2: error: ld returned [0-9]+ exit status|clang(?:\+\+)?: error: linker command failed.*|[ \t]+(?:_?[A-Za-z0-9_]+(?:\(.*\))? in .+\.o|in function .*)
MAKE_NOISE         g?make(?:\[[0-9]+\])?: \*\*\* .*|g?make(?:\[[0-9]+\])?: (?:Entering|Leaving) directory .*
GENERIC_FILE       (.+?\.[A-Za-z0-9]+):([0-9]+)(?::([0-9]+))?:? (?:fatal )?error(?:\[([^\]]+)\])?:? (.+)
GENERIC_PREFIX     (?:error|ERROR|Error|fatal|FATAL)(?:\[([^\]]+)\])?: (.+)
GENERIC_EXCEPTION  (?:Uncaught )?([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)): (.*)
COMPLETE_CARGO     Finished .* target\(s\) in .+
COMPLETE_SWIFTPM   Build complete!(?: \(.*\))?
COMPLETE_XCODEBUILD \*\* [A-Z]+(?: [A-Z]+)* SUCCEEDED \*\*(?: \[.*\])?
COMPLETE_GRADLE    BUILD SUCCESSFUL(?: in .+)?
COMPLETE_MAVEN     \[INFO\] BUILD SUCCESS
COMPLETE_PRETTIER  All matched files use Prettier code style!
COMPLETE_VITE      \u2713 built in [0-9]+(?:\.[0-9]+)?(?:ms|s)
PATCH_HEADER       \*\*\* (Add File|Delete File|Update File|Move to): (.+)
HASHLINE_HEADER    \[(.+)#([0-9A-Za-z]{4})\]
HASHLINE_INSERT    PUT (?:<[0-9]+|>[0-9]+\*?|>\$):
HASHLINE_MOVE      MV (.+)
```

### A.5 Rule order within each recogniser

The patterns each recogniser tries, in the order its code first uses them (5.3).

- Pytest: `PYTEST_FAIL`, `PYTEST_VERBOSE`, `PYTEST_SUMMARY`, `PYTEST_NONE`
- Unittest: `UNITTEST_FAIL`, `UNITTEST_RAN`, `UNITTEST_OK`, `UNITTEST_FAILED`
- Cargo: `LIBTEST_LISTED`, `LIBTEST_LINE`, `LIBTEST_STDOUT`, `LIBTEST_RESULT`, `LIBTEST_FAILURES`, `NEXTEST_FAIL`, `NEXTEST_OTHER`, `NEXTEST_SUMMARY`
- Rustc: `RUST_COULD_NOT`, `RUST_ABORTING`, `RUST_STATUS`, `RUST_HEADER`, `RUST_LOC`, `RUST_WARN`
- Swift: `XB_LISTED`, `XB_COUNT`, `ST_FAILED`, `ST_CASES`, `ST_RUN`, `ST_ISSUE`, `ST_OTHER`, `XC_CASE`, `XC_ERROR`, `XC_SUITE`, `XC_EXECUTED`, `XB_FAILING`, `XB_RESULT`, `XB_COMMANDS`, `SWIFTC_ERROR`, `SWIFTC_WARN`, `SWIFT_NOISE`
- Go: `GO_FAIL`, `GO_PASS`, `GO_RUN`, `GO_BARE`, `GO_HEADER`, `GO_PKG`, `GO_ERROR`
- Js: `MOCHA_ITEM`, `VITEST_LOAD`, `VITEST_FAIL`, `JEST_FILE`, `JEST_BULLET`, `JEST_SUITES`, `JEST_TESTS`, `JEST_NOISE`, `VITEST_FILE`, `VITEST_X`, `VITEST_FILES`, `VITEST_TESTS`, `MOCHA_PASSING`, `MOCHA_FAILING`, `MOCHA_PENDING`, `BUN_FAIL`, `BUN_COUNT`, `DENO_SUMMARY`, `DENO_FAILURE`
- Checkers: `TSC_ROW`, `TSC_PAREN`, `TSC_PRETTY`, `MYPY_FOUND`, `TSC_FOUND`, `FOUND_ERRORS`, `TSC_TABLE`, `ESLINT_UNIX`, `ESLINT_FILE`, `ESLINT_MSG`, `ESLINT_SUMMARY`, `ESLINT_FIXABLE`, `RUFF_CONCISE`, `RUFF_HEADER`, `RUFF_LOC`, `RUFF_PASSED`, `RUFF_WOULD`, `BLACK_WOULD`, `FORMAT_SUMMARY`, `MYPY_ERROR`, `MYPY_NOTE`, `MYPY_SUCCESS`, `PYRIGHT_ERROR`, `PYRIGHT_OTHER`, `PYRIGHT_RULE`, `PYRIGHT_SUMMARY`
- Dotnet: `MSB_ERROR`, `MSB_TOOL_ERROR`, `MSB_WARNING`, `MSB_COUNT`, `MSB_TERMINAL`, `MSB_NOISE`, `VSTEST_FAILED`, `VSTEST_PASSED`, `VSTEST_SUMMARY`, `MTP_SUMMARY`, `MTP_FAILED`
- Jvm: `SUREFIRE_RUN`, `SUREFIRE_LISTED`, `GRADLE_TEST`, `GRADLE_SUMMARY`, `GRADLE_NOISE`, `SUREFIRE_TEST`, `SUREFIRE_OLD`, `SUREFIRE_HEADER`, `MAVEN_JAVAC`, `MAVEN_ERRORS`, `MAVEN_NOISE`, `JAVAC_ERROR`, `JAVAC_WARNING`, `JAVAC_COUNT`, `KOTLIN_ERROR`, `KOTLIN_ERROR_OLD`, `KOTLIN_WARNING`
- Misc: `CTEST_LISTED`, `RSPEC_FAILED`, `RSPEC_SUMMARY`, `RSPEC_LOAD`, `PHPUNIT_SECTION`, `PHPUNIT_ITEM`, `PHPUNIT_TESTS`, `PHPUNIT_OK`, `PHPUNIT_NOISE`, `CTEST_LINE`, `CTEST_LIST_HEADER`, `CTEST_SUMMARY`, `CTEST_NOISE`
- Cc: `LD_SYMBOL`, `CC_ERROR`, `CC_WARNING`, `CC_CONTEXT`, `CC_GENERATED`, `LD_UNDEFINED_ARCH`, `LD_REFERENCE`, `LD_NOISE`, `MAKE_NOISE`

### A.6 Named strings and maps

Strings as JSON string literals (escapes as in JSON; regular-expression fragments that the patterns of A.4 already contain expanded); maps as `key → values`.

```text
GREP_FLAGS         "iEnwFPashHo"
NO_OUTPUT          "(Bash completed with no output)"
CODE_COMPLETED     "Script completed"
CC_EXT             "(?:c|cc|cpp|cxx|c\\+\\+|C|h|hh|hpp|hxx|h\\+\\+|m|mm|cu|cuh|ino|S|s|ipp|tpp|inl)"
NOT_IN_PATH        "(?<![A-Za-z0-9._~/\\\\-])"
RUNNER_SUBCOMMANDS mise → "run", "r"; deno → "task"; turbo → "run"; composer → "run-script", "run"
SCRIPT_LABEL       npm → "npm test", "npm run "; pnpm → "pnpm test", "pnpm run "; yarn → "yarn test", "yarn run "; bun → "bun run test", "bun run "; deno → "deno task test", "deno task "; composer → "composer test", "composer "; make → "make test", "make "; ninja → "ninja test", "ninja "; just → "just test", "just "; task → "task test", "task "; rake → "rake test", "rake "; mise → "mise run test", "mise run "; turbo → "turbo test", "turbo "; nx → "nx test", "nx "
```

<!-- appendix end -->
