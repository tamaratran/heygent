# What actually crosses between the Boss and its workers

Investigation only — no code changed. Evidence: `~/.voice-conductor/logs/conductor-1176.jsonl`
(tonight's conductor, pid 1176, run `run_433c90112443`), the Boss transcript
`~/.claude/projects/-Users-tamaratran--voice-conductor-boss/8c63bb4c-….jsonl`, and the workers'
own transcripts under `~/.claude/projects/-Users-tamaratran--voice-conductor-workspaces-proj-19d43908-task-*`.
Line numbers are this branch unless marked `master-live`
(`~/Downloads/voice-agent/.claude/worktrees/master-live`, `a4b5ef5`) — the running conductor.

---

## 1. Into a WORKER's PTY

Three different things arrive, and **all three land as if the user typed them.**

### 1a. The creation blob — argv, not typed

`Conductor.create_task` (`conductor/conductor.py:397`) calls
`build_worker_prompt` (`conductor/conductor.py:41`), and the string is passed as the CLI's
positional prompt (`cli_adapter.py:243 ClaudeCodeAdapter.launch_argv`), so it is the session's
first user message. Tonight's four workers got 2,073 / 2,196 / 2,261 / 2,725 characters. Shape:

```
Project context: …                        (only if PROJECT.md exists)
Your workspace: …/proj_19d43908/task_X
Your branch: agent/task_X (already checked out)

Your task: <title>

Goal: User's words: "<verbatim utterance>"

Context a worker starting cold needs: <the Boss's briefing, 3-8 sentences>

<SHARED_MACHINE_RULE — 607 chars, identical in every worker, every time>
<ASK_THEN_WAIT_RULE — 380 chars, master-live conductor.py:86, same>
```

So ~1,000 of every ~2,200 characters is fixed boilerplate repeated per worker, sitting inside the
*user* message. `User's words: "…"` + `Context a worker starting cold needs: …` is a convention the
Boss invents each time (`prompts/manager.md:230-238` asks for "a faithful goal", not this format) —
it is decent structure, but it is prose inside prose with no machine-readable boundary.

### 1b. Follow-ups from `send_to_task` — and the thing that actually breaks

`GlobalConductor.send_to_task` (`conductor/global_conductor.py:1305`) →
`hold_to_users_words` (`:94`) → `Conductor.send_to_task` (`conductor/conductor.py:540`) →
`runtime.send` → typed into the pane.

`hold_to_users_words` discards whatever the Boss wrote unless it is literally a substring of
`self._utterance`, and `self._utterance` is **every currently-open utterance joined together**
(`global_conductor.py:1714 _refresh_utterance`, appended/removed per turn at `:1719-1754`). Voice
turns overlap constantly (`manager.turn_overlapping` fired 4× tonight), so by the time a Boss turn
gets round to calling `send_to_task`, the open utterances are usually **not** the ones that turn was
about.

Measured tonight, six consecutive replacements:

| time (Z) | Boss wrote | worker actually received |
|---|---|---|
| 08:36:51 | "…write the prompt edits you proposed …and open a PR. Don't merge it" | "Also, I'm wondering why does our, like, basically, I think we send a lot of text… **. Just so you know**" |
| 08:36:53 | "…implement the worker-state cleanup you proposed and open a PR…" | *the same sentence again* |
| 08:38:59 | "That messy-messaging question is already owned by task_1b85027f — drop it…" | "**. Just so you know**" |
| 08:39:11 | "Resending the instruction that never arrived…" | "**. Just so you know**" |
| 08:39:20 | "Clear instruction, please act on this one: implement the WORKER-STATE cleanup…" | "**. Just so you know**" |

Confirmed at the receiving end — `task_64260475`'s own transcript, 08:36:52.590Z, user role:

> "Also, I'm wondering why does our, like, basically, I think we send a lot of text. Um, I guess like
> the worker sends text and, like when the boss session and the worker sessions are communicating with
> each other, it's so like weird and messy and it's hard to read, especially when it comes as a message
> from the user side . Just so you know"

`task_14d8ec49` received the identical sentence at 08:36:54.645Z. **That is the duplicated fanout the
user is describing, and it is not fanout by design** — it is one utterance substituted for two
unrelated instructions. `hold_to_users_words`'s docstring owns the trade ("one utterance aimed at two
tasks reaches both in full"), but the trade it is actually making is worse: the utterance it
substitutes is often about *neither* task.

Downstream cost: the Boss's five instructions never arrived, so at 08:40:48 it gave up and created two
new tasks ("PR: fix the voice's prompt", "PR: one honest worker state") to carry instructions two live
workers were already sitting there waiting for. Every rewrite is logged as
`manager.follow_up_rewritten` (severity `warning`) — the log knew, nobody was told.

### 1c. Fanned-out utterances

There is no broadcast path in the code: fanout is the Boss calling `send_to_task` once per task in a
turn (3 calls at 08:37:26, 3 more at 08:40:48). With 1b active, N calls in a turn become **N copies of
the same wrong sentence**.

---

## 2. Into the BOSS's PTY

Two kinds, both typed as user messages.

- **The user's words**, cleaned to one line: `BossSession.compose` (`conductor/pty_manager.py:1039`)
  — correct and minimal, mean 69 chars tonight.
- **Worker updates**: `update_line` (`pty_manager.py:890`), pushed by `deliver_supervisory` (`:902`)
  and `_push` (`:955`).

```python
whose = "Your worker" if self.is_child(...) else "Worker (started before this chat)"
line  = f"{whose} · {title} ({task_id}) {UPDATE_LABEL[type]}"
return f"{line}: {summary}"          # summary = " ".join(event.summary.split())
```

`event.summary` is the worker's **entire last assistant message** (`cli_adapter.py:223 _turn_end`,
capped at `SUMMARY_CEILING = 8000`, `agent_events.py:23`), in raw Markdown, with every newline
squashed to a space, and several updates are then joined with `" · "` into one keystroke burst
(`_push`, `pty_manager.py:967`).

