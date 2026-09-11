"""A turn end is seen when it is written, not on the next poll.

_TranscriptWakeup waits on the transcript file itself (kqueue,
EVFILT_VNODE) with the poll interval as the ceiling, so the watcher
reads a finished turn the moment Claude Code writes it.

Run with:  python3 -m unittest tests.test_transcript_wakeup -v
"""

from __future__ import annotations

import os
import select
import tempfile
import threading
import time
import unittest
from pathlib import Path

from conductor.tmux_runtime import _TranscriptWakeup

HAS_KQUEUE = hasattr(select, "kqueue")


class TranscriptWakeup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "session.jsonl"
        self.path.write_text("")
        self.wakeup = _TranscriptWakeup()

    def tearDown(self):
        self.wakeup.close()
        self.tmp.cleanup()

    @unittest.skipUnless(HAS_KQUEUE, "kqueue is not available here")
    def test_a_write_wakes_the_wait_before_the_timeout(self):
        def append_soon():
            time.sleep(0.05)
            with self.path.open("a") as f:
                f.write('{"type":"assistant"}\n')
        threading.Thread(target=append_soon).start()
        started = time.monotonic()
        self.wakeup.wait(self.path, 5.0)
        self.assertLess(time.monotonic() - started, 2.0,
                        "the wait slept out its timeout instead of waking")

    @unittest.skipUnless(HAS_KQUEUE, "kqueue is not available here")
    def test_a_write_during_a_read_is_not_lost(self):
        # The kernel keeps events raised between waits, so a line that
        # lands while the watcher is reading is seen on the next wait.
        self.wakeup.wait(self.path, 0.01)          # armed, timed out
        with self.path.open("a") as f:
            f.write('{"type":"assistant"}\n')      # nobody is waiting
        started = time.monotonic()
        self.wakeup.wait(self.path, 5.0)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_no_transcript_means_a_plain_sleep(self):
        started = time.monotonic()
        self.wakeup.wait(None, 0.02)
        self.assertGreaterEqual(time.monotonic() - started, 0.02)

    def test_a_missing_file_means_a_plain_sleep(self):
        started = time.monotonic()
        self.wakeup.wait(Path(self.tmp.name) / "not-yet.jsonl", 0.02)
        self.assertGreaterEqual(time.monotonic() - started, 0.02)

    @unittest.skipUnless(HAS_KQUEUE, "kqueue is not available here")
    def test_a_replaced_file_is_watched_again(self):
        self.wakeup.wait(self.path, 0.01)          # armed on the old file
        os.rename(self.path, self.path.with_suffix(".old"))
        self.path.write_text("")                   # a new file at the path
        self.wakeup.wait(self.path, 0.01)          # rename noticed; re-arms
        def append_soon():
            time.sleep(0.05)
            with self.path.open("a") as f:
                f.write('{"type":"assistant"}\n')
        threading.Thread(target=append_soon).start()
        started = time.monotonic()
        self.wakeup.wait(self.path, 5.0)
        self.assertLess(time.monotonic() - started, 2.0)

    @unittest.skipUnless(HAS_KQUEUE, "kqueue is not available here")
    def test_a_changed_path_is_watched_instead(self):
        self.wakeup.wait(self.path, 0.01)
        other = Path(self.tmp.name) / "other.jsonl"
        other.write_text("")
        def append_soon():
            time.sleep(0.05)
            with other.open("a") as f:
                f.write('{"type":"assistant"}\n')
        threading.Thread(target=append_soon).start()
        started = time.monotonic()
        self.wakeup.wait(other, 5.0)
        self.assertLess(time.monotonic() - started, 2.0)


if __name__ == "__main__":
    unittest.main()
