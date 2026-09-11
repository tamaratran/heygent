# Working in this repo

## Verify

The complete suite needs the AppKit/Quartz and audio dependencies; without
them several modules cannot import and the failures are environmental, not
real:

```bash
uv run \
  --with 'aiohttp>=3.10,<4' \
  --with 'numpy>=1.26,<3' \
  --with 'sounddevice>=0.4.6,<1' \
  --with 'claude-agent-sdk>=0.2,<1' \
  --with pyobjc-framework-Cocoa \
  --with pyobjc-framework-Quartz \
  --with pyobjc-framework-ApplicationServices \
  python -m unittest discover -s tests -p "test_*.py"
```

One module: `python3 -m unittest tests.test_logging -v`.

The tmux + Claude Code smoke test uses a real Claude account, so it is
separate and not part of the suite:
`env -u ANTHROPIC_API_KEY python3 -u tests/smoke_interactive.py`.

The computer-use smoke test drives the real screen (TextEdit) and needs
the Accessibility and Screen Recording permissions, so it too is separate:
`python3 -u tests/smoke_computer.py`. The Quartz bindings it and the driver
need are not a manual install: `conduct.py` and `conductor/computer.py`
declare `pyobjc-framework-Quartz` and `-ApplicationServices` inline, and a
worker is handed the driver as `uv run --script conductor/computer.py`
(`computer.cli_command()`), so uv resolves them wherever the repo lives.

## Debugging a run

`~/.voice-conductor/logs/conductor-<pid>.jsonl` is the one place to look:
JSONL, one line per event, one file per process (two writers on one file
lose lines to a rotation race). Rotates at 10 MB keeping 5 backups; the 20
most recent files survive. Both launchers print their path and a `run_id`
at startup. Globbing `conductor-*.jsonl` reads every run at once.

Do not ask the user to reproduce the problem with extra printing added.
The log already has it, including tracebacks from failures that are caught
and swallowed. Start here:

Spell the path out in each command. With an unset `$LOG` the shell expands
it to nothing, `jq` reads stdin instead, and the command exits 0 with no
output - indistinguishable from a clean log.

```bash
# 1. what failed
jq -c 'select(.level=="error")|{event,message,task_id}' \
  ~/.voice-conductor/logs/conductor-*.jsonl

# 2. why - the real traceback, not a summary
jq -r 'select(.exception)|.exception.traceback' \
  ~/.voice-conductor/logs/conductor-*.jsonl | tail -30

# 3. the interaction it happened in (mic -> Manager -> worker)
TRACE=$(jq -r 'select(.trace_id!="")|.trace_id' \
  ~/.voice-conductor/logs/conductor-*.jsonl | tail -1)
jq -r --arg t "$TRACE" 'select(.trace_id==$t)
  |"\(.component)\t\(.event)\t\(.message[0:60])"' \
  ~/.voice-conductor/logs/conductor-*.jsonl

# 4. one worker's whole life, in file order (already chronological)
jq -r 'select(.task_id=="task_xxxx")
  |"\(.timestamp)\t\(.component)\t\(.event)\t\(.message[0:60])"' \
  ~/.voice-conductor/logs/conductor-*.jsonl
```

Reading the result:

- `task.approval_required` with no later `approval.resolved` for that task
  means a worker is blocked on a decision nobody made.
- `task.worker_gone` means the watchdog found a running task with no worker behind it and reclassified it as interrupted. `window=gone` is the PTY itself missing; `window=empty` is the window still listed but no process in it (a cmux workspace outlives the claude that exited inside it, and until this check that task stayed a busy worker for ever - one of the three slots create_task will fill).
- `task.failed` with `tmux session ended` is the per-session watcher giving
  up. It is preceded by `runtime.session_missing` (first missed poll) and
  followed by `runtime.watch_ended`; if `cmux.listing_failed` sits just
  before them, cmux stopped answering `workspace list` and the sessions
  were probably alive - check `pgrep -f 'claude --permission'` before
  believing it. `runtime.session_found` means the scare passed;
  `runtime.session_process_alive` means the host lost the session but its
  claude process is running and it is still being watched.
  `cmux.listing_stale` means cmux has not answered for 30 s and its last
  good listing no longer stands in for it.
