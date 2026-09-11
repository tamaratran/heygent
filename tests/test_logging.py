"""The debugging log: one file, one format, everything correlated.

The rule these tests encode is that a failure must never be invisible. A
swallowed exception with no record is the bug that costs an afternoon, so
the broken-sink and broken-callback paths assert a traceback was written,
not merely that the app survived.

Run with:  python3 -m unittest tests.test_logging -v
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from conductor.observability import (JsonLogFormatter, LoggingSink,
                                     ObservabilityBus, ObservabilityEvent,
                                     _iso_ms, application_log,
                                     configure_logging, current_log_path,
                                     current_run, drain_subprocess_stderr,
                                     new_run, new_trace, prune_old_logs)

REPO = Path(__file__).resolve().parent.parent


def setUpModule():
    """Never write into the real session transcript.

    These tests drive the same log() the live agent uses, so without this
    a test run appends fixture lines - "hey there", "my
    api_key=hunter2hunter2 ok" - into the user's actual conversation
    transcript, interleaved with what they really said.
    """
    import tempfile
    from pathlib import Path
    import voice_agent
    global _TRANSCRIPT_DIR, _REAL_TRANSCRIPT
    _TRANSCRIPT_DIR = tempfile.TemporaryDirectory()
    _REAL_TRANSCRIPT = voice_agent.TRANSCRIPT_LOG
    voice_agent.TRANSCRIPT_LOG = Path(_TRANSCRIPT_DIR.name) / "session.jsonl"


def tearDownModule():
    import voice_agent
    voice_agent.TRANSCRIPT_LOG = _REAL_TRANSCRIPT
    _TRANSCRIPT_DIR.cleanup()


class LoggingTestCase(unittest.TestCase):
    """Each test gets its own log directory and leaves logging as it found
    it, so a configured handler cannot leak into an unrelated test."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # resolve(): on macOS the temp dir is reached through /var, which is
        # a symlink to /private/var, and configure_logging resolves it.
        self.home = Path(self.tmp.name).resolve()
        self._saved = list(logging.getLogger().handlers)
        self.log_path = configure_logging(self.home, console=False)

    def tearDown(self) -> None:
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in self._saved:
            root.addHandler(handler)
        self.tmp.cleanup()

    def entries(self) -> list[dict]:
        logging.getLogger().handlers[0].flush()
        text = self.log_path.read_text().splitlines()
        return [json.loads(line) for line in text if line.strip()]

    def find(self, event: str) -> dict:
        found = [e for e in self.entries() if e["event"] == event]
        self.assertTrue(found, f"no {event} entry in {self.entries()}")
        return found[-1]


class LogFileTest(LoggingTestCase):
    def test_log_file_is_created_under_the_home(self) -> None:
        self.assertEqual(self.log_path,
                         self.home / "logs" / f"conductor-{os.getpid()}.jsonl")
        self.assertTrue(self.log_path.exists())
        self.assertEqual(current_log_path(), self.log_path)

    def test_configure_announces_itself(self) -> None:
        """The first line records where the log lives and how it rotates."""
        entry = self.find("logging.configured")
        self.assertEqual(entry["data"]["path"], str(self.log_path))
        self.assertGreater(entry["data"]["max_bytes"], 0)

    def test_entries_are_compact_so_documented_greps_match(self) -> None:
        """The README tells people to `grep '"level":"error"'`. json.dumps
        defaults to `": "`, which would make every such grep silently
        return nothing - the worst kind of broken documentation."""
        application_log("runtime", "test.compact", "boom", severity="error",
                        task_id="task_1")
        raw = self.log_path.read_text()
        self.assertIn('"level":"error"', raw)
        self.assertIn('"task_id":"task_1"', raw)
        self.assertIn('"event":"test.compact"', raw)

    def test_every_entry_is_one_json_object_per_line(self) -> None:
        application_log("conductor", "test.event", "hello")
        for entry in self.entries():
            self.assertIn("timestamp", entry)
            self.assertIn("level", entry)
            self.assertIn("component", entry)
            self.assertIn("event", entry)
            self.assertIn("run_id", entry)

    def test_reconfiguring_does_not_duplicate_handlers(self) -> None:
        """Two configure calls must not write every line twice."""
        configure_logging(self.home, console=False)
        application_log("conductor", "test.once", "only once")
        matches = [e for e in self.entries() if e["event"] == "test.once"]
        self.assertEqual(len(matches), 1)


