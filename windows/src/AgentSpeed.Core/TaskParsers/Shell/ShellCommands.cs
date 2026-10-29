using System.Text;
using System.Text.Json;
using static AgentSpeed.Core.TaskParsers.ShellWords;

namespace AgentSpeed.Core.TaskParsers;

/// <summary>A command with its parse and classification, so <c>command_info</c> and <c>masked_status</c> share one parse.</summary>
internal sealed record ShellAnalysis(ShellParse Parsed, CommandClassification[] Classes, int Chosen, CommandInfo Info);

/// <summary>
/// The command text of a shell tool (TASK-PARSERS 3.1), <c>command_info</c> (3, 3.7), the shell kind (3.9), the patch
/// texts a command carries, and <c>action_kind</c> (2).
/// </summary>
internal static class ShellCommands
{
    private static readonly HashSet<string> ArgvTools = Set("shell", "container.exec", "local_shell");

    private static readonly HashSet<string> Cosmetic = Set(
        "-q", "-qq", "-v", "-vv", "-vvv", "--quiet", "--verbose", "--color", "--no-color", "--colors", "--no-colors", "--ansi", "--no-ansi",
        "--nocapture", "--no-capture", "--show-output", "--silent");

    private static readonly string[] CosmeticPrefixes = ["--color=", "--colour=", "--colors=", "--tb="];

    private static readonly HashSet<string> ReadProgs = Set(
        "cat", "head", "tail", "less", "more", "nl", "wc", "ls", "tree", "stat", "file", "pwd", "bat", "batcat", "sed", "awk", "jq", "yq",
        "realpath", "readlink", "du", "basename", "dirname", "od", "xxd", "hexdump", "strings", "diff", "cmp", "sort", "uniq", "cut", "tr",
        "column", "which", "type", "whereis", "printenv", "date", "env", "exa", "eza", "lsd");

    private static readonly HashSet<string> SearchProgs = Set("rg", "grep", "egrep", "fgrep", "ag", "ack", "find", "fd", "fdfind", "locate", "mdfind");

    private static readonly HashSet<string> GitRead = Set(
        "status", "diff", "log", "show", "blame", "rev-parse", "ls-tree", "cat-file", "describe", "shortlog", "reflog", "branch", "remote",
        "tag", "config");

    private static readonly HashSet<string> GitSearch = Set("grep", "ls-files");
    private static readonly HashSet<string> GitValue = Set("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env");
    private static readonly HashSet<string> FindActions = Set("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls");
    private static readonly HashSet<string> WriteRedirections = Set(">", ">>", ">|", "&>", "&>>", "<>");

    /// <summary><c>command_text</c> (3.1): the command line a shell tool runs, or null.</summary>
    public static string? CommandText(TaskFamily family, string? tool, JsonElement input)
    {
        if (family == TaskFamily.None || tool is null)
        {
            return null;
        }

        var name = TaskFamilies.ToolName(family, tool);
        if (ToolKinds.Of(family, name) != TaskParsers.ActionKind.Command)
        {
            return null;
        }

        var args = TaskJson.AsObject(input);
        if (family == TaskFamily.Codex && name == "exec")
        {
            if (args is { } obj)
            {
                return TaskJson.GetString(obj, "cmd") ?? TaskJson.GetString(obj, "command");
            }

            return TaskJson.String(input);
        }

        if (args is not { } fields)
        {
            return null;
        }

        var command = TaskJson.Get(fields, family == TaskFamily.Codex && name == "exec_command" ? "cmd" : "command");
        if (command.ValueKind == JsonValueKind.String)
        {
            return TaskJson.String(command);
        }

        if (family == TaskFamily.Codex && ArgvTools.Contains(name) && command.ValueKind == JsonValueKind.Array && command.GetArrayLength() > 0)
        {
            var builder = new StringBuilder();
            foreach (var item in command.EnumerateArray())
            {
                if (item.ValueKind != JsonValueKind.String)
                {
                    return null;
                }

                if (builder.Length > 0)
                {
                    builder.Append(' ');
                }

                AppendQuoted(builder, TaskJson.String(item)!);
            }

            return builder.ToString();
        }

        return null;
    }

    /// <summary>An argument that matches <c>SAFE_ARG</c> stays as is; any other is single-quoted.</summary>
    private static void AppendQuoted(StringBuilder builder, string arg)
    {
        if (IsSafeArg(arg))
        {
            builder.Append(arg);
            return;
        }

        builder.Append('\'').Append(arg.Replace("'", "'\"'\"'", StringComparison.Ordinal)).Append('\'');
    }

