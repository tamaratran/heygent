---
name: testing-conductor-pipeline-linux
description: How to test voice-agent's conductor pipeline (transcript watcher, cli_adapter, subagent_state) on a Linux box where the macOS app cannot run, including a visual demo inside the official cmux TUI.
---

# Testing the conductor pipeline on Linux

## Real process-level restart test (no stubs at all)

A full GlobalConductor + real TmuxClaudeRuntime + real tmux session works on
Linux with a fake worker CLI standing in for Claude Code
(`.testing/restart/fakebin/claude`): it prints "? for shortcuts" (the
PROMPT_READY line), writes a real session JSONL (user → progress) into
`~/.claude/projects/<munged cwd>/`, re-execs itself with `--session-id <sid>`
on its cmdline so `pgrep -f <sid>` (the runtime's real `_process_alive`
probe) finds it, appends the assistant `end_turn` after `FINISH_DELAY`
seconds, and sleeps for ever. Point the runtime at it with
`TmuxClaudeRuntime(claude_binary=<path>)`.

- The project dir must satisfy `locator.inspect_path` (a real git repo with a
  commit works); GitWorktreeManager then makes a worktree whose leaf is the
  task id, which `_live_worker_in` expects.
- Wire logging yourself or nothing lands in the JSONL logs:
  `configure_logging(home)`, `bus = ObservabilityBus()`,
  `bus.subscribe(LoggingSink())`, pass `bus` to both runtime and conductor.
- Launch process A from a persistent/interactive shell and `kill -9` the
  python pid it prints — killing the wrapping subshell (or letting the exec
  tool's group-kill fire) can miss A or take the tmux server down with it.
  Install `lsof` (the `_process_alive` cwd probe uses it).
- Working example: `.testing/restart/` (proc_a.py, proc_b.py,
  run_restart_demo.sh, test_plan_real_restart.md).


The macOS app (AppKit, PortAudio, cmux.app) cannot launch on a Linux VM, but
everything from the session-JSONL bytes onward is testable:

## Transcript-driven harness (no GUI needed)

- Build `TmuxClaudeRuntime` with `__new__` (skips the tmux check), set
  `epoch`/`transcript=None`, stub `_alive` (return True) and `_pane`
  (return `"> \n? for shortcuts"` so the approval detector sees a ready
  prompt), and point a `_TmuxSession(jsonl_path=...)` at a temp JSONL.
- Append entries line-by-line and call `await rt._watch_once(sess)`; collect
  events via `sess.handlers.append(...)`; feed them to
  `conductor.subagent_state.reduce`. This exercises the real watcher →
  `adapter.normalize` → reducer path.
- `SubagentState` requires `id, task_id, project_id, provider,
  provider_session_id, title` plus `status`.
- To compare against master without dirtying the branch:
  `git archive master | tar -x -C /tmp/va-master` and `sys.path.insert(0, ...)`.
- Working examples: `.testing/harness.py`, `.testing/cmux_demo.py` (may be
  recreated from the PR #136 testing session if deleted).

## Testing the adoption / missed-finish recovery path

- To exercise `ensure_watched` → `_readopt(recover_finish=True)` →
  `deliver_adopted_finish` without tmux: additionally stub
  `_live_worker_in` (return any `cond_*` name) and set
  `rt.adapter.projects_root = <tmpdir>` so the REAL
  `adapter.transcript_for` resolves; write the session JSONL at
  `<tmpdir>/<munge_project_dir(cwd)>/<session_id>.jsonl` (cwd leaf must
  start with `task_`).
- Replicate watch_unwatched's ordering: `ensure_watched(...)`, then
  `sess.handlers.append(collector)`, then
  `getattr(rt, "deliver_adopted_finish", None)` (getattr, so the same
  harness runs against pre-fix code where the method is absent).
- Working example: `.testing/recovery_demo.py`.

## Visual demo in the official cmux TUI (Linux)

- Install: `curl -fsSL https://cmux.com/tui/install.sh | sh` → `~/.cmux/bin/cmux`.
- Run a durable server: `nohup cmux --headless --session demo &`; attach in a
  GUI terminal with `cmux attach --session demo` (sidebar: Ctrl-b s).
- CLI: `cmux --session demo workspace create --name X --json` (returns
  workspace/terminal ids), `terminal <id> write --text '...'`,
  `agent report --terminal <id> --state working|idle|done|blocked|unknown
  --source socket`, `notification create`, `workspace <id> rename/focus`.
- Gotcha: an attached client does NOT live-update workspace renames or agent
  states in the sidebar; it DOES live-update terminal titles. To show a live
  status badge in the sidebar, print an OSC title escape into the workspace's
  terminal (e.g. via a `tail -f feed` pane):
  `printf '\033]0;● running\007' >> feed`.
- Keep workspace names short (~17 chars) — the sidebar truncates.
- The TUI's socket accepts commands from outside cmux (no `cmuxOnly`
  restriction like the macOS app described in docs/cmux-spike.md).
