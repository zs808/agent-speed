import Foundation

/// The task score's rows in `reference.py`'s dump format (METRICS 6.9 *Dumps*, 6.11): compact JSON objects with the
/// reference's field names and order, numbers written to round-trip (the dumps compare them within 1e-9). The probe
/// prints them; the end-to-end fixture test parses them back.
public enum TaskDumpJSON {
    /// One object with members in insertion order.
    struct Object {
        private var parts: [String] = []

        mutating func add(_ key: String, raw: String) {
            parts.append(TaskDumpJSON.quote(key) + ":" + raw)
        }

        mutating func add(_ key: String, _ value: String?) { add(key, raw: value.map(TaskDumpJSON.quote) ?? "null") }
        mutating func add(_ key: String, _ value: Int64?) { add(key, raw: value.map { String($0) } ?? "null") }
        mutating func add(_ key: String, _ value: Int?) { add(key, raw: value.map { String($0) } ?? "null") }
        mutating func add(_ key: String, _ value: Double?) { add(key, raw: value.map(TaskDumpJSON.number) ?? "null") }
        mutating func add(_ key: String, _ value: Bool?) { add(key, raw: value.map { $0 ? "true" : "false" } ?? "null") }

        var text: String { "{" + parts.joined(separator: ",") + "}" }
    }

    static func array(_ items: [String]) -> String { "[" + items.joined(separator: ",") + "]" }

    /// A double as Python's `repr` writes it: the shortest text that reads back (`40.0`, `0.25`, `1e-05`).
    static func number(_ value: Double) -> String {
        value.isFinite ? value.description : "null"
    }

    static func quote(_ text: String) -> String {
        var result = "\""
        for scalar in text.unicodeScalars {
            switch scalar {
            case "\"": result += "\\\""
            case "\\": result += "\\\\"
            case "\n": result += "\\n"
            case "\r": result += "\\r"
            case "\t": result += "\\t"
            case _ where scalar.value < 0x20: result += String(format: "\\u%04x", scalar.value)
            default: result.unicodeScalars.append(scalar)
            }
        }
        return result + "\""
    }

    // MARK: - Events (METRICS 6.9 *Event stream*)

    public static func event(_ event: TaskEvent) -> String {
        var object = Object()
        object.add("t", event.t)
        switch event.body {
        case .prompt(let continuation):
            object.add("type", "prompt")
            object.add("continuation", continuation)
        case .action(let action):
            object.add("type", "action")
            object.add("kind", action.kind.rawValue)
            object.add("tool", action.tool)
            object.add("digest", action.digest.hex)
            if let command = action.command {
                var inner = Object()
                inner.add("class", command.commandClass.rawValue)
                inner.add("suite", command.suite)
                inner.add("targeted", command.targeted)
                inner.add("key", command.key.hex)
                if let key = command.mavenCompileKey { inner.add("mavenCompileKey", key.hex) }
                object.add("command", raw: inner.text)
            }
            if let hunks = action.hunks {
                object.add("hunks", raw: array(hunks.map { hunk in
                    var inner = Object()
                    inner.add("file", hunk.file.hex)
                    inner.add("old", hunk.old?.hex)
                    inner.add("new", hunk.new.hex)
                    return inner.text
                }))
            }
            if let outcome = action.outcome { object.add("outcome", raw: self.outcome(outcome)) }
            if let failures = action.failures { object.add("failures", raw: self.failures(failures)) }
        }
        return object.text
    }

    static func outcome(_ outcome: TaskOutcome) -> String {
        var object = Object()
        object.add("exitCode", outcome.exitCode)
        object.add("failed", outcome.failed)
        object.add("t", outcome.t)
        return object.text
    }

    static func failures(_ failures: TaskRunFailures) -> String {
        var object = Object()
        object.add("fingerprints", raw: array(failures.fingerprints.map { quote($0.hex) }))
        object.add("failedCount", failures.failedCount)
        object.add("passedCount", failures.passedCount)
        object.add("framework", failures.framework)
        return object.text
    }

