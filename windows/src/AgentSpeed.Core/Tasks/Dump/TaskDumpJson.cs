using System.Text.Json;
using AgentSpeed.Core.History;
using AgentSpeed.Core.TaskParsers;

namespace AgentSpeed.Core.Tasks;

/// <summary>
/// The task score in the reference's dump format (METRICS 6.9 <i>Dumps</i>, <c>spec/tools/reference.py</c>
/// <c>task_value_rows</c>, <c>RECORD_FIELDS</c>, <c>dump_tasks</c>, and the events of <c>task-e2e-expected.json</c>), so
/// <c>compare_dumps.py</c> and the fixture tests read the platforms' output the same way. Classes, digests, counts,
/// exit codes and times only.
/// </summary>
internal static class TaskDumpJson
{
    public static void WriteEvent(Utf8JsonWriter w, TaskEvent e)
    {
        w.WriteStartObject();
        w.WriteNumber("t", e.T);
        switch (e)
        {
            case TaskPrompt prompt:
                w.WriteString("type", "prompt");
                w.WriteBoolean("continuation", prompt.Continuation);
                break;
            case TaskAction action:
                w.WriteString("type", "action");
                w.WriteString("kind", TaskWire.Name(action.Kind));
                w.WriteString("tool", action.Tool);
                w.WriteString("digest", action.Digest);
                if (action.Command is { } command)
                {
                    w.WritePropertyName("command");
                    WriteCommand(w, command.Class, command.Suite, command.Targeted, command.Key, command.MavenCompileKey);
                }

                if (action.Hunks is { } hunks)
                {
                    w.WriteStartArray("hunks");
                    foreach (var hunk in hunks)
                    {
                        w.WriteStartObject();
                        w.WriteString("file", hunk.File);
                        w.WriteString("old", hunk.Old);
                        w.WriteString("new", hunk.New);
                        w.WriteEndObject();
                    }

                    w.WriteEndArray();
                }

                if (action.Outcome is { } outcome)
                {
                    w.WriteStartObject("outcome");
                    WriteNullable(w, "exitCode", outcome.ExitCode);
                    WriteNullable(w, "failed", outcome.Failed);
                    WriteNullable(w, "t", outcome.T);
                    w.WriteEndObject();
                }

                if (action.Failures is { } failures)
                {
                    w.WriteStartObject("failures");
                    w.WriteStartArray("fingerprints");
                    foreach (var fingerprint in failures.Fingerprints)
                    {
                        w.WriteStringValue(fingerprint);
                    }

                    w.WriteEndArray();
                    WriteNullable(w, "failedCount", failures.FailedCount);
                    WriteNullable(w, "passedCount", failures.PassedCount);
                    w.WriteString("framework", failures.Framework);
                    w.WriteEndObject();
                }

                break;
        }

        w.WriteEndObject();
    }

