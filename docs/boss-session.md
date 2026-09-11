# The Boss session

The session you are talking to, as a first-class, persistent, visible,
interactive thing.

    Voice conversation  <->  BossSession  ->  workers it starts (children)

## The one decision everything else follows from

**The Boss is an actual Claude Code session, and the visible session is
the execution.** No mirror, no hidden Boss, no model loop of our own.
The Boss was always a real Claude Code session - a session id, a
transcript on disk - run invisibly through the SDK. Now that same
session, **resuming the id it already has**, is hosted the way a worker
is: a `claude` process in its own cmux workspace, watched through its
transcript, typed into through the PTY.

A spoken utterance is typed into that window. A line you type into that
window is a turn of the same session. Both land in one ordered timeline.

It is typed **the moment it is said**, whatever the Boss is doing. Mid-turn,
Claude Code keeps it as a queued message in its own input box - you see
your words land - and reads it when it next looks up: into the running
turn, in which case both get one answer (spoken once), or as the turn
after, with an answer of its own. Which happened is read off the
transcript. It used to be a lock around the whole turn, and the second
thing said was not typed until the first was answered - measured at
three minutes, once, while the Boss ran tools.

```
                      USER  (voice / typing)
                        │
                        ▼
              REAL CLAUDE CODE BOSS SESSION           cmux workspace
                        │ MCP stdio
                        ▼
                     boss-mcp                         ours; on uv's runtime
                        │ authenticated Unix socket
                        ▼
                     BossBridge  ──►  Conductor       canonical truth
                                        │
                              workers A, B, C (children)
```

## Giving a real Claude Code session our tools

Claude Code reaches external tools through MCP, so its orchestration
tools are **our own MCP server**, `boss-mcp` - not a third-party one,
and not something the user installs.

| piece | file | owns |
|---|---|---|
| definitions | `conductor/boss_tools.py` | the tool names, schemas, descriptions; `PROTOCOL_VERSION`; `REQUIRED_TOOLS`; the orientation text. Imports nothing. The SDK backend, boss-mcp and the startup gate all read this. |
| server | `conductor/boss_mcp.py` | the `boss` MCP server (`mcp__boss__*`). Stateless adapter: every call forwards over the bridge. Says hello on start; exits if refused. |
| helper | `conductor/boss_helper.py` | `~/.voice-conductor/boss/bin/boss-mcp`, a generated launcher that re-enters **uv's managed Python** with `mcp` pinned and our code on the path - never the system Python. `verify_helper` runs `--version`; `python -m conductor.boss_helper --check` is what CI runs. |
| bridge | `conductor/boss_bridge.py` | a Unix socket in `conduct.py` bound to **one credential and one Boss session id**; refuses anything else; executes each call and reports start/finish with one execution id. |
| execution | `conductor/pty_manager.py` | the startup gates, launching/resuming, typing turns in, reading answers out, writing every event down. |
| record | `conductor/boss_session.py` | `BossSession`, the ordered timeline, conversation ↔ Boss binding, readable rendering. |

The MCP SDK is a **build dependency** (pinned `mcp>=2,<3` in the
launcher); the user never installs it, never runs `pip`, never edits
`~/.claude.json`. The config is `~/.voice-conductor/boss/mcp.json`, named
on the Boss's own command line with `--strict-mcp-config`, so an
unrelated Claude Code session the user opens never sees `spawn_subagent`.

## Identity and the handshake

When a Boss session is (re)started the Conductor mints a credential,
binds the bridge to it and the Boss session id, and puts both in
boss-mcp's environment via the config. **Every message on the socket
carries them; the wrong pair is refused before anything runs.** The model
supplies tool arguments only - never an identity.

