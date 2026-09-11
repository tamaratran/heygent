"""The browser front end over the app-server (conductor/app_web.py).

Same wire as tests/test_codex_app.py - tests/fakes/ stands in for the
real server - but here the front end is a page, so these tests speak
HTTP to the loopback server the way the browser does: GET the page,
POST a prompt, press a button with POST /answer, and read the
conversation back off the event stream's history.

Run with:  python3 -m unittest tests.test_app_web -v
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from conductor import turn_toast
from conductor.codex_app import CodexApp
from conductor.app_web import CodexWeb, HOST

FAKES = Path(__file__).resolve().parent / "fakes"


def app(mode: str = "plain", **kwargs) -> CodexApp:
    os.environ["FAKE_MODE"] = mode
    os.environ["FAKE_PYTHON"] = sys.executable
    return CodexApp(cwd="/tmp", binary=str(FAKES / "codex"), **kwargs)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


async def http(port: int, method: str, path: str,
               body: dict | None = None) -> tuple[str, str]:
    """One request, the way the page makes it. Returns (status, body)."""
    reader, writer = await asyncio.open_connection(HOST, port)
    payload = json.dumps(body).encode() if body is not None else b""
    writer.write(
        f"{method} {path} HTTP/1.1\r\nHost: {HOST}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, rest = raw.decode("utf-8", "replace").partition("\r\n\r\n")
    return head.split("\r\n")[0].split(" ", 1)[1], rest


async def settled(web: CodexWeb, kind: str, timeout: float = 20.0) -> dict:
    """Wait until the history carries one of these, and return it."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        for message in web.history:
            if message["kind"] == kind:
                return message
        await asyncio.sleep(0.02)
    raise AssertionError(f"no {kind!r} in {[m['kind'] for m in web.history]}")


class ThePage(unittest.TestCase):
    def page(self):
        """The served page, for the tests that read what it draws."""
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            port = await web.start()
            _, page = await http(port, "GET", "/")
            await web.stop()
            await client.stop()
            return page
        return go()

    def test_the_page_is_served_and_carries_the_form(self):
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            port = await web.start()
            status, page = await http(port, "GET", "/")
            await web.stop()
            await client.stop()
            return status, page
        status, page = run(go())
        self.assertEqual(status, "200 OK")
        self.assertIn("EventSource", page)
        self.assertIn("Message the Boss", page)

    def test_the_page_follows_only_a_reader_at_the_bottom(self):
        """Measured on 2026-09-01: the chat yanked to the end on every
        streamed update, so the page could not be scrolled up while a
        worker ran. The page must carry the pinned guard the terminal
        view already had."""
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            port = await web.start()
            _, page = await http(port, "GET", "/")
            await web.stop()
            await client.stop()
            return page
        page = run(go())
        # Off for now (asked 2026-09-01): a fresh chat comes from the
        # voice or a restart, not a sidebar button.
        self.assertNotIn("newThread\").onclick", page)
        self.assertNotIn("<button id=\"newThread\"", page)
        self.assertIn("function follow", page)
        self.assertIn('addEventListener("scroll"', page)
        self.assertNotIn("streaming.scrollIntoView", page)
        # To the document's padded bottom, not the element's edge: new
        # turns were landing underneath the fixed composer (measured
        # 2026-09-01, "all crowded at the bottom of the screen").
        self.assertIn("window.scrollTo(0, document.body.scrollHeight)", page)
        self.assertNotIn('scrollIntoView({ block: "end" })',
                         page.split("function pollTerm")[0])

    def test_a_live_worker_is_a_real_terminal_not_a_poll(self):
        """The pane used to be a 350ms capture-pane repaint - a picture
        of a terminal, with a fought-over scrollbar. Now it is a real
        emulator fed by the session's PTY bytes (the way cmux draws its
        panes), so scrollback, colours, cursor and keys are its own."""
        page = run(self.page())
        self.assertNotIn("pollTerm", page)
        self.assertNotIn("setInterval(pollTerm", page)
        self.assertIn("new Terminal(", page)
        self.assertIn("FitAddon.FitAddon()", page)
        self.assertIn('post("/attach"', page)
        self.assertIn('post("/stdin"', page)
        self.assertIn('post("/resize"', page)
        self.assertIn('post("/detach"', page)
        self.assertIn('attachedId && attachedId !== taskId', page)
        self.assertIn('msg.kind === "term_data"', page)
        self.assertIn('msg.kind === "term_exit"', page)
        self.assertIn("term.write(fromB64(msg.data))", page)

    def test_a_dead_worker_shows_its_saved_last_screen(self):
        """A session that has ended has no PTY to attach; the pane falls
        back to the memento the conductor kept."""
        page = run(self.page())
        self.assertIn('api("/term/" + taskId)', page)
        self.assertIn("this is its last screen", page)

    def test_the_terminal_keeps_a_scrollbar_the_rest_of_the_app_hides(self):
        """`.app ::-webkit-scrollbar { display: none }` left the one
        view that must scroll with no sign that there was more."""
        page = run(self.page())
        self.assertIn(".app #termScreen::-webkit-scrollbar", page)
        self.assertIn("display: block", page.split(
            ".app #termScreen::-webkit-scrollbar")[1][:80])

    def test_clicking_the_screen_gives_the_session_the_keyboard(self):
        page = run(self.page())
        self.assertIn('termScreen.addEventListener("mousedown"', page)
        self.assertIn("term.focus()", page)

    def test_the_history_goes_through_the_same_bridge_as_everything_else(self):
        """#171 put the page in a WKWebView with no port: a bare fetch()
        reaches nothing there, so the worker's history would have been
        empty in the native window."""
        page = run(self.page())
        self.assertIn('api("/history/" + taskId)', page)
        self.assertNotIn('fetch("/history/', page)

    def test_the_native_bridge_answers_every_door_the_page_calls(self):
        """Two route tables for one page is a trap, and it sprang:
        /history was added to the HTTP router only, so a worker's
        scrollback was full in a browser and empty in the app - which
        is the one that ships. Whatever the page calls, the bridge must
        answer."""
        import re as _re
        from conductor.app_web import PAGE, WindowBridge

        called = set()
        for match in _re.finditer(r'(?:api|post)\(\s*"(/[^"]*)"', PAGE):
            called.add(match.group(1))
        for match in _re.finditer(r'(?:api|post)\(\s*"(/[^"]*)"\s*\+', PAGE):
            called.add(match.group(1))
        self.assertIn("/history/", called, "the page stopped asking for it")
        self.assertIn("/term/", called)

        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            bridge = WindowBridge(web)
            answers = {}
            for path in sorted(called):
                probe = path + "task_probe01" if path.endswith("/") else path
                reply = await bridge.handle(probe, {"task_id": "task_probe01"})
                answers[path] = reply
            await client.stop()
            return answers
        answers = run(go())
        unanswered = [path for path, reply in answers.items()
                      if isinstance(reply, dict)
                      and str(reply.get("error", "")).startswith("nothing at")]
        self.assertEqual(unanswered, [],
                         f"the app cannot reach: {unanswered}")

    def test_a_dropped_file_reaches_the_app_the_same_as_a_browser(self):
        """The guard above caught /drop reaching the HTTP router only.
        The page is the same page in both, so the reply must be too:
        post() hands back a parsed object either way, never a Response."""
        from conductor.app_web import PAGE, WindowBridge
        self.assertIn("reply.path", PAGE)
        self.assertNotIn("await reply.json()", PAGE)
        self.assertNotIn("await reply.text()", PAGE)

        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            with tempfile.TemporaryDirectory() as home:
                web = CodexWeb(client, home=Path(home))
                bridge = WindowBridge(web)
                good = await bridge.handle(
                    "/drop", {"name": "notes.txt",
                              "data": base64.b64encode(b"hello").decode()})
                empty = await bridge.handle("/drop", {"name": "x", "data": ""})
                kept = Path(good["path"]).read_bytes() \
                    if good.get("path") else b""
            await client.stop()
            return good, empty, kept
        good, empty, kept = run(go())
        self.assertTrue(good["ok"], good)
        self.assertEqual(kept, b"hello", "the drop was not written")
        self.assertFalse(empty["ok"])

    def test_the_boss_row_is_the_way_back_to_the_chat(self):
        """Clicking the top (unindented) row returns from a worker's
        terminal to the Boss conversation (asked for 2026-09-01)."""
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            port = await web.start()
            _, page = await http(port, "GET", "/")
            await web.stop()
            await client.stop()
            return page
        page = run(go())
        self.assertIn("row.onclick = closeTerm", page)
        # The status line is the form's child; shifting both doubled the
        # sidebar offset and centered "Working..." (measured 2026-09-01).
        self.assertNotIn(".side-open #status", page)

    def test_the_native_bridge_is_in_the_page_and_http_still_works(self):
        """Inside the WKWebView the page speaks the script-message
        bridge; in a browser the loopback path still works."""
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            port = await web.start()
            _, page = await http(port, "GET", "/")
            await web.stop()
            await client.stop()
            return page
        page = run(go())
        for needle in ("const NATIVE", "messageHandlers.bridge",
                       "__deliver", "function bridgeCall",
                       '{ path: "/ready" }', "open_term",
                       'new EventSource("/events")', 'api("/threads")'):
            self.assertIn(needle, page)

    def test_anything_else_is_a_404(self):
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            port = await web.start()
            status, _ = await http(port, "GET", "/nope")
            await web.stop()
            await client.stop()
            return status
        self.assertEqual(run(go()), "404 Not Found")