    /// <summary>A <c>task</c> row of <c>--dump-today</c> / a <c>values</c> item of the fixture.</summary>
    public static void WriteAgentRow(Utf8JsonWriter w, TaskAgentRow row, Func<string, string> label)
    {
        var agent = row.Agent;
        w.WriteStartObject();
        w.WriteString("agentId", label(agent.Id));
        w.WriteString("source", SourceNames.Of(agent.Source));
        w.WriteString("phase", PhaseName(agent.Phase));
        WriteNullable(w, "lastLineMs", agent.LastLineMs);
        w.WriteString("parentId", agent.ParentId is { } parent ? label(parent) : null);
        WriteNullable(w, "taskStartMs", row.TaskStartMs);
        w.WriteString("form", FormName(row.Value.Form));
        WriteNullable(w, "score", row.Value.Score);
        WriteNullable(w, "shown", row.Value.Shown);
        w.WriteString("state", row.Value.State is { } state ? TaskStateNames.Name(state) : null);
        if (row.Task is { } task)
        {
            WriteDims(w, task.Dims);
            w.WriteNumber("ig", task.Ig);
            w.WriteNumber("loop", task.Loop);
            w.WriteNumber("osc", task.Osc);
            WriteCounts(w, task.Counts);
            WriteNullable(w, "firstPassMs", task.FirstPassMs);
            WriteNullable(w, "vHeld", task.VHeld);
            if (task.OpenFailure is { } open)
            {
                w.WriteStartObject("openFailure");
                w.WriteString("suite", open.Suite);
                w.WriteString("class", TaskWire.Name(open.Class));
                w.WriteString("key", open.Key);
                w.WriteNumber("errors", open.Errors);
                w.WriteNumber("peak", open.Peak);
                w.WriteBoolean("counted", open.Counted);
                w.WriteBoolean("edited", open.Edited);
                w.WriteEndObject();
            }
            else
            {
                w.WriteNull("openFailure");
            }

            if (task.LastPass is { } pass)
            {
                w.WriteStartObject("lastPass");
                w.WriteString("suite", pass.Suite);
                w.WriteString("class", TaskWire.Name(pass.Class));
                w.WriteString("key", pass.Key);
                w.WriteBoolean("buildOnly", pass.BuildOnly);
                w.WriteEndObject();
            }
            else
            {
                w.WriteNull("lastPass");
            }
        }

        if (row.Predictions is { } p)
        {
            w.WriteStartObject("predictions");
            w.WriteNumber("success", p.Success);
            w.WriteNumber("stall", p.Stall);
            WriteNullable(w, "fix", p.Fix);
            w.WriteNumber("successShown", p.SuccessShown);
            w.WriteNumber("stallShown", p.StallShown);
            WriteNullable(w, "fixShown", p.FixShown);
            w.WriteEndObject();
        }
        else
        {
            w.WriteNull("predictions");
        }

        w.WriteStartObject("models");
        foreach (var (name, model) in Models(row.Models))
        {
            w.WriteStartObject(name);
            w.WriteBoolean("calibrated", model.Calibrated);
            w.WriteNumber("tasks", model.Tasks);
            w.WriteEndObject();
        }

        w.WriteEndObject();
        w.WriteEndObject();
    }

    /// <summary>The <c>taskSets</c> row.</summary>
    public static void WriteSets(Utf8JsonWriter w, TaskSetView view, Func<string, string> label)
    {
        w.WriteStartObject();
        w.WriteStartArray("listed");
        foreach (var id in view.Listed)
        {
            w.WriteStringValue(label(id));
        }

        w.WriteEndArray();
        w.WriteStartArray("sets");
        foreach (var set in view.Sets)
        {
            w.WriteStartObject();
            if (set.Sources is { } sources)
            {
                w.WriteStartArray("sources");
                foreach (var source in sources)
                {
                    w.WriteStringValue(SourceNames.Of(source));
                }

                w.WriteEndArray();
            }
            else
            {
                w.WriteNull("sources");
            }

            WriteSetValue(w, set.Value, label);
            w.WriteEndObject();
        }

        w.WriteEndArray();
        w.WriteStartObject("groups");
        foreach (var group in view.Groups)
        {
            w.WriteStartObject(label(group.Root));
            w.WriteStartArray("members");
            foreach (var member in group.Members)
            {
                w.WriteStringValue(label(member));
            }

            w.WriteEndArray();
            WriteSetValue(w, group.Value, label);
            w.WriteEndObject();
        }

        w.WriteEndObject();
        w.WriteEndObject();
    }

