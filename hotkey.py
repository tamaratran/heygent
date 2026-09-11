#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyobjc-framework-Quartz>=10,<13"]
# ///

"""Watch the Fn key and print its state as NDJSON.

Fn is not an ordinary key on macOS: it never arrives as a keyDown, only as a
flagsChanged event carrying kCGEventFlagMaskSecondaryFn. Reading it needs a
Quartz event tap, which needs Input Monitoring permission for whichever app
owns this process (Terminal, iTerm, ...).

    {"fn": true}      # pressed
    {"fn": false}     # released

Two taps in immediate succession are also a gesture of their own - it is
what shows and hides the notifications - and the release that completes one
carries a flag saying so:

    {"fn": false, "double": true}

It rides on that release rather than arriving as a line of its own, so a
consumer can never act on the gesture before the key movement it came from.

The flag stream lies, so it is not believed on its own. Two things went
wrong in one recorded session:

  - the Fn bit dropped for a fraction of a millisecond while the key was
    still physically down. Five holds in one evening were reported as
    lasting between 0.6 ms and 2.9 ms - no other hold was under a second -
    and every one of them closed the microphone in the middle of a
    sentence the user was already speaking.
  - the tap itself can be switched off by macOS, which announces it
    through this same callback. That notification carries no modifier
    flags, so reading it as one reported a release the user never made,
    and the tap stayed off afterwards.

  - a hold ran for 258.3 s. Measured 2026-09-03T02:23:07Z: the gate
    opened on a poll-sourced press and did not close until 02:27:25Z
    (`voice.listening_stopped`, duration_ms 258275.5), while two
    utterances of the room's own conversation were transcribed and sent
    on as if the user had asked for them. Every hold the user actually
    made that session lasted between 1.3 s and 12.0 s. Whether the press
    was invented or its release was simply missed is not decidable from
    the log - the poll reads CGEventSourceFlagsState and the tap reads
    CGEventGetFlags, which return different flag words, so the extra
    0x20000000 bit on the poll's reading distinguishes the two APIs and
    not a real press from a false one. The length is the evidence.

Neither shows up as an error anywhere downstream: a released key means
silence is fed to the session on purpose, so a phantom release is
indistinguishable from a user who stopped talking - and a phantom press
is indistinguishable from a user who chose to talk. Hence three defences,
all here at the source:

  - a release is only reported once the key has stayed up for
    RELEASE_DEBOUNCE_S. A press is reported the instant it arrives,
    because that is the latency the user feels; a release is not.
  - the hardware state is polled independently of the tap, so a tap that
    lies or dies cannot pin the key in the wrong state. The poll may keep
    a hold alive and it may end one; it may never START one, because the
    tap is the only witness to a press that a human actually made.
  - a hold has a ceiling. Nothing else here reasons about how long the
    key has been down, so a stuck bit that survives every check above
    still holds the microphone open for ever; MAX_HOLD_S ends it.
"""

from __future__ import annotations

import json
import sys
import time

FN_MASK = 0x800000  # kCGEventFlagMaskSecondaryFn
NUMPAD_MASK = 0x200000  # kCGEventFlagMaskNumericPad


def fn_held(flags: int) -> bool:
    """Whether these modifier flags mean the Fn key itself is down.

    On Apple keyboards the arrow keys, Home, End, Page Up/Down and forward
    Delete are "keypad" keys, and macOS reports every one of them with the
    Fn bit set for as long as it is held - alongside the NumericPad bit,
    which the Fn key alone never sets. Read on the Fn bit only, holding an
    arrow key to scroll a log was a hold: the microphone opened for 3.7 s
    and transcribed a video playing in the room, and a single arrow tap was
    a 0.0 ms "hold" (measured, 2026-08-28). The user was not pressing Fn.
    """
    return bool(flags & FN_MASK) and not (flags & NUMPAD_MASK)

# How long the key must stay up before a release is believed. Long enough to
# outlast the flag dropping out on its own, short enough that nobody can
# feel it at the end of an utterance.
RELEASE_DEBOUNCE_S = 0.06

# How often the real hardware state is re-read. Also what drives a pending
# release to maturity when the tap has gone quiet - the tap only speaks on
# edges, and a release that is never confirmed would never be reported.
POLL_INTERVAL_S = 0.025

# What makes two presses a double tap: both short, and almost nothing
# between them. Both halves have to be taps because pressing to talk twice
# in quick succession is an ordinary thing to do, and must never move the
# notifications out from under the user.
TAP_MAX_S = 0.35        # longest press that still counts as a tap
TAP_GAP_MAX_S = 0.35    # longest gap from the first tap's release to the
                        # second tap's press

