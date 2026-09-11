"""New chat starts an ordinary chat; the Boss's chat says so.

Every chat in the window was a Boss conversation: "New chat" opened
another orchestrator, when what the user wanted was a chat. Now the
window is given a way to make a plain coding session, New chat makes
one of those, and the two kinds sit in one list - so the Boss's own
chat is tagged in the sidebar, and clicking an old thread goes back to
whichever kind it was.

Run with:  python3 -m unittest tests.test_new_chat_is_a_chat -v
"""

from __future__ import annotations

import asyncio
import re
import tempfile
import unittest
from pathlib import Path

from conductor.app_web import PAGE, CodexWeb


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeApp:
    """What the page needs of a backend, and a record of what it was
    asked to do."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.model = name
        self.thread_id = None
        self.started = 0
        self.resumed: list[str] = []
        self.starts = 0

    async def start(self) -> None:
        self.starts += 1

    async def stop(self) -> None:
        pass

    async def start_thread(self, **options) -> None:
        self.started += 1
        self.thread_id = f"{self.name}-{self.started}"

    async def resume_thread(self, thread_id: str) -> None:
        self.resumed.append(thread_id)
        self.thread_id = thread_id

    async def interrupt(self) -> None:
        pass


class NewChatIsAPlainChat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.boss = FakeApp("boss")
        self.made: list[FakeApp] = []

        def plain():
            app = FakeApp("chat")
            self.made.append(app)
            return app

        self.web = CodexWeb(self.boss, home=Path(self.tmp.name), plain=plain)

    def test_new_chat_does_not_open_another_boss(self):
        run(self.web.switch_thread(None))
        self.assertEqual(self.boss.started, 0, "the Boss was started again")
        self.assertEqual(len(self.made), 1)
        self.assertEqual(self.made[0].started, 1)
        self.assertIs(self.web.app, self.made[0])

    def test_the_plain_backend_is_started_before_it_is_used(self):
        run(self.web.switch_thread(None))
        self.assertEqual(self.made[0].starts, 1)

    def test_a_second_new_chat_reuses_the_one_backend(self):
        run(self.web.switch_thread(None))
        run(self.web.switch_thread(None))
        self.assertEqual(len(self.made), 1, "a second process for one window")
        self.assertEqual(self.made[0].started, 2)

    def test_a_new_chat_is_remembered_as_a_chat(self):
        run(self.web.switch_thread(None))
        tid = self.web.app.thread_id
        self.assertEqual(self.web.kind_of(tid), "chat")
        self.assertEqual([t["kind"] for t in self.web.threads() if t["id"] == tid],
                         ["chat"])

    def test_without_a_factory_new_chat_is_what_it_always_was(self):
        """The page served on its own has one backend; New chat there is
        a fresh thread of it."""
        web = CodexWeb(self.boss, home=Path(self.tmp.name))
        run(web.switch_thread(None))
        self.assertEqual(self.boss.started, 1)
        self.assertIs(web.app, self.boss)

    def test_an_old_thread_goes_back_to_its_own_kind(self):
        run(self.web.switch_thread(None))          # a plain chat
        chat_id = self.web.app.thread_id
        self.web._titles[chat_id] = "the plain one"
        run(self.web.switch_thread("boss-thread"))  # a Boss conversation
        self.assertIs(self.web.app, self.boss)
        self.assertEqual(self.boss.resumed, ["boss-thread"])
        run(self.web.switch_thread(chat_id))
        self.assertIs(self.web.app, self.made[0])
        self.assertEqual(self.made[0].resumed, [chat_id])

    def test_the_kind_survives_the_window_closing(self):
        run(self.web.switch_thread(None))
        chat_id = self.web.app.thread_id
        self.web._titles[chat_id] = "kept"
        self.web._save_thread()
        again = CodexWeb(FakeApp("boss"), home=Path(self.tmp.name))
        self.assertEqual(again.kind_of(chat_id), "chat")

    def test_an_unknown_thread_is_the_boss_by_default(self):
        """Every thread was a Boss conversation before this existed."""
        self.assertEqual(self.web.kind_of("something-older"), "boss")
        self.assertEqual(self.web.kind_of(None), "boss")


class TheSidebarSaysWhichIsTheBoss(unittest.TestCase):
    def setUp(self):
        self.script = re.findall(r"<script>(.*?)</script>", PAGE, re.S)[-1]

    def test_there_is_a_tag_to_draw(self):
        self.assertIn("function bossTag()", self.script)
        self.assertIn('tag.textContent = "boss"', self.script)

    def test_the_open_chat_wears_it_only_when_it_is_the_boss(self):
        self.assertIn('if (!current || current.kind !== "chat") '
                      "top.appendChild(bossTag());", self.script)

    def test_the_other_rows_wear_it_by_their_kind(self):
        self.assertIn('if (t.kind !== "chat") other.appendChild(bossTag());',
                      self.script)

    def test_the_tag_has_a_look_of_its_own(self):
        self.assertIn(".thread .tag {", PAGE)


if __name__ == "__main__":
    unittest.main()
