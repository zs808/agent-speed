#!/usr/bin/env python3
"""The task score's event composer (METRICS 6.9 *Composing the stream*): one agent's log lines, after its source parser
(METRICS 4) has read them, become the prompt and action events of METRICS 6.9 through the functions of
spec/TASK-PARSERS.md, with results matched to their calls and continuations stitched back (TASK-PARSERS 4.3).

reference.py feeds every line to `Composer.feed` right after the source's own handler, so copied history and
deduplication are decided exactly as for tokens. Inputs, outputs and prompt texts are read in memory only; the events
hold classes, digests, counts, exit codes and times.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import execution_modes_reference as modes  # noqa: E402
import task_parsers as parsers  # noqa: E402
import task_score as model  # noqa: E402

SCORED = model.SCORED_SOURCES
COPY_MS = 2000
STALE_MS = 300_000
LISTED_MS = 600_000
COMMAND_FIELDS = ("class", "suite", "targeted", "key")
PI_FAMILY = ("omp", "pi", "openclaw")


def engine():
    import reference
    return reference


def family(source):
    return "claude" if source in ("claudeCode", "claudeDesktop") else source


def blank(value):
    return not isinstance(value, str) or not value.strip()


# ----------------------------------------------------------------------------- raw input slices (METRICS 6.10 digest)

_DECODER = json.JSONDecoder()
_WS = " \t\n\r"


def _skip(s, i):
    while i < len(s) and s[i] in _WS:
        i += 1
    return i


def _members(s, i):
    """(key, start, end) of the members of the JSON object that starts at s[i]."""
    out, i = [], _skip(s, i + 1)
    if s[i] == "}":
        return out
    while True:
        key, i = json.decoder.scanstring(s, i + 1)
        i = _skip(s, _skip(s, i) + 1)
        end = _DECODER.raw_decode(s, i)[1]
        out.append((key, i, end))
        i = _skip(s, end)
        if s[i] != ",":
            return out
        i = _skip(s, i + 1)


def _elements(s, i):
    """(start, end) of the elements of the JSON array that starts at s[i]."""
    out, i = [], _skip(s, i + 1)
    if s[i] == "]":
        return out
    while True:
        end = _DECODER.raw_decode(s, i)[1]
        out.append((i, end))
        i = _skip(s, end)
        if s[i] != ",":
            return out
        i = _skip(s, i + 1)


def _last(s, i, name):
    span = None
    for key, start, end in _members(s, i):
        if key == name:
            span = (start, end)
    return span


def raw_inputs(text, field):
    """The raw JSON text of `message.content[k][field]` for every content index k whose block holds that field (a repeated
    member takes its last value, as json.loads)."""
    out = {}
    s = text if isinstance(text, str) else text.decode("utf-8", "surrogatepass")
    start = _skip(s, 0)
    message = _last(s, start, "message")
    if message is None or s[message[0]] != "{":
        return out
    content = _last(s, message[0], "content")
    if content is None or s[content[0]] != "[":
        return out
    for k, (a, _) in enumerate(_elements(s, content[0])):
        if s[a] == "{":
            span = _last(s, a, field)
            if span is not None:
                out[k] = s[span[0]:span[1]]
    return out


def digest(value, raw):
    """METRICS 6.10: a string input's text; an object's or array's JSON text exactly as the line stores it (`raw`);
    an absent or null input hashes as empty text."""
    if value is None:
        return parsers.EMPTY
    if isinstance(value, str):
        return parsers.sha16(value)
    text = raw if raw is not None else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


# ----------------------------------------------------------------------------- the composer

class Call:
    """A call waiting for its result: the input is held in memory only until then."""
    __slots__ = ("event", "tool", "value", "cwd", "info", "continues", "answered")

    def __init__(self, event, tool, value, cwd, info, continues):
        self.event, self.tool, self.value, self.cwd, self.info = event, tool, value, cwd, info
        self.continues, self.answered = continues, False


class Running:
    """A command whose result left a session running (TASK-PARSERS 4.3): its output so far, until an exit code."""
    __slots__ = ("event", "info", "cwd", "masked", "texts")

    def __init__(self, event, info, cwd, masked, text):
        self.event, self.info, self.cwd, self.masked, self.texts = event, info, cwd, masked, [text]


class Composer:
    """The event stream of one agent (a log file, or an OpenClaw transcript window)."""

    def __init__(self, state):
        self.path = state.path
        self.source = family(state.source)
        self.cwd = None
        self.events = []
        self.models = []
        self.calls = {}
        self.sessions = {}
        self.pending = None
        self.header_seen = False
        self.stats = {"lines": 0, "prompts": 0, "withdrawn": 0, "calls": 0, "results": 0, "orphanResults": 0,
                      "stitched": 0, "continuationResults": 0}

    # -- entry point
    def feed(self, state, obj, dedupe, raw):
        fam = family(state.source)
        if fam == "claude":
            t = self.claude(state, obj, dedupe, raw)
        elif fam == "codex":
            t = self.codex(state, obj)
        elif fam in PI_FAMILY:
            t = self.pi_family(state, obj, dedupe, raw)
        elif fam == "droid":
            t = self.droid(state, obj, dedupe, raw)
        else:
            return
        if t is not None:
            self.stats["lines"] += 1
            self.track(state, t)

    # -- Claude Code and Claude Desktop
    def claude(self, state, obj, dedupe, raw):
        ref = engine()
        t = ref.parse_ts(obj.get("timestamp"))
        if t is None:
            return None
        if not blank(obj.get("cwd")):
            self.cwd = obj["cwd"]
        kind = obj.get("type")
        if kind in ("user", "attachment"):
            found = model.prompt_line(state.source, obj)
            if found is not None:
                self.line_kind(found["kind"], found.get("t", t), found.get("continuation"))
            notice = parsers.notification(state.source, obj) if kind == "attachment" else None
            if notice is not None:
                queued = ref.parse_ts(obj["attachment"].get("timestamp"))
                code = notice["exitCode"]
                self.finish(notice["session"], {"exitCode": code, "failed": code != 0}, None,
                            queued if queued is not None else t)
            message = obj.get("message") if kind == "user" and isinstance(obj.get("message"), dict) else {}
            content = message.get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self.result(state.source, block.get("tool_use_id"), block, t)
        elif kind == "assistant":
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            if message.get("model") == "<synthetic>":
                return t
            key = message.get("id") or obj.get("requestId") or obj.get("uuid")
            duplicate = isinstance(key, str) and key != "" and dedupe.owner(key) not in (None, self.path)
            self.assistant(duplicate)
            if not duplicate:
                self.calls_in(state.source, message.get("content"), "tool_use", "input", t, raw)
        return t

    # -- Codex
    def codex(self, state, obj):
        ref = engine()
        t = ref.parse_ts(obj.get("timestamp"))
        if t is None:
            return None
        kind = obj.get("type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if kind == "session_meta":
            if self.header_seen:
                return None
            self.header_seen = True
            if not blank(payload.get("cwd")):
                self.cwd = payload["cwd"]
            return t
        if state.file_created is not None and t < state.file_created - COPY_MS:
            return None
        line_ordinal = ref.ordinal(obj.get("ordinal"))
        if state.start_ordinal is not None and line_ordinal is not None and line_ordinal < state.start_ordinal:
            return None
        ptype = payload.get("type")
        if kind == "turn_context":
            if not blank(payload.get("cwd")):
                self.cwd = payload["cwd"]
        elif kind == "response_item":
            found = model.prompt_line(state.source, obj)
            if found is not None:
                self.prompt(found.get("t", t), found.get("continuation"))
            elif ptype in ("function_call", "custom_tool_call"):
                value = payload.get("arguments" if ptype == "function_call" else "input")
                script = parsers.code_script(state.source, payload.get("name"), value) \
                    if ptype == "custom_tool_call" else None
                if script is not None:
                    self.script(state.source, payload.get("call_id"), t, script)
                else:
                    self.call(state.source, payload.get("call_id"), t, payload.get("name"), value, None)
            elif ptype in ("function_call_output", "custom_tool_call_output"):
                self.result(state.source, payload.get("call_id"), payload, t)
        return t

    # -- omp, Pi and OpenClaw
    def pi_family(self, state, obj, dedupe, raw):
        ref = engine()
        kind = obj.get("type")
        if kind == "session":
            if self.header_seen:
                return None
            self.header_seen = True
            if not blank(obj.get("cwd")):
                self.cwd = obj["cwd"]
            return ref.parse_ts(obj.get("timestamp"))
        if not self.header_seen or not isinstance(obj.get("id"), str):
            return None
        t = ref.parse_ts(obj.get("timestamp"))
        if t is None or (state.file_created is not None and t < state.file_created - COPY_MS):
            return None
        message = obj.get("message")
        if kind != "message" or not isinstance(message, dict):
            return t
        role = message.get("role")
        if role == "user":
            found = model.prompt_line(state.source, obj)
            if found is not None:
                self.line_kind(found["kind"], found.get("t", t), found.get("continuation"))
        elif role == "assistant":
            if not ref.omp_guard(message):
                return t
            stamp = ref.trunc64(message["timestamp"]) if ref.finite(message.get("timestamp")) and \
                message["timestamp"] > 0 else t
            key = "%s:%s:%d" % (state.source, obj["id"], stamp)
            duplicate = dedupe.owner(key) not in (None, self.path)
            self.assistant(duplicate)
            if not duplicate and not (state.source == "openclaw" and openclaw_mirror(state, message)):
                self.calls_in(state.source, message.get("content"), "toolCall", "arguments", t, raw)
        elif role == "toolResult":
            self.result(state.source, message.get("toolCallId"), message, t)
        return t

    # -- Factory Droid
    def droid(self, state, obj, dedupe, raw):
        ref = engine()
        kind = obj.get("type")
        if kind == "session_start":
            if self.header_seen:
                return None
            self.header_seen = True
            if not blank(obj.get("cwd")):
                self.cwd = obj["cwd"]
            return ref.parse_ts(obj.get("timestamp"))
        t = ref.parse_ts(obj.get("timestamp"))
        message = obj.get("message")
        if t is None or kind != "message" or not isinstance(message, dict):
            return t
        role = message.get("role")
        if role == "user":
            found = model.prompt_line(state.source, obj)
            if found is not None:
                self.line_kind(found["kind"], found.get("t", t), found.get("continuation"))
            content = message.get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self.result(state.source, block.get("tool_use_id"), block, t)
        elif role == "assistant" and isinstance(obj.get("id"), str) and obj["id"]:
            duplicate = dedupe.owner("droid:" + obj["id"]) not in (None, self.path)
            self.assistant(duplicate)
            if not duplicate:
                self.calls_in(state.source, message.get("content"), "tool_use", "input", t, raw)
        return t

    # -- prompts and withdrawal (6.9 Tasks)
    def line_kind(self, kind, t, continuation):
        if kind in ("prompt", "command"):
            self.prompt(t, kind == "prompt" and bool(continuation))
            self.pending = (kind, self.events[-1])
        elif kind == "commandOutput":
            if self.pending is not None and self.pending[0] == "command":
                self.withdraw()
            self.pending = None

    def prompt(self, t, continuation):
        self.events.append({"t": t, "type": "prompt", "continuation": bool(continuation)})
        self.stats["prompts"] += 1

    def assistant(self, duplicate):
        if self.pending is not None and duplicate:
            self.withdraw()
        self.pending = None

    def withdraw(self):
        event = self.pending[1]
        for i in range(len(self.events) - 1, -1, -1):
            if self.events[i] is event:
                del self.events[i]
                break
        self.stats["withdrawn"] += 1
        self.stats["prompts"] -= 1

    # -- calls and results (6.10 Actions, TASK-PARSERS 8)
    def calls_in(self, source, content, block_type, field, t, raw):
        if not isinstance(content, list):
            return
        raws = None
        for k, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != block_type:
                continue
            value = block.get(field)
            if raws is None and isinstance(value, (dict, list)) and raw is not None:
                raws = raw_inputs(raw, field)
            self.call(source, block.get("id"), t, block.get("name"), value, (raws or {}).get(k))

    def call(self, source, call_id, t, tool, value, raw_value):
        if not isinstance(call_id, str) or not isinstance(tool, str) or call_id in self.calls:
            return
        self.calls[call_id] = self.action(source, t, tool, value, digest(value, raw_value))

    def script(self, source, call_id, t, script):
        """A Codex code-mode script (TASK-PARSERS 4.4): its tool calls are the actions; the script is none."""
        if not isinstance(call_id, str) or call_id in self.calls:
            return
        subs = [self.action(source, t, c["tool"], c["input"], c["digest"]) for c in script["calls"]]
        self.calls[call_id] = (subs, script["prints"])
        self.stats["scripts"] = self.stats.get("scripts", 0) + 1

    def action(self, source, t, tool, value, dig):
        kind = parsers.action_kind(source, tool, value, self.cwd)
        event = {"t": t, "type": "action", "kind": kind, "tool": tool, "digest": dig}
        info = None
        if kind == "command":
            text = parsers.command_text(source, tool, value)
            if text is not None:
                info = parsers.command_info(text, self.cwd)
                event["command"] = {k: info[k] for k in COMMAND_FIELDS}
                if (info.get("suite") or "").startswith("mvn "):
                    compile_cwd = parsers.maven_compile_cwd(source, tool, value, self.cwd)
                    coverage = (info if compile_cwd == self.cwd else parsers.command_info(text, compile_cwd)) \
                        .get("mavenCompileKey")
                    if coverage is not None:
                        event["command"]["mavenCompileKey"] = coverage
        elif kind == "edit":
            event["hunks"] = parsers.edit_hunks(source, tool, value, self.cwd)
        self.events.append(event)
        self.stats["calls"] += 1
        return Call(event, tool, value, self.cwd, info, parsers.continuation(source, tool, value))

    def result(self, source, call_id, result, t):
        entry = self.calls.get(call_id) if isinstance(call_id, str) else None
        if isinstance(entry, tuple):
            del self.calls[call_id]
            subs, prints = entry
            items = parsers.code_items(source, result)
            for sub in subs:
                sub.answered = True
            if items is not None and prints and len(items) == len(prints):
                self.stats["scriptsMapped"] = self.stats.get("scriptsMapped", 0) + 1
                done = set()
                for (index, _), item in zip(prints, items):
                    if index not in done:
                        done.add(index)
                        self.answer(source, subs[index], item if item is not None else "", t)
            for sub in subs:
                sub.value = None
            return
        if entry is None or entry.answered:
            self.stats["orphanResults"] += 1
            return
        self.answer(source, entry, result, t)

    def answer(self, source, call, result, t):
        call.answered = True
        self.stats["results"] += 1
        event = call.event
        out = parsers.outcome(source, result, t, call.tool, call.value, call.cwd)
        event["outcome"] = out
        text = parsers.result_text(source, result)
        session = parsers.running_session(source, result) if call.continues is None else None
        if session is not None:
            masked = parsers.shell_call_state(source, call.tool, call.value, call.cwd)[1]
            self.sessions[session] = Running(event, call.info, call.cwd, masked, text)
        elif call.info is not None:
            event["failures"] = parsers.failures(call.info, text, call.cwd, out["failed"])
        if call.continues is not None and call.continues in self.sessions:
            self.stats["continuationResults"] += 1
            self.finish(call.continues, out, text, t)
        call.value = None

    def finish(self, key, out, text, t):
        """A continuation's result for session `key`: its output (None for a Claude notice, which has none) joins the
        command's; an exit code ends it."""
        running = self.sessions.get(key)
        if running is None:
            return
        if text is not None:
            running.texts.append(text)
        if out["exitCode"] is None or out["failed"] is None:
            return
        final = {"exitCode": out["exitCode"], "failed": None if running.masked else out["failed"], "t": t}
        running.event["outcome"] = final
        if running.info is not None:
            joined = ("" if self.source == "codex" else "\n").join(running.texts)
            running.event["failures"] = parsers.failures(running.info, joined, running.cwd, final["failed"])
        del self.sessions[key]
        self.stats["stitched"] += 1

    # -- model and effort after each line (6.11 Task records)
    def track(self, state, t):
        ref = engine()
        effort = modes.attribution(ref.agent_facts(state), "effort")["mode"]
        entry = (t, state.model, None if effort == "unknown" else effort)
        last = self.models[-1] if self.models else None
        if last is not None and last[1:] == entry[1:] and t >= last[0]:
            return
        self.models.append(entry)