    /// <summary><c>SAFE_ARG</c>: <c>[A-Za-z0-9_@%+=:,./-]+</c>.</summary>
    private static bool IsSafeArg(string arg)
    {
        if (arg.Length == 0)
        {
            return false;
        }

        foreach (var c in arg)
        {
            if (!char.IsAsciiLetterOrDigit(c) && c is not ('_' or '@' or '%' or '+' or '=' or ':' or ',' or '.' or '/' or '-'))
            {
                return false;
            }
        }

        return true;
    }

    /// <summary><c>action_kind</c> (2).</summary>
    public static ActionKind ActionKind(TaskFamily family, string? tool, JsonElement input, string? cwd)
    {
        if (family == TaskFamily.None || tool is null)
        {
            return TaskParsers.ActionKind.Other;
        }

        var kind = ToolKinds.Of(family, TaskFamilies.ToolName(family, tool)) ?? TaskParsers.ActionKind.Other;
        if (kind != TaskParsers.ActionKind.Command)
        {
            return kind;
        }

        var text = CommandText(family, tool, input);
        return text is null ? TaskParsers.ActionKind.Command : ShellKind(ShellParser.Parse(text, cwd));
    }

    /// <summary><c>command_info</c> (3).</summary>
    public static CommandInfo Info(string text, string? cwd) => Analyze(text, cwd).Info;

    /// <summary>
    /// <see cref="ActionKind"/> and, for a shell tool with command text, that text and its <see cref="Analyze"/> — the same
    /// values the separate calls give, from one parse of the command.
    /// </summary>
    internal static (ActionKind Kind, string? Text, ShellAnalysis? Command) Classify(TaskFamily family, string? tool, JsonElement input, string? cwd)
    {
        if (family == TaskFamily.None || tool is null)
        {
            return (TaskParsers.ActionKind.Other, null, null);
        }

        var kind = ToolKinds.Of(family, TaskFamilies.ToolName(family, tool)) ?? TaskParsers.ActionKind.Other;
        if (kind != TaskParsers.ActionKind.Command || CommandText(family, tool, input) is not { } text)
        {
            return (kind, null, null);
        }

        var parsed = ShellParser.Parse(text, cwd);
        var (classes, chosen) = Choose(parsed.Segments, cwd);
        return (ShellKind(parsed), text, new ShellAnalysis(parsed, classes, chosen, InfoOf(parsed, classes, chosen, cwd)));
    }

    /// <summary><c>parse_command(text, cwd).patches</c>: every patch text of the command, in order (3.9, 6.2).</summary>
    public static IReadOnlyList<string> PatchTexts(string text, string? cwd) => ShellParser.Parse(text, cwd).Patches;

    /// <summary><c>masked_status</c> (3.10).</summary>
    public static MaskedStatus? MaskedStatusOf(string text, string outputText, string? cwd) => MaskedRuns.Status(Analyze(text, cwd), outputText);

    public static ShellAnalysis Analyze(string text, string? cwd)
    {
        var parsed = ShellParser.Parse(text, cwd);
        var (classes, chosen) = Choose(parsed.Segments, cwd);
        return new ShellAnalysis(parsed, classes, chosen, InfoOf(parsed, classes, chosen, cwd));
    }

    private static bool IsNeutral(ShellSegment seg) => seg.Words.Length == 0 || Neutral.Contains(Prog(seg.Words[0]));

    /// <summary>The class of every segment and the chosen one: the first with the highest rank (3.7); -1 when none classifies.</summary>
    private static (CommandClassification[] Classes, int Chosen) Choose(List<ShellSegment> segs, string? cwd)
    {
        var classes = new CommandClassification[segs.Count];
        var chosen = -1;
        var best = CommandClass.Other;
        for (var k = 0; k < segs.Count; k++)
        {
            var result = IsNeutral(segs[k]) ? CommandClassification.Other : CommandPrograms.Classify(segs[k].Words, cwd);
            classes[k] = result;
            if (result.Class > best)
            {
                best = result.Class;
                chosen = k;
            }
        }

        return (classes, chosen);
    }

    private static CommandInfo InfoOf(ShellParse parsed, CommandClassification[] classes, int chosen, string? cwd)
    {
        var segs = parsed.Segments;
        var key = KeyOf(segs, classes, cwd);
        if (chosen < 0)
        {
            return new CommandInfo(CommandClass.Other, null, false, key, false, segs.Exists(s => s.Background), false);
        }

        var seg = segs[chosen];
        var best = classes[chosen];
        var masked = false;
        if (chosen + 1 < segs.Count)
        {
            masked = (seg.OpAfter == "|" && !parsed.Pipefail) || segs.Skip(chosen + 1).Any(x => x.OpBefore is ";" or "||" or "&");
        }

        var errorFilter = best.Class is CommandClass.Build or CommandClass.Check && MaskedRuns.ErrorFilter(segs, chosen);
        return new CommandInfo(best.Class, best.Suite, best.Targeted, key, masked, seg.Background, errorFilter,
            masked || parsed.CompileScopeUnsafe ? null : MavenCompileKey(segs, classes, chosen, cwd));
    }

