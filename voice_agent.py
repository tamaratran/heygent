#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "aiohttp>=3.10,<4",
#   "numpy>=1.26,<3",
#   "sounddevice>=0.4.6,<1",
#   "claude-agent-sdk>=0.2,<1",
#   # The startup permission check probes the Microphone and Input
#   # Monitoring grants, so the pane can be opened before the first hold
#   # of Fn instead of after a silent failure.
#   "pyobjc-framework-Quartz>=10,<13; sys_platform == 'darwin'",
#   "pyobjc-framework-AVFoundation>=10,<13; sys_platform == 'darwin'",
# ]
# ///

"""A full-duplex voice front end for Claude Code.

    hold Fn        it hears you
    release Fn     it answers
    tap Fn twice   the notifications come and go

How the answer arrives is set by `--reply`, defaulting to `boss.REPLY_MODE`:
`both` says it aloud and shows a notification card, `text` shows only the card,
`speak` only says it. The card stays until you click its x; the chevron under
it expands the full text.

The notifications - the reply card and the stack of task notices - are hidden
until you ask for them. They float over every other app, so they stay out of
the way until a double tap on Fn brings them up, and another puts them away.
The capsule is never hidden: it shows whenever Fn is held. What the
notifications would have shown is kept while they are hidden, and the
status-bar bell holds the notification history either way.

The Live session stays open the whole time and its audio clock never stalls:
holding Fn unmutes the microphone, releasing it feeds silence instead. GPT Live
is the ears and the mouth only: it transcribes, handles turn-taking, and
speaks. Every finished utterance is run by this program directly - there is no
delegate-or-not decision on the voice side - in a Claude Agent SDK session,
and the answer is appended back into the live conversation to be spoken. The
Claude session is resumed across turns, so follow-ups keep their context.

Claude's tools are read-only by default. `--allow-write` adds Edit, Write and
Bash, which is a real risk over a voice channel where nothing prompts you for
approval.

Auth: OPENAI_API_KEY from .env. Claude uses CLAUDE_CODE_OAUTH_TOKEN or
ANTHROPIC_API_KEY if .env sets one, otherwise your stored Claude login.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
import ctypes.util
import getpass
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

import aiohttp
import numpy as np
import sounddevice as sd

import boss
from conductor import dialogs
from conductor.manager import VERBATIM_FOOTER, VERBATIM_HEADER
from conductor.observability import (JsonlSink, LoggingSink,
                                     ObservabilityBus, ObservabilityEvent,
                                     application_log, configure_logging,
                                     current_run, drain_subprocess_stderr,
                                     install_asyncio_exception_handler,
                                     new_trace, start_loop_stall_monitor)
from conductor.gui_permissions import VOICE_GRANTS, ask_for_missing_grants
from conductor.projects import DEFAULT_HOME

HERE = Path(__file__).resolve().parent
MODEL = boss.LIVE_MODEL
VOICE = boss.LIVE_VOICE
# The live socket. The model is not a query parameter: it goes in the
# session.start message, which is the first thing sent on every
# connection (the API's own shape, 2026-09).
LIVE_WS = "wss://api.openai.com/v1/live/sessions"
RATE = 24_000
BLOCK = 480                    # 20 ms of 24 kHz mono pcm16
MIC_BACKLOG = 1500             # 30 s of blocks the pump may fall behind by
# The gate markers travel through the microphone queue itself, so the
# session is told "resume" before the first real block and "pause" after
# the last one, in stream order, whatever the event loop was doing.
MIC_RESUME = "session.input_audio.unmute"
MIC_PAUSE = "session.input_audio.mute"
ANSWER_CHAR_LIMIT = 1600       # one append is capped at 500 tokens
# The live conversation transcript. Overridable so a test run cannot write
# into the real one: it did, and the user's own transcript ended up with
# "hey there" / "my api_key=hunter2hunter2 ok" from a redaction fixture
# interleaved with their actual conversation - the same file being read to
# work out what the agent had heard.
TRANSCRIPT_LOG = Path(os.environ.get("VOICE_TRANSCRIPT_LOG",
                                     HERE / "session.jsonl"))

# Voice loudness is logarithmic, so the waveform is driven in dBFS. Quiet room
# tone sits near -60 dBFS; conversational speech peaks around -18.
NOISE_FLOOR_DB = -58.0
SPEECH_CEIL_DB = -17.0





# The transcript marks non-speech in brackets - "[breath]", "[laughter]" -
# and those marks were reaching the Boss's window as words the user said.
# The set of marks is open: a fixed list of them let "[mouth noise ]" and
# "[tongue click ]" through, so any short bracketed tag is a mark. Nobody
# says a square bracket out loud.
_NOISE = re.compile(r"\[[^\[\]]{0,40}\]")
_PUNCTUATION = ",.;:!?"


def tidy_spacing(text: str) -> str:
    """Ordinary spacing around the transcript's punctuation.

    Measured, off the wire: "Yeah, Um .I want to like , Um." The
    transcriber carries a mark in the fragment after the word it ends -
    ", Uh", ".formatting" - so its own text has the space on the wrong
    side of the mark. Only a space that is already there is moved or
    removed: "3.5" and "github.com" are left alone.
    """
    text = re.sub(rf"\s+([{_PUNCTUATION}]+)(?=\S)", r"\1 ", text)
    text = re.sub(rf"\s+([{_PUNCTUATION}])", r"\1", text)
    return " ".join(text.split())


def join_fragments(pieces) -> str:
    """The transcript's fragments, joined as the transcriber cut them.

    A fragment brings its own leading space or mark - " my", ", Uh",
    ".formatting", "]It" - so joining them with spaces put one before
    every comma and period: "Yeah , Um .". Butt them together, and add a
    space only between two that neither end nor start with one.
    """
    out = ""
    for piece in pieces:
        # A cut is a word boundary, so a mark glued to the word after it
        # (".formatting") takes the space it was cut off from.
        piece = re.sub(rf"^([{_PUNCTUATION}]+)(?=\S)", r"\1 ", piece)
        if (out and piece and not out[-1].isspace()
                and not piece[0].isspace()
                and piece[0] not in _PUNCTUATION + "]"):
            out += " "
        out += piece
    return out


def without_noise(text: str) -> str:
    """The words, without the transcript's stage directions."""
    return tidy_spacing(_NOISE.sub(" ", text))


def log(kind: str, **fields) -> None:
    """Append one line to the session transcript, for `tail -f session.jsonl`."""
    entry = {"t": time.strftime("%H:%M:%S"), "kind": kind, **fields}
    try:
        with TRANSCRIPT_LOG.open("a") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        application_log("voice", "transcript.write_failed",
                        "could not append to the session transcript",
                        severity="warning", exc_info=True,
                        path=str(TRANSCRIPT_LOG))


def tool_summary(name: str, args: dict) -> str:
    """One-line gist of a tool call: the argument that says what it touched."""
    for key in ("file_path", "command", "pattern", "path", "url", "prompt"):
        if key in args and isinstance(args[key], str):
            value = " ".join(args[key].split())
            return f"{name}({value[:70]})"
    return name


def close_quietly(stream) -> None:
    """Shut a stream down without caring how badly it is broken.

    A device that has been unplugged raises from stop() and close() alike,
    and by then there is nothing to salvage - the only goal is to stop
    holding the old handle before opening the new one.
    """
    for step in ("stop", "close"):
        try:
            getattr(stream, step)()
        except Exception:
            pass


def refresh_device_list() -> None:
    """Make PortAudio look at the world again.

    It caches the device list at initialisation, so a process that was
    running when the AirPods disconnected can go on offering them. Tearing
    the library down and back up is the supported way to re-enumerate;
    every stream must be closed first.
    """
    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        application_log("voice", "audio.refresh_failed",
                        "could not re-enumerate audio devices",
                        severity="warning", exc_info=True)


_CORE_AUDIO: dict = {}


def _preferred_input() -> int | None:
    """The input to hold open: the built-in microphone when there is one.

    The mic stream stays open for the whole session (the push-to-talk
    gate only mutes it), and holding a Bluetooth headset's microphone
    drops the whole headset from A2DP to HFP - music turns to
    phone-call quality for as long as the app runs ("my music became
    lower quality with headphones", reported 2026-09-01). The laptop's
    own microphone costs nothing to hold, and the headphones keep
    their music profile for output.

    VOICE_AGENT_MIC overrides: a substring of the input device to use,
    or "default" for the system default (the old behavior)."""
    want = os.environ.get("VOICE_AGENT_MIC", "").strip().lower()
    if want == "default":
        return None
    try:
        inputs = [(index, str(dev.get("name") or "").lower())
                  for index, dev in enumerate(sd.query_devices())
                  if int(dev.get("max_input_channels") or 0) > 0]
    except Exception:
        return None            # an odd device list is the default input
    if want:
        return next((index for index, name in inputs if want in name),
                    None)
    for builtin in ("macbook", "built-in"):
        for index, name in inputs:
            if builtin in name and "microphone" in name:
                return index
    return None


