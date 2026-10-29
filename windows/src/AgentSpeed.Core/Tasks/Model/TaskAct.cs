using AgentSpeed.Core.TaskParsers;

namespace AgentSpeed.Core.Tasks;

/// <summary>A run's result (METRICS 6.9 <i>Runs</i>); <see cref="None"/> = not a run (yet).</summary>
internal enum RunResult : byte
{
    None,
    Pass,
    Fail,
}

/// <summary>The check a run belongs to: <c>suite:&lt;suite&gt;</c> or <c>key:&lt;key&gt;</c> (METRICS 6.9 <i>Checks</i>).</summary>
internal readonly record struct CheckId(bool BySuite, string Name);

/// <summary>
/// One action of a task as 6.9 reads it at one instant (the reference's <c>Act</c>). The fields up to
/// <see cref="CheckNo"/> come from the event; the rest from the in-order pass of <see cref="TaskActs"/>, which every
/// prefix of the task shares.
/// </summary>
internal sealed class TaskAct
{
    private static readonly IReadOnlyList<string> NoFingerprints = [];

    public TaskAct(int index, TaskAction action, int keyId, long? now)
    {
        Index = index;
        Kind = action.Kind;
        KeyId = keyId;
        Command = Kind == ActionKind.Command ? action.Command : null;
        var outcome = action.Outcome;
        if (outcome is { T: { } t } && now is { } limit && t > limit)
        {
            outcome = null;
        }

        Outcome = outcome;
        Failures = outcome is null ? null : action.Failures;
        Hunks = Kind == ActionKind.Edit ? action.Hunks : null;
        var verifying = Command is { Class: CommandClass.Test or CommandClass.Build or CommandClass.Check };
        Weight = Kind == ActionKind.Other ? 0 : verifying ? 2 : 1;
        Result = verifying ? ResultOf(Outcome, Failures) : RunResult.None;
        var fingerprints = Failures?.Fingerprints ?? NoFingerprints;
        if (fingerprints.Count > 0)
        {
            Fingerprints = new HashSet<string>(fingerprints, StringComparer.Ordinal);
        }

        Errors = Result != RunResult.Fail ? 0 : Math.Max(Math.Max(1L, fingerprints.Count), FailedCount is { } count ? count : 0);
    }

    public int Index { get; }

    public ActionKind Kind { get; }

    /// <summary>The interned <c>(tool, digest)</c> key.</summary>
    public int KeyId { get; }

    public TaskCommand? Command { get; }

    /// <summary>Null before the result is read, or when it was read after the evaluation's instant.</summary>
    public TaskOutcome? Outcome { get; }

    /// <summary>With <see cref="Outcome"/> only.</summary>
    public FailureReport? Failures { get; }

    public IReadOnlyList<EditHunk>? Hunks { get; }

    /// <summary>0 for <c>other</c>, 2 for a test, build or check command, 1 otherwise.</summary>
    public int Weight { get; }

    public RunResult Result { get; }

    /// <summary>The distinct fingerprints; null when there are none.</summary>
    public HashSet<string>? Fingerprints { get; }

    /// <summary><c>F</c>: 0 for a pass, <c>max(1, |fingerprints|, failedCount)</c> for a fail.</summary>
    public long Errors { get; }

    /// <summary>The interned check of a run; -1 for any other action.</summary>
    public int CheckNo { get; set; } = -1;

    /// <summary>The position among the stream's runs; -1 for any other action.</summary>
    public int RunOrdinal { get; set; } = -1;

    /// <summary>Effective edits before this action.</summary>
    public int Block { get; set; }

    public bool Effective { get; set; }

    public bool Oscillating { get; set; }

    /// <summary><c>ΔI</c>.</summary>
    public int Gain { get; set; }

    public bool IsRun => Result != RunResult.None;

    public bool Targeted => Command is { Targeted: true };

    public string? Suite => Command?.Suite;

    public CommandClass RunClass => Command?.Class ?? CommandClass.Other;

    public long? FailedCount => Failures?.FailedCount;

    public CheckId Check => Command!.Suite == "mvn compile" && Command.MavenCompileKey is { } key
        ? new CheckId(false, "maven-compile:" + key)
        : Command.Targeted || Command.Suite is null ? new CheckId(false, Command.Key) : new CheckId(true, Command.Suite);

    /// <summary><c>run_result</c>.</summary>
    private static RunResult ResultOf(TaskOutcome? outcome, FailureReport? failures)
    {
        if (outcome is not { } known)
        {
            return RunResult.None;
        }

        var failedCount = failures?.FailedCount;
        var passedCount = failures?.PassedCount;
        if (known.Failed == true || failedCount > 0)
        {
            return RunResult.Fail;
        }

        return known.Failed == false || (failedCount == 0 && passedCount != 0) ? RunResult.Pass : RunResult.None;
    }
}
