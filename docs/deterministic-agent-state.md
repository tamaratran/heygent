# Deterministic agent state, cards, and Boss voice

What happens after a worker is already running, and how the product
answers, for every worker event:

    What happened in the worker?
    What should the live session card show?
    Should this appear in the activity/notification system?
    Should the Boss know about it?
    Should the Boss say something?
    If it was not spoken, why?

The principle: **the Boss is never in the worker → UI path.** The card is
a deterministic projection of canonical state. The Boss observes a small
semantic stream separately and decides what to *say*, never what is
*true*.

## The shape

```
worker
  ↓ provider adapter
AgentEvent            event_id, sequence, type, summary, detail
  ↓ Conductor._on_event
reduce()              conductor/subagent_state.py — pure
  ↓
SubagentState         one file per task beside the Task's own
  ↓ bus: subagent.state_changed
  ├── project_card()  conductor/session_card.py → overlay   (objective)
  ├── NotificationService → history / bell                  (attention)
  └── SupervisorInbox → Manager digest + SpeechPolicy       (semantic)
```

Three consumers read `subagent.state_changed`; none can make another
wrong. A NotificationService callback that raises, a Manager turn that
fails, a voice that is muted — the card is already correct, because it was
drawn from the state before any of them ran.

## What each module owns

| module | answers | never |
|---|---|---|
| `agent_events.AgentEvent` | what canonical runtime event happened; durable `event_id`; per-execution `sequence` | decides anything |
| `agent_activity.classify` | what the worker is *doing*, in nine words | shows prose |
| `subagent_state.reduce` | what is objectively true now | consults a model, a notification, the voice |
| `session_card.project_card` | exactly what the card shows for this state | reads a notification, waits for speech |
| `notifications.NotificationService` | history, bell, read/unread, dedupe | owns the live card |
| `supervisor_inbox.SupervisorInbox` | which events reached the Boss, durably, once | floods the Boss with progress |
| `supervisor_inbox.SpeechPolicy` | whether/how to say it, and why not | phrases it (the Live model does) |
| `voice_coordinator.VoiceCoordinator` | one voice, one stream, priority, barge-in | starts a second engine |

## Guards, and where they live

All three live in `reduce()` and nowhere else:

- **duplicate** — the same `event_id` applied twice changes nothing.
  Delivered ten times, a completion is one transition, one card change,
  one notification, one supervisory event, at most one spoken line.
- **stale** — a lower `sequence` than one already applied changes nothing.
  `20 → completed` then `19 → running tests` leaves the card Completed.
  The runtime stamps the sequence at its one emit seam.
- **terminal** — completed / failed / cancelled are final for events.
  Only `apply_lifecycle()` — an explicit decision — leaves them.

Lifecycle decisions (pause, resume, cancel, close, interrupt, recover) are
made in nineteen places. They all pass through `TaskStore.update`, which
now announces `task.status_changed`; one subscriber keeps canonical state
in step. No caller has to remember.

## One judgement, stated

A PTY worker ends a **turn**, not a task. The runtime's `completed` means
"answered and standing by", and closing a task has always been the
Manager's or the user's call. The reducer keeps that: a runtime completion
makes the worker `idle` with a result attached, and the card renders
idle-with-result as `Completed — <summary>` — the tick the user knows.
`completed` as a status is reached through the lifecycle, when the task
itself is closed. The spec's "completed" card and the product's "task is
done" are therefore two different facts that happen to look the same on
screen, and both are correct.

## The event matrix, as implemented

| event | card | history/bell | Boss receives | voice |
|---|---|---|---|---|
| session started | Starting… → Working… | no | no | no |
| routine activity | Working — running tests… | no | no | no |
| milestone (checkpoint) | Working — <milestone> | condensed | no | no |
| routine auto-approval | stays Working | yes | **withdrawn** on resolve | no |
| sensitive approval | Needs your approval — … (attention) | yes | yes | yes |
| input required | Needs your input — … (attention) | yes | yes | yes |
| approval resolved | Working — continuing | yes | no | no |
| turn ended (runtime completed) | Completed — <summary> ✓ | yes | yes | contextual |
| failed | Failed — <summary> | yes | yes | yes |
| paused / cancelled (user) | Paused / Cancelled | yes | no | no |
| interrupted (from working) | Interrupted | yes | yes | contextual |

"Contextual" for the voice means `SpeechPolicy` decides, and records why:

    spoken | queued | coalesced | suppressed_policy | suppressed_duplicate
    suppressed_context | suppressed_stale | interrupted_by_user

- several finishes within a beat become one sentence: *"Three tasks just
  finished: Posely login, billing, and the test app."*
- a subject the conversation already covered is not repeated;
- a finish older than fifteen minutes is not brought up unprompted;
- an approval the policy resolved before the voice reached it is
  withdrawn, so a routine auto-approval never becomes a question.

Every decision is in the inbox's JSONL beside the event, so *"did the user
ever hear about this, and if not, why not?"* is a read, not an
investigation.

## Restart and reconnect

The tray renders from `GlobalConductor.subagent_states()` — canonical
state on disk — not from replayed notifications. Nothing that already
finished is announced again: the inbox knows what the Manager has seen and
what was spoken, and comes back knowing it.

## A sequence belongs to an epoch

The stale guard compares an event's sequence with the persisted
high-water mark. The runtime counts from 1 per process; the mark
survives the process. Measured: a worker's sidecar said 45, the app
restarted, and the fresh runtime's 1, 2, 3 … were dropped as stale -
61 events for one worker, finished turns among them, so neither the
card nor the Boss heard. Every event now carries the runtime's
`epoch` (one per process, stamped in `_emit`); `SubagentState` keeps
`last_epoch`, and an event from a different epoch starts the count
again instead of being compared with the old one. Unstamped events are
compared as before. `runtime.session_created` logs the epoch, the
sequence the count starts from and the transcript offset, so the next
"nothing arrived after the restart" is a one-line grep. Tests:
`tests/test_restart_keeps_events.py`.

## Dismissal is state

Waving a card away used to live in the overlay process's memory, and
the projection set `force` for any finished worker. Measured: a
dismissed card came back on every later change to an idle worker, and
on every restart. Now `SubagentState` counts applied changes
(`revision`) and remembers at which revision the latest result and the
latest question arrived (`result_revision`, `attention_revision`).
Dismissing a card records `dismissed_at_revision` in the sidecar -
"seen up to here". `project_card` forces a reopen only for a result or
a question that arrived after that, and otherwise marks the card
`dismissed`, which the panel honours on the startup re-render. A
lifecycle decision (pause, interrupt, close) bumps the revision but is
not news. Tests: `tests/test_dismissal_sticks.py`.

## What this does not do yet

- **The Boss does not decide speech with a model.** `SpeechPolicy` is
  deterministic; the Live model phrases what it is handed. Handing the
  decision itself to the Manager — "you know the conversation; is this
  worth saying?" — is the natural next step and is what the inbox's
  digest and delivery records are shaped for.
- **Session routing is untouched**, as the spec requires.
- **Providers other than Claude Code** get the same reducer through the
  same `AgentEvent`; the classifier's tool names are Claude's today.