# The longest a hold is believed. Push-to-talk is held by a finger, and a
# finger does not hold for half a minute: past this the key state is wrong,
# whatever it says. Measured over one session, the longest hold a user
# actually made was 12.0 s and the stuck one ran to 258.3 s. High enough
# that no real utterance reaches it, low enough that a stuck key costs the
# user seconds of open microphone rather than minutes.
MAX_HOLD_S = 30.0


class FnGate:
    """Turn a noisy stream of key-state observations into honest events.

    Kept free of Quartz so the debounce can be tested without an event tap
    and without a Mac. `now` is passed in for the same reason.
    """

    def __init__(self, debounce: float = RELEASE_DEBOUNCE_S,
                 max_hold: float = MAX_HOLD_S) -> None:
        self.debounce = debounce
        self.max_hold = max_hold
        self.held = False            # what the consumer has been told
        self.capped = False          # the last release was the ceiling, not a finger
        self._up_since: float | None = None
        self._held_since: float | None = None
        self._poll_saw_key = False   # has the poll ever proved it sees Fn?

    def observe(self, down: bool, now: float) -> bool | None:
        """An edge from the event tap. Returns what to report, or None.

        Going down is reported immediately. Going up starts a clock, and is
        only reported if the key is still up when the clock runs out - so a
        flag that drops out and comes back is one unbroken hold, and never
        reaches the microphone as a release at all.
        """
        if down:
            self._up_since = None
            if not self.held:
                self.held = True
                # Only a newly reported press starts the clock. A flicker
                # or a poll confirming the key is still down must not
                # extend it, or the ceiling below could never be reached -
                # the stuck poll re-asserted the press every 25 ms.
                self._held_since = now
                self.capped = False
                return True
            return None
        if not self.held:
            return None
        if self._up_since is None:
            self._up_since = now
        if now - self._up_since >= self.debounce:
            self._up_since = None
            self.held = False
            return False
        return None

    def confirm(self, down: bool, now: float) -> bool | None:
        """A poll of the real hardware state, which is not trusted blindly.

        A poll that says the key is DOWN keeps a hold alive: that is the
        evidence that kills a phantom release, and it is the whole point.
        It does not START one. A press invented here opens the microphone
        with no finger behind it, and nothing downstream can tell it from
        one the user made. A press has
        exactly one witness, the tap. If the tap misses one the user gets
        no microphone and presses again; if the poll invents one the user
        gets a microphone they did not ask for - not the same cost.

        A poll that says UP is only allowed to finish a release the tap
        already started - until a poll has seen the key down at least once
        and so proved it can see Fn at all. Not every Mac reports this key
        in the hardware flags, and one that does not would otherwise look
        like a key held permanently released: worse than the bug this is
        here to fix. Seeing the key down still proves that, whether or not
        the sighting is allowed to open the gate.
        """
        if self._expired(now):
            return False
        if down:
            self._poll_saw_key = True
            if not self.held:
                return None          # the poll may confirm a press, never make one
            return self.observe(True, now)
        if self._poll_saw_key or self._up_since is not None:
            return self.observe(False, now)
        return None

    def tick(self, now: float) -> bool | None:
        """Time passing with no evidence at all - the poll could not read
        the key. It may finish a pending release, or hit the ceiling, and
        nothing else."""
        if self._expired(now):
            return False
        if self._up_since is None:
            return None
        return self.observe(False, now)

    def _expired(self, now: float) -> bool:
        """Has this hold outlasted any hold a person makes?

        Checked on every poll rather than on tap edges: a key stuck down
        produces no edges at all, which is exactly why nothing else here
        notices it. Reported as an ordinary release, because that is what
        the microphone needs to do; `capped` says it was the ceiling and
        not a finger, so the log can tell them apart.
        """
        if not self.held or self._held_since is None:
            return False
        if now - self._held_since < self.max_hold:
            return False
        self.held = False
        self.capped = True
        self._up_since = None
        self._held_since = None
        return True


class DoubleTap:
    """Recognise two quick taps in the stream of Fn events.

    Fed exactly what the consumer is told rather than what the tap saw, so
    the release debounce is already inside these numbers: a tap measures
    RELEASE_DEBOUNCE_S longer here than the finger made it, and the gap
    between two taps that much shorter. Both thresholds allow for that.

    A press long enough to speak into is not a tap, and clears whatever
    gesture was half-finished - so holding the key to talk, twice in a row,
    is two utterances and nothing else.

    Kept free of Quartz, and given `now`, for the same reason FnGate is:
    so the gesture can be driven from a test without a Mac.
    """

    def __init__(self, tap_max: float = TAP_MAX_S,
                 gap_max: float = TAP_GAP_MAX_S) -> None:
        self.tap_max = tap_max
        self.gap_max = gap_max
        self._down_at: float | None = None    # the press now in progress
        self._tapped_at: float | None = None  # when a first tap ended

    def observe(self, down: bool, now: float) -> bool:
        """One reported edge. True when it completes a double tap."""
        if down:
            self._down_at = now
            return False
        pressed_at, self._down_at = self._down_at, None
        if pressed_at is None:
            return False                 # a release with no press behind it
        if now - pressed_at > self.tap_max:
            self._tapped_at = None       # a hold: not part of any gesture
            return False
        first, self._tapped_at = self._tapped_at, now
        if first is None or pressed_at - first > self.gap_max:
            return False                 # a first tap, not a second
        # A third tap starts a new gesture rather than completing another
        # one, so three taps are one toggle and not two.
        self._tapped_at = None
        return True


