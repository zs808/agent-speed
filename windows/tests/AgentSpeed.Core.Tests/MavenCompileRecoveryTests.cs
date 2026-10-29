using System.Text.Json;
using AgentSpeed.Core.TaskParsers;
using AgentSpeed.Core.Tasks;

namespace AgentSpeed.Core.Tests;

public sealed class MavenCompileRecoveryTests
{
    private sealed class Reader : TaskLineReader
    {
        public Reader(string? cwd) : base(SourceKind.Codex) { Cwd = cwd; }
        public TaskAction Action(string command, object? workdir, bool includeWorkdir, long t, bool jsonString = false)
        {
            var input = new Dictionary<string, object?> { ["cmd"] = command };
            if (includeWorkdir) input["workdir"] = workdir;
            var element = jsonString ? JsonSerializer.SerializeToElement(JsonSerializer.Serialize(input)) : JsonSerializer.SerializeToElement(input);
            return Call(t.ToString(), "exec_command", element, t.ToString(), t)!.Action!;
        }
    }

    private static TaskAction Run(Reader reader, string command, string? cwd, long t, bool? failed)
    {
        var action = reader.Action(command, cwd, true, t);
        if (failed is { } value) action.Outcome = new TaskOutcome(value ? 1 : 0, value, t);
        return action;
    }

    private static TaskEvaluation Evaluate(TaskAction first, TaskAction second) => TaskEvaluation.Of([first, second]);

    [Theory]
    [InlineData("mvn -q -pl harvest-start -am -DskipTests compile", "84405fdaec2c24d8")]
    [InlineData("mvn compile", "6b8ba91685e10f62")]
    [InlineData("cd child && mvn test", "a39d70709849696e")]
    public void MavenCompileKeyMatchesReference(string command, string expected)
    {
        Assert.Equal(expected, TaskParser.CommandInfo(command, "/workspace/harvest").MavenCompileKey);
    }

    [Fact]
    public void SameModuleLifecyclePassRecoversCompile()
    {
        var reader = new Reader("/session");
        var failure = Run(reader, "mvn -pl app compile", "/repo", 1, true);
        var pass = Run(reader, "mvn -pl app test", "/repo", 2, false);
        Assert.NotNull(failure.Command!.MavenCompileKey);
        Assert.Equal(failure.Command.MavenCompileKey, pass.Command!.MavenCompileKey);
        Assert.Null(Evaluate(failure, pass).OpenFailure);
    }

    [Theory]
    [InlineData("mvn -pl other test", "/repo")]
    [InlineData("mvn -pl app test", "/other")]
    [InlineData("mvn -pl app test -Dmaven.main.skip=true", "/repo")]
    [InlineData("mvn -pl app surefire:test", "/repo")]
    [InlineData("mvn -pl $MODULE test", "/repo")]
    [InlineData("mvn -pl app test", "relative")]
    public void DifferentOrUnprovenScopeCannotRecover(string command, string cwd)
    {
        var reader = new Reader("/repo");
        var failure = Run(reader, "mvn -pl app compile", "/repo", 1, true);
        var pass = Run(reader, command, cwd, 2, false);
        Assert.NotNull(Evaluate(failure, pass).OpenFailure);
    }

    [Fact]
    public void MissingResultDoesNotRecoverAndCompileDoesNotRecoverTest()
    {
        var reader = new Reader("/repo");
        Assert.NotNull(Evaluate(Run(reader, "mvn compile", "/repo", 1, true),
            Run(reader, "mvn test", "/repo", 2, null)).OpenFailure);
        Assert.NotNull(Evaluate(Run(reader, "mvn test", "/repo", 1, true),
            Run(reader, "mvn compile", "/repo", 2, false)).OpenFailure);
        Assert.Null(Evaluate(Run(reader, "mvn test -Dtest=QueueTest", "/repo", 1, true),
            Run(reader, "mvn test", "/repo", 2, false)).OpenFailure);
    }

    [Theory]
    [InlineData("cd child || mvn test")]
    [InlineData("cd child; mvn test")]
    [InlineData("cd $MODULE/.. && mvn test")]
    [InlineData("cd $(pwd)/.. && mvn test")]
    [InlineData("cd - && mvn test")]
    [InlineData("cd */.. && mvn test")]
    [InlineData("cd ~/child && mvn test")]
    [InlineData("MAVEN_OPTS=-Dmaven.main.skip=true mvn test")]
    [InlineData("env MAVEN_ARGS=-Dmaven.main.skip=true mvn test")]
    [InlineData("mvn test && pytest")]
    [InlineData("mvn test || true")]
    [InlineData("mvn test &")]
    [InlineData("mvn -fn test")]
    [InlineData("mvn -Dmaven.compiler.failOnError=false test")]
    public void UncertainScopeCannotProveCompilation(string command)
    {
        Assert.Null(TaskParser.CommandInfo(command, "/repo").MavenCompileKey);
    }

    [Fact]
    public void JavaHomeAndSuccessfulStaticCdRemainUsable()
    {
        Assert.Equal(TaskParser.CommandInfo("mvn compile", "/repo").MavenCompileKey,
            TaskParser.CommandInfo("JAVA_HOME=/jdk mvn test", "/repo").MavenCompileKey);
        Assert.NotNull(TaskParser.CommandInfo("cd child && mvn test", "/repo").MavenCompileKey);
    }

    [Theory]
    [InlineData(4, 3)]
    [InlineData(null, 3)]
    [InlineData(2, null)]
    public void RecoveryRequiresKnownOrderedCompletionTimes(long? failedAt, long? passedAt)
    {
        var reader = new Reader("/repo");
        var failure = Run(reader, "mvn compile", "/repo", 1, true);
        var pass = Run(reader, "mvn test", "/repo", 2, false);
        failure.Outcome = new TaskOutcome(1, true, failedAt);
        pass.Outcome = new TaskOutcome(0, false, passedAt);
        Assert.NotNull(Evaluate(failure, pass).OpenFailure);
    }

    [Fact]
    public void AllPriorFailuresMustCompleteBeforePassingCandidate()
    {
        var reader = new Reader("/repo");
        var first = Run(reader, "mvn compile", "/repo", 1, true);
        var second = Run(reader, "mvn compile", "/repo", 2, true);
        var pass = Run(reader, "mvn test", "/repo", 3, false);
        first.Outcome = new TaskOutcome(1, true, 10);
        second.Outcome = new TaskOutcome(1, true, 2);
        pass.Outcome = new TaskOutcome(0, false, 3);
        Assert.NotNull(TaskEvaluation.Of([first, second, pass]).OpenFailure);
    }

    [Fact]
    public void InvalidExplicitWorkdirDoesNotFallBackToSession()
    {
        var reader = new Reader("/repo");
        Assert.Null(reader.Action("mvn test", 42, true, 1).Command!.MavenCompileKey);
        Assert.Null(reader.Action("mvn test", "C:", true, 1).Command!.MavenCompileKey);
        Assert.Null(reader.Action("mvn test", "C:relative", true, 1).Command!.MavenCompileKey);
        Assert.NotNull(reader.Action("mvn test", "C:/", true, 1).Command!.MavenCompileKey);
        Assert.NotNull(reader.Action("mvn test", null, false, 2).Command!.MavenCompileKey);
        Assert.Equal(reader.Action("mvn test", "/other", true, 3).Command!.MavenCompileKey,
            reader.Action("mvn test", "/other", true, 4, jsonString: true).Command!.MavenCompileKey);
    }
}
