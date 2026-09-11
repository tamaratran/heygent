"""Where each worker runs, decided per task instead of per launch.

Two places a worker can actually live:

  local   Claude Code in a tmux pane on this machine, in its own git
          worktree. Sees uncommitted work; supervised continuously.
  cloud   A Claude Code cloud session on Anthropic's infrastructure,
          readable at claude.ai/code and from a phone. Gets a copy of the
          working tree, reports nothing unless asked, and cannot bypass
          permissions.

That is a real difference in what a worker can do, not a preference, so it
belongs on the task rather than on the process. "Run this one in the cloud
so I can check it from my phone" and "this one needs my uncommitted
changes" are both ordinary things to want, in the same session.

Routing is remembered per session id, because everything after creation -
send, status, interrupt, destroy - has to reach the same runtime that
started it. A session this router has never seen falls to the default,
which is what a restart looks like.

There is no third option. The Claude desktop app runs its own Claude Code
in its own VM with its own session store, and exposes no way in: no API, no
socket, no port, and three deep links (new, needs-input,
continue?session=last) none of which takes a session id. It is a place work
can run, not a place we can put work.
"""

from __future__ import annotations

from typing import Callable

from .runtime import CodingAgentRuntime, EventHandler

LOCATIONS = ("local", "cloud")

# Distinguishes "this runtime has no such attribute" from "it has one and
# its value is None" - a runtime built without a transcript directory has
# transcript = None, and that is an answer, not a miss.
_MISSING = object()


