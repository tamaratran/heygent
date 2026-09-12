"""Quit in the overlay's menu ends the whole app, not the overlay alone.

Found on a fresh install: "Quit heygent" took the capsule and the bell
off the screen and left conduct.py, hotkey.py and the Boss running behind
them, still reconnecting to the voice API. The overlay reported
`quit_requested` on its stdout and exited; nobody read the report, and
nothing waited on the overlay going away, so the run carried on headless.

Now the overlay's request (or its death) sets Ui.quit_requested, and both
launchers' waits return on it, running the same shutdown ctrl-c runs.

Run with:  python3 -m unittest tests.test_quit_stops_everything -v
"""

from __future__ import annotations

import asyncio
import io
import re
import unittest
from pathlib import Path
from unittest import mock

import voice_agent
from voice_agent import HotkeyListener, Ui, VoiceAgent

HERE = Path(__file__).resolve().parent.parent


def make_ui() -> Ui:
    ui = Ui.__new__(Ui)
    ui.proc = mock.Mock()
    ui.state = ""
    ui.write_failed = False
    ui.quit_requested = asyncio.Event()
    return ui


def make_agent(ui: Ui) -> VoiceAgent:
    agent = VoiceAgent.__new__(VoiceAgent)
    agent.ui = ui
    agent.speaker = mock.Mock()
    agent.muted_turn = False
    agent._emit = mock.Mock()
    return agent


class OverlayReportTest(unittest.TestCase):
    def test_quit_requested_sets_the_event(self) -> None:
        async def run():
            ui = make_ui()
            agent = make_agent(ui)
            with mock.patch.object(voice_agent, "application_log") as logged:
                agent.handle_overlay_event({"event": "quit_requested"})
            self.assertTrue(ui.quit_requested.is_set())
            self.assertEqual(logged.call_args.args[1], "app.quit_requested")
        asyncio.run(run())

    def test_the_overlay_dying_sets_the_event(self) -> None:
        async def run_eof():
            ui = make_ui()
            agent = make_agent(ui)
            reader = asyncio.StreamReader()
            reader.feed_eof()
            with mock.patch.object(voice_agent, "application_log"):
                await agent._watch_overlay(reader)
            self.assertTrue(ui.quit_requested.is_set())
        asyncio.run(run_eof())

    def test_dismiss_still_mutes_the_turn(self) -> None:
        async def run():
            ui = make_ui()
            agent = make_agent(ui)
            with mock.patch.object(voice_agent, "log"), \
                    mock.patch("builtins.print"):
                agent.handle_overlay_event({"event": "dismiss"})
            self.assertTrue(agent.muted_turn)
            agent.speaker.flush.assert_called_once()
            self.assertFalse(ui.quit_requested.is_set())
        asyncio.run(run())


class WaitTest(unittest.TestCase):
    def test_ui_wait_returns_on_quit_with_the_session_still_running(self):
        async def run():
            ui = make_ui()
            session = asyncio.create_task(asyncio.sleep(30))
            asyncio.get_running_loop().call_later(
                0.01, ui.request_quit, "test")
            with mock.patch.object(voice_agent, "application_log"):
                await asyncio.wait_for(ui.wait_with(session), 5)
            self.assertFalse(session.done())
            session.cancel()
        asyncio.run(run())

    def test_ui_wait_raises_what_the_session_raised(self):
        async def run():
            ui = make_ui()

            async def boom():
                raise RuntimeError("mic gone")
            session = asyncio.create_task(boom())
            with self.assertRaises(RuntimeError):
                await ui.wait_with(session)
        asyncio.run(run())

    def test_hotkey_wait_returns_on_quit(self):
        async def run():
            ui = make_ui()
            agent = make_agent(ui)
            hotkey = HotkeyListener([], agent)
            session = asyncio.create_task(asyncio.sleep(30))
            asyncio.get_running_loop().call_later(
                0.01, ui.request_quit, "test")
            with mock.patch.object(voice_agent, "application_log"):
                await asyncio.wait_for(hotkey.wait_with(session), 5)
            self.assertFalse(session.done())
            session.cancel()
        asyncio.run(run())


class LaunchersTest(unittest.TestCase):
    """Both mains wait on the quit event, so the shutdown in their finally
    blocks (hotkey, overlay, Boss, lock) runs on it."""

    def test_the_no_hotkey_branch_waits_on_quit_too(self) -> None:
        for name in ("voice_agent.py", "conduct.py"):
            source = (HERE / name).read_text()
            self.assertIn("await ui.wait_with(session_task)", source, name)
            self.assertNotIn("            await session_task\n", source, name)

    def test_conduct_reads_the_overlay_once_and_forwards(self) -> None:
        source = (HERE / "conduct.py").read_text()
        self.assertIn("agent.handle_overlay_event(event)", source)
        self.assertIn('ui.request_quit("the overlay exited")', source)
        # Its own reader would raise beside main()'s on the same pipe.
        body = source[source.index("class ConductorVoice"):]
        self.assertIn("async def _watch_overlay", body)

    def test_dock_quit_ends_the_run_but_the_x_only_hides(self) -> None:
        """The Boss window's process is the app in the Dock: Quit there
        ends everything; closing the window keeps the conductor running
        (its x hides, the Dock icon brings it back)."""
        conduct = (HERE / "conduct.py").read_text()
        self.assertIn('ui.request_quit("the Boss window quit")', conduct)
        body = conduct[conduct.index("async def quit_with_window"):]
        self.assertLess(body.index("await process.wait()"),
                        body.index("ui.request_quit"))
        app_mac = (HERE / "conductor" / "app_mac.py").read_text()
        close = app_mac[app_mac.index("def windowShouldClose_"):]
        close = close[:close.index("\n    def ", 10)]
        self.assertIn("self.window.orderOut_(None)", close)
        self.assertIn("return False", close)

    def test_the_overlay_reports_before_it_leaves(self) -> None:
        source = (HERE / "overlay.py").read_text()
        body = source[source.index("def quitOverlay_"):]
        body = body[:body.index("\n    def ", 10)]
        self.assertLess(body.index('report(event="quit_requested")'),
                        body.index("terminate_"))


if __name__ == "__main__":
    unittest.main()