class ConcurrentProcessTest(LoggingTestCase):
    """Every process defaults to the same home. One shared file plus
    RotatingFileHandler is not multi-process safe, so each writer owns its
    own file and rotation goes back to being a single-writer operation."""

    def _child(self, marker: str, count: int) -> subprocess.Popen:
        code = textwrap.dedent(f"""
            import sys; sys.path.insert(0, {str(REPO)!r})
            from conductor.observability import (configure_logging,
                                                 application_log)
            configure_logging({str(self.home)!r}, console=False)
            for i in range({count}):
                application_log("runtime", "test.child", "{marker}" * 40, i=i)
        """)
        return subprocess.Popen([sys.executable, "-c", code],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)

    def test_two_processes_do_not_share_one_file(self) -> None:
        a, b = self._child("a", 200), self._child("b", 200)
        for proc in (a, b):
            _, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err.decode())
        files = sorted((self.home / "logs").glob("conductor-*.jsonl"))
        self.assertGreaterEqual(len(files), 3)   # both children + this test

        # Nothing was lost and no line was interleaved into another.
        written = 0
        for path in files:
            for line in path.read_text().splitlines():
                json.loads(line)                 # would raise if mangled
                written += line.count('"event":"test.child"')
        self.assertEqual(written, 400)


class RetentionTest(LoggingTestCase):
    def test_old_runs_are_pruned_newest_kept(self) -> None:
        log_dir = self.home / "logs"
        for pid in range(1000, 1030):
            (log_dir / f"conductor-{pid}.jsonl").write_text("{}\n")
        kept = sorted(p.name for p in log_dir.glob("conductor-*.jsonl"))
        self.assertGreater(len(kept), 5)
        prune_old_logs(log_dir, keep=5)
        self.assertEqual(len(list(log_dir.glob("conductor-*.jsonl"))), 5)

    def test_pruning_leaves_other_files_alone(self) -> None:
        log_dir = self.home / "logs"
        (log_dir / "notes.txt").write_text("keep me")
        prune_old_logs(log_dir, keep=0)
        self.assertTrue((log_dir / "notes.txt").exists())


