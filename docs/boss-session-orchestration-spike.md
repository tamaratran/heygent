# Boss ↔ worker orchestration: what Claude Code gives us, measured

The question this spike had to answer:

> What is the most robust way for our real, persistent Claude Code Boss
> session to create, observe, communicate with and supervise visible
> child agent sessions, while the Conductor stays the canonical,
> cross-provider control plane?

Everything below was measured on this machine, on 2026-08-28, against
**Claude Code 2.1.250** (`~/.local/bin/claude`, native binary). Nothing
is reported that was not observed; where something was not tested it
says so.

**RECOMMEND B: Conductor-hosted MCP owns orchestration; Claude Code's
native cross-session messaging becomes the Boss ↔ Claude-worker message
path, behind our own identity mapping and session-scoped settings.
Agent Teams are not usable as our worker system.** Reasons at the end.

---

## 1. What was built

- `conductor/conductor_mcp.py` — the Conductor answers MCP itself:
  streamable HTTP on `127.0.0.1:<port>/mcp`, served in-process by the
  MCP SDK (2.1.1) + starlette + uvicorn that `claude-agent-sdk` already
  brings into the app's runtime. No helper process, no second runtime.
  A pure-ASGI guard checks a per-Boss bearer credential before the SDK
  sees a byte, and marks the Boss "connected" on its `tools/list`.
  The port is remembered in `<home>/boss/mcp.port` so a Boss launched by
  the previous conductor finds the next one at the same URL.
- `conductor/boss_bridge.py` — the tool-execution core (`BossToolHost`:
  credential, readiness event, execution ids, timeline hooks, ToolCall
  record) is shared by the Unix-socket bridge (boss-mcp, PR 37) and the
  HTTP endpoint. One execution record per call, whichever way it arrived.
- `conductor/pty_manager.py` — `transport="http" | "stdio"`; the http
  gate refuses to launch a Boss if the endpoint is not listening;
  `session_settings` (`--settings <json>`, session-scoped, never the
  user's files); `debug_file` for diagnostics.
- `conduct.py --boss-transport http|stdio` (default still `stdio`; see
  "What to productionize").
- `conductor/cmux_runtime.py` — two launch-race fixes found by running
  the Boss six times in an afternoon (§11): the launch line is typed
  with its Enter in one call, the prompt must be stable before typing,
  and the workspace lookup after `new-workspace` retries.
- `conductor/pty_manager.py` — the liveness check that relaunched the
  Boss on every turn (§11), fixed. And worker turns pushed into the
  Boss's session as they happen (`deliver_supervisory`; see
  `docs/boss-session.md`, "Worker turns reach the Boss").
- The user's words go into the Boss's window unwrapped. The per-turn
  bundle (clock, capabilities, registry, "User says:") that the SDK Boss
  received invisibly is not typed into a window the user reads;
  capabilities moved to CLAUDE.md, state is pushed and on demand.
- Parity with the SDK Boss, checked and kept: the same 22 tools and
  nothing else (the HTTP endpoint is given `_manager_tools(conductor)`,
  not the whole schema table); the same builtins refused, now including
  `Agent`, which is what 2.1.250 calls the subagent tool the old list
  knew as `Task`; manager.md as the session's instructions; the per-turn
  context; the persisted session id; turn serialization; verbatim
  `send_to_task` through the same `handle_action`.
- Timeline: tools render under the product namespace, `Agent Control ·
  Find project`, never a server name, socket or URL.
- Tests: `tests/test_conductor_mcp.py` (11) — the SDK's own HTTP client
  against the endpoint: listed, called, refused without/with a wrong or
  retired credential, error text reaches the model, port reuse, the
  endpoint going away and coming back under the same client, the launch
  gate, the liveness check. Suite: 908 passed, 44 skipped, 2 failed — both
  `test_panel_geometry`, which needs AppKit the test runtime lacks
  (pre-existing, unrelated).

Nothing here touches `~/.claude.json`, `~/.claude/settings.json` or any
`.mcp.json`. The Boss's config is one private file named on its command
line with `--strict-mcp-config`.

