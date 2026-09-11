"""Structured observability: every important operation emits an event, and
every operation that fails says so in one place.

Two outputs, one story:

    ObservabilityEvent -> bus -> JsonlSink    (domain events, by day)
                             -> LoggingSink ─┐
    application_log(...) ──────────────────> ├─> logs/conductor.jsonl
    child process stderr ──────────────────> │   (rotating, everything)
    unhandled asyncio errors ──────────────> ┘

The JSONL log is what someone - or an agent - reads when something went
wrong. It is grep-able by `run_id` (one launch of the process), `trace_id`
(one interaction, opened at the microphone) and `task_id` (one worker), so
a failure and the events around it read in order without a viewer.

Nothing may fail silently. Handlers that keep the app alive - a broken sink,
a UI callback, a watcher - still write a traceback, because a swallowed
exception with no record is indistinguishable from the bug never happening.

One user interaction gets one trace id, carried by a contextvar so components
do not have to pass it around. Events flow through a bus to sinks. The app
must not depend on any hosted vendor - the default sink is a local JSONL
file under .myconductor/observability/.

Events are written verbatim: these traces are local, on the owner's own
machine, for the owner's own debugging - nothing here is telemetry and
nothing leaves the machine, so scrubbing them only made real payloads
harder to read. Credentials are never logged.

Canonical application state (state.json) and observability storage are
separate concerns; nothing here is ever read back to make decisions.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from .storage import append_jsonl

COMPONENTS = ("voice", "manager", "conductor", "task", "workspace",
              "runtime", "storage", "ui")
SEVERITIES = ("debug", "info", "warning", "error")
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5
# How many past runs' files to keep. RotatingFileHandler's backupCount only
# bounds one process's own file, so retention across runs lives here.
LOG_KEEP_FILES = 20
_LOGGER_NAME = "voice_conductor"
_run_id = ""
_log_path: Path | None = None


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


# -- trace context -----------------------------------------------------------
_trace: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id",
                                                             default="")


def new_trace(trace_id: str = "") -> str:
    """Start a trace for one user interaction; everything emitted until the
    next new_trace() shares it.

    Pass an existing id to adopt it instead of minting one. A spoken
    interaction begins at the microphone, not at the Manager: the voice front
    end opens the trace when the key goes down and hands it down, so the
    hold, the transcript and the routing decision all land on one trace.
    """
    trace_id = trace_id or new_id("trace")
    _trace.set(trace_id)
    return trace_id


def current_trace() -> str:
    return _trace.get()


def new_run(run_id: str = "") -> str:
    global _run_id
    _run_id = run_id or new_id("run")
    return _run_id


def current_run() -> str:
    return _run_id


def current_log_path() -> Path | None:
    return _log_path


def now_iso_ms() -> str:
    """Event time, to the millisecond.

    storage.now_iso() truncates to seconds, which is right for state.json -
    a task's created_at gains nothing from milliseconds. Events are
    different: several land inside one second routinely, and at second
    precision the log cannot say which came first.
    """
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _iso_ms(stamp: str) -> str:
    """Every line to millisecond precision.

    Events mint their own millisecond stamps, but anything older - a
    replayed event, a caller passing storage.now_iso() - can still arrive at
    second precision, and mixed formats sort wrongly: "...:20.162Z" orders
    before "...:20Z", because '.' < 'Z'. Widening on the way in keeps a
    timestamp sort honest.
    """
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return stamp
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = getattr(record, "timestamp", None)
        stamp = _iso_ms(stamp) if stamp else datetime.fromtimestamp(
            record.created, timezone.utc).isoformat(timespec="milliseconds") \
            .replace("+00:00", "Z")
        entry = {
            "timestamp": stamp,
            "level": record.levelname.lower(),
            "component": getattr(record, "component", record.name),
            "event": getattr(record, "event", "log.message"),
            "message": record.getMessage(),
            "run_id": getattr(record, "run_id", "") or current_run(),
            "trace_id": getattr(record, "trace_id", "") or current_trace(),
        }
        for key in ("event_id", "project_id", "task_id",
                    "provider_session_id", "manager_session_id", "turn_id",
                    "parent_event_id", "duration_ms"):
            value = getattr(record, key, None)
            if value is not None:
                entry[key] = value
        data = getattr(record, "data", None)
        if data:
            entry["data"] = data
        if record.exc_info:
            error = record.exc_info[1]
            entry["exception"] = {
                "type": type(error).__name__ if error else "Exception",
                "message": str(error) if error else "",
                "traceback": self.formatException(record.exc_info),
            }
        # Compact separators, so `grep '"level":"error"'` - what anyone
        # actually types - matches. json.dumps defaults to `", "` and `": "`,
        # which quietly makes every documented grep return nothing.
        return json.dumps(entry, ensure_ascii=False, default=str,
                          separators=(",", ":"))


class ConsoleLogFormatter(logging.Formatter):
    """The terminal gets a readable line, not JSON. Anything above info is
    labelled, so a warning cannot be mistaken for ordinary narration."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.levelno >= logging.WARNING:
            return f"{record.levelname.lower()}: {message}"
        return message