- `boss.watcher_restarted` means the Boss was alive but nobody was reading
  its transcript (its replies were not being spoken); a turn or a worker
  update restarted the watcher, which delivers what was written meanwhile.
- `launch.unconfirmed` (with `cmux.launch_unconfirmed` carrying the pane
  text) means the host could not see the launch line taken; the launch
  is then judged by the session's transcript - a `task.failed` after it
  is real and the session was closed, a `runtime.session_created` means
  the worker started anyway. It is not, by itself, a worker that never
  started.
- `boss.words_queued` is an utterance typed into the Boss while it was
  mid-turn (`manager.turn_overlapping` is the same moment, conductor
  side). It sits in Claude Code's input box until the Boss looks up; a
  `boss.turn` with `folded: true` is one that was read into the earlier
  turn and answered there. `boss.words_lost` is one Claude Code never
  wrote to its transcript (a later one was read instead): it gets no
  answer, and the timeline says "the Boss never read: ...". Which
  utterance a reply answers is matched by the words of the user line,
  never by order.
- A Codex Boss (`--boss-cli codex`, conductor/codex_boss.py) has no
  session id until its first message: `runtime.session_discovered`
  (with `handle: pending_...`) is its rollout being found, and the
  timeline's "Codex session id ... recorded" is what a restart resumes.
  A Boss turn that times out with no `session_discovered` before it
  means the rollout was never found. `boss.codex_servers_unknown` means
  `codex mcp list` did not answer, so the user's own MCP servers were
  left on for that Boss.
- `boss.note_for_voice` is the Boss telling the voice something on the side
  (ids, files, numbers); `voice.commentary_sent` is it reaching the voice
  model on the commentary channel. Neither is ever spoken or shown. A note
  with `outside_turn: true` was made with no voice turn open (the Boss
  replying to a pushed worker update) and goes to the voice at once.
- A worker finish reaches the user through the Boss: `supervisor.event_pushed`
  (typed into its window), then the Boss's reply as `boss.tell_user` with
  `source: worker_update`, then `voice.reply_spoken`. A reply recorded in
  the Boss timeline as `boss_message` / `typed` right after a push is one
  that was NOT heard - the push had been ended early.
- `task.watch_resumed` is a live worker nobody was reading getting a
  watcher: at startup, and on every sweep.
- `codex.app_server_started` is the Codex app-server spike
  (docs/codex-app-server.md) - a front end that draws its own turns,
  toasts and approval buttons. It is not wired into the Boss; nothing
  logs it during an ordinary run.
- A turn of the Boss that mentions a session gets a toast under it,
  drawn by a Stop hook on the Boss session (conductor/turn_toast.py).
  `jump.listening` is the loopback port its links resolve on,
  `jump.focused` a link that was clicked, `jump.unavailable` no port -
  toasts then name sessions without linking them. The hook reads
  boss/turn_sessions.json, which the conductor rewrites from the same
  cards the overlay draws.
- `agent.event_dropped` with `reason: terminal` is a report from a worker
  the reducer has closed (completed / failed / cancelled). A run of them
  after a `task.failed` "tmux session ended" is a worker written off by
  mistake; the next `send_to_task` / `resume_task` revives it
  (`subagent.state_changed` with `kind: task.running`) and its reports
  count again.
- `boss.update_push_failed` is a worker update that could not be typed
  into the Boss; it is kept, `boss.update_push_retry` tries it again, and
  past the retries it goes with the next flush. Nothing pushed is dropped.
- `voice.reply_spoken` is the voice model having said something (filler,
  small talk, a relayed answer). The Boss sees these only through its
  `what_the_voice_said` tool; nothing is typed into its window for them.
- `boss.window_shown` is the Boss's cmux workspace being put in front of
  the user, once, on the voice turn that makes this a conversation
  (`SHOW_AFTER_TURNS`). Before it the Boss runs in the background and cmux
  is launched hidden; a worker starting still raises its own window. A
  cmux the user quits stays quit: only a turn or a worker start relaunches
  it (`cmux_runtime._REPAIRING`), never the sweep's listing.
