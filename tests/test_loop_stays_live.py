"""The event loop stalling must not cost the user their words.

Measured live on 2026-08-28: create_task held the loop for 23 s while it
drove cmux synchronously. The user pressed Fn inside that window, spoke,
and released; the loop then processed the press and the release 1 ms
apart, and the microphone thread - which reads the hold flag - had sent
digital silence the whole time. The agent said it never heard them.

Three guarantees, each pinned here:

- the microphone gate flips where the key is read, not where the loop
  gets round to it, and the session hears resume / audio / pause in that
  order however late the loop is;
- driving a PTY (every _tmux call is a subprocess, and under cmux each is
  a slow socket round trip) does not stop the loop;
- a stall, should one still happen, leaves a line in the log.

Run with:  python3 -m unittest tests.test_loop_stays_live -v
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import unittest
from unittest import mock

from conductor import observability
from conductor.observability import ObservabilityBus
from conductor.tmux_runtime import TmuxClaudeRuntime

try:
    import voice_agent
except Exception:                       # the audio stack is not installed
    voice_agent = None


class _Result:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout, self.stderr, self.returncode = stdout, "", returncode


@unittest.skipIf(voice_agent is None, "voice_agent needs the audio stack")
class TheGateFlipsWhereTheKeyIsRead(unittest.TestCase):
    def setUp(self) -> None:
        real = voice_agent.Speaker
        voice_agent.Speaker = lambda: mock.Mock(speaking=False)
        self.addCleanup(setattr, voice_agent, "Speaker", real)
        self.agent = voice_agent.VoiceAgent(
            api_key="", ui=None, allow_write=False, bus=ObservabilityBus())

    def test_key_changed_opens_the_gate_without_the_loop(self) -> None:
        """No loop at all: the flag still flips, because that flag is what
        the microphone thread reads."""
        self.agent.key_changed(True)
        self.assertTrue(self.agent.holding)
        self.agent.key_changed(False)
        self.assertFalse(self.agent.holding)

    def test_a_hold_during_a_stall_reaches_the_session_in_order(self) -> None:
        """The exact live failure, replayed: the loop is stuck in a
        synchronous call while the user holds, speaks and releases."""
        agent = self.agent
        sent: list = []

        async def scenario() -> None:
            agent.loop = asyncio.get_running_loop()
            heard: list[bool] = []

            def user() -> None:
                time.sleep(0.05)
                agent.key_changed(True)
                heard.append(agent.holding)       # the gate, right now
                for _ in range(3):                # "speech" from the mic thread
                    time.sleep(0.01)
                    agent.loop.call_soon_threadsafe(
                        agent._enqueue_mic,
                        b"\x01\x02" if agent.holding else b"\x00\x00")
                agent.key_changed(False)
                heard.append(agent.holding)

            threading.Thread(target=user).start()
            time.sleep(0.4)                       # the loop is blocked
            await asyncio.sleep(0.05)             # now it catches up
            while not agent.mic_q.empty():
                sent.append(agent.mic_q.get_nowait())
            self.assertEqual(heard, [True, False])
            # Bookkeeping caught up too, once each, in order.
            self.assertFalse(agent.session_told_holding)

        asyncio.run(scenario())
        self.assertEqual(sent, [voice_agent.MIC_RESUME,
                                b"\x01\x02", b"\x01\x02", b"\x01\x02",
                                voice_agent.MIC_PAUSE])

    def test_gate_markers_survive_a_full_queue(self) -> None:
        self.agent.mic_q = asyncio.Queue(maxsize=2)
        self.agent._enqueue_mic(b"\x00\x00")
        self.agent._enqueue_mic(b"\x00\x00")
        self.agent._enqueue_mic(voice_agent.MIC_PAUSE)
        items = [self.agent.mic_q.get_nowait() for _ in range(2)]
        self.assertIn(voice_agent.MIC_PAUSE, items)

    def test_set_holding_still_tells_the_session(self) -> None:
        """The loop-side entry point (tests, --no-hotkey, direct callers)
        queues the same markers itself."""
        asyncio.run(self.agent.set_holding(True))
        asyncio.run(self.agent.set_holding(False))
        items = [self.agent.mic_q.get_nowait() for _ in range(2)]
        self.assertEqual(items, [voice_agent.MIC_RESUME,
                                 voice_agent.MIC_PAUSE])


class DrivingAPaneDoesNotStopTheLoop(unittest.TestCase):
    def test_create_session_keeps_the_loop_turning(self) -> None:
        """A slow PTY call (cmux's new-workspace plus its wait for a
        prompt) must not freeze everything else the process is doing."""
        rt = TmuxClaudeRuntime(transcript_dir=None, startup_timeout=0.5)

        def slow_tmux(*args):
            if args[0] == "new-session":
                time.sleep(0.6)                  # the 20 s, in miniature
            return _Result()

        rt._tmux = slow_tmux
        rt._alive = lambda name: False           # nothing to kill, then gone
        ticks: list[float] = []

        async def ticker() -> None:
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.05)

        async def scenario() -> None:
            tick = asyncio.create_task(ticker())
            with self.assertRaises(RuntimeError):   # no session file appears
                await rt.create_session("task_x", "/tmp", "hi")
            tick.cancel()

        asyncio.run(scenario())
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        self.assertGreater(len(ticks), 5)
        self.assertLess(max(gaps), 0.3,
                        f"the loop stopped for {max(gaps):.2f}s during "
                        "create_session")


class AStallLeavesALine(unittest.TestCase):
    def test_a_blocked_loop_is_logged_with_its_duration(self) -> None:
        records: list[logging.LogRecord] = []

        class Sink(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger(observability._LOGGER_NAME)
        sink = Sink()
        logger.addHandler(sink)
        self.addCleanup(logger.removeHandler, sink)
        level = logger.level
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.setLevel, level)

        async def scenario() -> None:
            monitor = observability.start_loop_stall_monitor(
                "voice", interval=0.02, threshold=0.1)
            await asyncio.sleep(0.05)
            time.sleep(0.3)                       # a synchronous call
            await asyncio.sleep(0.1)
            monitor.cancel()

        asyncio.run(scenario())
        stalls = [r for r in records
                  if getattr(r, "event", "") == "app.loop_stalled"
                  or "app.loop_stalled" in r.getMessage()
                  or "blocked for" in r.getMessage()]
        self.assertTrue(stalls, [r.getMessage() for r in records])


if __name__ == "__main__":
    unittest.main()
