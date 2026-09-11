"""CodexAdapter: OpenAI's Codex CLI, read through its own rollout log.

Phase 3 of docs/any-cli.md. Codex keeps a transcript as good as Claude
Code's - one JSONL "rollout" per session under ~/.codex/sessions,
filed by date - so this adapter tails it rather than the screen.
Measured on codex-cli 0.151.0 and a rollout from 2026-08-28:

    rollout-<timestamp>-<session id>.jsonl, lines of
      {"type": "session_meta",  "payload": {"id", "cwd", ...}}
      {"type": "response_item", "payload": {"type": "message",
                                            "role": "user"|"assistant",
                                            "content": [{"type": "input_text"
                                                         |"output_text",
                                                         "text": ...}]}}
      {"type": "response_item", "payload": {"type": "function_call"
                                            |"custom_tool_call"|..., "name"}}
      {"type": "event_msg",     "payload": {"type": "task_started"}}
      {"type": "event_msg",     "payload": {"type": "task_complete",
                                            "last_agent_message": ...}}

task_complete is the turn end, and it carries the answer. Which
rollout is a checkout's is decided by session_meta.cwd, not by where
the file sits; the session id is the tail of the file name.

Dialogs before the first prompt, measured: "Do you trust the contents
of this directory?" (Enter = Yes, continue) and "Hooks need review"
(1 Review, 2 Trust all, 3 Continue without trusting - a worker takes 3:
a checkout we made has hooks we did not review, and they stay off).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .agent_events import AgentEvent, SUMMARY_CEILING, keep_end
from .cli_adapter import CliAdapter

CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
# Rollouts older than this are not candidates for a session we just
# started; reading session_meta off every file Codex ever wrote is not.
DISCOVERY_WINDOW_S = 2 * 24 * 3600
_ROLLOUT = re.compile(r"rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                      r"[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
# Messages Codex injects as the "user" that the user never said.
_INJECTED = ("<", "# AGENTS.md", "<environment_context>")


def rollout_cwd(path: Path) -> str | None:
    """The checkout a rollout belongs to, from its session_meta line."""
    try:
        with path.open() as handle:
            for _ in range(3):
                line = handle.readline()
                if not line:
                    break
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("type") == "session_meta":
                    return (entry.get("payload") or {}).get("cwd")
    except OSError:
        return None
    return None


class CodexAdapter(CliAdapter):
    name = "codex"
    display = "Codex"
    binary_name = "codex"
    sessions_root: Path | None = None       # override for tests

    # Our permission modes onto Codex's approval policy and sandbox.
    # "auto": the sandbox lets it edit the checkout; commands outside
    # it, and anything Codex judges risky, ask - the asking is what the
    # card and the Boss see as an approval.
    _MODES = {
        "auto": ["-a", "on-request", "-s", "workspace-write"],
        "acceptEdits": ["-a", "on-request", "-s", "workspace-write"],
        "default": ["-a", "untrusted", "-s", "workspace-write"],
        "bypassPermissions": ["--dangerously-bypass-approvals-and-sandbox"],
    }

    def launch_argv(self, prompt: str, permission_mode: str,
                    session_id: str | None = None) -> list[str]:
        return [self.binary, *self._MODES.get(permission_mode, self._MODES["auto"]),
                prompt]

    def resume_argv(self, session_id: str,
                    permission_mode: str | None = None) -> list[str]:
        # `codex resume [OPTIONS] [SESSION_ID]`: the mode's flags go
        # before the id (0.151's help).
        mode = self._MODES.get(permission_mode, self._MODES["auto"]) \
            if permission_mode else []
        return [self.binary, "resume", *mode, session_id]

    # -- the screen -----------------------------------------------------------
    # Codex's composer: a "›" prompt line, and its footer hints. Measured
    # on 0.151.0 after the dialogs.
    _READY = re.compile(r"(?m)^\s*›\s|Ctrl\+C to quit|\? for shortcuts|/ for commands")
    # The approval overlay, from the 0.151 binary's own strings: one of
    # these questions, then numbered options ("Yes, just this once",
    # "No, and tell Codex what to do differently", ...), the chosen one
    # marked "›". Both parts are required. Words alone were not enough:
    # "approve" also appears in the record Codex leaves once an approval
    # is answered - "✔ You approved codex to run gh pr list ... this
    # time" - and matching that kept a prompt "pending" that was gone.
    # Measured 2026-08-30 09:23-09:25 on task_00eac099: the decision was
    # delivered and the prompt "did not clear" (twice), and every send
    # after it waited 45 s on a dialog that was not there.
    _QUESTIONS = ("would you like to run the following command",
                  "would you like to make the following edits",
                  "would you like to grant these permissions",
                  "would you like to send input to the existing terminal",
                  "allow codex to run", "allow codex to apply",
                  "apply changes?")
    _OPTION = re.compile(r"^\s*[›>]?\s*\d+\.\s*(yes|no|allow|decline)\b", re.I)

    # The composer as an input box: "› " and the text, wrapped lines
    # indented under it, a blank line before the footer (measured on
    # 0.151, "› Ask Codex to do anything"). Its hint rotates between
    # versions, so the probe (SUGGESTS_IN_BOX) backs the list up: typing
    # replaces a hint and appends to a draft, whatever the words.
    PROMPT_MARKS = ("\u203a",)
    PLACEHOLDERS = ("ask codex to do anything",)
    SUGGESTS_IN_BOX = True
    # "Working (12s • esc to interrupt)" while a turn runs - from the
    # binary's strings, like _QUESTIONS; not yet seen on a live screen.
    BUSY = re.compile(r"esc to interrupt", re.I)

    def prompt_ready(self, screen: str) -> bool:
        return bool(self._READY.search(screen)) and \
            self.startup_dialog(screen) is None and \
            self.approval_prompt(screen) is None

    def startup_dialog(self, screen: str) -> str | None:
        low = screen.lower()
        if "do you trust the contents of this directory" in low:
            return "trust"
        if "hooks need review" in low:
            return "hooks"
        # Measured 2026-09-11 on 0.151.0, before the trust dialog:
        #   ✨ Update available! 0.151.0 -> 0.154.0
        #   › 1. Update now (runs `npm install -g @openai/codex`)
        #     2. Skip
        #     3. Skip until next version
        if "update available" in low and "skip" in low:
            return "update"
        if "sign in" in low and ("chatgpt" in low or "api key" in low):
            return "auth"
        return None

    def approval_prompt(self, screen: str) -> str | None:
        if self.startup_dialog(screen) is not None:
            return None
        lines = [ln.strip() for ln in screen.splitlines() if ln.strip()]
        asked = next((i for i, ln in enumerate(lines)
                      if any(q in ln.lower() for q in self._QUESTIONS)), None)
        if asked is None:
            return None
        if not any(self._OPTION.match(ln) for ln in lines[asked + 1:]):
            return None                 # answered already; only the record remains
        # What is being asked: the question, the command ("$ ...") or
        # files, and Codex's reason - measured 2026-08-30 on 0.151:
        #   Would you like to run the following command?
        #   Environment: local
        #   Reason: May I connect to GitHub to read the login ...
        #   $ gh api user --jq .login
        # The command comes first after the question, whatever the
        # order on screen; the reason may be long, and the command is
        # what the Boss decides on.
        body = []
        for line in lines[asked + 1:]:
            if self._OPTION.match(line):
                break
            body.append(line)
        command = [ln for ln in body if ln.startswith("$ ")]
        rest = [ln for ln in body if not ln.startswith("$ ")]
        return " | ".join([lines[asked], *command, *rest][:4])

    def deny_keys(self, screen: str) -> list[list[str]]:
        """Pick the "No, ..." option by its number; Escape if there is
        none to be found."""
        lines = [ln.strip() for ln in screen.splitlines() if ln.strip()]
        for line in lines:
            match = self._OPTION.match(line)
            if match and match.group(1).lower() == "no":
                number = re.search(r"(\d+)\.", line).group(1)
                return [[number], ["Enter"]]
        return [["Escape"]]

    # -- the rollout ----------------------------------------------------------------
    def transcript_dir(self, working_directory: str) -> Path | None:
        return self.sessions_root or CODEX_SESSIONS

    def transcripts(self, working_directory: str) -> list[Path]:
        root = self.transcript_dir(working_directory)
        if root is None or not root.is_dir():
            return []
        wanted = os.path.normpath(working_directory)
        cutoff = time.time() - DISCOVERY_WINDOW_S
        out = []
        for path in root.rglob("rollout-*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            cwd = rollout_cwd(path)
            if cwd and os.path.normpath(cwd) == wanted:
                out.append(path)
        return out

    def session_id_of(self, transcript: Path) -> str:
        match = _ROLLOUT.search(transcript.name)
        return match.group(1) if match else transcript.stem

    def transcript_for(self, working_directory: str,
                       session_id: str) -> Path | None:
        root = self.transcript_dir(working_directory)
        if root is None or not root.is_dir():
            return None
        for path in root.rglob(f"rollout-*-{session_id}.jsonl"):
            return path
        return None

    def normalize(self, entry: dict, state: dict) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        kind = entry.get("type")
        payload = entry.get("payload") or {}
        ptype = payload.get("type")
        if kind == "response_item":
            if ptype == "message":
                text = " ".join(" ".join(
                    block.get("text", "") for block in (payload.get("content") or [])
                    if isinstance(block, dict)
                    and block.get("type") in ("input_text", "output_text")).split())
                if not text:
                    return events
                if payload.get("role") == "user":
                    if text.startswith(_INJECTED):
                        return events        # Codex's own context, not the user's
                    events.append(AgentEvent(type="progress",
                                             summary=f"> {text[:200]}",
                                             detail={"source": "user_message"}))
                elif payload.get("role") == "assistant":
                    state.setdefault("turn_text", []).append(text)
                    # The whole message too, as Claude Code's reader gives
                    # it: the Boss's window draws each one as it is
                    # written, and a 300-character summary drawn there
                    # was an answer cut mid-sentence. Paragraph breaks
                    # are kept for it; the summary is one line.
                    whole = "\n\n".join(
                        block.get("text", "").strip()
                        for block in (payload.get("content") or [])
                        if isinstance(block, dict)
                        and block.get("type") == "output_text"
                        and block.get("text", "").strip())
                    events.append(AgentEvent(type="progress", summary=text[:300],
                                             text=(whole or text)[:SUMMARY_CEILING]))
            elif ptype in ("function_call", "custom_tool_call", "local_shell_call",
                           "web_search_call"):
                name = payload.get("name") or ptype
                args = payload.get("arguments") or payload.get("input") or ""
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        pass
                if isinstance(args, dict):
                    gist = next((str(args[k])[:70] for k in
                                 ("cmd", "command", "path", "query", "url")
                                 if k in args), "")
                    if not gist:
                        gist = next((str(v)[:70] for v in args.values()
                                     if isinstance(v, str)), "")
                else:
                    gist = str(args)[:70]
                events.append(AgentEvent(
                    type="progress",
                    summary=f"{name}({gist})" if gist else name,
                    detail={"tool": name}))
        elif kind == "event_msg":
            if ptype == "task_started":
                state["turn_text"] = []
                events.append(AgentEvent(type="started"))
            elif ptype == "task_complete":
                # What Codex said last - the answer, not the turn's
                # narration before it - and all of it (cli_adapter's
                # _turn_end says why).
                texts = state.get("turn_text", [])
                said = payload.get("last_agent_message") or \
                    (texts[-1] if texts else "")
                state["turn_text"] = []
                events.append(AgentEvent(
                    type="completed",
                    summary=keep_end(" ".join(str(said).split()),
                                     SUMMARY_CEILING)))
        return events

    def process_needle(self, session_id: str | None) -> str | None:
        # A resumed session names its id (`codex resume <id>`); a fresh
        # one does not - the checkout finds it then.
        return session_id or None


ADAPTERS = {"codex": CodexAdapter}
