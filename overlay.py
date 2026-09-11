#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyobjc-framework-Cocoa>=10,<12"]
# ///

"""A floating "you're being heard" indicator, in the spirit of Wispr Flow.

A borderless, transparent, always-on-top capsule near the bottom of the screen,
hidden until the hotkey is held. Built on AppKit rather than Tk: Tk on macOS
cannot render a genuinely transparent window, so the pill would sit in a dark
rectangle.

It is a pure renderer. State arrives as NDJSON on stdin, one object per line,
so it can be driven by voice_agent.py or by hand:

    {"state": "listening", "level": 0.42}
    {"heard": "what's in this folder"}
    {"words": "what's in this folder"}
    {"state": "hidden"}
    {"card": {"title": "Claude Code", "body": "what I found was..."}}
    {"card": null}
    {"toggle_hidden": true}
    {"hidden": false}
    {"state": "quit"}

The notifications - the reply card and the stack of task notifications -
start hidden and only appear when they are asked for; the capsule and the
caption are never hidden. `toggle_hidden` flips them, `hidden` sets them
outright. Hiding is about the screen and nothing else: state keeps arriving and is kept, so what comes
back is what was there. The status-bar bell stays put either way; it is in
the menu bar rather than over the user's work, and its menu carries Quit.

It answers on stdout when the user acts on the card, and whenever it goes
on or off screen:

    {"event": "dismiss"}
    {"event": "visibility", "hidden": true}

The capsule means one thing: your microphone is open. Anything the agent has to
say arrives as a notification card stacked just above it, which stays put until
it is clicked away.

`level` is 0.0-1.0 mic loudness. `state` is hidden, listening, thinking, heard,
speaking, or quit. Closing stdin also exits. `words` is the transcript of the
utterance so far, drawn inside the capsule while listening when
boss.CAPSULE_WORDS is on; `heard` is the same text for the caption card.
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from collections import deque

import objc
from AppKit import (NSAppearance, NSApplication,
                    NSApplicationActivationPolicyAccessory,
                    NSMenu, NSMenuItem, NSStatusBar,
                    NSVariableStatusItemLength,
                    NSMutableParagraphStyle, NSParagraphStyleAttributeName,
                    NSTrackingArea, NSTrackingMouseEnteredAndExited,
                    NSTrackingMouseMoved, NSTrackingActiveAlways,
                    NSTrackingInVisibleRect,
                    NSStringDrawingUsesLineFragmentOrigin,
                    NSStringDrawingTruncatesLastVisibleLine, NSMakeSize,
                    NSLineBreakByTruncatingTail, NSShadow,
                    NSMutableAttributedString,
                    NSAttributedString, NSBezierPath, NSColor, NSFont,
                    NSFontAttributeName, NSForegroundColorAttributeName,
                    NSMakePoint, NSMakeRect, NSScreen, NSView, NSWindow,
                    NSWindowStyleMaskBorderless, NSBackingStoreBuffered,
                    NSStatusWindowLevel, NSVisualEffectView,
                    NSVisualEffectBlendingModeBehindWindow,
                    NSVisualEffectStateActive,
                    NSVisualEffectMaterialHUDWindow, NSGraphicsContext)
from Foundation import NSObject, NSTimer

from conductor.capsule_words import CapsuleWords
from conductor.notification_panel import NotificationPanel, panel_card

try:                                   # sizing lives in boss.py, see there
    import boss
    H = boss.PILL_HEIGHT
    BOTTOM_MARGIN = boss.PILL_BOTTOM_MARGIN
    _CARD_W = boss.CARD_WIDTH
    _BARS = boss.PILL_BARS
    _BAR_W, _BAR_GAP, _PAD = (boss.PILL_BAR_WIDTH, boss.PILL_BAR_GAP,
                              boss.PILL_PAD)
    CAPSULE_WORDS = boss.CAPSULE_WORDS
except Exception:
    H, BOTTOM_MARGIN, _CARD_W = 30.0, 100.0, 340.0
    _BARS, _BAR_W, _BAR_GAP, _PAD = 10, 2.0, 2.0, 17.5
    CAPSULE_WORDS = False

# Width follows the waveform, so the capsule always hugs its contents.
_SPAN = _BARS * _BAR_W + (_BARS - 1) * _BAR_GAP
W_COMPACT = _SPAN + 2 * _PAD
W_TEXT = 452.0
FPS = 1.0 / 60.0
WAVE_SLOTS = _BARS         # scrolling waveform history, newest on the right
TEXT_SLOTS = 9             # shorter waveform once a caption is showing
MAX_CHARS = 58

CARD_W = _CARD_W
CARD_PAD = 17.0
CARD_RADIUS = 21.0
CARD_MAX_H = 360.0
CARD_LINE = 22.0
CARD_COLLAPSED_LINES = 2
# The window is larger than the card so the dismiss button can overhang the
# corner and the chevron can hang below, the way Codex's notification does.
CAPTION_PAD = 12.0
CAPTION_LINE = 19.0
CAPTION_GAP = 9.0          # capsule -> caption, and caption -> card
CAPTION_LINES = 2          # wrap, then drop words off the front
CAPTION_W = _CARD_W        # same width as the card, so the two line up
CARD_TEXT_W = _CARD_W - 2 * 17.0 - 26.0   # room for the status glyph
# One dismiss control, drawn identically wherever it appears. Two call sites
# with their own numbers drifted to 26pt and 22pt circles side by side.
CLOSE_D = 24.0            # circle diameter
CLOSE_ARM = 4.2           # half-length of each stroke of the x
CLOSE_STROKE = 1.65

CARD_INSET = 24.0          # margin for the shadow; NSShadow clips to bounds
CARD_CHEVRON = 48.0
WINDOW_W = CARD_W + 2 * CARD_INSET
WIDTH_EASE = 0.22
WAVE_EVERY = 4             # advance the waveform every Nth frame, not every frame
PAD_X = _PAD
# Thin, tightly spaced bars: at rest these read as a hairline, not a row of dots.
BAR_W, BAR_GAP = _BAR_W, _BAR_GAP
BAR_MAX, BAR_MIN = H * 0.40, 4.0
BAR_EASE = 0.30            # glide between waveform samples instead of jumping
# Your words in the capsule (boss.CAPSULE_WORDS); conductor/capsule_words.py
# decides what shows, these only how it is drawn.
WORD_DOT = (1.8, 3.0)      # the level dot before the first word: radius, + level
VOICE_ATTACK, VOICE_RELEASE = 0.45, 0.14
VOICE_LIVE = 0.15          # seconds without a level before the voice has stopped

# The windows a double tap on Fn hides, each with the flag that says whether
# it belongs on screen: the reply card, the stack of task notifications, and
# the badge and chevron that belong to the stack. The capsule and the caption
# are not among them - they say the microphone is open and what it heard,
# which a user holding Fn needs to see whether the notifications are up or not.
NOTIFICATION_WINDOWS = {"card_window": "card_visible",
                        "panel_window": "panel_visible",
                        "badge_window": "badge_visible",
                        "chevron_window": "chevron_visible"}


def rgb(r: int, g: int, b: int, a: float = 1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(
        r / 255.0, g / 255.0, b / 255.0, a)


# state -> (wave colour, text colour, fallback caption)
def palette(state: str):
    return {
        "listening": (rgb(242, 244, 247), rgb(242, 244, 247), ""),
        "thinking":  (rgb(240, 180, 41), rgb(246, 211, 139), "Thinking"),
        "heard":     (rgb(154, 163, 178), rgb(232, 235, 240), ""),
        "speaking":  (rgb(139, 124, 246), rgb(217, 210, 255), ""),
    }.get(state, (rgb(75, 81, 92), rgb(124, 132, 148), ""))


def word_attrs(alpha: float = 1.0) -> dict:
    """The capsule's words: the same face as its caption label."""
    return {
        NSFontAttributeName: NSFont.systemFontOfSize_weight_(12.5, 0.25),
        NSForegroundColorAttributeName:
            palette("listening")[0].colorWithAlphaComponent_(alpha),
    }


_word_widths: dict[str, float] = {}


def measure_word(word: str) -> float:
    width = _word_widths.get(word)
    if width is None:
        width = NSAttributedString.alloc().initWithString_attributes_(
            word, word_attrs()).size().width
        if len(_word_widths) > 2000:       # a long session says many words
            _word_widths.clear()
        _word_widths[word] = width
    return width


def draw_capsule_words(view, w: float, h: float) -> None:
    """Your words, karaoke-style. conductor/capsule_words.py decides what.

    A module function rather than a PillView method, for the reason
    rounded_rect gives: PyObjC bridges every NSView method as a selector.
    """
    line, now, cy = view.words, time.monotonic(), h / 2
    shown = line.reveal
    white = palette("listening")[0]
    if shown < 1.0:
        # Before the first word lands: a dot that breathes with your voice.
        r = WORD_DOT[0] + WORD_DOT[1] * view.voice
        white.colorWithAlphaComponent_(0.9 * (1.0 - shown)).set()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(w / 2 - r, cy - r, 2 * r, 2 * r)).fill()
    if not line.words:
        return
    NSGraphicsContext.saveGraphicsState()
    NSBezierPath.clipRect_(NSMakeRect(PAD_X - 8, 0, max(0.0, w - PAD_X - 4), h))
    left = line.start(w, PAD_X)
    glow = line.presence * shown * (0.12 + 0.18 * view.voice)
    if line.hw > 0 and glow > 0.005:
        white.colorWithAlphaComponent_(glow).set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(left + line.hx - line.scroll, cy - 9, line.hw, 18),
            9, 9).fill()
    for word in line.words:
        alpha = line.alpha(word, now) * shown
        if alpha <= 0.01:
            continue
        text = NSAttributedString.alloc().initWithString_attributes_(
            word.text, word_attrs(alpha))
        x = left + word.x - line.scroll + line.slide(word, now)
        text.drawAtPoint_(NSMakePoint(x, cy - text.size().height / 2))
    NSGraphicsContext.restoreGraphicsState()


