"""Codex driven through its app-server, and drawn by us.

A CLI in a PTY owns its pixels. `codex app-server` is JSON-RPC over
stdio and renders nothing, so a front end can draw turns as cards, put
a linked toast under one, and answer an approval with a button - Codex
asks and BLOCKS, which is what makes a button possible.

The wire shapes here are from `codex app-server generate-json-schema`
and from a live 0.151 session on 2026-08-30 (initialize -> initialized
-> thread/start -> turn/start, then item/agentMessage/delta,
item/completed, turn/completed; approvals arrive as server REQUESTS).
tests/fakes/ stands in for the real server, which needs an account.

Run with:  python3 -m unittest tests.test_codex_app -v
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

from conductor import turn_toast
from conductor.codex_app import CodexApp, CodexUnavailable
from conductor.app_view import Card, ConversationView, Theme, _len, link

FAKES = Path(__file__).resolve().parent / "fakes"
ROWS = [{"task_id": "task_00eac099", "title": "Codex smoke test",
         "status": "waiting on you", "glyph": "attention",
         "url": "http://127.0.0.1:8977/s/task_00eac099"}]


def app(mode: str = "plain", **kwargs) -> CodexApp:
    os.environ["FAKE_MODE"] = mode
    os.environ["FAKE_PYTHON"] = sys.executable
    return CodexApp(cwd="/tmp", binary=str(FAKES / "codex"), **kwargs)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


class TheTransport(unittest.TestCase):
    def test_a_turn_streams_deltas_then_the_answer(self):
        async def go():
            client = app()
            await client.start()
            thread = await client.start_thread()
            events = [e async for e in client.turn("what year is it?")]
            await client.stop()
            return thread, events
        thread, events = run(go())
        self.assertTrue(thread)
        self.assertEqual("".join(e.text for e in events if e.kind == "delta"),
                         "2026")
        self.assertEqual([e.kind for e in events][-1], "turn_done")
        self.assertEqual(events[-1].text, "2026")

    def test_the_thread_carries_the_policy_a_button_needs(self):
        """Nothing asks, nothing to press: a front end that draws buttons
        wants Codex to be asking."""
        async def go():
            client = app()
            await client.start()
            await client.start_thread(approval_policy="untrusted",
                                      sandbox={"type": "readOnly"})
            await client.stop()
            return client
        client = run(go())
        self.assertEqual(client.model, "fake-model")

    def test_a_thread_can_be_resumed_by_id(self):
        async def go():
            client = app()
            await client.start()
            thread = await client.resume_thread("01a05536-dead-beef")
            await client.stop()
            return thread
        self.assertEqual(run(go()), "01a05536-dead-beef")

    def test_interrupt_names_the_turn_the_server_named(self):
        """Measured on a live 0.151 session on 2026-08-31: turn/interrupt
        with only a threadId is refused - "missing field 'turnId'" - and
        the turn runs on. The server names the turn in turn/started and
        wants it named back."""
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            [e async for e in client.turn("go")]
            sent = {}
            real = client._request

            async def spy(method, params, timeout=30.0):
                sent[method] = params
                return await real(method, params, timeout)
            client._request = spy
            await client.interrupt()
            await client.stop()
            return client.turn_id, sent
        turn_id, sent = run(go())
        self.assertTrue(turn_id)
        self.assertEqual(sent["turn/interrupt"],
                         {"threadId": "01a05536-5029-7f03-a98b-56096fe6a6b0",
                          "turnId": turn_id})

    def test_interrupt_before_any_turn_is_a_no_op(self):
        async def go():
            client = app()
            await client.start()
            await client.start_thread()
            await client.interrupt()      # nothing running: nothing sent
            await client.stop()
        run(go())

    def test_stopping_leaves_no_process_behind(self):
        async def go():
            client = app()
            await client.start()
            self.assertTrue(client.running)
            await client.stop()
            return client.running
        self.assertFalse(run(go()))

    def test_a_missing_binary_is_refused_not_half_started(self):
        async def go():
            client = CodexApp(cwd="/tmp", binary="/nonexistent/codex")
            with self.assertRaises(CodexUnavailable):
                await client.start()
            self.assertFalse(client.running)
        run(go())

    def test_a_server_that_never_answers_is_not_waited_on_for_ever(self):
        async def go():
            client = app("silent")
            from conductor import codex_app
            was = codex_app.START_TIMEOUT_S
            codex_app.START_TIMEOUT_S = 1.0
            try:
                with self.assertRaises(CodexUnavailable):
                    await client.start()
            finally:
                codex_app.START_TIMEOUT_S = was
            await client.stop()
        run(go())


class ApprovalsAreAnswered(unittest.TestCase):
    def test_a_button_settles_the_request_codex_is_blocked_on(self):
        held = []

        async def go():
            client = app("approval", on_approval=held.append)
            await client.start()
            await client.start_thread(approval_policy="untrusted")
            events = []
            async for event in client.turn("what year is it?"):
                events.append(event)
                if event.kind == "approval":
                    event.data["approval"].answer("accept")   # the button
            await client.stop()
            return events
        events = run(go())
        self.assertEqual([e.kind for e in events].count("approval"), 1)
        approval = held[0]
        self.assertEqual(approval.kind, "command")
        self.assertEqual(approval.question, "/bin/bash -lc 'date +%Y'")
        self.assertEqual(approval.detail, "outside the sandbox")
        self.assertTrue(approval.answered)
        self.assertIn("commandExecution", [e.item_type for e in events])
        self.assertEqual(events[-1].text, "2026")

    def test_declining_is_carried_too(self):
        async def go():
            client = app("approval")
            await client.start()
            await client.start_thread()
            out = []
            async for event in client.turn("go"):
                out.append(event)
                if event.kind == "approval":
                    event.data["approval"].answer("decline")
            await client.stop()
            return out
        self.assertIn("not run (decline)", run(go())[-1].text)

    def test_a_button_pressed_twice_is_one_answer(self):
        async def go():
            client = app("approval")
            await client.start()
            await client.start_thread()
            async for event in client.turn("go"):
                if event.kind == "approval":
                    event.data["approval"].answer("accept")
                    event.data["approval"].answer("decline")   # too late
            await client.stop()
        run(go())          # a second reply on the same id would wedge it

    def test_an_unknown_request_is_refused_so_the_turn_can_end(self):
        """A server request nobody answers blocks the turn for ever."""
        async def go():
            client = app("unknown")
            await client.start()
            await client.start_thread()
            events = [e async for e in client.turn("go")]
            await client.stop()
            return events
        self.assertIn("refused", run(go())[-1].text)

    def test_a_handler_that_raises_declines_rather_than_hangs(self):
        def boom(approval):
            raise RuntimeError("the front end fell over")

        async def go():
            client = app("approval", on_approval=boom)
            await client.start()
            await client.start_thread()
            events = [e async for e in client.turn("go")]
            await client.stop()
            return events
        self.assertIn("not run (decline)", run(go())[-1].text)

    def test_a_slow_human_is_not_a_timeout(self):
        """Measured on 2026-08-30: the demo crashed with TimeoutError
        after ten minutes at its own permission card, buttons still on
        screen. The event timeout is for a silent server; a server
        holding an approval is waiting, same as us."""
        held = []

        async def go():
            from conductor import codex_app
            was = codex_app.REQUEST_TIMEOUT_S
            codex_app.REQUEST_TIMEOUT_S = 0.2
            try:
                client = app("approval", on_approval=held.append)
                await client.start()
                await client.start_thread(approval_policy="untrusted")

                async def press_much_later():
                    await asyncio.sleep(0.7)     # three timeouts late
                    held[0].answer("accept")
                presser = asyncio.create_task(press_much_later())
                events = [e async for e in client.turn("go")]
                await presser
                await client.stop()
                return events
            finally:
                codex_app.REQUEST_TIMEOUT_S = was
        events = run(go())
        self.assertEqual(events[-1].kind, "turn_done")
        self.assertEqual(events[-1].text, "2026")


class WhatItLooksLike(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        turn_toast.write_sessions(self.home, ROWS)
        self.theme = Theme(colour=True, width=60)
        self.out: list[str] = []
        self.view = ConversationView(theme=self.theme, home=self.home,
                                     print_=self.out.append)

    @staticmethod
    def plain(text: str) -> str:
        text = re.sub(r"\x1b\]8;;[^\x1b]*\x1b\\", "", text)
        return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)

    def test_a_link_costs_no_width(self):
        self.assertEqual(_len("[ Accept ]"),
                         _len(link("http://x/a/1:accept", "[ Accept ]")))

    def test_a_card_with_buttons_still_lines_up(self):
        """Measured: wrapping by character count cut an OSC 8 link in
        half and the row came out as "acceptForSession[ Accept for
        session ]" over four lines."""
        card = Card(self.theme, "codex needs permission")
        card.add("/bin/bash -lc 'date +%Y'")
        card.add(" ".join(link(f"http://x/a/1:{d}", f"[ {d} ]")
                          for d in ("accept", "acceptForSession", "decline")))
        lines = self.plain(card.render()).splitlines()
        self.assertEqual(len(lines), 4, "the button row wrapped")
        self.assertEqual({len(l) for l in lines}, {self.theme.width})
        self.assertIn("[ accept ] [ acceptForSession ] [ decline ]", lines[2])

    def test_the_toast_names_the_session_and_links_it(self):
        self.view.toast("The Codex smoke test worker is still waiting on you.")
        self.assertEqual(len(self.out), 1)
        self.assertIn("Codex smoke test", self.plain(self.out[0]))
        self.assertIn("waiting on you", self.plain(self.out[0]))
        self.assertIn("http://127.0.0.1:8977/s/task_00eac099", self.out[0])

    def test_an_answer_about_nothing_gets_no_toast(self):
        self.view.toast("2026")
        self.assertEqual(self.out, [])

    def test_pressing_a_rendered_button_answers_the_approval(self):
        from conductor.codex_app import Approval
        decided = []
        approval = Approval(kind="command", item_id="exec_1", thread_id="t",
                            turn_id="u", question="rm -rf /", detail="",
                            _answer=decided.append)
        self.view.approval_card(approval)
        said = run(self.view.decide("exec_1:decline"))
        self.assertEqual(decided, ["decline"])
        self.assertIn("decline", said)

    def test_a_button_for_a_request_that_has_gone_says_so(self):
        said = run(self.view.decide("nobody:accept"))
        self.assertIn("no longer waiting", said)

    def test_the_card_says_how_a_button_is_pressed(self):
        """Measured on 2026-08-30: a user clicked Accept, nothing
        happened - terminals follow links on ⌘-click only, and nothing
        on screen said so."""
        from conductor.codex_app import Approval
        self.view.jump = type("Jump", (), {
            "url_for_action": staticmethod(
                lambda rest: f"http://127.0.0.1:1/a/{rest}")})()
        approval = Approval(kind="command", item_id="exec_2", thread_id="t",
                            turn_id="u", question="date +%Y", detail="",
                            _answer=lambda decision: None)
        self.view.approval_card(approval)
        self.assertIn("⌘-click", self.plain(self.out[-1]))

    def test_no_server_no_buttons_no_hint(self):
        from conductor.codex_app import Approval
        approval = Approval(kind="command", item_id="exec_3", thread_id="t",
                            turn_id="u", question="date +%Y", detail="",
                            _answer=lambda decision: None)
        self.view.approval_card(approval)      # self.view.jump is None
        self.assertNotIn("⌘-click", self.plain(self.out[-1]))


if __name__ == "__main__":
    unittest.main()