    private static readonly HashSet<string> MavenValues = Set("-pl", "--projects", "-f", "--file", "-s", "--settings", "-P", "--activate-profiles", "-T", "--threads", "-rf", "--resume-from", "-gs", "--global-settings", "-t", "--toolchains", "-l", "--log-file");
    private static readonly HashSet<string> MavenPhases = Set("compile", "test-compile", "process-classes", "test", "integration-test", "verify", "install", "deploy", "package");
    private static readonly HashSet<string> MavenReject = Set("-h", "--help", "-v", "--version", "-V", "--show-version", "-fn", "--fail-never");

    private static string? MavenCompileKey(List<ShellSegment> segs, CommandClassification[] classes, int chosen, string? cwd)
    {
        var basis = TaskText.NormCwd(cwd);
        var rawCwd = cwd?.Replace('\\', '/');
        var absolute = rawCwd is not null && (rawCwd.StartsWith('/') ||
            (rawCwd.Length >= 3 && char.IsAsciiLetter(rawCwd[0]) && rawCwd[1] == ':' && rawCwd[2] == '/'));
        if (basis is null || !absolute || classes.Count(c => c.Class != CommandClass.Other) != 1)
            return null;
        var seg = segs[chosen];
        if (Prog(seg.Words[0]) != "mvn" || seg.Background) return null;
        foreach (var prior in segs.Take(chosen))
        {
            if (prior.Words.Length > 0 && Prog(prior.Words[0]) is "cd" or "pushd" or "popd")
            {
                if (prior.OpAfter != "&&" || prior.Words.Length != 2 || prior.Words[1] == "-" || prior.Words[1].StartsWith('~')
                    || prior.Words.Any(IsDynamic)) return null;
            }
        }
        var args = seg.Words.Skip(1).ToArray();
        if (new[] { basis, seg.Context }.Concat(args).Any(IsDynamic)) return null;
        List<string> kept = [], goals = [];
        for (var k = 0; k < args.Length; k++)
        {
            var w = args[k];
            if (MavenReject.Contains(w)) return null;
            if (MavenValues.Contains(w))
            {
                if (k + 1 >= args.Length) return null;
                kept.Add(w); kept.Add(args[++k]); continue;
            }
            if (Cosmetic.Contains(w) || CosmeticPrefixes.Any(prefix => w.StartsWith(prefix, StringComparison.Ordinal))) continue;
            if (w.StartsWith("-D", StringComparison.Ordinal))
            {
                var prop = w[2..].Split('=', 2)[0];
                if (prop is "test" or "it.test" or "skipTests" or "surefire.failIfNoSpecifiedTests") continue;
                if ((prop.Contains("skip", StringComparison.OrdinalIgnoreCase) && prop != "maven.test.skip") || prop.Contains("failonerror", StringComparison.OrdinalIgnoreCase)) return null;
            }
            if (!w.StartsWith('-')) goals.Add(w); else kept.Add(w);
        }
        if (goals.Count > 0 && goals[0] == "clean") goals.RemoveAt(0);
        if (goals.Count != 1 || !MavenPhases.Contains(goals[0])) return null;
        return TaskText.Sha16(string.Join("\x1f", new[] { "maven-main-compile-v1", basis, seg.Context }.Concat(kept)));
    }

    private static bool IsDynamic(string word) => word.IndexOfAny(['$', '`', '*', '?', '[']) >= 0;

    /// <summary>The key (3.7): the digest of the kept segments' normalised tokens.</summary>
    private static string KeyOf(List<ShellSegment> segs, CommandClassification[] classes, string? cwd)
    {
        var kept = new List<ShellSegment>();
        for (var k = 0; k < segs.Count; k++)
        {
            if (classes[k].Class != CommandClass.Other)
            {
                kept.Add(segs[k]);
            }
        }

        if (kept.Count == 0)
        {
            kept.AddRange(segs.Where(s => !IsNeutral(s)));
        }

        if (kept.Count == 0)
        {
            kept.AddRange(segs.Where(s => s.Words.Length > 0));
        }

        var builder = new StringBuilder();
        for (var i = 0; i < kept.Count; i++)
        {
            if (i > 0)
            {
                builder.Append('\u001e');
            }

            var tokens = SegmentTokens(kept[i]);
            for (var t = 0; t < tokens.Count; t++)
            {
                if (t > 0)
                {
                    builder.Append('\u001f');
                }

                builder.Append(tokens[t]);
            }
        }

        return TaskText.Sha16(builder.ToString());
    }

