"""Your words inside the capsule while you talk: the part that decides.

The voice agent sends the transcript of the current utterance as it grows.
Each word keeps the moment it landed, so a new one fades and slides in while
the older ones dim behind it. A soft highlight settles on the newest word
and pulses with your voice, and a long utterance scrolls so the newest word
stays in view. overlay.py only draws what this works out.

Pure on purpose: the overlay runs all of this from a timer, and an exception
there once froze every later render without a word. The arithmetic lives
where a test can reach it without AppKit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

LANDING = 0.18          # seconds a new word takes to fade and slide in
SLIDE = 5.0             # points it slides in from
TRAIL = 1.5             # seconds an older word takes to dim to its floor
TRAIL_TOP, TRAIL_FLOOR = 0.98, 0.5
EDGE_FADE = 26.0        # points over which a word leaving on the left fades
LEAD = 40.0             # room kept to the right of the newest word
HIGHLIGHT_PAD = 5.0     # the highlight overhangs its word by this each side
EASE_HIGHLIGHT = 0.30   # per frame, at 60 fps
EASE_SCROLL = 0.18
EASE_PRESENCE = 0.15
EASE_REVEAL = 0.25      # how fast the words take over from the level dot


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


@dataclass
class Word:
    text: str
    landed: float       # time.monotonic() when it first showed
    x: float = 0.0      # from the start of the line, points
    width: float = 0.0


class CapsuleWords:
    """The current utterance, laid out on one line."""

    def __init__(self, measure: Callable[[str], float], space: float) -> None:
        self.measure = measure
        self.space = space
        self.words: list[Word] = []
        self.hx = 0.0           # the highlight's left edge, on the line
        self.hw = 0.0           # its width; 0 until there is a word
        self.presence = 0.0     # 1 while the voice is coming in, eased
        self.scroll = 0.0       # points the line has moved left
        self.reveal = 0.0       # 0 the level dot alone, 1 the words; eased

    def clear(self) -> None:
        self.words = []
        self.hx = self.hw = self.presence = self.scroll = self.reveal = 0.0

    def update(self, text: str, now: float) -> None:
        """Take the whole transcript so far, not just the newest fragment.

        A word already on show keeps when it landed. So does one still being
        transcribed: fragments split words ("[tong", "ue click ]"), and "pul"
        becoming "pull" is that word arriving, not a new one.
        """
        words: list[Word] = []
        x = 0.0
        for i, token in enumerate(text.split()):
            old = self.words[i] if i < len(self.words) else None
            same = old is not None and (token.startswith(old.text)
                                        or old.text.startswith(token))
            width = self.measure(token)
            words.append(Word(token, old.landed if same else now, x, width))
            x += width + self.space
        if not words:
            self.clear()
            return
        self.words = words

    @property
    def width(self) -> float:
        if not self.words:
            return 0.0
        last = self.words[-1]
        return last.x + last.width

    def capsule_width(self, pad: float, compact: float, widest: float) -> float:
        """Hug the words, from the compact capsule up to the widest one."""
        if not self.words:
            return compact
        return max(compact, min(widest, 2 * pad + self.width))

    def start(self, capsule: float, pad: float) -> float:
        """Where the line begins in a capsule this wide: after the padding,
        and centred when the words are narrower than the room they have."""
        return pad + max(0.0, (capsule - 2 * pad - self.width) / 2)

    def step(self, live: bool, room: float) -> None:
        """One frame. `live`: the voice is still coming in. `room`: how far
        the line may run before it has to scroll."""
        if self.words:
            newest = self.words[-1]
            tx = newest.x - HIGHLIGHT_PAD
            tw = newest.width + 2 * HIGHLIGHT_PAD
            if self.hw == 0.0:
                self.hx, self.hw = tx, tw
            self.hx += (tx - self.hx) * EASE_HIGHLIGHT
            self.hw += (tw - self.hw) * EASE_HIGHLIGHT
        target = 1.0 if live and self.words else 0.0
        self.presence += (target - self.presence) * EASE_PRESENCE
        # The words take over as they arrive, not as the capsule widens: a
        # one-word "yes" never widens it, and used to stay a dot.
        self.reveal += ((1.0 if self.words else 0.0) - self.reveal) * EASE_REVEAL
        overflow = max(0.0, self.width - room)
        want = clamp(self.hx + self.hw + LEAD - room, 0.0, overflow)
        self.scroll += (want - self.scroll) * EASE_SCROLL

    def alpha(self, word: Word, now: float) -> float:
        """How visible a word is: fading in as it lands, dimming as it ages,
        fading out as it scrolls off the left."""
        age = now - word.landed
        landing = clamp(age / LANDING)
        trail = TRAIL_FLOOR + (TRAIL_TOP - TRAIL_FLOOR) * clamp(1.0 - age / TRAIL)
        edge = 1.0
        if self.scroll > 0.5:
            edge = clamp((word.x - self.scroll + word.width) / EDGE_FADE)
        return trail * landing * edge

    def slide(self, word: Word, now: float) -> float:
        """Points a landing word still sits to the right of its place."""
        return (1.0 - clamp((now - word.landed) / LANDING)) * SLIDE