    /// <summary>A <c>record</c> row (<c>RECORD_FIELDS</c>).</summary>
    public static void WriteRecord(Utf8JsonWriter w, TaskRecord r, Func<string, string> label)
    {
        w.WriteStartObject();
        w.WriteString("agentId", label(r.AgentId));
        w.WriteNumber("ordinal", r.Ordinal);
        w.WriteString("source", SourceNames.Of(r.Source));
        w.WriteString("model", r.Model);
        w.WriteString("effort", r.Effort);
        w.WriteNumber("startMs", r.StartMs);
        w.WriteNumber("endMs", r.EndMs);
        w.WriteString("day", r.Day);
        w.WriteBoolean("complete", r.Complete);
        w.WriteString("state", TaskStateNames.Name(r.State));
        WriteNullable(w, "score", r.Score);
        WriteDims(w, r.Dims);
        WriteCounts(w, r.Counts);
        WriteNullable(w, "firstPassMs", r.FirstPassMs);
        w.WriteBoolean("success", r.Success);
        w.WriteBoolean("stalled", r.Stalled);
        WriteRows(w, "snapshots", r.Snapshots, 0, 8);
        WriteRows(w, "fixRows", r.FixRows, 5);
        w.WriteEndObject();
    }

    /// <summary>A source's three models (<c>calibrate</c>).</summary>
    public static void WriteModels(Utf8JsonWriter w, TaskModels models)
    {
        w.WriteStartObject();
        foreach (var (name, model) in Models(models))
        {
            w.WriteStartObject(name);
            w.WriteStartArray("coefficients");
            foreach (var c in model.Coefficients)
            {
                w.WriteNumberValue(c);
            }

            w.WriteEndArray();
            w.WriteBoolean("calibrated", model.Calibrated);
            w.WriteNumber("tasks", model.Tasks);
            w.WriteEndObject();
        }

        w.WriteEndObject();
    }

    /// <summary>The comparison's <c>bySource</c> and <c>byModel</c> members (inside an object the caller opened).</summary>
    public static void WriteComparison(Utf8JsonWriter w, TaskComparisonResult comparison)
    {
        w.WriteStartArray("bySource");
        foreach (var row in comparison.BySource)
        {
            w.WriteStartObject();
            w.WriteString("source", SourceNames.Of(row.Source));
            WriteAggregate(w, row);
            w.WriteEndObject();
        }

        w.WriteEndArray();
        w.WriteStartArray("byModel");
        foreach (var row in comparison.ByModel)
        {
            w.WriteStartObject();
            w.WriteString("model", row.Model);
            w.WriteStartArray("sources");
            foreach (var source in row.Sources)
            {
                w.WriteStringValue(SourceNames.Of(source));
            }

            w.WriteEndArray();
            WriteAggregate(w, row);
            w.WriteEndObject();
        }

        w.WriteEndArray();
    }

    public static string PhaseName(AgentPhase phase) => phase switch
    {
        AgentPhase.Generating => "generating",
        AgentPhase.Tools => "tools",
        _ => "idle",
    };

    public static string FormName(TaskValueForm form) => form switch
    {
        TaskValueForm.Live => "live",
        TaskValueForm.Resting => "resting",
        _ => "none",
    };

    private static void WriteAggregate(Utf8JsonWriter w, TaskAggregate a)
    {
        w.WriteNumber("tasks", a.Tasks);
        w.WriteNumber("scoredTasks", a.ScoredTasks);
        WriteNullable(w, "medianScore", a.MedianScore);
        WriteNullable(w, "medianShown", a.MedianShown);
        WriteNullable(w, "lowerQuartile", a.LowerQuartile);
        WriteNullable(w, "upperQuartile", a.UpperQuartile);
        w.WriteStartArray("histogram");
        foreach (var bin in a.Histogram)
        {
            w.WriteNumberValue(bin);
        }

        w.WriteEndArray();
        w.WriteNumber("successes", a.Successes);
        w.WriteNumber("stalls", a.Stalls);
        w.WriteNumber("fixFailures", a.FixFailures);
        w.WriteNumber("fixAttempts", a.FixAttempts);
        w.WriteNumber("recovered", a.Recovered);
        WriteNullable(w, "successRate", a.SuccessRate);
        WriteNullable(w, "stallRate", a.StallRate);
        WriteNullable(w, "fixFailureRate", a.FixFailureRate);
        WriteNullable(w, "recoverAttempts", a.RecoverAttempts);
        WriteNullable(w, "timeToFirstPassMs", a.TimeToFirstPassMs);
    }

