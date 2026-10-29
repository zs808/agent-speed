using System.Text.Json;
using AgentSpeed.Core.TaskParsers;

namespace AgentSpeed.Core.Tasks;

/// <summary>
/// The file side of the composer (METRICS 6.9 <i>Composing the stream</i>): reads one agent's lines in file order while
/// they are extracted, on the reading thread, and turns each into a <see cref="TaskLine"/>. It keeps the working
/// directory, the source's header and copied-history state, and every call's input until its first result, which it reads
/// against that call (outcome, output text, running session, failures). Decisions that depend on other files are the
/// composer's. Never used by two threads at once; inputs and outputs stay in memory only.
/// </summary>
internal abstract class TaskLineReader(SourceKind source)
{
    /// <summary>Calls waiting for a result beyond this many drop their oldest input (a call that never gets one).</summary>
    public const int MaxPendingCalls = 8192;

    private readonly Dictionary<string, Pending> _pending = new(StringComparer.Ordinal);
    private long _sequence;

    /// <summary>The source whose parser tables apply (one per family).</summary>
    protected SourceKind Source { get; } = source;

    /// <summary>The agent's working directory when the current line is read (held in memory only).</summary>
    protected string? Cwd { get; set; }

    /// <summary>The file is read from its start again: everything starts over.</summary>
    public virtual void Reset()
    {
        _pending.Clear();
        Cwd = null;
    }

    protected static bool NonBlank(string? text) => !string.IsNullOrWhiteSpace(text);

    /// <summary>A call block: an action at the current cwd, unless the id or tool name is not a string.</summary>
    protected TaskCall? Call(string? id, string? tool, JsonElement input, string digest, long t)
    {
        if (id is null || tool is null)
        {
            return null;
        }

        var (action, info, continues, shell) = Analyze(tool, input, digest, t);
        Remember(id, new Pending(tool, Keep(input), Cwd, info, continues, null, null, 0, shell));
        return new TaskCall(id, action, continues, info, Cwd);
    }

    /// <summary>A Codex code-mode script (TASK-PARSERS 4.4): its tool calls are the actions, each with its own digest.</summary>
    protected TaskCall? Script(string? id, CodeScript script, long t)
    {
        if (id is null)
        {
            return null;
        }

        var calls = new TaskCall[script.Calls.Count];
        var pending = new Pending[script.Calls.Count];
        for (var i = 0; i < calls.Length; i++)
        {
            var call = script.Calls[i];
            var input = call.Input ?? default;
            var (action, info, continues, shell) = Analyze(call.Tool, input, call.Digest, t);
            calls[i] = new TaskCall(id, action, continues, info, Cwd);
            pending[i] = new Pending(call.Tool, input, Cwd, info, continues, null, null, 0, shell);
        }

        Remember(id, new Pending("exec", default, Cwd, null, null, pending, script.Prints, 0, null));
        return new TaskCall(id, null, null, null, Cwd) { Script = calls };
    }

    /// <summary>
    /// A result block, read against the file's call with that id (which then forgets its input); its answer is null when
    /// the file had no waiting call with that id.
    /// </summary>
    protected TaskResult? Result(string? id, JsonElement result, long t)
    {
        if (id is null)
        {
            return null;
        }

        if (!_pending.Remove(id, out var call))
        {
            return new TaskResult(id);
        }

        if (call.Script is not { } subs)
        {
            return new TaskResult(id) { Answer = Answer(call, result, t) };
        }

        var answers = new List<(int, TaskAnswer)>();
        var items = TaskParser.CodeItems(Source, result);
        var prints = call.Prints!;
        if (items is not null && prints.Count > 0 && items.Count == prints.Count)
        {
            var done = new bool[subs.Length];
            for (var i = 0; i < prints.Count; i++)
            {
                var index = prints[i].Call;
                if (index >= 0 && index < subs.Length && !done[index])
                {
                    done[index] = true;
                    answers.Add((index, Answer(subs[index], TaskInputs.StringElement(items[i] ?? string.Empty), t)));
                }
            }
        }

        return new TaskResult(id) { IsScript = true, ScriptAnswers = answers };
    }