    private static List<string> SegmentTokens(ShellSegment seg)
    {
        var words = seg.Words;
        var p = Prog(words[0]);
        var tokens = new List<string>(words.Length + 2) { p };
        for (var i = 1; i < words.Length; i++)
        {
            var w = words[i];
            if (Cosmetic.Contains(w) || Array.Exists(CosmeticPrefixes, prefix => w.StartsWith(prefix, StringComparison.Ordinal)))
            {
                continue;
            }

            if (p == "pytest" && (w is "-s" or "--capture=no" || IsReportFlag(w)))
            {
                continue;
            }

            tokens.Add(w);
        }

        if (p is "npm" or "pnpm" or "yarn" && tokens.Count > 1 && tokens[1] is "test" or "t" or "tst")
        {
            tokens.RemoveAt(1);
            tokens.InsertRange(1, ["run", "test"]);
        }
        else if (p is "npm" or "pnpm" or "yarn" or "bun" && tokens.Count > 1 && tokens[1] is "run-script" or "rum" or "urn")
        {
            tokens[1] = "run";
        }
        else if (p is "pnpm" or "yarn" && tokens.Count > 1 && !CommandPrograms.NpmBuiltins.Contains(tokens[1]) && !tokens[1].StartsWith('-'))
        {
            tokens.Insert(1, "run");
        }

        if (seg.Context.Length > 0)
        {
            tokens.Insert(0, "@" + seg.Context);
        }

        return tokens;
    }

    /// <summary><c>-r[A-Za-z]+</c>.</summary>
    private static bool IsReportFlag(string w)
    {
        if (w.Length < 3 || w[0] != '-' || w[1] != 'r')
        {
            return false;
        }

        for (var i = 2; i < w.Length; i++)
        {
            if (!char.IsAsciiLetter(w[i]))
            {
                return false;
            }
        }

        return true;
    }

    /// <summary><c>shell_kind</c> (3.9) for <c>action_kind</c>.</summary>
    private static ActionKind ShellKind(ShellParse parsed)
    {
        if (parsed.ApplyPatch || parsed.Patches.Count > 0)
        {
            return TaskParsers.ActionKind.Edit;
        }

        var (_, chosen) = Choose(parsed.Segments, null);
        if (chosen >= 0)
        {
            return TaskParsers.ActionKind.Command;
        }

        foreach (var seg in parsed.Segments)
        {
            foreach (var (op, target) in seg.Redirections)
            {
                if (WriteRedirections.Contains(op) && target is not ("/dev/null" or "NUL" or "nul") && !IsFdTarget(target))
                {
                    return TaskParsers.ActionKind.Command;
                }
            }
        }

        var any = false;
        var search = false;
        foreach (var seg in parsed.Segments)
        {
            if (IsNeutral(seg))
            {
                continue;
            }

            any = true;
            var p = Prog(seg.Words[0]);
            var args = seg.Words;
            if (p == "git")
            {
                var k = DropOptions(args, 1, GitValue);
                var sub = k < args.Length ? args[k] : null;
                if (sub is not null && GitRead.Contains(sub))
                {
                    continue;
                }

                if (sub is not null && GitSearch.Contains(sub))
                {
                    search = true;
                    continue;
                }

                return TaskParsers.ActionKind.Command;
            }

            if (p == "sed" && HasArg(args, a => a.StartsWith("-i", StringComparison.Ordinal) || a.StartsWith("--in-place", StringComparison.Ordinal)))
            {
                return TaskParsers.ActionKind.Command;
            }

            if (ReadProgs.Contains(p))
            {
                continue;
            }

            if (SearchProgs.Contains(p))
            {
                if (p == "find" && HasArg(args, FindActions.Contains))
                {
                    return TaskParsers.ActionKind.Command;
                }

                search = true;
                continue;
            }

            return TaskParsers.ActionKind.Command;
        }

        if (!any)
        {
            return TaskParsers.ActionKind.Command;
        }

        return search ? TaskParsers.ActionKind.Search : TaskParsers.ActionKind.Read;
    }

    private static bool HasArg(string[] words, Predicate<string> test)
    {
        for (var i = 1; i < words.Length; i++)
        {
            if (test(words[i]))
            {
                return true;
            }
        }

        return false;
    }

    /// <summary><c>FD_TARGET</c>: <c>[0-9-]+</c>.</summary>
    private static bool IsFdTarget(string target)
    {
        if (target.Length == 0)
        {
            return false;
        }

        foreach (var c in target)
        {
            if (!char.IsAsciiDigit(c) && c != '-')
            {
                return false;
            }
        }

        return true;
    }
}