class PillView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(PillView, self).initWithFrame_(frame)
        if self is None:
            return None
        # Start hidden: the pill must not appear until something asks for it.
        self.state = "hidden"
        self.text = ""
        self.pulse = 0.0
        self.wave = deque([0.05] * WAVE_SLOTS, maxlen=WAVE_SLOTS)
        self.render = [0.05] * WAVE_SLOTS
        self.words = CapsuleWords(measure_word, measure_word(" "))
        self.voice = 0.0            # the level, eased: what the highlight pulses with
        return self

    def isFlipped(self):
        return True

    def drawRect_(self, _rect):
        bounds = self.bounds()
        w, h = bounds.size.width, bounds.size.height
        wave_color, text_color, caption = palette(self.state)

        # Solid, not vibrancy. A blurred capsule takes its colour from
        # whatever is behind it, so it reads fuzzy and never quite the same
        # black twice.
        body = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(0, 0, w, h), h / 2, h / 2)
        rgb(11, 12, 16, 0.98).set()
        body.fill()

        cy = h / 2

        # The waveform means one thing only: your voice is being heard. It is
        # drawn while the key is held and never synthesised for other states.
        if self.state == "listening" and CAPSULE_WORDS and not self.text:
            draw_capsule_words(self, w, h)
            text_x = PAD_X
        elif self.state == "listening":
            slots = TEXT_SLOTS if self.text else WAVE_SLOTS
            # Smooth across neighbours so the bars read as one envelope.
            raw = self.render[-slots:]
            heights = [
                (raw[max(i - 1, 0)] + 2.0 * raw[i] + raw[min(i + 1, len(raw) - 1)]) / 4.0
                for i in range(len(raw))
            ]
            span = slots * BAR_W + (slots - 1) * BAR_GAP
            start = (w - span) / 2 if not self.text else PAD_X
            # One uniform colour: a brightness ramp reads as a gradient rather
            # than as a single waveform.
            wave_color.colorWithAlphaComponent_(0.95).set()
            for i, sample in enumerate(heights):
                bar_h = max(BAR_MIN, sample * BAR_MAX)
                x = start + i * (BAR_W + BAR_GAP)
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(x, cy - bar_h / 2, BAR_W, bar_h),
                    BAR_W / 2, BAR_W / 2).fill()
            text_x = start + span + 12
        elif self.state == "thinking":
            dot = 7.0
            path = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(PAD_X, cy - dot / 2, dot, dot))
            wave_color.colorWithAlphaComponent_(0.35 + 0.55 * self.pulse).set()
            path.fill()
            text_x = PAD_X + dot + 11
        else:
            text_x = PAD_X + 3

        label = self.text or caption
        if label:
            if len(label) > MAX_CHARS:
                label = label[:MAX_CHARS - 1] + "…"
            attrs = {
                NSFontAttributeName: NSFont.systemFontOfSize_weight_(12.5, 0.25),
                NSForegroundColorAttributeName: text_color,
            }
            text = NSAttributedString.alloc().initWithString_attributes_(
                label, attrs)
            size = text.size()
            x = text_x
            text.drawAtPoint_(NSMakePoint(x, cy - size.height / 2))


def report(**event) -> bool:
    """Tell the agent something. Never raise.

    Every write went straight to stdout, so once the agent was gone the first
    dismissal killed the overlay with a BrokenPipeError - leaving an orphan
    on screen that could not be dismissed at all, which is the state that
    needed pkill. An orphan should still close its own cards.
    """
    try:
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()
        return True
    except (BrokenPipeError, ValueError, OSError):
        return False


def draw_close(cx: float, cy: float) -> None:
    """The dismiss control, centred on (cx, cy). One drawing for every card,
    so the panel and the voice card cannot end up with different sizes."""
    rgb(241, 242, 244, 1.0).set()
    NSBezierPath.bezierPathWithOvalInRect_(
        NSMakeRect(cx - CLOSE_D / 2, cy - CLOSE_D / 2,
                   CLOSE_D, CLOSE_D)).fill()
    cross = NSBezierPath.bezierPath()
    cross.setLineWidth_(CLOSE_STROKE)
    cross.moveToPoint_(NSMakePoint(cx - CLOSE_ARM, cy - CLOSE_ARM))
    cross.lineToPoint_(NSMakePoint(cx + CLOSE_ARM, cy + CLOSE_ARM))
    cross.moveToPoint_(NSMakePoint(cx + CLOSE_ARM, cy - CLOSE_ARM))
    cross.lineToPoint_(NSMakePoint(cx - CLOSE_ARM, cy + CLOSE_ARM))
    rgb(93, 97, 106, 1.0).set()
    cross.stroke()


def rounded_rect(rect, radius):
    # A module function, not a method: PyObjC bridges every method on an NSView
    # subclass as a selector, and a leading-underscore name collides.
    return NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        rect, radius, radius)


_clip_cache: dict[tuple[str, int], str] = {}


def clip_title(title: str) -> str:
    """One line, ellipsised. The title is a request, so it can be long."""
    return title if len(title) <= 34 else title[:33].rstrip() + "…"


def card_body_attrs(collapsed: bool = False):
    # Always word wrap. NSLineBreakByTruncatingTail would truncate instead of
    # wrapping, collapsing the body to a single line.
    style = NSMutableParagraphStyle.alloc().init()
    return {
        NSFontAttributeName: NSFont.systemFontOfSize_weight_(14.5, 0.0),
        NSForegroundColorAttributeName: rgb(28, 30, 34, 1.0),
        NSParagraphStyleAttributeName: style,
    }


def measure_body(body: str) -> float:
    text = NSAttributedString.alloc().initWithString_attributes_(
        body, card_body_attrs())
    return text.boundingRectWithSize_options_(
        NSMakeSize(CARD_TEXT_W, 10_000),
        NSStringDrawingUsesLineFragmentOrigin).size.height


def clip_body(body: str, collapsed: bool, tail: bool = False) -> str:
    """Shorten to at most CARD_COLLAPSED_LINES.

    `tail` drops words off the front instead of the end. A streaming answer
    grows past two lines, and keeping the first two would freeze the card while
    the voice carried on - the newest words are the ones worth showing.
    """
    if not collapsed or not body:
        return body
    key = (body, CARD_COLLAPSED_LINES, tail)
    if key in _clip_cache:
        return _clip_cache[key]
    limit = CARD_LINE * CARD_COLLAPSED_LINES + 4
    result = body
    if measure_body(body) > limit:
        if tail:
            words = body.split()
            while words and measure_body("… " + " ".join(words)) > limit:
                words.pop(0)
            result = "… " + " ".join(words)
        else:
            low, high = 0, len(body)
            while low < high:
                mid = (low + high + 1) // 2
                if measure_body(body[:mid].rstrip() + "…") <= limit:
                    low = mid
                else:
                    high = mid - 1
            result = body[:low].rstrip() + "…"
    _clip_cache[key] = result
    return result


def card_body_height(body: str, collapsed: bool, tail: bool = False) -> float:
    """Height of the message alone. Empty means zero, not a blank line."""
    if not body.strip():
        return 0.0
    return min(CARD_MAX_H, measure_body(clip_body(body, collapsed, tail)))


class CardView(NSView):
    """A notification in the shape of a system alert: title, body, status."""

    def initWithFrame_(self, frame):
        self = objc.super(CardView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.title = ""
        self.body = ""
        self.status = "working"
        self.title_style = "plain"
        self.collapsed = True
        self.tail = False
        self.spin = 0.0
        self.on_dismiss = None
        self.on_toggle = None
        self.on_move = None
        self.on_open = None       # body click, when the card deep-links
        self.dragging = False
        return self

    def isFlipped(self):
        return True

    def card_height(self):
        head = 21 if self.title else 0
        return CARD_PAD * 2 + head + card_body_height(
            self.body, self.collapsed, self.tail)

    def mouseDown_(self, event):
        self.dragging = False

    def mouseDragged_(self, event):
        """Drag the card anywhere; a drag never counts as a click."""
        self.dragging = True
        window = self.window()
        origin = window.frame().origin
        window.setFrameOrigin_(NSMakePoint(origin.x + event.deltaX(),
                                           origin.y - event.deltaY()))
        if self.on_move:
            self.on_move()

    def mouseUp_(self, event):
        if self.dragging:
            self.dragging = False
            return
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        card_bottom = CARD_INSET + self.card_height()
        # The chevron below toggles; the x overhanging the top left
        # dismisses. The body deep-links when on_open is set (notification
        # cards); otherwise a stray click does nothing, so a voice answer
        # survives it.
        if point.y > card_bottom:
            if self.on_toggle:
                self.on_toggle()
        elif (point.x - CARD_INSET) ** 2 + (point.y - CARD_INSET) ** 2 < 17 ** 2:
            if self.on_dismiss:
                self.on_dismiss()
        elif self.on_open:
            self.on_open()

    def drawRect_(self, _rect):
        w = self.bounds().size.width
        card_h = self.card_height()
        card = NSMakeRect(CARD_INSET, CARD_INSET, CARD_W, card_h)

        shadow = NSShadow.alloc().init()
        shadow.setShadowBlurRadius_(16.0)
        shadow.setShadowOffset_(NSMakeSize(0, -3))
        shadow.setShadowColor_(rgb(0, 0, 0, 0.20))
        shadow.set()
        # Tinted, not white: this is the agent speaking to you, and it sits
        # directly under a stack of white session cards. Identical styling
        # made it read as one more session - a fourth card in a panel that
        # only ever shows three.
        rgb(243, 244, 247, 1.0).set()
        rounded_rect(card, CARD_RADIUS).fill()

        text_x = CARD_INSET + CARD_PAD

        # The title is what was asked for, the way Codex titles a card with its
        # session name - a fixed product name in the slot says nothing. While
        # waiting it becomes Claude Code's own spinner line instead, in its
        # monospace and terracotta rather than a restyled version of it.
        if not self.title:
            pass
        elif self.title_style == "thinking":
            mono = NSFont.monospacedSystemFontOfSize_weight_(13.0, 0.0)
            head, sep, tail = self.title.partition(" (")
            line = NSMutableAttributedString.alloc().init()
            line.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(head, {
                    NSFontAttributeName: mono,
                    NSForegroundColorAttributeName: rgb(203, 123, 93, 1.0),
                }))
            if sep:
                line.appendAttributedString_(
                    NSAttributedString.alloc().initWithString_attributes_(
                        sep + tail, {
                            NSFontAttributeName: mono,
                            NSForegroundColorAttributeName: rgb(138, 143, 154, 1.0),
                        }))
            line.drawAtPoint_(NSMakePoint(text_x, CARD_INSET + CARD_PAD))
        else:
            NSAttributedString.alloc().initWithString_attributes_(
                clip_title(self.title), {
                    NSFontAttributeName: NSFont.systemFontOfSize_weight_(15.0, 0.60),
                    NSForegroundColorAttributeName: rgb(17, 19, 23, 1.0),
                }).drawAtPoint_(NSMakePoint(text_x, CARD_INSET + CARD_PAD - 2))

        # A spinner while the agent is still speaking, and nothing once it
        # has. No green tick: that is a session's completion mark, and
        # wearing it made this reply look like a fourth session in a panel
        # that only ever shows three.
        size = 17.0
        x = CARD_INSET + CARD_W - CARD_PAD - size
        y = CARD_INSET + CARD_PAD
        if self.status != "done":
            ring = NSBezierPath.bezierPath()
            ring.setLineWidth_(2.0)
            ring.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
                NSMakePoint(x + size / 2, y + size / 2), size / 2 - 1.5,
                self.spin, self.spin + 280.0)
            rgb(178, 182, 192, 1.0).set()
            ring.stroke()

        body = NSAttributedString.alloc().initWithString_attributes_(
            clip_body(self.body, self.collapsed, self.tail), card_body_attrs())
        body.drawWithRect_options_(
            NSMakeRect(text_x, CARD_INSET + CARD_PAD + (21 if self.title else 0),
                       CARD_TEXT_W,
                       card_body_height(self.body, self.collapsed, self.tail)),
            NSStringDrawingUsesLineFragmentOrigin)

        # Dismiss button, overhanging the top left corner.
        shadow.set()
        draw_close(CARD_INSET, CARD_INSET)

        # No chevron here: it is the column's control and lives in its own
        # window, so it survives this card retiring.


