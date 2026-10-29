import Testing
@testable import AgentSpeedCore

@Suite("Maven compile recovery")
struct MavenCompileRecoveryTests {
    private func command(_ text: String, cwd: String? = "/repo") -> TaskCommand {
        let info = TaskParsers.commandInfo(text, cwd: cwd)
        return TaskCommand(commandClass: info.commandClass, suite: info.suite, targeted: info.targeted,
                           key: TaskDigest(hex: info.key)!, mavenCompileKey: info.mavenCompileKey.flatMap(TaskDigest.init(hex:)))
    }

    private func run(_ command: TaskCommand, _ failed: Bool?, t: Int64) -> TaskEvent {
        .action(t, TaskAction(kind: .command, tool: "exec_command", digest: command.key, command: command,
                              outcome: TaskOutcome(exitCode: nil, failed: failed, t: t)))
    }

    private func evaluation(_ first: TaskCommand, _ second: TaskCommand, failed: Bool? = false) -> TaskEvaluation {
        let edit = TaskEvent.action(2, TaskAction(kind: .edit, tool: "apply_patch", digest: TaskDigest(value: 42)))
        return TaskEvaluation([run(first, true, t: 1), edit, run(second, failed, t: 3)], now: nil)
    }

    @Test func lifecyclePassRecoversCompileOnlyWithinScope() {
        let compile = command("mvn -pl module-a compile")
        let test = command("mvn -pl module-a test -Dtest=Foo -DskipTests")
        #expect(compile.mavenCompileKey != nil)
        #expect(compile.mavenCompileKey == test.mavenCompileKey)
        #expect(evaluation(compile, test).episodes.first?.recovered == true)
        for different in [command("mvn -pl module-b test"), command("mvn -pl module-a test", cwd: "/other"),
                          command("mvn -pl module-a -Pprod test"), command("mvn -pl module-a test -Dmaven.compiler.skip=true"),
                          command("mvn -pl module-a surefire:test")] {
            #expect(evaluation(compile, different).episodes.first?.recovered == false)
        }
        #expect(evaluation(compile, test, failed: nil).episodes.first?.recovered == false)
    }

    @Test func testFailuresAreNotClosedByCompileAndExistingFullTestRecoveryRemains() {
        let targeted = command("mvn test -Dtest=Foo")
        #expect(evaluation(targeted, command("mvn compile")).episodes.first?.recovered == false)
        #expect(evaluation(targeted, command("mvn test")).episodes.first?.recovered == true)
    }

    @Test func conservativeCommandCoverage() {
        let good = command("mvn clean compile -pl a -DskipTests").mavenCompileKey
        #expect(good != nil)
        #expect(good == command("mvn -pl a verify -Dtest=Foo").mavenCompileKey)
        for text in ["mvn compile test", "mvn compiler:compile", "mvn compile -fn", "mvn compile -Dfoo.skip=true",
                     "mvn compile && mvn test", "mvn compile || true", "mvn compile -pl $MODULE", "MAVEN_OPTS=foo mvn compile", "cd child; mvn compile", "cd $DIR && mvn compile", "cd ~ && mvn compile", "popd && mvn compile", "cd */.. && mvn compile", "cd ~/child && mvn compile"] {
            #expect(command(text).mavenCompileKey == nil)
        }
        #expect(command("mvn compile", cwd: nil).mavenCompileKey == nil)
        #expect(command("mvn compile", cwd: "relative").mavenCompileKey == nil)
    }

    @Test func asynchronousCompletionDoesNotCloseFailure() {
        let first = command("mvn compile")
        let second = command("mvn test")
        let failure = TaskEvent.action(1, TaskAction(kind: .command, tool: "exec_command", digest: first.key, command: first,
                                                     outcome: TaskOutcome(exitCode: nil, failed: true, t: 9)))
        let earlyPass = TaskEvent.action(2, TaskAction(kind: .command, tool: "exec_command", digest: second.key, command: second,
                                                       outcome: TaskOutcome(exitCode: nil, failed: false, t: 3)))
        let unknownTimePass = TaskEvent.action(3, TaskAction(kind: .command, tool: "exec_command", digest: second.key, command: second,
                                                             outcome: TaskOutcome(exitCode: nil, failed: false, t: nil)))
        #expect(TaskEvaluation([failure, earlyPass], now: nil).episodes.first?.recovered == false)
        #expect(TaskEvaluation([failure, unknownTimePass], now: nil).episodes.first?.recovered == false)
        #expect(command("JAVA_HOME=/jdk mvn compile").mavenCompileKey != nil)
        #expect(command("mvn compile", cwd: "C:relative").mavenCompileKey == nil)
        #expect(command("mvn compile", cwd: "C:").mavenCompileKey == nil)
    }

    @Test func pythonGoldenHashes() {
        let cwd = "/workspace/harvest"
        #expect(command("mvn -q -pl harvest-start -am -DskipTests compile", cwd: cwd).mavenCompileKey?.hex == "84405fdaec2c24d8")
        #expect(command("mvn compile", cwd: cwd).mavenCompileKey?.hex == "6b8ba91685e10f62")
        #expect(command("cd child && mvn test", cwd: cwd).mavenCompileKey?.hex == "a39d70709849696e")
    }

    @Test func codexWorkdirOverridesSessionCwd() throws {
        var reader = try #require(TaskLineReader(source: .codex))
        reader.setCwd("/repo")
        func input(_ dir: JSONValue) -> JSONValue { .object(["cmd": .string("mvn compile"), "workdir": dir]) }
        let first = reader.call(id: "a", t: 1, tool: "exec_command", input: input(.string("/a")), digest: TaskDigest(value: 1))
        let second = reader.call(id: "b", t: 2, tool: "exec_command", input: input(.string("/b")), digest: TaskDigest(value: 2))
        let invalid = reader.call(id: "c", t: 3, tool: "exec_command", input: input(.null), digest: TaskDigest(value: 3))
        let one = try #require(first?.action.command)
        let two = try #require(second?.action.command)
        #expect(one.mavenCompileKey != nil && one.mavenCompileKey != two.mavenCompileKey)
        #expect(invalid?.action.command?.mavenCompileKey == nil)
        #expect(evaluation(one, two).episodes.first?.recovered == false)
    }
}
