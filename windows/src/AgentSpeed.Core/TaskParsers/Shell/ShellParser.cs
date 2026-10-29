namespace AgentSpeed.Core.TaskParsers;

/// <summary>A segment of a command (TASK-PARSERS 3.3): its words after keywords, prefixes and runners are removed.</summary>
internal sealed class ShellSegment
{
    public string[] Words { get; set; } = [];

    public List<(string Op, string Target)> Redirections { get; } = [];

    public List<string> HereDocs { get; } = [];

    /// <summary>The operators read since the previous segment, parentheses included.</summary>
    public List<string> Ops { get; set; } = [];

    public string? OpBefore { get; set; }

    public string? OpAfter { get; set; }

    public bool Background { get; set; }

    /// <summary>The directory context the segment runs in, relative to the starting directory (empty = the start).</summary>
    public string Context { get; set; } = "";

    public bool HasContent => Words.Length > 0 || HereDocs.Count > 0;
}

/// <summary>A parsed command: its kept segments, its patch texts and whether it sets <c>pipefail</c>.</summary>
internal sealed class ShellParse
{
    public List<ShellSegment> Segments { get; } = [];

    /// <summary>Here-document bodies and words that hold a <c>*** Begin Patch</c> line, in order.</summary>
    public List<string> Patches { get; } = [];

    public bool ApplyPatch { get; set; }

    public bool Pipefail { get; set; }

    public bool CompileScopeUnsafe { get; set; }
}

/// <summary>Segments, directory contexts, shell wrappers and operators between segments (TASK-PARSERS 3.3).</summary>
internal static class ShellParser
{
    private const int MaxWrapperDepth = 4;

    private sealed class State(string? cwd)
    {
        public string? Cwd { get; } = cwd;

        public string Context { get; set; } = "";

        public Stack<string> Parens { get; } = new();

        public Stack<string> Dirs { get; } = new();

        public List<string> Carry { get; set; } = [];
    }

    public static ShellParse Parse(string text, string? cwd)
    {
        var parsed = new ShellParse();
        var st = new State(cwd);
        ParseInto(TaskText.Scalars(text), parsed, st, 0);
        var segs = parsed.Segments;
        for (var k = 0; k < segs.Count; k++)
        {
            var seg = segs[k];
            seg.OpBefore = k > 0 ? LastOperator(seg.Ops) : null;
            var following = k + 1 < segs.Count ? segs[k + 1].Ops : st.Carry;
            seg.OpAfter = LastOperator(following);
            if (FirstOperator(following) == "&")
            {
                seg.Background = true;
            }
        }

        return parsed;
    }

    private static void ParseInto(string text, ShellParse parsed, State st, int depth)
    {
        if (depth > MaxWrapperDepth)
        {
            return;
        }

        var (segments, tail) = Split(ShellTokenizer.Tokenize(TaskText.Lf(text)));
        foreach (var seg in segments)
        {
            ApplyParens(seg.Ops, st);
            var ops = st.Carry.Count == 0 ? seg.Ops : [.. st.Carry, .. seg.Ops];
            st.Carry = [];
            if (seg.Words.Any(word => ShellWords.IsAssignment(word) && !word.StartsWith("JAVA_HOME=", StringComparison.Ordinal)))
                parsed.CompileScopeUnsafe = true;
            ShellWords.DropKeywords(seg);
            ShellWords.StripPrefixes(seg);
            ShellWords.DropKeywords(seg);
            if (seg.Words.Length > 0 && (ShellWords.ClosingKeywords.Contains(seg.Words[0]) || ShellWords.Constructs.Contains(seg.Words[0])))
            {
                seg.Words = [];
            }

            var script = ShellWords.ShellScript(seg);
            foreach (var body in seg.HereDocs)
            {
                if (HasPatchLine(body))
                {
                    parsed.Patches.Add(body);
                }
            }

            if (script is not null)
            {
                st.Carry = ops;
                var before = parsed.Segments.Count;
                ParseInto(script, parsed, st, depth + 1);
                if (seg.Background && parsed.Segments.Count > before)
                {
                    parsed.Segments[^1].Background = true;
                }

                continue;
            }

            foreach (var word in seg.Words)
            {
                if (word.Contains('\n', StringComparison.Ordinal) && HasPatchLine(word))
                {
                    parsed.Patches.Add(word);
                }
            }

            seg.Words = ShellWords.Unwrap(seg.Words);
            seg.Context = st.Context;
            if (seg.Words.Length > 0)
            {
                var p = ShellWords.Prog(seg.Words[0]);
                if (p is "apply_patch" or "applypatch")
                {
                    parsed.ApplyPatch = true;
                }

                if (p == "set" && SetsPipefail(seg.Words))
                {
                    parsed.Pipefail = true;
                }

                if (p is "cd" or "pushd")
                {
                    if (p == "pushd")
                    {
                        st.Dirs.Push(st.Context);
                    }

                    var target = "~";
                    for (var k = 1; k < seg.Words.Length; k++)
                    {
                        if (!seg.Words[k].StartsWith('-') || seg.Words[k] == "-")
                        {
                            target = seg.Words[k];
                            break;
                        }
                    }

                    st.Context = NormDir(target, st.Context, st.Cwd);
                }
                else if (p == "popd" && st.Dirs.Count > 0)
                {
                    st.Context = st.Dirs.Pop();
                }
            }

            if (seg.HasContent)
            {
                seg.Ops = ops;
                parsed.Segments.Add(seg);
            }
            else
            {
                st.Carry = ops;
            }
        }

        ApplyParens(tail, st);
        st.Carry = [.. st.Carry, .. tail];
    }