def caption_attrs():
    # Same palette and metrics as the notification card: one visual system.
    style = NSMutableParagraphStyle.alloc().init()
    style.setLineSpacing_(2.0)
    return {
        NSFontAttributeName: NSFont.systemFontOfSize_weight_(13.5, 0.0),
        NSForegroundColorAttributeName: rgb(28, 30, 34, 1.0),
        NSParagraphStyleAttributeName: style,
    }


def caption_measure(text: str) -> float:
    return NSAttributedString.alloc().initWithString_attributes_(
        text, caption_attrs()).boundingRectWithSize_options_(
            NSMakeSize(CAPTION_W - 2 * CAPTION_PAD, 10_000),
            NSStringDrawingUsesLineFragmentOrigin).size.height


def caption_tail(text: str) -> str:
    """Wrap to CAPTION_LINES, dropping the oldest words off the front.

    A live caption outgrows any box. Trimming the end would hide the words just
    spoken, which are the ones worth checking, so the front goes instead.
    """
    limit = CAPTION_LINE * CAPTION_LINES + 4
    if caption_measure(text) <= limit:
        return text
    words = text.split()
    while words and caption_measure("… " + " ".join(words)) > limit:
        words.pop(0)
    return "… " + " ".join(words)


class CaptionView(NSView):
    """A live transcript of what the microphone is picking up."""

    def initWithFrame_(self, frame):
        self = objc.super(CaptionView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.text = ""
        return self

    def isFlipped(self):
        return True

    def drawRect_(self, _rect):
        if not self.text:
            return
        bounds = self.bounds()
        w, h = bounds.size.width, bounds.size.height
        shadow = NSShadow.alloc().init()
        shadow.setShadowBlurRadius_(14.0)
        shadow.setShadowOffset_(NSMakeSize(0, -3))
        shadow.setShadowColor_(rgb(0, 0, 0, 0.20))
        shadow.set()
        rgb(255, 255, 255, 1.0).set()
        rounded_rect(NSMakeRect(16, 16, w - 32, h - 32), CARD_RADIUS).fill()
        NSAttributedString.alloc().initWithString_attributes_(
            self.text, caption_attrs()).drawWithRect_options_(
                NSMakeRect(CAPTION_PAD + 16, CAPTION_PAD + 16,
                           w - 2 * CAPTION_PAD - 32, h - 2 * CAPTION_PAD - 32),
                NSStringDrawingUsesLineFragmentOrigin)


PANEL_PAD = 16.0          # inner padding of each card
PANEL_GAP = 10.0          # the one spacing token, used between every card
PANEL_ICON = 20.0         # status glyph diameter
PANEL_TITLE_H = 20.0      # one line of the 13 pt title
PANEL_HEAD_H = 52.0       # padding plus the title line
PANEL_BODY_LINE = 16.0    # one body line: every card's whole body
PANEL_PILL_H = 26.0       # Latest / +N pill height
# Panel cards share the voice card's inset and width exactly: the two
# windows are the same size and centred alike, so matching these is what
# makes the whole column read as one list rather than two stacked things.
PANEL_EDGE = CARD_INSET


class NotificationPanelView(NSView):
    """The waveform notification stack: multiple compact cards inside ONE
    window - the old UI's visual grammar. Separate rounded cards with gaps,
    status glyph top right, a per-card x overhanging the top-left corner, a
    chat semantics - older at the top, newest at the bottom next to the
    waveform. A "Latest" pill straddles the bottom card exactly when the
    viewport is at the bottom; a "+N" pill takes its place when newer cards
    sit below the viewport (clicking it jumps to the bottom). No chevron of
    its own: the stack sits tightly above the voice card, whose chevron
    serves the whole column - one arrow total, like the reference app.
    Every card shares the same x, width, padding and corner radius by
    construction: they are drawn from one frame list inside this one view.
    Scrolling moves the viewport by whole cards."""

    def initWithFrame_(self, frame):
        self = objc.super(NotificationPanelView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.cards = []             # visible slice, render order: old -> new
        self.at_bottom = True
        self.hidden_below = 0
        self.hidden_above = 0
        self.attention_above = 0    # blocked cards hidden in history
        self.attention_below = 0    # blocked cards hidden below
        self.spin = 0.0
        self.on_dismiss = None      # (notice_id)
        self.on_open = None         # (notice_id)
        self.on_scroll = None       # (+1 up/older, -1 down/newer)
        self.on_reveal = None       # bring a hidden blocked card into view
        self.hovered = None         # card id under the cursor
        self.dragging = False
        self.on_move = None         # (dx, dy) while being dragged
        self.on_jump_bottom = None
        self._scroll_accum = 0.0
        return self

    def isFlipped(self):
        return True

    # -- hover ------------------------------------------------------------
    def updateTrackingAreas(self):
        for area in list(self.trackingAreas()):
            self.removeTrackingArea_(area)
        options = (NSTrackingMouseEnteredAndExited | NSTrackingMouseMoved
                   | NSTrackingActiveAlways | NSTrackingInVisibleRect)
        self.addTrackingArea_(NSTrackingArea.alloc()
                              .initWithRect_options_owner_userInfo_(
                                  self.bounds(), options, self, None))

    def mouseMoved_(self, event):
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        hit = None
        for card, frame in self._card_frames():
            if (frame.origin.x <= point.x <= frame.origin.x + frame.size.width
                    and frame.origin.y <= point.y
                    <= frame.origin.y + frame.size.height):
                hit = card["id"]
                break
        if hit != self.hovered:
            # Say the card is a door before it is clicked: the status glyph
            # becomes a chevron under the cursor, the way Codex does it.
            self.hovered = hit
            self.setNeedsDisplay_(True)

    def mouseExited_(self, _event):
        if self.hovered is not None:
            self.hovered = None
            self.setNeedsDisplay_(True)

    # -- geometry ---------------------------------------------------------
    def card_height(self, card) -> float:
        # Every card is one constant size: the head plus one ellipsised
        # body line, whatever the body says and whatever its status. A
        # height that follows the text moves the whole stack whenever a
        # live worker rewrites its body, or when a card finishes.
        return PANEL_HEAD_H + PANEL_BODY_LINE

    def shows_latest(self) -> bool:
        """The Latest pill sits on the TOP edge, so it needs room up there."""
        return bool(self.at_bottom and (self.hidden_below or
                                        self.hidden_above))

    def shows_more(self) -> bool:
        """+N sits on the bottom edge, pointing at what is below."""
        return bool(not self.at_bottom and self.hidden_below)

    def _top_inset(self) -> float:
        # Room for the Latest pill is ALWAYS reserved, shown or not. It
        # used to be reserved only while Latest showed, and the +N room at
        # the bottom only while +N showed - so scrolling one card up moved
        # every card down by half a pill and the panel changed height, and
        # scrolling back moved them up again: the stack jittered to make
        # space for +1, +2. Card frames now depend on the cards alone.
        return PANEL_EDGE + PANEL_PILL_H / 2

    def panel_height(self) -> float:
        if not self.cards:
            return 0.0
        total = sum(self.card_height(c) for c in self.cards)
        total += PANEL_GAP * (len(self.cards) - 1)
        # Both pills straddle an edge and need half their height clear of
        # the cards: Latest on top, +N underneath. Both halves are always
        # there (see _top_inset).
        return self._top_inset() + total + PANEL_PILL_H / 2 + PANEL_EDGE / 2

    def _card_frames(self):
        """One frame list, one x, one width, one gap: alignment by
        construction. Render order matches self.cards (older -> newer)."""
        y = self._top_inset()
        frames = []
        for card in self.cards:
            h = self.card_height(card)
            frames.append((card, NSMakeRect(CARD_INSET, y, CARD_W, h)))
            y += h + PANEL_GAP
        return frames

    # -- interaction ------------------------------------------------------
    def mouseDown_(self, _event):
        self.dragging = False

    def mouseDragged_(self, event):
        """Dragging the panel drags the whole stack with it."""
        self.dragging = True
        if self.on_move:
            self.on_move(event.deltaX(), -event.deltaY())

    def mouseUp_(self, event):
        if self.dragging:
            self.dragging = False
            return                    # a drag is not a click
        p = self.convertPoint_fromView_(event.locationInWindow(), None)
        # The amber pill on the top edge: a blocked card is hidden in
        # history; clicking scrolls it into view. A pill that only ever
        # announced something unreachable would be a taunt, not a control.
        if self.cards and self.attention_above and self.at_bottom:
            top = self._card_frames()[0][1].origin.y
            if abs(p.y - top) < PANEL_PILL_H:
                if self.on_reveal:
                    self.on_reveal()
                return
        # The +N pill (straddling the bottom card): jump to the bottom.
        if self.cards and self.shows_more():
            last = self._card_frames()[-1][1]
            bottom = last.origin.y + last.size.height
            if abs(p.y - bottom) < PANEL_PILL_H:
                if self.on_jump_bottom:
                    self.on_jump_bottom()
                return
        for card, frame in self._card_frames():
            # The x overhangs the card's top-left corner.
            if (p.x - frame.origin.x) ** 2 + \
                    (p.y - frame.origin.y) ** 2 < 15 ** 2:
                if self.on_dismiss:
                    self.on_dismiss(card["id"])
                return
            if frame.origin.x <= p.x <= frame.origin.x + frame.size.width \
                    and frame.origin.y <= p.y <= frame.origin.y + \
                    frame.size.height:
                if self.on_open:
                    self.on_open(card["id"])
                return

    def scrollWheel_(self, event):
        if self.on_scroll is None:
            return
        self._scroll_accum += event.scrollingDeltaY()
        if self._scroll_accum > 24.0:
            self._scroll_accum = 0.0
            self.on_scroll(+1)        # up: into history (older)
        elif self._scroll_accum < -24.0:
            self._scroll_accum = 0.0
            self.on_scroll(-1)        # down: toward newest

    # -- drawing ----------------------------------------------------------
    def drawRect_(self, _rect):
        if not self.cards:
            return
        for card, frame in self._card_frames():
            self._draw_card(card, frame)
        # Exactly one pill, straddling the bottom card's lower edge:
        # "Latest" iff the viewport is at the bottom; "+N" iff newer cards
        # sit below it. Never both, never neither-with-overflow.
        # Two indicators, two edges. "Latest" labels the top of the stack -
        # you are at the newest, nothing below you. "+N" hangs off the bottom
        # because that is the direction the unseen cards are in.
        frames = self._card_frames()
        if self.at_bottom and self.attention_above:
            # A blocked card is hidden in history: say so where "Latest"
            # would sit, in the attention amber, and make it a door to the
            # card (see mouseUp_). "Latest" says where you are; this says
            # where you are needed, which outranks it.
            count = self.attention_above
            label = "1 needs you" if count == 1 else f"{count} need you"
            self._draw_pill(label, frames[0][1].origin.y, attention=True)
        elif self.shows_latest():
            self._draw_pill("Latest", frames[0][1].origin.y)
        elif self.shows_more():
            last = frames[-1][1]
            # Amber when one of the cards below is waiting on the user:
            # +N alone reads as "more chatter", which is exactly what a
            # buried question must not read as.
            self._draw_pill(f"+{self.hidden_below}",
                            last.origin.y + last.size.height,
                            attention=bool(self.attention_below))

    def _draw_card(self, card, frame):
        shadow = NSShadow.alloc().init()
        shadow.setShadowBlurRadius_(14.0)
        shadow.setShadowOffset_(NSMakeSize(0, -3))
        shadow.setShadowColor_(rgb(0, 0, 0, 0.18))
        NSGraphicsContext.currentContext().saveGraphicsState()
        shadow.set()
        rgb(255, 255, 255, 1.0).set()
        rounded_rect(frame, CARD_RADIUS).fill()
        NSGraphicsContext.currentContext().restoreGraphicsState()

        x = frame.origin.x + PANEL_PAD
        y = frame.origin.y + PANEL_PAD - 2
        # Title and body get the same width: the card minus its padding
        # and the glyph's column. The title used to be drawn at a point,
        # with no width at all, so a long request ran under the glyph and
        # off the card ("...punctuation and noise ta" cut by the edge)
        # while the body beneath it ellipsised properly.
        text_w = frame.size.width - 2 * PANEL_PAD - PANEL_ICON
        NSAttributedString.alloc().initWithString_attributes_(
            card.get("title", ""), panel_title_attrs()).drawWithRect_options_(
                NSMakeRect(x, y, text_w, PANEL_TITLE_H),
                NSStringDrawingUsesLineFragmentOrigin)
        body = NSAttributedString.alloc().initWithString_attributes_(
            card.get("body", ""), panel_body_attrs())
        body.drawWithRect_options_(
            NSMakeRect(x, y + 26, text_w, PANEL_BODY_LINE),
            NSStringDrawingUsesLineFragmentOrigin |
            NSStringDrawingTruncatesLastVisibleLine)

        # Status glyph, top right - or, under the cursor, a chevron saying
        # this card is a door to its session.
        gx = frame.origin.x + frame.size.width - PANEL_PAD - PANEL_ICON
        gy = frame.origin.y + PANEL_PAD
        if self.hovered == card.get("id"):
            mid = gy + PANEL_ICON / 2
            arrow = NSBezierPath.bezierPath()
            arrow.setLineWidth_(2.0)
            arrow.moveToPoint_(NSMakePoint(gx + 6.5, mid - 5.0))
            arrow.lineToPoint_(NSMakePoint(gx + 12.0, mid))
            arrow.lineToPoint_(NSMakePoint(gx + 6.5, mid + 5.0))
            rgb(120, 124, 134, 1.0).set()
            arrow.stroke()
        elif card.get("glyph") == "attention":
            # Waiting on the user: amber, filled, unmistakable at a glance.
            rgb(245, 166, 35, 1.0).set()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(gx, gy, PANEL_ICON, PANEL_ICON)).fill()
            bang = NSBezierPath.bezierPath()
            bang.setLineWidth_(2.2)
            bang.moveToPoint_(NSMakePoint(gx + PANEL_ICON / 2, gy + 5.0))
            bang.lineToPoint_(NSMakePoint(gx + PANEL_ICON / 2, gy + 11.5))
            rgb(255, 255, 255, 1.0).set()
            bang.stroke()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(gx + PANEL_ICON / 2 - 1.3, gy + 13.2, 2.6, 2.6)).fill()
        elif card.get("glyph") == "failed":
            rgb(220, 64, 64, 1.0).set()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(gx, gy, PANEL_ICON, PANEL_ICON)).fill()
            # A status mark, not a dismiss control: the x that closes a
            # card is drawn by draw_close alone.
            mark = NSBezierPath.bezierPath()
            mark.setLineWidth_(2.0)
            mark.moveToPoint_(NSMakePoint(gx + 5.5, gy + 5.5))
            mark.lineToPoint_(NSMakePoint(gx + PANEL_ICON - 5.5, gy + PANEL_ICON - 5.5))
            mark.moveToPoint_(NSMakePoint(gx + PANEL_ICON - 5.5, gy + 5.5))
            mark.lineToPoint_(NSMakePoint(gx + 5.5, gy + PANEL_ICON - 5.5))
            rgb(255, 255, 255, 1.0).set()
            mark.stroke()
        elif card.get("status") == "done":
            rgb(48, 197, 85, 1.0).set()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(gx, gy, PANEL_ICON, PANEL_ICON)).fill()
            tick = NSBezierPath.bezierPath()
            tick.setLineWidth_(2.0)
            tick.moveToPoint_(NSMakePoint(gx + 5.2, gy + 10.4))
            tick.lineToPoint_(NSMakePoint(gx + 8.6, gy + 13.8))
            tick.lineToPoint_(NSMakePoint(gx + 14.8, gy + 6.4))
            rgb(255, 255, 255, 1.0).set()
            tick.stroke()
        else:
            ring = NSBezierPath.bezierPath()
            ring.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
                NSMakePoint(gx + PANEL_ICON / 2, gy + PANEL_ICON / 2),
                PANEL_ICON / 2 - 1.6, self.spin, self.spin + 300.0)
            ring.setLineWidth_(2.0)
            rgb(176, 180, 190, 1.0).set()
            ring.stroke()

        # Dismiss x overhanging the top-left corner.
        draw_close(frame.origin.x + 2, frame.origin.y + 2)

    def _draw_pill(self, text, center_y, attention=False):
        label = NSAttributedString.alloc().initWithString_attributes_(
            text, panel_pill_attrs(attention))
        size = label.size()
        w = size.width + 22
        x = (self.frame().size.width - w) / 2
        # Attention wears the same amber as the glyph it points at, so the
        # pill and the card it reveals read as one thing.
        (rgb(245, 166, 35, 1.0) if attention
         else rgb(255, 255, 255, 1.0)).set()
        pill = rounded_rect(NSMakeRect(x, center_y - PANEL_PILL_H / 2, w,
                                       PANEL_PILL_H), PANEL_PILL_H / 2)
        pill.fill()
        rgb(0, 0, 0, 0.08).set()
        pill.setLineWidth_(1.0)
        pill.stroke()
        label.drawAtPoint_(NSMakePoint(
            x + 11, center_y - size.height / 2))