    private static void WriteSetValue(Utf8JsonWriter w, TaskSetValue value, Func<string, string> label)
    {
        w.WriteString("form", FormName(value.Form));
        WriteNullable(w, "score", value.Score);
        WriteNullable(w, "shown", value.Shown);
        w.WriteString("agent", value.AgentId is { } agent ? label(agent) : null);
    }

    private static void WriteCommand(Utf8JsonWriter w, CommandClass cls, string? suite, bool targeted, string key, string? mavenCompileKey)
    {
        w.WriteStartObject();
        w.WriteString("class", TaskWire.Name(cls));
        w.WriteString("suite", suite);
        w.WriteBoolean("targeted", targeted);
        w.WriteString("key", key);
        if (mavenCompileKey is not null) { w.WriteString("mavenCompileKey", mavenCompileKey); }
        w.WriteEndObject();
    }

    private static void WriteDims(Utf8JsonWriter w, TaskDims d)
    {
        w.WriteStartObject("dims");
        w.WriteNumber("V", d.V);
        w.WriteNumber("C", d.C);
        w.WriteNumber("R", d.R);
        w.WriteNumber("E", d.E);
        w.WriteNumber("K", d.K);
        w.WriteEndObject();
    }

    private static void WriteCounts(Utf8JsonWriter w, TaskCounts c)
    {
        w.WriteStartObject("counts");
        w.WriteNumber("actions", c.Actions);
        w.WriteNumber("counted", c.Counted);
        w.WriteNumber("edits", c.Edits);
        w.WriteNumber("oscillations", c.Oscillations);
        w.WriteNumber("loopActions", c.LoopActions);
        w.WriteNumber("runs", c.Runs);
        w.WriteNumber("failingRuns", c.FailingRuns);
        w.WriteNumber("episodes", c.Episodes);
        w.WriteNumber("recovered", c.Recovered);
        w.WriteNumber("recoverAttempts", c.RecoverAttempts);
        w.WriteNumber("fixAttempts", c.FixAttempts);
        w.WriteNumber("fixFailures", c.FixFailures);
        w.WriteEndObject();
    }

    /// <summary>Number rows; the columns listed are integers (the snapshot's <c>n</c> and labels, the fix label).</summary>
    private static void WriteRows(Utf8JsonWriter w, string name, IReadOnlyList<double[]> rows, params int[] integers)
    {
        w.WriteStartArray(name);
        foreach (var row in rows)
        {
            w.WriteStartArray();
            for (var i = 0; i < row.Length; i++)
            {
                if (Array.IndexOf(integers, i) >= 0)
                {
                    w.WriteNumberValue((long)row[i]);
                }
                else
                {
                    w.WriteNumberValue(row[i]);
                }
            }

            w.WriteEndArray();
        }

        w.WriteEndArray();
    }

    private static IEnumerable<(string Name, TaskModel Model)> Models(TaskModels models) =>
        [("success", models.Success), ("stall", models.Stall), ("fix", models.Fix)];

    private static void WriteNullable(Utf8JsonWriter w, string name, long? value)
    {
        if (value is { } v)
        {
            w.WriteNumber(name, v);
        }
        else
        {
            w.WriteNull(name);
        }
    }

    private static void WriteNullable(Utf8JsonWriter w, string name, int? value) => WriteNullable(w, name, (long?)value);

    private static void WriteNullable(Utf8JsonWriter w, string name, double? value)
    {
        if (value is { } v)
        {
            w.WriteNumber(name, v);
        }
        else
        {
            w.WriteNull(name);
        }
    }

    private static void WriteNullable(Utf8JsonWriter w, string name, bool? value)
    {
        if (value is { } v)
        {
            w.WriteBoolean(name, v);
        }
        else
        {
            w.WriteNull(name);
        }
    }
}
