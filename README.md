# Voice Agent

Hold **Fn**, talk, and run many Claude Code sessions at once — by voice, on
your Mac.

Voice Agent (the *Voice Conductor*) is a push-to-talk front end for a
Claude Code "Boss" that plans and delegates coding work to worker sessions
running in tmux panes or the cloud. You speak; it listens through the
OpenAI Live API, hands what you said to the Boss, and the Boss creates,
steers, checks on and stops workers, each in its own git worktree. A
floating overlay and a Boss window show what is going on; the voice tells
you when something finishes or needs a decision.

```
"Fix the login redirect"                   -> a new worker in a fresh worktree
"Tell the login one not to touch OAuth"    -> a message to that worker
"How's everything going?"                  -> a spoken status report
"Stop the settings one"                    -> that worker is interrupted
"Run this one in the cloud"                -> a Claude Code cloud session
```

Everything persists under `~/.voice-conductor/`; a restart reconnects to
the workers that are still alive and tells you about the ones that are not.

## Requirements

- macOS on Apple silicon (the dependency tree has no Intel wheels)
- [uv](https://astral.sh/uv), [tmux](https://github.com/tmux/tmux) and
  [Claude Code](https://code.claude.com/docs/en/setup), logged in
  (`claude` once)
- An OpenAI API key with access to the Realtime/Live API (the first run
  asks for it and keeps it in `.env`)
- macOS permissions, which the first run walks you through: Microphone,
  Input Monitoring (to see the Fn key), and — for computer use —
  Accessibility and Screen Recording

## Install

### One-liner

```bash
curl -fsSL https://raw.githubusercontent.com/tamaratran/heygent/main/install.sh | bash
```

Clones the app under `~/.voice-conductor/app`, installs uv / tmux / Claude
Code if missing, and puts a `conduct` command on your PATH. Re-running it
updates the app.

### From a checkout

```bash
git clone https://github.com/tamaratran/heygent.git
cd heygent
./conduct.sh
```

### As a macOS app

`Voice Agent.app` is a thin bundle around the checkout: Finder, the Dock
and Spotlight can launch it, and macOS attaches the microphone and
automation permissions to "Voice Agent" rather than to "Python". It execs
`conduct.sh` in the repo it was built from, so a `git pull` there updates
the app with no rebuild.

```bash
python3 -m conductor.app_bundle              # ./dist/Voice Agent.app
python3 -m conductor.app_bundle --install    # straight into /Applications
```

To sign it for other Macs, pass a Developer ID identity (the bundle is
then signed with the hardened runtime and a timestamp, ready for
`notarytool`):

```bash
python3 -m conductor.app_bundle \
  --sign "Developer ID Application: Your Company (TEAMID)"
```

### Homebrew

A head-only formula lives in [`homebrew/`](homebrew/); see its README for
setting up the tap.

## Use

1. Start it: `conduct` (or `./conduct.sh`, or open Voice Agent.app).
2. Hold **Fn** and speak. Release to send. The overlay capsule shows when
   it is listening, thinking and speaking.
3. Double-tap **Fn** to show or hide the notification panel.
4. Talk to the Boss like a lead engineer: describe work, ask for status,
   redirect a worker, approve or decline what a worker asks for.

The Boss window draws each spoken turn live; you can also type into it.
Workers appear as tmux panes inside that window (default) or as one
[cmux](https://github.com/manaflow-ai/cmux) workspace each
(`--worker-surface cmux`). A worker that needs a decision (a destructive
command, an unclear spec) pauses and the voice asks you.

Useful flags (`conduct --help` has them all):

| Flag | What it does |
|------|--------------|
| `--register PATH` | Manage tasks in another repo (default: the current one) |
| `--worker-location cloud` | Run workers as Claude Code cloud sessions |
| `--new-chat` | Start a fresh Boss conversation instead of continuing the last |
| `--quiet` | Toast and bell only; nothing spoken |
| `--no-hotkey` | Listen continuously instead of on Fn |
| `--takeover` | Replace a conductor already running in this home |
| `--debug` | Verbose console logging (the file log is always complete) |

## How it works

```
   Fn key ──► hotkey.py ──► voice_agent.py (OpenAI Live: ears + mouth)
                                   │ transcript
                                   ▼
                        conductor/global_conductor.py
                                   │
                                   ▼
        the Boss  (a Claude Code session with the conductor's MCP tools)
                  create_task · send_to_task · list_tasks · interrupt_task …
                                   │
                 ┌─────────────────┼─────────────────┐
                 ▼                 ▼                 ▼
             worker A          worker B          worker C
        (tmux pane, own    (tmux pane, own    (cloud session)
         git worktree)      git worktree)
```

- **Voice does no routing.** Every utterance becomes one
  `handle_user_message()` call — the same one the tests drive.
- **The Boss** is an ordinary Claude Code session given the conductor's
  tools over MCP. It owns the plan; the conductor owns the processes.
- **Workers** are Claude Code sessions in isolated sibling worktrees,
  supervised by watching their transcripts. Approvals, finishes and
  failures flow back to the Boss and then to you.
- **State** lives in `~/.voice-conductor/` (projects, tasks, Boss
  sessions, logs) and survives restarts.

Key files: `conduct.py` (entrypoint), `voice_agent.py` (voice),
`overlay.py` (status window), `hotkey.py` (Fn), `conductor/` (task
operating system, runtimes, Boss bridge, MCP server), `prompts/` (the
Boss's system prompt), `docs/` (design notes and spikes).

## Debugging

One place to look: `~/.voice-conductor/logs/conductor-<pid>.jsonl` — JSONL,
one line per event, one file per process, with tracebacks for everything
that failed, including failures the app caught and kept running through.

```bash
# what failed
jq -c 'select(.level=="error")|{event,message,task_id}' \
  ~/.voice-conductor/logs/conductor-*.jsonl

# why
jq -r 'select(.exception)|.exception.traceback' \
  ~/.voice-conductor/logs/conductor-*.jsonl | tail -30
```

Events carry `run_id` (one launch), `trace_id` (one interaction, opened at
the microphone) and `task_id` (one worker). `AGENTS.md` has the full
walkthrough and event vocabulary.

Logs are local and unredacted (prompts and transcripts verbatim). The app
launched from Voice Agent.app also writes `~/.voice-conductor/logs/app-launch.log`.

## Development

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

One module: `python3 -m unittest tests.test_app_bundle -v`. The smoke tests
that need a real Claude login or the real screen are separate; see
`AGENTS.md`.

## Privacy and secrets

Audio goes to OpenAI while Fn is held; code and transcripts go to Anthropic
through Claude Code. `OPENAI_API_KEY` is read from `.env` and removed from
the environment of every worker the conductor starts. Nothing else leaves
the machine.
