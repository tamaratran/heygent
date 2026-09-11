"""SupervisorInbox: the semantic branch, delivered reliably.

    meaningful worker event -> SupervisoryEvent -> inbox -> Boss -> voice?

The Boss does not get the firehose. It gets a short list of things worth
its attention - an approval only a person can give, a question, a finish,
a failure, a worker that stopped unexpectedly - and it gets each one once,
durably, with a record of what was done about it.

Two consumers read the inbox, and the inbox does not know which is which:

  - the Manager, at the top of its next turn, sees everything pending
    ("SINCE YOUR LAST TURN") and acknowledges it when the turn completes;
  - the voice, through SpeechPolicy, decides per event whether to say
    something now, fold it into one line with others, or stay quiet
    because the conversation already covered it - and writes that
    decision down.

Nothing here touches the card. A Boss that is busy, failing, or absent
leaves the inbox pending and the card correct.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .observability import new_id
from .plain_text import plain_text
from .storage import append_jsonl, now_iso, read_jsonl

SUPERVISORY_TYPES = ("approval_required", "input_required", "completed",
                     "failed", "unexpected_interruption", "recovery_failed")
REQUIRES_ACTION = ("approval_required", "input_required")

VOICE_DECISIONS = ("spoken", "queued", "coalesced", "superseded",
                   "suppressed_policy", "suppressed_duplicate",
                   "suppressed_context", "suppressed_stale",
                   "interrupted_by_user", "pending")

# How long a finish stays worth announcing. Older than this, the Boss
# reads it from the inbox but the voice does not bring it up unprompted.
STALE_AFTER_S = 15 * 60
# Finishes closer together than this are one announcement.
COALESCE_WINDOW_S = 4.0


@dataclass
class SupervisoryEvent:
    event_id: str
    task_id: str
    subagent_id: str
    type: str
    summary: str
    requires_action: bool
    created_at: str = field(default_factory=now_iso)
    trace_id: str = ""
    source_event_id: str = ""
    task_title: str = ""
    project_name: str = ""

    def __post_init__(self) -> None:
        if self.type not in SUPERVISORY_TYPES:
            raise ValueError(f"unknown supervisory type: {self.type!r}")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DeliveryRecord:
    """What happened to one supervisory event, for the debugging question
    "did the user ever hear about this, and if not, why not?"."""
    event_id: str
    manager_delivered: bool = False
    voice_decision: str = "pending"
    notification_recorded: bool = False
    timestamp: str = field(default_factory=now_iso)
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def semantic_key(type_: str, task_id: str, summary: str) -> str:
    """The same fact arriving through two paths is one fact."""
    digest = hashlib.sha1(" ".join(summary.lower().split()).encode()).hexdigest()
    return f"{type_}:{task_id}:{digest[:12]}"


class SupervisorInbox:
    """Durable, deduplicated, acknowledged.

    Backed by one JSONL file: events and records appended as they happen,
    replayed on open. A restart therefore comes back knowing which events
    the Boss has already seen and which were spoken, so it neither
    re-announces last night's finishes nor forgets an approval nobody
    answered.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._events: dict[str, SupervisoryEvent] = {}
        self._records: dict[str, DeliveryRecord] = {}
        self._keys: dict[str, str] = {}          # semantic key -> event id
        self._load()

    def _load(self) -> None:
        for entry in read_jsonl(self.path):
            kind = entry.pop("_kind", "")
            try:
                if kind == "event":
                    event = SupervisoryEvent(**entry)
                    self._events[event.event_id] = event
                    self._keys[semantic_key(event.type, event.task_id,
                                            event.summary)] = event.event_id
                elif kind == "record":
                    record = DeliveryRecord(**entry)
                    self._records[record.event_id] = record
            except (TypeError, ValueError):
                continue

    def _append(self, kind: str, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl(self.path, {"_kind": kind, **data})

    # -- intake -------------------------------------------------------------
    def offer(self, event: SupervisoryEvent) -> bool:
        """Accept an event unless it is one already held. Returns whether
        it was new. A replayed event id, or the same fact under a new id,
        is not a second event."""
        with self._lock:
            if event.event_id in self._events:
                return False
            key = semantic_key(event.type, event.task_id, event.summary)
            if key in self._keys:
                return False
            self._events[event.event_id] = event
            self._keys[key] = event.event_id
            self._records[event.event_id] = DeliveryRecord(event.event_id)
            self._append("event", event.to_dict())
            self._append("record", self._records[event.event_id].to_dict())
            return True

    # -- the manager's side --------------------------------------------------
    def pending_for_manager(self) -> list[SupervisoryEvent]:
        with self._lock:
            return [e for e in self._events.values()
                    if not self._records[e.event_id].manager_delivered]

    def ack_manager(self, event_ids: list[str]) -> None:
        with self._lock:
            for event_id in event_ids:
                record = self._records.get(event_id)
                if record and not record.manager_delivered:
                    record.manager_delivered = True
                    record.timestamp = now_iso()
                    self._append("record", record.to_dict())

    def digest_for_manager(self, limit: int = 8) -> str:
        """What the Manager reads at the top of a turn. Decisions first."""
        pending = self.pending_for_manager()
        if not pending:
            return ""
        pending.sort(key=lambda e: (not e.requires_action, e.created_at))
        lines = ["SINCE YOUR LAST TURN:"]
        for event in pending[:limit]:
            who = event.task_title or event.task_id
            lines.append(f"  {event.task_id} ({who}) {event.type}: "
                         f"{plain_text(event.summary)[:160]}")
        if len(pending) > limit:
            lines.append(f"  ...and {len(pending) - limit} more")
        return "\n".join(lines)

    # -- the voice's side ------------------------------------------------------
    def pending_for_voice(self) -> list[SupervisoryEvent]:
        with self._lock:
            return [e for e in self._events.values()
                    if self._records[e.event_id].voice_decision == "pending"]

    def record_voice(self, event_id: str, decision: str,
                     detail: str = "") -> None:
        if decision not in VOICE_DECISIONS:
            raise ValueError(f"unknown voice decision: {decision!r}")
        with self._lock:
            record = self._records.get(event_id)
            if record is None:
                return
            record.voice_decision = decision
            record.detail = detail[:200]
            record.timestamp = now_iso()
            self._append("record", record.to_dict())

    def record_notification_for(self, task_id: str) -> None:
        """A notification record exists for this task's latest event."""
        with self._lock:
            latest = [e for e in self._events.values() if e.task_id == task_id]
            if not latest:
                return
            latest.sort(key=lambda e: e.created_at)
            record = self._records.get(latest[-1].event_id)
            if record and not record.notification_recorded:
                record.notification_recorded = True
                self._append("record", record.to_dict())

    def withdraw(self, task_id: str, types: tuple, reason: str) -> int:
        """The thing is no longer true - an approval the policy or the user
        answered before the voice got to it. Nothing pending about it
        should be said or shown to the Boss as open."""
        with self._lock:
            count = 0
            for event in self._events.values():
                if event.task_id != task_id or event.type not in types:
                    continue
                record = self._records[event.event_id]
                changed = False
                if record.voice_decision in ("pending", "queued"):
                    record.voice_decision = "suppressed_stale"
                    record.detail = reason[:200]
                    changed = True
                if not record.manager_delivered:
                    record.manager_delivered = True
                    changed = True
                if changed:
                    self._append("record", record.to_dict())
                    count += 1
            return count

    def record_notification(self, event_id: str) -> None:
        with self._lock:
            record = self._records.get(event_id)
            if record and not record.notification_recorded:
                record.notification_recorded = True
                self._append("record", record.to_dict())

    # -- reading back ----------------------------------------------------------
    def record(self, event_id: str) -> DeliveryRecord | None:
        return self._records.get(event_id)

    def events(self) -> list[SupervisoryEvent]:
        return list(self._events.values())


@dataclass(frozen=True)
class Utterance:
    """One thing for the voice to say, and which events it covers."""
    text: str
    kind: str                 # VoiceCoordinator priority kind
    subject: str
    event_ids: tuple


class SpeechPolicy:
    """Decides what the voice says about pending supervisory events.

    Deterministic, and every decision is written to the delivery record.
    The phrasing is still the Live model's - the text here is what it is
    handed - but WHETHER something is said, and whether three finishes
    become one sentence, is decided here and can be read back later.
    """

    def __init__(self, mode: str = "important",
                 spoken_types: dict | None = None) -> None:
        self.mode = mode
        self.spoken_types = spoken_types or {
            "silent": (),
            # What blocks the user or broke: said at once, mechanically.
            # A finish is the Boss's to judge and report (tell_user).
            "attention": ("approval_required", "input_required", "failed",
                          "unexpected_interruption", "recovery_failed"),
            "important": ("approval_required", "input_required",
                          "completed", "failed", "unexpected_interruption",
                          "recovery_failed"),
            "conversational": SUPERVISORY_TYPES,
        }

    def decide(self, inbox: SupervisorInbox, covered_subjects=(),
               now: float | None = None) -> list[Utterance]:
        allowed = self.spoken_types.get(self.mode,
                                        self.spoken_types["important"])
        pending = inbox.pending_for_voice()
        if not pending:
            return []
        now = now if now is not None else time.time()
        out: list[Utterance] = []
        finishes: list[SupervisoryEvent] = []
        for event in sorted(pending, key=lambda e: e.created_at):
            if event.type not in allowed:
                inbox.record_voice(event.event_id, "suppressed_policy",
                                   f"mode={self.mode}")
                continue
            if event.task_id in covered_subjects:
                inbox.record_voice(event.event_id, "suppressed_context",
                                   "the conversation already covered it")
                continue
            if _age_s(event, now) > STALE_AFTER_S and event.type == "completed":
                inbox.record_voice(event.event_id, "suppressed_stale",
                                   f"older than {STALE_AFTER_S}s")
                continue
            if event.type == "completed":
                finishes.append(event)
                continue
            out.append(Utterance(_line(event), _kind(event), event.task_id,
                                 (event.event_id,)))
            inbox.record_voice(event.event_id, "queued")
        # One task, one finish. A worker whose turn ends in two text
        # blocks reports completed twice, twenty milliseconds apart, and
        # the voice said "Two tasks just finished: X and X". The later
        # finish is the one with the answer in it; the earlier is folded
        # into it, and written down as such.
        latest: dict[str, SupervisoryEvent] = {}
        for event in finishes:
            earlier = latest.get(event.task_id)
            if earlier is not None:
                inbox.record_voice(earlier.event_id, "superseded",
                                   "a later finish of the same task")
            latest[event.task_id] = event
        finishes = list(latest.values())
        if len(finishes) == 1:
            event = finishes[0]
            out.append(Utterance(_line(event), "completed", event.task_id,
                                 (event.event_id,)))
            inbox.record_voice(event.event_id, "queued")
        elif len(finishes) > 1:
            # Three workers finishing together are one piece of news.
            names = [e.task_title or e.task_id for e in finishes]
            text = (f"{_count(len(names))} tasks just finished: "
                    f"{_join(names)}.")
            out.append(Utterance(text, "completed",
                                 "completed:" + ",".join(e.task_id for e in finishes),
                                 tuple(e.event_id for e in finishes)))
            for event in finishes:
                inbox.record_voice(event.event_id, "coalesced",
                                   f"with {len(finishes) - 1} other(s)")
        return out


def _age_s(event: SupervisoryEvent, now: float) -> float:
    from datetime import datetime, timezone
    try:
        then = datetime.fromisoformat(event.created_at.replace("Z", "+00:00"))
        return now - then.astimezone(timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _kind(event: SupervisoryEvent) -> str:
    if event.type in REQUIRES_ACTION:
        return "needs_input"
    if event.type in ("failed", "unexpected_interruption", "recovery_failed"):
        return "failed"
    return "completed"


def _line(event: SupervisoryEvent) -> str:
    who = f"The {event.project_name + ' ' if event.project_name else ''}" \
          f"{event.task_title or event.task_id} agent"
    summary = " ".join(plain_text(event.summary).split())
    if event.type == "approval_required":
        return f"{who} wants to {summary}. Want me to allow it?" \
            if summary else f"{who} needs your approval."
    if event.type == "input_required":
        return f"{who} needs you: {summary}" if summary else f"{who} has a question."
    if event.type == "completed":
        first = summary.split(". ")[0]
        return f"{who} finished. {first}" if first else f"{who} finished."
    if event.type == "failed":
        return f"{who} failed: {summary}" if summary else f"{who} failed."
    if event.type == "unexpected_interruption":
        return f"{who} stopped unexpectedly."
    return f"{who} could not be recovered."


def _count(n: int) -> str:
    return {2: "Two", 3: "Three", 4: "Four", 5: "Five"}.get(n, str(n))


def _join(names: list[str]) -> str:
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def event_id_for(source_event_id: str, type_: str) -> str:
    """A supervisory event id derived from its source, so a replayed
    source event derives the same id and deduplicates by identity."""
    if source_event_id:
        return f"sup_{hashlib.sha1((source_event_id + type_).encode()).hexdigest()[:12]}"
    return new_id("sup")