## 2. Installed Claude Code: what exists

Verified in `claude --help` and the binary's strings:

| Feature | Present in 2.1.250 | How |
|---|---|---|
| HTTP MCP (`"type": "http"` in `--mcp-config`) | yes | `claude mcp add --transport http` documented; "streamable-http" in binary |
| `--mcp-config`, `--strict-mcp-config` | yes | |
| `--session-id`, `--resume`, `--fork-session` | yes | |
| `--name <display name>` | yes | sets the session's registry name (`nameSource: "user"`) |
| `--settings <json\|file>` | yes | session-scoped settings |
| `--debug-file <path>` | yes | client-side MCP log |
| Cross-session messaging (`ListAgents`, `SendMessage`, `notify_when_idle`) | yes | registry at `~/.claude/sessions/<pid>.json`, sockets at `/tmp/cc-socks/<pid>.sock`, `peerProtocol: 1` |
| Agent Teams | yes, experimental | `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, `--teammate-mode tmux\|iterm2\|in-process\|auto`, state in `~/.claude/teams/<team>/config.json`, `~/.claude/tasks/<team>/` |
| `crossSessionInbound` setting | yes | `accept` / `hold` (default `hold` for mismatched classes); a repo setting can only tighten; managed settings win |

## 3. Identity: what the registry exposes

`~/.claude/sessions/<pid>.json`, one per live interactive/background
process (plus a `<pid>.<hash>.key` secret):

```json
{"pid":16395,"sessionId":"2ae0106d-…","cwd":"/Users/…/.voice-conductor/boss",
 "version":"2.1.250","peerProtocol":1,
 "peerFeatures":["notify_idle","reply_across_default_dirs","artifact_yield"],
 "kind":"interactive","messagingSocketPath":"/tmp/cc-socks/16395.sock",
 "name":"boss-b3","nameSource":"derived","status":"idle"}
