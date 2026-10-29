using System.Text.Json;
using System.Text.Json.Nodes;
using AgentSpeed.Core.TaskParsers;
using AgentSpeed.Core.Tasks;

namespace AgentSpeed.Core.Tests;

/// <summary>
/// The task score and task history (METRICS 6.9, 6.11) against every case of <c>spec/fixtures/task-score-cases.json</c>
/// except <c>thinking</c>, as <c>spec/tools/task_score.py</c> checks them: numbers within 10⁻⁹ (<c>close</c>), objects by
/// key set. One partial file per group of sections.
/// </summary>
public sealed partial class TaskScoreCaseTests
{
    private const double Tolerance = 1e-9;

    private static readonly string[] Sections =
        ["continuations", "promptLines", "segmentation", "tasks", "values", "predictions", "records", "calibration", "comparison"];

    private static readonly Lazy<JsonElement> Root = new(() =>
        JsonDocument.Parse(File.ReadAllBytes(FixtureLocator.PathOf("task-score-cases.json"))).RootElement);

    [Fact]
    public void EverySectionIsChecked()
    {
        foreach (var section in Root.Value.EnumerateObject())
        {
            Assert.True(section.Name is "_comment" or "thinking" || Sections.Contains(section.Name), "unchecked section " + section.Name);
        }

        int[] minimum = [32, 44, 6, 24, 4, 5, 2, 2, 5];
        for (var i = 0; i < Sections.Length; i++)
        {
            Assert.True(Cases(Sections[i]).Length >= minimum[i], Sections[i] + " holds " + Cases(Sections[i]).Length + " cases");
        }
    }

    internal static JsonElement[] Cases(string section) => [.. Root.Value.GetProperty(section).EnumerateArray()];

    internal static JsonElement Case(string section, int index) => Cases(section)[index];

    /// <summary>Case indices with their names, for readable theory rows.</summary>
    internal static TheoryData<int, string> Indices(string section)
    {
        var data = new TheoryData<int, string>();
        var cases = Cases(section);
        for (var i = 0; i < cases.Length; i++)
        {
            var name = cases[i].TryGetProperty("name", out var n) ? n.GetString()! : cases[i].GetProperty("text").GetString()!;
            data.Add(i, name);
        }

        return data;
    }

    internal static JsonElement Member(JsonElement item, string name) => item.TryGetProperty(name, out var value) ? value : default;

    internal static long? OptionalLong(JsonElement value) =>
        value.ValueKind == JsonValueKind.Number && value.TryGetInt64(out var number) ? number : null;

    internal static string? OptionalString(JsonElement value) => value.ValueKind == JsonValueKind.String ? value.GetString() : null;

    internal static SourceKind Source(JsonElement value) =>
        AgentSpeed.Core.History.SourceNames.Parse(value.GetString()) ?? throw new InvalidDataException("unknown source " + value);

    // ---------------------------------------------------------------- the event stream of 6.9

    internal static List<TaskEvent> Events(JsonElement array)
    {
        var events = new List<TaskEvent>();
        foreach (var e in array.EnumerateArray())
        {
            var t = e.GetProperty("t").GetInt64();
            if (e.GetProperty("type").GetString() == "prompt")
            {
                events.Add(new TaskPrompt(t, Member(e, "continuation").ValueKind == JsonValueKind.True));
                continue;
            }

            var command = Member(e, "command") is { ValueKind: JsonValueKind.Object } c
                ? new TaskCommand(ClassOf(c.GetProperty("class").GetString()), OptionalString(Member(c, "suite")),
                    Member(c, "targeted").ValueKind == JsonValueKind.True, c.GetProperty("key").GetString()!, OptionalString(Member(c, "mavenCompileKey")))
                : null;
            List<EditHunk>? hunks = null;
            if (Member(e, "hunks") is { ValueKind: JsonValueKind.Array } list)
            {
                hunks = [.. list.EnumerateArray().Select(h =>
                    new EditHunk(h.GetProperty("file").GetString()!, OptionalString(Member(h, "old")), h.GetProperty("new").GetString()!))];
            }

            var action = new TaskAction(t, KindOf(OptionalString(Member(e, "kind"))), OptionalString(Member(e, "tool")) ?? "",
                OptionalString(Member(e, "digest")) ?? "", command, hunks);
            if (Member(e, "outcome") is { ValueKind: JsonValueKind.Object } outcome)
            {
                var failed = Member(outcome, "failed").ValueKind switch
                {
                    JsonValueKind.True => true,
                    JsonValueKind.False => false,
                    _ => (bool?)null,
                };
                action.Outcome = new TaskOutcome(OptionalLong(Member(outcome, "exitCode")), failed, OptionalLong(Member(outcome, "t")));
            }

            if (Member(e, "failures") is { ValueKind: JsonValueKind.Object } failures)
            {
                var fingerprints = Member(failures, "fingerprints") is { ValueKind: JsonValueKind.Array } f
                    ? f.EnumerateArray().Select(x => x.GetString()!).ToList()
                    : [];
                action.Failures = new FailureReport(fingerprints, OptionalLong(Member(failures, "failedCount")),
                    OptionalLong(Member(failures, "passedCount")), OptionalString(Member(failures, "framework")));
            }

            events.Add(action);
        }

        return events;
    }