def emit(**event) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def note(message: str) -> None:
    """Diagnostics go to stderr, which the parent drains into the log as
    hotkey.stderr. stdout is the event channel and carries nothing else."""
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def main() -> int:
    import Quartz

    # Named on some pyobjc builds and not others; the values are stable.
    TAP_DISABLED = {
        getattr(Quartz, "kCGEventTapDisabledByTimeout", 0xFFFFFFFE),
        getattr(Quartz, "kCGEventTapDisabledByUserInput", 0xFFFFFFFF),
    }
    HID_STATE = getattr(Quartz, "kCGEventSourceStateHIDSystemState", 1)

    gate = FnGate()
    taps = DoubleTap()
    tap = None
    polling = True

    def report(state: bool | None, source: str = "", flags: int = 0) -> None:
        # Where the edge came from and the raw flags travel with it, so a
        # hold nobody made can be explained from the log instead of guessed.
        if state is None:
            return
        if state is False and gate.capped:
            # Not a release anybody made: the hold outlasted MAX_HOLD_S and
            # was ended on its behalf. Named apart from a poll's release so
            # the log says which happened, and said once on stderr, because
            # a microphone open that long is worth an entry of its own.
            source = "cap"
            note(f"a hold reached the {MAX_HOLD_S:.0f}s ceiling and was "
                 "ended; the key state was wrong, not the finger")
        event = {"fn": state, "source": source, "flags": f"0x{flags:x}"}
        # The gesture is read from the events that are actually reported,
        # which is what the rest of the app sees, and rides on the release
        # that completes it. A capped release is a thirty-second hold, far
        # too long to be a tap, so it can never complete one.
        if taps.observe(state, time.monotonic()):
            event["double"] = True
        emit(**event)

    def on_event(proxy, event_type, event, refcon):
        # macOS switches a tap off when it decides the process answered too
        # slowly, and says so through this callback rather than out of band.
        # The notification has no modifier flags on it, so reading it as one
        # reports Fn released - and without re-enabling, the tap never speaks
        # again, which is a hotkey that silently stops working for good.
        if event_type in TAP_DISABLED:
            Quartz.CGEventTapEnable(tap, True)
            note(f"event tap disabled ({event_type}); re-enabled")
            return event
        flags = Quartz.CGEventGetFlags(event)
        report(gate.observe(fn_held(flags), time.monotonic()),
               source="tap", flags=flags)
        return event

    def on_tick(timer, info) -> None:
        # The tap reports edges; this reports the truth. Polling the HID
        # state means a tap that missed an edge, lied about one, or died
        # cannot leave the key stuck in the wrong state - and it is what
        # matures a pending release when no further edge is coming.
        nonlocal polling
        now = time.monotonic()
        if not polling:
            report(gate.tick(now), source="tick")
            return
        try:
            flags = Quartz.CGEventSourceFlagsState(HID_STATE)
        except Exception as exc:      # pragma: no cover - platform specific
            polling = False
            note(f"cannot read the hardware key state ({exc}); "
                 "falling back to the event tap alone")
            report(gate.tick(now), source="tick")
            return
        report(gate.confirm(fn_held(flags), now), source="poll", flags=flags)

    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap,
        Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionListenOnly,   # observe only, never swallow keys
        Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged),
        on_event,
        None,
    )
    if tap is None:
        emit(error="event tap refused",
             hint="Grant Input Monitoring to your terminal app in "
                  "System Settings > Privacy & Security > Input Monitoring, "
                  "then restart it.")
        return 1

    source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), source,
                              Quartz.kCFRunLoopCommonModes)
    Quartz.CGEventTapEnable(tap, True)

    timer = Quartz.CFRunLoopTimerCreate(
        None, Quartz.CFAbsoluteTimeGetCurrent() + POLL_INTERVAL_S,
        POLL_INTERVAL_S, 0, 0, on_tick, None)
    Quartz.CFRunLoopAddTimer(Quartz.CFRunLoopGetCurrent(), timer,
                             Quartz.kCFRunLoopCommonModes)

    emit(ready=True)
    Quartz.CFRunLoopRun()
    return 0


if __name__ == "__main__":
    sys.exit(main())
