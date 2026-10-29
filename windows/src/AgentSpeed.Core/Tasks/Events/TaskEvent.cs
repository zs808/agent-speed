using AgentSpeed.Core.TaskParsers;

namespace AgentSpeed.Core.Tasks;

/// <summary>
/// One event of an agent's task stream (METRICS 6.9 <i>Event stream</i>): a prompt or an action, in file order. Holds
/// only what the stream lists — classes, flags, suite names, 16-hex digests, counts, exit codes and times.
/// </summary>
public abstract class TaskEvent
{
    private protected TaskEvent(long t)
    {
        T = t;
    }

    /// <summary>The time of the line that holds the prompt or the call (Unix ms); a queued Claude message's own time.</summary>
    public long T { get; }
}

/// <summary><c>{t, type: "prompt", continuation}</c>.</summary>
public sealed class TaskPrompt(long t, bool continuation) : TaskEvent(t)
{
    /// <summary>The prompt is in the continuation list: it joins the current task (6.9 <i>Continuation</i>).</summary>
    public bool Continuation { get; } = continuation;
}

/// <summary>
/// <c>{t, type: "action", kind, tool, digest, command?, outcome?, failures?, hunks?}</c>. The outcome and failures arrive
/// with the call's result, possibly lines later, so the composer sets them after the call.
/// </summary>
public sealed class TaskAction(long t, ActionKind kind, string tool, string digest, TaskCommand? command = null,
    IReadOnlyList<EditHunk>? hunks = null) : TaskEvent(t)
{
    public ActionKind Kind { get; } = kind;

    public string Tool { get; } = tool;

    /// <summary>16 lowercase hex digits (METRICS 6.10).</summary>
    public string Digest { get; } = digest;

    /// <summary><c>kind == command</c> with command text only: the four kept fields of <c>command_info</c>.</summary>
    public TaskCommand? Command { get; } = command;

    /// <summary><c>kind == edit</c> only: <c>edit_hunks</c>.</summary>
    public IReadOnlyList<EditHunk>? Hunks { get; } = hunks;

    /// <summary>Set once the result is read (or a running session is stitched); null before.</summary>
    public TaskOutcome? Outcome { get; set; }

    /// <summary>With the outcome of an action that has <see cref="Command"/>.</summary>
    public FailureReport? Failures { get; set; }
}

/// <summary>The fields <c>class</c>, <c>suite</c>, <c>targeted</c> and <c>key</c> of <c>command_info</c>.</summary>
public sealed record TaskCommand(CommandClass Class, string? Suite, bool Targeted, string Key, string? MavenCompileKey = null)
{
    public static TaskCommand Of(CommandInfo info)
    {
        ArgumentNullException.ThrowIfNull(info);
        return new TaskCommand(info.Class, info.Suite, info.Targeted, info.Key, info.MavenCompileKey);
    }
}