    /// <summary>The call's action; a shell command is parsed once for its kind, its command info and its outcome.</summary>
    private (TaskAction Action, CommandInfo? Info, string? Continues, ShellAnalysis? Shell) Analyze(string tool, JsonElement input, string digest, long t)
    {
        var family = TaskFamilies.Of(Source);
        var (kind, text, shell) = ShellCommands.Classify(family, tool, input, Cwd);
        TaskCommand? command = null;
        CommandInfo? info = null;
        IReadOnlyList<EditHunk>? hunks = null;
        if (kind == ActionKind.Command && shell is not null)
        {
            info = shell.Info;
            var args = TaskJson.AsObject(input);
            if (info.Suite?.StartsWith("mvn ", StringComparison.Ordinal) == true
                && family == TaskFamily.Codex && TaskFamilies.ToolName(family, tool) == "exec_command"
                && args is { } obj && TaskJson.Has(obj, "workdir"))
            {
                var workdir = TaskJson.String(TaskJson.Get(obj, "workdir"));
                if (workdir != Cwd)
                    info = info with { MavenCompileKey = text is null ? null : TaskParser.CommandInfo(text, workdir).MavenCompileKey };
            }
            command = TaskCommand.Of(info);
        }
        else if (kind == ActionKind.Edit)
        {
            hunks = TaskParser.EditHunks(Source, tool, input, Cwd);
        }

        return (new TaskAction(t, kind, tool, digest, command, hunks), info, TaskParser.Continuation(Source, tool, input), shell);
    }

    private TaskAnswer Answer(Pending call, JsonElement result, long t)
    {
        // The output text only matters to a command's failures and a session's output (a file read's is never kept).
        var wantsText = call.Info is not null || call.Continues is not null;
        var read = ResultParser.Read(TaskFamilies.Of(Source), result, t, call.Tool, call.Input, call.Cwd,
            session: call.Continues is null, text: wantsText, command: call.Shell);
        var outcome = read.Outcome;
        if (read.Session is { } session)
        {
            return new TaskAnswer(outcome) { Session = session, Masked = Masked(call), Text = read.Text ?? TaskParser.ResultText(Source, result) };
        }

        if (!wantsText)
        {
            return new TaskAnswer(outcome);
        }

        var text = read.Text!;
        return new TaskAnswer(outcome)
        {
            Failures = call.Info is { } info ? TaskParser.Failures(info, text, call.Cwd, outcome.Failed) : null,
            Text = call.Continues is not null ? text : null,
        };
    }

    /// <summary><c>shell_call_state(…).masked</c>: the command's own flag, whatever the call's kind.</summary>
    private bool Masked(Pending call) =>
        call.Shell?.Info.Masked ?? (TaskParser.CommandText(Source, call.Tool, call.Input) is { } text && TaskParser.CommandInfo(text, call.Cwd).Masked);

    private void Remember(string id, Pending call)
    {
        if (_pending.ContainsKey(id))
        {
            return;
        }

        _pending[id] = call with { Sequence = ++_sequence };
        if (_pending.Count > MaxPendingCalls)
        {
            var oldest = _pending.MinBy(p => p.Value.Sequence).Key;
            _pending.Remove(oldest);
        }
    }

    private static JsonElement Keep(JsonElement input) =>
        input.ValueKind is JsonValueKind.Undefined ? default : input.Clone();

    /// <summary>A call waiting for its result: its input in memory until then.</summary>
    private sealed record Pending(
        string Tool,
        JsonElement Input,
        string? Cwd,
        CommandInfo? Info,
        string? Continues,
        Pending[]? Script,
        IReadOnlyList<CodePrint>? Prints,
        long Sequence,
        ShellAnalysis? Shell);
}