def openclaw_mirror(state, message):
    """METRICS 4.14 *Mirrors*: a delegated turn mirrored into the transcript."""
    meta = message.get("__openclaw") if isinstance(message.get("__openclaw"), dict) else {}
    origin = meta.get("mirrorOrigin")
    return ((isinstance(origin, str) and origin != "") or (message["api"] == "cli" and message["provider"] == "claude-cli")
            or message["provider"].startswith("acp-") or state.oc_mirror_window)


def attach(state):
    """Gives a parser state its composer when its source has a task score (call before reading)."""
    if state.source in SCORED:
        state.tasks = Composer(state)
    return state


# ----------------------------------------------------------------------------- values (6.9 Values) at an instant

def effective_phase(state, now):
    """6.7 step 1: a phase older than 300 s without a new line is idle."""
    phase = state.phase
    if phase != "idle" and state.last_line is not None and now - state.last_line > STALE_MS:
        return "idle"
    return phase


def listed(agents, now):
    """6.7 steps 1–4: the agents list (ids) and each listed agent's tree parent."""
    by_id = {a["id"]: a for a in agents}
    chosen = {a["id"] for a in agents if a["phase"] == "generating" or
              (a["lastLineMs"] is not None and now - a["lastLineMs"] <= LISTED_MS)}
    for aid in list(chosen):
        seen, cur = {aid}, by_id[aid].get("parentId")
        while cur in by_id and cur not in seen:
            chosen.add(cur)
            seen.add(cur)
            cur = by_id[cur].get("parentId")
    return {aid: (by_id[aid].get("parentId") if by_id[aid].get("parentId") in chosen else None) for aid in chosen}


