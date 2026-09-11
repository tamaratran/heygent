"""Whose keyboard is it, at the instant we press a key?

The Conductor is not the only thing driving this screen. On 2026-09-08
ChatGPT's computer-use agent was running on the same machine, and our
worker and theirs took turns writing over each other: a "?" went to a
contact nobody meant to message, and a word said out loud to the Boss
landed in a Messages compose box. Two agents, one keyboard, no shared
notion of whose turn it is - and there is no such notion to share. macOS
offers no arbitration between automation clients; nothing announces
"I have the input".

So this is not arbitration. It is a warning that an action was taken on a
stale view of the world. What macOS does offer, all of it used here:

    CGEventSourceCounterForEventType   how many key presses and mouse
                                       clicks the system has seen, ever
    CGEventSourceSecondsSinceLastEventType
                                       how long since the last input of
                                       any kind
    CGWindowListCopyWindowInfo         every on-screen window, front to
                                       back, with owner pid and bounds
    AXUIElementCopyElementAtPosition   what is under a point, and which
                                       process owns it
    AXFocusedApplication / AXFocusedUIElement
                                       where typing would go right now
    CGEventTapCreate (listen-only)     an observed event's
                                       kCGEventSourceUnixProcessID: 0 for
                                       hardware, the poster's pid for a
                                       posted one

And what it does NOT offer, measured rather than assumed:

  - There is no point-in-time question that answers "was the last event
    synthetic". The HID and combined event-source states were the obvious
    candidate - HID reads like "hardware only" - and they are not that:
    posting five mouse-moved events to kCGHIDEventTap advanced BOTH
    counters by five (probed 2026-09-09 on this machine). Attribution
    exists only inside an event tap, which is a running observer, not a
    query.
  - Nothing tells you another automation client exists, is running, or
    intends to type. No API, no notification, no lock.

What is genuinely reliable is the negative: a counter that advanced, or a
window that changed, since we last looked means the screen is not what
our screenshot showed - whoever did it. That is the check this module
builds, and it is enough for the damage that happened, because every one
of tonight's mistakes was an action taken on a view of the screen that
had already stopped being true.

The rule, then:

    look             -> record what the screen was
    click/type/key   -> re-verify, act, re-record

Any key press or mouse click the system counted that we did not make, any
change of the frontmost app, any different window under the point we are
about to click: the action still happens, and the worker is told what
changed and to look again. Nothing is refused - the user wants a worker
that is told, not one that is stopped - and LOOKING is what clears it,
which is exactly the thing a worker skipped when it wrote over somebody
else.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

# The counters that mean somebody acted. Cursor movement is deliberately
# not among them: the user's hand is on this machine, the mouse moves
# constantly, and aborting on that would make the guard something a
# worker learns to route around. A press and a click are acts.
COUNTED = ("key_down", "left_click", "right_click")
_COUNTER_EVENTS = {"key_down": "kCGEventKeyDown",
                   "left_click": "kCGEventLeftMouseDown",
                   "right_click": "kCGEventRightMouseDown"}

# How old a sighting may be and still describe the screen. Long enough
# that reading a screenshot and deciding what to click does not cost a
# pointless second look, short enough that it still means "recent" on a
# machine somebody else is also using. The counter and window checks do
# the real work at any age; this is the backstop for a worker that went
# away and came back.
MAX_AGE_S = 120.0
# Input this recently is input happening NOW. If we did not make it,
# something else is mid-action and we are about to interleave with it.
RECENT_INPUT_S = 1.0
# Our own last action happened just before the sighting that followed it,
# so anything the system saw at or before that moment was ours. The grace
# absorbs the gap between posting an event and the sighting being taken.
OURS_UNTIL_S = 0.5
# How long the tap listens when it is asked WHO is posting. Only ever run
# to explain a warning, never on the path of an action nothing is wrong with.
PROBE_S = 0.35


# Where a caret can live. A focused element with one of these roles takes
# typed characters; a button, a list or a bare window swallows them, and
# macOS says nothing about it.
TEXT_ROLES = frozenset({"AXTextField", "AXTextArea", "AXComboBox",
                        "AXSearchField"})
# Controls whose AXPress is the click. Rows, cells and text fields are
# left to a real click on purpose: selecting a row or putting the caret in
# a field is what the pointer does, and a press that "succeeds" without
# doing either sends the next step into nothing.
PRESSABLE_ROLES = frozenset({"AXButton", "AXCheckBox", "AXRadioButton",
                             "AXPopUpButton", "AXMenuButton", "AXLink",
                             "AXMenuItem", "AXMenuBarItem",
                             "AXDisclosureTriangle"})
DOCK_BUNDLE = "com.apple.dock"
# "No value" is an answer - nothing has focus. Any other error is the app
# not answering (a Python process in front gave -25204), which is none.
AX_SUCCESS = 0
AX_NO_VALUE = -25212


@dataclass(frozen=True)
class Element:
    """One accessibility element: what it is, and which app owns it."""
    pid: int = 0
    app: str = ""
    bundle_id: str = ""
    role: str = ""
    subrole: str = ""
    label: str = ""
    actions: tuple = ()
    text_entry: bool = False
    ref: object = field(default=None, compare=False, repr=False)

    @property
    def pressable(self) -> bool:
        return self.role in PRESSABLE_ROLES and "AXPress" in self.actions

    @property
    def in_dock(self) -> bool:
        return self.bundle_id == DOCK_BUNDLE or self.role == "AXDockItem"

    def belongs_to(self, app: str) -> bool:
        wanted = app.strip().lower()
        return bool(wanted) and (wanted in self.app.lower()
                                 or wanted == self.bundle_id.lower())

    def describe(self) -> str:
        kind = (self.subrole or self.role or "element").removeprefix("AX")
        named = f"{kind} {self.label!r}" if self.label else kind
        if self.app:
            return f"{named} in {self.app}"
        return f"{named} in process {self.pid}" if self.pid else named


@dataclass(frozen=True)
class Window:
    pid: int = 0
    number: int = 0
    app: str = ""
    title: str = ""
    bounds: tuple = ()          # x, y, w, h

    def contains(self, x: float, y: float) -> bool:
        if len(self.bounds) != 4:
            return False
        left, top, width, height = self.bounds
        return left <= x < left + width and top <= y < top + height

    def describe(self) -> str:
        name = self.app or "an unnamed app"
        return f"{name} ({self.title})" if self.title else name

    def as_json(self) -> dict:
        return {"pid": self.pid, "number": self.number, "app": self.app,
                "title": self.title, "bounds": list(self.bounds)}


def _window(raw: dict) -> Window:
    bounds = raw.get("bounds") or {}
    if isinstance(bounds, (list, tuple)):
        box = tuple(float(v) for v in bounds[:4])
    else:
        box = (float(bounds.get("X", 0)), float(bounds.get("Y", 0)),
               float(bounds.get("Width", 0)), float(bounds.get("Height", 0)))
    return Window(pid=int(raw.get("pid") or 0),
                  number=int(raw.get("number") or 0),
                  app=str(raw.get("app") or ""),
                  title=str(raw.get("title") or ""),
                  bounds=box)


@dataclass(frozen=True)
class Sighting:
    """What the screen was, the last time we were entitled to believe it."""
    at: float = 0.0
    screenshot: str = ""
    counters: dict = field(default_factory=dict)
    windows: tuple = ()
    focused_pid: int = 0
    focused_role: str = ""

    @property
    def front(self) -> Window | None:
        return self.windows[0] if self.windows else None

    def under(self, x: float, y: float) -> Window | None:
        return next((w for w in self.windows if w.contains(x, y)), None)

    def as_json(self) -> dict:
        return {"at": self.at, "screenshot": self.screenshot,
                "counters": dict(self.counters),
                "windows": [w.as_json() for w in self.windows],
                "focused_pid": self.focused_pid,
                "focused_role": self.focused_role}

    @classmethod
    def from_json(cls, raw: dict) -> "Sighting":
        return cls(at=float(raw.get("at") or 0.0),
                   screenshot=str(raw.get("screenshot") or ""),
                   counters={k: int(v) for k, v in
                             (raw.get("counters") or {}).items()},
                   windows=tuple(_window(w) for w in
                                 (raw.get("windows") or [])),
                   focused_pid=int(raw.get("focused_pid") or 0),
                   focused_role=str(raw.get("focused_role") or ""))


def frontmost_application():
    """The NSRunningApplication in front, or None.

    Read after one short turn of the run loop. A command-line process has
    no running loop, and NSWorkspace only learns that apps launched, quit
    or came forward when its loop turns: without the turn, its app list
    went on reporting what it first read (measured 2026-09-10), and the
    app in front is kept by the same notifications - so after `open` it
    would still name the app that was in front before.
    """
    try:
        from AppKit import NSWorkspace
        from Foundation import NSDate, NSDefaultRunLoopMode, NSRunLoop
    except ImportError:
        return None
    NSRunLoop.currentRunLoop().runMode_beforeDate_(
        NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.01))
    return NSWorkspace.sharedWorkspace().frontmostApplication()


class MacEyes:
    """Everything the guard can ask macOS, in one injectable place."""

    # Layers above the ordinary window layer are menu bars, status items,
    # the cursor and screen-saver furniture. A click never means one of
    # those, and the notification banner that slid in half a second ago
    # must not read as "the front app changed".
    ORDINARY_LAYER = 0

    def __init__(self, quartz=None, ax=None) -> None:
        self._quartz = quartz
        self._ax = ax

    @property
    def quartz(self):
        if self._quartz is None:
            import Quartz
            self._quartz = Quartz
        return self._quartz

    @property
    def ax(self):
        """The accessibility bindings, or None where they are not there.

        Missing bindings are not a reason to act blind, but they are also
        not this module's error to raise: the driver already refuses
        input without the Accessibility grant, in one sentence that says
        what to do. Here they simply mean no opinion on focus.
        """
        if self._ax is None:
            try:
                import ApplicationServices
            except ImportError:
                return None
            self._ax = ApplicationServices
        return self._ax

    def counters(self) -> dict:
        q = self.quartz
        state = q.kCGEventSourceStateCombinedSessionState
        return {name: int(q.CGEventSourceCounterForEventType(
            state, getattr(q, event))) for name, event in
            _COUNTER_EVENTS.items()}

    def seconds_since_input(self) -> float:
        q = self.quartz
        return float(q.CGEventSourceSecondsSinceLastEventType(
            q.kCGEventSourceStateCombinedSessionState,
            q.kCGAnyInputEventType))

    def windows(self) -> tuple:
        """On-screen ordinary windows, front to back."""
        q = self.quartz
        listing = q.CGWindowListCopyWindowInfo(
            q.kCGWindowListOptionOnScreenOnly
            | q.kCGWindowListExcludeDesktopElements, q.kCGNullWindowID)
        out = []
        for raw in listing or ():
            if int(raw.get("kCGWindowLayer") or 0) != self.ORDINARY_LAYER:
                continue
            bounds = raw.get("kCGWindowBounds") or {}
            out.append(Window(
                pid=int(raw.get("kCGWindowOwnerPID") or 0),
                number=int(raw.get("kCGWindowNumber") or 0),
                app=str(raw.get("kCGWindowOwnerName") or ""),
                title=str(raw.get("kCGWindowName") or ""),
                bounds=(float(bounds.get("X", 0)), float(bounds.get("Y", 0)),
                        float(bounds.get("Width", 0)),
                        float(bounds.get("Height", 0)))))
        return tuple(out)

    def front_pid(self) -> int:
        """The process of the app in front, or 0."""
        app = frontmost_application()
        return int(app.processIdentifier()) if app is not None else 0

    def _focused_node(self) -> tuple[int, object]:
        """The focused element, asked of the app in front first.

        The system-wide element is the documented place to ask, and on
        this Mac it answered -25204 (cannot complete) with Python, Notes
        and Signal in front, for the focused application as well - while
        Signal's own application element named its focused text area at
        once (probed 2026-09-10). Asked system-wide only, every focus
        check here was quietly checking nothing.
        """
        ax = self.ax
        pid = self.front_pid()
        if pid:
            err, node = ax.AXUIElementCopyAttributeValue(
                ax.AXUIElementCreateApplication(pid), "AXFocusedUIElement",
                None)
            if err in (AX_SUCCESS, AX_NO_VALUE):
                return err, node
        return ax.AXUIElementCopyAttributeValue(
            ax.AXUIElementCreateSystemWide(), "AXFocusedUIElement", None)

    def focus(self) -> tuple[int, str]:
        """The pid typing would reach, and the role of the element in it."""
        ax = self.ax
        if ax is None:
            return 0, ""
        err, element = self._focused_node()
        if err != 0 or element is None:
            return 0, ""
        _, pid = ax.AXUIElementGetPid(element, None)
        _, role = ax.AXUIElementCopyAttributeValue(element, "AXRole", None)
        return int(pid or 0), str(role or "")

    def element_at(self, x: float, y: float) -> tuple[int, str]:
        """The pid and role of whatever is under a point."""
        ax = self.ax
        if ax is None:
            return 0, ""
        system = ax.AXUIElementCreateSystemWide()
        err, element = ax.AXUIElementCopyElementAtPosition(
            system, float(x), float(y), None)
        if err != 0 or element is None:
            return 0, ""
        _, pid = ax.AXUIElementGetPid(element, None)
        _, role = ax.AXUIElementCopyAttributeValue(element, "AXRole", None)
        return int(pid or 0), str(role or "")

    def focused_element(self) -> Element | None:
        """Where typed characters would go right now, in detail.

        None when there is no answer - no bindings, or an app in front
        that does not answer accessibility queries - which is no opinion.
        An empty Element when macOS answers that nothing has the focus,
        which is an answer.
        """
        if self.ax is None:
            return None
        err, node = self._focused_node()
        if err == AX_NO_VALUE or (err == AX_SUCCESS and node is None):
            return Element()
        if err != AX_SUCCESS:
            return None
        return self._element(node)

    def element_under(self, x: float, y: float) -> Element | None:
        """What a click at this point would land on, in detail."""
        ax = self.ax
        if ax is None:
            return None
        system = ax.AXUIElementCreateSystemWide()
        err, node = ax.AXUIElementCopyElementAtPosition(
            system, float(x), float(y), None)
        if err != AX_SUCCESS or node is None:
            return None
        return self._element(node)

    def _element(self, node) -> Element:
        ax = self.ax

        def read(name):
            err, value = ax.AXUIElementCopyAttributeValue(node, name, None)
            return value if err == AX_SUCCESS else None

        _, pid = ax.AXUIElementGetPid(node, None)
        pid = int(pid or 0)
        names_err, names = ax.AXUIElementCopyAttributeNames(node, None)
        names = set(names or ()) if names_err == AX_SUCCESS else set()
        actions_err, actions = ax.AXUIElementCopyActionNames(node, None)
        _, settable = ax.AXUIElementIsAttributeSettable(node, "AXValue", None)
        role = str(read("AXRole") or "")
        text_entry = (role in TEXT_ROLES
                      # Chromium marks what sits inside a contenteditable
                      or "AXEditableAncestor" in names
                      # a custom text view: a caret range, a writable value
                      or ("AXSelectedTextRange" in names and bool(settable)))
        label = next((" ".join(str(value).split())[:80] for value in
                      (read("AXTitle"), read("AXDescription"),
                       read("AXPlaceholderValue")) if value), "")
        app, bundle_id = self.app_of(pid)
        return Element(pid=pid, app=app, bundle_id=bundle_id, role=role,
                       subrole=str(read("AXSubrole") or ""), label=label,
                       actions=tuple(str(a) for a in actions or ())
                       if actions_err == AX_SUCCESS else (),
                       text_entry=text_entry, ref=node)

    def app_of(self, pid: int) -> tuple[str, str]:
        """The name and bundle id of the app a process belongs to."""
        if not pid:
            return "", ""
        try:
            from AppKit import NSRunningApplication
        except ImportError:
            return "", ""
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(
            pid)
        if app is None:
            return "", ""
        return str(app.localizedName() or ""), str(app.bundleIdentifier() or "")

    def posting_processes(self, seconds: float = PROBE_S) -> set:
        """Which processes post input events while we watch.

        A listen-only tap is the ONE place macOS attributes an event: a
        tapped event's kCGEventSourceUnixProcessID is 0 for a key a
        person pressed and the poster's pid for one a program posted.
        It is a running observer, not a question, so it is asked only to
        name the thing that has already been caught by a counter - never
        on the path of an action the guard is going to allow.
        """
        q = self.quartz
        mask = 0
        for event in ("kCGEventKeyDown", "kCGEventLeftMouseDown",
                      "kCGEventRightMouseDown", "kCGEventMouseMoved"):
            mask |= q.CGEventMaskBit(getattr(q, event))
        seen: set = set()

        def callback(proxy, kind, event, refcon):
            seen.add(int(q.CGEventGetIntegerValueField(
                event, q.kCGEventSourceUnixProcessID)))
            return event

        tap = q.CGEventTapCreate(q.kCGSessionEventTap,
                                 q.kCGHeadInsertEventTap,
                                 q.kCGEventTapOptionListenOnly,
                                 mask, callback, None)
        if tap is None:
            return set()
        source = q.CFMachPortCreateRunLoopSource(None, tap, 0)
        q.CFRunLoopAddSource(q.CFRunLoopGetCurrent(), source,
                             q.kCFRunLoopCommonModes)
        q.CGEventTapEnable(tap, True)
        q.CFRunLoopRunInMode(q.kCFRunLoopDefaultMode, float(seconds), False)
        q.CGEventTapEnable(tap, False)
        q.CFRunLoopRemoveSource(q.CFRunLoopGetCurrent(), source,
                                q.kCFRunLoopCommonModes)
        return seen


def state_path(directory: str | Path, task_id: str = "") -> Path:
    """One sighting per worker, not one per machine.

    Two of our own computer-use workers can be running at once, and a
    shared file would let each overwrite the other's baseline - which is
    exactly how one of them stops noticing that the other just clicked.
    Kept apart, each measures against its own last action, so the other's
    clicks show up in the counters as what they are: not ours.
    """
    leaf = f"screen-guard-{task_id}.json" if task_id else "screen-guard.json"
    return Path(directory) / leaf


class Guard:
    """The look-then-act discipline, enforced.

    Times are injectable so the age rules are testable without sleeping.
    """

    def __init__(self, directory: str | Path, eyes: MacEyes | None = None,
                 now=time.time, task_id: str = "") -> None:
        self.path = state_path(directory, task_id)
        self.eyes = eyes or MacEyes()
        self.now = now

    # -- recording -------------------------------------------------------
    def observe(self, screenshot: str = "") -> Sighting:
        """What the screen is, right now."""
        pid, role = self.eyes.focus()
        return Sighting(at=self.now(), screenshot=screenshot,
                        counters=self.eyes.counters(),
                        windows=self.eyes.windows(),
                        focused_pid=pid, focused_role=role)

    def record(self, screenshot: str = "") -> Sighting:
        """Observe and make it the sighting everything is measured from.

        Called by `look`, and again after every action of ours that
        succeeded - so what the guard compares against is always "the
        screen as our own last act left it", and any later change is
        somebody else's.
        """
        sighting = self.observe(screenshot)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(sighting.as_json(), indent=2))
        temporary.replace(self.path)
        return sighting

    def last(self) -> Sighting | None:
        try:
            return Sighting.from_json(json.loads(self.path.read_text()))
        except (OSError, ValueError, TypeError):
            return None

    # -- checking --------------------------------------------------------
    def check(self, action: str, point: tuple | None = None,
              expect: str = "", target: str = "front") -> list[str]:
        """What is no longer as we last saw it, as warnings.

        Said, never refused: the action goes ahead, and these sentences
        are printed after it, so the worker knows to look again and put
        right anything that landed somewhere it did not mean. The user
        asked for a worker that is told, not one that is stopped.

        target says what this action aims at, because that is what has to
        still be true: "point" for a click (the window under it), "focus"
        for typing (the caret, which is where the characters actually go),
        "front" for anything aimed at the frontmost window, and "none" for
        moving the cursor, which aims at nothing and only has to be sure
        nobody else is mid-action.
        """
        before = self.last()
        if before is None:
            return [f"nothing had looked at this screen before this {action}, "
                    "so there was no telling what was under the cursor. Look "
                    "now, and check it did what was meant."]
        warnings = []
        age = self.now() - before.at
        if age > MAX_AGE_S:
            warnings.append(
                f"the last look at this screen was {age:.0f}s before this "
                f"{action}, and this machine has other people and other "
                "agents on it. Look again to check what it did.")
        now_sighting = self.observe(before.screenshot)
        warnings += self._not_someone_else(action, before, now_sighting)
        warnings += self._same_place(action, before, now_sighting, point,
                                     expect, target)
        return warnings

    def _not_someone_else(self, action: str, before: Sighting,
                          now_sighting: Sighting) -> list[str]:
        moved = {name: now_sighting.counters.get(name, 0)
                 - before.counters.get(name, 0)
                 for name in COUNTED}
        busy = {name: count for name, count in moved.items() if count > 0}
        if busy:
            return [f"{self._describe(busy)} between our last look and this "
                    f"{action}, and none of it was ours" + self._blame()
                    + ". Something else may be using this keyboard: look "
                    "again, and if the screen is someone else's right now, "
                    "say so."]
        # And input happening right now, which a counter has not caught up
        # with yet: between our look and our act is milliseconds, and a
        # foreign agent mid-action will often not have pressed anything in
        # that window. Our OWN last action was posted just before the
        # sighting that recorded it, so only input the system saw after
        # that moment can be somebody else's - otherwise every second
        # click in a row would be warned about on account of the first.
        idle = self.eyes.seconds_since_input()
        if idle < RECENT_INPUT_S \
                and self.now() - idle > before.at + OURS_UNTIL_S:
            return [f"something used the keyboard or mouse {idle:.1f}s "
                    f"before this {action}, and it was not us"
                    + self._blame() + ". Look again to check nobody was "
                    "mid-action."]
        return []

    def _same_place(self, action: str, before: Sighting, now_sighting: Sighting,
                    point: tuple | None, expect: str,
                    target: str) -> list[str]:
        warnings = []
        was, is_ = before.front, now_sighting.front
        if target in ("front", "point") and was is not None \
                and is_ is not None \
                and (was.pid, was.number) != (is_.pid, is_.number):
            warnings.append(
                f"the front window changed between our last look and this "
                f"{action} - it was {was.describe()}, it is now "
                f"{is_.describe()}. Look again to check what it did.")
        if expect:
            front = is_ or was
            named = f"{front.app} {front.title}" if front else ""
            if expect.lower() not in named.lower():
                warnings.append(
                    f"this {action} was meant for {expect!r}, but the front "
                    f"window was {front.describe() if front else 'nothing'}.")
        if target == "focus":
            return warnings + self._same_focus(action, before, now_sighting)
        if target != "point" or point is None:
            return warnings
        x, y = point
        then, current = before.under(x, y), now_sighting.under(x, y)
        if then is None and current is None:
            return warnings
        if then is None or current is None \
                or (then.pid, then.number) != (current.pid, current.number):
            warnings.append(
                f"what was at {x:g},{y:g} was not what the screenshot showed "
                f"- it was {then.describe() if then else 'nothing'}, it is "
                f"now {current.describe() if current else 'nothing'}. Look "
                f"again to check what the {action} hit.")
            return warnings
        owner, _role = self.eyes.element_at(x, y)
        if owner and current.pid and owner != current.pid:
            warnings.append(
                f"the window at {x:g},{y:g} belongs to {current.describe()} "
                "but the thing actually under the cursor belongs to process "
                f"{owner}. Look again to check what the {action} hit.")
        return warnings

    def _same_focus(self, action: str, before: Sighting,
                    now_sighting: Sighting) -> list[str]:
        """Typing goes where the focus is, so for typing the focus IS the
        target. This is the check that "Hello" needed: a word meant for
        one window went to whichever one had the caret.

        By process, not by element. The role is recorded and reported but
        not enforced: apps re-render and re-focus their own controls
        constantly, and a caret that moved from one field to another
        inside the app we are working in is not the failure - a caret
        that moved to a DIFFERENT APP is, every time.
        """
        if not before.focused_pid and not now_sighting.focused_pid:
            return []
        if before.focused_pid != now_sighting.focused_pid:
            return [f"the keyboard focus moved to another process between "
                    f"our last look and this {action} "
                    f"({before.focused_pid or 'none'} "
                    f"{before.focused_role or ''} -> "
                    f"{now_sighting.focused_pid or 'none'} "
                    f"{now_sighting.focused_role or ''}), so it went into "
                    "whatever had the caret. Look again to check where it "
                    "landed."]
        return []

    # -- aiming ----------------------------------------------------------
    # Said, never refused. The action goes ahead either way, and what comes
    # back is a sentence for the worker to read after it: a worker told
    # "the caret was not in a field" can look and put it right, and one
    # that is stopped cannot do what it was asked at all.
    def typing_target(self, expect: str = "",
                      into: str = "") -> tuple[Element | None, str]:
        """Where typing is about to land, and a warning when that looks
        wrong: no focus, not a text field, not the app or field named.

        _same_focus asks whether the focus MOVED. This asks whether it is
        anywhere useful: on 2026-09-09 a worker typed a name into Messages'
        search box while the box did not have the caret, the characters
        went nowhere, and nothing said so.

        No target and no warning when the focus cannot be read at all: an
        app that does not answer is not an app with nothing focused.
        """
        found = self.eyes.focused_element()
        if found is None:
            return None, ""
        if not found.pid:
            return found, (
                "nothing had the keyboard focus, so these characters "
                "probably went nowhere. Look, click into the field you "
                "mean, and type again.")
        if expect and not found.belongs_to(expect):
            return found, (
                f"this was meant for {expect!r}, but the keyboard focus was "
                f"on {found.describe()}. Look at where the text went.")
        if not found.text_entry:
            return found, (
                f"the keyboard focus was on {found.describe()}, which is not "
                "a text field, so the characters may not have landed. Look, "
                "and click into the field you mean if they did not.")
        if into and into.lower() not in \
                f"{found.role} {found.subrole} {found.label}".lower():
            return found, (
                f"this was meant for the {into!r} field, but the caret was "
                f"in {found.describe()}. Look at where the text went.")
        return found, ""

    def click_target(self, x: float, y: float,
                     expect: str = "") -> tuple[Element | None, str]:
        """What a click here lands on, and a warning when it looks wrong.

        The Dock: an icon picked by position opens whatever app sits there,
        and on 2026-09-09 that was FaceTime, with its camera, instead of
        Messages. `open APP` does it by name. With an expected app, the
        element itself should belong to it - the window listing can say
        Messages while a popover or another app's panel is what is actually
        under the point.
        """
        found = self.eyes.element_under(x, y)
        if found is None:
            return None, ""
        if found.in_dock:
            return found, (
                f"that was the Dock ({found.describe()}). A Dock icon picked "
                "by position opens whatever app is there: look to check the "
                "right one opened. `open APP` opens an app by name.")
        if expect and found.pid and not found.belongs_to(expect):
            return found, (
                f"this was meant for {expect!r}, but what was under the "
                f"point was {found.describe()}. Look to check what it did.")
        return found, ""

    # -- explaining ------------------------------------------------------
    @staticmethod
    def _describe(busy: dict) -> str:
        words = {"key_down": ("key press", "key presses"),
                 "left_click": ("click", "clicks"),
                 "right_click": ("right click", "right clicks")}
        parts = [f"{count} {words[name][0 if count == 1 else 1]}"
                 for name, count in busy.items()]
        return " and ".join(parts) + " happened"

    def _blame(self) -> str:
        """Name the process posting input, if one still is.

        Only reached once a warning is already decided, so the third of a
        second it costs is spent on the message rather than on latency.
        """
        try:
            pids = {pid for pid in self.eyes.posting_processes()
                    if pid and pid != os.getpid()}
        except Exception:
            return ""
        if not pids:
            return " (nothing is posting input now, so it was a hand on the keyboard or an agent between actions)"
        named = ", ".join(f"{pid} ({_process_name(pid)})" for pid in
                          sorted(pids))
        return f" - another process is posting input right now: {named}"


def _process_name(pid: int) -> str:
    import subprocess
    try:
        done = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return os.path.basename(done.stdout.strip()) or "unknown"