**Volume, this session (2026-09-01, Boss transcript):**

| | messages | chars | mean | max |
|---|---|---|---|---|
| user utterances | 12 | 829 | 69 | 193 |
| worker updates | 11 | 16,176 | 1,470 | **5,099** |

A 20:1 ratio. The largest single line (08:35:08Z, task_64260475) is 5,099 characters of flattened
Markdown on one line: `## Where the prompt lives - \`prompts/voice_conductor.md\` — **this is the
live one.** …` — headings, bullets, bold and backticks all intact, all inline.

**Approvals and questions** get their text from a screen scrape:
`detect_approval_prompt` (`cli_adapter.py:131`) takes the last three non-blank pane lines and joins
them with `" | "`. That is why 08:33:27Z reached the Boss as:

> Your worker · Open the new UI for the user (task_417558dd) is asking for approval: This conversation
> is cached for the current model. Switching to | Opus 5 (1M context) (default) means the full history
> gets re-read | on your next message.

— a model-switch dialog, not a permission request, delivered with terminal pipes in it.

### 2a. A hard bug: multi-KB pushes lose characters silently

`TmuxRuntime.send` (`conductor/tmux_runtime.py:969`) types the whole blob in one
`send-keys -l` (`_send_argv`, `:294`); the cmux path does one `cmux send` (`cmux_client.py:159`).
No chunking.

At 08:36:24Z the Boss was told task_417558dd's finish in 1,404 chars. The worker's actual message
(its transcript, 08:36:22.643Z) is 2,353 chars. **1,022 characters were dropped from the middle** and
the two halves spliced mid-word:

- worker wrote: `…a sessions sidebar, and a \`View session ›\` card that clicks through to a worker's cmux window. I launched it with the --claude backend… run \`python3 -m conductor.codex_web …\` for the browser version…`
- Boss received: `…a sessions sidebar, and a \`View sess` **`for the browser version`** `of the same page…`

`_confirm_submitted` (`tmux_runtime.py:1078`) only checks that the **last 40 characters** left the
input box, so a message that arrived with a hole in the middle passes as delivered. The Boss then
reasons — and speaks to the user — from corrupted text.

### 2b. Documentation drift

`prompts/manager.md:189-192` and `boss_tools.py:243` tell the Boss to expect
`Worker update · <title> (<task id>) …`. The code has emitted `Your worker · …` /
`Worker (started before this chat) · …` since the `is_child` change. `docs/boss-session.md:176` also
still shows the old prefix.

---

## 3. Why it reads as messy — the six defects, named

1. **Everything is a user message.** A worker cannot distinguish the user, the Boss, and the system;
   the Boss cannot distinguish the user from a worker except by a prose prefix. The PTY has exactly
   one input channel and everything is crammed into it.
2. **The wire format is free-form Markdown.** `_turn_end` makes the worker's last message *the
   protocol*. Nobody chose it; whatever the worker happened to write becomes what the Boss reads —
   headings, tables and all, on one line.
