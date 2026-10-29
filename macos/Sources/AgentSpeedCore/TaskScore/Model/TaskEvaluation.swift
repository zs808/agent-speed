import Foundation

/// A check's runs (METRICS 6.9 *Checks*): its own runs and its sequence, as act indices in order.
struct TaskCheck: Sendable {
    var id: TaskCheckID
    var own: [Int]
    var seq: [Int] = []
}

/// An episode of a check (METRICS 6.9 *Episodes*): its failing runs and whether a pass closed it.
struct TaskEpisode: Sendable {
    var check: Int
    var fails: [Int]
    var recovered: Bool
}

/// Everything METRICS 6.9 derives from one task's events at one instant (`Evaluation` of `task_score.py`): the events
/// with `t ≤ now` count, and an outcome whose `t` is after `now` is not known yet. `now` nil: everything.
public struct TaskEvaluation: Sendable {
    static let vTest = 40.0
    static let vBuild = 20.0
    static let cMax = 25.0
    static let rMax = 15.0
    static let eMax = 10.0
    static let kBuild = 4.0
    static let kTest = 4.0
    static let kFull = 2.0
    static let ramp = 8

    var acts: [TaskAct] = []
    /// Act indices of the counted actions (`w > 0`), in order.
    var counted: [Int] = []
    /// Effective edits.
    var edits = 0
    var oscillations = 0
    var loopActions = 0
    /// Act indices of the runs, in order.
    var runs: [Int] = []
    /// In order of their first run (the reference's dict order).
    var checks: [TaskCheck] = []
    var episodes: [TaskEpisode] = []
    /// Ranges of `runs`.
    var currentBlock: Range<Int> = 0..<0
    var latestBlock: Range<Int> = 0..<0

    public private(set) var state = TaskState.none
    /// `IG`.
    public private(set) var ig = 0.0
    /// `L`.
    public private(set) var loop = 0.0
    /// `O`.
    public private(set) var osc = 0.0
    /// 1-based counted indices that end a stall window.
    public private(set) var stallEnds: [Int] = []
    public private(set) var dims = TaskDimensions.zero
    /// nil in state `none`.
    public private(set) var score: Double?

    public init(_ events: some Sequence<TaskEvent>, now: Int64?) {
        var keys = TaskActKeys()
        var files: [TaskDigest: TaskFileEdits] = [:]
        var lastSeen: [Int] = []
        var lastRun: [TaskDigest: (edits: Int, act: Int)] = [:]
        var checkIndex: [TaskCheckID: Int] = [:]
        var offset = -1
        for event in events {
            offset += 1
            guard case .action(let action) = event.body else { continue }
            if let now, event.t > now { continue }
            let key = action.kind == .other ? -1 : keys.id(action.tool, action.digest)
            var act = TaskAct(event: offset, action: action, now: now, key: key)
            let index = acts.count
            act.block = edits
            if act.kind == .edit {
                (act.effective, act.oscillating) = TaskFileEdits.apply(&files, hunks: action.hunks,
                                                                     failed: act.outcomeFailed)
                act.gain = act.effective && !act.oscillating ? 1 : 0
                if act.effective { edits += 1 }
                if act.oscillating { oscillations += 1 }
            } else if let result = act.result, let command = act.command {
                if let previous = lastRun[command.key] {
                    let earlier = acts[previous.act]
                    let same = previous.edits == edits && earlier.result == result
                        && earlier.fingerprints == act.fingerprints && earlier.failedCount == act.failedCount
                    act.gain = same ? 0 : 1
                } else {
                    act.gain = 1
                }
                lastRun[command.key] = (edits, index)
                let id = TaskCheckID(command)
                if let check = checkIndex[id] {
                    act.check = check
                    checks[check].own.append(index)
                } else {
                    act.check = checks.count
                    checkIndex[id] = checks.count
                    checks.append(TaskCheck(id: id, own: [index]))
                }
                runs.append(index)
            } else if act.weight > 0 {
                if key >= lastSeen.count { lastSeen.append(contentsOf: repeatElement(-1, count: key + 1 - lastSeen.count)) }
                act.gain = lastSeen[key] != edits ? 1 : 0
                lastSeen[key] = edits
            }
            if act.weight > 0 { counted.append(index) }
            acts.append(act)
        }
        gainsAndLoops()
        sequencesAndEpisodes()
        blocksAndState()
        dimensions()
    }