def prune_old_logs(log_dir: Path, keep: int = LOG_KEEP_FILES) -> list[Path]:
    """Drop the oldest run files, newest kept. Returns what was removed."""
    files = sorted((p for p in log_dir.glob("conductor-*.jsonl*")
                    if p.is_file()),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for stale in files[keep:]:
        try:
            stale.unlink()
            removed.append(stale)
        except OSError:
            pass                      # a file we cannot drop is not fatal
    return removed


def configure_logging(home: str | Path, debug: bool = False,
                      console: bool = True) -> Path:
    """Point the root logger at <home>/logs/conductor-<pid>.jsonl.

    Two handlers: a rotating JSONL file that always records everything, and
    a human-readable console line. Tests pass console=False so assertions
    are not buried in their own log output.

    One file per process, not one shared file. Every process defaults to
    the same home, and RotatingFileHandler is not multi-process
    safe: two processes rolling one file at 10 MB interleave and lose
    lines. Each writer owning its own file makes rotation a single-writer
    operation again. Retention across runs is prune_old_logs(), since
    backupCount only bounds one process's own file.
    """
    global _log_path
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_voice_conductor", False):
            root.removeHandler(handler)
            handler.close()
    log_dir = Path(home).expanduser().resolve() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    prune_old_logs(log_dir)
    _log_path = log_dir / f"conductor-{os.getpid()}.jsonl"
    file_handler = RotatingFileHandler(
        _log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JsonLogFormatter())
    file_handler._voice_conductor = True
    root.addHandler(file_handler)
    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.DEBUG if debug else logging.WARNING)
        console_handler.setFormatter(ConsoleLogFormatter())
        console_handler._voice_conductor = True
        root.addHandler(console_handler)
    # Third-party loggers stay at INFO so a library's debug stream cannot
    # drown the file; this application's own logger always records debug.
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    logging.getLogger(_LOGGER_NAME).setLevel(logging.DEBUG)
    logging.captureWarnings(True)
    new_run()
    application_log("conductor", "logging.configured", "",
                    path=str(_log_path), pid=os.getpid(),
                    max_bytes=LOG_MAX_BYTES,
                    backup_count=LOG_BACKUP_COUNT,
                    keep_files=LOG_KEEP_FILES)
    return _log_path


def application_log(component: str, event: str, message: str = "",
                    severity: str = "info", exc_info=None, **fields) -> None:
    """One line in the debugging log.

    Correlation ids passed as keywords (task_id, trace_id, ...) become
    top-level fields so they can be grepped; everything else lands in
    `data`. Pass exc_info=True inside an except block to record the
    traceback.
    """
    level = getattr(logging, severity.upper(), logging.INFO)
    context = {}
    for key in ("timestamp", "event_id", "run_id", "trace_id",
                "project_id", "task_id", "provider_session_id",
                "manager_session_id", "turn_id", "parent_event_id",
                "duration_ms"):
        if key in fields:
            context[key] = fields.pop(key)
    data = fields.pop("data", {}) or {}
    if fields:
        data = {**data, **fields}
    logging.getLogger(f"{_LOGGER_NAME}.{component}").log(
        level, message or event, exc_info=exc_info,
        extra={"component": component, "event": event,
               "data": data, **context})


def install_asyncio_exception_handler(loop: asyncio.AbstractEventLoop) -> None:
    """A background task that dies takes its traceback with it. This app is
    almost entirely background tasks - watchers, pumps, sweeps - so record
    them rather than letting the default handler print into the void."""
    previous = loop.get_exception_handler()

    def handle_exception(event_loop, context) -> None:
        error = context.get("exception")
        details = {key: str(value) for key, value in context.items()
                   if key not in ("exception", "message")}
        exc_info = (type(error), error, error.__traceback__) if error else None
        application_log("asyncio", "asyncio.unhandled_exception",
                        context.get("message", "Unhandled asyncio exception"),
                        severity="error", exc_info=exc_info, data=details)
        if previous is not None:
            previous(event_loop, context)

    loop.set_exception_handler(handle_exception)


async def drain_subprocess_stderr(stream: asyncio.StreamReader | None,
                                  component: str) -> None:
    """Child stderr into the one log. The overlay's and hotkey's warnings and
    tracebacks used to go nowhere: their stdout is a protocol, so nobody was
    reading the other pipe."""
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            return
        application_log(component, f"{component}.stderr",
                        line.decode(errors="replace").rstrip(),
                        severity="warning")