boss-mcp's first act is a hello: protocol version and the tools it
registered. A helper on another protocol is refused ("Boss tooling
incompatible") and exits; Claude Code then reports a failed MCP server
rather than the Boss running with half its tools.

## Starting a Boss is a sequence of refusals

```
helper present, executable, `--version` == PROTOCOL_VERSION      else: not launched
credential minted; bridge bound to (boss id, credential)
session launched with a config naming that helper and that credential
boss-mcp said hello, within MCP_CONNECT_TIMEOUT_S                  else: status failed
hello's tools ⊇ REQUIRED_TOOLS                                     else: status failed
ONLY THEN: status ready
```

Every refusal raises `BossUnavailable("Boss orchestration tooling is
unavailable: …")` and is written to the timeline. A Boss that cannot call
`create_task` is not marked ready on hope.

## The timeline

One JSONL per Boss under `~/.voice-conductor/boss/sessions/<id>/`, one
monotonic sequence, reconstructable with no window alive:

```
YOU
  Fix login in Posely
✓ Find project
  "Posely"
  ~/Developer/posely
◉ Worker started
  Posely · Fix login    [Open sub_task_a]
BOSS
  I've started a worker on the login fix.
◉ Worker update
  Posely · Fix login
  12 tests passed
```

A tool is **one item** from start to finish (the finish rewrites the
start's line), carrying the bridge's `execution_id` - so these are the
actual MCP invocations, never previews composed afterwards. Worker
actions carry the child's stable `subagent_id`; `[Open …]` resolves by
identity, never by title. No raw JSON; no chain-of-thought recorded.

## Parent and child

Every worker the Boss starts gets `parent_boss_session_id` on its
canonical `SubagentState`; the Boss gets the child's id in
`child_subagent_ids`. Persisted both ways, never inferred from timing,
project, or a window. `GlobalConductor.children_of` reads the tree from
the children's own field.

## Conversations, restart, new chat, a dead helper

- `boss/conversations.json` binds each voice conversation to its Boss
  and names the current one. `current_boss_session_id` reads that
  binding - never a window, never the last model request.
- The provider session id is captured **the moment the session opens**.
  A restart resumes it: same Boss id, same window, same history.
- `--new-chat` starts a new conversation. Closing the window is not that.
- boss-mcp is disposable. If it dies, Claude Code restarts it, it says
  hello again with the same credential, and nothing about the Boss, its
  timeline or its workers has moved.

## What the Boss is told

`~/.voice-conductor/boss/CLAUDE.md` = the manager prompt + an
orientation: that it is the user's Boss supervising workers, that its
tools are `mcp__boss__*`, that every worker it starts is a persistent
child session, that it must never invent an id, and that everything it
does here is recorded. It has no file, shell or web tools on purpose.
The capability snapshot for the run is written there too.

**What the user says goes in as their words, nothing added.** The
invisible Boss got a bundle in every user message - clock, capability
snapshot, task registry, then "User says: ..." - where nobody saw it.
In a window that bundle is the transcript the user reads, so it is
gone: capabilities are in CLAUDE.md, worker state is pushed as it
happens (next section) and available on demand through `situation`,
`list_open_sessions` and `inspect_task`, and the date is in Claude
Code's own prompt. Line breaks in an utterance become spaces, because
a terminal input submits on Enter; the words are not touched.

**And every utterance gets there.** The voice front end (GPT Live) is
ears and mouth only: the client takes every completed utterance off
the transcript and runs it through the Boss directly, greetings
included - no delegation step, no decision on the voice side. Measured
before this: with nothing running, "Let's start a new voice agent" was
answered in four seconds with "Sure. What would you like that agent to
do?" and never reached the Boss.

## Worker turns reach the Boss as they happen

The Boss used to learn what its workers did only when the user next
spoke, as a digest in that turn's context. Now every meaningful turn of
a worker the Boss created is typed into the Boss's session by the
Conductor the moment the deterministic pipeline decides it is
meaningful (a finished turn, a failure, an approval or a question, an
unexpected stop):

    Worker update · Fix login (task_1a2b) finished a turn: tests pass

Rules, each with a test in `tests/test_boss_updates.py`:

- every visible worker's, labelled: `Your worker · …` for one this Boss
  started (`child_subagent_ids`), `Worker (started before this chat) ·
  …` for one an earlier Boss did. It used to be children only;
  measured, eleven Boss sessions in a day meant the workers of the
  previous ten were never mentioned to the current one;
- as it happens, whatever the Boss is doing: mid-turn, Claude Code keeps
  the line in its input box and reads it when it looks up, like the
  user's own next words. Which line a turn end answers is matched by the
  words of the user line in the transcript, so an update cannot be taken
  for the user's utterance (updates used to wait for every open voice
  turn to end; measured, three finishes held three minutes behind one
  hung turn). One push is out at a time; more queue and follow it as
  one line;
- the Boss's reply to an update is recorded as `boss_message` with
  source `worker_update`, and the push itself as a `system_event`, so
  the timeline shows what it was told and what it made of it;
- `push_updates = False` switches it off.

## The toast under a turn

When the Boss says "I've asked the PR 81 worker to re-run the tests",
that worker is one click away, on the line under the answer:

```
⏺ The PR 81 worker is re-running the tests now.
  ⎿  Stop says: ~ What does PR 81 do - working · http://127.0.0.1:8977/s/task_c31a8fe6
```

Claude Code renders exactly one thing under a finished turn that the
model never sees: a Stop hook's `systemMessage`. That is the whole
mechanism, and its limits shape the toast (measured on 2.1.251):

- the hook's plain stdout is discarded - stdout must be one JSON
  object or nothing is drawn at all;
- the text is plain. OSC 8 hyperlinks and colour are stripped, so the
  link is a bare URL and the terminal linkifies it (Ghostty, which cmux
  runs, does);
- the payload carries `last_assistant_message`, so the hook reads the
  answer without parsing a transcript;
- the hook runs as its own process with no access to the conductor, so
  the conductor leaves it `boss/turn_sessions.json` - one row per live
  session, rewritten from the same cards the overlay draws.

The hook is installed with `--settings` on the Boss's own command line
(`conduct.toast_hook`), so no other Claude Code session on the machine
grows a hook and the user's settings files are untouched.

Clicking a link reaches `conductor/jump.py`, a loopback server whose
only power is to bring one existing window forward - the same thing
`focus_task` does. Its port is remembered in `boss/jump.port`, so a
toast printed before a restart still points somewhere.

This is the Conductor's canonical pipeline feeding the Boss - the
worker card updated deterministically whether or not the Boss says
anything about it, and the same event reaches the Boss's own window.

## What is not done, and said so

- **Packaging into a signed .app.** There is no bundle yet; the helper
  is a generated launcher onto uv's managed runtime, which satisfies
  "never the user's system Python" today. When a bundle exists, the
  launcher body becomes `exec <bundle>/Contents/Resources/bin/…`; nothing
  that calls it changes. Signing/notarization and a universal build are
  build-system work for that day.
- **Claude Code itself** is detected, not bundled: `conduct.sh` refuses
  to start without it.
- **Observing Claude Code's `tools/list`** from inside the server would
  be one step stronger than the hello; the hello proves the server is
  up with the tools registered, which is what the gate checks.
- The window is Claude Code's own TUI - readable, but a terminal. A
  cmux pane tailing `render_timeline` is the natural next renderer.
- Session-routing policy is untouched.