def panel_title_attrs():
    # Medium, not bold: the card is a notification, not a headline. One
    # line. A task's title never needs the ellipsis: create_task caps it
    # at TITLE_MAX_CHARS (conductor/boss_tools.py), chosen so a full-length
    # title fits this column in this font. The tail truncation is a
    # backstop for the titles nobody generates - a project name, or a body
    # promoted to the heading when a notification has no title.
    style = NSMutableParagraphStyle.alloc().init()
    style.setLineBreakMode_(NSLineBreakByTruncatingTail)
    return {NSFontAttributeName: NSFont.systemFontOfSize_weight_(13.0, 0.23),
            NSForegroundColorAttributeName: rgb(58, 62, 70, 1.0),
            NSParagraphStyleAttributeName: style}


def panel_body_attrs():
    # Word wrap plus NSStringDrawingTruncatesLastVisibleLine at the draw
    # site: the one-line drawing rect ends the body with an ellipsis.
    style = NSMutableParagraphStyle.alloc().init()
    return {NSFontAttributeName: NSFont.systemFontOfSize_weight_(13.5, 0.0),
            NSForegroundColorAttributeName: rgb(24, 27, 33, 1.0),
            NSParagraphStyleAttributeName: style}


def panel_pill_attrs(attention: bool = False):
    return {NSFontAttributeName: NSFont.systemFontOfSize_weight_(11.5, 0.4),
            NSForegroundColorAttributeName: (rgb(255, 255, 255, 1.0)
                                             if attention
                                             else rgb(90, 94, 102, 1.0))}


