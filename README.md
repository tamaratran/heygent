# voice-agent

Hold a key, talk, and Claude Code answers out loud.

One command, `conduct`, starts the **Voice Conductor**: many concurrent
coding tasks, each in its own Claude session and its own git worktree,
routed by voice. "Fix the login redirect", then "tell the login one not to
touch OAuth", then "how's everything going?"

GPT Live is the ear and the mouth: it hears you, handles turn-taking, and speaks.
When you ask for something that touches your machine, it hands that work to a
Claude Agent SDK session running locally, then relays the result in conversation.

There is no server. Everything runs on your Mac; the only network traffic is an
outbound WebSocket to the OpenAI API.

## Requirements

- macOS (the overlay and the hotkey are AppKit and Quartz)
- [uv](https://astral.sh/uv)
- [tmux](https://formulae.brew.sh/formula/tmux) for attachable worker sessions
- [Claude Code](https://code.claude.com/docs/en/setup), logged in locally
- An OpenAI project key with GPT Live access

## Install

One line:

```bash
curl -fsSL https://raw.githubusercontent.com/tamaratran/voice-agent/master/install.sh | bash
```

It clones the app under `~/.voice-conductor/app`, installs the tools it
needs (uv, tmux, Claude Code), and puts a `conduct` command on your PATH.
Running it again updates everything. Homebrew users can install from the
tap instead once one exists (see `homebrew/`):

```bash
brew install tamaratran/tap/voice-conductor
```

Then run `claude` once in a new terminal and complete login and any
one-time setup screens before exiting. The Conductor starts delegated
workers unattended inside tmux; it keeps browser tools off if Claude Code
detects the Chrome extension during worker startup, so that prompt cannot
block a task.

```bash
conduct
```

The first run asks for your OpenAI API key on the terminal (input hidden)
and keeps it in `.env` next to the app, so there is no file to edit by
hand. From a git checkout, `./conduct.sh` is the same launcher. Use it
rather than running `conduct.py` directly: it fails fast when tmux or
Claude Code is missing, and prefers an arm64 `uv` at `~/.local/bin/uv`
when one exists, which matters on a Mac whose Homebrew `uv` is an Intel
build (this dependency tree has no Intel macOS wheels).

### The app in /Applications

To launch it like any Mac app - from the Dock, Spotlight or Finder,
with the microphone and automation permissions attributed to "Voice
Agent" rather than "Python":

```bash
python3 -m conductor.app_bundle --install
```

That builds `Voice Agent.app` into `/Applications` (leave `--install`
off to build into `./dist` instead). The bundle is a shell around this
checkout's `conduct.sh`, so a `git pull` here updates the app with no
rebuild. Launched this way there is no terminal; the console narration
goes to `~/.voice-conductor/logs/app-launch.log` and everything
diagnostic is in the JSONL logs as always.

### First launch

The very first launch introduces the permissions before anything is asked
for, so the Settings panes that follow arrive announced:

```
╭────────────────────────────────────────────────────────────────────────╮
│  Voice Conductor · First launch                                        │
│                                                                        │
│  macOS permissions this app uses                                       │
│  (System Settings > Privacy & Security):                               │
│                                                                        │
│    Microphone        to hear you          needed always                │
│    Input Monitoring  to see the Fn key    needed always                │
│    Accessibility     to click and type    only for computer-use tasks  │
│    Screen Recording  to take screenshots  only for computer-use tasks  │
│                                                                        │
│  Input Monitoring reaches only new processes:                          │
│  restart your terminal after granting it.                              │
│                                                                        │
│  Anything missing is asked for below:                                  │
│  the exact Settings pane opens for each.                               │
╰────────────────────────────────────────────────────────────────────────╯
```

On a terminal that renders hyperlinks (iTerm2, Ghostty, kitty, WezTerm)
each permission name is a click-to-open link to its Settings pane; other
terminals show the plain name. Shown once per machine (remembered in
`~/.voice-conductor/gui-permissions.json` alongside the asks). An
undetermined Microphone is left for macOS's own first-use prompt; only an
explicit denial gets the app's guidance and pane.

Four macOS permissions matter, and the launcher checks them at startup:
for each one the terminal lacks it opens the exact Privacy & Security pane
and prints which apps to turn it on for. Asked once per missing grant,
remembered in `~/.voice-conductor/gui-permissions.json`, quiet once granted.

Two are needed always:

- **Microphone** — to hear you (macOS also asks on first use; a denial is
  fixed in the pane)
- **Input Monitoring** — to see the Fn key (restart your terminal after
  granting; macOS applies it only to new processes)

Two more only for computer use - a task you open with the `computer` flag,
whose worker may look at and act on this Mac's screen. Turn them on for the
terminal (where the conductor's capability probe runs) and the app whose
terminal runs the workers (cmux when it is the worker surface):

- **Accessibility** — to click and type (posted input is silently dropped without it)
- **Screen Recording** — to take screenshots (`screencapture` returns a blank desktop without it)

The Python side of computer use, the Quartz and AppKit bindings (`pyobjc`), is declared in
`conduct.py` and in `conductor/computer.py` like every other dependency, so uv
installs it on the first run and there is nothing to add by hand. A worker runs
the driver on uv's managed Python, never on whatever `python3` its shell finds.

## Use

Hold **Fn**, speak, release. A capsule shows your words, as they are heard,
while the key is down (`CAPSULE_WORDS` in `boss.py`; off, it shows bars);
the answer arrives as a notification card and, by default, out loud.

The notifications - the reply card and the stack of task notifications -
start hidden, because they float above every other app and most of the
time you are working in one of them. **Tap Fn twice** to bring them up, and
twice again to put them away; nothing is lost while they are hidden, and
the 🔔 in the menu bar keeps the notification history either way. The
capsule is never hidden: it shows your words whenever you hold Fn. The
gesture is two presses of the same push-to-talk key, so - like any press
of it - double-tapping while the agent is speaking stops it mid-sentence.

```bash
conduct                   # manage tasks in the current repo
conduct --register ~/code/app
conduct --reply text      # written only
conduct --reply speak     # spoken only
```

**Workers always run in `--permission-mode auto`.** A delegated agent works in
its own git worktree on its own branch, in a window nobody is watching, so a
worker that stops for every edit is a worker that never finishes - there is no
read-only mode to fall back to. `auto` lets ordinary work proceed while still
weighing what the provider judges consequential, rather than waving everything
through the way `bypassPermissions` did. The worktree contains file changes;
`Bash` is not contained, so over a voice channel a misheard sentence can still
become a command worth reviewing.

Consequential prompts still escalate to you rather than being auto-answered;
the policy lives in `conductor/runtime.py`.

The **Manager** is different: it delegates rather than edits, so its own tools
stay read-only (`Read`, `Glob`, `Grep`).

### One conductor, one keyboard

Two conductors on one machine is not a degraded mode. They would share
the microphone, the tmux session names and the workers themselves, and
nothing prevented it or could even tell you it had happened - `ps` lists
`uv run ... conduct.py` beside the python it spawned, so one conductor
already reads as two. Now the second one refuses to start:

```
another conductor is already running here: pid 95280, run run_1f2a
Quit that one first, or start again with --takeover.
```

`conduct --takeover` asks the live one to leave (SIGTERM, its own
shutdown) and takes its place. Nothing happens by accident.

`~/.voice-conductor/conductor.lock` is that claim: pid, run id, what `ps`
says the process started at (so a recycled pid is not mistaken for the
conductor), and the list of computer-use workers this conductor is
supervising. It is rewritten on every sweep, and deleted on the way out.

That list is also the **GUI lease**. A worker allowed to drive the screen
asks for it before every click and keystroke. A worker whose conductor
died, or that a restart did not re-adopt, still acts, and is told so in a
warning after each action, so it can tell the user it was left behind.

### Somebody else is driving too

The user is on this machine, and so, sometimes, is another vendor's
computer-use agent. macOS offers no arbitration between automation
clients - nothing announces "I have the input", and there is no
point-in-time question that answers "was that event synthetic" (the HID
and combined event-source counters look like that answer and are not:
posting to `kCGHIDEventTap` advances both). What it does offer is enough
to know the screen has changed under you, which is what every one of
those collisions actually was.

So `conductor/computer.py` checks every action against the last look.
`look` records what the screen was - the on-screen window list, the
keyboard focus, and the system's key and click counters - and `click`,
`type`, `paste` and `key` re-check it in the moment before they act. A key
press or click nobody of ours made, input happening right now, a new front
window, a different window under the point, a moved caret: the action
still happens, and its output ends with a `warning:` line saying what
changed. `--expect Chrome` adds the one thing the driver cannot work out
for itself.

Nothing is refused. The worker is told, and looks again - which is
exactly the step that gets skipped when one agent writes over another.

### Aim at the right thing

A screen that has not changed can still be the wrong target. On
2026-09-09 a worker asked to text someone needed Messages, clicked the
Dock by position, hit FaceTime (camera on), then typed a name into
Messages' search box while the box did not have the caret. Five minutes,
and the thread never opened. So the driver also checks what an action
lands on, and says so rather than stopping: an action that looks
mis-aimed still happens, its output ends with a `warning:` line, and the
worker looks and puts it right.

- A `click` on the Dock warns that an icon picked by position opens
  whatever app is there (`open`, below, reaches an app by name); with
  `--expect`, so does a click on an element another app owns.
- A left click on a button, link or menu item is an accessibility press
  first, which leaves the user's cursor alone. Fields, rows and anything
  that will not take the press get a real click; `--pointer` forces one.
- `type` and `paste` name the field they went into, and warn when the
  keyboard focus was not a text field, or not in the `--expect` app or the
  `--into search` field.

These come from intent-pilot's fork of this driver, which found that
asking a model to prefer quiet presses did nothing (85% of 308 logged
actions were pixel clicks, zero were accessibility presses), so the quiet
route has to be what a click is.

### Reaching an app, and keystrokes macOS throws away

A worker reaches an app with `open Messages`, never by clicking its Dock
icon: the icons sit side by side, and on 2026-09-09 a click one over
opened FaceTime - camera on - instead of Messages. `open` turns the name
into a bundle id (a running app showing exactly that name first, then
Launch Services; nothing is guessed from part of a name) and runs
`open -g -b <id>`, which launches the app, or reopens one that is running
with no window, behind whatever the user is looking at. `--front` brings
it forward. If the app comes forward anyway, the app that was in front is
put back. `apps` lists what is running, which app keystrokes go to, and
which apps have a window on screen.

`paste TEXT` puts text in the focused field with one cmd+v instead of an
event per twenty characters. Everything on the clipboard is set aside
first and put back after - pictures and files, not only text - unless
something else wrote to the clipboard in between, which is then left
alone. The pasted text is marked for this Mac only, so Universal
Clipboard does not carry it to the user's other devices, and as
transient, so clipboard managers do not keep it.

`type`, `paste` and `key` warn while macOS secure input is on. A password
field, a terminal with Secure Keyboard Entry or a call window turns it
on, and while it is on macOS discards posted keystrokes without an error -
without the warning the driver would print "typed 13 characters" and
nothing else. The keystrokes are still sent, and posted clicks arrive as
usual. macOS does not reliably say who holds it (`ioreg`'s
`kCGSSessionSecureInputPID` named the front app, not the process that
had turned it on), so the warning names the front app as the place to
look.

All three are macOS-only for now; the X11 and Windows backends say so.

## cmux (optional today, required when selected)

cmux is the terminal built for coding agents - workspaces in a sidebar,
one per worker. It is not assumed to be on your machine and it is not
looked for on `$PATH`: the app bundle is the canonical location, because
the Homebrew symlink at `/usr/local/bin/cmux` points into it.

```bash
brew tap manaflow-ai/cmux
brew install --cask cmux
python3 -m conductor.cmux_setup      # what CI and dev bootstrap run
```

That last command prints the dependency state and exits non-zero when cmux
cannot be driven, so a build that expects it cannot ship unable to reach
it:

```json
{ "installed": true, "version": "0.64.22", "compatible": true,
  "executablePath": "/Applications/cmux.app/Contents/Resources/bin/cmux",
  "socketAvailable": true, "capabilitiesOk": true }
```

Two things it will tell you about if they are wrong. cmux's control socket
exists **only while cmux is running** - there is no daemon. And access is
denied by default (`socketControlMode: "cmuxOnly"`), so this app needs
`"password"` with a `socketPassword`, which is narrower than `allowAll`.
The command above prints the exact settings.

cmux is GPLv3 and this project bundles nothing - it is a repository run
with uv - so installing it is setup rather than something shipped inside a
package.

Selecting it is `WORKER_SURFACE = "cmux"` in `boss.py`. As the default it
is self-healing: startup installs, launches and configures it, and when it
still cannot be driven the launch says so and falls back to tmux panes -
every worker still in a window you can open and type into. Asking for it
by name (`--worker-surface cmux`) makes it hard: startup stops with setup
instructions rather than quietly ignoring what you asked for.

## Configuration

`boss.py` holds everything worth tuning: models, voice, tool policy, overlay
sizing, and the waiting-line vocabulary. The two prompts live in `prompts/` as
markdown - the text below each `---` divider is the live prompt.

| File | What it controls |
| --- | --- |
| `boss.py` | models, voice, tools, sizing, defaults |
| `prompts/voice_agent.md` | how the voice agent talks while the client works |
| `prompts/claude_worker.md` | how Claude answers when it is spoken aloud |

## Layout

| File | Role |
| --- | --- |
| `voice_agent.py` | the session: audio, the work loop, and the event loop |
| `overlay.py` | the capsule, the notification card, and the caption - the notifications hidden until a double tap on Fn asks for them |
| `hotkey.py` | watches the Fn key through a Quartz event tap, and reads the double tap |
| `conduct.py` | the Voice Conductor: multi-task voice control |
| `conductor/` | tasks, sessions, workspaces, Manager, observability, evals |
| `prompts/manager.md` | how the Manager routes utterances to tasks |
| `evals/gold/` | 100 routing cases (`python3 -m conductor.evals evals/gold`) |
| `evals/scenarios/` | 20 multi-turn scenarios (`python3 -m conductor.scenarios ...`) |
| `evals/adversarial/` | deliberately confusing phrasings |
| `tests/` | unit, property, contract, isolation and recovery tests (no LLM needed) |

## Debugging

Start here. The launcher prints the log path and a run id before anything
can fail, and everything that process does lands in that one file:

```
debug log: /Users/you/.voice-conductor/logs/conductor-8412.jsonl
run id: run_8779024cad09
```

One file per process, named for its pid. Every process shares a
home, and two processes rotating one file at 10 MB lose lines to a rename
race - so each writer owns its own. The recipes below glob
`conductor-*.jsonl`, which reads every run at once; filter by `run_id` to
narrow to one.

One JSON object per line, so ordinary tools are enough - no viewer needed to
answer "what happened just now":

```bash
tail -f ~/.voice-conductor/logs/conductor-*.jsonl
grep '"level":"error"' ~/.voice-conductor/logs/conductor-*.jsonl
grep '"task_id":"task_a3b4fb42"' ~/.voice-conductor/logs/conductor-*.jsonl
```

Every command here spells the path out. Shortening it to `$LOG` reads
better until someone copies one line on its own: with the variable unset
the shell expands it to nothing, `jq` reads stdin instead of the file, and
the command exits 0 with no output - which looks exactly like "no errors
found" on a log full of them. A wrong literal path says
`No such file or directory`.

Every line carries `timestamp`, `level`, `component`, `event`, `message`,
`run_id` and `trace_id`, plus `task_id`, `project_id`,
`provider_session_id` and `duration_ms` where they apply. A `run_id` is one
launch of the process; a `trace_id` is one interaction, opened at the
microphone, so filtering on either reads as a single story. Failures include
an `exception` object with the type, message and full traceback - a
swallowed exception with no record is the bug that costs an afternoon, so
handlers that keep the app alive still write one.

### Triage recipes

Start at the top and stop when you have the answer. These are the questions
people actually ask, with the command that answers each:

**"Something broke - what?"** Every failure, newest last:

```bash
jq -c 'select(.level=="error")|{event,message,task_id}' \
  ~/.voice-conductor/logs/conductor-*.jsonl
```

**"Show me the traceback."** The exception object carries the real one, not
a one-line summary:

```bash
jq -r 'select(.exception)|.exception.traceback' \
  ~/.voice-conductor/logs/conductor-*.jsonl | tail -30
```

**"What happened in the last thing I said?"** One interaction, across the
microphone, the Manager and the worker it touched:

```bash
TRACE=$(jq -r 'select(.trace_id!="")|.trace_id' \
  ~/.voice-conductor/logs/conductor-*.jsonl | tail -1)
jq -r --arg t "$TRACE" 'select(.trace_id==$t)
  |"\(.component)\t\(.event)\t\(.message[0:60])"' \
  ~/.voice-conductor/logs/conductor-*.jsonl
```

**"Why is this task stuck?"** One worker's whole life. Read it in file
order - that is write order, and it is already chronological:

```bash
jq -r 'select(.task_id=="task_a3b4fb42")
  |"\(.timestamp)\t\(.component)\t\(.event)\t\(.message[0:60])"' \
  ~/.voice-conductor/logs/conductor-*.jsonl
```

Look for a `task.approval_required` with no later `approval.resolved`: that
is a worker waiting on a decision nobody made. `task.worker_gone` means the
PTY died and the sweep noticed.

**"What was slow?"** Anything timed reports `duration_ms`:

```bash
jq -r 'select(.duration_ms)|"\(.duration_ms)\t\(.event)\t\(.task_id//"")"' \
  ~/.voice-conductor/logs/conductor-*.jsonl | sort -rn | head -20
```

**"Only the most recent launch."** Each process writes its own file, so the
newest one is that run:

```bash
ls -t ~/.voice-conductor/logs/conductor-*.jsonl | head -1
jq -c . "$(ls -t ~/.voice-conductor/logs/conductor-*.jsonl | head -1)"
```

**No `jq`?** The field order is stable, so plain grep works:
`grep '"level":"error"' ~/.voice-conductor/logs/conductor-*.jsonl`.

### The event vocabulary

`event` is a stable dotted name, and it is what you grep for. The prefixes:

| Prefix | Covers |
| --- | --- |
| `app.*` | process start, stop, crash, missing key |
| `voice.*` | the microphone leg: listening, transcript, barge-in, replies |
| `manager.*` | routing turns, tool calls and their failures |
| `conductor.*` | actions received/executed, project resolution |
| `task.*` | lifecycle: created, started, approval_required, completed, failed, worker_gone, retired (closed by the watchdog after `boss.IDLE_RETIRE_S` unaddressed) |
| `runtime.*` | the provider session: progress, approvals, readers, watchers |
| `approval.*` | detection, policy decision, delivery, resolution |
| `recovery.*` | reconnecting or reconstructing a lost worker |
| `surface.*` | terminal windows opening, focusing, going missing |
| `storage.*` | state writes and unreadable files |
| `overlay.*`, `hotkey.*` | the child processes, including their stderr. `hotkey.hold_implausibly_short` means the key reported a release nobody made, and whatever was being said was dropped - a deliberate double tap is exempt, being two short holds on purpose. `overlay.visibility_changed` is the notifications going on or off screen (the capsule never hides) |

A few that name the two-agents problems specifically:

- `conductor.instance_refused` - a second conductor tried to start here
  and did not. `conductor.instance_takeover` is `--takeover` asking the
  live one to go; `conductor.instance_stale_lock` is a lock left by a
  process that is no longer running, cleared on the way in.
- `runtime.session_name_reclaimed` - a session name was still listed with
  nothing running under it (one survived a week of restarts and was what
  every `duplicate session: cond_task_b152b744` collided with), so the
  empty window was closed and the name reused.
  `runtime.session_name_taken` is the opposite: a live worker holds it,
  and nothing is started beside it. `runtime.session_name_collision` is
  tmux refusing a name outright - almost always a second conductor.
- `computer.action_warned` - a click, keystroke, paste or `open` that went
  ahead with a warning: what changed on screen, a missing conductor, the
  Dock, no text field, secure input. The worker sees the same sentence
  after the action.
- `computer.app_opened` - `open` reaching an app: its bundle id, whether
  `--front` was asked for, its windows on screen, and `restored` when it
  came forward anyway and the previous front app was (`true`) or could
  not be (`false`) put back.
- `computer.pasted` - a `paste`, with its length and whether the
  clipboard went back, and went back whole.
- `computer.secure_input_unknown` - the secure-input probe itself failing,
  which reads as secure input being off.
- `voice.speech_queued` - one caller waiting for the speakable channel
  because another is using it. Before the lock, both spoke at once.

### What is *not* in this log

Knowing where it stops saves a fruitless search:

- The worker's actual reasoning and file edits - that is
  `~/.voice-conductor/executions/<task>.log` (readable) and
  `~/.claude/projects/` (the provider's own record).
- The conversation as spoken - `session.jsonl`.
- Terminal narration (`you:`, `agent:`). That is `print`, for the human
  watching; it is deliberately not a debugging channel.

Every timestamp is millisecond precision, so sorting on it is safe within a
run. Across runs, prefer `run_id` - two processes have no shared clock
ordering worth trusting.

Each file rotates at 10 MB keeping 5 backups, and the 20 most recent files
survive; older runs are pruned at startup. `--debug` makes the console
verbose; the file is always complete regardless. Child-process stderr
(`overlay.py`, `hotkey.py`) is captured under the `overlay` and `hotkey`
components - it previously went nowhere.

To check that capture end to end against the real AppKit and Quartz
children, without an API key or a microphone:

```bash
python3 tests/smoke_logging.py
```

These logs are local and unredacted by design (same stance as the traces
below): they record what was actually said and run, including prompts and
command arguments, so treat them as sensitive. No API keys or tokens are
written.

| Where | What |
| --- | --- |
| `~/.voice-conductor/logs/conductor-*.jsonl` | everything: events, warnings, tracebacks |
| `~/.voice-conductor/observability/events/` | domain events by day, for the viewer |
| `~/.voice-conductor/executions/<task>.log` | one worker's readable transcript |
| `session.jsonl` | the spoken conversation |
| `~/.claude/projects/` | each worker's raw provider history |

The trace viewer reads the domain events - traces, per-trace timelines with
the Manager's inputs and decisions, task timelines, errors, and latency
metrics:

```bash
python3 -m conductor.viewer <conductor_home> traces
python3 -m conductor.viewer <conductor_home> trace <trace_id>
python3 -m conductor.viewer <conductor_home> metrics
```

A trace starts at the microphone, not at the Manager: holding Fn opens it, and
the hold, what was heard, the work it started, the routing decision and the spoken
reply all carry the same trace id. `--component voice` narrows a trace to the
voice leg alone - the hold's duration, the transcript as it landed, barge-in,
and how long after release the first word was spoken.

A real routing failure becomes a permanent regression test:
`conductor.replay.trace_to_eval(trace, name)` writes it to
`evals/regressions/`, and the eval runner picks it up from there.

The conductor persists tasks under `~/.voice-conductor/projects/<id>/`:
`state.json` is authoritative state, each task keeps a `context.md` (semantic
memory) and `events.jsonl` (history), and observability traces land in
`~/.voice-conductor/observability/`. Run the complete test suite, including the
AppKit and Quartz assertions, with:

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

The real tmux + Claude Code launch path is intentionally separate because it
uses your Claude account. Run it after setup when you need to verify worker
startup and follow-up delivery:

```bash
env -u ANTHROPIC_API_KEY python3 -u tests/smoke_interactive.py
```

Every exchange is appended to `session.jsonl`. Each utterance runs as a real
Claude Code session whose id is printed, so `claude -r <id>` reopens it with the
full tool history.
