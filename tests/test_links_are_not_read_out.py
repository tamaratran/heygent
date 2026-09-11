"""Links, file paths and long ids are not read out.

Asked 2026-09-11: "Make our Heygent not read out links cuz that's too
much text." What is said is the short human version - "PR 217", "it's
in your drafts" - and the link itself stays in the window, where it can
be clicked.

Nothing here plays audio or opens anything: the voice's websocket is a
recorder, and the page's opener is a list.

Run with:  python3 -m unittest tests.test_links_are_not_read_out -v
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from conductor.spoken_text import spoken_text

try:
    import voice_agent
except Exception:                       # the audio stack is not installed
    voice_agent = None

ROOT = Path(__file__).resolve().parent.parent


class WhatIsSaid(unittest.TestCase):
    def test_a_pull_request_link_is_its_number(self):
        self.assertEqual(
            spoken_text("Opened https://github.com/tamaratran/heygent/pull/217."),
            "Opened PR 217.")

    def test_a_link_after_the_thing_it_names_is_not_said_twice(self):
        self.assertEqual(
            spoken_text("Opened PR #217 "
                        "(https://github.com/tamaratran/heygent/pull/217)."),
            "Opened PR #217.")

    def test_a_link_on_its_own_line_is_for_clicking(self):
        self.assertEqual(
            spoken_text("The draft is saved.\n"
                        "https://mail.google.com/mail/u/0/#drafts/FMfcgzQX"),
            "The draft is saved.")

    def test_a_gmail_draft_is_your_drafts(self):
        self.assertEqual(
            spoken_text("It's in https://mail.google.com/mail/u/0/#drafts"),
            "It's in your drafts")

    def test_other_links_are_named_by_their_site(self):
        self.assertEqual(
            spoken_text("See https://docs.example.com/a/b?c=1 for details."),
            "See a link on example.com for details.")
        self.assertEqual(
            spoken_text("[the design doc](https://docs.google.com/document/d/1x/edit)"),
            "the design doc")

    def test_a_file_path_is_its_file_name(self):
        self.assertEqual(
            spoken_text("I changed "
                        "/Users/tamaratran/Downloads/heygent/conductor/plain_text.py "
                        "and tests/test_plain_text.py."),
            "I changed plain_text.py and test_plain_text.py.")

    def test_long_ids_and_shas_are_left_out(self):
        self.assertEqual(spoken_text("Merged as 57615bc0a9d."), "Merged.")
        self.assertEqual(
            spoken_text("Session 5e389f3a-f20c-4c23-9b34-63574577562e ended."),
            "Session ended.")
        self.assertEqual(spoken_text("Worker task_955a1c0c is still running."),
                         "Worker is still running.")
        self.assertEqual(spoken_text("Resumed task_955a1c0c."),
                         "Resumed the task.")

    def test_words_that_only_look_like_paths_or_ids_survive(self):
        for text in ("Tests pass and/or the build is green.",
                     "It shipped on 9/11/2026 to origin/main.",
                     "The error was ENOENT at line 1234567.",
                     "The log is conduct-console-heygent-57615bc.log.",
                     "Nothing to change here."):
            self.assertEqual(spoken_text(text), text)

    def test_the_link_is_all_there_is(self):
        self.assertEqual(spoken_text("https://github.com/a/b/pull/5"), "PR 5")


class TheVoiceSaysTheShortVersion(unittest.TestCase):
    """VoiceAgent.announce is the one place speech is made."""

    class Wire:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.closed = False

        async def send_json(self, frame: dict) -> None:
            self.sent.append(frame["content"])

    class Speaker:
        """Audible once for each utterance sent, then quiet."""
        def __init__(self, wire) -> None:
            self.wire, self.heard = wire, 0

        @property
        def speaking(self) -> bool:
            if self.heard < len(self.wire.sent):
                self.heard += 1
                return True
            return False

    @unittest.skipIf(voice_agent is None, "voice_agent needs the audio stack")
    def test_announce_sends_the_spoken_words_and_logs_what_was_written(self):
        agent = voice_agent.VoiceAgent.__new__(voice_agent.VoiceAgent)
        agent.speech_lock = asyncio.Lock()
        agent.holding = False
        agent.ws = wire = self.Wire()
        agent.speaker = self.Speaker(wire)
        events = []
        agent._emit = lambda kind, **fields: events.append((kind, fields))
        written = ("Opened PR 217 for the link fix.\n"
                   "https://github.com/tamaratran/heygent/pull/217")
        asyncio.run(agent.announce(written))
        self.assertEqual(wire.sent, ["Opened PR 217 for the link fix."])
        kinds = [kind for kind, _ in events]
        self.assertIn("voice.text_shortened", kinds)
        data = dict(events)["voice.text_shortened"]["data"]
        self.assertIn("pull/217", data["written"])


class TheLinkStaysInTheWindow(unittest.TestCase):
    def web(self):
        from conductor.app_web import CodexWeb
        web = CodexWeb.__new__(CodexWeb)
        web.opened = []
        web.open_url = web.opened.append
        return web

    def test_a_clicked_link_opens_in_the_browser(self):
        web = self.web()
        self.assertTrue(web.open_link("https://github.com/o/r/pull/217"))
        self.assertEqual(web.opened, ["https://github.com/o/r/pull/217"])

    def test_only_web_links_are_opened(self):
        web = self.web()
        for url in ("", "file:///etc/passwd", "javascript:alert(1)",
                    "/Applications/Calculator.app", "https://x.y/a b"):
            self.assertFalse(web.open_link(url), url)
        self.assertEqual(web.opened, [])

    def test_both_doors_open_links(self):
        from conductor import app_web
        source = (ROOT / "conductor/app_web.py").read_text()
        self.assertIn('post("/open_link"', app_web.PAGE)
        self.assertIn('path == "/open_link"', source)
        self.assertEqual(source.count('path == "/open_link"'), 2,
                         "the bridge and HTTP must both answer it")

    def test_the_page_draws_links_as_links(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not on PATH to run the script")
        import re
        from conductor.app_web import PAGE
        source = re.search(r"function links\(box, text\) \{.*?\n\}", PAGE,
                           re.S).group(0)
        script = """
        const posted = [];
        function post(path, body) { posted.push([path, body]); }
        const document = {
          createTextNode: (t) => ({ text: t }),
          createElement: (tag) => ({ tag: tag }),
        };
        const box = { kids: [], appendChild(n) { this.kids.push(n); } };
        """ + source + """
        links(box, "Opened PR 217 (https://github.com/o/r/pull/217).");
        const a = box.kids.find((k) => k.tag === "a");
        a.onclick({ preventDefault() {} });
        console.log(JSON.stringify({
          kids: box.kids.map((k) => k.text !== undefined ? k.text
                                                          : [k.tag, k.href]),
          posted: posted }));
        """
        done = subprocess.run([node, "-e", script], capture_output=True,
                              text=True)
        self.assertEqual(done.returncode, 0, done.stderr[:800])
        seen = json.loads(done.stdout)
        self.assertEqual(seen["kids"], [
            "Opened PR 217 (", ["a", "https://github.com/o/r/pull/217"], ")."])
        self.assertEqual(seen["posted"], [
            ["/open_link", {"url": "https://github.com/o/r/pull/217"}]])


class ThePromptsSayIt(unittest.TestCase):
    def test_the_boss_the_voice_and_the_worker_are_told(self):
        manager = (ROOT / "prompts/manager.md").read_text()
        self.assertIn('"PR 217"', manager)
        self.assertIn("its own line", manager)
        for name in ("voice_conductor.md", "voice_agent.md"):
            text = (ROOT / "prompts" / name).read_text()
            self.assertIn("Never read out a link", text, name)
            self.assertIn("in the window", text, name)
        self.assertIn("Never spell out a link",
                      (ROOT / "prompts/claude_worker.md").read_text())


if __name__ == "__main__":
    unittest.main()
