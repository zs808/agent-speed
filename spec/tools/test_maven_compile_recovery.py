"""Regression coverage for Maven main-compilation evidence (no Maven subprocesses)."""
import json
from types import SimpleNamespace
import unittest

import task_events
import task_parsers as parsers
import task_score

ROOT = '/workspace/harvest'
COMPILE = 'mvn -q -pl harvest-start -am -DskipTests compile > /tmp/compile.log 2>&1'
TEST = ('mvn -q -pl harvest-start -am -Dtest=BlindResampleLifecycleTest,BlindPlanStationTest '
        '-Dsurefire.failIfNoSpecifiedTests=false test > /tmp/test.log 2>&1')


def coverage(command, cwd=ROOT):
    return parsers.command_info(command, cwd).get('mavenCompileKey')


def stream(commands, edit=False):
    c = task_events.Composer(SimpleNamespace(path='fixture.jsonl', source='codex'))
    c.cwd = '/workspace/session'
    for i, (command, workdir, code) in enumerate(commands):
        call = c.action('codex', 100 + i * 100, 'exec_command',
                        json.dumps({'cmd': command, 'workdir': workdir}), parsers.sha16(str(i)))
        if code is not None:
            c.answer('codex', call, json.dumps({'exit_code': code, 'output': ''}), 150 + i * 100)
        if edit and i == 0:
            c.events.append({'t': 175, 'type': 'action', 'kind': 'edit', 'tool': 'apply_patch',
                             'digest': parsers.sha16('edit'),
                             'hunks': [{'file': parsers.sha16('file'), 'old': parsers.sha16('bad'),
                                        'new': parsers.sha16('good')}]})
    return c.events


