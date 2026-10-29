using System.Text.Json;

namespace AgentSpeed.Core.TaskParsers;

/// <summary>What a tool call did (TASK-PARSERS 2).</summary>
public enum ActionKind
{
    Other,
    Read,
    Search,
    Edit,
    Command,
    Delegate,
    Web,
}

/// <summary>The class of a command (TASK-PARSERS 3); the numeric value is the rank, test &gt; build &gt; check &gt; other.</summary>
public enum CommandClass
{
    Other = 0,
    Check = 1,
    Build = 2,
    Test = 3,
}

/// <summary>
/// <c>command_info</c> (TASK-PARSERS 3): the chosen segment's class, suite (null or one of <c>SUITES</c>) and whether its
/// run is narrowed, the 16-hex key of the normalised command, and the <see cref="Masked"/>, <see cref="Background"/>
/// and <see cref="ErrorFilter"/> flags that <c>outcome</c> and <c>failures</c> read (3.7, 3.10).
/// </summary>
public sealed record CommandInfo(CommandClass Class, string? Suite, bool Targeted, string Key, bool Masked, bool Background, bool ErrorFilter, string? MavenCompileKey = null);

/// <summary>
/// <c>masked_status</c> (TASK-PARSERS 3.10): the status a masked command shows in its own output. A null
/// <see cref="ExitCode"/> means the exit code is not shown.
/// </summary>
public readonly record struct MaskedStatus(long? ExitCode, bool Failed);

/// <summary><c>outcome</c> (TASK-PARSERS 4.2): a null <see cref="Failed"/> means unknown, never a failure.</summary>
public readonly record struct TaskOutcome(long? ExitCode, bool? Failed, long? T);

/// <summary>A Claude background-task notice that reports an exit code (TASK-PARSERS 4.3).</summary>
public readonly record struct TaskNotification(string Session, long ExitCode);

/// <summary>How a code-mode script prints a call (TASK-PARSERS 4.4).</summary>
public enum CodePrintMode
{
    /// <summary><c>text(CALL)</c> or <c>text(NAME)</c>: the call's whole result.</summary>
    Result,

    /// <summary><c>text((CALL).output)</c> or <c>text(NAME.output)</c>: the command's output alone.</summary>
    Output,
}

/// <summary>One <c>tools.NAME(…)</c> call of a code-mode script: <see cref="Input"/> is its literal argument, or null.</summary>
public sealed record CodeCall(string Tool, JsonElement? Input, string Digest);

/// <summary>A printing statement of a plain script: the index of the call it prints and how.</summary>
public readonly record struct CodePrint(int Call, CodePrintMode Mode);

/// <summary><c>code_script</c> (TASK-PARSERS 4.4).</summary>
public sealed record CodeScript(IReadOnlyList<CodeCall> Calls, bool Plain, IReadOnlyList<CodePrint> Prints);

/// <summary>
/// <c>failures</c> (TASK-PARSERS 5): sorted distinct 16-hex fingerprints (at most 256), the failed and passed counts
/// (null = unknown) and the framework that decided them.
/// </summary>
public sealed record FailureReport(IReadOnlyList<string> Fingerprints, long? FailedCount, long? PassedCount, string? Framework);

/// <summary>
/// One hunk of <c>edit_hunks</c> (TASK-PARSERS 6): the digest of the file, of the removed lines (null for a whole-file
/// write or a deletion) and of the added lines.
/// </summary>
public readonly record struct EditHunk(string File, string? Old, string New);

/// <summary>The source families that record tool calls (TASK-PARSERS 1).</summary>
internal enum TaskFamily
{
    None,
    Claude,
    Codex,
    Omp,
    Pi,
    OpenClaw,
    Droid,
}

internal static class TaskFamilies
{
    public static TaskFamily Of(SourceKind source) => source switch
    {
        SourceKind.ClaudeCode or SourceKind.ClaudeDesktop => TaskFamily.Claude,
        SourceKind.Codex => TaskFamily.Codex,
        SourceKind.Omp => TaskFamily.Omp,
        SourceKind.Pi => TaskFamily.Pi,
        SourceKind.OpenClaw => TaskFamily.OpenClaw,
        SourceKind.Droid => TaskFamily.Droid,
        _ => TaskFamily.None,
    };

    /// <summary>omp, Pi and OpenClaw compare tool names after ASCII case folding.</summary>
    public static bool FoldsToolNames(TaskFamily family) => family is TaskFamily.Omp or TaskFamily.Pi or TaskFamily.OpenClaw;

    public static bool IsPiFamily(TaskFamily family) => family is TaskFamily.Omp or TaskFamily.Pi or TaskFamily.OpenClaw;

    /// <summary>The tool name as the source's table compares it.</summary>
    public static string ToolName(TaskFamily family, string tool) => FoldsToolNames(family) ? TaskText.LowerAscii(tool) : tool;
}

/// <summary>The lower-case names the spec and the dumps write.</summary>
public static class TaskWire
{
    public static string Name(ActionKind kind) => kind switch
    {
        ActionKind.Read => "read",
        ActionKind.Search => "search",
        ActionKind.Edit => "edit",
        ActionKind.Command => "command",
        ActionKind.Delegate => "delegate",
        ActionKind.Web => "web",
        _ => "other",
    };

    public static string Name(CommandClass cls) => cls switch
    {
        CommandClass.Test => "test",
        CommandClass.Build => "build",
        CommandClass.Check => "check",
        _ => "other",
    };

    public static string Name(CodePrintMode mode) => mode == CodePrintMode.Output ? "output" : "result";
}
