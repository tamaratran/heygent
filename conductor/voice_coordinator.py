"""VoiceCoordinator: ONE voice, ONE speech stream, for every spoken
notification.

The rules this enforces by construction:

    - all user-facing speech routes through this single coordinator, whose
      one path is the live session itself; no component may instantiate
      playback directly
    - one voice identity: every utterance uses the same configured voice
    - activeUserFacingSpeechStreams <= 1: utterances serialize through one
      worker, and the path itself is the session already carrying the
      agent's own voice - so notifications cannot talk over the manager,
      which is what a separate engine let happen
    - priority: approvals/input first, then failures, then completions,
      then anything else - a blocking question outranks good news
    - user barge-in is the one legitimate interruption: when the user
      starts speaking, system speech stops immediately and stays quiet
      until they finish; queued items wait

The speech path is injected rather than owned: conduct.py wires it to the
live session's announce(), and tests pass a recorder. There is deliberately
no built-in synthesiser to fall back on - that fallback was the second voice.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import time
import heapq
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .observability import application_log

_STOP = {"the", "a", "an", "is", "was", "and", "it", "to", "of", "in", "that",
         "just", "has", "have", "all", "its", "on", "for", "with", "at"}


def _keywords(text: str) -> set:
    return {w for w in "".join(
        c.lower() if c.isalnum() else " " for c in text).split()
        if w not in _STOP and len(w) > 2}


# Lower number speaks first.
PRIORITIES = {"needs_input": 0, "failed": 1, "boss": 1, "completed": 2,
              "info": 3}


@dataclass(order=True)
class _Queued:
    priority: int
    sequence: int
    text: str = field(compare=False)
    # What this utterance is about. A later item on the same subject makes an
    # unspoken earlier one stale - "still checking the repo" must not be said
    # after "the repo check is finished".
    subject: str = field(compare=False, default="")
    supersedes: bool = field(compare=False, default=False)


class VoiceCoordinator:
    def __init__(self, speak: Callable[[str], Awaitable[None]] | None = None,
                 voice: str | None = None) -> None:
        self.voice = voice            # one configured voice for everything
        # No default engine. An unwired coordinator is silent, because the
        # alternative - falling back to a local synthesiser - is a second
        # voice on a second audio stream.
        self._speak = speak
        self._heap: list[_Queued] = []
        self._yielded: set[str] = set()   # spoken once, cut off, requeued
        # Delivery hooks, so whoever queued a line can write down what
        # became of it: said in full, or cut off by the user.
        self.on_spoken = None
        self.on_yielded = None
        self._sequence = 0
        self._wakeup = asyncio.Event()
        self._user_speaking = False
        self._current: asyncio.Task | None = None
        self._worker: asyncio.Task | None = None
        self.spoken: list[str] = []   # history, for tests and traces
        self._covered: list[str] = []  # what the conversation already said
        self._covered_subjects: set[str] = set()

    # -- the only entry point for notification speech ---------------------
    def set_speaker(self, speak) -> None:
        """Install the one speech path, once the live session exists."""
        self._speak = speak

    def enqueue(self, text: str, kind: str = "info", subject: str = "",
                supersedes: bool = True) -> None:
        """Queue one utterance.

        The queue is semantic, not FIFO. A new item about the same subject
        drops any unspoken earlier one, and anything the conversation has
        already covered is never queued at all - the user should not be told
        twice, once by the answer and once by an announcement.
        """
        text = " ".join(str(text).split())
        if not text:
            return
        if subject and subject in self._covered_subjects:
            return                     # the conversation already covered it
        if self._already_said(text):
            return
        if subject and supersedes:
            self._heap = [q for q in self._heap if q.subject != subject]
            heapq.heapify(self._heap)
        self._sequence += 1
        heapq.heappush(self._heap, _Queued(
            PRIORITIES.get(kind, 3), self._sequence, text, subject, supersedes))
        self._ensure_worker()
        self._wakeup.set()

    def note_spoken_elsewhere(self, text: str, subject: str = "") -> None:
        """Record what the conversation already covered.

        The subject is the reliable half: when the manager answers about a
        task, anything queued about that task is now a repeat. Word overlap
        is only a backstop, and a weak one - "finished" and "completed" say
        the same thing and share no words - so it is deliberately not the
        mechanism anything depends on.
        """
        if subject:
            self._covered_subjects.add(subject)
            self.drop_subject(subject)
        text = " ".join(str(text).split())
        if text:
            self._covered.append(text.lower())
            del self._covered[:-40]

    def _already_said(self, text: str) -> bool:
        key = _keywords(text)
        if not key:
            return False
        for prior in self._covered:
            other = _keywords(prior)
            if other and len(key & other) / len(key) > 0.72:
                return True
        return False

    def drop_subject(self, subject: str) -> int:
        """Forget unspoken items about something that is no longer true -
        an approval the user has already granted, say."""
        before = len(self._heap)
        self._heap = [q for q in self._heap if q.subject != subject]
        heapq.heapify(self._heap)
        return before - len(self._heap)

    def coalesce(self, kind: str = "completed",
                 joiner: str = "Also, {n} tasks just finished: {items}.") -> None:
        """Fold several queued announcements of one kind into one sentence.
        Three separate completions read as nagging; one reads as an update."""
        same = [q for q in self._heap if PRIORITIES.get(kind, 3) == q.priority]
        if len(same) < 3:
            return
        self._heap = [q for q in self._heap if q not in same]
        heapq.heapify(self._heap)
        items = ", ".join(q.subject or q.text for q in same)
        self._sequence += 1
        heapq.heappush(self._heap, _Queued(
            PRIORITIES.get(kind, 3), self._sequence,
            joiner.format(n=len(same), items=items), "", False))

    # A sentence interrupted before this much of it has played was not
    # heard, so it is said once more after the user finishes. Later than
    # this, they heard enough to decide, and the interruption is the
    # decision. 0 restores "never replay".
    REPLAY_IF_CUT_WITHIN_S = 1.5

    def covered_subjects(self) -> set:
        """Subjects the conversation has already covered."""
        return set(self._covered_subjects)

    def has_pending(self, subject: str) -> bool:
        """Whether something about this subject is queued and unspoken."""
        return any(q.subject == subject for q in self._heap)

    def interrupt_for_user(self) -> None:
        """The user started speaking: yield immediately. Queued items hold
        until user_finished()."""
        self._user_speaking = True
        if self._current is not None and not self._current.done():
            self._current.cancel()

    def user_finished(self) -> None:
        self._user_speaking = False
        self._wakeup.set()

    def is_speaking(self) -> bool:
        return self._current is not None and not self._current.done()

    def pending(self) -> int:
        return len(self._heap)

    # -- the single serializing worker -------------------------------------
    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        while self._heap:
            if self._user_speaking:
                self._wakeup.clear()
                await self._wakeup.wait()
                continue
            item = heapq.heappop(self._heap)
            self._current = asyncio.ensure_future(self._say(item.text))
            started = time.monotonic()
            try:
                await self._current        # exactly one stream at a time
                self.spoken.append(item.text)
                if self.on_spoken:
                    self.on_spoken(item.text)
            except asyncio.CancelledError:
                if self.on_yielded:
                    self.on_yielded(item.text)
                application_log("voice", "voice.speech_interrupted",
                                "spoken item yielded to the user",
                                severity="debug", text=item.text[:120])
                # Yielding is not delivering - but replaying what the user
                # deliberately cut off would nag, and that rule stands. The
                # distinction is WHEN they cut in. Within the first moment,
                # they were not reacting to the sentence: they pressed to
                # talk as it began, and it was deleted unheard. Measured:
                # "The Explain PR 29 agent finished..." started and was
                # interrupted in the same second, by the user asking why
                # notifications were not being read out. That interruption
                # threw the notification away. Say it once more after they
                # finish; cut off later, or twice, and they meant it.
                cut_within = time.monotonic() - started
                if cut_within < self.REPLAY_IF_CUT_WITHIN_S \
                        and item.text not in self._yielded:
                    self._yielded.add(item.text)
                    heapq.heappush(self._heap, item)
            except Exception:
                application_log("voice", "voice.speech_failed",
                                "could not speak a queued notification",
                                severity="error", exc_info=True,
                                text=item.text[:120])
            finally:
                self._current = None

    # -- default backend: macOS say, one configured voice -------------------
    async def _say(self, text: str) -> None:
        """Hand the text to the one configured speech path.

        There is no fallback engine on purpose. `say` here would be a second
        voice on a second audio stream, which is exactly the overlap this
        class exists to prevent: with no speak callable wired, speech is
        dropped loudly rather than spoken in the wrong voice.
        """
        if self._speak is None:
            print("voice: no speech path configured; dropping "
                  f"{text[:60]!r}", file=sys.stderr, flush=True)
            application_log("voice", "voice.no_speech_path",
                            "dropping speech: no speech path is configured",
                            severity="warning", text=text[:120])
            return
        result = self._speak(text)
        if inspect.isawaitable(result):
            await result