class TimestampTest(LoggingTestCase):
    def test_every_line_is_millisecond_precision(self) -> None:
        """Domain events arrive with seconds, direct calls with
        milliseconds. Mixed, a timestamp sort scrambles them: '.' sorts
        before 'Z', so 20.162Z would come before 20Z."""
        bus = ObservabilityBus()
        bus.subscribe(LoggingSink())
        # Emitted in real time, interleaved, so file order is true order.
        bus.emit(ObservabilityEvent(type="task.created", component="task"))
        application_log("runtime", "test.direct", "x")
        bus.emit(ObservabilityEvent(type="task.started", component="task"))
        stamps = [e["timestamp"] for e in self.entries()]
        for stamp in stamps:
            self.assertRegex(stamp, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(stamps, sorted(stamps),
                         "file order must equal timestamp order")

    def test_second_precision_is_widened_not_shifted(self) -> None:
        self.assertEqual(_iso_ms("2026-08-27T07:35:20Z"),
                         "2026-08-27T07:35:20.000Z")

    def test_an_unparseable_stamp_is_passed_through(self) -> None:
        self.assertEqual(_iso_ms("not a date"), "not a date")


class CorrelationTest(LoggingTestCase):
    def test_run_id_is_stable_and_on_every_entry(self) -> None:
        run_id = current_run()
        self.assertTrue(run_id.startswith("run_"))
        application_log("conductor", "test.a", "a")
        application_log("voice", "test.b", "b")
        self.assertEqual({e["run_id"] for e in self.entries()}, {run_id})

    def test_new_run_changes_the_id(self) -> None:
        first = current_run()
        self.assertNotEqual(new_run(), first)

    def test_trace_id_is_picked_up_from_the_context(self) -> None:
        trace_id = new_trace()
        application_log("manager", "test.traced", "routed")
        self.assertEqual(self.find("test.traced")["trace_id"], trace_id)

    def test_explicit_ids_win_over_the_context(self) -> None:
        new_trace()
        application_log("manager", "test.explicit", "x",
                        trace_id="trace_explicit", task_id="task_7",
                        project_id="proj_1", provider_session_id="sess_9")
        entry = self.find("test.explicit")
        self.assertEqual(entry["trace_id"], "trace_explicit")
        self.assertEqual(entry["task_id"], "task_7")
        self.assertEqual(entry["project_id"], "proj_1")
        self.assertEqual(entry["provider_session_id"], "sess_9")

    def test_extra_fields_land_in_data(self) -> None:
        application_log("runtime", "test.data", "m", tool="Bash", attempt=2)
        self.assertEqual(self.find("test.data")["data"],
                         {"tool": "Bash", "attempt": 2})

    def test_unserializable_values_do_not_break_the_log(self) -> None:
        application_log("runtime", "test.object", "m", value=object())
        self.assertIn("test.object",
                      [e["event"] for e in self.entries()])


class SeverityTest(LoggingTestCase):
    def test_severity_becomes_the_level(self) -> None:
        application_log("conductor", "test.warn", "careful",
                        severity="warning")
        self.assertEqual(self.find("test.warn")["level"], "warning")

    def test_debug_entries_reach_the_file(self) -> None:
        """The console stays quiet at debug; the file never does."""
        application_log("runtime", "test.debug", "fine detail",
                        severity="debug")
        self.assertEqual(self.find("test.debug")["level"], "debug")

    def test_exceptions_are_recorded_with_a_traceback(self) -> None:
        try:
            raise ValueError("the tmux pane vanished")
        except ValueError:
            application_log("runtime", "test.failed", "it broke",
                            severity="error", exc_info=True)
        entry = self.find("test.failed")
        self.assertEqual(entry["level"], "error")
        self.assertEqual(entry["exception"]["type"], "ValueError")
        self.assertEqual(entry["exception"]["message"],
                         "the tmux pane vanished")
        self.assertIn("Traceback", entry["exception"]["traceback"])


class LoggingSinkTest(LoggingTestCase):
    def test_observability_events_reach_the_log(self) -> None:
        bus = ObservabilityBus()
        bus.subscribe(LoggingSink())
        bus.emit(ObservabilityEvent(
            type="task.failed", component="task", task_id="task_x",
            severity="error", data={"error": "worker died"}))
        entry = self.find("task.failed")
        self.assertEqual(entry["component"], "task")
        self.assertEqual(entry["task_id"], "task_x")
        self.assertEqual(entry["level"], "error")
        self.assertEqual(entry["data"]["error"], "worker died")

    def test_the_message_says_something_the_event_name_does_not(self) -> None:
        """An approval's message should be the question, not a second copy
        of the event name."""
        bus = ObservabilityBus()
        bus.subscribe(LoggingSink())
        bus.emit(ObservabilityEvent(
            type="task.approval_required", component="task",
            task_id="task_q", data={"question": "Bash(git push)"}))
        self.assertEqual(self.find("task.approval_required")["message"],
                         "Bash(git push)")

    def test_message_falls_back_to_the_event_type(self) -> None:
        bus = ObservabilityBus()
        bus.subscribe(LoggingSink())
        bus.emit(ObservabilityEvent(type="task.started", component="task"))
        self.assertEqual(self.find("task.started")["message"], "task.started")

    def test_event_identity_survives_into_the_log(self) -> None:
        bus = ObservabilityBus()
        bus.subscribe(LoggingSink())
        event = ObservabilityEvent(type="task.started", component="task",
                                   task_id="task_y", duration_ms=12.5)
        bus.emit(event)
        entry = self.find("task.started")
        self.assertEqual(entry["event_id"], event.event_id)
        # The same instant, widened to milliseconds so the file sorts.
        self.assertEqual(entry["timestamp"], _iso_ms(event.timestamp))
        self.assertEqual(entry["duration_ms"], 12.5)

    def test_a_broken_sink_is_logged_rather_than_swallowed(self) -> None:
        """The bus still protects the app - but not by hiding the failure."""
        bus = ObservabilityBus()
        delivered = []
        bus.subscribe(lambda event: 1 / 0)
        bus.subscribe(delivered.append)
        bus.emit(ObservabilityEvent(type="task.created", component="task"))
        self.assertEqual(len(delivered), 1)
        entry = self.find("observability.sink_failed")
        self.assertEqual(entry["level"], "error")
        self.assertEqual(entry["exception"]["type"], "ZeroDivisionError")
        self.assertEqual(entry["data"]["source_event"], "task.created")


class SubprocessStderrTest(LoggingTestCase):
    def test_child_stderr_lines_are_captured(self) -> None:
        async def scenario() -> None:
            proc = await asyncio.create_subprocess_exec(
                "/bin/sh", "-c", "echo boom >&2",
                stderr=asyncio.subprocess.PIPE)
            await drain_subprocess_stderr(proc.stderr, "overlay")
            await proc.wait()
        asyncio.run(scenario())
        entry = self.find("overlay.stderr")
        self.assertEqual(entry["message"], "boom")
        self.assertEqual(entry["component"], "overlay")
        self.assertEqual(entry["level"], "warning")

    def test_no_stream_is_not_an_error(self) -> None:
        asyncio.run(drain_subprocess_stderr(None, "overlay"))


class FloodControlTest(LoggingTestCase):
    """A hot loop must not turn one broken thing into a million lines."""

    def test_a_dead_overlay_is_reported_once(self) -> None:
        try:
            import voice_agent
        except Exception:                  # pragma: no cover - no audio stack
            self.skipTest("voice_agent needs the audio dependencies")

        class DeadPipe:
            def is_closing(self):
                return False

            def write(self, _payload):
                raise BrokenPipeError("overlay is gone")

        ui = voice_agent.Ui.__new__(voice_agent.Ui)
        ui.proc = type("P", (), {"stdin": DeadPipe()})()
        ui.state = ""
        ui.write_failed = False
        for _ in range(50):                # the UI pump's real cadence
            ui.send(state="listening")
        matches = [e for e in self.entries()
                   if e["event"] == "overlay.write_failed"]
        self.assertEqual(len(matches), 1)

    @staticmethod
    async def _blank_pane(_name: str) -> str:
        return ""

    def test_a_failing_watcher_is_reported_once_per_streak(self) -> None:
        """_watch polls three times a second; a persistent failure there
        must not become a traceback per poll."""
        try:
            from conductor.tmux_runtime import TmuxClaudeRuntime, _TmuxSession
        except RuntimeError:               # pragma: no cover - tmux missing
            self.skipTest("tmux is not installed")
        runtime = TmuxClaudeRuntime(transcript_dir=None)
        sess = _TmuxSession(task_id="task_w", name="cond_w",
                            working_directory="/tmp", session_id="sess_w")
        runtime._alive = lambda name: True
        # The screen read is not what this measures, and through the real
        # tmux (no such session) under a loaded thread pool it could take
        # the whole 1.1 s window - measured: zero polls completed in a full
        # suite run while the test passed alone.
        runtime._pane = self._blank_pane
        calls = {"n": 0}

        def always_fails(_sess, pane=None):
            calls["n"] += 1
            raise RuntimeError("tmux is unreachable")
        runtime._check_approval_prompt = always_fails

        async def poll_a_few_times() -> None:
            watcher = asyncio.ensure_future(runtime._watch(sess))
            await asyncio.sleep(1.1)       # ~3 polls at 0.3s
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass
        asyncio.run(poll_a_few_times())

        self.assertGreater(calls["n"], 1, "the watcher should have retried")
        matches = [e for e in self.entries()
                   if e["event"] == "runtime.approval_watch_failed"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["task_id"], "task_w")
        self.assertEqual(matches[0]["exception"]["type"], "RuntimeError")


class FormatterTest(unittest.TestCase):
    def test_formatter_emits_a_single_line(self) -> None:
        record = logging.LogRecord("x", logging.INFO, "f.py", 1,
                                   "multi\nline", None, None)
        line = JsonLogFormatter().format(record)
        self.assertNotIn("\n", line)
        self.assertEqual(json.loads(line)["message"], "multi\nline")


if __name__ == "__main__":
    unittest.main()