def agent_rows(states, parents, now, models_by_source=None):
    """Per agent of a scored source: its form, value, state, dimensions, facts and predictions at `now`."""
    rows = []
    for st in states:
        if getattr(st, "tasks", None) is None:
            continue
        agent = {"id": st.path, "source": st.source, "phase": effective_phase(st, now), "lastLineMs": st.last_line,
                 "parentId": parents.get(st.path), "events": st.tasks.events}
        value = model.agent_value(agent, now)
        task = model.current_task(agent["events"], now)
        detail = model.task_result(model.evaluate(task["events"], now)) if task else None
        models = (models_by_source or {}).get(st.source) or model.prior_models()
        pred = model.predictions(agent["events"], now, models)
        rows.append({"agent": agent, "value": value, "task": detail, "taskStartMs": task["startMs"] if task else None,
                     "predictions": pred, "flags": {k: flag(models[k]) for k in models}})
    return rows


def flag(m):
    return {"calibrated": m["calibrated"], "tasks": m["tasks"]}


def set_values(rows, now, sets=None):
    """6.9 *Sets and groups*: the whole set, source sets and board groups over the agents list."""
    agents = [r["agent"] for r in rows]
    tree = listed(agents, now)
    values = {r["agent"]["id"]: r["value"] for r in rows}
    shown = [dict(a, parentId=tree[a["id"]]) for a in agents if a["id"] in tree]
    shown.sort(key=lambda a: model._ordinal(a["id"]))
    out = {"listed": [a["id"] for a in shown], "sets": [], "groups": {}}
    for sources in sets if sets is not None else [None] + [[s] for s in SCORED]:
        members = [a for a in shown if sources is None or a["source"] in sources]
        if sources is not None and not members and sets is None:
            continue
        out["sets"].append(dict(sources=sources, **model.highest(members, values)))
    by_id = {a["id"]: a for a in shown}
    for root, members in model.thinking.board_groups(shown).items():
        out["groups"][root] = dict(members=members, **model.highest([by_id[m] for m in members], values))
    return out


# ----------------------------------------------------------------------------- history records (6.11)

def records_of(state, analysis_ms, day_of):
    """The task records of one agent read whole, with the dump's identity fields."""
    out = []
    for rec in model.task_records(state.tasks.events, analysis_ms, state.last_line or 0, state.tasks.models):
        rec = dict(rec, agentId=state.path, source=state.source, day=day_of(rec["endMs"]))
        out.append(rec)
    return out