class MavenCompileRecoveryTests(unittest.TestCase):
    def evaluation(self, later=TEST, cwd=ROOT, outcome=0, edit=False, first=COMPILE):
        return task_score.Evaluation(stream([(first, ROOT, 1), (later, cwd, outcome)], edit))

    def test_real_command_shape_recovers_after_type_fix(self):
        result = self.evaluation(edit=True)
        self.assertEqual(result.state, 'passing')
        self.assertEqual(len(result.episodes), 1)
        self.assertTrue(result.episodes[0][2])
        self.assertIsNone(result.open_failure())
        self.assertEqual(result.dims()['R'], 15)

    def test_same_block_compile_fail_test_pass_recovers(self):
        self.assertEqual(self.evaluation().state, 'passing')

    def test_only_same_scope_lifecycle_evidence_recovers(self):
        for command, cwd in [(TEST.replace('harvest-start', 'farmer-start'), ROOT),
                             (TEST, '/workspace/other'),
                             (TEST + ' -Pother', ROOT),
                             (TEST + ' -f alternate.xml', ROOT),
                             (TEST + ' -rf harvest-start', ROOT),
                             (TEST.replace(' -am ', ' '), ROOT),
                             (TEST + ' -Dcustom.build=true', ROOT)]:
            with self.subTest(command=command, cwd=cwd):
                result = self.evaluation(command, cwd)
                self.assertFalse(result.episodes[0][2])
                self.assertIsNotNone(result.open_failure())

    def test_workdir_override_is_used_even_with_same_session_directory(self):
        events = stream([(COMPILE, ROOT, 1), (TEST, '/workspace/farmer', 0)])
        self.assertNotEqual(events[0]['command']['mavenCompileKey'], events[1]['command']['mavenCompileKey'])

    def test_invalid_explicit_workdir_does_not_fall_back(self):
        for cwd in [None, 42, 'relative', '$PWD']:
            with self.subTest(cwd=cwd):
                events = stream([(TEST, cwd, 0)])
                self.assertNotIn('mavenCompileKey', events[0]['command'])

    def test_unknown_or_failing_outcome_does_not_recover(self):
        for outcome in [None, 1]:
            with self.subTest(outcome=outcome):
                self.assertFalse(self.evaluation(outcome=outcome).episodes[0][2])

    def test_unsafe_commands_have_no_compile_coverage(self):
        for command in ['mvn surefire:test', 'mvn compiler:compile', 'mvn compile test',
                        'mvn test clean', 'mvn test; mvn compile', 'mvn test && pytest',
                        'mvn test || true', 'mvn test &', 'mvn -fn test', 'mvn --help test',
                        'mvn -Dmaven.main.skip=true test', 'mvn -Dmaven.compiler.skip=true test',
                        'mvn -Dmaven.compiler.failOnError=false test', 'mvn -pl "$MODULE" test']:
            with self.subTest(command=command):
                self.assertIsNone(coverage(command))
                self.assertFalse(self.evaluation(command).episodes[0][2])

    def test_pass_completing_before_failed_compile_does_not_recover(self):
        events = stream([(COMPILE, ROOT, 1), (TEST, ROOT, 0)])
        events[0]['outcome']['t'] = 400
        result = task_score.Evaluation(events)
        self.assertFalse(result.episodes[0][2])

    def test_uncertain_cd_or_environment_cannot_prove_compile(self):
        for command in ['cd child || mvn test', 'cd child; mvn test', 'cd $MODULE/.. && mvn test',
                        'cd - && mvn test', 'cd */.. && mvn test', 'cd ~/child && mvn test', 'MAVEN_OPTS=-Dmaven.main.skip=true mvn test',
                        'env MAVEN_ARGS=-Dmaven.main.skip=true mvn test']:
            with self.subTest(command=command):
                self.assertIsNone(coverage(command))
        self.assertEqual(coverage('JAVA_HOME=/jdk mvn test'), coverage('mvn compile'))

    def test_fixed_scope_digests(self):
        self.assertEqual(coverage('mvn -q -pl harvest-start -am -DskipTests compile'), '84405fdaec2c24d8')
        self.assertEqual(coverage('mvn compile'), '6b8ba91685e10f62')
        self.assertEqual(coverage('cd child && mvn test'), 'a39d70709849696e')

    def test_missing_directory_has_no_coverage(self):
        for cwd in [None, 'C:', 'C:relative']:
            self.assertIsNone(coverage(TEST, cwd))

    def test_relative_shell_context_is_part_of_scope(self):
        self.assertNotEqual(coverage('cd one && mvn compile'), coverage('cd two && mvn test'))
        self.assertEqual(coverage('cd one && mvn compile'), coverage('cd one && mvn test'))

    def test_known_later_phases_prove_main_compile(self):
        for phase in ['process-classes', 'test-compile', 'test', 'package', 'integration-test', 'verify', 'install', 'deploy']:
            with self.subTest(phase=phase):
                self.assertEqual(coverage('mvn compile'), coverage('mvn clean ' + phase))

    def test_test_skip_does_not_suppress_main_compile(self):
        self.assertEqual(coverage('mvn -DskipTests compile'), coverage('mvn -DskipTests=false package'))

    def test_compile_success_cannot_recover_test_failure(self):
        result = self.evaluation('mvn -pl harvest-start -am compile', first=TEST)
        self.assertFalse(result.episodes[0][2])
        self.assertIsNotNone(result.open_failure())

    def test_other_unrelated_failure_remains_open(self):
        commands = [(COMPILE, ROOT, 1), ('mvn -pl farmer-start -am compile', ROOT, 1), (TEST, ROOT, 0)]
        result = task_score.Evaluation(stream(commands))
        self.assertEqual(result.state, 'failing')
        self.assertEqual(sum(ep[2] for ep in result.episodes), 1)

    def test_existing_targeted_test_full_test_recovery_is_preserved(self):
        result = task_score.Evaluation(stream([('mvn -Dtest=One test', ROOT, 1), ('mvn test', ROOT, 0)]))
        self.assertEqual(result.state, 'passing')
        self.assertTrue(result.episodes[0][2])

    def test_compile_checks_do_not_use_global_suite_fallback(self):
        result = task_score.Evaluation(stream([('mvn -pl harvest-start compile', ROOT, 1),
                                               ('mvn compile', '/workspace/other', 0)]))
        self.assertEqual(result.state, 'failing')
        self.assertFalse(result.episodes[0][2])


if __name__ == '__main__':
    unittest.main()