BADGE_D = 38.0            # same circle as the chevron it replaces
REPLY_LINGER = 2.5        # seconds a finished reply stays before retiring
PANEL_SETTLE = 2.5        # seconds the panel keeps its height after the
                          # surfaces below it shrink, before gliding down
PANEL_EASE = 0.18         # per-frame glide once it does settle


class BadgeView(NSView):
    """The whole stack, folded into one counted circle.

    Collapsing should leave something that says how much is waiting without
    occupying the screen - the same footprint as the chevron that produced
    it, so the fold reads as the stack shrinking into the control rather
    than being replaced by a different thing.
    """

    def initWithFrame_(self, frame):
        self = objc.super(BadgeView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.count = 0
        self.attention = False
        self.on_expand = None
        return self

    def isFlipped(self):
        return True

    def mouseUp_(self, _event):
        if self.on_expand:
            self.on_expand()

    def drawRect_(self, _rect):
        d = BADGE_D
        x = (self.bounds().size.width - d) / 2
        y = (self.bounds().size.height - d) / 2
        shadow = NSShadow.alloc().init()
        shadow.setShadowBlurRadius_(12.0)
        shadow.setShadowOffset_(NSMakeSize(0, -2))
        shadow.setShadowColor_(rgb(0, 0, 0, 0.22))
        shadow.set()
        # Green like the completion tick, amber when something needs you.
        (rgb(240, 168, 44, 1.0) if self.attention
         else rgb(48, 178, 92, 1.0)).set()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(x, y, d, d)).fill()
        label = NSAttributedString.alloc().initWithString_attributes_(
            str(self.count), {
                NSFontAttributeName: NSFont.systemFontOfSize_weight_(15.0, 0.5),
                NSForegroundColorAttributeName: rgb(255, 255, 255, 1.0)})
        size = label.size()
        label.drawAtPoint_(NSMakePoint(x + (d - size.width) / 2,
                                       y + (d - size.height) / 2))


CHEVRON_D = 38.0


class ChevronView(NSView):
    """The column's one control, in its own window.

    It used to be drawn by the reply card, which meant it disappeared
    whenever that card did - including as soon as a finished answer retired
    itself, leaving no way to fold the stack. It belongs to the column, so it
    outlives any single card in it.
    """

    def initWithFrame_(self, frame):
        self = objc.super(ChevronView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.collapsed = False
        self.on_toggle = None
        return self

    def isFlipped(self):
        return True

    def mouseUp_(self, _event):
        if self.on_toggle:
            self.on_toggle()

    def drawRect_(self, _rect):
        d = CHEVRON_D
        x = (self.bounds().size.width - d) / 2
        y = (self.bounds().size.height - d) / 2
        shadow = NSShadow.alloc().init()
        shadow.setShadowBlurRadius_(12.0)
        shadow.setShadowOffset_(NSMakeSize(0, -2))
        shadow.setShadowColor_(rgb(0, 0, 0, 0.20))
        shadow.set()
        rgb(255, 255, 255, 1.0).set()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(x, y, d, d)).fill()
        arrow = NSBezierPath.bezierPath()
        arrow.setLineWidth_(2.1)
        mx, my = x + d / 2, y + d / 2
        if self.collapsed:
            arrow.moveToPoint_(NSMakePoint(mx - 5.5, my + 2.8))
            arrow.lineToPoint_(NSMakePoint(mx, my - 2.8))
            arrow.lineToPoint_(NSMakePoint(mx + 5.5, my + 2.8))
        else:
            arrow.moveToPoint_(NSMakePoint(mx - 5.5, my - 2.8))
            arrow.lineToPoint_(NSMakePoint(mx, my + 2.8))
            arrow.lineToPoint_(NSMakePoint(mx + 5.5, my - 2.8))
        rgb(118, 122, 132, 1.0).set()
        arrow.stroke()