def _default_device_name(selector: str) -> str:
    """Ask CoreAudio which device macOS considers default RIGHT NOW.

    Not PortAudio, deliberately. PortAudio caches the device list at
    initialisation, so a process that was running when the user changed
    input in Sound settings keeps reporting - and keeps recording from -
    the device it started with. Nothing raises: the agent simply goes on
    listening to a microphone nobody is talking into.

    Returns "" when it cannot be determined, which callers must read as
    "no evidence of a change" rather than as a change, so a failure here
    can never cause a stream to be torn down.
    """
    try:
        if not _CORE_AUDIO:
            _CORE_AUDIO["ca"] = ctypes.CDLL(
                ctypes.util.find_library("CoreAudio"))
            _CORE_AUDIO["cf"] = ctypes.CDLL(
                ctypes.util.find_library("CoreFoundation"))
        ca, cf = _CORE_AUDIO["ca"], _CORE_AUDIO["cf"]

        class Address(ctypes.Structure):
            _fields_ = [("selector", ctypes.c_uint32),
                        ("scope", ctypes.c_uint32),
                        ("element", ctypes.c_uint32)]

        def code(text: str) -> int:
            return int.from_bytes(text.encode(), "big")

        SYSTEM_OBJECT = 1
        want = Address(code(selector), code("glob"), 0)
        device = ctypes.c_uint32(0)
        size = ctypes.c_uint32(4)
        if ca.AudioObjectGetPropertyData(SYSTEM_OBJECT, ctypes.byref(want), 0,
                                         None, ctypes.byref(size),
                                         ctypes.byref(device)) != 0:
            return ""
        named = Address(code("lnam"), code("glob"), 0)
        ref = ctypes.c_void_p()
        size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
        if ca.AudioObjectGetPropertyData(device.value, ctypes.byref(named), 0,
                                         None, ctypes.byref(size),
                                         ctypes.byref(ref)) != 0:
            return ""
        UTF8 = 0x08000100
        cf.CFStringGetCStringPtr.restype = ctypes.c_char_p
        direct = cf.CFStringGetCStringPtr(ref, UTF8)
        if direct:
            return direct.decode()
        buffer = ctypes.create_string_buffer(256)
        if cf.CFStringGetCString(ref, buffer, 256, UTF8):
            return buffer.value.decode()
        return ""
    except Exception:
        return ""


def default_input_name() -> str:
    return _default_device_name("dIn ")


def default_output_name() -> str:
    return _default_device_name("dOut")


def level_from_pcm(samples: np.ndarray) -> float:
    """Map a block of int16 audio to a 0-1 bar height the way an ear hears it."""
    if samples.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float64))))) / 32768.0
    if rms <= 1e-7:
        return 0.0
    db = 20.0 * math.log10(rms)
    return max(0.0, min(1.0, (db - NOISE_FLOOR_DB) / (SPEECH_CEIL_DB - NOISE_FLOOR_DB)))


def audible(pcm: bytes) -> bool:
    """Is there a voice in this block, or only the session's silence?"""
    return level_from_pcm(np.frombuffer(pcm, dtype=np.int16)) > 0.0


def same_words(a: str, b: str) -> bool:
    """True when two transcripts differ only in spacing or punctuation."""
    strip = lambda t: re.sub(r"[^a-z0-9]+", "", t.lower())
    return bool(a) and strip(a) == strip(b)


def load_env() -> None:
    """Read .env, ignoring blank values so they cannot shadow real auth."""
    path = HERE / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)


OPENAI_KEYS_URL = "https://platform.openai.com/api-keys"
OPENAI_BILLING_URL = "https://platform.openai.com/settings/organization/billing"
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
KEY_PROMPT = ("heygent needs an OpenAI API key to hear and speak.\n\n"
              f"Create one at {OPENAI_KEYS_URL} (the account needs credit, "
              "and access to GPT Live), then paste it below. It is saved to "
              "~/.voice-conductor/app/.env and never leaves this Mac except "
              "to talk to OpenAI.")
# An error that is a decision OpenAI made about this key or account, not
# the network: a new socket gets the same answer, so it is not retried.
REFUSAL_CODES = {
    "invalid_api_key": "OpenAI says the key is invalid",
    "credit_balance_exhausted": "OpenAI says the account has no credit",
    "insufficient_quota": "OpenAI says the account has no quota left",
    "model_not_found": f"OpenAI says this key cannot use {MODEL}",
}
REFUSAL_URLS = {
    "invalid_api_key": OPENAI_KEYS_URL,
    "credit_balance_exhausted": OPENAI_BILLING_URL,
    "insufficient_quota": OPENAI_BILLING_URL,
    "model_not_found": OPENAI_KEYS_URL,
}
KEY_REFUSED = "key refused"


def save_api_key(key: str) -> None:
    path = HERE / ".env"
    existing = path.read_text() if path.exists() else ""
    lines = [line for line in existing.splitlines()
             if not line.strip().startswith("OPENAI_API_KEY")]
    if key:
        lines.append(f"OPENAI_API_KEY={key}")
    path.write_text("\n".join(lines) + "\n" if lines else "")


def forget_api_key() -> None:
    """Drop a key OpenAI refused, so the next launch asks for one instead
    of failing the same way."""
    if (HERE / ".env").exists():
        save_api_key("")
    os.environ.pop("OPENAI_API_KEY", None)


def check_api_key(key: str, opener=urllib.request.urlopen) -> str | None:
    """Ask OpenAI whether it takes this key. The sentence to show when it
    does not; None when it does, or when the answer is not about the key
    (no network, a 5xx) - the live session finds out then."""
    request = urllib.request.Request(
        OPENAI_MODELS_URL, headers={"Authorization": f"Bearer {key}"})
    try:
        with opener(request, timeout=10):
            return None
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return (f"OpenAI rejected that key (HTTP 401). Check it at "
                    f"{OPENAI_KEYS_URL} and paste it again.")
        if exc.code == 403:
            return ("OpenAI refused that key (HTTP 403): the account or "
                    f"project it belongs to is not allowed in. See "
                    f"{OPENAI_KEYS_URL}.")
        return None
    except (OSError, ValueError):
        return None


def refusal_from_error(error: dict) -> str | None:
    """The sentence for a Live API error event that is a refusal of the
    key or account; None for any other error."""
    code = str(error.get("code") or "")
    if code not in REFUSAL_CODES:
        return None
    detail = str(error.get("message") or "").strip()
    text = REFUSAL_CODES[code]
    if detail:
        text += f": {detail}"
    if REFUSAL_URLS[code] not in text:
        text += f"\n\nFix it at {REFUSAL_URLS[code]}"
    return text


def refusal_from_status(status: int) -> str | None:
    if status == 401:
        return ("OpenAI rejected the API key (HTTP 401). heygent has "
                "forgotten it; open heygent again to paste a new one "
                f"from {OPENAI_KEYS_URL}.")
    if status == 403:
        return ("OpenAI refused the API key (HTTP 403): the account or "
                "project it belongs to is not allowed to use GPT Live. "
                f"See {OPENAI_KEYS_URL}.")
    return None


def ask_for_api_key(check=None) -> str:
    """A missing OpenAI key, asked for and kept in .env, so a first run
    needs no file editing: on the terminal when there is one, in a dialog
    when the app was opened from Finder. A key OpenAI rejects is asked
    for again; "" when the user gives up or nothing can ask."""
    check = check_api_key if check is None else check
    on_terminal = dialogs.has_terminal()
    if not on_terminal and not dialogs.can_show():
        return ""
    while True:
        if on_terminal:
            print(f"An OpenAI API key is needed ({OPENAI_KEYS_URL}).")
            try:
                key = getpass.getpass(
                    "Paste it here (hidden, saved to .env): ").strip()
            except (EOFError, KeyboardInterrupt):
                return ""
        else:
            key = dialogs.ask_secret(KEY_PROMPT)
        if not key:
            return ""
        rejected = check(key)
        if rejected is None:
            break
        application_log("voice", "app.api_key_rejected", rejected,
                        severity="warning")
        dialogs.tell(rejected)
    save_api_key(key)
    os.environ["OPENAI_API_KEY"] = key
    application_log("voice", "app.api_key_saved",
                    "the OpenAI key was taken from the user and saved to "
                    ".env")
    return key


def withhold_api_key() -> None:
    """Take the OpenAI key out of this process's environment once the voice
    has it. Everything started from here inherits os.environ - the Boss,
    every worker, a tmux or cmux server - and only the voice uses the key,
    which it keeps as its own copy. Servers that already hold it are
    handled per launch: conductor/tmux_runtime.CONDUCTOR_SECRETS."""
    os.environ.pop("OPENAI_API_KEY", None)


