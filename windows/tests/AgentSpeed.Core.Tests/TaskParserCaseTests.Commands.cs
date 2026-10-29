using System.Text.Json;
using System.Text.RegularExpressions;
using AgentSpeed.Core.TaskParsers;

namespace AgentSpeed.Core.Tests;

/// <summary><c>action_kind</c>, <c>command_info</c> and <c>masked_status</c> (TASK-PARSERS 2, 3, 3.10).</summary>
public sealed partial class TaskParserCaseTests
{
    public static TheoryData<string> ActionKindIds() => Ids("actionKind");

    public static TheoryData<string> CommandIds() => Ids("command");

    public static TheoryData<string> MaskedStatusIds() => Ids("maskedStatus");

    [Theory]
    [MemberData(nameof(ActionKindIds))]
    public void ActionKindMatches(string id)
    {
        var item = Case(id);
        var kind = TaskParser.ActionKind(Source(item), Tool(item), Member(item, "input"), Cwd(item));
        Assert.True(item.GetProperty("expect").GetString() == TaskWire.Name(kind), id + ": " + TaskWire.Name(kind));
    }

    [Theory]
    [MemberData(nameof(CommandIds))]
    public void CommandInfoMatches(string id)
    {
        var item = Case(id);
        var expect = item.GetProperty("expect");
        var info = TaskParser.CommandInfo(item.GetProperty("text").GetString()!, Cwd(item));
        var actual = new Dictionary<string, object?>
        {
            ["class"] = TaskWire.Name(info.Class),
            ["suite"] = info.Suite,
            ["targeted"] = info.Targeted,
            ["key"] = info.Key,
            ["masked"] = info.Masked,
            ["background"] = info.Background,
            ["errorFilter"] = info.ErrorFilter,
        };
        var expected = new Dictionary<string, object?>
        {
            ["class"] = expect.GetProperty("class").GetString(),
            ["suite"] = OptionalString(expect.GetProperty("suite")),
            ["targeted"] = expect.GetProperty("targeted").GetBoolean(),
            ["key"] = expect.GetProperty("key").GetString(),
            ["masked"] = expect.GetProperty("masked").GetBoolean(),
            ["background"] = expect.GetProperty("background").GetBoolean(),
            ["errorFilter"] = expect.GetProperty("errorFilter").GetBoolean(),
        };
        if (info.MavenCompileKey is not null) actual["mavenCompileKey"] = info.MavenCompileKey;
        if (expect.TryGetProperty("mavenCompileKey", out var coverage)) expected["mavenCompileKey"] = coverage.GetString();
        Assert.Equal(expected.Count, actual.Count);
        foreach (var (name, value) in expected)
        {
            Assert.True(Equals(value, actual[name]), id + " " + name + ": expected " + value + ", got " + actual[name]);
        }

        Assert.True(info.Suite is null || TaskParser.Suites.Contains(info.Suite), id + " suite outside SUITES: " + info.Suite);
        if (item.TryGetProperty("sameKeyAs", out var same))
        {
            var other = Case(same.GetString()!);
            Assert.Equal(TaskParser.CommandInfo(other.GetProperty("text").GetString()!, Cwd(other)).Key, info.Key);
        }
    }

    [Theory]
    [MemberData(nameof(MaskedStatusIds))]
    public void MaskedStatusMatches(string id)
    {
        var item = Case(id);
        var expect = item.GetProperty("expect");
        var status = TaskParser.MaskedStatus(item.GetProperty("text").GetString()!, item.GetProperty("output").GetString()!, Cwd(item));
        if (expect.ValueKind == JsonValueKind.Null)
        {
            Assert.True(status is null, id + ": expected null, got " + status);
            return;
        }

        Assert.True(status is not null, id + ": expected a status, got null");
        Assert.True(OptionalLong(expect.GetProperty("exitCode")) == status.Value.ExitCode, id + " exitCode: " + status.Value.ExitCode);
        Assert.True(expect.GetProperty("failed").GetBoolean() == status.Value.Failed, id + " failed: " + status.Value.Failed);
    }

    /// <summary>The suite vocabulary equals Appendix A.2's <c>SUITES</c> list, which the reference checks against itself.</summary>
    [Fact]
    public void SuitesEqualTheSpecVocabulary()
    {
        var spec = File.ReadAllText(SpecFile("TASK-PARSERS.md"));
        var line = spec.Split('\n').Single(l => l.StartsWith("- `SUITES`: ", StringComparison.Ordinal));
        var names = Regex.Matches(line["- `SUITES`: ".Length..], "`([^`]+)`").Select(m => m.Groups[1].Value).ToArray();
        Assert.Equal(200, names.Length);
        Assert.Equal(names, TaskParser.Suites);
    }

    private static string SpecFile(string name)
    {
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
        {
            var candidate = Path.Combine(dir.FullName, "spec", name);
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }

        throw new FileNotFoundException("spec/" + name + " not found above " + AppContext.BaseDirectory);
    }

    [Fact]
    public void ParsersAcceptAnyInput()
    {
        using var document = JsonDocument.Parse("""[null, 3, "x", [], {}, {"command": 5}, {"command": ["a", 1]}, "{\"cmd\": \"\\ud800\"}"]""");
        foreach (var source in Enum.GetValues<SourceKind>())
        {
            foreach (var value in document.RootElement.EnumerateArray().Append(default))
            {
                foreach (var tool in new string?[] { null, "Bash", "exec", "exec_command", "shell", "edit", "Execute", "apply_patch", "write_stdin" })
                {
                    _ = TaskParser.ActionKind(source, tool, value, "/");
                    _ = TaskParser.Outcome(source, value, null, tool, value, null);
                    _ = TaskParser.ResultText(source, value);
                    _ = TaskParser.Continuation(source, tool, value);
                    _ = TaskParser.RunningSession(source, value);
                    _ = TaskParser.Notification(source, value);
                    _ = TaskParser.CodeScript(source, tool, value);
                    _ = TaskParser.CodeItems(source, value);
                    _ = TaskParser.EditHunks(source, tool, value, "C:");
                }
            }
        }

        foreach (var text in new[] { "", "'", "\"", "$(", "${", "`", "<<EOF", "a | | b", "((", "))", "\\", "#", "cd; cd -; popd", "\ud800" })
        {
            _ = TaskParser.CommandInfo(text, null);
            _ = TaskParser.MaskedStatus(text, "\n0\n", "/x");
        }
    }
}
