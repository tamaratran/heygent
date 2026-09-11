"""Where a toast's link goes: focus that session's window.

The toast under a Boss turn (conductor/turn_toast.py) is plain text -
Claude Code strips escape sequences, so there is no OSC 8 hyperlink to
be had. What survives is a bare URL, which the terminal linkifies
itself. This is the other end of that URL: a loopback server whose only
job is to bring one worker's window forward.

Deliberately small. It answers on 127.0.0.1 and nowhere else, it serves
two paths, and the only thing it can do is focus a window that already
exists - the same thing the focus_task tool does. Its port is
remembered in boss/jump.port so a toast printed by one run still points
somewhere after a restart.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

from .observability import application_log

HOST = "127.0.0.1"
PAGE = ("<!doctype html><meta charset=utf-8><title>{title}</title>"
        "<style>body{{font:14px -apple-system,system-ui;margin:3rem auto;"
        "max-width:22rem;color:#333}}</style><p>{body}</p>"
        "<script>setTimeout(()=>window.close(),400)</script>")


class JumpServer:
    """focus is an async callable taking a task id."""

    def __init__(self, home, focus, port: int | None = None,
                 routes: dict | None = None, persist: bool = True) -> None:
        self.home = Path(home)
        self.focus = focus
        self.port = port
        # Anything else a link may do, as {"/a/": async fn(rest) -> str}.
        # A terminal can only linkify a URL, so a URL is how a rendered
        # button is pressed (conductor/app_view.py draws some).
        self.routes = dict(routes or {})
        # A visiting server (the app_view demo) sets persist=False: it
        # neither claims the remembered port nor writes its own over it.
        # Measured on 2026-08-30: the demo left boss/jump.port naming a
        # port that died with the demo, so old toasts opened nothing.
        self.persist = persist
        self._server: asyncio.AbstractServer | None = None

    # -- the address ---------------------------------------------------------
    def _port_file(self) -> Path:
        return self.home / "boss" / "jump.port"

    def url_for(self, task_id: str) -> str:
        return f"http://{HOST}:{self.port}/s/{task_id}" if self.port else ""

    def url_for_action(self, rest: str) -> str:
        """A URL that presses one of the registered routes' buttons."""
        return f"http://{HOST}:{self.port}/a/{rest}" if self.port else ""

    def _remembered(self) -> int:
        if self.port:
            return self.port
        try:
            return int(self._port_file().read_text().strip())
        except (OSError, ValueError):
            return 0

    async def start(self) -> int:
        """Listen, preferring the port the last run used. Returns it."""
        wanted = self._remembered() if self.persist else 0
        for attempt in (wanted, 0):
            try:
                self._server = await asyncio.start_server(
                    self._client, HOST, attempt)
                break
            except OSError:
                if attempt == 0:
                    raise
                application_log("ui", "jump.port_taken",
                                f"port {attempt} is in use; choosing another",
                                severity="debug")
        sock = self._server.sockets[0]
        self.port = sock.getsockname()[1]
        if self.persist:
            self._port_file().parent.mkdir(parents=True, exist_ok=True)
            self._port_file().write_text(str(self.port))
        application_log("ui", "jump.listening",
                        f"toast links resolve on {HOST}:{self.port}",
                        severity="debug", port=self.port)
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    # -- the two paths -------------------------------------------------------
    async def _client(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            request = line.decode("latin-1", "replace")
            handled = await self._route(request, writer)
            if handled:
                return
            target = self._target(request)
            if target is None:
                await self._say(writer, "404 Not Found", "Nothing here",
                                "That link does not name a session.")
                return
            try:
                await self.focus(target)
            except Exception as exc:
                application_log("ui", "jump.focus_failed",
                                f"could not focus {target}",
                                severity="warning", exc_info=True,
                                task_id=target)
                await self._say(writer, "500 Internal Server Error",
                                "Could not open it",
                                f"{target} did not come forward: {exc}")
                return
            application_log("ui", "jump.focused", f"opened {target} from a "
                            "turn toast", severity="debug", task_id=target)
            await self._say(writer, "200 OK", "Opening",
                            f"Bringing {target} forward.")
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _route(self, request_line: str,
                     writer: asyncio.StreamWriter) -> bool:
        """A registered route, if this is one of theirs."""
        parts = request_line.split()
        if len(parts) < 2 or parts[0] != "GET":
            return False
        path = parts[1].split("?", 1)[0]
        for prefix, handler in self.routes.items():
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix):].strip("/")
            try:
                said = await handler(rest)
            except Exception as exc:
                await self._say(writer, "500 Internal Server Error",
                                "Did not work", str(exc)[:200])
                return True
            await self._say(writer, "200 OK", "Done", said or "Done.")
            return True
        return False

    @staticmethod
    def _target(request_line: str) -> str | None:
        parts = request_line.split()
        if len(parts) < 2 or parts[0] != "GET":
            return None
        path = parts[1].split("?", 1)[0]
        if not path.startswith("/s/"):
            return None
        task_id = path[3:].strip("/")
        # A task id and nothing else: this is reachable by anything on the
        # machine, and it should not be a way to make the conductor look
        # up arbitrary strings.
        if not task_id or len(task_id) > 64 or \
                not all(c.isalnum() or c in "_-" for c in task_id):
            return None
        return task_id

    @staticmethod
    async def _say(writer: asyncio.StreamWriter, status: str, title: str,
                   body: str) -> None:
        page = PAGE.format(title=title, body=body).encode()
        writer.write(f"HTTP/1.1 {status}\r\nContent-Type: text/html; "
                     f"charset=utf-8\r\nContent-Length: {len(page)}\r\n"
                     f"Connection: close\r\n\r\n".encode() + page)
        try:
            await writer.drain()
        except Exception:
            pass