class Ui:
    """The overlay, as a subprocess we write NDJSON to."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        # Not "hidden": the idle branch skips sending a state it thinks is
        # already set, which would leave the overlay's own default unchallenged.
        self.state = ""
        self.write_failed = False
        # Set when the user chooses Quit in the overlay's menu, or the
        # overlay itself goes away: the only visible way out of an app
        # launched from Finder, so the whole run ends on it, not the
        # overlay alone.
        self.quit_requested = asyncio.Event()

    def request_quit(self, reason: str) -> None:
        if not self.quit_requested.is_set():
            application_log("ui", "app.quit_requested", reason)
        self.quit_requested.set()

    async def wait_with(self, session_task: asyncio.Task) -> None:
        """Return when quit is requested or the session ends; a session
        that failed raises here, as awaiting it directly would."""
        quit_ = asyncio.ensure_future(self.quit_requested.wait())
        try:
            await asyncio.wait({session_task, quit_},
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            quit_.cancel()
        if session_task.done():
            session_task.result()

    def send(self, **msg) -> None:
        if "state" in msg:
            self.state = msg["state"]
        if self.proc.stdin and not self.proc.stdin.is_closing():
            try:
                self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            except (BrokenPipeError, ConnectionResetError):
                # The UI pump writes ~50 times a second, so a dead overlay
                # would otherwise fill the log with the same traceback.
                if not self.write_failed:
                    self.write_failed = True
                    application_log("ui", "overlay.write_failed",
                                    "the overlay pipe is closed; further "
                                    "write failures are not logged",
                                    severity="warning", exc_info=True,
                                    keys=sorted(msg))


def audio_open_failed(what: str, exc: Exception) -> SystemExit:
    """A device that cannot be opened at all, spelled out: the most common
    cause on a fresh machine is no audio device, or one macOS will not
    hand over."""
    print(f"could not open {what}: {exc}\n\n"
          "Check that this Mac has audio devices (System Settings > "
          "Sound) and that this terminal is allowed to use the "
          "microphone (System Settings > Privacy & Security > "
          "Microphone), then restart.",
          file=sys.stderr, flush=True)
    application_log("voice", "audio.open_failed",
                    f"could not open {what}",
                    severity="error", exc_info=True)
    return SystemExit(1)


class Speaker:
    """Continuous playback of output_audio.delta, with a level for the UI.

    The Live API sends 24 kHz mono, but a Mac's default output is usually
    48 kHz stereo and PortAudio does not resample. Opening the stream at the
    device's own rate and converting here is what makes it audible at all.
    """

    # How long after the last audible block the voice still counts as
    # speaking: the gap between two words, not the gap between two replies.
    AUDIBLE_GRACE = 0.4

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.lock = threading.Lock()
        self.level = 0.0
        self.last_drain = 0.0
        self.last_audible = 0.0     # when audible audio was last fed or played
        self.warned = False
        self.device_name = ""
        self.stream = None
        try:
            self._open()
        except Exception as exc:
            raise audio_open_failed("an audio output", exc) from exc

    def _open(self) -> None:
        """Bind to whatever the default output is NOW.

        Rate and channel count are read per open rather than once at
        startup, because they belong to the device and not to us: AirPods
        and the built-in speakers do not agree on either, and a stream
        built for the wrong one plays nothing.
        """
        try:
            device = sd.query_devices(kind="output")
            out_rate = int(device["default_samplerate"])
            channels = 2 if device["max_output_channels"] >= 2 else 1
            self.device_name = str(device["name"])
        except Exception:
            out_rate, channels = RATE, 1
            self.device_name = ""
            application_log("voice", "audio.device_query_failed",
                            "falling back to 24 kHz mono output",
                            severity="warning", exc_info=True)
        self.factor = max(1, round(out_rate / RATE))
        self.rate = RATE * self.factor
        self.channels = channels
        self.stream = sd.OutputStream(
            samplerate=self.rate, channels=self.channels, dtype="int16",
            blocksize=BLOCK * self.factor, callback=self._on_audio)

    def reopen(self) -> None:
        """Rebuild playback on the current device, keeping what is queued.

        The buffer survives on purpose: the reply being spoken when the
        headphones died should finish out loud, not be dropped.
        """
        close_quietly(self.stream)
        self._open()
        self.stream.start()
        self.warned = False

    def _on_audio(self, outdata, frames, _time, _status) -> None:
        self.last_drain = time.monotonic()
        wanted = frames // self.factor          # source samples at 24 kHz
        need = wanted * 2
        with self.lock:
            take = bytes(self.buffer[:need])
            del self.buffer[:need]
        if len(take) < need:
            take += b"\x00" * (need - len(take))
        block = np.frombuffer(take, dtype=np.int16)
        self.level = level_from_pcm(block)
        if self.level > 0.0:
            self.last_audible = self.last_drain
        if self.factor > 1:
            block = np.repeat(block, self.factor)
        if len(block) < frames:
            block = np.concatenate([block, np.zeros(frames - len(block),
                                                    dtype=np.int16)])
        outdata[:] = np.repeat(block[:frames, None], self.channels, axis=1)

    def feed(self, pcm: bytes) -> None:
        if audible(pcm):
            self.last_audible = time.monotonic()
        with self.lock:
            self.buffer.extend(pcm)
            # If playback is not consuming audio, do not let it pile up.
            if len(self.buffer) > RATE * 2 * 8:
                del self.buffer[:-RATE * 2]
                if not self.warned:
                    self.warned = True
                    print("audio is not draining - is output playing?",
                          file=sys.stderr, flush=True)
                    application_log("voice", "audio.not_draining",
                                    "playback buffer overflowed; the output "
                                    "device is not consuming audio",
                                    severity="warning",
                                    buffered_bytes=len(self.buffer))

    @property
    def speaking(self) -> bool:
        """True only while a voice is actually coming out of the speaker.

        Three conditions, each measured because each was wrong once. Audio
        is queued and the device is consuming it: without the freshness
        check a stalled output stream left this permanently true, pinning
        the overlay on screen for ever. And what is queued is audible: the
        session streams 100 ms of output audio ten times a second whether
        or not anyone is talking, silence included, so "the buffer is not
        empty" was true from the first frame to the last. That made every
        hold a barge-in, the reply card never retire, and the
        release-to-first-word latency read a few milliseconds, always.
        """
        with self.lock:
            queued = len(self.buffer) > 0
        now = time.monotonic()
        return queued and (now - self.last_drain) < 0.5 \
            and (now - self.last_audible) < self.AUDIBLE_GRACE

    def flush(self) -> None:
        with self.lock:
            self.buffer.clear()


class VoiceAgent:
    def __init__(self, api_key: str, ui: Ui, allow_write: bool,
                 reply_mode: str = "both",
                 bus: ObservabilityBus | None = None) -> None:
        self.api_key = api_key
        self.ui = ui
        # A bus with no sinks is a correct no-op, so every emit below is
        # unconditional and a caller without one needs no special case.
        self.bus = bus or ObservabilityBus()
        self.trace_id = ""
        self.held_at = 0.0                # when the key went down
        self.allow_write = allow_write
        self.reply_mode = reply_mode
        self.reply_text = ""
        self.text_hold_until = 0.0
        # When the reply card should retire. A finished answer is history the
        # moment it has been said; leaving it up pushes the session rows
        # further from the waveform for no benefit.
        self.reply_expires_at = 0.0
        self.awaiting_narration = False
        self.request = "Voice session"    # card title: what was last asked
        self.heard = ""                   # live caption of the current utterance
        self.thinking_task: asyncio.Task | None = None
        self.work_started = 0.0           # when the current work item began
        self.fresh_turn = True            # next card starts a new answer
        self.released_at = 0.0            # when the key came up
        self.awaiting_first_audio = False
        self.muted_turn = False           # the user dismissed this answer
        self.turn_id = None               # the assistant turn being streamed
        # A turn is over when its words stop; these hold the waiting.
        self.asked_parts: list[str] = []   # the user's words, as cut
        self.ask_settle: asyncio.Task | None = None
        self.reply_settle: asyncio.Task | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.mic_q: asyncio.Queue = asyncio.Queue(maxsize=MIC_BACKLOG)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.speaker = Speaker()
        self.mic: sd.InputStream | None = None
        self.mic_device = ""
        # When the microphone last delivered a block. A device that goes
        # away stops calling the callback rather than raising, so silence
        # here IS the failure signal - nothing else reports it.
        self.last_mic_at = 0.0
        self.audio_recovering = False
        self.mic_level = 0.0
        # The one speakable channel, held by one caller at a time. There
        # is a single audio stream out of this session, and two callers
        # reach it: the coordinator, speaking a queued notification, and
        # _run_claude, speaking the answer to what the user just asked.
        # Nothing sat between them. Measured 2026-09-08: the Boss's reply
        # to the user and a worker's update were appended to the speakable
        # channel within the same moment and the user heard both at once.
        # VoiceCoordinator serialises everything IT queues and says so in
        # its own docstring - but it cannot serialise a caller that never
        # went through it, and the answer to a user turn never does.
        self.speech_lock = asyncio.Lock()
        self.holding = False              # the gate: read by the mic thread
        self.key_moved_at = 0.0           # when the key last moved (any thread)
        self.session_told_holding = False # what the session was last told
        self.was_holding = False
        self.last_active = 0.0
        self.claude_session: str | None = None
        self.stopping = False             # a stop the user asked for
        self.in_flight: set[str] = set()  # work items still running
        # What the microphone actually heard, kept so the work prompt can
        # carry it whatever the transcript's final form is. See
        # _verbatim_since_last_turn.
        self.spoken: deque[tuple[float, str]] = deque(maxlen=60)
        self.spoken_forwarded_at = 0.0
        self.last_utterance = ""          # the words the last work item carried
        # What the working half noted for the voice model alone - ids,
        # files, numbers, caveats - set by _work, delivered with the
        # answer on the commentary channel. Never spoken, never typed.
        self.pending_commentary = ""
        self.event_id = 0
        self.running = False
        # Set when OpenAI refused the key or the account: the sentence for
        # the user, and whether the key itself is the problem (then it is
        # forgotten, so the next launch asks for a new one).
        self.refusal = ""
        self.refused_key = False

    async def _give_up_on_key(self) -> None:
        """OpenAI has refused the key or the account. Reconnecting gets
        the same answer, so stop, and tell the user what to fix - in a
        dialog when the app was opened from Finder, where stderr is a log
        file nobody is watching."""
        application_log("voice", "voice.key_refused", self.refusal,
                        severity="error",
                        key_forgotten=self.refused_key)
        if self.refused_key:
            forget_api_key()
        await asyncio.to_thread(dialogs.tell, self.refusal)
        self.ui.request_quit("the OpenAI key was refused")

    # -- observability --------------------------------------------------
    def _emit(self, event_type: str, severity: str = "info",
              trace_id: str = "", **fields) -> None:
        """One voice event, on the same bus the conductor half already uses.

        The trace id is passed explicitly rather than read from the contextvar
        the conductor uses. These emits are spread across several long-lived
        asyncio tasks - the hotkey loop, the websocket reader, the UI pump -
        and a task only ever sees the context it was created with, so a trace
        opened at key-down would never reach the reader loop on its own.
        """
        self.bus.emit(ObservabilityEvent(type=event_type, component="voice",
                                         trace_id=trace_id or self.trace_id,
                                         severity=severity, **fields))

    # Shorter than any real press. A "hold" under this did not come from a
    # finger, so the utterance it was carrying was lost.
    IMPLAUSIBLE_HOLD_MS = 150.0

    async def set_holding(self, value: bool, gate_sent: bool = False,
                          double_tap: bool = False) -> None:
        """Gate the microphone, and say so explicitly.

        Feeding silence alone leaves the model to infer the turn ended, which
        costs a beat. Muting and unmuting the input states it outright while
        the frame cadence keeps running. The word goes through the microphone
        queue rather than straight to the socket, so it can never overtake
        or trail the audio it applies to.

        gate_sent: key_changed already flipped the gate and queued the
        marker where the key actually moved; only the bookkeeping is left.
        double_tap: this release ended the second tap of the gesture that
        toggles the overlay, so the very short hold it reports is a finger
        doing exactly what it meant to - not a key that lied.
        """
        if value == self.session_told_holding:
            return
        self.session_told_holding = value
        if not gate_sent:
            if value:
                self._enqueue_mic(MIC_RESUME)   # before the gate opens
            self.holding = value
            if not value:
                self._enqueue_mic(MIC_PAUSE)    # after the gate closes
        if value:
            # One utterance is one trace, and it starts at the microphone -
            # not at the Manager, which only ever sees text.
            self.trace_id = new_trace()
            # Timed from where the key moved, not from when the loop
            # got round to it: a hold replayed after a stall is still
            # the length the finger made it.
            self.held_at = self.key_moved_at if gate_sent \
                else time.monotonic()
            if self.speaker.speaking:
                self._emit("voice.barge_in")
            self._emit("voice.listening_started")
        else:
            self.speaker.flush()
            # The key going up is the end of the utterance. The words
            # usually trail it by a moment, so the settle waits for them;
            # arming it here is what guarantees an utterance whose words
            # arrived before the release is still run.
            self._arm_ask()
            # Time to first spoken word, which is what latency actually
            # feels like: the answer's words start well after this.
            self.released_at = self.key_moved_at if gate_sent \
                else time.monotonic()
            self.awaiting_first_audio = True
            held_ms = (round((self.released_at - self.held_at) * 1000, 1)
                       if self.held_at else None)
            if held_ms is not None and held_ms < self.IMPLAUSIBLE_HOLD_MS \
                    and not double_tap:
                # Nobody presses and releases a key this fast, so the key
                # reported a release nobody made and the microphone shut in
                # the middle of a sentence. Worth a line of its own: feeding
                # silence on release is the designed path, so the words that
                # went missing here leave no other trace anywhere.
                application_log("hotkey", "hotkey.hold_implausibly_short",
                                f"the key reported a {held_ms}ms hold; "
                                "anything said into it was dropped",
                                severity="warning", trace_id=self.trace_id,
                                duration_ms=held_ms)
            self._emit("voice.listening_stopped", duration_ms=held_ms)

    def key_changed(self, down: bool, double: bool = False) -> None:
        """The Fn key moved. Safe from any thread, and meant for one.

        The gate has to flip the instant the key does, and the event loop
        is not that instant. Measured live: create_task held the loop for
        23 s; the user pressed Fn, spoke, and released inside that window;
        the loop then processed down and up 1 ms apart, and the microphone
        thread - which reads `holding` - had sent silence throughout. So
        the hotkey is read on its own thread and lands here: the gate
        flips now, the resume/pause markers are queued in order with the
        audio, and only the bookkeeping is handed to the loop.
        """
        if down == self.holding:
            return
        self.key_moved_at = time.monotonic()
        loop = self.loop
        if down:
            if loop is not None:
                loop.call_soon_threadsafe(self._enqueue_mic, MIC_RESUME)
            self.holding = True
        else:
            self.holding = False
            if loop is not None:
                loop.call_soon_threadsafe(self._enqueue_mic, MIC_PAUSE)
        if loop is not None:
            loop.call_soon_threadsafe(self._hold_changed, down, double)

    def _hold_changed(self, down: bool, double: bool = False) -> None:
        asyncio.ensure_future(
            self.set_holding(down, gate_sent=True, double_tap=double))

    def toggle_overlay(self) -> None:
        """Show the notifications or put them away: Fn was double-tapped.

        They start hidden and sit on top of whatever the user is working in,
        so this is both the way to see the cards and the notification stack
        and the way back out of them; the capsule is not affected. Which way
        it went is the overlay's to know - it owns the windows - and it says
        so on its own channel.
        """
        self.ui.send(toggle_hidden=True)
        application_log("ui", "overlay.toggle_requested",
                        "the user double-tapped Fn", severity="debug",
                        trace_id=self.trace_id)

    def _next_id(self, prefix: str) -> str:
        self.event_id += 1
        return f"{prefix}_{self.event_id}"

    # -- audio ----------------------------------------------------------
    def _mic_callback(self, loop):
        def callback(indata, _frames, _time, _status):
            self.last_mic_at = time.monotonic()
            if self.holding:
                data = bytes(indata)
                self.mic_level = level_from_pcm(indata)
            else:
                # Silence rather than nothing: the session runs on an audio
                # clock, and a stalled inbound stream stops it responding.
                data = b"\x00" * (len(memoryview(indata).tobytes()))
                self.mic_level = 0.0
            try:
                loop.call_soon_threadsafe(self._enqueue_mic, data)
            except RuntimeError:
                pass
        return callback

    MIC_STALL_SECONDS = 2.5       # no blocks for this long: device is gone
    AUDIO_CHECK_SECONDS = 1.0

    def _open_mic(self, loop) -> None:
        """Bind the microphone to the preferred input - the built-in
        mic when there is one, so Bluetooth headphones keep their
        music-quality profile (see _preferred_input)."""
        device = _preferred_input()
        self.mic = sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                                  blocksize=BLOCK, device=device,
                                  callback=self._mic_callback(loop))
        self.mic.start()
        self.last_mic_at = time.monotonic()
        try:
            self.mic_device = str(
                (sd.query_devices(device) if device is not None
                 else sd.query_devices(kind="input"))["name"])
        except Exception:
            self.mic_device = ""

    async def _watch_audio(self, loop) -> None:
        """Survive the user switching microphones, or their AirPods dying.

        Both streams are bound to the device that was default when they
        were opened. Take that device away - disconnect the headphones,
        switch input in Sound settings - and PortAudio does not reopen
        anything: it stops calling the callback, prints an AUHAL error,
        and the session stays up with a microphone that hears nothing.
        Which looks exactly like the agent having stopped listening,
        because it has.

        So: notice the silence, rebuild both streams on the current
        device, and keep the WebSocket. The session and the conversation
        survive a device change; only the streams are rebuilt.
        """
        while self.running:
            await asyncio.sleep(self.AUDIO_CHECK_SECONDS)
            if not self.running or self.audio_recovering:
                continue
            # Two different failures, and only one of them is silent.
            # A device that DISAPPEARS stops the callbacks. A device the
            # user switches AWAY from keeps delivering perfectly - from
            # the wrong microphone - so the stall check would never fire
            # and the agent would go on listening to the old one.
            switched = self._device_switched()
            if switched:
                await self._recover_audio(loop, 0.0, switched)
                continue
            silent_for = time.monotonic() - self.last_mic_at
            if silent_for < self.MIC_STALL_SECONDS:
                continue
            # The session runs on an audio clock: an inbound stream that
            # stops entirely stops it responding. Silence keeps that clock
            # ticking across the gap, the same way a held key does.
            self._feed_silence()
            await self._recover_audio(loop, silent_for)

    def _device_switched(self) -> str:
        """What the user switched to, or "" if nothing changed.

        An unreadable default is not a change: CoreAudio failing must
        never tear down a working stream.
        """
        now_in = default_input_name()
        if now_in and self.mic_device and now_in != self.mic_device:
            return f"input switched to {now_in}"
        now_out = default_output_name()
        if now_out and self.speaker.device_name \
                and now_out != self.speaker.device_name:
            return f"output switched to {now_out}"
        return ""

    def _feed_silence(self) -> None:
        block = b"\x00" * (BLOCK * 2)
        for _ in range(int(self.AUDIO_CHECK_SECONDS * RATE / BLOCK)):
            try:
                self.mic_q.put_nowait(block)
            except asyncio.QueueFull:
                return

    async def _recover_audio(self, loop, silent_for: float,
                             reason: str = "") -> None:
        """Rebuild both streams on the current default devices."""
        self.audio_recovering = True
        was = reason or (f"input stalled for {silent_for:.1f}s on "
                         f"{self.mic_device or 'the previous input'}")
        try:
            close_quietly(self.mic)
            close_quietly(self.speaker.stream)
            # Only now: PortAudio re-enumerates on init, and it will not
            # do it with streams open.
            refresh_device_list()
            self._open_mic(loop)
            self.speaker.reopen()
        except Exception as exc:
            # No usable device at all - between the headphones leaving and
            # the built-in taking over there is a window with neither. Try
            # again on the next tick rather than killing the session.
            application_log("voice", "audio.recover_failed",
                            f"could not reopen audio: {exc}",
                            severity="warning", exc_info=True)
            self.last_mic_at = time.monotonic()   # do not spin
            return
        finally:
            self.audio_recovering = False
        print(f"audio device changed: listening on {self.mic_device}, "
              f"speaking on {self.speaker.device_name}", flush=True)
        application_log("voice", "audio.recovered",
                        f"{was}; reopened on {self.mic_device} / "
                        f"{self.speaker.device_name}",
                        severity="warning")

    def _enqueue_mic(self, data) -> None:
        """Drop a block rather than raise inside the event loop.

        put_nowait was handed to call_soon_threadsafe directly, so a full
        queue raised QueueFull in the loop callback where the except
        around the scheduling call could not see it - an unhandled
        traceback per block for as long as the drain was behind.

        A gate marker is never dropped: losing a pause leaves the session
        listening to silence, losing a resume loses the utterance. It
        takes the place of the oldest block instead.
        """
        try:
            self.mic_q.put_nowait(data)
        except asyncio.QueueFull:
            if not isinstance(data, str):
                return
            try:
                self.mic_q.get_nowait()
                self.mic_q.put_nowait(data)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    async def _pump_mic(self) -> None:
        """Stream microphone audio continuously.

        The session runs on an audio clock, so the inbound stream must never
        stall - that is also what lets the model interrupt and be interrupted.
        The gate markers ride the same queue, so the session hears "resume"
        before the first real block and "pause" after the last.
        """
        while self.running:
            chunk = await self.mic_q.get()
            if self.ws is None or self.ws.closed:
                # No socket right now: a reconnect is in progress. This
                # task is created ONCE for the life of the agent, so
                # returning here - which it used to do - would have left
                # the microphone dead after the first reconnect, silently.
                # Drop the block and keep going; the next socket gets the
                # next one.
                continue
            try:
                if isinstance(chunk, str):
                    await self.ws.send_json({"type": chunk})
                else:
                    await self.ws.send_json({
                        "type": "session.input_audio.append",
                        "audio": base64.b64encode(chunk).decode(),
                    })
            except (ConnectionResetError, aiohttp.ClientError):
                if isinstance(chunk, str):
                    application_log("voice", "voice.mic_gate_failed",
                                    "could not tell the session the "
                                    "microphone was "
                                    f"{'opened' if chunk == MIC_RESUME else 'closed'}",
                                    severity="warning", exc_info=True)
                continue            # the socket died mid-send; reconnecting

    REPLY_LINGER = 2.0        # seconds a finished reply stays up
    # How long the words have to stop before a turn counts as over. The
    # live session announces no turn boundaries: it sends transcript
    # deltas, and a gap between them is all there is. Measured against a
    # real session: words inside one sentence arrive 200ms apart, so a
    # gap this size is a pause between turns and not a breath inside one.
    ASK_SETTLE_S = 0.5
    REPLY_SETTLE_S = 0.8
    # The transcript lags the finger: the key comes up while the last
    # words are still being transcribed. Measured on a real session, the
    # tail arrived 0.9s after the release - and firing on the gap alone
    # cut "how many PRs are open on my repo" into two utterances, the
    # second of them the word "repo". Nothing is run until the release
    # is this old, and any word that arrives meanwhile pushes it out.
    POST_RELEASE_GRACE = 1.2

    def _arm_ask(self) -> None:
        """The user's words are still arriving: wait for the tail."""
        if self.ask_settle is not None:
            self.ask_settle.cancel()
        self.ask_settle = asyncio.create_task(self._settle_ask())

    async def _settle_ask(self) -> None:
        try:
            await asyncio.sleep(self.ASK_SETTLE_S)
        except asyncio.CancelledError:
            return
        self.ask_settle = None
        if self.holding:
            return          # still talking: the utterance is not over
        since = time.monotonic() - (self.released_at or 0.0)
        if since < self.POST_RELEASE_GRACE:
            # The words are still catching up with the key.
            self.ask_settle = asyncio.create_task(self._settle_ask())
            return
        parts, self.asked_parts = self.asked_parts, []
        asked = without_noise(join_fragments(parts))
        if not asked:
            return
        self.request = asked
        self._emit("voice.utterance_completed", data={"text": asked[:300]})
        # What the user says goes to the Boss, spoken or typed: the work
        # starts the moment their words stop, before the frontend has
        # said anything.
        self._start_work(asked, self.trace_id)

    def _arm_reply(self) -> None:
        if self.reply_settle is not None:
            self.reply_settle.cancel()
        self.reply_settle = asyncio.create_task(self._settle_reply())

    async def _settle_reply(self) -> None:
        try:
            await asyncio.sleep(self.REPLY_SETTLE_S)
        except asyncio.CancelledError:
            return
        self.reply_settle = None
        final = re.sub(r"\s+", " ", self.reply_text).strip()
        if not final:
            return
        print(f"agent: {final}", flush=True)
        log("agent", text=final)
        self._emit("voice.reply_spoken", turn_id=self.turn_id,
                   data={"text": final[:300]})
        self._reply_spoken(final)
        if self.in_flight:
            return              # filler said while Claude works
        self.awaiting_narration = False
        if self.muted_turn:
            return              # nothing of this turn was heard
        # The answer is complete: start its retirement clock. It is
        # cleared again if the model keeps talking.
        self.reply_expires_at = time.monotonic() + self.REPLY_LINGER
        self.card(final, status="done", collapse=True, title="", tail=True)

    def _stream_card(self) -> None:
        """Show the answer as it is spoken, matching what is actually heard."""
        if self.in_flight or self.muted_turn or self.holding:
            return
        self.reply_expires_at = 0.0        # still talking
        spoken = re.sub(r"\s+", " ", self.reply_text).strip()
        if spoken:
            self.card(spoken, status="working", title="", fresh=self.fresh_turn,
                      tail=True)
            self.fresh_turn = False

    async def _watch_overlay(self, stream) -> None:
        """Listen for the overlay reporting what the user did to the card."""
        while True:
            line = await stream.readline()
            if not line:
                self.ui.request_quit("the overlay exited")
                return
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                application_log("ui", "overlay.invalid_message",
                                "overlay sent a line that is not JSON",
                                severity="warning",
                                line=line.decode(errors="replace")[:300])
                continue
            self.handle_overlay_event(event)

    def handle_overlay_event(self, event: dict) -> None:
        """One overlay message, from whichever loop reads the pipe."""
        kind = event.get("event")
        if kind == "quit_requested":
            self.ui.request_quit("Quit chosen in the overlay menu")
        elif kind == "visibility":
            hidden = bool(event.get("hidden"))
            application_log("ui", "overlay.visibility_changed",
                            f"the overlay is now "
                            f"{'hidden' if hidden else 'on screen'}",
                            hidden=hidden)
        elif kind == "dismiss":
            self.muted_turn = True
            self.speaker.flush()
            print("dismissed - stopping playback", flush=True)
            log("dismissed")
            self._emit("voice.reply_dismissed")

    async def _pump_ui(self) -> None:
        """The capsule means exactly one thing: the microphone is open.

        Thinking and speaking are shown by the notification card, not here. An
        earlier version drove the capsule from the output buffer, which never
        empties - the model streams audio continuously, silence included - so
        the pill stayed on screen for ever showing nothing.
        """
        while self.running:
            if self.holding:
                if not self.was_holding:
                    # Barge-in: stop the agent mid-sentence rather than talking
                    # over queued audio it has already been sent.
                    self.speaker.flush()
                if not self.was_holding:
                    self.heard = ""              # a new utterance
                    if boss.SHOW_CAPTION:
                        self.ui.send(heard="")
                    if boss.CAPSULE_WORDS:
                        self.ui.send(words="")
                self.ui.send(state="listening", level=self.mic_level)
                self.last_active = time.monotonic()
            # Retire a finished reply once it has been said and read, so the
            # session rows move back down beside the waveform.
            if (self.reply_expires_at
                    and time.monotonic() > self.reply_expires_at
                    and not self.speaker.speaking):
                self.reply_expires_at = 0.0
                self.reply_text = ""
                self.ui.send(card=None)
            if self.ui.state != "hidden" and \
                    time.monotonic() - self.last_active > 1.2:
                # Linger a moment after release so the last words stay readable.
                self.ui.send(state="hidden")
                if boss.SHOW_CAPTION:
                    self.ui.send(heard="")
            self.was_holding = self.holding
            await asyncio.sleep(0.02)

    # -- the user's own words ---------------------------------------------
    def _verbatim_since_last_turn(self, window: float = 180.0) -> str:
        """What the user actually said, in their words, since the last
        work item - straight from the transcript of the microphone.

        The turn transcript can be something that means nothing on its
        own. Measured, from a real session: the user asked for tests to be
        run on PR 22, answered a clarifying question, and the turn carried
        only "Yes, correct". The manager received a confirmation with no
        idea what was being confirmed and sent nothing, while the user had
        been told out loud it was handled. The microphone transcript makes
        that not matter: the words reach the manager either way.
        """
        now = time.monotonic()
        floor = max(self.spoken_forwarded_at, now - window)
        words = without_noise(join_fragments(
            text for at, text in self.spoken if at > floor))
        if not words:
            # Nothing new since the last work item: this is a follow-up to
            # what was already said, so carry the tail of it rather than
            # nothing.
            words = without_noise(join_fragments(
                text for _, text in list(self.spoken)[-8:]))
        return words[-800:]

    def _with_verbatim(self, prompt: str) -> str:
        """Attach the user's own words to a work prompt."""
        spoken = self._verbatim_since_last_turn()
        self.spoken_forwarded_at = time.monotonic()
        self.last_utterance = spoken
        if not spoken:
            return prompt
        if spoken.lower() in prompt.lower():
            return prompt          # already carrying them; do not repeat
        return (f"{prompt}\n\n"
                f"{VERBATIM_HEADER}\n"
                f"{spoken}\n"
                f"{VERBATIM_FOOTER} The line above this block is the "
                f"voice frontend's summary and may be a bare confirmation "
                f'("yes", "correct") that means nothing on its own. When '
                f"you pass an instruction to a working agent, send the "
                f"user's words, not a paraphrase of them.")

    # -- work ---------------------------------------------------------------
    async def _run_claude(self, item_id: str, prompt: str,
                          trace_id: str = "") -> None:
        """One work item: do the work, speak the answer.

        The verbatim backstop is attached HERE, above the seam, so it
        reaches whichever half does the work. It used to live inside the
        Claude-SDK body - which conduct.py replaces wholesale - so the
        Manager, the half it was written for, never saw it.
        """
        prompt = self._with_verbatim(prompt)
        answer = await self._work(prompt, trace_id)
        self._finish_work(item_id)
        # The side note goes first, so the voice has it when it speaks.
        note, self.pending_commentary = self.pending_commentary, ""
        if note:
            await self._session_commentary(note)
        if answer:                 # a folded turn: said elsewhere
            await self.announce(answer)

    def _finish_work(self, item_id: str) -> None:
        self.in_flight.discard(item_id)
        if self.thinking_task and not self.in_flight:
            self.thinking_task.cancel()
            self.thinking_task = None
        # The next assistant turn is the spoken answer, not filler.
        self.awaiting_narration = True

    def _start_work(self, text: str, trace_id: str) -> str:
        """Run one utterance as one unit of work, with the waiting line on
        the card. Every completed user turn lands here: the voice side
        makes no delegate-or-not decision."""
        item_id = self._next_id("work")
        self.in_flight.add(item_id)
        if self.thinking_task:
            self.thinking_task.cancel()
        self.fresh_turn = True
        self.thinking_task = asyncio.create_task(self._thinking_line(text))
        asyncio.create_task(self._run_claude(item_id, text, trace_id))
        return item_id

    def _reply_spoken(self, text: str) -> None:
        """The voice model finished saying something - filler, small
        talk or a relayed answer. Overridden where somebody else needs
        to know what the user has heard."""

    async def _work(self, prompt: str, trace_id: str = "") -> str:
        """Run one unit of work as a Claude Code session; return the answer.

        Separate from the work bookkeeping so conduct.py can replace it
        wholesale with a Manager turn.
        """
        from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                      ResultMessage, TextBlock, ToolUseBlock,
                                      query)

        print(f"  → claude: {prompt}", flush=True)
        log("work", prompt=prompt)
        started = time.monotonic()
        options = ClaudeAgentOptions(
            max_turns=boss.MAX_TURNS,
            allowed_tools=boss.WRITE_TOOLS if self.allow_write else boss.READ_ONLY_TOOLS,
            disallowed_tools=[] if self.allow_write else boss.BLOCKED_WITHOUT_WRITE,
            permission_mode="bypassPermissions",
            system_prompt=boss.CLAUDE_SYSTEM_PROMPT,
            cwd=str(HERE),
            resume=self.claude_session,      # keep one session across turns
        )
        parts: list[str] = []
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            gist = tool_summary(block.name, block.input or {})
                            print(f"     ⚒ {gist}", flush=True)
                            log("tool", tool=block.name, gist=gist)
                elif isinstance(message, ResultMessage):
                    usage = message.usage or {}
                    tokens = int(usage.get("output_tokens", 0) or 0)
                    elapsed = int(time.monotonic() - self.work_started)
                    log("claude_done", seconds=elapsed, output_tokens=tokens)
                    first = self.claude_session is None
                    self.claude_session = message.session_id
                    log("claude_session", session_id=message.session_id,
                        cost_usd=message.total_cost_usd, turns=message.num_turns)
                    if first and message.session_id:
                        print(f"     session {message.session_id}\n"
                              f"     replay it with:  claude -r {message.session_id}",
                              flush=True)
            answer = re.sub(r"\s+", " ", " ".join(parts)).strip()
        except Exception as exc:
            answer = f"The tool call failed: {exc}"
            application_log("voice", "voice.work_failed",
                            "the Claude work item failed", severity="error",
                            exc_info=True, trace_id=trace_id,
                            provider_session_id=self.claude_session,
                            prompt=prompt[:300])
            self._emit("voice.work_failed", severity="error",
                       trace_id=trace_id, data={"error": str(exc)[:300]})

        answer = answer[:ANSWER_CHAR_LIMIT] or "I could not find an answer."
        print(f"  ← claude: {answer}", flush=True)
        log("claude_answer", text=answer)
        self._emit("voice.work_completed", trace_id=trace_id,
                   provider_session_id=self.claude_session,
                   duration_ms=round((time.monotonic() - started) * 1000, 1),
                   data={"summary": answer[:300]})
        return answer

    # A ceiling on holding the speakable channel. The drain loop ends when
    # the voice stops being audible, which it does; this is only so that a
    # stream that never drains cannot make the channel unusable for ever.
    SPEECH_CEILING_S = 90.0

    async def announce(self, text: str) -> None:
        """Say something through the live session itself, one at a time.

        The one speech path for everything the user hears. A separate
        engine - macOS `say`, a second Live session - would be a second
        voice and a second audio stream, able to talk over this one; there
        is only ever the session's own output_audio.delta reaching the
        speaker.

        And one voice is not one queue. Two callers reach this method -
        the coordinator with a queued notification, and _run_claude with
        the answer to what the user just asked - and until the lock they
        could each append to the speakable channel while the other was
        still being spoken, which is two replies at once out of one
        stream. Waiting for playback to drain was always here; what was
        missing is that nothing made the next caller wait for it.
        """
        text = re.sub(r"\s+", " ", text or "").strip()
        if not text or self.ws is None or self.ws.closed:
            return
        if self.speech_lock.locked():
            self._emit("voice.speech_queued",
                       data={"text": text[:120]})
        async with self.speech_lock:
            # Re-checked inside the lock: the session can close while a
            # caller waits its turn.
            if self.ws is None or self.ws.closed:
                return
            # commentary is the channel that is SPOKEN - the model
            # paraphrases what is appended and says it. (thinking is the
            # silent one; the two are easy to swap and the cost of
            # swapping them is the user hearing our private notes.)
            await self.ws.send_json({
                "type": "session.commentary.append",
                "delegation_id": None,
                "content": text[:1600],
            })
            # Let it begin, then wait for the utterance to finish.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not self.speaker.speaking:
                await asyncio.sleep(0.05)
            ceiling = time.monotonic() + self.SPEECH_CEILING_S
            while self.speaker.speaking and not self.holding:
                if time.monotonic() > ceiling:
                    self._emit("voice.speech_ceiling_reached",
                               severity="warning",
                               data={"text": text[:120]})
                    break
                await asyncio.sleep(0.05)

    # -- what the voice should know, and the user should not hear ---------
    async def _session_commentary(self, text: str) -> None:
        """A side note for the voice model alone: ids, files, numbers,
        caveats the working half noted. The commentary channel is held,
        not spoken - so "which file was that?" is answered from it
        without another round trip."""
        if self.ws is None or self.ws.closed:
            return
        self._emit("voice.commentary_sent", data={"text": text[:300]})
        # thinking, not commentary: appended for the model to reason
        # from, never spoken on arrival. The names are the API's, and
        # they are the opposite way round from how they read.
        await self.ws.send_json({
            "type": "session.thinking.append",
            "delegation_id": None,
            "content": text[:1600],
        })

    # -- session --------------------------------------------------------
    # How long to wait between reconnects, doubling up to the cap. The
    # first retry is quick: most drops are a single bad frame, and every
    # second of silence after one is a second the user is talking to nobody.
    RECONNECT_FLOOR = 0.5
    RECONNECT_CAP = 15.0

    async def run(self) -> None:
        """Keep a voice session open until told to stop.

        One connection per loop iteration. The socket ending is NOT the
        session ending: the app used to exit the moment the WebSocket
        closed, and a WebSocket closes for a ping frame, a close frame, or
        a dropped connection - three silent deaths in one evening, each
        logged as a clean app.stopped with zero errors, one of them
        mid-sentence. Now the socket is reopened, with the same
        instructions, while the conductor and every worker carry on
        untouched.

        What ends the loop: a stop the user asked for (self.stopping), or
        the server closing the session on purpose (session.closed).
        """
        headers = {"Authorization": f"Bearer {self.api_key}"}
        # Startup has no utterance behind it, so give it its own trace rather
        # than letting session events fall outside every trace.
        self.trace_id = new_trace()
        loop = asyncio.get_running_loop()
        self.loop = loop
        self.stopping = False
        self.running = True
        try:
            self._open_mic(loop)
        except Exception as exc:
            raise audio_open_failed("a microphone", exc) from exc
        try:
            self.speaker.stream.start()
            device = sd.query_devices(kind="output")
            print(f"speaker: {device['name']} "
                  f"@ {self.speaker.rate} Hz x{self.speaker.channels}",
                  flush=True)
        except Exception as exc:
            print(f"could not open the speaker: {exc}",
                  file=sys.stderr, flush=True)
            application_log("voice", "audio.speaker_open_failed",
                            "could not open the output stream; "
                            "replies will not be audible",
                            severity="error", exc_info=True)
        # Audio and UI outlive any one connection: the microphone does not
        # care which socket its blocks go down, and the overlay must not
        # flicker because the network did.
        tasks = [asyncio.create_task(self._pump_mic()),
                 asyncio.create_task(self._pump_ui()),
                 asyncio.create_task(self._watch_audio(loop))]
        if self.ui.proc.stdout is not None:
            tasks.append(asyncio.create_task(
                self._watch_overlay(self.ui.proc.stdout)))
        delay = 0.0
        attempt = 0
        try:
            async with aiohttp.ClientSession() as session:
                while not self.stopping:
                    if delay:
                        await asyncio.sleep(delay)
                    ended = await self._one_connection(session, headers,
                                                       attempt)
                    if ended == KEY_REFUSED:
                        await self._give_up_on_key()
                        break
                    if ended == "closed_by_server" or self.stopping:
                        break
                    attempt += 1
                    delay = min(self.RECONNECT_CAP,
                                self.RECONNECT_FLOOR * (2 ** (attempt - 1)))
                    print(f"voice session dropped ({ended}); reconnecting "
                          f"in {delay:.1f}s", file=sys.stderr, flush=True)
                    application_log("voice", "voice.reconnecting",
                                    f"session ended: {ended}",
                                    severity="warning",
                                    attempt=attempt, delay_s=delay)
        finally:
            self.running = False
            for task in tasks:
                task.cancel()
            # Whatever stream is open NOW - recovery may have replaced the
            # one this session started with, and the old local name would
            # have stopped a dead handle while the live device kept running.
            close_quietly(self.mic)
            close_quietly(self.speaker.stream)
            self.ws = None

    async def _one_connection(self, session, headers: dict,
                              attempt: int) -> str:
        """Run one WebSocket to its end. Returns why it ended, for the
        log and for the decision to reconnect: the reason used to be
        invisible, which is why three drops read as three mysteries."""
        try:
            ws = await session.ws_connect(LIVE_WS, headers=headers,
                                          heartbeat=20)
        except aiohttp.WSServerHandshakeError as exc:
            refusal = refusal_from_status(exc.status)
            if refusal:
                self.refusal = refusal
                self.refused_key = exc.status == 401
                return KEY_REFUSED
            return f"connect failed: {type(exc).__name__} {exc.status}"
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
            return f"connect failed: {type(exc).__name__}"
        async with ws:
            self.ws = ws
            # session.start, not session.update: the session does not
            # exist until this arrives, and the model, the instructions
            # and the audio format are all settled here. session.update
            # afterwards takes almost none of them back.
            await ws.send_json({
                "type": "session.start",
                "session": {
                    "model": MODEL,
                    "instructions": boss.FRONTEND_INSTRUCTIONS,
                    "audio": {"format": {"type": "audio/pcm", "rate": RATE},
                              "output": {"voice": VOICE}},
                },
            })
            if attempt:
                self._emit("voice.session_reconnected",
                           data={"attempt": attempt})
                # The gate marker that was in flight when the last socket
                # died went down with it; the new session is told where
                # the key is now.
                self._enqueue_mic(MIC_RESUME if self.holding else MIC_PAUSE)
            try:
                return await self._read_events(ws)
            except (aiohttp.ClientError, ConnectionResetError) as exc:
                return f"transport error: {type(exc).__name__}"
            finally:
                if self.ws is ws:
                    self.ws = None

    async def _read_events(self, ws) -> str:
        async for msg in ws:
            if msg.type is not aiohttp.WSMsgType.TEXT:
                # Say WHICH frame ended it. CLOSE and CLOSED are the server
                # or the network hanging up; ERROR is aiohttp giving up.
                # Any of them used to be an anonymous `break`.
                return f"non-text frame: {msg.type.name}"
            event = json.loads(msg.data)
            kind = event.get("type")

            if kind == "session.output_audio.delta":
                # While the key is held the floor is yours: drop the agent's
                # audio rather than talking over you. Flushing at the start of
                # the hold alone would let later deltas resume mid-barge-in.
                if self.holding or self.muted_turn:
                    continue
                # The samples are in "delta" (measured 2026-09-11). Reading
                # "audio" found nothing, so every reply played as silence.
                pcm = base64.b64decode(event.get("delta", ""))
                # The stream never stops - silence arrives as audio too -
                # so the first delta after release is not the first word.
                # The first audible one is.
                if self.awaiting_first_audio and self.released_at \
                        and audible(pcm):
                    gap = time.monotonic() - self.released_at
                    self.awaiting_first_audio = False
                    print(f"[latency] first word {gap:.2f}s after release",
                          flush=True)
                    log("latency", to_first_audio_s=round(gap, 2))
                    # The voice leg's own latency: release to first word, the
                    # part no Manager timing can account for.
                    self._emit("voice.reply_started",
                               duration_ms=round(gap * 1000, 1))
                if self.reply_mode in ("speak", "both"):
                    self.speaker.feed(pcm)
            elif kind == "session.started":
                sid = event.get("session", {}).get("id", "")
                print(f"live session open ({sid}) - transcript: {TRANSCRIPT_LOG}",
                      flush=True)
                log("live_session", session_id=sid)
                self._emit("voice.session_started", provider_session_id=sid,
                           data={"model": MODEL, "voice": VOICE})
            elif kind == "session.input_transcript.delta":
                # The transcript arrives a word at a time now, with no
                # event to say the utterance is over: the key's release
                # is what ends it, and _settle_ask waits for the tail of
                # the words to catch up with the finger.
                text = event.get("delta", "")
                self.asked_parts.append(text)
                self._arm_ask()
                if text.strip():
                    print(f"you: {text}", flush=True)
                    log("you", text=text)
                    # Kept as cut, leading space and all: a mark can arrive
                    # split across fragments ("[tong", "ue click ]") and is
                    # only recognisable once they are joined.
                    self.spoken.append((time.monotonic(), text))
                    # What the ear actually heard, before anything acts on it.
                    self._emit("voice.transcript_added",
                               data={"text": text[:300]})
                if boss.SHOW_CAPTION or boss.CAPSULE_WORDS:
                    # Show it as it lands, so a misheard word is visible before
                    # it is acted on.
                    self.heard = without_noise(join_fragments([self.heard, text]))
                    if boss.SHOW_CAPTION:
                        self.ui.send(heard=self.heard)
                    if boss.CAPSULE_WORDS:
                        self.ui.send(words=self.heard)
            elif kind == "session.output_transcript.delta":
                # There is no event for an answer beginning or ending -
                # only its words, as they are said. A gap in them is the
                # end (_settle_reply); the first word after one is a new
                # answer.
                if self.reply_settle is None:
                    self.turn_id = event.get("event_id")
                    self.reply_text = ""
                    self.fresh_turn = True
                    self.muted_turn = False
                self.reply_text += event.get("delta", "")
                self._arm_reply()
                self._stream_card()
            elif kind == "session.delegation.created":
                # The model has decided this one needs the machine. The
                # words are already on their way to the Boss (the key's
                # release sends them), so this is a note in the log, not
                # a second run.
                self._emit("voice.delegation_created", data={
                    "delegation_id": (event.get("delegation") or {}).get("id", ""),
                    "offset_ms": event.get("offset_ms")})
            elif kind == "error":
                error = event.get("error")
                print(f"live api error: {error}",
                      file=sys.stderr, flush=True)
                application_log("voice", "voice.provider_error",
                                str(error)[:300], severity="error")
                self._emit("voice.error", severity="error",
                           data={"error": str(error)[:300]})
                refusal = (refusal_from_error(error)
                           if isinstance(error, dict) else None)
                if refusal:
                    self.refusal = refusal
                    self.refused_key = error.get("code") == "invalid_api_key"
                    return KEY_REFUSED
            elif kind == "session.closed":
                self._emit("voice.session_closed")
                return "closed_by_server"
        return "socket ended"

    def card(self, body: str, status: str = "working", collapse: bool = False,
             title: str | None = None, title_style: str = "plain",
             fresh: bool = False, tail: bool = False) -> None:
        if self.reply_mode == "speak":
            return                      # spoken only: no written updates
        self.ui.send(card={"title": title if title is not None else self.request,
                           "body": body, "status": status, "collapse": collapse,
                           "title_style": title_style, "fresh": fresh,
                           "tail": tail})

    async def _thinking_line(self, request: str) -> None:
        """Claude Code's waiting line: a gerund, a glyph, and elapsed time."""
        word = random.choice(boss.THINKING_WORDS)
        started = self.work_started = time.monotonic()
        frame = 0
        try:
            while True:
                glyph = boss.THINKING_GLYPHS[frame % len(boss.THINKING_GLYPHS)]
                elapsed = int(time.monotonic() - started)
                self.card("", status="working", collapse=True,
                          title=f"{glyph} {word}… ({elapsed}s)",
                          title_style="thinking", fresh=(frame == 0))
                frame += 1
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        self.stopping = True         # the loop must not reconnect after this
        if self.ws and not self.ws.closed:
            try:
                await self.ws.send_json({"type": "session.close",
                                         "event_id": self._next_id("event_close")})
            except (ConnectionResetError, aiohttp.ClientError):
                application_log("voice", "voice.close_failed",
                                "the session was already gone at shutdown",
                                severity="debug", exc_info=True)


