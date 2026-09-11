"""NotificationPanel: the waveform notification stack, as pure state.

Many cards, ONE window, chat scroll semantics:

    OLDER
      ↑
    notification list
      ↓
    NEWEST (bottom, next to the waveform)

The panel holds every current notification as data and exposes a viewport
of up to max_visible cards measured from the bottom:

    scroll == 0            at the bottom: newest visible, "Latest" shows
    scroll == k            k newer cards hidden BELOW the viewport - the
                           "+N" indicator (viewport overflow, NOT unread)

"Latest" is true the instant the viewport sits at the bottom and false the
instant it does not; +N is visible exactly when hidden_below() > 0. Arrivals
are chat-like: at the bottom the viewport follows new cards; scrolled up it
never yanks - the new card appends below and +N increments. Identity
dedupes: the same notification id updates its card in place.

The renderer paints whatever this state says; it can never become a second
window, and dismissing a card mutates data, never exposes hidden windows.
AppKit-free so every rule is unit-testable.
"""

from __future__ import annotations

from conductor.notifications import concise


FINISHED_TYPES = ("milestone", "completed", "failed")

# The two glyphs that mean the user is the blocker: a question or an
# approval (attention), or a worker that stopped in a bad way (failed).
# "working" is the opposite of needing the user.
ATTENTION_GLYPHS = ("attention", "failed")


def needs_user(item: dict) -> bool:
    return item.get("glyph") in ATTENTION_GLYPHS


def _echoes(body: str, title: str) -> bool:
    """Whether the body would just repeat the heading.

    Compared loosely - trailing punctuation and case differ between the
    event that carried the text and the task that was named from it - and a
    truncated echo still counts.

    A prefix alone is not enough: a task called "Auth" with the body "Auth
    turned out to be a session race, now fixed" is a sentence that happens
    to open with its own name, and eating it would lose the only news on
    the card. Only a near-complete overlap is an echo.
    """
    a = " ".join((body or "").split()).strip(" .:-").lower()
    b = " ".join((title or "").split()).strip(" .:-").lower()
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return long.startswith(short) and len(short) >= 0.6 * len(long)


def notice_payload(notification, force: bool = False) -> dict:
    """One card per session, not per event.

    Keyed on the task, so a worker gets a row the moment it starts and that
    row follows it - spinner while it works, its latest line as the body, a
    tick when it finishes. Keying on the notification id instead stacked a
    fresh card for every event, which is why the panel read as a list of
    completions rather than the sessions you have open.

    Keying on the task is also what makes the body update in place on a
    follow-up turn: the second answer rewrites the row rather than adding
    one. Lives here, not in a closure inside main(), so that is testable.
    """
    finished = notification.type in FINISHED_TYPES
    title = notification.task_title or notification.project_name
    # Never print the same sentence twice. A task.created event carries the
    # task's own title as its data["title"], which is also what names the
    # card, so a starting worker rendered its name as heading AND body -
    # the second copy truncated, which read as a bug rather than an echo.
    # Falling back to the notification's own headline ("Working", "Paused")
    # says something the heading does not.
    body = notification.body or notification.title or ""
    if _echoes(body, title):
        body = notification.title or ""
    if _echoes(body, title):
        body = ""
    if not title:                    # nothing to head it with: promote
        title, body = body, ""
    return {
        "id": notification.task_id or notification.id,
        "task_id": notification.task_id,
        "title": title,
        "body": concise(body, 160),
        "status": "done" if finished else "working",
        # Escalations reopen a dismissed row; activity never does.
        "force": force,
    }


# What the renderer keeps of a card it is sent. `dismissed` and `force` are
# the two flags the projection uses to say what the user has and has not
# seen; a copy that dropped either turned the dismissal into overlay memory
# only, and every restart replayed each waved-away card straight back.
CARD_KEYS = ("id", "task_id", "title", "body", "status", "glyph", "state",
             "dismissed", "force")


def panel_card(notice: dict) -> dict:
    """The panel's copy of a card the conductor sent: the same keys every
    time, as strings, with the two flags intact. One function, so the
    renderer cannot quietly choose which parts of the projection to
    believe - measured 2026-08-30: the overlay rebuilt the card by hand
    and left `dismissed` out, so a card the user had dismissed came back
    on every restart, and a card the panel had resolved came back on the
    next tick."""
    card = {}
    for key in CARD_KEYS:
        if key in ("dismissed", "force"):
            card[key] = bool(notice.get(key))
        else:
            card[key] = str(notice.get(key, "") or "")
    if not card["status"]:
        card["status"] = "done"
    return card


