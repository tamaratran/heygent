"""ClaudeCodeRuntime: the Claude Code provider adapter.

Wraps ClaudeSDKClient, which holds a persistent interactive session: connect
once, then send any number of turns into it, interrupt mid-turn, and resume by
session id after a restart. All Claude message types are translated into the
normalized AgentEvent stream here and nowhere else.

Tool policy defaults to read-only, the same stance as the voice agent: over a
voice channel nothing prompts for approval, so write tools are opt-in.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                              ClaudeSDKClient, PermissionResultAllow,
                              PermissionResultDeny, ResultMessage,
                              SystemMessage, TextBlock, ToolUseBlock,
                              create_sdk_mcp_server, tool)

from .agent_events import AgentEvent, SUMMARY_CEILING, keep_end
from .observability import (ObservabilityBus, ObservabilityEvent,
                            application_log)
from .runtime import (ApprovalPolicy, CodingAgentRuntime,
                      ExecutionTranscript, EventHandler, TaskExecution)

READ_ONLY_TOOLS = ["Read", "Glob", "Grep"]
WRITE_TOOLS = READ_ONLY_TOOLS + ["Edit", "Write", "Bash"]
BLOCKED_WITHOUT_WRITE = ["Bash", "Edit", "Write", "NotebookEdit",
                         "Task", "WebFetch", "WebSearch"]
PROGRESS_TOOL = "mcp__progress__report_progress"

# Workers report semantic progress through a control-plane tool (spec 48);
# the Conductor turns these checkpoints into context.md updates and events
# instead of summarizing transcripts with another model.
_PROGRESS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string",
                    "description": "One or two sentences of current state."},
        "status": {"type": "string",
                   "description": "e.g. investigating, implementing, testing"},
        "findings": {"type": "array", "items": {"type": "string"},
                     "description": "Important discoveries worth remembering."},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"},
                     "description": "Caveats the user should hear at "
                                    "completion (e.g. needs manual "
                                    "verification)."},
        "needs_user_input": {"type": "boolean",
                             "description": "True if you are blocked on a "
                                            "user decision."},
    },
    "required": ["summary"],
}


def _tool_gist(name: str, args: dict) -> str:
    for key in ("file_path", "command", "pattern", "path", "url", "prompt"):
        if isinstance(args.get(key), str):
            return f"{name}({' '.join(args[key].split())[:70]})"
    return name


@dataclass
class _Session:
    task_id: str
    client: ClaudeSDKClient
    working_directory: str
    session_id: str | None = None
    status: str = "starting"
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    handlers: list[EventHandler] = field(default_factory=list)
    reader: asyncio.Task | None = None
    turn_text: list[str] = field(default_factory=list)
    pending: dict = field(default_factory=dict)   # approval_id -> Future
    pending_detail: dict = field(default_factory=dict)  # approval_id -> request
    first_prompt: str = ""


class ClaudeCodeRuntime(CodingAgentRuntime):
    def __init__(self, allow_write: bool = False,
                 system_prompt: str | None = None,
                 max_turns: int | None = None,
                 bus: ObservabilityBus | None = None,
                 approval_policy: ApprovalPolicy | None = None,
                 transcript_dir: str | None = None) -> None:
        self.allow_write = allow_write
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.bus = bus or ObservabilityBus()
        self.approval_policy = approval_policy or ApprovalPolicy()
        self.transcript = ExecutionTranscript(transcript_dir) \
            if transcript_dir else None
        self.sessions: dict[str, _Session] = {}

    def _log(self, sess: _Session, text: str) -> None:
        if self.transcript is not None:
            self.transcript.write(sess.task_id, text)

    def _observe(self, event_type: str, sess: _Session,
                 severity: str = "info", **data) -> None:
        self.bus.emit(ObservabilityEvent(
            type=event_type, component="runtime", task_id=sess.task_id,
            provider_session_id=sess.session_id, severity=severity,
            data=data))

    # -- internals --------------------------------------------------------
    def _progress_server(self, sess: _Session):
        """One in-process report_progress tool, bound to this session."""
        @tool("report_progress",
              "Report semantic progress on your task: current status, "
              "important findings, and next steps. Call this at meaningful "
              "milestones (discovery, approach change, blocker, completion), "
              "not after every file read.", _PROGRESS_SCHEMA)
        async def report_progress(args: dict) -> dict:
            detail = {k: args[k] for k in ("status", "findings", "next_steps",
                                           "files_changed", "warnings")
                      if k in args}
            summary = str(args.get("summary", "")).strip()
            if args.get("needs_user_input"):
                self._emit(sess, AgentEvent(type="needs_input",
                                            question=summary, detail=detail))
            else:
                self._emit(sess, AgentEvent(type="checkpoint",
                                            summary=summary, detail=detail))
            self._observe("runtime.progress", sess, summary=summary[:200])
            return {"content": [{"type": "text", "text": "recorded"}]}
        return create_sdk_mcp_server("progress", tools=[report_progress])

    def _approval_callback(self, sess: _Session):
        """Approvals as structured control-plane events (spec section 17):
        the policy allows or asks; asking blocks THIS tool call on a Future
        the Conductor resolves via approve/deny - the session itself stays
        alive and waiting, never replaced."""
        async def can_use_tool(tool_name: str, args: dict, context):
            if self.approval_policy.decide(tool_name, args) == "allow":
                # Routine and pre-allowed: auto-approved, but observable -
                # the Manager can see it happened without being woken up.
                self._observe("approval.policy_decision", sess,
                              decision="allow",
                              description=_tool_gist(tool_name, args))
                return PermissionResultAllow()
            approval_id = "appr_" + __import__("secrets").token_hex(4)
            approval = {"approval_id": approval_id, "action_type": "command"
                        if tool_name == "Bash" else "other",
                        "description": _tool_gist(tool_name, args),
                        "risk_level": "high", "created_at": ""}
            future = asyncio.get_running_loop().create_future()
            # Keep the request beside its future. Storing only the future left
            # pending_approvals able to report an id and nothing else, so the
            # Manager could not say what it was asking permission for.
            sess.pending[approval_id] = future
            sess.pending_detail[approval_id] = approval
            sess.status = "waiting_for_approval"
            self._emit(sess, AgentEvent(type="approval_required",
                                        question=approval["description"],
                                        detail={"approval": approval}))
            self._observe("runtime.approval_required", sess,
                          approval_id=approval_id,
                          description=approval["description"])
            self._log(sess, f"\n⚠ approval needed: {approval['description']}")
            approved = await future
            # The future resolving IS the live session unblocking: this
            # return value is what lets the exact worker proceed. Emit the
            # acknowledgement so the Conductor clears pending state only on
            # provider-side evidence, never on hope.
            self._log(sess, "  ✓ approved" if approved else "  ✗ denied")
            sess.status = "running"
            self._emit(sess, AgentEvent(
                type="approval_resolved",
                detail={"approval_id": approval_id,
                        "decision": "approved" if approved else "denied"}))
            self._observe("runtime.approval_resolved", sess,
                          approval_id=approval_id,
                          decision="approved" if approved else "denied")
            if approved:
                return PermissionResultAllow()
            return PermissionResultDeny(message="The user denied this "
                                                "action.")
        return can_use_tool

    def _options(self, working_directory: str, sess: _Session,
                 resume: str | None = None) -> ClaudeAgentOptions:
        if self.allow_write:
            # Bash stays OUT of allowed_tools so it reaches the approval
            # callback; everyday reads/edits are pre-approved by the list.
            return ClaudeAgentOptions(
                cwd=working_directory,
                max_turns=self.max_turns,
                mcp_servers={"progress": self._progress_server(sess)},
                allowed_tools=READ_ONLY_TOOLS + ["Edit", "Write",
                                                 PROGRESS_TOOL],
                permission_mode="default",
                can_use_tool=self._approval_callback(sess),
                system_prompt=self.system_prompt,
                resume=resume,
            )
        return ClaudeAgentOptions(
            cwd=working_directory,
            max_turns=self.max_turns,
            mcp_servers={"progress": self._progress_server(sess)},
            allowed_tools=list(READ_ONLY_TOOLS) + [PROGRESS_TOOL],
            disallowed_tools=BLOCKED_WITHOUT_WRITE,
            permission_mode="bypassPermissions",
            system_prompt=self.system_prompt,
            resume=resume,
        )

    def _emit(self, sess: _Session, event: AgentEvent) -> None:
        for handler in list(sess.handlers):
            try:
                handler(event)
            except Exception:
                application_log(
                    "runtime", "runtime.event_handler_failed",
                    f"task event handler failed for {event.type}",
                    severity="error", exc_info=True, task_id=sess.task_id,
                    provider_session_id=sess.session_id,
                    agent_event=event.type,
                    handler=getattr(handler, "__qualname__", repr(handler)))

    async def _read(self, sess: _Session) -> None:
        """Translate the Claude message stream into AgentEvents."""
        try:
            async for message in sess.client.receive_messages():
                sid = getattr(message, "session_id", None)
                if sid is None and isinstance(message, SystemMessage):
                    sid = (message.data or {}).get("session_id")
                if sid and sess.session_id is None:
                    sess.session_id = sid
                    self.sessions[sid] = sess
                    if self.transcript is not None:
                        self.transcript.header(
                            sess.task_id, f"task {sess.task_id}",
                            sid, sess.working_directory)
                        if sess.first_prompt:
                            self._log(sess, f"\n> {sess.first_prompt}\n")
                    sess.ready.set()
                    self._emit(sess, AgentEvent(type="started"))
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            sess.turn_text.append(block.text)
                            self._log(sess, f"Claude: {block.text.strip()}")
                            self._emit(sess, AgentEvent(
                                type="progress", summary=block.text.strip()))
                        elif isinstance(block, ToolUseBlock):
                            gist = _tool_gist(block.name, block.input or {})
                            self._log(sess, f"  ⚒ {gist}")
                            self._emit(sess, AgentEvent(
                                type="progress", summary=gist,
                                detail={"tool": block.name}))
                elif isinstance(message, ResultMessage):
                    # The last thing said is the answer; the texts before
                    # it were narration between tool calls.
                    summary = keep_end(" ".join(sess.turn_text[-1].split())
                                       if sess.turn_text else "",
                                       SUMMARY_CEILING)
                    sess.turn_text.clear()
                    sess.status = "idle"
                    if message.is_error:
                        self._log(sess, f"\n✗ failed: {message.subtype}\n")
                        self._observe("runtime.failed", sess,
                                      severity="error",
                                      error=message.subtype)
                        self._emit(sess, AgentEvent(
                            type="failed", error=summary or message.subtype))
                    else:
                        self._log(sess, "\n✓ turn complete - waiting for "
                                        "instructions\n")
                        self._observe("runtime.completed", sess,
                                      cost_usd=message.total_cost_usd,
                                      num_turns=message.num_turns)
                        self._emit(sess, AgentEvent(
                            type="completed", summary=summary,
                            detail={"cost_usd": message.total_cost_usd,
                                    "num_turns": message.num_turns}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            sess.status = "disconnected"
            application_log("runtime", "runtime.reader_failed",
                            "Claude runtime reader failed", severity="error",
                            exc_info=True, task_id=sess.task_id,
                            provider_session_id=sess.session_id)
            self._observe("runtime.reader_failed", sess, severity="error",
                          error=str(exc)[:300])
            self._emit(sess, AgentEvent(type="failed", error=str(exc)))
        else:
            sess.status = "disconnected"

    async def _start(self, task_id: str, working_directory: str,
                     prompt: str | None, resume: str | None) -> _Session:
        sess = _Session(task_id=task_id, client=None,
                        working_directory=working_directory,
                        session_id=None, status="running",
                        first_prompt=prompt or "")
        client = ClaudeSDKClient(self._options(working_directory, sess,
                                               resume))
        sess.client = client
        await client.connect()
        sess.reader = asyncio.create_task(self._read(sess))
        if prompt is not None:
            await client.query(prompt)
        else:
            # A resume with nothing to say: learn the session id by asking
            # nothing. The id arrives with the first turn instead.
            sess.session_id = resume
            self.sessions[resume] = sess
            sess.ready.set()
            sess.status = "idle"
        await asyncio.wait_for(sess.ready.wait(), timeout=60)
        return sess

    # -- CodingAgentRuntime -------------------------------------------------
    async def create_session(self, task_id: str, working_directory: str,
                             initial_prompt: str) -> str:
        # One task, one execution: a hidden second worker is prohibited.
        for sess in self.sessions.values():
            if sess.task_id == task_id and sess.status != "disconnected":
                raise RuntimeError(
                    f"task {task_id} already has a live execution "
                    f"({sess.session_id}); a second session is prohibited")
        sess = await self._start(task_id, working_directory,
                                 initial_prompt, resume=None)
        assert sess.session_id is not None
        return sess.session_id

    async def executions(self) -> list[TaskExecution]:
        return [TaskExecution(
                    task_id=sess.task_id, provider="claude-code",
                    provider_session_id=sess.session_id or "",
                    workspace_path=sess.working_directory,
                    status=sess.status,
                    transcript_path=str(self.transcript.path(sess.task_id))
                    if self.transcript else None)
                for sess in self.sessions.values()
                if sess.status != "disconnected"]

    async def send(self, session_id: str, message: str) -> None:
        sess = self._require(session_id)
        sess.status = "running"
        self._log(sess, f"\n> {message}\n")
        await sess.client.query(message)
        self._observe("runtime.message_sent", sess)

    async def interrupt(self, session_id: str) -> None:
        sess = self._require(session_id)
        self._observe("runtime.interrupt_requested", sess)
        await sess.client.interrupt()
        sess.status = "idle"
        self._log(sess, "\nⅡ interrupted by the user\n")
        self._observe("runtime.interrupted", sess)

    async def resume(self, session_id: str,
                     working_directory: str | None = None) -> None:
        sess = self.sessions.get(session_id)
        if sess and sess.status != "disconnected":
            return                   # already live
        cwd = working_directory or (sess.working_directory if sess else None)
        if cwd is None:
            raise ValueError("resume needs a working_directory for a session "
                             "this runtime has never seen")
        task_id = sess.task_id if sess else ""
        handlers = sess.handlers if sess else []
        new = await self._start(task_id, cwd, prompt=None, resume=session_id)
        new.handlers = handlers      # subscriptions survive the reconnect
        self.sessions[session_id] = new

    async def get_status(self, session_id: str) -> str:
        sess = self.sessions.get(session_id)
        return sess.status if sess else "disconnected"

    async def reconcile_session(self, session_id: str) -> str:
        sess = self.sessions.get(session_id)
        if sess is None:
            return "unreachable"     # a resume may still revive it
        if sess.pending:
            return "waiting_for_approval"
        return {"starting": "starting", "running": "running",
                "idle": "idle",
                "disconnected": "unreachable"}.get(sess.status,
                                                   "unreachable")

    async def pending_approvals(self, session_id: str) -> list[dict]:
        sess = self.sessions.get(session_id)
        if sess is None:
            return []
        return [dict(sess.pending_detail.get(approval_id,
                                             {"approval_id": approval_id}),
                     approval_id=approval_id)
                for approval_id in sess.pending]

    async def resolve_approval(self, session_id: str, approval_id: str,
                               approve: bool) -> None:
        sess = self._require(session_id)
        future = sess.pending.pop(approval_id, None)
        sess.pending_detail.pop(approval_id, None)
        if future is None or future.done():
            raise KeyError(f"approval {approval_id} is unknown or already "
                           "resolved")
        future.set_result(approve)

    async def subscribe(self, session_id: str,
                        handler: EventHandler) -> Callable[[], None]:
        sess = self._require(session_id)
        sess.handlers.append(handler)

        def unsubscribe() -> None:
            try:
                sess.handlers.remove(handler)
            except ValueError:
                pass
        return unsubscribe

    async def destroy(self, session_id: str) -> None:
        sess = self.sessions.pop(session_id, None)
        if sess is None:
            return
        if sess.reader:
            sess.reader.cancel()
        try:
            await sess.client.disconnect()
        except Exception:
            pass
        sess.status = "disconnected"

    def _require(self, session_id: str) -> _Session:
        sess = self.sessions.get(session_id)
        if sess is None:
            raise KeyError(f"no live session {session_id}; call resume() first")
        return sess
