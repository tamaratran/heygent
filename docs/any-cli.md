# One product, any CLI

cmux holds any terminal. The Conductor should drive any coding CLI in
one - Claude Code, Codex, Gemini, Cursor, whatever ships next - with
the same cards, the same Boss, the same notifications. This says what
is already provider-agnostic, what is not, and the seam that makes the
rest so.

## What is already agnostic (keep)

- **Hosting.** A worker is a workspace in cmux (or a tmux session)
  that we create, type into, read the screen of, focus and close. Six
  verbs; nothing in them is Claude's.
- **Canonical state.** `SubagentState`, the reducer, the card
  projection, the inbox, the timeline, dismissal, epochs: all consume
  `AgentEvent`s (started / progress / completed / failed /
  approval_required / needs_input) and never a provider's format.
- **The Boss and its tools.** `create_task` already carries a
  `provider`; `capabilities` already reports which providers are on
  PATH (`claude`, `codex`); `Task.provider` is persisted. The Boss
  stays Claude Code - it needs our MCP tools - but what it starts can
  be anything.
- **The runtime contract.** `CodingAgentRuntime` is abstract:
  `create_session`, `send`, `interrupt`, `resume`, `get_status`,
  `subscribe`, `executions`, `reconcile_session`. `RoutingRuntime`
  already routes a task between runtimes - by *location* (local /
  cloud). Routing by *provider* is the same switch.

## What is Claude-specific today (29 places in `tmux_runtime.py`)

All of it is "how do I know what this CLI is doing", and it lives
inside the PTY runtime:

| Concern | Claude Code answer today |
|---|---|
| launch | `claude --permission-mode auto <prompt>`; `--session-id`, `--resume`, `--name` |
| the input box is ready | screen matches `shift+tab to cycle` / `for shortcuts` / `⏵⏵` |
| what it is doing / a turn ended | its own transcript: `~/.claude/projects/<munged cwd>/<id>.jsonl`, `normalize_entry` reads user / assistant / tool_use / `stop_reason` |
| which session is mine | newest transcript in the project dir, or the pinned id |
| it is asking permission | screen contains `do you want to proceed` / `❯ 1. yes` |
| answer the permission | arrow keys + Enter in that pane |
| the process is alive | `pgrep -f <session id>` |
| peers / native messaging | `~/.claude/sessions` registry, `SendMessage` |

None of that is wrong; it is just unlabeled. Every row is one method
of an adapter.

## The seam: `CliAdapter`

```
PtyHost (cmux | tmux)           one per surface, provider-free
   create · send · read_screen · alive · focus · close

CliAdapter (one per CLI)        what the host is hosting
   name, available()
   launch_argv(cwd, prompt, session_id=None, resume=None, permission)
   prompt_ready(screen) -> bool
   transcript_for(cwd, session_id) -> Path | None
   normalize(entry, state) -> list[AgentEvent]      # their log -> our events
   approval_prompt(screen) -> str | None
   approve_keys(screen) / deny_keys(screen) -> the send-keys sequences
   answer(host, decision)
   discover_session(cwd, existing) -> session_id
   process_alive(session_id, cwd) -> bool | None

PtyRuntime = PtyHost + CliAdapter   ->  the CodingAgentRuntime we have
```

`TmuxClaudeRuntime` becomes `PtyRuntime(host, ClaudeCodeAdapter())`
with no behaviour change - the existing tests are the proof. Each new
CLI is a new adapter and nothing else.

### The universal fallback: `ScreenAdapter`

A CLI with no transcript on disk still runs in a terminal we can read.
`ScreenAdapter` derives everything from `read-screen --scrollback`:

- **turn end**: the CLI's prompt marker reappears after activity and
  the screen is unchanged for `SETTLE_S`;
- **text**: the scrollback delta since the last turn end, cleaned;
- **approval**: any question shaped like a choice (`_PROMPT_SHAPES`
  already lists the shapes);
- **alive**: the host says so, the process table confirms.

Lower fidelity than a transcript (no tool names, no structured
results), fully universal, and it is what cmux makes possible. Every
real adapter can start as `ScreenAdapter` + a prompt regex, and
graduate to a transcript parser when one is measured.

## Per CLI, as measured on this machine (2026-08-30)

| CLI | State here | Launch | Resume | Own transcript | Approval |
|---|---|---|---|---|---|
| **Claude Code** 2.1.250 | done | `--permission-mode`, `--session-id` | `--resume <id>` (cwd-scoped) | `~/.claude/projects/…/<id>.jsonl` | TUI prompt, arrows + Enter |
| **Codex CLI** | installed but **broken**: `spawn …/codex-darwin-arm64/…/codex ENOENT` (the arm64 vendor binary is missing from the npm install) | `codex [-a never \| --full-auto] <prompt>`; headless `codex exec --json` | `codex resume <id>` | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (exists here) | TUI y/n |
| **Gemini CLI** 0.35.2 | works | `gemini --approval-mode yolo\|auto_edit\|default`; headless `-p … -o stream-json` | `--resume <n\|latest>` (by index, not id) | to measure | TUI |
| **Cursor** (`cursor-agent`) | not installed | `--print --output-format stream-json` per docs | `--resume` per docs | to measure | to measure |

Two of the four (Codex, Cursor) cannot be measured until installed or
repaired; nothing about them should be reported as known until then.

## What stays true

- One worker, one execution, one visible surface - the adapter never
  hides a process behind a fake window.
- Cards come from canonical state, so a `ScreenAdapter` worker gets
  the same card as a Claude one, with less detail in the body.
- The Boss talks to any worker through `send_to_task` (PTY). Native
  `SendMessage` is a Claude-to-Claude extra, never the only path.
- `provider` is a first-class field on the task, chosen by the user
  ("start a Codex on this") or by capability, never inferred from a
  window.

## Phases

1. **Extract the adapter** from `TmuxClaudeRuntime` (`ClaudeCodeAdapter`,
   `PtyRuntime`), zero behaviour change, existing suite green.
2. **`ScreenAdapter` + Gemini**, the one non-Claude CLI that runs here
   today: prompt marker, approval shape, turn end from the screen; a
   Gemini worker on a card, messaged from the Boss.
3. **Codex**: fix the install, then a rollout parser
   (`~/.codex/sessions`) - Codex's transcript is as good as Claude's.
4. **Cursor**, once installed: `stream-json` if its interactive mode
   writes a log; `ScreenAdapter` otherwise.

Each phase ends with one live worker of that CLI in cmux: started by
the Boss, followed on a card, answered to by voice.

## Status, 2026-08-30

| Phase | PR | State | Measured live |
|---|---|---|---|
| 1 — `CliAdapter` extracted, no behaviour change | #99 | done | the existing runtime suite, plus a recording adapter at every seam |
| 2 — `ScreenAdapter` + Gemini; provider routing end to end | #100 | done | Gemini's trust and auth dialogs on 0.35.2; **not** a signed-in turn (Google sign-in is the user's) |
| 3 — Codex through its rollout | #101 | done | a live rollout from codex-cli 0.151.0: found by checkout, turn read off `task_started`/`task_complete`, dialogs answered |
| 4 — Cursor | #102 | done | cursor-agent 2026.08.25, installed and signed in: the arrow prompt, the "Run this command?" approval, a turn end off the screen |

Left to measure, each needing the user at the keyboard: a Gemini turn
after sign-in (its ready-prompt regex), Codex's approval wording (its
sandbox ran every probe command without asking), and everything about
Cursor is measured; what remains there is a transcript to tail, if its per-project state ever holds one.