    // MARK: - Evaluation pieces

    static func dims(_ dims: TaskDimensions) -> String {
        var object = Object()
        object.add("V", dims.v)
        object.add("C", dims.c)
        object.add("R", dims.r)
        object.add("E", dims.e)
        object.add("K", dims.k)
        return object.text
    }

    static func counts(_ counts: TaskCounts) -> String {
        var object = Object()
        for (name, value) in zip(TaskCounts.names, counts.values) { object.add(name, value) }
        return object.text
    }

    static func openFailure(_ facts: TaskOpenFailure?) -> String {
        guard let facts else { return "null" }
        var object = Object()
        object.add("suite", facts.suite)
        object.add("class", facts.commandClass.rawValue)
        object.add("key", facts.key.hex)
        object.add("errors", facts.errors)
        object.add("peak", facts.peak)
        object.add("counted", facts.counted)
        object.add("edited", facts.edited)
        return object.text
    }

    static func lastPass(_ facts: TaskLastPass?) -> String {
        guard let facts else { return "null" }
        var object = Object()
        object.add("suite", facts.suite)
        object.add("class", facts.commandClass.rawValue)
        object.add("key", facts.key.hex)
        object.add("buildOnly", facts.buildOnly)
        return object.text
    }

    static func predictions(_ predictions: TaskPredictions?) -> String {
        guard let predictions else { return "null" }
        var object = Object()
        object.add("success", predictions.success)
        object.add("stall", predictions.stall)
        object.add("fix", predictions.fix)
        object.add("successShown", predictions.successShown)
        object.add("stallShown", predictions.stallShown)
        object.add("fixShown", predictions.fixShown)
        return object.text
    }

    static func flags(_ flags: TaskModelFlags) -> String {
        func flag(_ value: TaskModelFlag) -> String {
            var object = Object()
            object.add("calibrated", value.calibrated)
            object.add("tasks", value.tasks)
            return object.text
        }
        var object = Object()
        object.add("success", raw: flag(flags.success))
        object.add("stall", raw: flag(flags.stall))
        object.add("fix", raw: flag(flags.fix))
        return object.text
    }

    // MARK: - Records, calibration, comparison (6.11)

    /// `RECORD_FIELDS`.
    public static func record(_ record: TaskRecord, agentId: String) -> String {
        var object = Object()
        object.add("agentId", agentId)
        object.add("ordinal", record.ordinal)
        object.add("source", record.source.rawValue)
        object.add("model", record.model)
        object.add("effort", record.effort)
        object.add("startMs", record.startMs)
        object.add("endMs", record.endMs)
        object.add("day", record.day)
        object.add("complete", record.complete)
        object.add("state", record.state.rawValue)
        object.add("score", record.score)
        object.add("dims", raw: dims(record.dims))
        object.add("counts", raw: counts(record.counts))
        object.add("firstPassMs", record.firstPassMs)
        object.add("success", record.success)
        object.add("stalled", record.stalled)
        object.add("snapshots", raw: array(record.snapshots.map { row in
            array(row.values.enumerated().map { index, value in
                index == 0 || index == 8 ? String(Int(value)) : number(value)
            })
        }))
        object.add("fixRows", raw: array(record.fixRows.map { row in
            array(row.values.enumerated().map { index, value in index == 5 ? String(Int(value)) : number(value) })
        }))
        return object.text
    }

    public static func models(_ models: TaskModels) -> String {
        var object = Object()
        for name in TaskModelName.allCases {
            let model = models[name]
            var inner = Object()
            inner.add("coefficients", raw: array(model.coefficients.map(number)))
            inner.add("calibrated", model.calibrated)
            inner.add("tasks", model.tasks)
            object.add(name.rawValue, raw: inner.text)
        }
        return object.text
    }