3. **No length contract.** 8,000-char ceiling in a channel whose reliable payload is empirically
   under ~1,400 chars.
4. **Boilerplate in-band.** ~1,000 chars of unchanging machine rules re-sent as user text to every
   worker, ahead of the actual task.
5. **The substitution rule fires on the wrong utterance**, replacing precise instructions with
   whatever the user happened to be saying at that moment — and does it silently, to N tasks at once.
6. **Screen scrapes leak into prose.** Approval text is three lines of terminal joined by pipes.

---

## 4. Proposal

### 4a. Stop the bleeding (small, high value, in order)

1. **Scope `hold_to_users_words` to the utterance of the turn that is calling.** Pass the calling
   turn's own utterance down instead of `self._utterance` (the join of all open turns) — the trace id
   already exists to key it. Keep it a *guard*, not a substitution: if the Boss's message is not the
   user's words, send `Boss: <message>` and log it, rather than sending an unrelated sentence. A
   silent replacement that changes the instruction is worse than any paraphrase.
   *(`global_conductor.py:94,1305,1714`)*
2. **Never send an empty-ish fragment.** Refuse a substitution shorter than ~25 chars or with no verb
   (". Just so you know") and send the Boss's text instead.
3. **Chunk and verify the PTY write.** Split at ~800 chars on sentence boundaries with a real
   inter-chunk settle, and make `_confirm_submitted` check length + head + tail, not just the last 40
   chars. Or bypass the keyboard: write the payload to a temp file and type
   `Read <path> — worker update` (see 4b). *(`tmux_runtime.py:294,969,1078`)*
4. **Run `plain_text()` over every summary before it is typed.** It already exists
   (`conductor/plain_text.py:25`) and is used for notifications and voice, but not for the Boss push.
   *(`pty_manager.py:890`)*

### 4b. A cleaner protocol

**One header line, then a body the reader can skip.** Both directions, same shape:

```
[worker task_64260475 · voice prompt] finished — investigation done, no files changed
· proposes 5 prompt edits; wants a yes/no on writing the PR
· full report: ~/.voice-conductor/tasks/task_64260475/report.md
```

- **Speaker label is a token, not prose**: `[user]`, `[boss]`, `[worker <id> · <short title>]`,
  `[system]`. One glance tells the reader who is talking. Replaces
  `Your worker · … (…) finished a turn:` (43 chars of boilerplate per update) and, critically, tells a
  *worker* that a message came from the Boss rather than from the user.
- **Outcome first, in one line, ≤200 chars.** Detail goes to a file the Boss reads *if it wants to*;
  the pane carries the pointer. This is the fix for both the 5,099-char line and the 1,022-char
  dropout: nothing large goes through the keyboard.
- **Give the worker a finish contract** instead of scraping its last message: ask for
  `STATUS: done|blocked|question` + one-line outcome + optional detail path, in
  `prompts/claude_worker.md`, and have `_turn_end` prefer that when present (falling back to today's
  behaviour). This makes the wire format something we chose. *(`cli_adapter.py:223`,
  `prompts/claude_worker.md`)*
- **Boilerplate out of the user message.** `SHARED_MACHINE_RULE` and `ASK_THEN_WAIT_RULE` belong in
  `claude --append-system-prompt`, not in the first user turn — the worker still obeys them, and the
  first thing in its transcript becomes the task. *(`conductor/conductor.py:41`,
  `cli_adapter.py:243`)*
- **Structure the creation blob** with three labelled fields the worker can parse and the UI can
  render — `TASK:` / `USER SAID:` / `CONTEXT:` — instead of a prose wall.
- **De-duplicate fanout at the source.** One utterance routed to N tasks should reach each once, with
  a marker (`[user · also sent to 2 other workers]`), so a worker knows it was not addressed alone;
  or the Boss must quote the part that belongs to each. Today N tasks each get the whole thing with
  nothing said about it.
- **Approvals: say the request, not the screen.** Emit the tool/command being requested (the runtime
  knows it) with the pane text as fallback, and drop the `" | "` join.
  *(`cli_adapter.py:131`, `tmux_runtime.py`)*
- **Fix the docs** so `prompts/manager.md:189`, `boss_tools.py:243` and `docs/boss-session.md:176`
  describe the header the code actually sends.

### 4c. What this changes for the user

The Boss's window becomes a readable conversation — their own sentences, and one labelled line per
worker event — instead of 16 KB of flattened Markdown around 800 characters of speech. Workers stop
receiving the user's stray asides as instructions. And a worker's finish stops silently losing a
kilobyte on the way to the Boss.