- `voice.utterance_completed` means a finished user turn; the client runs
  every one of them directly, so a completed utterance with no
  `voice.work_completed` or `voice.work_failed` after it was dropped.
- `runtime.*_failed` / `*.callback_failed` carry an `exception` object.
- `overlay.stderr` / `hotkey.stderr` are the child processes' own output.
- `hotkey.hold_implausibly_short` means the Fn key reported a release
  the user never made. The microphone shuts on release by design, so
  the utterance it cut off leaves no other trace - this line is it. The
  two short holds of a deliberate double tap (the notifications toggle)
  do not raise it; they arrive flagged, and `overlay.visibility_changed`
  says which way the notifications went. The capsule is never hidden.
- `conductor.instance_refused` means a second conductor tried to start in
  this home and did not: one is already running (pid in `data`). Two of
  them would share the microphone, the tmux session names and the
  workers, so the second one refuses rather than joining in. `conduct
  --takeover` asks the live one to leave first. (Before believing a
  report of two conductors, check the pids: `ps` lists `uv run ...
  conduct.py` beside the python it spawned, and that is one.)
  `conductor.instance_stale_lock` is a lock left by a process that is no
  longer there, cleared on the way in.
- `runtime.session_name_reclaimed` is a session name still listed with
  nothing running under it, closed so the name can be used. One of those
  survived a week of restarts and was what every `duplicate session:
  cond_task_b152b744` collided with. `runtime.session_name_taken` is a
  LIVE worker under the name: nothing is started beside it.
  `runtime.session_name_collision` is tmux refusing the name outright,
  which almost always means a second conductor.
- `computer.action_warned` is a click, keystroke, paste or `open` that
  went ahead with a warning: the screen changed since the last `look` (a
  key or click nobody of ours made, a new front window, a different window
  under the point, a moved caret), no live conductor holds the worker, the
  click hit the Dock, typing or pasting had no text field, or macOS secure
  input was on (the keystrokes were still sent, and macOS probably threw
  them away). The worker gets the same sentence as a `warning:` line after
  the action. Nothing is refused; the way to clear it is `look`.
  `computer.app_opened` with `restored` set is an app that came forward
  despite `open -g`: `true` means the app the user had in front was put
  back, `false` that it could not be.
- `voice.speech_queued` is one caller waiting for the speakable channel
  while another uses it. Before the lock in VoiceAgent.announce, the
  Boss's reply and a worker update reached that channel together and the
  user heard both at once.
- `duration_ms` appears on anything timed; sort by it to find slowness.

Correlation ids: `run_id` (one launch), `trace_id` (one interaction, opened
at the microphone), `task_id` (one worker). The README's Debugging section
has the full event vocabulary and the other artifacts (per-task
transcripts, domain events, the provider's own history), plus what is
deliberately *not* in this log.

## Logging conventions

Use `application_log` from `conductor.observability` rather than `print` for
anything diagnostic. `print` is for the user's live console narration only
(what was heard, what was said); it is not captured anywhere.

```python
from .observability import application_log

application_log("runtime", "runtime.session_lost", "the PTY is gone",
                severity="warning", task_id=task.id)
```

- `component`: voice, manager, conductor, task, workspace, runtime, storage,
  ui, overlay, hotkey.
- `event`: a stable dotted name, `noun.verb_past` - it is what people grep.
- `severity`: debug, info, warning, error. Debug always reaches the file and
  never the console unless `--debug`.
- Correlation ids (`task_id`, `project_id`, `trace_id`,
  `provider_session_id`) are top-level keyword arguments; anything else you
  pass lands in `data`.

**Never swallow an exception silently.** Keeping the app alive is fine;
hiding why is not:

```python
except Exception:
    application_log("runtime", "runtime.watch_failed", "watcher failed",
                    severity="error", exc_info=True, task_id=task_id)
```

Domain events already flow through `ObservabilityBus`; `LoggingSink` bridges
them into the same file, so do not log an event you are also emitting.

`conductor/storage.py` imports `application_log` lazily inside functions -
`observability` imports `storage`, so a module-level import is circular.

Logs are local and unredacted by design (prompts, transcripts and command
arguments are written verbatim). Never log credentials.
