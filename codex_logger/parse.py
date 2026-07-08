"""Parse a Codex CLI rollout JSONL file into normalized records.

Rollout files live at ~/.codex/sessions/YYYY/MM/DD/rollout-<id>.jsonl and are
append-only. Each line is a JSON object with a top-level `type`:

  session_meta   one per file (first line): session identity + cwd + originator
  turn_context   per turn: active model, cwd, approval/sandbox policy
  event_msg      payload.type in {task_started, user_message, agent_message,
                 token_count, task_complete, patch_apply_end, ...}
  response_item  payload.type in {message, reasoning, function_call,
                 function_call_output, custom_tool_call, custom_tool_call_output}

Tool calls are function_call / function_call_output pairs matched by call_id and
cover every tool (exec_command, apply_patch, shell, MCP) — not shell-only, which
is the whole reason this reads the rollout file instead of PreToolUse hooks.
Success/failure is resolved from the structured `*_end` event_msg events Codex
emits per call (`exec_command_end.exit_code`, `patch_apply_end.success`); a tool
whose outcome Codex doesn't report is preserved as `unknown` rather than guessed.

Assistant + user text is taken from `response_item`/`message` (the durable record).
The `event_msg`/`agent_message`/`user_message` events are the streamed duplicates of
that same text, so they are intentionally not re-ingested (no double-counting).

Pure stdlib, fail-open: a malformed line is skipped, never raised.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional


def iter_lines(path: str) -> Iterator[dict]:
    """Yield each JSON object in a rollout file, skipping unparseable lines."""
    try:
        fh = open(path, "r", encoding="utf-8")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (ValueError, TypeError):
                continue


@dataclass
class ToolCall:
    call_id: str
    tool_name: str = ""
    arguments: Optional[str] = None
    output: Optional[str] = None
    exit_code: Optional[int] = None
    status: str = "pending"  # pending | success | failure | unknown
    turn_id: Optional[str] = None
    ts: Optional[str] = None
    seq: int = 0


@dataclass
class Message:
    role: str
    text: str
    phase: Optional[str] = None
    turn_id: Optional[str] = None
    ts: Optional[str] = None
    seq: int = 0


@dataclass
class Turn:
    turn_id: str
    model: Optional[str] = None
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    ts: Optional[str] = None


@dataclass
class Session:
    session_id: str = ""
    parent_thread_id: Optional[str] = None
    thread_source: Optional[str] = None
    originator: Optional[str] = None
    subagent_type: Optional[str] = None
    cwd: Optional[str] = None
    cli_version: Optional[str] = None
    model_provider: Optional[str] = None
    model: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    # session-cumulative token totals (last token_count wins)
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    rollout_path: Optional[str] = None

    @property
    def num_turns(self) -> int:
        return len(self.turns)

    @property
    def num_tool_calls(self) -> int:
        return len(self.tool_calls)


def _extract_subagent(source: Any) -> Optional[str]:
    """source looks like {"subagent": {"other": "guardian"}} or {"subagent": "x"}."""
    if not isinstance(source, dict):
        return None
    sub = source.get("subagent")
    if isinstance(sub, str):
        return sub
    if isinstance(sub, dict):
        # take the first string value we find (e.g. {"other": "guardian"})
        for v in sub.values():
            if isinstance(v, str):
                return v
    return None


def _text_from_content(content: Any) -> str:
    """Join text out of a message content list of {type, text} blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict):
            t = block.get("text")
            if isinstance(t, str):
                parts.append(t)
    return "\n".join(parts)


def _parse_exit_code(output: str) -> Optional[int]:
    """exec_command output embeds 'Process exited with code N'."""
    if not output:
        return None
    marker = "Process exited with code"
    idx = output.find(marker)
    if idx == -1:
        return None
    tail = output[idx + len(marker):].strip()
    num = ""
    for ch in tail:
        if ch.isdigit() or (ch == "-" and not num):
            num += ch
        else:
            break
    try:
        return int(num)
    except ValueError:
        return None


