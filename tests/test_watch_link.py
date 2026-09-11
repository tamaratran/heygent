"""The watch link: the clickable line under a Boss reply that delegated.

    Boss reply --plain URL--> WatchLinkServer --focus_task--> the worker

The URL is visible in the Claude Code transcript (a terminal renders it
blue and clickable); the server behind it does exactly one thing per
click - focus that task's workspace - and the voice path never speaks
the line.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen

from conductor.claude_manager import _serialize
from conductor.task_types import Task
from conductor.watch_link import WatchLinkServer, without_watch_links


def _get(url: str) -> int:
    with urlopen(url, timeout=5) as response:
        return response.status


class WatchLinkServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.opened: list[str] = []
        self.seen = asyncio.Event()

        async def on_watch(task_id: str) -> None:
            self.opened.append(task_id)
            self.seen.set()

        self.server = WatchLinkServer(self.home, on_watch)
        await self.server.start()

    async def asyncTearDown(self) -> None:
        await self.server.stop()

    async def test_a_click_focuses_that_task(self) -> None:
        url = self.server.url_for("task_abc12")
        status = await asyncio.to_thread(_get, url)
        self.assertEqual(status, 204)
        await asyncio.wait_for(self.seen.wait(), 5)
        self.assertEqual(self.opened, ["task_abc12"])

    async def test_the_url_names_the_task(self) -> None:
        self.assertEqual(
            self.server.url_for("task_1"),
            f"http://127.0.0.1:{self.server.port}/watch/task_1")

    async def test_other_paths_do_nothing(self) -> None:
        for path in ("/", "/watch/", "/watch/a/b", "/quit"):
            status = await asyncio.to_thread(
                _get, f"http://127.0.0.1:{self.server.port}{path}")
            self.assertEqual(status, 204, path)
        await asyncio.sleep(0.05)
        self.assertEqual(self.opened, [])

    async def test_a_broken_callback_does_not_kill_the_server(self) -> None:
        async def explode(task_id: str) -> None:
            raise RuntimeError("boom")

        self.server.on_watch = explode
        await asyncio.to_thread(_get, self.server.url_for("task_x"))
        status = await asyncio.to_thread(_get, self.server.url_for("task_x"))
        self.assertEqual(status, 204)

    async def test_the_port_survives_a_restart(self) -> None:
        """Yesterday's transcript still holds links with yesterday's port;
        the next run listens there again so they keep opening sessions."""
        port = self.server.port
        await self.server.stop()

        async def on_watch(task_id: str) -> None:
            pass

        again = WatchLinkServer(self.home, on_watch)
        await again.start()
        try:
            self.assertEqual(again.port, port)
        finally:
            await again.stop()


class WithoutWatchLinksTest(unittest.TestCase):
    def test_the_link_line_is_not_spoken(self) -> None:
        reply = ("Started a worker on the login flake.\n"
                 "▶ Fix login flake — http://127.0.0.1:49321/watch/task_1")
        self.assertEqual(without_watch_links(reply),
                         "Started a worker on the login flake.")

    def test_every_link_line_goes(self) -> None:
        reply = ("Both are running.\n"
                 "▶ One — http://127.0.0.1:5/watch/task_1\n"
                 "▶ Two — http://127.0.0.1:5/watch/task_2")
        self.assertEqual(without_watch_links(reply), "Both are running.")

    def test_a_linkless_reply_is_untouched(self) -> None:
        self.assertEqual(without_watch_links("All done."), "All done.")


class SerializedTasksCarryTheLinkTest(unittest.TestCase):
    def test_a_task_result_names_its_watch_url(self) -> None:
        task = Task(id="task_1", project_id="p", title="t", goal="g")
        data = json.loads(_serialize(
            task, watch_url=lambda tid: f"http://x/watch/{tid}"))
        self.assertEqual(data["watch"], "http://x/watch/task_1")
        rows = json.loads(_serialize(
            [task], watch_url=lambda tid: f"http://x/watch/{tid}"))
        self.assertEqual(rows[0]["watch"], "http://x/watch/task_1")

    def test_without_a_host_there_is_no_link(self) -> None:
        task = Task(id="task_1", project_id="p", title="t", goal="g")
        self.assertNotIn("watch", json.loads(_serialize(task)))


if __name__ == "__main__":
    unittest.main()
