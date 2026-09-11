"""One region above the waveform, one thing in it at a time.

Conversation, notifications, status and idle were each deciding their own
visibility, so they stacked on top of one another and competed for the same
strip of screen. They are modes of one region now, and this class is the only
thing that picks which one owns it.

The rules worth stating, because they are what the individual surfaces kept
getting wrong on their own:

    - a user-initiated turn outranks everything; the system wanting to show
      something never outranks the user talking
    - notifications are persistent state, conversation is temporary
      foreground: a turn hides the panel, it never discards it
    - passive status is the weakest mode and may not push notifications off
    - restoring means recomputing, never replaying: stale status text and
      resolved notifications are gone, not hidden
    - manual interaction with the panel locks it in the foreground until the
      user starts a turn, which is a stronger explicit action

Pure state: no AppKit, no windows, so every transition in the spec is a unit
test rather than a screenshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CONVERSATION = "conversation"
NOTIFICATIONS = "notifications"
STATUS = "status"
IDLE = "idle"

# How long conversation keeps the foreground after speech ends, so rapid
# turns do not flash the panel between them.
GRACE_SECONDS = 0.8


@dataclass
class ViewSnapshot:
    """Where the user had the panel, so it can be put back."""
    expanded: bool = True
    scroll_offset: int = 0
    at_latest: bool = True


class PresentationCoordinator:
    def __init__(self, grace: float = GRACE_SECONDS) -> None:
        self.grace = grace
        self.user_speaking = False
        self.assistant_speaking = False
        self.turn_ended_at: float | None = None
        self.user_collapsed = False      # they closed it; respect that
        self.user_opened = False         # they opened it; it stays put
        self.notification_count = 0
        self.attention = False           # an approval or input is waiting
        self.status_text = ""
        self.status_id = ""
        self.snapshot = ViewSnapshot()
        self._now = 0.0

    # -- clock ------------------------------------------------------------
    def tick(self, now: float) -> None:
        self._now = now

    # -- conversation -----------------------------------------------------
    def user_turn_started(self, now: float) -> None:
        """The strongest signal there is: the user chose to talk."""
        self._now = now
        self.user_speaking = True
        self.turn_ended_at = None
        # A turn overrides a manual panel override, in both directions.
        self.user_opened = False

    def user_turn_finished(self, now: float) -> None:
        self._now = now
        self.user_speaking = False
        if not self.assistant_speaking:
            self.turn_ended_at = now

    def assistant_started(self, now: float) -> None:
        self._now = now
        self.assistant_speaking = True
        self.turn_ended_at = None

    def assistant_finished(self, now: float) -> None:
        self._now = now
        self.assistant_speaking = False
        if not self.user_speaking:
            self.turn_ended_at = now

    def in_conversation(self) -> bool:
        if self.user_speaking or self.assistant_speaking:
            return True
        if self.turn_ended_at is None:
            return False
        return (self._now - self.turn_ended_at) < self.grace

    # -- notifications ----------------------------------------------------
    def notifications_changed(self, count: int, attention: bool = False) -> None:
        """Arrivals never seize the foreground; they update the count."""
        self.notification_count = count
        self.attention = attention
        if count == 0:
            self.user_collapsed = False
            self.user_opened = False

    def user_opened_panel(self) -> None:
        """Manual open beats proactive status and beats assistant speech:
        the user asked to look at this."""
        self.user_opened = True
        self.user_collapsed = False

    def user_collapsed_panel(self) -> None:
        self.user_collapsed = True
        self.user_opened = False

    # -- status -----------------------------------------------------------
    def set_status(self, text: str, state_id: str = "") -> None:
        self.status_text = " ".join(str(text).split())
        self.status_id = state_id or self.status_text

    def clear_status(self) -> None:
        """Status is recomputed, never restored: if it is no longer true it
        does not come back when something else stops covering it."""
        self.status_text = ""
        self.status_id = ""

    # -- the decision -----------------------------------------------------
    def foreground(self) -> str:
        if self.in_conversation():
            # Manual panel opening survives assistant speech (section 15),
            # but never the user's own turn.
            if self.user_opened and not self.user_speaking:
                return NOTIFICATIONS
            return CONVERSATION
        if self.notification_count and self.user_opened:
            return NOTIFICATIONS
        if self.attention and self.notification_count:
            return NOTIFICATIONS          # approval outranks a collapse
        if self.notification_count and not self.user_collapsed:
            return NOTIFICATIONS
        if self.status_text:
            return STATUS
        return IDLE

    def compact_count(self) -> int:
        """The badge shown when notifications are not the foreground."""
        return (self.notification_count
                if self.foreground() != NOTIFICATIONS else 0)

    def expanded(self) -> dict:
        """Exactly one expanded surface, by construction."""
        mode = self.foreground()
        return {CONVERSATION: mode == CONVERSATION,
                NOTIFICATIONS: mode == NOTIFICATIONS,
                STATUS: mode == STATUS}

    # -- snapshot ---------------------------------------------------------
    def save_view(self, expanded: bool, scroll_offset: int,
                  at_latest: bool) -> None:
        self.snapshot = ViewSnapshot(expanded, scroll_offset, at_latest)

    def restore_view(self) -> ViewSnapshot:
        return self.snapshot