def start_loop_stall_monitor(component: str, interval: float = 0.1,
                             threshold: float = 0.25) -> asyncio.Task:
    """Notice when the event loop stops turning, and say for how long.

    Everything the user feels goes through this one loop: the Fn key, the
    microphone pump, playback, the session. A synchronous call anywhere
    in the process freezes all of it at once, and the freeze leaves no
    trace of its own - the log just has a gap, and the user has an
    utterance nobody heard. Measured live: 23 s inside create_task, and
    the only evidence was a hold that read as 1 ms. This turns the gap
    into a line: `app.loop_stalled` with the stall as duration_ms.
    """
    async def watch() -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            late = now - last - interval
            if late > threshold:
                application_log(component, "app.loop_stalled",
                                f"the event loop was blocked for {late:.2f}s; "
                                "keys, audio and the session all waited",
                                severity="warning",
                                duration_ms=round(late * 1000, 1))
            last = now
    return asyncio.get_running_loop().create_task(watch())


# -- events -----------------------------------------------------------------
@dataclass
class ObservabilityEvent:
    type: str
    component: str
    event_id: str = field(default_factory=lambda: new_id("evt"))
    trace_id: str = field(default_factory=current_trace)
    timestamp: str = field(default_factory=now_iso_ms)
    project_id: str | None = None
    task_id: str | None = None
    provider_session_id: str | None = None
    manager_session_id: str | None = None
    turn_id: str | None = None
    parent_event_id: str | None = None
    duration_ms: float | None = None
    severity: str = "info"
    data: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.component not in COMPONENTS:
            raise ValueError(f"unknown component: {self.component!r}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity: {self.severity!r}")

    def to_dict(self) -> dict:
        # Optional fields that are unset stay out of the log entirely.
        entry = {"event_id": self.event_id, "trace_id": self.trace_id,
                 "timestamp": self.timestamp, "type": self.type,
                 "component": self.component, "severity": self.severity}
        for key in ("project_id", "task_id", "provider_session_id",
                    "manager_session_id", "turn_id", "parent_event_id",
                    "duration_ms"):
            value = getattr(self, key)
            if value is not None:
                entry[key] = value
        if self.data:
            entry["data"] = self.data
        return entry


Sink = Callable[[ObservabilityEvent], None]


class ObservabilityBus:
    """emit() fans one event out to every sink, verbatim.

    A bus with no subscribers is a correct no-op, so components can take a
    bus unconditionally and never test for None.
    """

    def __init__(self) -> None:
        self._sinks: list[Sink] = []

    def subscribe(self, sink: Sink) -> Callable[[], None]:
        self._sinks.append(sink)

        def unsubscribe() -> None:
            try:
                self._sinks.remove(sink)
            except ValueError:
                pass
        return unsubscribe

    def emit(self, event: ObservabilityEvent) -> None:
        for sink in list(self._sinks):
            try:
                sink(event)
            except Exception:
                application_log(
                    "observability", "observability.sink_failed",
                    f"observability sink failed for {event.type}",
                    severity="error", exc_info=True,
                    sink=type(sink).__name__, source_event=event.type)


# -- sinks -----------------------------------------------------------------
class LoggingSink:
    """Domain events into the debugging log, so a failure and the events
    that led to it are one ordered read instead of two files to correlate
    by timestamp."""

    # What to say about an event, best first. Falling straight through to
    # the type made a scannable column read "task.approval_required
    # task.approval_required" while the question itself sat in data.
    _MESSAGE_KEYS = ("summary", "error", "question", "title", "text", "tool")

    def __call__(self, event: ObservabilityEvent) -> None:
        message = next((event.data[key] for key in self._MESSAGE_KEYS
                        if event.data.get(key)), event.type)
        application_log(
            event.component, event.type, str(message),
            severity=event.severity, timestamp=event.timestamp,
            event_id=event.event_id, trace_id=event.trace_id,
            project_id=event.project_id, task_id=event.task_id,
            provider_session_id=event.provider_session_id,
            manager_session_id=event.manager_session_id,
            turn_id=event.turn_id, parent_event_id=event.parent_event_id,
            duration_ms=event.duration_ms, data=event.data)


class JsonlSink:
    """One file per day under .myconductor/observability/events/ - or, when
    the root is a conductor home (holds global.json), under
    <home>/observability/events/."""

    def __init__(self, project_root: str | Path) -> None:
        self.root = Path(project_root).resolve()
        self._dir: Path | None = None

    @property
    def dir(self) -> Path:
        """Resolved on first write, not in __init__.

        A conductor home is only marked by global.json once something has
        written it, so deciding here at construction time sent a fresh home's
        events to <home>/.myconductor/ and everything afterwards to
        <home>/observability/ - splitting one session's history across two
        directories.
        """
        if self._dir is None:
            if (self.root / "global.json").exists():
                self._dir = self.root / "observability" / "events"
            else:
                self._dir = self.root / ".myconductor" / "observability" / "events"
        return self._dir

    def __call__(self, event: ObservabilityEvent) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        append_jsonl(self.dir / f"{day}.jsonl", event.to_dict())


class ConsoleSink:
    """One line per event on stderr, for development."""

    def __call__(self, event: ObservabilityEvent) -> None:
        gist = event.data.get("summary") or event.data.get("tool") or ""
        print(f"[{event.component}] {event.type}"
              f"{' ' + str(gist) if gist else ''}"
              f"{' task=' + event.task_id if event.task_id else ''}",
              file=sys.stderr, flush=True)