```

What a model sees in `ListAgents`: `name [ref]  ·  interactive  ·  idle
·  tmux cond_task_9cfbd908:@4.%4  ·  started 1d ago` (the tmux location
is shown for tmux-hosted sessions; cmux-hosted ones show none).

Measured about identity:

- **Every Conductor-launched worker is already in the registry**, named
  from its cwd: `task-9cfbd908-a8`, `task-7f624cac-45`, … — the task
  directory's basename plus two hex characters.
- **Derived names are not stable across restarts.** The same Boss
  session `2ae0106d` was `boss-b3` in one process and `boss-35` in the
  next. Two processes of session `653ca900` were `typed-work-notification-d5`
  and `voice-agent-28`.
- **`--name` is honoured and stable**: `claude --name probe-xyz-name`
  registered exactly that (`nameSource: "user"`). So a worker launched
  `--name task_<id>` is addressable as its ManagedSubagent id, and the
  Boss as `boss`.
- The stable keys we control are `sessionId` (our providerSessionId — the
  transcript name) and `cwd` (our task directory). `pid` and the
  6-hex `[ref]` change per process. The receiver verifies a sender by
  pid + socket: `from uds:/tmp/cc-socks/79526.sock [verified pid 79526]
  (peer claims name: …)`.
- Duplicate names: the tool says "a newer agent took the name (latest
  wins)"; disambiguate with `name [ref]` from a listing.

Mapping rule for the product, therefore: **name = our id (`--name`),
truth = `sessionId` + `cwd` in the registry file**, never the display
name alone.

## 4. Cross-session messaging: what actually happens

Sender: this Claude Code session (kind `bg`, bypass class). Receivers:
a Conductor-launched worker in auto mode; a disposable auto-mode worker
launched with `--settings '{"crossSessionInbound":"accept"}'`.

| Step | Result |
|---|---|
| `SendMessage` → Conductor worker `task-24d11ba4-3c` (auto mode, default settings) | **Held, not delivered.** The worker's window shows: *"Held peer message — from uds:/tmp/cc-socks/79526.sock [verified pid 79526] … not delivered to Claude (1 held). The sending session's permission mode class doesn't match this session's. Review it below, or set "crossSessionInbound" to "accept"."* A human must approve it in that window. The sender later received two notices: *"held for the recipient user's approval … Do not wait for a reply"*, then *"not approved before expiry … Not delivered"* — a held message expires; it is not queued indefinitely. |
| `notify_when_idle: true` on the same send | Subscription accepted: *"A process claiming the address uds:… asked to be told when this session is next idle — it will get one automated status notice"*. No notice reached this (bypass-class) session; the tool result had said it would go to the user instead in that case. |
| `SendMessage` → `probe-worker-a` (auto mode, `crossSessionInbound: accept`) | **Delivered immediately**; the worker acted on it within seconds, with no PTY input from us. |
| Worker replies with `SendMessage` to the `from` address | Sent by the worker (*"Allowed by auto mode classifier"*) — and **held on the receiving (bypass) side**: *"Cross-session message held for approval (recipient: uds:…). The recipient's session has different permission-mode settings, so their user must approve it."* |
| Message arrives as | `<cross-session-message from="…">` in the receiver's transcript, i.e. observable by our transcript watcher. |

So native messaging works, without any PTY injection, **exactly when
both ends are in the same permission class or opted in with
`crossSessionInbound: accept`** — a setting we can pass per session on
the command line for every session we launch (Boss and Claude workers),
without touching the user's files. A Boss in `bypassPermissions` talking
to workers in `auto` needs it on both sides. The gate is per-session
policy, not a bug; it is Claude Code refusing "cross-session permission
laundering".

Boss-driven run (the real Boss, HTTP tools, `--settings accept`):
see §8.

## 5. Conductor-hosted HTTP MCP: what actually happens

Probe (`claude -p` with a session-scoped config, `--debug-file`):

```
MCP server "boss": Initializing HTTP transport to http://127.0.0.1:60202/mcp
MCP server "boss": HTTP transport options: {"url":…,"headers":{"User-Agent":"claude-code/2.1.250 (sdk-cli)","Accept-Encoding":"identity","Authorization":"[REDACTED]"…
MCP server "boss": Successfully connected (transport: http) in 187ms
MCP server "boss": Connection established with capabilities: {"hasTools":true,…}
ToolSearchTool: selected mcp__boss__find_project
MCP server "boss": Tool 'find_project' completed successfully in 32ms
MCP server "boss": HTTP connection closed after 11s (cleanly)
MCP server "boss": Cleared connection cache for reconnection
```

Requests the endpoint saw, in order: `server/discover`,
`subscriptions/listen`, `prompts/list`, `resources/list`, `tools/list`,
`tools/call`. **No `initialize`.** Claude Code 2.1.250's MCP runtime
("mcp runtime arm: v2") does not send the classic handshake; the first
readiness gate, keyed on `initialize`, never fired and the Boss was
refused as toolless. The gate now keys on `tools/list`, which both the
SDK client and Claude Code send. Worth remembering: Claude Code's MCP
client is not the reference client, and a gate must be measured against
it.

Live Boss (real conductor, real cmux window, session `2ae0106d`
resumed): connected, `Agent Control · List open sessions` on the
timeline with its result, answer "28." in 66 s. The credential rides in
the `Authorization` header; a request without it is answered 401 before
the SDK sees it (tested with the SDK client: no token, wrong token,
retired token).

Reconnect after the conductor goes away and comes back on the same
port: see §7.

## 6. Agent Teams: what actually happens

Disposable lead in tmux, `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
claude --teammate-mode in-process --permission-mode auto --name
teams-lead-probe`, asked to spawn one teammate `scout` to read a file and
message the line back.

Observed:

- The lead spawned it with its `Agent` tool (`Agent(Scout reads README
  first line)`), footer showing `⏺ main / ◯ scout … ↓ 34.7k tokens`.
- Team state: `~/.claude/teams/session-02bc8e88/config.json` with
  `leadAgentId: team-lead@session-02bc8e88`, members `team-lead`
  (`tmuxPaneId: "leader"`) and `scout@session-02bc8e88` (`agentType:
  general-purpose`, `backendType: in-process`, `tmuxPaneId: "in-process"`).
  `~/.claude/tasks/session-02bc8e88/` empty.
- The teammate's transcript: `~/.claude/projects/<lead cwd>/<lead
  session>/subagents/agent-ascout-ffc1b77e6c26017f.jsonl` + `.meta.json`
  (`taskKind: in_process_teammate`, `permissionMode: auto` inherited,
  `model`, `teamName`).
- **The teammate is not a session.** It has no pid, no registry entry,
  no messaging socket, no PTY: `ListAgents` from the lead does not list
  it ("only peer sessions are shown, no in-process subagents"). There is
  nothing to put in a cmux workspace and nothing the user can type into.
- Teammate → lead: `SendMessage to team-lead` delivered; the idle lead
  woke and reported the line. Its `SendMessage` was evaluated by the
  auto-mode classifier in the lead (`auto-mode-classifier-error.txt`,
  313 lines, "Request was aborted") — it went through anyway.
- **Lead `/exit`: the team directory was deleted.** Resume the lead
  (`--resume 02bc8e88…`, same session id in the registry): a **new,
  empty team** `session-4271b48f` was created — team names are
  per-process, not per-session. Asked to message scout: *"Not sent — no
  agent named 'scout' is reachable"*, then *"to must be a bare teammate
  name — there is only one team per session"*. The lead then **did the
  work itself** with Bash — the exact behaviour a Boss must never have.
- Confirmed constraints: one team per lead; teammate mode `tmux`/`iterm2`
  spawn panes in Claude's own layout (not tested — it would need a tmux
  server or iTerm2 and would not land in cmux either way).

Against the product invariants: no stable session identity, not
visible/interactable as a session, not surface-addressable, not
recoverable across our restart or the Boss's, Codex impossible. Agent
Teams cannot be the worker system. Their coordination semantics (a
teammate messaging its lead, the lead waking on it) are exactly what
native cross-session messaging gives us between real sessions, which
we already have.

## 7. Conductor goes away and comes back (HTTP)

Run: real conductor, Boss in tmux (see §11 for why not cmux that hour),
stateless endpoint on the remembered port 58243. Turn 1; then the
endpoint was stopped (uvicorn down, socket closed), 3 s pause, a new
`ConductorMcp` started on the same port expecting the same credential;
turn 2.

```
runtime.session_created                       (once, for the whole run)
turn 1 took 11.7s   tools called: ['list_open_sessions']   reply: 28.
requests seen: server/discover, subscriptions/listen, prompts/list,
               resources/list, tools/list, tools/call
---- bouncing the endpoint ----
before turn 2: runtime status of the Boss = 'idle'
turn 2 took 114.9s  tools called: ['list_projects']        reply: 2.
requests seen (new endpoint): tools/call
```

**Same Boss process, same session, same window; after the conductor
came back the client did not even rediscover the server — it POSTed the
next `tools/call` to the same URL with the same header and the stateless
endpoint answered.** (Turn 2's 114 s is the tmux paste race waiting for
an Enter, §11; the Boss's own work was 21 s.) In an earlier run, before
the liveness bug below was fixed, the old process was seen re-running
`server/discover` + `subscriptions/listen` against the new endpoint on
its own, and a process holding a retired credential was refused with
401 — so a conductor that mints a new credential per start would force
a relaunch; the fix is to persist the credential with the port, which
the stdio path (PR 37) already needs too.

A stateful endpoint (`Mcp-Session-Id`) was not exercised live; the SDK
default would hand the client a session id the next conductor process
does not know. Stateless is the right setting for us: no server-initiated
traffic is needed.

## 8. The Boss talks to a worker natively

Run: the real Boss (HTTP tools, relaunched with `--settings
'{"crossSessionInbound":"accept"}'` — visible on its command line, in
no settings file), one typed turn: find the local session
`probe-worker-a` with `ListAgents`, message it with `SendMessage` and
`notify_when_idle`, report, do not wait. The worker: auto mode, same
session-scoped setting.

The Boss's answer (18 s, no Conductor tool involved):

> ListAgents showed probe-worker-a as one interactive local session,
> reference 7b920c, idle, in tmux window probe_worker at pane 385,
> started about four hours ago. The SendMessage result reported success:
> the message was delivered to probe-worker-a … and it confirmed a
> one-shot subscription …

Then, in the Boss's window, as new turns of the same session, with no
one typing:

```
› Message from @probe-worker-a: PONG from probe-worker-a.
⏺ The reply came back: probe-worker-a answered with exactly "PONG from
  probe-worker-a." …
⏺ probe-worker-a is idle — finished a turn at 10:07 · «Sent `PONG from
  probe-worker-a.` to boss-09 as requested. Nothing else needed …»
⏺ And the idle notice just landed too … message delivered, reply
  received, session idle.
```

Boss → worker delivered, worker → Boss delivered, idle notice delivered
with the worker's own one-line status — **all three without a byte
typed into a PTY**, both sessions opted in for this session only. The
worker addressed the Boss as `boss-09`: the Boss's fifth derived name of
the day for one session id (§3), which is why the Boss must be launched
`--name boss`.

Two things the product must add before this is the recorded path: the
Boss's `SendMessage` is not a Conductor tool call, so the timeline does
not show it yet (it shows the typed turn and the answer); and the reply
and idle notice arrived as turns the Conductor did not initiate — they
are in the transcript our watcher reads, so recording them is
observation, not new plumbing.

## 9. Restart and recovery matrix

| Case | What survives | Measured |
|---|---|---|
| Conductor restarts, Boss kept (HTTP) | Boss process, its window, its session id, our timeline; the endpoint comes back on the remembered port with the same credential | §7 |
| Conductor restarts, Boss kept (stdio, PR 37) | Same, boss-mcp reconnects to the new socket on its next call | PR 37 |
| Boss process restarts | Resumed by session id in the same cmux workspace; children stay children (`parent_boss_session_id` is ours, on disk) | PR 37 + this run: `Boss session resumed (2ae0106d)` |
| Worker restarts | New pid, new registry entry; derived name changes; `--name` keeps it; our task dir and session id do not change | §3 |
| Agent Teams lead restarts | Teammate gone, team gone, new empty team | §6 |
| cmux closed / reopened | Not re-measured here; PR 37's `needs_repair`/`repair` path | — |

## 10. Permissions

- Boss: `bypassPermissions` (it has only our tools). Workers: `auto`.
  These are different classes for cross-session messaging → `hold`
  unless `crossSessionInbound: accept` on the receiving side.
- Agent Teams teammate inherits the lead's `permissionMode` (`auto`
  in `meta.json`); its tool calls are classified in the lead process;
  prompts would appear in the lead's TUI (not observed: auto mode
  approved everything in this run).
- Nothing was widened globally to make anything work: `accept` was
  passed with `--settings` to the one disposable session that needed it.

## 11. What failed, what was flaky

- **Every turn was killing and resuming the Boss.** `PtyManagerBackend
  ._alive` read `.status` off the runtime's answer; the runtimes answer
  with a plain string (`"idle"`), so the check was always false and each
  `_ensure_session` relaunched the session (same id, same window name,
  new process — no duplicate, but a recycled window and a fresh MCP
  connection per turn). PR 37's one-turn smoke could not show it; the
  test fake returned an object with `.status`, so the tests could not
  either. Fixed, regression test added, the fake made honest, and
  re-measured: two turns, one process (§7).
- First live HTTP run: Boss refused as toolless — the `initialize` gate
  (above). Fixed and re-measured.
- The MCP SDK masks tool exceptions as "Error executing tool X" unless
  they are `ToolError`; the endpoint now raises `ToolError` so the
  Conductor's message ("no such project") reaches the Boss.
- Agent Teams: the lead needed a second Enter to submit a long pasted
  prompt (tmux paste); the "Teach auto mode about your environment?"
  dialog interrupted the first turn.
- Two of five live Boss launches in cmux stalled before Claude started:
  the workspace showed the launch command typed at the shell prompt,
  Enter lost. The launch typed the line with `send` and then pressed
  Enter with a separate `send-key`; the longer the line (the Boss's
  grew by a `--debug-file`), the more often Enter arrived first. Fixed:
  the line is sent with its newline in one call, which cmux types in
  order (it presses Enter for a newline - measured in PR 37). It is the
  kind of PTY seam native messaging lets the Boss ↔ worker path avoid;
  launching still goes through it. A second, related race: under load
  cmux acknowledged `new-workspace` before its listing showed the
  workspace; the runtime reported "cmux made no workspace called
  'cond_boss'" while the workspace appeared a moment later, empty. The
  lookup now retries for a few seconds.
- cmux itself was intermittently slow during the afternoon runs (one
  `read-screen` took longer than 60 s). Every stall above happened in
  that window; the morning runs launched cleanly first time.
- tmux has its own version: a long typed turn arrives in Claude Code as
  a bracketed paste (`[Pasted text #2]`) and the Enter sent right behind
  it is sometimes consumed by the paste rather than submitting it — once
  in three tmux turns here, and the Agent Teams lead showed the same.
  The turn then waits until someone presses Enter. This is the PTY seam
  in its purest form; `send_to_task` over a PTY inherits it, native
  `SendMessage` does not.
- The Boss's `list_open_sessions` result is cut at a few thousand
  characters; the Boss retried it four times in one turn. A paging or
  count-first result would save a minute per status question (not in
  scope here).

## 12. Recommendation

**RECOMMEND B — Conductor MCP + native Claude cross-session messaging.**

- **Control plane: Conductor-hosted HTTP MCP** (`conductor_mcp.py`).
  It removes the packaged helper, its uv launcher, its socket and its
  lifecycle; the conductor is already running; the SDK is already in the
  runtime; the credential is a header; the endpoint survives our restart
  on the same port. It satisfies every "zero install, zero config, no
  localhost in the UI" requirement.
- **Claude ↔ Claude messages: native `SendMessage`**, for Claude workers
  only, once (a) each worker is launched `--name task_<id>` and (b) both
  Boss and workers get `--settings '{"crossSessionInbound":"accept"}'`.
  It delivers without PTY typing, queues at the receiver, and wakes an
  idle session — the three things `send_to_task`'s PTY path is weakest
  at (newline-is-Enter, prompt races, text left typed-but-unsent).
  Codex workers keep `send_to_task`.
- **Conductor stays canonical** for everything §7 of the spec lists.
  Native messages are observable in the receiver's transcript
  (`<cross-session-message from=…>`), which our watcher already reads,
  so the timeline can record them; the Boss's `SendMessage` is not a
  Conductor tool call, so it must be recorded from the Boss's transcript
  the same way. Until that observation exists, `send_to_task` remains
  the recorded path.
- **Agent Teams: no.** Not a session, not addressable, not recoverable,
  not Codex. Revisit only if teammates become registry sessions that
  survive their lead.

Why not A (all custom): it keeps the PTY as the only Claude → Claude
channel, which is the least reliable seam we have, when the platform now
offers a queued, verified-sender channel between the very processes we
launch. Why not C: §6. D: nothing better was discovered.

## 13. What to productionize next

1. Flip `--boss-transport` default to `http`; keep `stdio` one release
   as the fallback; then delete `boss_helper.py`/`boss_mcp.py`.
2. Workers: `--name task_<id>`; Boss: `--name boss`.
3. Session-scoped `crossSessionInbound: accept` for Boss and Claude
   workers (both directions are needed; measured).
4. Record native messages: parse `<cross-session-message>` entries and
   the Boss's `SendMessage` tool uses from transcripts into
   `subagent_messaged` / `subagent_event_received` timeline events.
5. Tell the Boss (ORIENTATION) when to use `SendMessage` (a Claude
   worker's `task_<id>` name from `inspect_task`) and when
   `send_to_task` (Codex, or any worker whose delivery must be recorded
   and verified by the Conductor).
6. `notify_when_idle` as an additional completion signal — additive
   only; the deterministic card pipeline stays the source of truth.
