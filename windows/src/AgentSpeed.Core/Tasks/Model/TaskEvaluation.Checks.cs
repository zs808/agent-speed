using AgentSpeed.Core.TaskParsers;

namespace AgentSpeed.Core.Tasks;

/// <summary>An episode of a check (METRICS 6.9 <i>Episodes</i>): its fails in order, and whether a pass closed it.</summary>
internal sealed class TaskEpisode(int checkNo)
{
    public int CheckNo { get; } = checkNo;

    public List<TaskAct> Fails { get; } = [];

    public bool Recovered { get; set; }
}

/// <summary>Runs, checks, sequences, episodes, blocks and the state (the reference's <c>_runs</c>).</summary>
public sealed partial class TaskEvaluation
{
    private int _runCount;

    /// <summary>Per check (in order of first run): its sequence.</summary>
    private List<TaskAct>[] _seq = [];

    private readonly List<TaskEpisode> _episodes = [];

    /// <summary>Per run ordinal, for a failing run: the next element of its check's sequence, or null.</summary>
    private TaskAct?[] _nextInSeq = [];

    /// <summary>The current block (runs after the last effective edit) as a run-ordinal range.</summary>
    private int _currentStart;

    /// <summary>The latest non-empty block as a run-ordinal range ending at <see cref="_runCount"/>.</summary>
    private int _latestStart;

    private TaskAct Run(int ordinal) => _source.Runs[ordinal];

    private bool HasCurrentBlock => _currentStart < _runCount;

    private void Analyze()
    {
        var runs = _source.Runs;
        var runCount = 0;
        var checkCount = 0;
        while (runCount < runs.Count && runs[runCount].Index < _length)
        {
            checkCount = Math.Max(checkCount, runs[runCount].CheckNo + 1);
            runCount++;
        }

        _runCount = runCount;
        var own = new List<TaskAct>[checkCount];
        for (var r = 0; r < runCount; r++)
        {
            (own[runs[r].CheckNo] ??= []).Add(runs[r]);
        }

        _seq = new List<TaskAct>[checkCount];
        for (var c = 0; c < checkCount; c++)
        {
            _seq[c] = Sequence(c, own[c]);
        }

        _nextInSeq = new TaskAct?[runCount];
        for (var c = 0; c < checkCount; c++)
        {
            Episodes(c, _seq[c]);
        }

        _currentStart = runCount;
        while (_currentStart > 0 && runs[_currentStart - 1].Block == Edits)
        {
            _currentStart--;
        }

        _latestStart = runCount;
        if (runCount > 0)
        {
            var latest = runs[runCount - 1].Block;
            while (_latestStart > 0 && runs[_latestStart - 1].Block == latest)
            {
                _latestStart--;
            }
        }

        if (HasCurrentBlock)
        {
            State = BlockFails(_currentStart) ? TaskState.Failing : TaskState.Passing;
        }
        else if (runCount > 0)
        {
            State = BlockFails(_latestStart) ? TaskState.Failing : TaskState.Pending;
        }
        else
        {
            State = Edits > 0 ? TaskState.Unverified : TaskState.None;
        }
    }

    /// <summary>
    /// A check's sequence: its own runs, and for a targeted check whose suite is known every passing non-targeted run of
    /// that suite after its own first run, in order.
    /// </summary>
    private List<TaskAct> Sequence(int checkNo, List<TaskAct> own)
    {
        var first = own[0];
        List<TaskAct>? covered = null;
        if (!(first.Suite == "mvn compile" && first.Command?.MavenCompileKey is not null) && first.Targeted && first.Suite is { } suite
            && _source.FullPasses.TryGetValue(suite, out var passes))
        {
            foreach (var pass in passes)
            {
                if (pass.RunOrdinal > first.RunOrdinal && pass.RunOrdinal < _runCount && pass.CheckNo != checkNo)
                    (covered ??= []).Add(pass);
            }
        }
        if (first.Suite == "mvn compile" && first.Command?.MavenCompileKey is { } compileKey)
        {
            for (var r = first.RunOrdinal + 1; r < _runCount; r++)
            {
                var run = Run(r);
                if (run.Result == RunResult.Pass && run.Command?.MavenCompileKey == compileKey && run.CheckNo != checkNo
                    && run.Outcome?.T is { } completed
                    && own.Where(p => p.Index < run.Index && p.Result == RunResult.Fail)
                        .All(p => p.Outcome?.T is { } failedAt && failedAt <= completed))
                    (covered ??= []).Add(run);
            }
        }
        if (covered is null) return own;

        var merged = new List<TaskAct>(own.Count + covered.Count);
        int i = 0, j = 0;
        while (i < own.Count || j < covered.Count)
        {
            merged.Add(j >= covered.Count || (i < own.Count && own[i].Index < covered[j].Index) ? own[i++] : covered[j++]);
        }

        return merged;
    }