    // MARK: Information gain, loops, oscillation

    private mutating func gainsAndLoops() {
        osc = edits > 0 ? taskDivide(oscillations, edits) : 0.0
        var total = 0
        var weighted = 0
        var weights: [Int] = []
        var gains: [Int] = []
        var keys: [Int] = []
        weights.reserveCapacity(counted.count)
        gains.reserveCapacity(counted.count)
        keys.reserveCapacity(counted.count)
        for index in counted {
            let act = acts[index]
            total += act.weight
            weighted += act.weight * act.gain
            weights.append(act.weight)
            gains.append(act.gain)
            keys.append(act.key)
        }
        ig = total > 0 ? taskDivide(weighted, total) : 0.0
        loopActions = TaskLoops.marks(keys).reduce(0) { $0 + ($1 ? 1 : 0) }
        loop = counted.isEmpty ? 0.0 : taskDivide(loopActions, counted.count)
        stallEnds = TaskLoops.stallEnds(weights: weights, gains: gains)
    }

    // MARK: Runs, checks, episodes, blocks, state

    private mutating func sequencesAndEpisodes() {
        var passesBySuite: [TaskOrdinalKey: [Int]]?
        for check in checks.indices {
            let own = checks[check].own
            let first = acts[own[0]]
            var added: [Int] = []
            if first.targeted, let suite = first.suite, !(first.suite == "mvn compile" && first.command?.mavenCompileKey != nil) {
                if passesBySuite == nil { passesBySuite = fullPassesBySuite() }
                let candidates = passesBySuite?[TaskOrdinalKey(suite)] ?? []
                let start = Self.firstPosition(after: own[0], in: candidates)
                added += candidates[start...].filter { acts[$0].check != check }
            }
            if first.suite == "mvn compile", let key = first.command?.mavenCompileKey {
                added += runs.filter { candidate in
                    guard candidate > own[0], acts[candidate].result == .pass,
                          acts[candidate].command?.mavenCompileKey == key, acts[candidate].check != check,
                          let time = acts[candidate].outcomeT else { return false }
                    return own.filter { $0 < candidate && acts[$0].result == .fail }
                        .allSatisfy { acts[$0].outcomeT.map { $0 <= time } ?? false }
                }
            }
            checks[check].seq = added.isEmpty ? own : Self.merged(own, added.sorted())
        }
        for check in checks.indices {
            let seq = checks[check].seq
            var current: Int?
            for (position, run) in seq.enumerated() {
                if acts[run].result == .fail {
                    if acts[run].check == check { acts[run].next = position + 1 < seq.count ? seq[position + 1] : nil }
                    if current == nil {
                        current = episodes.count
                        episodes.append(TaskEpisode(check: check, fails: [], recovered: false))
                    }
                    episodes[current!].fails.append(run)
                } else if let open = current {
                    episodes[open].recovered = true
                    current = nil
                }
            }
        }
    }

    /// Passing non-targeted runs of a known suite, by suite, in order.
    private func fullPassesBySuite() -> [TaskOrdinalKey: [Int]] {
        var found: [TaskOrdinalKey: [Int]] = [:]
        for run in runs {
            let act = acts[run]
            guard act.result == .pass, !act.targeted, let suite = act.suite else { continue }
            found[TaskOrdinalKey(suite), default: []].append(run)
        }
        return found
    }

    /// The position of the first element above `value` in an ascending array.
    private static func firstPosition(after value: Int, in sorted: [Int]) -> Int {
        var low = 0
        var high = sorted.count
        while low < high {
            let middle = (low + high) / 2
            if sorted[middle] > value { high = middle } else { low = middle + 1 }
        }
        return low
    }