class RoutingRuntime(CodingAgentRuntime):
    def __init__(self, local: CodingAgentRuntime,
                 cloud: CodingAgentRuntime | None = None,
                 default: str = "local",
                 providers: dict[str, CodingAgentRuntime] | None = None) -> None:
        self.runtimes = {"local": local}
        if cloud is not None:
            self.runtimes["cloud"] = cloud
        # One runtime per other CLI, by provider name ("gemini"): the
        # same hosting with a different adapter. A task that names its
        # provider is routed here the way a task that names a place is.
        for name, runtime in (providers or {}).items():
            self.runtimes[name] = runtime
        self.default = default if default in self.runtimes else "local"
        self.owner: dict[str, str] = {}      # session id -> location
        self.wanted: dict[str, str] = {}     # task id -> location, once

    # -- routing ---------------------------------------------------------
    def want(self, task_id: str, location: str) -> None:
        """Ask for the next session of this task to be created somewhere.

        Set just before create_session rather than passed through it: the
        runtime protocol is shared with every other implementation, and
        widening it for a choice only this one can make would oblige them
        all to carry a parameter they cannot honour.
        """
        if location in self.runtimes:
            self.wanted[task_id] = location

    def route(self, session_id: str, provider: str) -> None:
        """Say which provider's runtime a session belongs to, when this
        router has not seen it created.

        `owner` is filled by create_session, so after a restart it is
        empty, and every session fell to the default - Claude Code's
        runtime - whatever CLI it was. A send to a Codex or Gemini worker
        then asked Claude's runtime to resume it: its process check looks
        for `claude` in that checkout, finds none, and would close the
        live window as empty and run `claude --resume` on another CLI's
        session id. The task knows its provider; the conductor tells the
        router before it asks for anything. Never moves a session this
        router already placed, and ignores a provider it has no runtime
        for (Claude Code's own local/cloud choice is not this)."""
        if session_id and provider in self.runtimes and \
                provider not in ("local", "cloud") and \
                session_id not in self.owner:
            self.owner[session_id] = provider

    @property
    def providers(self) -> list[str]:
        """Which CLIs this router can start: Claude Code (local and
        cloud), plus every provider that has its own runtime here. The
        capability probe reads this; without it every other provider
        was reported "not available (runtime does not drive it)" and
        the Boss believed it - measured in its own CLAUDE.md."""
        out = ["claude-code"]
        for name, runtime in self.runtimes.items():
            if name in ("local", "cloud"):
                continue
            provider = getattr(runtime, "provider", name)
            if provider not in out:
                out.append(provider)
        return out

    def location_of(self, session_id: str) -> str:
        return self.owner.get(session_id, self.default)

    def _for(self, session_id: str) -> CodingAgentRuntime:
        return self.runtimes[self.location_of(session_id)]

    def available(self) -> tuple:
        return tuple(self.runtimes)

    @property
    def transcript(self):
        """The local runtime's ExecutionTranscript.

        CodingAgentRuntime declares `transcript = None` as a class
        attribute, so the router inherited one: ordinary lookup succeeded,
        __getattr__ never ran, and `getattr(runtime, "transcript", None)`
        in _surface_request was always None. Every SurfaceRequest went out
        with transcript_path unset. Nothing failed, because the registered
        surfaces attach to a PTY and ignore it - it would have shown up as
        a surface opening onto nothing the day a transcript-based one was
        registered again.

        Local, specifically, and not by preference: a cloud session writes
        no transcript at all, teleported or not, so there is only ever one
        to report.
        """
        return self.runtimes["local"].transcript

    # -- lifecycle -------------------------------------------------------
    async def create_session(self, task_id: str, working_directory: str,
                             initial_prompt: str) -> str:
        where = self.wanted.pop(task_id, self.default)
        runtime = self.runtimes.get(where) or self.runtimes[self.default]
        session_id = await runtime.create_session(
            task_id, working_directory, initial_prompt)
        self.owner[session_id] = where if where in self.runtimes \
            else self.default
        return session_id

    async def send(self, session_id: str, message: str) -> None:
        await self._for(session_id).send(session_id, message)

    async def interrupt(self, session_id: str) -> None:
        await self._for(session_id).interrupt(session_id)

    async def resume(self, session_id: str,
                     working_directory: str | None = None) -> None:
        await self._for(session_id).resume(session_id, working_directory)

    async def get_status(self, session_id: str) -> str:
        return await self._for(session_id).get_status(session_id)

    async def reconcile_session(self, session_id: str) -> str:
        return await self._for(session_id).reconcile_session(session_id)

    async def pending_approvals(self, session_id: str) -> list[dict]:
        return await self._for(session_id).pending_approvals(session_id)

    async def resolve_approval(self, session_id: str, approval_id: str,
                               approve: bool) -> None:
        await self._for(session_id).resolve_approval(
            session_id, approval_id, approve)

    async def subscribe(self, session_id: str,
                        handler: EventHandler) -> Callable[[], None]:
        return await self._for(session_id).subscribe(session_id, handler)

    async def destroy(self, session_id: str) -> None:
        runtime = self._for(session_id)
        self.owner.pop(session_id, None)
        await runtime.destroy(session_id)

    # -- pass-throughs the cloud half adds -------------------------------
    def __getattr__(self, name: str):
        """Anything only one runtime offers - peek, teleport, show,
        unstick - reaches whichever has it. Bound to the session where the
        call names one, so a cloud-only capability never lands on a local
        worker.
        """
        if name.startswith("_") or name in ("runtimes", "owner", "wanted"):
            raise AttributeError(name)
        runtimes = self.__dict__.get("runtimes", {})
        holders = [r for r in runtimes.values()
                   if callable(getattr(r, name, None))]
        if not holders:
            # Not a method but a plain attribute one half exposes.
            # Forwarding callables only meant such an attribute read as
            # missing rather than as the value it has. (`transcript` is
            # NOT this case - the ABC gives it a class-attribute default,
            # so it never reaches here; see the property above.)
            #
            # There is no call here and so no session id to route by: the
            # first runtime that has the attribute wins, in registration
            # order (local, then cloud), which is the same convention the
            # unrouted call below uses.
            for runtime in runtimes.values():
                value = getattr(runtime, name, _MISSING)
                if value is not _MISSING:
                    return value
            raise AttributeError(name)

        def dispatch(*args, **kwargs):
            if args and isinstance(args[0], str) and args[0] in self.owner:
                target = self._for(args[0])
                method = getattr(target, name, None)
                if callable(method):
                    return method(*args, **kwargs)
                # The runtime that owns this session does not offer it.
                # That is an answer, not a failure: asking "is this cloud
                # worker readable?" about a local one means no, and
                # raising made focus_task fail for every local worker.
                return None
            return getattr(holders[0], name)(*args, **kwargs)
        return dispatch