class AConversation(unittest.TestCase):
    def test_a_posted_prompt_streams_and_lands_as_a_turn(self):
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            port = await web.start()
            status, _ = await http(port, "POST", "/say",
                                   {"text": "what year is it?"})
            turn = await settled(web, "turn")
            await web.stop()
            await client.stop()
            return status, web.history, turn
        status, history, turn = run(go())
        self.assertEqual(status, "200 OK")
        kinds = [m["kind"] for m in history]
        self.assertIn("you", kinds)
        self.assertIn("delta", kinds)
        self.assertEqual("".join(m["text"] for m in history
                                 if m["kind"] == "delta"), "2026")
        self.assertEqual(turn["answer"], "2026")
        self.assertEqual(turn["model"], "fake-model")

    def test_the_history_carries_busy_on_and_off(self):
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            port = await web.start()
            await http(port, "POST", "/say", {"text": "go"})
            await settled(web, "turn")
            await web.stop()
            await client.stop()
            return [m["busy"] for m in web.history if m["kind"] == "state"]
        self.assertEqual(run(go()), [True, False])

    def test_the_turn_settles_before_busy_drops(self):
        # The page treats a busy=False with a bubble still streaming as
        # an interruption, so an ordinary turn must land first.
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            await web.start()
            await http(web.port, "POST", "/say", {"text": "go"})
            await settled(web, "turn")
            await web.stop()
            await client.stop()
            return [m["kind"] for m in web.history
                    if m["kind"] == "turn"
                    or (m["kind"] == "state" and not m["busy"])]
        self.assertEqual(run(go()), ["turn", "state"])

    def test_the_event_stream_replays_the_history(self):
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            port = await web.start()
            await http(port, "POST", "/say", {"text": "go"})
            await settled(web, "turn")
            reader, writer = await asyncio.open_connection(HOST, port)
            writer.write(b"GET /events HTTP/1.1\r\nHost: x\r\n\r\n")
            await writer.drain()
            got = []
            while not any(json.loads(line)["kind"] == "turn"
                          for line in got):
                line = await asyncio.wait_for(reader.readline(), 10)
                text = line.decode().strip()
                if text.startswith("data: "):
                    got.append(text[6:])
            writer.close()
            await web.stop()
            await client.stop()
            return [json.loads(line)["kind"] for line in got]
        kinds = run(go())
        self.assertEqual(kinds[0], "you")
        self.assertEqual(kinds[-1], "turn")


class TheActivity(unittest.TestCase):
    def test_a_turn_carries_what_the_terminal_would_show(self):
        async def go():
            client = app("work")
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            port = await web.start()
            await http(port, "POST", "/say", {"text": "go"})
            turn = await settled(web, "turn")
            await web.stop()
            await client.stop()
            return turn, web.history
        turn, history = run(go())
        rows = {row["type"]: row for row in turn["tools"]}
        self.assertEqual(rows["thought"],
                         {"type": "thought", "text": "Which year?",
                          "detail": "Which year?\nThe clock will say."})
        self.assertEqual(rows["command"],
                         {"type": "command", "text": "date +%Y",
                          "detail": "2026", "exit": 1})
        self.assertEqual(rows["edit"],
                         {"type": "edit", "text": "year.txt",
                          "detail": "add year.txt\n+2026"})
        self.assertEqual(rows["plan"],
                         {"type": "plan", "text": "plan \u00b7 1/2",
                          "detail": "\u2713 read the clock\n"
                                    "\u25cb write it down"})
        # The live stream carried each row as it happened, too.
        live = [m for m in history if m["kind"] == "tool"]
        self.assertEqual([m["type"] for m in live],
                         ["thought", "command", "edit", "plan"])


class TheTerminalView(unittest.TestCase):
    def test_the_capture_reaches_into_history(self):
        """Measured 2026-09-01: with only the visible screen captured
        there was nothing to scroll into, and the view's scrollbar was
        dead."""
        from unittest import mock
        from conductor.app_web import _capture_pane
        done = subprocess.CompletedProcess([], 0, stdout="a screen", stderr="")
        with mock.patch("conductor.app_web.subprocess.run",
                        return_value=done) as run_:
            html, alive = _capture_pane("task_x")
        self.assertTrue(alive)
        self.assertIn("a screen", html)
        argv = run_.call_args[0][0]
        self.assertIn("-S", argv)
        self.assertEqual(argv[argv.index("-S") + 1], "-2000")

    def test_a_dead_pane_falls_back_to_its_saved_last_screen(self):
        """The runtime saves the pane's final capture at destroy; the
        card then opens onto what the worker did, not onto "The session
        has ended." over nothing (measured 2026-09-01)."""
        from unittest import mock
        from conductor import tmux_runtime
        from conductor.app_web import _capture_pane
        with tempfile.TemporaryDirectory() as tmp:
            name = tmux_runtime.session_name("task_gone")
            with mock.patch.object(tmux_runtime, "TERMINALS_DIR",
                                   Path(tmp)):
                tmux_runtime.final_screen_path(name).write_text(
                    "the last thing it said")
                failed = subprocess.CompletedProcess([], 1, stdout="",
                                                     stderr="no session")
                with mock.patch("conductor.app_web.subprocess.run",
                                return_value=failed):
                    html, alive = _capture_pane("task_gone")
        self.assertFalse(alive)
        self.assertIn("the last thing it said", html)

    def test_no_pane_and_no_memento_is_simply_ended(self):
        from unittest import mock
        from conductor import tmux_runtime
        from conductor.app_web import _capture_pane
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tmux_runtime, "TERMINALS_DIR",
                                   Path(tmp)):
                failed = subprocess.CompletedProcess([], 1, stdout="",
                                                     stderr="no session")
                with mock.patch("conductor.app_web.subprocess.run",
                                return_value=failed):
                    html, alive = _capture_pane("task_gone")
        self.assertEqual((html, alive), ("", False))