    private void Episodes(int checkNo, List<TaskAct> seq)
    {
        TaskEpisode? current = null;
        for (var i = 0; i < seq.Count; i++)
        {
            var run = seq[i];
            if (run.CheckNo == checkNo && run.Result == RunResult.Fail)
            {
                _nextInSeq[run.RunOrdinal] = i + 1 < seq.Count ? seq[i + 1] : null;
            }

            if (run.Result == RunResult.Fail)
            {
                if (current is null)
                {
                    current = new TaskEpisode(checkNo);
                    _episodes.Add(current);
                }

                current.Fails.Add(run);
            }
            else if (current is not null)
            {
                current.Recovered = true;
                current = null;
            }
        }
    }

    /// <summary>
    /// <c>_block_fails</c> for the block of runs <c>[start, _runCount)</c>: some check with a run in it has a fail as the
    /// last of its sequence's elements in the block.
    /// </summary>
    private bool BlockFails(int start)
    {
        var block = Run(start).Block;
        for (var r = start; r < _runCount; r++)
        {
            var seq = _seq[Run(r).CheckNo];
            for (var k = seq.Count - 1; k >= 0; k--)
            {
                if (seq[k].Block == block)
                {
                    if (seq[k].Result == RunResult.Fail)
                    {
                        return true;
                    }

                    break;
                }
            }
        }

        return false;
    }

    /// <summary><c>open_failure</c>: the newest failing run that is still the last element of its check's sequence.</summary>
    internal TaskAct? OpenFailureRun()
    {
        TaskAct? newest = null;
        for (var c = 0; c < _seq.Length; c++)
        {
            var last = _seq[c][^1];
            if (last.Result == RunResult.Fail && last.CheckNo == c && (newest is null || last.Index > newest.Index))
            {
                newest = last;
            }
        }

        return newest;
    }

    /// <summary>A failing run's position in its episode (1 for the first fail) and its check's peak <c>F</c>.</summary>
    internal (int Attempts, long Peak) EpisodeOf(TaskAct failingRun)
    {
        var attempts = 1;
        foreach (var episode in _episodes)
        {
            if (episode.CheckNo == failingRun.CheckNo && episode.Fails.IndexOf(failingRun) is var position and >= 0)
            {
                attempts = position + 1;
                break;
            }
        }

        return (attempts, Peak(_seq[failingRun.CheckNo]));
    }

    /// <summary>The fix label of a failing run: 1 when the next element of its sequence fails or there is none.</summary>
    internal int FixLabel(TaskAct failingRun) => _nextInSeq[failingRun.RunOrdinal] is { Result: RunResult.Pass } ? 0 : 1;

    /// <summary>The failing runs, in order.</summary>
    internal IEnumerable<TaskAct> FailingRuns()
    {
        for (var r = 0; r < _runCount; r++)
        {
            if (Run(r).Result == RunResult.Fail)
            {
                yield return Run(r);
            }
        }
    }

    private static bool IsPassingTest(TaskAct run) => run.RunClass == CommandClass.Test && run.Result == RunResult.Pass;

    private bool AnyPassingTest(int start)
    {
        for (var r = start; r < _runCount; r++)
        {
            if (IsPassingTest(Run(r)))
            {
                return true;
            }
        }

        return false;
    }
}