class HotkeyListener:
    """hotkey.py, read on a thread of its own.

    The key must reach the microphone gate the moment it moves, and the
    event loop cannot promise that: any synchronous call in the process
    stops it, and while it is stopped the hold is not seen, the audio is
    silence, and the words are gone. So the pipe is read off the loop.
    key_changed flips the gate right there; the loop only gets told.
    """

    def __init__(self, spawn: list[str], agent: VoiceAgent,
                 on_change=None) -> None:
        self.spawn = spawn
        self.agent = agent
        self.on_change = on_change          # loop-side hook, called with down
        self.proc: subprocess.Popen | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ended = asyncio.Event()        # the hotkey process went away

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.proc = subprocess.Popen(
            [*self.spawn, str(HERE / "hotkey.py")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._read_events, name="hotkey",
                         daemon=True).start()
        threading.Thread(target=self._read_stderr, name="hotkey-stderr",
                         daemon=True).start()

    def _read_events(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            for raw in self.proc.stdout:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    application_log("hotkey", "hotkey.invalid_message",
                                    "hotkey sent a line that is not JSON",
                                    severity="warning",
                                    line=raw.decode(errors="replace")[:300])
                    continue
                if event.get("error"):
                    print(f"hotkey: {event['error']} - {event.get('hint', '')}",
                          file=sys.stderr, flush=True)
                    application_log("hotkey", "hotkey.error",
                                    str(event["error"]), severity="error",
                                    hint=event.get("hint", ""))
                    continue
                if "fn" in event:
                    down = bool(event["fn"])
                    # Two quick taps are a gesture rather than an utterance:
                    # the flag rides on the release that completes them.
                    double = bool(event.get("double"))
                    application_log("hotkey", "hotkey.key_changed",
                                    f"fn {'down' if down else 'up'} via "
                                    f"{event.get('source', '?')} "
                                    f"flags={event.get('flags', '?')}"
                                    f"{' (double tap)' if double else ''}",
                                    severity="debug",
                                    source=event.get("source", ""),
                                    flags=event.get("flags", ""),
                                    double=double)
                    self.agent.key_changed(down, double=double)
                    if self.loop is not None and double:
                        # After key_changed, which has already queued the
                        # key's own bookkeeping: the gesture is the last
                        # thing this release means, not the first.
                        self.loop.call_soon_threadsafe(
                            self.agent.toggle_overlay)
                    if self.on_change is not None and self.loop is not None:
                        self.loop.call_soon_threadsafe(self.on_change, down)
        finally:
            if self.loop is not None:
                try:
                    self.loop.call_soon_threadsafe(self.ended.set)
                except RuntimeError:
                    pass                    # the loop is already gone

    def _read_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for raw in self.proc.stderr:
            application_log("hotkey", "hotkey.stderr",
                            raw.decode(errors="replace").rstrip(),
                            severity="warning")

    async def wait_with(self, session_task: asyncio.Task) -> None:
        """Return when the hotkey goes away, the session does, or the
        user quits from the overlay."""
        gone = asyncio.ensure_future(self.ended.wait())
        quit_ = asyncio.ensure_future(self.agent.ui.quit_requested.wait())
        try:
            await asyncio.wait({session_task, gone, quit_},
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            gone.cancel()
            quit_.cancel()

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-write", action="store_true",
                        help="let Claude use Edit, Write and Bash (risky by voice)")
    parser.add_argument("--reply", choices=("speak", "text", "both"),
                        default=None,
                        help="answer aloud, on the card, or both "
                             f"(default: {boss.REPLY_MODE}, set in boss.py)")
    parser.add_argument("--no-hotkey", action="store_true",
                        help="start talking immediately instead of waiting for Fn")
    parser.add_argument("--debug", action="store_true",
                        help="verbose console logging (the file log is "
                             "always complete)")
    args = parser.parse_args()

    reply_mode = args.reply or boss.REPLY_MODE

    log_path = configure_logging(DEFAULT_HOME, debug=args.debug)
    install_asyncio_exception_handler(asyncio.get_running_loop())
    stall_monitor = start_loop_stall_monitor("voice")
    print(f"debug log: {log_path}\nrun id: {current_run()}", flush=True)
    application_log("voice", "app.started", "voice agent starting",
                    reply=reply_mode, allow_write=args.allow_write,
                    no_hotkey=args.no_hotkey)

    load_env()
    api_key = (os.environ.get("OPENAI_API_KEY", "")
               or await asyncio.to_thread(ask_for_api_key))
    withhold_api_key()
    if not api_key:
        print("OPENAI_API_KEY is empty - paste your key into .env", file=sys.stderr)
        application_log("voice", "app.missing_api_key",
                        "OPENAI_API_KEY is empty", severity="error")
        return 1

    # The two grants the voice itself needs, asked for up front: a missing
    # Microphone or Input Monitoring otherwise surfaces as an agent that
    # simply never hears anything.
    await asyncio.to_thread(ask_for_missing_grants, DEFAULT_HOME,
                            grants=VOICE_GRANTS)

    # Prefer a local arm64 uv when one is installed; otherwise take PATH.
    local_uv = Path.home() / ".local/bin/uv"
    uv = str(local_uv) if local_uv.exists() else (shutil.which("uv") or "uv")
    spawn = [uv, "run", "--python-preference", "only-managed", "--python", "3.13"]
    # stdout is the overlay's NDJSON protocol; only stderr is captured.
    overlay = await asyncio.create_subprocess_exec(
        *spawn, str(HERE / "overlay.py"), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    ui = Ui(overlay)
    stderr_readers = [asyncio.create_task(
        drain_subprocess_stderr(overlay.stderr, "overlay"))]

    # The session is opened once and stays open; Fn only gates the microphone,
    # so there is no connection latency on the first word.
    # Voice events go to the same local store the conductor already writes,
    # so `conductor.viewer` shows both halves of one interaction.
    bus = ObservabilityBus()
    bus.subscribe(JsonlSink(DEFAULT_HOME))
    bus.subscribe(LoggingSink())
    agent = VoiceAgent(api_key, ui, args.allow_write, reply_mode, bus=bus)
    session_task = asyncio.create_task(agent.run())

    hotkey = None
    if not args.no_hotkey:
        hotkey = HotkeyListener(spawn, agent)
        hotkey.start(asyncio.get_running_loop())

    try:
        if args.no_hotkey:
            agent.holding = True
            await ui.wait_with(session_task)
        else:
            print("hold Fn and talk, release to get an answer, "
                  "double-tap Fn for notifications, ctrl-c to quit",
                  flush=True)
            await hotkey.wait_with(session_task)
    except (KeyboardInterrupt, asyncio.CancelledError):
        application_log("voice", "app.interrupted",
                        "shutting down on interrupt")
    except Exception:
        application_log("voice", "app.crashed",
                        "the voice agent exited with an unhandled error",
                        severity="error", exc_info=True)
        raise
    finally:
        agent.holding = False
        await agent.close()
        session_task.cancel()
        for reader in stderr_readers:
            reader.cancel()
        stall_monitor.cancel()
        ui.send(state="quit")
        if hotkey is not None:
            hotkey.stop()
        if overlay.returncode is None:
            overlay.terminate()
        application_log("voice", "app.stopped", "voice agent stopped")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