    /// <summary>Segments with the operators read since the previous one, and the operators after the last one.</summary>
    private static (List<ShellSegment> Segments, List<string> Tail) Split(List<ShellToken> items)
    {
        var segments = new List<ShellSegment>();
        var current = new ShellSegment();
        var words = new List<string>();
        var ops = new List<string>();
        foreach (var item in items)
        {
            switch (item.Kind)
            {
                case ShellTokenKind.Word:
                    words.Add(item.Text);
                    break;
                case ShellTokenKind.Redirection:
                    current.Redirections.Add((item.Text, item.Target));
                    break;
                case ShellTokenKind.HereDoc:
                    current.HereDocs.Add(item.Text);
                    break;
                default:
                    if (words.Count > 0 || current.HereDocs.Count > 0)
                    {
                        current.Words = [.. words];
                        current.Ops = ops;
                        segments.Add(current);
                        ops = [];
                    }

                    current = new ShellSegment();
                    words.Clear();
                    ops.Add(item.Text);
                    break;
            }
        }

        if (words.Count > 0 || current.HereDocs.Count > 0)
        {
            current.Words = [.. words];
            current.Ops = ops;
            segments.Add(current);
            ops = [];
        }

        return (segments, ops);
    }

    private static void ApplyParens(List<string> ops, State st)
    {
        foreach (var op in ops)
        {
            if (op == "(")
            {
                st.Parens.Push(st.Context);
            }
            else if (op == ")" && st.Parens.Count > 0)
            {
                st.Context = st.Parens.Pop();
            }
        }
    }

    /// <summary><c>set</c> with a word matching <c>PIPEFAIL_FLAG</c> (<c>-[A-Za-z]*o</c>) followed by <c>pipefail</c>.</summary>
    private static bool SetsPipefail(string[] words)
    {
        for (var k = 0; k + 1 < words.Length; k++)
        {
            if (words[k + 1] == "pipefail" && IsPipefailFlag(words[k]))
            {
                return true;
            }
        }

        return false;
    }

    private static bool IsPipefailFlag(string w)
    {
        if (w.Length < 2 || w[0] != '-' || w[^1] != 'o')
        {
            return false;
        }

        for (var i = 1; i < w.Length; i++)
        {
            if (!char.IsAsciiLetter(w[i]))
            {
                return false;
            }
        }

        return true;
    }

    /// <summary>Some line, after optional spaces or tabs, is <c>*** Begin Patch</c> (<c>PATCH_LINE</c>).</summary>
    public static bool HasPatchLine(string text)
    {
        var start = 0;
        while (start <= text.Length)
        {
            var end = text.IndexOf('\n', start);
            var line = text.AsSpan(start, (end < 0 ? text.Length : end) - start);
            if (line.Trim(" \t").SequenceEqual("*** Begin Patch"))
            {
                return true;
            }

            if (end < 0)
            {
                break;
            }

            start = end + 1;
        }

        return false;
    }

    private static string? LastOperator(List<string> ops)
    {
        for (var i = ops.Count - 1; i >= 0; i--)
        {
            if (ops[i] is not ("(" or ")"))
            {
                return ops[i];
            }
        }

        return null;
    }

    private static string? FirstOperator(List<string> ops)
    {
        foreach (var op in ops)
        {
            if (op is not ("(" or ")"))
            {
                return op;
            }
        }

        return null;
    }

    /// <summary>The directory context after <c>cd target</c> (3.3).</summary>
    public static string NormDir(string target, string ctx, string? cwd)
    {
        var t = target.Replace('\\', '/');
        if (t.Length == 0)
        {
            return ctx;
        }

        var rel = TaskText.CwdRelative(t, cwd, out var inside);
        string joined;
        if (inside)
        {
            joined = rel;
        }
        else if (t == "~" || t.StartsWith("~/", StringComparison.Ordinal) || t.StartsWith('/') || IsDrive(t) || t == "-")
        {
            joined = t;
        }
        else
        {
            joined = ctx.Length > 0 ? ctx + "/" + t : t;
        }

        var absolute = joined.StartsWith('/');
        var parts = new List<string>();
        foreach (var part in joined.Split('/'))
        {
            if (part is "" or ".")
            {
                continue;
            }

            if (part == ".." && parts.Count > 0 && parts[^1] != "..")
            {
                parts.RemoveAt(parts.Count - 1);
                continue;
            }

            parts.Add(part);
        }

        var result = (absolute ? "/" : "") + string.Join('/', parts);
        return result is "" or "." ? "" : result;
    }

    /// <summary><c>DRIVE</c> as a prefix match: <c>[A-Za-z]:/</c>.</summary>
    private static bool IsDrive(string t) => t.Length >= 3 && char.IsAsciiLetter(t[0]) && t[1] == ':' && t[2] == '/';
}