class NotificationPanel:
    def __init__(self, max_visible: int = 3) -> None:
        self.max_visible = max_visible
        self.items: list[dict] = []   # chronological: oldest ... newest
        self.scroll = 0               # cards scrolled up from the bottom
        self.dismissed: set[str] = set()   # ids the user has waved away

    # -- data ------------------------------------------------------------
    def upsert(self, notice: dict, force: bool = False) -> None:
        """Append a card, or mutate the existing one with the same id in
        place (same approval observed again, status change, etc.).

        A dismissed card stays gone. Cards are keyed on the task, and a
        live worker emits telemetry continuously, so without this the x
        removed the row and the next progress tick put it straight back -
        the last card standing was always the one still running, and it
        could not be dismissed at all.

        force is the escalation path: a notification worth interrupting for
        (needs input, finished, failed) overrides an earlier dismissal,
        because dismissal means "I have seen this", not "never speak of
        this task again". Plain activity never overrides.
        """
        if notice.get("dismissed") and not force:
            # The projection remembers the dismissal (canonical state,
            # persisted), so it holds across a restart of this process
            # and across every tick that is not news.
            self.dismissed.add(notice["id"])
            self._remove(notice["id"])
            return
        if notice["id"] in self.dismissed:
            if not force:
                return
            self.dismissed.discard(notice["id"])
        for item in self.items:
            if item["id"] == notice["id"]:
                # A card that just became blocked on the user moves to the
                # newest slot: with three visible rows a long-running worker
                # sits in scrolled-away history, and a question mutated into
                # it in place is invisible. The move happens once, on the
                # transition into attention, so a waiting card re-observed
                # by the sweep stays where it is - the same rule as resolve:
                # only news moves a session.
                if needs_user(notice) and not needs_user(item):
                    self._remove(item["id"])
                    break
                item.update(notice)
                return
        self.items.append(notice)
        if self.scroll > 0:
            # Reading history: preserve the viewport; the new card waits
            # below and +N increments. Never snap the user down.
            self.scroll += 1

    def dismiss(self, notice_id: str) -> bool:
        """Presentation-only removal (the read receipt). Returns whether
        the card existed."""
        self.dismissed.add(notice_id)
        return self._remove(notice_id)

    def resolve(self, notice_id: str) -> bool:
        """The underlying state resolved (approval answered, question
        addressed). The card is the session, not the question: it stays
        where it is and the next projection rewrites it in place. Removing
        it here made the row vanish and reappear at the bottom of the
        stack on the worker's next tick - the same session jumping around
        the list. Returns whether the card exists."""
        self.dismissed.discard(notice_id)   # a later run starts clean
        return any(item["id"] == notice_id for item in self.items)

    def _remove(self, notice_id: str) -> bool:
        for index, item in enumerate(self.items):
            if item["id"] == notice_id:
                del self.items[index]
                newer_than_removed = len(self.items) - index
                if self.scroll > newer_than_removed:
                    self.scroll -= 1  # removed below the viewport
                self._clamp()
                return True
        return False

    # -- viewport ----------------------------------------------------------
    def visible(self) -> list[dict]:
        """Up to max_visible cards, in render order (older at the top,
        newer at the bottom)."""
        end = len(self.items) - self.scroll
        return self.items[max(0, end - self.max_visible):end]

    def at_bottom(self) -> bool:
        """Latest.visible == at_bottom(), exactly."""
        return self.scroll == 0

    def hidden_below(self) -> int:
        """Newer cards below the viewport - the +N indicator. Viewport
        overflow, never unread count."""
        return self.scroll

    def hidden_above(self) -> int:
        """Older cards above the viewport (scrollable history)."""
        return max(0, len(self.items) - self.scroll - self.max_visible)

    def attention_count(self) -> int:
        """Unresolved blocking cards anywhere in the panel - separate
        state, never conflated with viewport overflow, and never with
        mere activity: a running worker is the system doing its job, a
        blocked one is the user's turn."""
        return len([n for n in self.items if needs_user(n)])

    def hidden_attention_above(self) -> int:
        """Blocking cards in scrolled-away history, above the viewport."""
        end = len(self.items) - self.scroll
        return len([n for n in self.items[:max(0, end - self.max_visible)]
                    if needs_user(n)])

    def hidden_attention_below(self) -> int:
        """Blocking cards newer than the viewport, below it."""
        end = len(self.items) - self.scroll
        return len([n for n in self.items[end:] if needs_user(n)])

    def reveal_attention(self) -> bool:
        """Scroll the newest hidden blocking card into the viewport.
        Returns whether there was one to reveal."""
        shown = {id(n) for n in self.visible()}
        for index in range(len(self.items) - 1, -1, -1):
            item = self.items[index]
            if needs_user(item) and id(item) not in shown:
                # Put it at the bottom of the viewport.
                self.scroll = len(self.items) - index - 1
                self._clamp()
                return True
        return False

    # -- scrolling -----------------------------------------------------------
    def scroll_up(self, cards: int = 1) -> None:
        """Into history (older)."""
        self.scroll += cards
        self._clamp()

    def scroll_down(self, cards: int = 1) -> None:
        """Toward the newest."""
        self.scroll -= cards
        self._clamp()

    def scroll_to_bottom(self) -> None:
        self.scroll = 0

    def _clamp(self) -> None:
        top = max(0, len(self.items) - self.max_visible)
        self.scroll = max(0, min(self.scroll, top))
