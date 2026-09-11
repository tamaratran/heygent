# Driving Codex through its app-server

A CLI in a PTY owns its pixels. We start `claude`, it draws what it
likes, and we scrape the screen and tail a transcript. Everything the
Boss's window looks like is Claude Code's decision, and the only thing
we can put under a finished turn is a Stop hook's `systemMessage` -
plain text, escape sequences stripped (docs/boss-session.md).

`codex app-server` inverts that. It is JSON-RPC over stdio with a
published protocol - 98 client methods, 79 server notifications, 10
server requests - and it renders nothing at all:

```sh
codex app-server generate-json-schema --out ./schema   # the whole contract
codex app-server generate-ts --out ./ts
```

So whoever speaks it draws the conversation. That is the point of this
spike.

## What is here

`conductor/codex_app.py` is the transport and nothing else:

```
thread     a conversation, started or resumed by id
turn       one exchange; streams items while it runs
item       a message, a command, a file change, a tool call
approval   a server REQUEST - Codex asking, and waiting for our answer
```

```python
app = CodexApp(cwd="/repo", on_approval=draw_a_card)
await app.start()
await app.start_thread(approval_policy="untrusted",
                       sandbox={"type": "readOnly"})
async for event in app.turn("what changed in PR 81?"):
    ...   # Event(kind="delta" | "item" | "approval" | "turn_done" | "error")
```

`conductor/app_view.py` is one front end over it, run for the look of
the thing:

```sh
python3 -m conductor.app_view "what year is it?" /tmp
```

```
╭─ you ────────────────────────────────────────────────╮
│ Run the shell command date +%Y and tell me the year. │
╰──────────────────────────────────────────────────────╯
╭─ codex needs permission ─────────────────────────────╮
│ /bin/bash -lc 'date +%Y'                             │
│ [ Accept ] [ Accept for session ] [ Decline ]        │
╰──────────────────────────────────────────────────────╯
   ✓ accept
╭─ codex · gpt-5 ──────────────────────────────────────╮
│ 2026                                                 │
│ ⚙ /bin/bash -lc 'date +%Y'                           │
│ 33.2s                                                │
╰──────────────────────────────────────────────────────╯
   ↳ ● Codex smoke test — waiting on you   open
```

Three things there are not possible in the Boss's window today:

- **turns are cards.** One box per exchange, the tools it ran listed
  under the answer, instead of a scroll of interleaved output.
- **the toast is linked.** `open` is an OSC 8 hyperlink to the worker -
  the same mention rule as the Claude Code toast
  (`conductor/turn_toast.py`), drawn the way that one cannot be, because
  a `systemMessage` is stripped of escape sequences.
- **the buttons are real.** Codex asks for permission with a server
  *request* and blocks until it is answered. A terminal can only
  linkify a URL, so `[ Accept ]` is an OSC 8 link onto the loopback
  server (`conductor/jump.py`, which now takes routes); pressing it
  settles the request Codex is waiting on. Measured: click -> 200 ->
  the command ran -> the turn finished.

## What it is not

Not the Boss. The Boss is a Claude Code session in a PTY
(`conductor/pty_manager.py`) and this changes nothing about it - no
runtime, backend or launch path is touched. Making Codex the Boss
needs its own work, none of it started here: the conductor's MCP tools
re-plumbed into Codex's config, approvals routed to the supervisor
inbox, the BossSession timeline fed from app-server events instead of a
transcript, and a decision about where the front end runs.

## Gotchas found on the way

- Wrapping a rendered line by character count cuts an OSC 8 link in
  half and the terminal prints the URL: the button row came out as
  `acceptForSession[ Accept for session ]` across four lines. Lines
  that carry escape sequences are never wrapped, and width is measured
  with the sequences skipped (`_len`).
- A server request nobody answers blocks the turn for ever, so anything
  unimplemented is refused immediately with a JSON-RPC error, and an
  approval handler that raises declines rather than hangs.
- The default thread approval policy does not ask. A front end that
  draws buttons wants `approval_policy="untrusted"`.

## Testing it without an account

`tests/fakes/` is a stand-in app-server speaking the same wire, so the
suite runs offline. Its shapes come from the generated schema and from
a live 0.151 session on 2026-08-30.