def parse_session(path: str) -> Optional[Session]:
    """Parse a rollout file into a Session, or None if there's nothing usable."""
    s = Session(rollout_path=path)
    calls: dict[str, ToolCall] = {}
    seq = 0
    last_turn_id: Optional[str] = None
    turns: dict[str, Turn] = {}
    saw_any = False

    def touch_call(cid: str) -> ToolCall:
        """Get-or-create the ToolCall for call_id, stamping the active turn."""
        nonlocal seq
        tc = calls.get(cid)
        if tc is None:
            seq += 1
            tc = ToolCall(call_id=cid, seq=seq, turn_id=last_turn_id)
            calls[cid] = tc
        elif tc.turn_id is None:
            tc.turn_id = last_turn_id
        return tc

    for obj in iter_lines(path):
        saw_any = True
        typ = obj.get("type")
        ts = obj.get("timestamp")
        if ts:
            if s.started_at is None:
                s.started_at = ts
            s.ended_at = ts
        payload = obj.get("payload") or {}

        if typ == "session_meta":
            s.session_id = payload.get("id") or s.session_id
            s.parent_thread_id = payload.get("parent_thread_id")
            s.thread_source = payload.get("thread_source")
            s.originator = payload.get("originator")
            s.cli_version = payload.get("cli_version")
            s.model_provider = payload.get("model_provider")
            s.cwd = payload.get("cwd")
            s.subagent_type = _extract_subagent(payload.get("source"))
            if payload.get("timestamp"):
                s.started_at = payload["timestamp"]

        elif typ == "turn_context":
            tid = payload.get("turn_id") or last_turn_id or ""
            model = payload.get("model")
            if model:
                s.model = model  # last non-null turn model = session's modal-ish model
            if tid:
                last_turn_id = tid
                t = turns.setdefault(tid, Turn(turn_id=tid, ts=ts))
                if model:
                    t.model = model
            if s.cwd is None:
                s.cwd = payload.get("cwd")

        elif typ == "event_msg":
            pt = payload.get("type")
            if pt == "task_started":
                tid = payload.get("turn_id")
                if tid:
                    last_turn_id = tid
                    turns.setdefault(tid, Turn(turn_id=tid, ts=ts))
            elif pt == "token_count":
                info = payload.get("info") or {}
                tot = info.get("total_token_usage") or {}
                if tot:
                    # cumulative; last one wins for the session total
                    s.input_tokens = tot.get("input_tokens", s.input_tokens)
                    s.cached_input_tokens = tot.get("cached_input_tokens", s.cached_input_tokens)
                    s.output_tokens = tot.get("output_tokens", s.output_tokens)
                    s.reasoning_tokens = tot.get("reasoning_output_tokens", s.reasoning_tokens)
                    s.total_tokens = tot.get("total_tokens", s.total_tokens)
                last = info.get("last_token_usage") or {}
                if last and last_turn_id:
                    t = turns.setdefault(last_turn_id, Turn(turn_id=last_turn_id, ts=ts))
                    t.input_tokens = last.get("input_tokens", 0)
                    t.cached_input_tokens = last.get("cached_input_tokens", 0)
                    t.output_tokens = last.get("output_tokens", 0)
                    t.reasoning_tokens = last.get("reasoning_output_tokens", 0)
                    t.total_tokens = last.get("total_tokens", 0)
            elif pt == "exec_command_end":
                # Authoritative shell outcome — a real exit_code, not scraped text.
                cid = payload.get("call_id")
                if cid:
                    tc = touch_call(cid)
                    ec = payload.get("exit_code")
                    if ec is not None:
                        tc.exit_code = ec
                        tc.status = "success" if ec == 0 else "failure"
                    if not tc.output:
                        tc.output = payload.get("aggregated_output") or tc.output
            elif pt == "patch_apply_end":
                # apply_patch reports a success bool (and a 'declined' status when
                # a guard rejects it) — the only reliable signal for a patch call.
                cid = payload.get("call_id")
                if cid:
                    tc = touch_call(cid)
                    succ = payload.get("success")
                    if succ is not None:
                        tc.status = "success" if succ else "failure"
                    if not tc.output:
                        tc.output = payload.get("stderr") or payload.get("stdout") or tc.output

        elif typ == "response_item":
            pt = payload.get("type")
            if pt in ("function_call", "custom_tool_call"):
                cid = payload.get("call_id") or payload.get("id") or f"_noid_{seq}"
                tc = touch_call(cid)
                tc.tool_name = payload.get("name") or tc.tool_name
                args = payload.get("arguments")
                if args is None and "input" in payload:
                    args = payload.get("input")
                if isinstance(args, (dict, list)):
                    args = json.dumps(args)
                tc.arguments = args
                tc.ts = ts
            elif pt in ("function_call_output", "custom_tool_call_output"):
                cid = payload.get("call_id") or payload.get("id") or ""
                out = payload.get("output")
                if isinstance(out, (dict, list)):
                    out = json.dumps(out)
                tc = touch_call(cid)
                tc.output = out
                # exit-code scrape is a fallback only — never downgrade a status
                # already resolved by an exec_command_end / patch_apply_end event.
                code = _parse_exit_code(out or "")
                if code is not None:
                    tc.exit_code = code
                    tc.status = "success" if code == 0 else "failure"
                elif tc.status == "pending":
                    tc.status = "unknown"
            elif pt == "message":
                role = payload.get("role")
                if role in ("assistant", "user"):
                    text = _text_from_content(payload.get("content"))
                    if text.strip():
                        seq += 1
                        s.messages.append(Message(
                            role=role, text=text, phase=payload.get("phase"),
                            turn_id=last_turn_id, ts=ts, seq=seq,
                        ))

    if not saw_any or not s.session_id:
        return None

    s.tool_calls = sorted(calls.values(), key=lambda c: c.seq)
    s.turns = list(turns.values())
    return s