class TheScrollbackSeed(unittest.TestCase):
    """The raw pipe-pane stream, chunked for the emulator to replay:
    an attached client only gets tmux's repaints from attach-time on,
    so the stream is the only scrollback there is (2026-09-01)."""

    def test_the_stream_is_chunked_from_where_you_left_off(self):
        import base64
        from unittest import mock
        from conductor import tmux_runtime
        from conductor.app_web import _stream_chunk
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tmux_runtime, "STREAMS_DIR", Path(tmp)):
                path = tmux_runtime.stream_path(
                    tmux_runtime.session_name("task_s"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("hello world")
                ok = subprocess.CompletedProcess([], 0, stdout="",
                                                 stderr="")
                with mock.patch("conductor.app_web.subprocess.run",
                                return_value=ok):
                    first = _stream_chunk("task_s", 0)
                    rest = _stream_chunk("task_s", first["next"])
        self.assertEqual(base64.b64decode(first["b64"]), b"hello world")
        self.assertTrue(first["have"])
        self.assertTrue(first["alive"])
        self.assertEqual(rest["b64"], "")
        self.assertEqual(rest["next"], first["next"])

    def test_no_stream_file_says_so_instead_of_pretending(self):
        from unittest import mock
        from conductor import tmux_runtime
        from conductor.app_web import _stream_chunk
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tmux_runtime, "STREAMS_DIR", Path(tmp)):
                out = _stream_chunk("task_gone", 0)
        self.assertEqual(out, {"b64": "", "next": 0, "have": False,
                               "alive": False})

    def test_the_seed_is_in_the_page(self):
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            port = await web.start()
            _, page = await http(port, "GET", "/")
            await web.stop()
            await client.stop()
            return page
        page = run(go())
        self.assertIn('"/stream/"', page)
        self.assertIn("Seed the scrollback", page)


class TheOneScrollbar(unittest.TestCase):
    def test_the_emulator_owns_the_only_scrollbar(self):
        """Measured 2026-09-01: the outer container's scrollbar stacked
        beside the emulator's own on the right edge. Live, the outer
        stops scrolling and the emulator's bar wears the app's style."""
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            port = await web.start()
            _, page = await http(port, "GET", "/")
            await web.stop()
            await client.stop()
            return page
        page = run(go())
        self.assertIn(".term-live #termScreen { overflow: hidden; }", page)
        self.assertIn('classList.add("term-live")', page)
        self.assertIn(".xterm-viewport::-webkit-scrollbar", page)


class TheBootingWorker(unittest.TestCase):
    def test_starting_is_not_ended(self):
        """Measured 2026-09-01: clicking a worker the moment it appeared
        showed "The session has ended." - the loading state wore the
        tombstone. A booting worker (no session, no last screen) gets
        Starting and a retried attach; a finished one keeps its
        tombstone."""
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            port = await web.start()
            _, page = await http(port, "GET", "/")
            await web.stop()
            await client.stop()
            return page
        page = run(go())
        self.assertIn("Starting…", page)
        self.assertIn("tryAttach", page)
        self.assertIn("!probe || !probe.html", page)


class ThePageParses(unittest.TestCase):
    def test_the_script_is_valid_javascript(self):
        """Measured 2026-09-01: a lone \\n in the Python PAGE literal
        collapsed into a real newline inside a JS string, the whole
        script block died at parse, and the window rendered a shell
        that answered nothing - no sidebar, no typing, no turns."""
        import shutil
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not on PATH to parse the script")
        from conductor.app_web import PAGE
        start = PAGE.index("<script>") + len("<script>")
        end = PAGE.rindex("</script>")
        with tempfile.NamedTemporaryFile("w", suffix=".js",
                                         delete=False) as handle:
            handle.write(PAGE[start:end])
            path = handle.name
        done = subprocess.run([node, "--check", path],
                              capture_output=True, text=True)
        os.unlink(path)
        self.assertEqual(done.returncode, 0, done.stderr[:800])


class TheSidebarSelection(unittest.TestCase):
    """Asked 2026-09-10: the Boss row stayed selected while a worker's
    terminal was open, and the worker rows were indented under it."""

    def test_the_selection_follows_the_pane(self):
        import re
        import shutil
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not on PATH to run the script")
        from conductor.app_web import PAGE
        source = re.search(r"function markSelected\(\) \{.*?\n\}", PAGE,
                           re.S).group(0)
        script = """
        class Classes { constructor() { this.s = new Set(); }
          toggle(c, on) { on ? this.s.add(c) : this.s.delete(c); }
          has(c) { return this.s.has(c); } }
        const boss = { classList: new Classes() };
        const rows = ["a", "b"].map((t) => ({ dataset: { task: t },
                                              classList: new Classes() }));
        const document = { getElementById: () => boss,
                           querySelectorAll: () => rows };
        let termId = null;
        """ + source + """
        const seen = [];
        const look = () => seen.push([boss.classList.has("off"),
          rows.map((r) => r.classList.has("current"))]);
        markSelected(); look();
        termId = "b"; markSelected(); look();
        termId = null; markSelected(); look();
        console.log(JSON.stringify(seen));
        """
        done = subprocess.run([node, "-e", script], capture_output=True,
                              text=True)
        self.assertEqual(done.returncode, 0, done.stderr[:800])
        self.assertEqual(json.loads(done.stdout), [
            [False, [False, False]],    # the Boss's chat: the Boss row
            [True, [False, True]],      # worker b's terminal: only b
            [False, [False, False]]])   # back to the Boss

    def test_a_worker_header_has_no_back_button(self):
        # Asked 2026-09-10. The Boss row and a double Esc still return.
        from conductor.app_web import PAGE
        self.assertNotIn("termBack", PAGE)
        self.assertIn("row.onclick = closeTerm", PAGE)
        self.assertIn("if (now - lastEsc < 600) { closeTerm();", PAGE)

    def test_the_rows_are_not_indented(self):
        from conductor.app_web import PAGE
        self.assertNotIn(".sess.kid", PAGE)
        self.assertNotIn('"sess kid"', PAGE)
        self.assertNotIn("margin: .05rem 0 0 1.05rem", PAGE)
        self.assertIn('row.id = "bossRow"', PAGE)
        self.assertIn("markSelected();", PAGE)


class TheWindowBridge(unittest.TestCase):
    """The same page over stdio: no HTTP, no port (2026-09-01)."""

    def bridge(self):
        from conductor.app_web import WindowBridge
        client = app()
        web = CodexWeb(client)
        bridge = WindowBridge(web)
        sent = []
        bridge._send = sent.append
        return client, web, bridge, sent

    def test_the_doors_answer_the_same_as_http(self):
        async def go():
            client, web, bridge, sent = self.bridge()
            await client.start()
            await client.start_thread()
            threads = await bridge.handle("/threads", {})
            said = await bridge.handle("/say", {"text": "hello"})
            queued = web._prompts.get_nowait()
            nothing = await bridge.handle("/nope", {})
            await client.stop()
            return threads, said, queued, nothing
        threads, said, queued, nothing = run(go())
        self.assertIn("threads", threads)
        self.assertEqual(said, {"ok": True})
        self.assertEqual(queued, "hello")
        self.assertIn("error", nothing)

    def test_ready_replays_history_and_follows_the_stream(self):
        async def go():
            client, web, bridge, sent = self.bridge()
            web.history.append({"kind": "you", "text": "before"})
            web._history_loaded = True
            await bridge._call({"path": "/ready"})
            web.push({"kind": "delta", "text": "after"})
            await asyncio.sleep(0.05)          # the pump task runs
            await bridge.stop()
            return sent
        sent = run(go())
        js = [e["js"] for e in sent if "js" in e]
        self.assertTrue(any("__deliver" in j and "before" in j for j in js))
        self.assertTrue(any("after" in j for j in js))

    def test_a_call_with_an_id_is_replied_to(self):
        async def go():
            client, web, bridge, sent = self.bridge()
            await client.start()
            await client.start_thread()
            await bridge._call({"id": 7, "path": "/threads", "body": {}})
            await client.stop()
            return sent
        sent = run(go())
        self.assertTrue(any("__reply(7" in e.get("js", "") for e in sent))

    def test_a_notification_opens_the_session_here_not_in_cmux(self):
        """Asked 2026-09-01: "we dont want cmux to pop up anymore"."""
        client, web, bridge, sent = self.bridge()
        bridge.open_task("task_abc", "Fix login")
        self.assertEqual(sent[0], {"raise": True})
        self.assertIn("open_term", sent[1]["js"])
        self.assertIn("task_abc", sent[1]["js"])

    def test_every_door_the_page_calls_exists_on_the_bridge(self):
        """Measured 2026-09-01: /history/ was merged after the bridge
        and never given a door, so the native window's worker view had
        no past to scroll ("the working agent scroll hasnt been fixed
        yet"). Every path the page's api()/post() names must answer."""
        import re
        from conductor.app_web import PAGE
        called = set(re.findall(r'(?:api|post)\("(/[a-z]+/?)"', PAGE))
        called.discard("/ready")           # handled before the doors

        async def go():
            client, web, bridge, sent = self.bridge()
            await client.start()
            await client.start_thread()
            missing = []
            for path in sorted(called):
                probe = path + ("task_x" if path.endswith("/") else "")
                data = await bridge.handle(probe, {})
                if str(data.get("error", "")).startswith("nothing at"):
                    missing.append(path)
            await client.stop()
            return missing
        self.assertEqual(run(go()), [])
        self.assertIn("/history/", called)   # the regex still sees it

    def test_the_beacons_are_answered_quietly(self):
        """/raised is the window's ack, not a request: logged, never
        replied to."""
        async def go():
            client, web, bridge, sent = self.bridge()
            await bridge._call({"path": "/raised"})
            await bridge.stop()
            return sent
        self.assertEqual([e for e in run(go()) if "js" in e], [])

    def test_the_placeholder_is_not_a_chat(self):
        """Measured 2026-09-01: a phantom "boss" row KeyErrored on every
        click, and a wordless current chat wore its raw session id."""
        client = app()
        web = CodexWeb(client)
        web._titles["boss"] = "boss"
        web._titles["thread_a"] = "thread_a"
        client.thread_id = "thread_a"
        rows = web.threads()
        self.assertEqual([r["id"] for r in rows], ["thread_a"])
        self.assertEqual(rows[0]["title"], "New chat")
        self.assertTrue(rows[0]["current"])


class TheButtons(unittest.TestCase):
    def test_a_pressed_button_settles_the_approval(self):
        async def go():
            client = app("approval")
            await client.start()
            await client.start_thread(approval_policy="untrusted")
            web = CodexWeb(client)
            port = await web.start()
            await http(port, "POST", "/say", {"text": "go"})
            card = await settled(web, "approval")
            status, _ = await http(port, "POST", "/answer",
                                   {"item_id": card["item_id"],
                                    "decision": "accept"})
            turn = await settled(web, "turn")
            await web.stop()
            await client.stop()
            return card, status, web.history, turn
        card, status, history, turn = run(go())
        self.assertEqual(card["question"], "/bin/bash -lc 'date +%Y'")
        self.assertEqual(card["detail"], "outside the sandbox")
        self.assertEqual(card["cwd"], "/tmp/somewhere")
        self.assertEqual(status, "200 OK")
        decisions = [m for m in history if m["kind"] == "decision"]
        self.assertEqual(decisions, [{"kind": "decision",
                                      "item_id": card["item_id"],
                                      "decision": "accept"}])
        self.assertEqual(turn["answer"], "2026")

    def test_declining_is_carried_too(self):
        async def go():
            client = app("approval")
            await client.start()
            await client.start_thread(approval_policy="untrusted")
            web = CodexWeb(client)
            port = await web.start()
            await http(port, "POST", "/say", {"text": "go"})
            card = await settled(web, "approval")
            await http(port, "POST", "/answer",
                       {"item_id": card["item_id"], "decision": "decline"})
            turn = await settled(web, "turn")
            await web.stop()
            await client.stop()
            return turn
        self.assertEqual(run(go())["answer"], "not run (decline)")

    def test_a_button_for_a_request_no_longer_waiting_is_a_404(self):
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            port = await web.start()
            status, _ = await http(port, "POST", "/answer",
                                   {"item_id": "gone", "decision": "accept"})
            await web.stop()
            await client.stop()
            return status
        self.assertEqual(run(go()), "404 Not Found")


class TheThreads(unittest.TestCase):
    def test_a_conversation_is_saved_and_listed(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                client = app()
                await client.start()
                await client.start_thread()
                web = CodexWeb(client, home=home)
                port = await web.start()
                await http(port, "POST", "/say", {"text": "what year is it?"})
                await settled(web, "turn")
                _, listing = await http(port, "GET", "/threads")
                saved = list((home / "web_threads").glob("*.json"))
                await web.stop()
                await client.stop()
                return json.loads(listing), saved, client.thread_id
        listing, saved, thread_id = run(go())
        self.assertEqual(listing["threads"], [
            {"id": thread_id, "title": "what year is it?",
             "kind": "boss", "current": True}])
        self.assertEqual([f.stem for f in saved], [thread_id])

    def test_an_untitled_thread_is_named_for_its_first_words(self):
        """Measured: the sidebar read "boss_4edd02c0" over a 31-line
        conversation - an untitled thread was named for its id, then that
        name was saved as its title, and the first-message rule never
        fired again because the id was "a title"."""
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                folder = home / "web_threads"
                folder.mkdir(parents=True)
                (folder / "boss_4edd02c0.json").write_text(json.dumps({
                    "title": "boss_4edd02c0",
                    "history": [{"kind": "state", "busy": False},
                                {"kind": "you", "text": "  check my   PRs "},
                                {"kind": "turn", "text": "Two are open."}]}))
                (folder / "boss.json").write_text(json.dumps({
                    "title": "", "history": [{"kind": "state", "busy": False}]}))
                client = app()
                await client.start()
                await client.start_thread()
                web = CodexWeb(client, home=home)
                port = await web.start()
                _, listing = await http(port, "GET", "/threads")
                await web.stop()
                await client.stop()
                return json.loads(listing)["threads"], client.thread_id
        threads, current = run(go())
        by_id = {t["id"]: t for t in threads}
        self.assertEqual(by_id["boss_4edd02c0"]["title"], "check my PRs")
        self.assertNotIn("boss", by_id, "an empty thread is not listed")
        self.assertIn(current, by_id)

    def test_the_page_follows_the_boss_from_the_placeholder(self):
        """The bridge restores history at app start, before the Boss
        exists: the thread is the "boss" placeholder. The page attaches
        later on the real session. Measured: 243 entries on disk, and the
        window said "What should we work on?"."""
        import types
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            folder = home / "web_threads"
            folder.mkdir(parents=True)
            (folder / "boss_real.json").write_text(json.dumps({
                "title": "check my PRs",
                "history": [{"kind": "you", "text": "check my PRs"},
                            {"kind": "turn", "text": "Two are open."}]}))
            (folder / "boss.json").write_text(json.dumps({
                "title": "", "history": [{"kind": "state", "busy": False}]}))
            client = types.SimpleNamespace(thread_id="boss", model="boss")
            web = CodexWeb(client, home=home)
            web._restore_history()          # app start: no Boss yet
            self.assertEqual(web.history, [])
            client.thread_id = "boss_real"   # the Boss opened, resumed
            web._restore_history()          # the page attached
            self.assertEqual([m["kind"] for m in web.history], ["you", "turn"])
            web._restore_history()          # and again: still that thread
            self.assertEqual(len(web.history), 2)
            client.thread_id = "boss"
            web._save_thread()
            self.assertEqual(json.loads((folder / "boss.json").read_text())
                             ["history"], [{"kind": "state", "busy": False}],
                             "the placeholder file is never written")

    def test_switching_threads_replays_that_conversation(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                client = app()
                await client.start()
                await client.start_thread()
                first = client.thread_id
                web = CodexWeb(client, home=home)
                port = await web.start()
                await http(port, "POST", "/say", {"text": "what year is it?"})
                await settled(web, "turn")
                queue: asyncio.Queue = asyncio.Queue()
                web._watchers.append(queue)
                status, _ = await http(port, "POST", "/thread",
                                       {"id": "thread_b"})
                reset = queue.get_nowait()
                emptied = list(web.history)
                await http(port, "POST", "/say", {"text": "and now?"})
                await settled(web, "turn")
                _, listing = await http(port, "GET", "/threads")
                await http(port, "POST", "/thread", {"id": first})
                replayed = list(web.history)
                await web.stop()
                await client.stop()
                return (status, reset, emptied, json.loads(listing),
                        replayed, client.thread_id, first)
        (status, reset, emptied, listing, replayed,
         thread_id, first) = run(go())
        self.assertEqual(status, "200 OK")
        self.assertEqual(reset["kind"], "reset")
        self.assertEqual(reset["history"], [])
        self.assertEqual(emptied, [])
        self.assertEqual([t["title"] for t in listing["threads"]],
                         ["what year is it?", "and now?"])
        self.assertEqual([t["current"] for t in listing["threads"]],
                         [False, True])
        self.assertEqual(thread_id, first)
        yours = [m["text"] for m in replayed if m["kind"] == "you"]
        self.assertEqual(yours, ["what year is it?"])

    def test_a_restart_replays_the_saved_transcript(self):
        """A fresh CodexWeb over the same home and thread starts from
        the thread's saved history, not from nothing."""
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                client = app()
                await client.start()
                await client.start_thread()
                web = CodexWeb(client, home=home)
                port = await web.start()
                await http(port, "POST", "/say", {"text": "what year is it?"})
                await settled(web, "turn")
                await web.stop()
                reborn = CodexWeb(client, home=home)   # a restart
                reborn._restore_history()
                await reborn.stop()
                await client.stop()
                return list(reborn.history)
        history = run(go())
        self.assertEqual([m["text"] for m in history if m["kind"] == "you"],
                         ["what year is it?"])
        self.assertTrue(any(m["kind"] == "turn" for m in history))

    def test_switching_mid_turn_is_refused(self):
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            port = await web.start()
            web.busy = True
            status, _ = await http(port, "POST", "/thread", {})
            await web.stop()
            await client.stop()
            return status
        self.assertEqual(run(go()), "409 Conflict")


class TheSessions(unittest.TestCase):
    def test_every_worker_session_is_listed(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                turn_toast.write_sessions(home, [
                    {"task_id": "2026", "title": "The year worker",
                     "status": "counting", "glyph": "working"}])
                client = app()
                await client.start()
                await client.start_thread()
                web = CodexWeb(client, home=home)
                web.list_cmux = lambda: []
                port = await web.start()
                _, body = await http(port, "GET", "/sessions")
                await web.stop()
                await client.stop()
                return json.loads(body)
        self.assertEqual(run(go())["rows"], [
            {"task_id": "2026", "title": "The year worker",
             "status": "counting", "glyph": "working"}])

    def test_no_home_means_no_sessions(self):
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            web.list_cmux = lambda: []
            port = await web.start()
            _, body = await http(port, "GET", "/sessions")
            await web.stop()
            await client.stop()
            return json.loads(body)
        self.assertEqual(run(go())["rows"], [])

    def test_a_workspace_the_file_forgot_still_gets_a_row(self):
        """The conductor's file restarts empty; the workspaces live on in
        cmux, and the sidebar lists what actually exists."""
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                turn_toast.write_sessions(home, [
                    {"task_id": "2026", "title": "The year worker",
                     "status": "counting", "glyph": "working"}])
                client = app()
                await client.start()
                await client.start_thread()
                web = CodexWeb(client, home=home)
                web.list_cmux = lambda: ["2026", "task_lost"]
                port = await web.start()
                _, body = await http(port, "GET", "/sessions")
                await web.stop()
                await client.stop()
                return json.loads(body)
        self.assertEqual(run(go())["rows"], [
            {"task_id": "2026", "title": "The year worker",
             "status": "counting", "glyph": "working"},
            {"task_id": "task_lost", "title": "task_lost",
             "status": "Still open", "glyph": "open"}])


class TheNames(unittest.TestCase):
    def test_a_session_once_named_keeps_its_name(self):
        """The sessions file trims and restarts empty; a workspace it
        forgot still shows the name it was given, never its raw id."""
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                turn_toast.write_sessions(home, [
                    {"task_id": "task_abc123", "title": "Fix the tests",
                     "status": "working away", "glyph": "working"}])
                client = app()
                await client.start()
                await client.start_thread()
                web = CodexWeb(client, home=home)
                web.list_cmux = lambda: ["task_abc123"]
                web.sessions()                    # the name is learned here
                turn_toast.write_sessions(home, [])   # ...and forgotten
                after_trim = web.sessions()
                # A fresh page (a restart) reads the names back off disk.
                other = CodexWeb(client, home=home)
                other.list_cmux = lambda: ["task_abc123"]
                after_restart = other.sessions()
                await client.stop()
                return after_trim, after_restart
        after_trim, after_restart = run(go())
        for rows in (after_trim, after_restart):
            self.assertEqual(rows, [
                {"task_id": "task_abc123", "title": "Fix the tests",
                 "status": "Still open", "glyph": "open"}])


class TheTerminal(unittest.TestCase):
    """The embedded worker terminal: the page shows the tmux pane itself
    and types into it, instead of sending the user to another app."""

    def test_the_pane_is_served_as_html(self):
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            port = await web.start()
            from conductor import app_web
            before = app_web._capture_pane
            app_web._capture_pane = \
                lambda task_id: (f"pane of {task_id}", True)
            try:
                status, body = await http(port, "GET", "/term/task_ab12")
            finally:
                app_web._capture_pane = before
                await web.stop()
                await client.stop()
            return status, json.loads(body)
        status, body = run(go())
        self.assertEqual(status, "200 OK")
        self.assertEqual(body, {"html": "pane of task_ab12", "alive": True})

    def test_a_key_goes_into_the_pane(self):
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            port = await web.start()
            from conductor import app_web
            sent = []
            before = app_web._send_to_pane
            app_web._send_to_pane = \
                lambda task_id, text, key: sent.append(
                    (task_id, text, key)) or True
            try:
                status, _ = await http(port, "POST", "/key",
                                       {"task_id": "task_ab12",
                                        "text": "hello"})
            finally:
                app_web._send_to_pane = before
                await web.stop()
                await client.stop()
            return status, sent
        status, sent = run(go())
        self.assertEqual(status, "200 OK")
        self.assertEqual(sent, [("task_ab12", "hello", "")])

    def test_a_bad_id_is_refused(self):
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            port = await web.start()
            try:
                shown, _ = await http(port, "GET", "/term/../etc/passwd")
                typed, _ = await http(port, "POST", "/key",
                                      {"task_id": "a;rm -rf",
                                       "text": "x"})
            finally:
                await web.stop()
                await client.stop()
            return shown, typed
        shown, typed = run(go())
        self.assertEqual(shown, "404 Not Found")
        self.assertEqual(typed, "404 Not Found")

    def test_the_boss_is_not_listed_as_a_worker(self):
        """tmux hosts the Boss too (cond_boss); only cond_task_* sessions
        are workers the sidebar should list."""
        import subprocess
        from unittest import mock

        from conductor import app_web
        done = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="cond_boss\ncond_task_ab12\nother\n")
        with mock.patch.object(app_web.subprocess, "run",
                               return_value=done):
            self.assertEqual(app_web._tmux_task_ids(), ["task_ab12"])

    def test_a_slow_tmux_is_not_a_dead_session(self):
        """capture-pane failing is tmux under load, not the session
        ending: only tmux's own word (has-session) ends it."""
        import subprocess
        from unittest import mock

        from conductor import app_web

        def failing_run(args, **kwargs):
            if args[1] == "capture-pane":
                raise subprocess.TimeoutExpired(args, 5)
            raise AssertionError("has-session is not asked on a timeout")

        with mock.patch.object(app_web.subprocess, "run", failing_run):
            self.assertEqual(app_web._capture_pane("task_ab12"),
                             ("", True))

    def test_a_failed_capture_asks_tmux_whether_the_session_lives(self):
        import subprocess
        from unittest import mock

        from conductor import app_web

        def run_for(alive):
            def fake_run(args, **kwargs):
                if args[1] == "capture-pane":
                    return subprocess.CompletedProcess(args, 1, stdout="",
                                                       stderr="no pane")
                self.assertEqual(args[1], "has-session")
                return subprocess.CompletedProcess(
                    args, 0 if alive else 1, stdout=b"", stderr=b"")
            return fake_run

        with mock.patch.object(app_web.subprocess, "run", run_for(True)):
            self.assertEqual(app_web._capture_pane("task_ab12"),
                             ("", True))
        with mock.patch.object(app_web.subprocess, "run", run_for(False)):
            self.assertEqual(app_web._capture_pane("task_ab12"),
                             ("", False))

    def test_the_capture_reaches_into_the_scrollback(self):
        """The pane alone is one screen; the reader scrolls up through
        what came before, so the capture asks for the history too."""
        import subprocess
        from unittest import mock

        from conductor import app_web
        asked = []

        def fake_run(args, **kwargs):
            asked.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="lines",
                                               stderr="")

        with mock.patch.object(app_web.subprocess, "run", fake_run):
            app_web._capture_pane("task_ab12")
        self.assertIn("-S", asked[0])
        self.assertEqual(asked[0][asked[0].index("-S") + 1], "-2000")

    def test_only_known_keys_are_sent(self):
        from conductor.app_web import _send_to_pane
        self.assertFalse(_send_to_pane("task_ab12", "", "x; rm -rf /"))
        self.assertFalse(_send_to_pane("task_ab12", "", ""))

    def test_the_colours_survive_the_translation(self):
        from conductor.app_web import ansi_html
        made = ansi_html("plain \x1b[31mred\x1b[0m \x1b[1;38;5;40mgreen\x1b[m"
                         " <script>")
        self.assertIn("plain ", made)
        self.assertIn(">red</span>", made)
        self.assertIn("font-weight:600", made)
        self.assertIn("&lt;script&gt;", made)
        self.assertNotIn("\x1b", made)

    def test_other_escapes_are_stripped(self):
        from conductor.app_web import ansi_html
        made = ansi_html("\x1b[2Jtop\x1b]0;title\x07 line\x1b[K")
        self.assertEqual(made, "top line")

    def test_every_open_host_is_listed_once(self):
        from conductor import app_web
        before = (app_web._cmux_task_ids, app_web._tmux_task_ids)
        app_web._cmux_task_ids = lambda: ["task_a", "task_b"]
        app_web._tmux_task_ids = lambda: ["task_b", "task_c"]
        try:
            self.assertEqual(app_web._open_task_ids(),
                             ["task_a", "task_b", "task_c"])
        finally:
            app_web._cmux_task_ids, app_web._tmux_task_ids = before


class TheMirror(unittest.TestCase):
    def test_a_spoken_turn_is_drawn_on_the_page(self):
        """The voice's turns reach the page as mirror calls: the prompt
        as a you line, the reply as a settled turn, and a mentioned
        session gets its delegation card."""
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                turn_toast.write_sessions(home, [
                    {"task_id": "2026", "title": "The year worker",
                     "status": "counting", "glyph": "working"}])
                client = app()
                await client.start()
                await client.start_thread()
                web = CodexWeb(client, home=home)
                port = await web.start()
                web.mirror_prompt("what year is it?")
                self_busy = web.busy
                web.mirror_answer("I asked The year worker.")
                toast = await settled(web, "toast")
                await web.stop()
                await client.stop()
                return self_busy, web.busy, web.history, toast, port
        busy, idle, history, toast, _ = run(go())
        self.assertTrue(busy)
        self.assertFalse(idle)
        kinds = [m["kind"] for m in history]
        self.assertIn("you", kinds)
        self.assertIn("turn", kinds)
        you = next(m for m in history if m["kind"] == "you")
        self.assertEqual(you["text"], "what year is it?")
        turn = next(m for m in history if m["kind"] == "turn")
        self.assertEqual(turn["answer"], "I asked The year worker.")
        self.assertEqual(toast["rows"][0]["task_id"], "2026")

    def test_the_boss_reply_is_drawn_as_it_is_written(self):
        """Paragraphs arrive as deltas while the turn is on the page; the
        settled turn follows. With no turn on the page, nothing is drawn."""
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client, home=None)
            web.mirror_delta("stray prose")          # no turn on the page
            web.mirror_prompt("start a worker")
            web.mirror_delta("Starting a worker.")
            web.mirror_delta("  ")
            web.mirror_delta("It is up.")
            web.mirror_answer("Starting a worker.\n\nIt is up.")
            await client.stop()
            return web.history
        history = run(go())
        kinds = [m["kind"] for m in history if m["kind"] != "state"]
        self.assertEqual(kinds, ["you", "delta", "delta", "turn"])
        deltas = [m["text"] for m in history if m["kind"] == "delta"]
        self.assertEqual(deltas, ["Starting a worker.\n\n", "It is up.\n\n"])


class TheBossApp(unittest.TestCase):
    def test_a_typed_turn_goes_to_the_conductor(self):
        from conductor.app_web import BossApp

        class FakeTurn:
            reply = "The year is 2026."
            folded = False

        class FakeConductor:
            current_boss_session_id = "boss_1"

            def __init__(self):
                self.heard = []

            async def handle_user_message(self, text, source=""):
                self.heard.append((text, source))
                return FakeTurn()

        async def go():
            conductor = FakeConductor()
            boss = BossApp(conductor)
            events = [e async for e in boss.turn("what year?")]
            return conductor, boss, events
        conductor, boss, events = run(go())
        self.assertEqual(conductor.heard, [("what year?", "text")])
        self.assertEqual(boss.thread_id, "boss_1")
        self.assertEqual([(e.kind, e.text) for e in events],
                         [("turn_done", "The year is 2026.")])

    def test_a_folded_turn_says_nothing(self):
        from conductor.app_web import BossApp

        class FakeTurn:
            reply = "answered on the earlier turn"
            folded = True

        class FakeConductor:
            async def handle_user_message(self, text, source=""):
                return FakeTurn()

        async def go():
            boss = BossApp(FakeConductor())
            return [e async for e in boss.turn("and?")]
        events = run(go())
        self.assertEqual([(e.kind, e.text) for e in events],
                         [("turn_done", "")])

    def test_new_chat_opens_a_fresh_conversation(self):
        from conductor.boss_session import BossSession, BossSessionStore
        from conductor.app_web import BossApp

        class FakeManager:
            def __init__(self):
                self.rebound = 0

            def rebind(self):
                self.rebound += 1

        class FakeConductor:
            def __init__(self, store):
                self.boss_store = store
                self.manager = FakeManager()

            def new_conversation(self):
                return self.boss_store.new_conversation()

        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                store = BossSessionStore(tmp)
                conv = store.current_conversation()
                store.save(BossSession(id="boss_1", conversation_id=conv))
                store.bind(conv, "boss_1")
                conductor = FakeConductor(store)
                boss = BossApp(conductor)
                await boss.start_thread()
                fresh = store.current_boss()
                await boss.resume_thread("boss_1")
                back = store.current_boss().id
                return conductor.manager.rebound, fresh, back
        rebound, fresh, back = run(go())
        self.assertEqual(rebound, 2)
        self.assertIsNone(fresh)          # a new chat has no Boss yet
        self.assertEqual(back, "boss_1")  # the old chat is its old Boss


class TheToast(unittest.TestCase):
    def test_a_mentioned_session_gets_a_linked_toast(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                turn_toast.write_sessions(home, [
                    {"task_id": "2026", "title": "The year worker",
                     "status": "counting", "glyph": "working"}])
                client = app()
                await client.start()
                await client.start_thread()
                web = CodexWeb(client, home=home)
                port = await web.start()
                await http(port, "POST", "/say", {"text": "what year?"})
                toast = await settled(web, "toast")
                await web.stop()
                await client.stop()
                return toast
        toast = run(go())
        self.assertEqual(toast["rows"], [
            {"task_id": "2026", "title": "The year worker",
             "status": "counting", "glyph": "working"}])

    def test_a_delegated_session_reports_back_to_the_bar(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                turn_toast.write_sessions(home, [
                    {"task_id": "2026", "title": "The year worker",
                     "status": "counting", "glyph": "working"}])
                client = app()
                await client.start()
                await client.start_thread()
                web = CodexWeb(client, home=home)
                web.poll = 0.05
                port = await web.start()
                await http(port, "POST", "/say", {"text": "what year?"})
                await settled(web, "toast")
                turn_toast.write_sessions(home, [
                    {"task_id": "2026", "title": "The year worker",
                     "status": "finished", "glyph": "done"}])
                update = await settled(web, "session")
                await web.stop()
                await client.stop()
                return update
        update = run(go())
        self.assertEqual(update, {"kind": "session", "task_id": "2026",
                                  "title": "The year worker",
                                  "status": "finished", "glyph": "done"})

    def test_the_boss_never_appears_as_a_worker_row(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                turn_toast.write_sessions(home, [
                    {"task_id": "boss", "title": "boss",
                     "status": "open", "glyph": "working"},
                    {"task_id": "task_a", "title": "A worker",
                     "status": "running", "glyph": "working"}])
                client = app()
                await client.start()
                await client.start_thread()
                web = CodexWeb(client, home=home)
                web.list_cmux = lambda: []
                port = await web.start()
                _, body = await http(port, "GET", "/sessions")
                await web.stop()
                await client.stop()
                return json.loads(body)
        rows = run(go())["rows"]
        self.assertEqual([r["task_id"] for r in rows], ["task_a"])

    def test_working_rows_sit_above_settled_ones(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                turn_toast.write_sessions(home, [
                    {"task_id": "task_done", "title": "Settled",
                     "status": "finished", "glyph": "done"},
                    {"task_id": "task_live", "title": "Live",
                     "status": "running", "glyph": "working"}])
                client = app()
                await client.start()
                await client.start_thread()
                web = CodexWeb(client, home=home)
                web.list_cmux = lambda: []
                port = await web.start()
                _, body = await http(port, "GET", "/sessions")
                await web.stop()
                await client.stop()
                return json.loads(body)
        rows = run(go())["rows"]
        self.assertEqual([r["task_id"] for r in rows],
                         ["task_live", "task_done"])

    def test_a_worker_gets_its_card_the_moment_it_starts(self):
        """The card does not wait for a turn whose answer names the
        session: the sessions file gaining a task id is the worker
        starting, and the card follows at once."""
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                turn_toast.write_sessions(home, [])
                client = app()
                await client.start()
                await client.start_thread()
                web = CodexWeb(client, home=home)
                web.poll = 0.05
                await web.start()
                await asyncio.sleep(0.2)     # the watcher seeds itself
                turn_toast.write_sessions(home, [
                    {"task_id": "task_new", "title": "The new worker",
                     "status": "starting", "glyph": "working"}])
                toast = await settled(web, "toast")
                await web.stop()
                await client.stop()
                return toast
        toast = run(go())
        self.assertEqual(toast["rows"], [
            {"task_id": "task_new", "title": "The new worker",
             "status": "starting", "glyph": "working"}])

    def test_a_carded_session_is_not_carded_again_by_the_spawn_watch(self):
        """One delegation, one card: a session a turn's answer already
        drew (it is in delegated) gets nothing from the spawn watcher,
        however new its task id looks to it."""
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                turn_toast.write_sessions(home, [])
                client = app()
                await client.start()
                await client.start_thread()
                web = CodexWeb(client, home=home)
                web.poll = 0.05
                await web.start()
                await asyncio.sleep(0.2)     # the watcher seeds itself
                web.delegated["task_seen"] = {"status": "starting",
                                              "glyph": "working"}
                turn_toast.write_sessions(home, [
                    {"task_id": "task_seen", "title": "Already carded",
                     "status": "starting", "glyph": "working"},
                    {"task_id": "task_new", "title": "The new worker",
                     "status": "starting", "glyph": "working"}])
                toast = await settled(web, "toast")
                await web.stop()
                await client.stop()
                return toast
        toast = run(go())
        self.assertEqual([r["task_id"] for r in toast["rows"]],
                         ["task_new"])

    def test_the_toast_link_focuses_the_session(self):
        focused = []

        async def focus(task_id: str) -> None:
            focused.append(task_id)

        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client, focus=focus)
            port = await web.start()
            ok, _ = await http(port, "GET", "/s/task_00eac099")
            bad, _ = await http(port, "GET", "/s/../etc/passwd")
            await web.stop()
            await client.stop()
            return ok, bad
        ok, bad = run(go())
        self.assertEqual(ok, "200 OK")
        self.assertEqual(bad, "404 Not Found")
        self.assertEqual(focused, ["task_00eac099"])

    def test_show_terminal_tells_every_page_to_open_it(self):
        """A notification's deep link: the session's terminal is drawn in
        the window itself, so opening it is a message to the page - live
        only, never replayed from history."""
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            web._session_names["task_00eac099"] = "Count the PRs"
            queue: asyncio.Queue = asyncio.Queue()
            web._watchers.append(queue)
            web.show_terminal("task_00eac099")
            sent = queue.get_nowait()
            history = list(web.history)
            await client.stop()
            return sent, history
        sent, history = run(go())
        self.assertEqual(sent, {"kind": "focus_session",
                                "task_id": "task_00eac099",
                                "title": "Count the PRs"})
        self.assertEqual(history, [])

    def test_a_toast_link_without_a_focus_opens_the_terminal_here(self):
        """No focus callable means the window IS the surface: the link
        opens the session's embedded terminal instead of doing nothing."""
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            queue: asyncio.Queue = asyncio.Queue()
            web._watchers.append(queue)
            port = await web.start()
            status, page = await http(port, "GET", "/s/task_00eac099")
            sent = queue.get_nowait()
            await web.stop()
            await client.stop()
            return status, page, sent
        status, page, sent = run(go())
        self.assertEqual(status, "200 OK")
        self.assertIn("Bringing task_00eac099 forward", page)
        self.assertEqual(sent["kind"], "focus_session")
        self.assertEqual(sent["task_id"], "task_00eac099")


class TheLiveTerminal(unittest.TestCase):
    """TermStreams: one real tmux client on a PTY per open worker pane,
    its bytes streamed to the page, keys and resizes written back."""

    NAME = "cond_task_termtest"
    TASK = "task_termtest"

    def setUp(self):
        if subprocess.run(["tmux", "-V"], capture_output=True).returncode:
            self.skipTest("no tmux on this machine")
        subprocess.run(["tmux", "kill-session", "-t", self.NAME],
                       capture_output=True)

    def tearDown(self):
        subprocess.run(["tmux", "kill-session", "-t", self.NAME],
                       capture_output=True)

    def _session(self):
        subprocess.run(["tmux", "new-session", "-d", "-s", self.NAME,
                        "-x", "80", "-y", "24", "cat"], check=True)

    def test_a_missing_session_refuses_the_attach(self):
        from conductor.app_web import TermStreams

        async def go():
            streams = TermStreams(lambda message: None)
            missing = await streams.attach("task_never_was", 80, 24)
            unsafe = await streams.attach("task_/etc", 80, 24)
            await streams.close()
            return missing, unsafe
        missing, unsafe = run(go())
        self.assertFalse(missing)
        self.assertFalse(unsafe)

    def test_bytes_flow_both_ways_and_the_exit_is_told(self):
        """Attach a real client, type into it, read the echo back as
        term_data, and see term_exit when the session dies."""
        from conductor.app_web import TermStreams
        self._session()

        async def go():
            sent = []
            streams = TermStreams(sent.append)
            self.assertTrue(await streams.attach(self.TASK, 80, 24))
            # cat echoes the typed line back through the pane.
            self.assertTrue(streams.write(self.TASK, b"marco\r"))

            async def until(kind, holds, timeout=10.0):
                deadline = asyncio.get_running_loop().time() + timeout
                while asyncio.get_running_loop().time() < deadline:
                    for message in sent:
                        if message["kind"] == kind and holds(message):
                            return message
                    await asyncio.sleep(0.05)
                raise AssertionError(
                    f"no {kind}: {[m['kind'] for m in sent]}")

            def carries_marco(message):
                import base64 as b64
                return b"marco" in b64.b64decode(message["data"])

            echoed = await until("term_data", carries_marco)
            self.assertEqual(echoed["task_id"], self.TASK)
            self.assertTrue(streams.resize(self.TASK, 120, 40))
            subprocess.run(["tmux", "kill-session", "-t", self.NAME],
                           check=True)
            exited = await until("term_exit", lambda m: True)
            self.assertEqual(exited["task_id"], self.TASK)
            self.assertFalse(streams.write(self.TASK, b"x"))
            self.assertFalse(streams.resize(self.TASK, 80, 24))
            await streams.close()
        run(go())

    def test_detach_lets_the_session_go_on_without_a_client(self):
        from conductor.app_web import TermStreams
        self._session()

        async def go():
            sent = []
            streams = TermStreams(sent.append)
            self.assertTrue(await streams.attach(self.TASK, 80, 24))
            await streams.detach(self.TASK)
            self.assertFalse(streams.write(self.TASK, b"x"))
            await asyncio.sleep(0.3)
            self.assertNotIn("term_exit", [m["kind"] for m in sent])
            await streams.close()
        run(go())
        alive = subprocess.run(["tmux", "has-session", "-t", self.NAME],
                               capture_output=True)
        self.assertEqual(alive.returncode, 0)


class ADroppedFile(unittest.TestCase):
    """Dragging a file into the box, the way Claude Code takes one.

    Measured 2026-09-01: nothing in the page touched a drag, so a file
    dropped on the Boss window was WebKit's to handle - it left the
    conversation for the file. The page takes every drop now, and a
    browser, which is never told the file's path, posts the bytes here.
    """

    def drop(self, name: str, raw: bytes, home: Path):
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client, home=home)
            port = await web.start()
            status, body = await http(port, "POST", "/drop", {
                "name": name,
                "data": base64.b64encode(raw).decode()})
            await web.stop()
            await client.stop()
            return status, body
        return run(go())

    def test_the_bytes_are_kept_and_the_path_comes_back(self):
        with tempfile.TemporaryDirectory() as home:
            status, body = self.drop("shot.png", b"\x89PNG\r\n\x1a\nhello",
                                     Path(home))
            self.assertEqual(status, "200 OK")
            kept = Path(json.loads(body)["path"])
            self.assertEqual(kept.parent, Path(home) / "drops")
            self.assertEqual(kept.name, "shot.png")
            self.assertEqual(kept.read_bytes(), b"\x89PNG\r\n\x1a\nhello")

    def test_a_second_file_of_the_same_name_does_not_eat_the_first(self):
        with tempfile.TemporaryDirectory() as home:
            _, first = self.drop("shot.png", b"one", Path(home))
            _, again = self.drop("shot.png", b"two", Path(home))
            one, two = json.loads(first)["path"], json.loads(again)["path"]
            self.assertNotEqual(one, two)
            self.assertEqual(Path(one).read_bytes(), b"one")
            self.assertEqual(Path(two).read_bytes(), b"two")

    def test_a_name_cannot_climb_out_of_the_folder(self):
        with tempfile.TemporaryDirectory() as home:
            status, body = self.drop("../../../etc/passwd", b"x", Path(home))
            self.assertEqual(status, "200 OK")
            kept = Path(json.loads(body)["path"])
            self.assertEqual(kept.parent, Path(home) / "drops")
            self.assertEqual(kept.name, "passwd")

    def test_a_drop_with_nothing_in_it_is_refused(self):
        with tempfile.TemporaryDirectory() as home:
            status, _ = self.drop("empty.png", b"", Path(home))
            self.assertEqual(status, "400 Bad Request")

    def test_a_body_far_bigger_than_a_message_still_arrives(self):
        """A message body is capped at 1 MB; a dropped file is not."""
        with tempfile.TemporaryDirectory() as home:
            raw = os.urandom(2 << 20)
            status, body = self.drop("big.bin", raw, Path(home))
            self.assertEqual(status, "200 OK")
            self.assertEqual(Path(json.loads(body)["path"]).read_bytes(), raw)

    def test_the_page_takes_every_drop_itself(self):
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            web = CodexWeb(client)
            port = await web.start()
            _, page = await http(port, "GET", "/")
            await web.stop()
            await client.stop()
            return page
        page = run(go())
        # Without preventDefault on dragover there is no drop at all, and
        # without it on drop the window navigates to the file.
        self.assertIn('addEventListener("dragover"', page)
        self.assertIn('addEventListener("drop"', page)
        self.assertIn("window.insertPaths = insertPaths", page)
        self.assertIn('post("/drop"', page)
        # Only a file is ours: text dragged into the box is still the
        # page's own drop, and preventDefault would swallow it.
        self.assertIn("if (!droppedFiles(event)) return;", page)