    private static func merged(_ lhs: [Int], _ rhs: [Int]) -> [Int] {
        var out: [Int] = []
        out.reserveCapacity(lhs.count + rhs.count)
        var i = 0
        var j = 0
        while i < lhs.count || j < rhs.count {
            if j == rhs.count || (i < lhs.count && lhs[i] < rhs[j]) {
                out.append(lhs[i])
                i += 1
            } else {
                out.append(rhs[j])
                j += 1
            }
        }
        return out
    }

    private mutating func blocksAndState() {
        guard let last = runs.last else {
            state = edits > 0 ? .unverified : .none
            return
        }
        let latest = acts[last].block
        var start = runs.count - 1
        while start > 0, acts[runs[start - 1]].block == latest { start -= 1 }
        latestBlock = start..<runs.count
        if latest == edits {
            currentBlock = latestBlock
            state = blockFails(currentBlock) ? .failing : .passing
        } else {
            state = blockFails(latestBlock) ? .failing : .pending
        }
    }

    /// Some check with a run in the block has a fail as the last of its sequence's elements in the block.
    private func blockFails(_ block: Range<Int>) -> Bool {
        guard !block.isEmpty else { return false }
        let number = acts[runs[block.lowerBound]].block
        var seen = Set<Int>()
        for position in block {
            let check = acts[runs[position]].check
            guard seen.insert(check).inserted else { continue }
            for run in checks[check].seq.reversed() {
                let block = acts[run].block
                if block > number { continue }
                if block == number && acts[run].result == .fail { return true }
                break
            }
        }
        return false
    }

    // MARK: Dimensions

    private mutating func dimensions() {
        let block = currentBlock.map { acts[runs[$0]] }
        let passingTests = block.contains { $0.runClass == .test && $0.result == .pass }
        let v = state == .passing ? (passingTests ? Self.vTest : Self.vBuild) : 0.0
        let c = convergence()
        var r = 0.0
        if episodes.isEmpty {
            r = runs.contains { acts[$0].result == .pass } ? Self.rMax : 0.0
        } else {
            var sum = 0.0
            for episode in episodes { sum += episode.recovered ? taskDivide(2, episode.fails.count + 1) : 0.0 }
            r = Self.rMax * sum / Double(episodes.count)
        }
        let n = counted.count
        var e = 0.0
        if n > 0 {
            let ramp = taskDivide(n, Self.ramp)
            e = Self.eMax * (ramp < 1.0 ? ramp : 1.0) * ig * (1 - loop) * (1 - osc)
        }
        var k = 0.0
        if passingTests || block.contains(where: { $0.runClass == .build || $0.runClass == .check }) { k += Self.kBuild }
        if block.contains(where: { $0.runClass == .test }) { k += Self.kTest }
        if let lastFullPass = block.lastIndex(where: { $0.runClass == .test && $0.result == .pass && !$0.targeted }),
           !block[(lastFullPass + 1)...].contains(where: { $0.runClass == .test && $0.result == .fail }) {
            k += Self.kFull
        }
        dims = TaskDimensions(v: v, c: c, r: r, e: e, k: k)
        score = state == .none ? nil : v + c + r + e + k
    }

    /// `C`: per check with a positive peak, `peak · max(0, 1 − (F_last + N_new) / peak)`, weighed by the peaks.
    private func convergence() -> Double {
        guard !runs.isEmpty else { return 0.0 }
        var weighted = 0.0
        var total = 0.0
        for check in checks {
            let seq = check.seq
            var peak = 0
            var firstPeak = 0
            for (position, run) in seq.enumerated() where acts[run].errors > peak {
                peak = acts[run].errors
                firstPeak = position
            }
            if peak == 0 { continue }
            let last = acts[seq[seq.count - 1]]
            var new = 0
            if last.result == .fail {
                var base = Set<TaskDigest>()
                for run in seq[...firstPeak] { base.formUnion(acts[run].fingerprints) }
                new = last.fingerprints.reduce(0) { $0 + (base.contains($1) ? 0 : 1) }
            }
            let share = 1 - taskDivide(last.errors + new, peak)
            weighted += Double(peak) * (share > 0.0 ? share : 0.0)
            total += Double(peak)
        }
        return total == 0 ? Self.cMax : Self.cMax * weighted / total
    }
}
