"""The watch link: how a delegation becomes clickable in the Boss's own
transcript.

The Boss's replies live in a Claude Code terminal, and cmux has no API for
injecting live widgets between transcript messages. What that terminal
*does* render is a URL: plain, visible, blue, cmd+clickable. So each
delegation tool result carries one, the Boss prints it under its reply,
and clicking it lands here - a loopback HTTP server that answers exactly
one question: "show me that worker".

The handler focuses the task's workspace and answers 204 No Content, so
whichever browser took the click has nothing to show and the workspace the
user just asked for is what is in front of them.

The port is remembered in the conductor's home between runs, so a link in
yesterday's transcript still opens today's session for the same task. A
link is only a focus request; the task id routes through
``focus_task`` and nothing else, so the surface exposes no other action.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Awaitable, Callable

from .observability import application_log

_WATCH = re.compile(r"^/watch/([A-Za-z0-9._-]+)$")
_LINK_LINE = re.compile(r"^.*http://127\.0\.0\.1:\d+/watch/\S+.*$\n?",
                        re.MULTILINE)


def without_watch_links(text: str) -> str:
    """The reply without its watch-link lines.

    The link is for the transcript's eyes - a thing to click. The same
    reply is also spoken aloud, and a URL read out letter by letter is
    noise, so the voice path drops the whole line it sits on."""
    return _LINK_LINE.sub("", text).strip()


_RESPONSE = (b"HTTP/1.1 204 No Content\r\n"
             b"Connection: close\r\n\r\n")


class WatchLinkServer:
    """One GET /watch/<task_id> -> one focus_task(task_id). Loopback only."""

    def __init__(self, home: Path,
                 on_watch: Callable[[str], Awaitable[None]]) -> None:
        self.home = Path(home)
        self.on_watch = on_watch
        self.port = 0
        self._server: asyncio.base_events.Server | None = None

    @property
    def _port_file(self) -> Path:
        return self.home / "boss" / "watch.port"

    def _saved_port(self) -> int:
        try:
            return int(self._port_file.read_text().strip())
        except (OSError, ValueError):
            return 0

    async def start(self) -> None:
        # The remembered port first, so links in older transcripts keep
        # working across restarts; an ephemeral one when it is taken.
        for port in (self._saved_port(), 0):
            try:
                self._server = await asyncio.start_server(
                    self._serve, "127.0.0.1", port)
                break
            except OSError:
                if port == 0:
                    raise
        self.port = self._server.sockets[0].getsockname()[1]
        try:
            self._port_file.parent.mkdir(parents=True, exist_ok=True)
            self._port_file.write_text(str(self.port))
        except OSError:
            application_log("ui", "watch.port_unsaved",
                            "the watch port could not be remembered",
                            severity="warning", exc_info=True,
                            port=self.port)
        application_log("ui", "watch.server_started",
                        f"watch links serve on 127.0.0.1:{self.port}",
                        port=self.port)

    def url_for(self, task_id: str) -> str:
        return f"http://127.0.0.1:{self.port}/watch/{task_id}"

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _serve(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        try:
            request = (await reader.readline()).decode("ascii", "replace")
            while (await reader.readline()).strip():
                pass                       # headers: read past, unused
            writer.write(_RESPONSE)
            await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            return
        finally:
            writer.close()
        parts = request.split()
        if len(parts) < 2 or parts[0] != "GET":
            return
        match = _WATCH.match(parts[1])
        if not match:
            return
        task_id = match.group(1)
        application_log("ui", "watch.link_opened",
                        f"a watch link was clicked for {task_id}",
                        task_id=task_id)
        try:
            await self.on_watch(task_id)
        except Exception:
            application_log("ui", "watch.open_failed",
                            f"could not show {task_id}",
                            severity="error", exc_info=True,
                            task_id=task_id)