    public static func comparison(_ comparison: TaskComparison) -> (bySource: String, byModel: String) {
        func row(_ row: TaskComparisonRow) -> String {
            var object = Object()
            if let source = row.source {
                object.add("source", source.rawValue)
            } else {
                object.add("model", row.model ?? "")
                object.add("sources", raw: array(row.sources.map { quote($0.rawValue) }))
            }
            object.add("tasks", row.tasks)
            object.add("scoredTasks", row.scoredTasks)
            object.add("medianScore", row.medianScore)
            object.add("medianShown", row.medianShown)
            object.add("lowerQuartile", row.lowerQuartile)
            object.add("upperQuartile", row.upperQuartile)
            object.add("histogram", raw: array(row.histogram.map { String($0) }))
            object.add("successes", row.successes)
            object.add("stalls", row.stalls)
            object.add("fixFailures", row.fixFailures)
            object.add("fixAttempts", row.fixAttempts)
            object.add("recovered", row.recovered)
            object.add("successRate", row.successRate)
            object.add("stallRate", row.stallRate)
            object.add("fixFailureRate", row.fixFailureRate)
            object.add("recoverAttempts", row.recoverAttempts)
            object.add("timeToFirstPassMs", row.timeToFirstPassMs)
            return object.text
        }
        return (array(comparison.bySource.map(row)), array(comparison.byModel.map(row)))
    }

    // MARK: - Values (6.9 *Values*, the `task` and `taskSets` rows)

    static func task(_ row: TaskValueReport.Row, parentId: String?, label: (String) -> String) -> String {
        var object = Object()
        object.add("agentId", label(row.agent.id))
        object.add("source", row.agent.source.rawValue)
        object.add("phase", row.phase.rawValue)
        object.add("lastLineMs", row.agent.lastLineMs)
        object.add("parentId", parentId.map(label))
        object.add("taskStartMs", row.status.taskStartMs)
        object.add("form", row.value.form.rawValue)
        object.add("score", row.value.score)
        object.add("shown", row.value.shown)
        object.add("state", row.value.state?.rawValue)
        if let result = row.status.result {
            object.add("dims", raw: dims(result.dims))
            object.add("ig", result.ig)
            object.add("loop", result.loop)
            object.add("osc", result.osc)
            object.add("counts", raw: counts(result.counts))
            object.add("firstPassMs", result.firstPassMs)
            object.add("vHeld", result.vHeld)
            object.add("openFailure", raw: openFailure(result.openFailure))
            object.add("lastPass", raw: lastPass(result.lastPass))
        }
        object.add("predictions", raw: predictions(row.status.predictions))
        object.add("models", raw: flags(row.status.models))
        return object.text
    }

    static func setValue(_ value: TaskSetValue, label: (String) -> String) -> [(String, String)] {
        [("form", quote(value.form.rawValue)), ("score", value.score.map(number) ?? "null"),
         ("shown", value.shown.map { String($0) } ?? "null"), ("agent", value.agentId.map { quote(label($0)) } ?? "null")]
    }

    static func taskSets(_ report: TaskValueReport, label: (String) -> String) -> String {
        var object = Object()
        object.add("listed", raw: array(report.listed.map { quote(label($0)) }))
        object.add("sets", raw: array(report.sets.map { set in
            var inner = Object()
            inner.add("sources", raw: set.sources.map { array($0.map { quote($0.rawValue) }) } ?? "null")
            for (key, raw) in setValue(set.value, label: label) { inner.add(key, raw: raw) }
            return inner.text
        }))
        var groups = Object()
        for group in report.groups {
            var inner = Object()
            inner.add("members", raw: array(group.members.map { quote(label($0)) }))
            for (key, raw) in setValue(group.value, label: label) { inner.add(key, raw: raw) }
            groups.add(label(group.root), raw: inner.text)
        }
        object.add("groups", raw: groups.text)
        return object.text
    }
}