    private static ActionKind KindOf(string? name) => name switch
    {
        "read" => ActionKind.Read,
        "search" => ActionKind.Search,
        "edit" => ActionKind.Edit,
        "command" => ActionKind.Command,
        "delegate" => ActionKind.Delegate,
        "web" => ActionKind.Web,
        _ => ActionKind.Other,
    };

    private static CommandClass ClassOf(string? name) => name switch
    {
        "test" => CommandClass.Test,
        "build" => CommandClass.Build,
        "check" => CommandClass.Check,
        _ => CommandClass.Other,
    };

    // ---------------------------------------------------------------- results as the reference writes them

    internal static JsonNode? Number(double? value) => value is { } v ? JsonValue.Create(v) : null;

    internal static JsonNode? Number(long? value) => value is { } v ? JsonValue.Create(v) : null;

    internal static JsonNode? Text(string? value) => value is null ? null : JsonValue.Create(value);

    internal static JsonObject Dims(TaskDims dims) => new()
    {
        ["V"] = dims.V,
        ["C"] = dims.C,
        ["R"] = dims.R,
        ["E"] = dims.E,
        ["K"] = dims.K,
    };

    internal static JsonObject Counts(TaskCounts counts) => new()
    {
        ["actions"] = counts.Actions,
        ["counted"] = counts.Counted,
        ["edits"] = counts.Edits,
        ["oscillations"] = counts.Oscillations,
        ["loopActions"] = counts.LoopActions,
        ["runs"] = counts.Runs,
        ["failingRuns"] = counts.FailingRuns,
        ["episodes"] = counts.Episodes,
        ["recovered"] = counts.Recovered,
        ["recoverAttempts"] = counts.RecoverAttempts,
        ["fixAttempts"] = counts.FixAttempts,
        ["fixFailures"] = counts.FixFailures,
    };

    internal static JsonArray Rows(IReadOnlyList<double[]> rows) => [.. rows.Select(row => (JsonNode)new JsonArray([.. row.Select(v => (JsonNode)v)]))];

    // ---------------------------------------------------------------- close()

    /// <summary>Asserts <c>close(expected, actual)</c>, listing every difference by path.</summary>
    internal static void AssertClose(JsonElement expected, JsonNode? actual, string label, double tolerance = Tolerance)
    {
        var actualElement = actual is null ? JsonDocument.Parse("null").RootElement : JsonDocument.Parse(actual.ToJsonString()).RootElement;
        var differences = new List<string>();
        Close(expected, actualElement, "$", tolerance, differences);
        Assert.True(differences.Count == 0, label + ":\n  " + string.Join("\n  ", differences.Take(20)));
    }

    private static void Close(JsonElement expected, JsonElement actual, string path, double tolerance, List<string> differences)
    {
        if (expected.ValueKind == JsonValueKind.Number && actual.ValueKind == JsonValueKind.Number)
        {
            if (!(Math.Abs(expected.GetDouble() - actual.GetDouble()) <= tolerance))
            {
                differences.Add(path + ": expected " + expected.GetRawText() + ", actual " + actual.GetRawText());
            }

            return;
        }

        if (expected.ValueKind != actual.ValueKind)
        {
            differences.Add(path + ": expected " + expected.GetRawText() + ", actual " + actual.GetRawText());
            return;
        }

        switch (expected.ValueKind)
        {
            case JsonValueKind.Object:
                var keys = expected.EnumerateObject().Select(p => p.Name).ToHashSet(StringComparer.Ordinal);
                var actualKeys = actual.EnumerateObject().Select(p => p.Name).ToHashSet(StringComparer.Ordinal);
                if (!keys.SetEquals(actualKeys))
                {
                    differences.Add(path + ": keys " + string.Join(",", keys.Order(StringComparer.Ordinal)) + " vs " +
                                    string.Join(",", actualKeys.Order(StringComparer.Ordinal)));
                    return;
                }

                foreach (var key in keys)
                {
                    Close(expected.GetProperty(key), actual.GetProperty(key), path + "." + key, tolerance, differences);
                }

                break;
            case JsonValueKind.Array:
                if (expected.GetArrayLength() != actual.GetArrayLength())
                {
                    differences.Add(path + ": length " + expected.GetArrayLength() + " vs " + actual.GetArrayLength());
                    return;
                }

                var index = 0;
                foreach (var (e, a) in expected.EnumerateArray().Zip(actual.EnumerateArray()))
                {
                    Close(e, a, path + "[" + index++ + "]", tolerance, differences);
                }

                break;
            case JsonValueKind.String:
                if (expected.GetString() != actual.GetString())
                {
                    differences.Add(path + ": expected " + expected.GetRawText() + ", actual " + actual.GetRawText());
                }

                break;
        }
    }
}