class Controller(NSObject):
    def init(self):
        self = objc.super(Controller, self).init()
        if self is None:
            return None

        self.level = 0.0
        self.level_at = 0.0         # when the last level arrived
        self.peak = 0.0
        self.samples = 0
        self.accum = 0.0
        self.frame = 0
        self.phase = 0.0
        self.visible = False
        self.width = W_COMPACT
        self.target_width = W_COMPACT
        self.lock = threading.Lock()
        self.pending: list[dict] = []
        # The notifications stay off screen until the user asks for them.
        # They float above every other app on the machine, in the middle of
        # whatever the user is actually working in, so they keep out of the
        # way and a double tap on Fn brings them up - see set_hidden. The
        # capsule is not one of them (NOTIFICATION_WINDOWS).
        self.hidden = True

        screen = NSScreen.mainScreen().frame()
        rect = NSMakeRect((screen.size.width - W_COMPACT) / 2, BOTTOM_MARGIN,
                          W_COMPACT, H)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(NSColor.clearColor())
        self.window.setLevel_(NSStatusWindowLevel)
        self.window.setIgnoresMouseEvents_(True)      # never steal a click
        self.window.setHasShadow_(True)
        self.window.setCollectionBehavior_(1 << 0 | 1 << 4)  # all spaces

        # A real blur layer is what separates this from a flat dark rectangle.
        self.blur = NSVisualEffectView.alloc().initWithFrame_(
            NSMakeRect(0, 0, W_COMPACT, H))
        self.blur.setMaterial_(NSVisualEffectMaterialHUDWindow)
        self.blur.setState_(2)      # NSVisualEffectStateInactive: no blur
        # HUD material follows the system appearance; pin it dark so the pill
        # does not turn frosted-white with white bars in light mode.
        self.blur.setAppearance_(
            NSAppearance.appearanceNamed_("NSAppearanceNameVibrantDark"))
        self.blur.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        self.blur.setState_(NSVisualEffectStateActive)
        self.blur.setWantsLayer_(True)
        layer = self.blur.layer()
        layer.setCornerRadius_(H / 2)
        layer.setMasksToBounds_(True)
        # Tint the glass dark: vibrant-dark over a light desktop is grey.
        layer.setBorderWidth_(1.0)
        layer.setBorderColor_(rgb(255, 255, 255, 0.14).CGColor())
        self.window.setContentView_(self.blur)

        self.view = PillView.alloc().initWithFrame_(
            NSMakeRect(0, 0, W_COMPACT, H))
        self.blur.addSubview_(self.view)

        # A second window for the notification card. It accepts clicks (the
        # pill never does) so it can be dismissed and expanded.
        self.card_visible = False
        self.card_dismissed = False
        # When a finished reply should take itself down. Owned here rather
        # than by whoever is driving: the card knows it is done, so it can
        # retire without the driver having to remember to say so.
        self.card_expires_at = 0.0
        self.card_window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WINDOW_W, 140), NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered, False)
        self.card_window.setOpaque_(False)
        self.card_window.setBackgroundColor_(NSColor.clearColor())
        self.card_window.setLevel_(NSStatusWindowLevel)
        self.card_window.setHasShadow_(False)     # the view draws its own
        self.card_window.setCollectionBehavior_(1 << 0 | 1 << 4)
        self.card_view = CardView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WINDOW_W, 140))
        self.card_view.on_dismiss = self.dismiss_card
        self.card_view.on_toggle = self.toggle_card
        self.card_view.on_move = self.pin_card
        # Where the user dragged the stack to, as (dx, dy) from its default
        # position. One offset for the capsule, the caption, the panel and
        # the reply card: they are one object on screen, so dragging any of
        # them has to move all of them. Pinning just the reply card left it
        # sitting somewhere else entirely while the panel stayed centred.
        self.stack_offset = (0.0, 0.0)
        self.card_pin = None      # deprecated: kept until callers are gone
        self.card_window.setContentView_(self.card_view)

        # A caption strip above the capsule showing what was heard, so a
        # misheard word is visible before it reaches the model.
        self.caption_visible = False
        self.caption_height = 0.0
        self.caption_window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, CAPTION_W, 48), NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered, False)
        self.caption_window.setOpaque_(False)
        self.caption_window.setBackgroundColor_(NSColor.clearColor())
        self.caption_window.setLevel_(NSStatusWindowLevel)
        self.caption_window.setIgnoresMouseEvents_(True)
        self.caption_window.setHasShadow_(False)   # the view draws its own
        self.caption_window.setCollectionBehavior_(1 << 0 | 1 << 4)
        self.caption_view = CaptionView.alloc().initWithFrame_(
            NSMakeRect(0, 0, CAPTION_W, 48))
        self.caption_window.setContentView_(self.caption_view)

        # The waveform notification panel: MANY compact cards, ONE window.
        # A pure state machine (NotificationPanel) holds every card as data
        # and exposes a viewport - up to three cards, a Latest pill at the
        # newest end, +N for older overflow - and this single window paints
        # it. Dismissing mutates data; there is no second window to expose.
        self.panel = NotificationPanel(max_visible=3)
        self.panel_window = NSWindow.alloc() \
            .initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, WINDOW_W, 200),
                NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
        self.panel_window.setOpaque_(False)
        self.panel_window.setBackgroundColor_(NSColor.clearColor())
        self.panel_window.setLevel_(NSStatusWindowLevel)
        self.panel_window.setAcceptsMouseMovedEvents_(True)
        self.panel_window.setHasShadow_(False)
        self.panel_window.setCollectionBehavior_(1 << 0 | 1 << 4)
        self.panel_view = NotificationPanelView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WINDOW_W, 200))
        self.panel_view.on_dismiss = self.dismiss_notice
        self.panel_view.on_open = self.open_notice
        self.panel_view.on_scroll = self.scroll_notices
        self.panel_view.on_jump_bottom = self.jump_to_latest
        self.panel_view.on_reveal = self.reveal_attention
        self.panel_view.on_move = self.move_stack

        # Collapsed state: one counted circle where the chevron was.
        self.collapsed_stack = False
        self.badge_window = NSWindow.alloc() \
            .initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, 64, 64), NSWindowStyleMaskBorderless,
                NSBackingStoreBuffered, False)
        self.badge_window.setOpaque_(False)
        self.badge_window.setBackgroundColor_(NSColor.clearColor())
        self.badge_window.setLevel_(NSStatusWindowLevel)
        self.badge_window.setHasShadow_(False)
        self.badge_window.setCollectionBehavior_(1 << 0 | 1 << 4)
        self.badge_view = BadgeView.alloc().initWithFrame_(
            NSMakeRect(0, 0, 64, 64))
        self.badge_view.on_expand = self.expand_stack
        self.badge_window.setContentView_(self.badge_view)
        self.badge_visible = False

        # The column's chevron, in its own window so it outlives any card.
        self.chevron_window = NSWindow.alloc() \
            .initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, 64, 64), NSWindowStyleMaskBorderless,
                NSBackingStoreBuffered, False)
        self.chevron_window.setOpaque_(False)
        self.chevron_window.setBackgroundColor_(NSColor.clearColor())
        self.chevron_window.setLevel_(NSStatusWindowLevel)
        self.chevron_window.setHasShadow_(False)
        self.chevron_window.setCollectionBehavior_(1 << 0 | 1 << 4)
        self.chevron_view = ChevronView.alloc().initWithFrame_(
            NSMakeRect(0, 0, 64, 64))
        self.chevron_view.on_toggle = self.toggle_stack
        self.chevron_window.setContentView_(self.chevron_view)
        self.chevron_visible = False
        self.panel_window.setContentView_(self.panel_view)
        self.panel_visible = False
        # The panel's rendered baseline. It rises at once when a caption or
        # reply card needs the room, but it never falls at once: the spinner
        # card and the caption come and go every few seconds, and dropping
        # the whole column each time made every worker card bounce ~100pt.
        # The baseline holds for PANEL_SETTLE, then glides down in tick_.
        self.panel_base = 0.0
        self.panel_settle_at = 0.0

        # The activity bell: a status-bar item whose menu is the persistent
        # notification dropdown, grouped by task. The stacked notices and
        # the bell render the same TaskNotification objects.
        self.bell_data = {"unread": 0, "sections": []}
        self.status_item = NSStatusBar.systemStatusBar() \
            .statusItemWithLength_(NSVariableStatusItemLength)
        self.status_item.button().setTitle_("🔔")
        self.status_menu = NSMenu.alloc().init()
        self.status_menu.setDelegate_(self)
        self.status_item.setMenu_(self.status_menu)

        # An overlay whose agent has died still owns the screen. Without a
        # way out of it the only remedy was pkill, so it carries its own.
        self.parent_pid = os.getppid()
        threading.Thread(target=self._read_stdin, daemon=True).start()
        threading.Thread(target=self._watch_parent, daemon=True).start()
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            FPS, self, "tick:", None, True)
        return self

    # -- the notification panel (one window, many cards) ----------------------
    def show_notice(self, notice: dict) -> None:
        notice_id = str(notice.get("id", ""))
        if not notice_id:
            return
        # The projection's copy, flags and all: `dismissed` is how a
        # dismissal survives a restart, and it has to reach the panel.
        card = panel_card(notice)
        self.panel.upsert(card, force=card["force"])
        self.render_panel()

    def remove_notice(self, notice_id: str) -> None:
        """The underlying state resolved (approval answered, question
        addressed): the card retires itself."""
        self.panel.resolve(notice_id)
        self.render_panel()

    def dismiss_notice(self, notice_id: str) -> None:
        """Per-card x: presentation-only. Data mutates; nothing is hiding
        behind it."""
        if self.panel.dismiss(notice_id):
            report(event="notice_dismissed", id=notice_id)
        self.render_panel()

    def open_notice(self, notice_id: str) -> None:
        """Card click: deep-link to the exact worker."""
        task_id = next((n["task_id"] for n in self.panel.items
                        if n["id"] == notice_id), "")
        report(event="notice_opened", id=notice_id, task_id=task_id)

    def scroll_notices(self, direction: int) -> None:
        if direction > 0:
            self.panel.scroll_up()       # into history
        else:
            self.panel.scroll_down()     # toward newest
        self.render_panel()

    def jump_to_latest(self) -> None:
        self.panel.scroll_to_bottom()
        self.render_panel()

    def reveal_attention(self) -> None:
        """The amber pill: scroll the hidden blocked card into view."""
        if self.panel.reveal_attention():
            self.render_panel()

    def render_panel(self) -> None:
        """Paint the ONE window from panel state."""
        if self.collapsed_stack:
            self.render_badge()       # folded: the count grows, nothing opens
            return
        if not self.panel.items:
            if self.panel_visible:
                self.panel_window.orderOut_(None)
                self.panel_visible = False
                self.panel_base = 0.0
                self.panel_settle_at = 0.0
            # Still reposition: the arrow now hangs under the reply card
            # instead of the panel. Returning here left it wherever the
            # panel last put it, so the gap changed as notifications came
            # and went rather than staying one constant.
            self.render_chevron()
            return
        view = self.panel_view
        view.cards = self.panel.visible()
        view.at_bottom = self.panel.at_bottom()
        view.hidden_below = self.panel.hidden_below()
        view.hidden_above = self.panel.hidden_above()
        view.attention_above = self.panel.hidden_attention_above()
        view.attention_below = self.panel.hidden_attention_below()
        target = self.stack_base()
        if not self.panel_visible or target >= self.panel_base:
            # Rising (or first show): the surface below needs the room now.
            self.panel_base = target
            self.panel_settle_at = 0.0
        elif not self.panel_settle_at:
            # Shrinking: hold this height and let tick_ settle it later.
            self.panel_settle_at = time.monotonic() + PANEL_SETTLE
        height = view.panel_height()
        self._place_panel(height)
        view.setFrame_(NSMakeRect(0, 0, WINDOW_W, height))
        view.setNeedsDisplay_(True)
        if not self.panel_visible:
            self._show_window(self.panel_window)
            self.panel_visible = True
        self.render_chevron()

    def stack_base(self) -> float:
        """The y the panel needs to clear the capsule, the caption and the
        reply card - everything stacked below it right now."""
        base = BOTTOM_MARGIN + H + CAPTION_GAP
        if self.caption_visible:
            base += self.caption_height + CAPTION_GAP
        if self.card_visible:
            # Sit PANEL_GAP above the voice card's visible top edge - not
            # above its window, whose height includes the inset margin and
            # the chevron hanging below. Stacking window heights left a gap
            # of roughly forty points of empty air.
            card_top = self.card_view.card_height() + CARD_CHEVRON
            # The reply card's x overhangs its own top edge by half the
            # button, so the gap has to clear that too or the button lands
            # on the card above it.
            base += (card_top + PANEL_GAP + CLOSE_D / 2
                     - PANEL_PILL_H / 2 - PANEL_EDGE / 2)
        return base

    def _place_panel(self, height: float) -> None:
        screen = NSScreen.mainScreen().frame()
        dx, dy = self.stack_offset
        self.panel_window.setFrame_display_(
            NSMakeRect((screen.size.width - WINDOW_W) / 2 + dx,
                       self.panel_base + dy, WINDOW_W, height), True)

    # -- notification dropdown --------------------------------------------
    _GLYPHS = {"running": "●", "starting": "●", "waiting_for_user": "◐",
               "waiting_for_approval": "◐", "waiting_for_input": "◐",
               "paused": "Ⅱ", "interrupted": "Ⅱ", "completed": "✓",
               "failed": "!", "cancelled": "✕"}
    _TYPE_GLYPHS = {"needs_input": "⚠", "failed": "!", "milestone": "✓",
                    "completed": "✓", "warning": "⚠", "progress": "•",
                    "info": "·"}

    def set_bell(self, data: dict) -> None:
        self.bell_data = data
        unread = data.get("unread", 0)
        self.status_item.button().setTitle_(
            f"🔔 {unread}" if unread else "🔔")

    def quitOverlay_(self, _sender) -> None:
        """The always-present way out, whatever state the agent is in."""
        report(event="quit_requested")
        NSApplication.sharedApplication().terminate_(None)

    def _add_quit_item(self, menu) -> None:
        menu.addItem_(NSMenuItem.separatorItem())
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit Heygent", "quitOverlay:", "q")
        item.setTarget_(self)
        menu.addItem_(item)

    def menuNeedsUpdate_(self, menu) -> None:
        """Rebuild the dropdown each time it opens (and while it is open,
        AppKit re-asks as the menu tracks), so activity streams in."""
        menu.removeAllItems()
        sections = [s for s in self.bell_data.get("sections", [])
                    if s.get("groups")]
        if not sections:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "No agent activity yet", None, "")
            item.setEnabled_(False)
            menu.addItem_(item)
            self._add_quit_item(menu)
            return
        for index, section in enumerate(sections):
            if index:
                menu.addItem_(NSMenuItem.separatorItem())
            header = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                section["name"], None, "")
            header.setEnabled_(False)
            menu.addItem_(header)
            for group in section["groups"]:
                glyph = self._GLYPHS.get(group.get("status", ""), "·")
                title = f"{glyph} {group['task_title'] or group['task_id']}" \
                        f" — {group['project_name']}"
                if group.get("unread"):
                    title += f"   ({group['unread']} new)"
                item = NSMenuItem.alloc() \
                    .initWithTitle_action_keyEquivalent_(title,
                                                         "notifClicked:", "")
                item.setTarget_(self)
                item.setRepresentedObject_({"task_id": group["task_id"]})
                menu.addItem_(item)
                if group.get("activity"):
                    line = NSMenuItem.alloc() \
                        .initWithTitle_action_keyEquivalent_(
                            f"      {group['activity'][:70]}", None, "")
                    line.setEnabled_(False)
                    menu.addItem_(line)
                for notification in group["notifications"][:3]:
                    mark = self._TYPE_GLYPHS.get(notification["type"], "·")
                    text = f"    {mark} {notification['title']}"
                    if notification.get("body"):
                        text += f" — {notification['body'][:50]}"
                    sub = NSMenuItem.alloc() \
                        .initWithTitle_action_keyEquivalent_(
                            text, "notifClicked:", "")
                    sub.setTarget_(self)
                    sub.setRepresentedObject_(
                        {"task_id": group["task_id"],
                         "notification_id": notification["id"]})
                    menu.addItem_(sub)
        self._add_quit_item(menu)


    def notifClicked_(self, sender) -> None:
        payload = dict(sender.representedObject() or {})
        payload["event"] = "notification"
        report(**payload)

    # -- caption ---------------------------------------------------------
    def show_caption(self, text: str) -> None:
        text = text.strip()
        if not text:
            self.hide_caption()
            return
        text = caption_tail(text)
        self.caption_view.text = text
        height = caption_measure(text) + 2 * CAPTION_PAD + 32
        self.caption_height = height
        screen = NSScreen.mainScreen().frame()
        dx, dy = self.stack_offset
        self.caption_window.setFrame_display_(
            NSMakeRect((screen.size.width - CAPTION_W) / 2 + dx,
                       BOTTOM_MARGIN + H + CAPTION_GAP + dy,
                       CAPTION_W, height), True)
        self.caption_view.setFrame_(NSMakeRect(0, 0, CAPTION_W, height))
        self.caption_view.setNeedsDisplay_(True)
        if not self.caption_visible:
            self._show_window(self.caption_window)
            self.caption_visible = True
        if self.card_visible:
            self.layout_card()          # push the card up above the caption
        else:
            self.render_panel()

    def hide_caption(self) -> None:
        self.caption_view.text = ""
        self.caption_window.orderOut_(None)
        self.caption_visible = False
        self.caption_height = 0.0
        if self.card_visible:
            self.layout_card()          # drop the card back down
        else:
            self.render_panel()

    # -- notification card ----------------------------------------------
    def layout_card(self) -> None:
        """Stack the card directly above the capsule, both centred at the
        bottom of the screen, with the chevron sitting between them."""
        height = CARD_INSET + self.card_view.card_height() + CARD_CHEVRON
        screen = NSScreen.mainScreen().frame()
        dx, dy = self.stack_offset
        x = (screen.size.width - WINDOW_W) / 2 + dx
        stack = BOTTOM_MARGIN + H + CAPTION_GAP
        if self.caption_visible:
            stack += self.caption_height + CAPTION_GAP
        y = stack + dy
        self.card_window.setFrame_display_(
            NSMakeRect(x, y, WINDOW_W, height), True)
        self.card_view.setFrame_(NSMakeRect(0, 0, WINDOW_W, height))
        self.card_view.setNeedsDisplay_(True)
        self.render_panel()          # the panel rides above this stack

    def show_card(self, card: dict) -> None:
        # A card marked fresh belongs to a new answer, so it overrides an
        # earlier dismissal. Without this the next streaming update would put
        # a card the user just closed straight back on screen.
        if card.get("fresh"):
            self.card_dismissed = False
        elif self.card_dismissed:
            return
        self.card_view.title = str(card.get("title", "Claude Code"))
        self.card_view.body = str(card.get("body", ""))
        self.card_view.status = str(card.get("status", "working"))
        self.card_view.title_style = str(card.get("title_style", "plain"))
        self.card_view.tail = bool(card.get("tail", False))
        if card.get("collapse", False):
            self.card_view.collapsed = True
        # Mark it visible BEFORE laying out. layout_card re-anchors the
        # notification panel above this card and reads card_visible to do
        # it; setting the flag afterwards meant the first card of a turn was
        # laid out as though it did not exist, so the panel sat where the
        # card was about to appear and the two briefly overlapped.
        # A finished answer is history once it has been read; a working one
        # is still arriving, so its clock is cleared.
        self.card_expires_at = (time.monotonic() + REPLY_LINGER
                                if self.card_view.status == "done" else 0.0)
        first_show = not self.card_visible
        self.card_visible = True
        self.layout_card()
        if first_show:
            self._show_window(self.card_window)

    def move_stack(self, dx: float, dy: float) -> None:
        """Shift every window by the same delta.

        The capsule, the caption, the panel and the reply card are one object
        as far as the user is concerned, so a drag on any of them moves all
        of them. Previously only the reply card could be dragged and only it
        moved, which is how it ended up sitting somewhere else entirely while
        the panel stayed centred.
        """
        ox, oy = self.stack_offset
        self.stack_offset = (ox + dx, oy + dy)
        self._resize()                # capsule
        if self.caption_visible:
            self.show_caption(self.caption_view.text)
        if self.card_visible:
            self.layout_card()        # reply card (re-anchors the panel)
        else:
            self.render_panel()

    def pin_card(self) -> None:
        """Kept for the reply card's drag, which reports absolute position."""
        screen = NSScreen.mainScreen().frame()
        frame = self.card_window.frame()
        default_x = (screen.size.width - WINDOW_W) / 2
        default_y = BOTTOM_MARGIN + H + CAPTION_GAP
        if self.caption_visible:
            default_y += self.caption_height + CAPTION_GAP
        self.stack_offset = (frame.origin.x - default_x,
                             frame.origin.y - default_y)
        self._resize()
        self.render_panel()

    def dismiss_card(self) -> None:
        """The user closed the reply card. Hide it first, then tell the agent
        - which also stops the speech. Reporting first meant a dead pipe
        took the process down before the card ever came off screen."""
        self.hide_card()
        report(event="dismiss")

    def hide_card(self) -> None:
        self.card_dismissed = True
        self.card_window.orderOut_(None)
        self.card_visible = False
        self.render_panel()          # the panel settles back down

    def toggle_card(self) -> None:
        """The chevron folds the whole stack away, not just this card."""
        if self.panel.items:
            self.collapse_stack()
        else:
            self.card_view.collapsed = not self.card_view.collapsed
            self.layout_card()

    def toggle_stack(self) -> None:
        """Fold the column away - or, with nothing to fold, dismiss.

        Collapsing exists to trade many cards for one counted circle. With
        no notifications behind it there is no count to show: folding the
        lone reply card into a badge reading zero left the user with a
        control that hid the card and put nothing useful in its place. When
        the column is just that card, the arrow is its dismiss.
        """
        if self.collapsed_stack:
            self.expand_stack()
        elif not self.panel.items:
            self.dismiss_card()
        else:
            self.collapse_stack()

    # Gap between the bottom-most card's lower edge and the arrow circle.
    # One token, whoever is bottom-most: the reply card's window keeps a
    # legacy 48pt strip below the card, so a fixed arrow position sat 43pt
    # under a reply card but hugged a notification card - the gap visibly
    # grew and shrank with what was on screen.
    CHEVRON_GAP = 7.0        # the one gap under the bottom-most card

    def render_chevron(self) -> None:
        """Sit CHEVRON_GAP under whatever is bottom-most: the reply card if
        one is up, the panel otherwise. Hidden only when there is nothing
        to control."""
        if self.collapsed_stack or not (self.panel.items or self.card_visible):
            if self.chevron_visible:
                self.chevron_window.orderOut_(None)
                self.chevron_visible = False
            return
        screen = NSScreen.mainScreen().frame()
        dx, dy = self.stack_offset
        bottom = BOTTOM_MARGIN + H + CAPTION_GAP + dy
        if self.caption_visible:
            bottom += self.caption_height + CAPTION_GAP
        # The lower edge of the bottom-most visible content (screen y-up).
        if self.card_visible:
            content_bottom = self.card_window.frame().origin.y + CARD_CHEVRON
        else:
            # The panel reserves half an edge below its bottom card (the +N
            # pill, when shown, also fits inside that reservation).
            content_bottom = self.panel_window.frame().origin.y \
                + PANEL_EDGE / 2
        # Circle top = window y + 51 (64pt window, 38pt circle, centred).
        # CHEVRON_GAP is the gap, always: the floor exists only to stop the
        # arrow dropping into the capsule when there is no content above it,
        # so it must sit below every real position or it silently becomes a
        # second, larger spacing.
        y = max(bottom - 64,
                content_bottom - self.CHEVRON_GAP - 51)
        self.chevron_view.collapsed = self.collapsed_stack
        self.chevron_window.setFrame_display_(
            NSMakeRect((screen.size.width - 64) / 2 + dx, y, 64, 64), True)
        self.chevron_view.setFrame_(NSMakeRect(0, 0, 64, 64))
        self.chevron_view.setNeedsDisplay_(True)
        if not self.chevron_visible:
            self._show_window(self.chevron_window)
            self.chevron_visible = True
    def collapse_stack(self) -> None:
        self.collapsed_stack = True
        self.panel_window.orderOut_(None)
        self.panel_visible = False
        self.card_window.orderOut_(None)
        self.card_visible = False
        self.render_badge()

    def expand_stack(self) -> None:
        self.collapsed_stack = False
        self.badge_window.orderOut_(None)
        self.badge_visible = False
        self.render_panel()

    def render_badge(self) -> None:
        items = self.panel.items
        if not self.collapsed_stack or not items:
            if self.badge_visible:
                self.badge_window.orderOut_(None)
                self.badge_visible = False
            return
        self.badge_view.count = len(items)
        # Amber means the user is the blocker, nothing softer: a badge
        # that went amber for any running worker cried wolf, and the one
        # time a worker was genuinely stuck it looked like every other.
        self.badge_view.attention = self.panel.attention_count() > 0
        screen = NSScreen.mainScreen().frame()
        dx, dy = self.stack_offset
        # Exactly where the chevron sat, so the fold looks like a collapse
        # into that control rather than a jump.
        self.badge_window.setFrame_display_(
            NSMakeRect((screen.size.width - 64) / 2 + dx,
                       BOTTOM_MARGIN + H + CAPTION_GAP + dy - 8, 64, 64), True)
        self.badge_view.setFrame_(NSMakeRect(0, 0, 64, 64))
        self.badge_view.setNeedsDisplay_(True)
        if not self.badge_visible:
            self._show_window(self.badge_window)
            self.badge_visible = True
        self.render_chevron()

    # -- notifications on screen, or out of the way ---------------------
    def _show_window(self, window) -> None:
        """Put one window on screen - unless it is a notification and the
        notifications are hidden.

        Every orderFront in this file goes through here, so hiding cannot
        be undone by whatever renders next. Hiding is only about what
        reaches the screen: the flags that say what *should* be up
        (card_visible, panel_visible, ...) go on being maintained while
        hidden, because the layout arithmetic reads them, and because
        coming back has to restore what was there rather than a blank
        screen and a lost notification. The capsule and the caption are
        never held back: see NOTIFICATION_WINDOWS.
        """
        if self.hidden and any(window is getattr(self, name)
                               for name in NOTIFICATION_WINDOWS):
            return
        window.orderFrontRegardless()

    def set_hidden(self, hidden: bool) -> None:
        """Take the notifications off screen, or put them back as they were.

        The capsule and the caption stay where they are: they say the
        microphone is open and what it heard, and a user talking needs to
        see that either way. The status-bar bell is deliberately left alone
        too. It sits in the menu bar rather than over the user's work, it is
        where the notification history stays readable while the cards are
        away, and its menu carries the only Quit.
        """
        hidden = bool(hidden)
        if hidden == self.hidden:
            return
        self.hidden = hidden
        for name in NOTIFICATION_WINDOWS:
            window = getattr(self, name)
            if hidden:
                window.orderOut_(None)
            elif getattr(self, NOTIFICATION_WINDOWS[name]):
                window.orderFrontRegardless()
        report(event="visibility", hidden=self.hidden)

    # -- input ----------------------------------------------------------
    def _watch_parent(self) -> None:
        """Leave when the process that launched us does.

        stdin EOF usually covers this, but an agent killed hard can leave the
        pipe open in a way the reader never notices - which is exactly how
        orphans ended up outliving their session.
        """
        while True:
            time.sleep(1.0)
            if os.getppid() != self.parent_pid or self.parent_pid == 1:
                with self.lock:
                    self.pending.append({"state": "quit"})
                return

    def _read_stdin(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if line:
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                with self.lock:
                    self.pending.append(msg)
        with self.lock:
            self.pending.append({"state": "quit"})

    def _drain(self) -> None:
        with self.lock:
            messages, self.pending = self.pending, []
        for msg in messages:
            if msg.get("state") == "quit":
                NSApplication.sharedApplication().terminate_(None)
                return
            if "hidden" in msg:
                self.set_hidden(msg["hidden"])
            if msg.get("toggle_hidden"):
                self.set_hidden(not self.hidden)
            if "bell" in msg:
                self.set_bell(msg["bell"] or {})
            if "notice" in msg and msg["notice"]:
                self.show_notice(msg["notice"])
            if "notice_remove" in msg:
                self.remove_notice(str(msg["notice_remove"]))
            if "heard" in msg:
                self.show_caption(str(msg["heard"] or ""))
            if "card" in msg:
                if msg["card"]:
                    self.show_card(msg["card"])
                else:
                    self.hide_card()
            if "state" in msg and msg["state"] != self.view.state:
                self.view.state = msg["state"]
                if self.view.state in ("listening", "hidden"):
                    self.view.text = ""
                    self.view.wave.extend([0.05] * WAVE_SLOTS)
                    self.view.words.clear()
            if "text" in msg:
                self.view.text = str(msg["text"])
            if "words" in msg:
                self.view.words.update(str(msg["words"] or ""), time.monotonic())
            if "level" in msg:
                try:
                    self.level = max(0.0, min(1.0, float(msg["level"])))
                    self.level_at = time.monotonic()
                    # Average across the interval rather than peak-holding it:
                    # peaks make neighbouring bars jump and the envelope ragged.
                    self.accum += self.level
                    self.samples += 1
                    self.peak = max(self.peak, self.level)
                except (TypeError, ValueError):
                    pass
            if self.view.text:
                self.target_width = W_TEXT
            elif CAPSULE_WORDS:
                self.target_width = self.view.words.capsule_width(
                    PAD_X, W_COMPACT, W_TEXT)
            else:
                self.target_width = W_COMPACT

    def _advance_wave(self) -> None:
        """Push one sample; the waveform scrolls right to left.

        At 60 fps a 17-slot history would cover under 300 ms - narrower than a
        syllable - so the whole wave sits inside a single trough. Advancing
        every WAVE_EVERY frames widens it to roughly a second.
        """
        self.phase += 0.16
        self.view.pulse = 0.5 + 0.5 * math.sin(self.phase * 1.6)
        self._advance_words()

        # Ease every bar toward its sample each frame, so the waveform glides
        # rather than stepping once every WAVE_EVERY frames.
        target = list(self.view.wave)
        render = self.view.render
        for i, value in enumerate(target):
            render[i] += (value - render[i]) * BAR_EASE

        self.frame += 1
        if self.frame % WAVE_EVERY:
            return
        if self.view.state == "listening":
            mean = self.accum / self.samples if self.samples else 0.0
            # A little peak mixed into the mean keeps transients visible
            # without letting one spike define the bar.
            self.view.wave.append(max(0.05, 0.72 * mean + 0.28 * self.peak))
            self.accum, self.samples, self.peak = 0.0, 0, 0.0

    def _advance_words(self) -> None:
        """Ease the level the highlight pulses with, and move the line on.

        Levels arrive every 20 ms while the key is held and stop at release,
        so their going quiet is what tells the highlight the voice stopped.
        """
        live = time.monotonic() - self.level_at < VOICE_LIVE
        target = self.level if live else 0.0
        ease = VOICE_ATTACK if target > self.view.voice else VOICE_RELEASE
        self.view.voice += (target - self.view.voice) * ease
        self.view.words.step(live, W_TEXT - 2 * PAD_X)

    def tick_(self, _timer) -> None:
        self._drain()
        if (self.card_expires_at and self.card_visible
                and time.monotonic() > self.card_expires_at):
            # Make room for the session rows, which sit above it.
            self.card_expires_at = 0.0
            self.card_window.orderOut_(None)
            self.card_visible = False
            self.render_panel()
        # Spinners turn for a screen that is showing them. Hidden, the
        # windows are still tracked and still laid out - they are just not
        # worth sixty repaints a second nobody sees.
        if self.card_visible and not self.hidden \
                and self.card_view.status != "done":
            self.card_view.spin = (self.card_view.spin - 6.0) % 360.0
            self.card_view.setNeedsDisplay_(True)
        if self.panel_visible and not self.hidden and any(
                c.get("status") != "done" for c in self.panel_view.cards):
            self.panel_view.spin = (self.panel_view.spin - 6.0) % 360.0
            self.panel_view.setNeedsDisplay_(True)
        if self.panel_visible:
            target = self.stack_base()
            if self.panel_base <= target:
                self.panel_settle_at = 0.0
            elif (self.panel_settle_at
                    and time.monotonic() > self.panel_settle_at):
                # The hold expired with nothing new below: glide down.
                self.panel_base += (target - self.panel_base) * PANEL_EASE
                if self.panel_base - target < 0.5:
                    self.panel_base = target
                    self.panel_settle_at = 0.0
                self._place_panel(self.panel_view.panel_height())
                self.render_chevron()

        want = self.view.state != "hidden"
        if want and not self.visible:
            self.width = self.target_width
            self._resize()
            self._show_window(self.window)
            self.visible = True
        elif not want and self.visible:
            self.window.orderOut_(None)
            self.visible = False
        if not self.visible:
            return

        if abs(self.width - self.target_width) > 0.5:
            self.width += (self.target_width - self.width) * WIDTH_EASE
            self._resize()

        self._advance_wave()
        self.view.setNeedsDisplay_(True)

    def _resize(self) -> None:
        screen = NSScreen.mainScreen().frame()
        dx, dy = self.stack_offset
        self.window.setFrame_display_(
            NSMakeRect((screen.size.width - self.width) / 2 + dx,
                       BOTTOM_MARGIN + dy, self.width, H), True)
        self.blur.setFrame_(NSMakeRect(0, 0, self.width, H))
        self.view.setFrame_(NSMakeRect(0, 0, self.width, H))


def main() -> None:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    controller = Controller.alloc().init()
    objc.setVerbose(False)
    app.run()


if __name__ == "__main__":
    main()
