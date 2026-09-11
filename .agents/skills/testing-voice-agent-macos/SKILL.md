---
name: testing-voice-agent-macos
description: How to run and end-to-end test the voice-agent macOS app (conduct.sh) on a Mac VM with no physical microphone, including simulated speech and synthetic Fn key.
---

# Testing voice-agent on a macOS VM (no physical mic)

## cmux terminal hyperlinks (OSC 8 / bare URLs)
- cmux/Ghostty recognizes both OSC 8 hyperlinks and bare URLs, but only on
  **cmd+click**; a plain click in a shell does nothing, and no hover
  underline is visible in screenshots (cmd-hover may underline, but a
  screenshot can't capture it while the modifier is held).
- Cmd+click opens the URL in an **internal cmux browser pane** (right
  split of the same workspace), not a new workspace or external browser.
  The pane is discoverable via `cmux list-pane-surfaces` (shows e.g.
  `surface:NN  127.0.0.1`) and closable with
  `cmux close-surface --workspace <ws> --surface <id>` (requires the
  explicit --workspace) — so click + CLI auto-close works as a "button".
- Inside Claude Code (mouse-reporting TUI): Bash-tool output is collapsed
  ("ran 1 shell command") — click the summary to expand. In the expanded
  output the OSC 8 escape survives: cmd+click on the label opens the
  hidden target. Plain URLs in assistant reply text are rendered blue and
  cmd+click also opens the internal pane. A **plain click** on a link in
  Claude Code opens it in the **default macOS browser (Safari)** instead.
- When crafting OSC 8 test strings, avoid quoting through `cmux send`
  (escapes get mangled) — write a script file with printf '\033]8;;URL\033\\'
  and run it.
- macOS may pop TCC permission dialogs ("cmux would like to access ...")
  on first browser-pane use; dismiss with Don't Allow.

## Inline watch links (conductor/watch_link.py)
- The watch server logs `watch.server_started` with its port; the port is
  persisted in `~/.voice-conductor/boss/watch.port` and reused on restart.
- Verify a click landed by grepping the run log for `watch.link_opened`
  with the task_id — a cmd+click can silently miss (transcript reflows
  between screenshot and click); always re-screenshot right before
  clicking and confirm the log event afterwards.
- `curl http://127.0.0.1:<port>/watch/task_bogus` is a safe smoke test:
  expect 204 + `watch.link_opened` then `watch.open_failed` (no such task).
- Clicking a bottom notification card's BODY navigates cmux to that
  worker's workspace — click only the small X to dismiss.

## cmux custom (interpreted Swift) sidebars
- Installed at `~/.config/cmux/sidebars/*.swift`; manage with
  `cmux sidebar validate|select|reload <name>`. `validate` passing does NOT
  mean it renders: the interpreter skips unsupported expressions silently
  (blank sections, no inline error).
- Known-unsupported in the interpreter: optional-coalescing `??` inside
  closures (e.g. `($0.description ?? "").hasPrefix(...)` renders nothing).
  Use direct optional member access (`$0.description.hasPrefix(...)`) and
  `if let` for optionals instead.
- `w.agents` is absent on every workspace on this VM (probe with a tiny
  sidebar dumping `if let agents = w.agents`), so any spinner/checkmark
  keyed on agent status never shows; only fallback branches render.
- A completed worker's cmux workspace is closed when the Boss calls
  `complete_task` (~3s after task.completed), so "completed" sidebar states
  are transient. To verify done-state rendering, run the same commands
  dress() writes on a surviving managed workspace:
  `cmux set-progress 1.0 --label "reported back to Boss" --workspace <id>`.
- The first dress at task.created races workspace listing; since ef1e5fc
  WorkspaceDecor retries a missed dress (3x, 2s apart), so the bar should be
  titled with a spinner within ~10s — `workspace.dress_missed` in the log
  means all retries lost the race.
- Trivial tasks (haiku/limerick) complete in ~10-15s and the workspace closes
  ~3s later — too fast to screenshot any sidebar state. Use a task that reads
  source and writes 100+ lines to keep the "working" state up for minutes.
- If a delegation is refused with `boss.bridge_refused` after a conductor
  restart, kill the stale adopted boss claude process
  (`pgrep -f voice-conductor/boss`) and restart conduct.sh; the fresh Boss
  gets a valid bridge token.

## One-time environment setup
- `brew install tmux uv` and install Claude Code CLI (`~/.local/bin/claude`).
- Headless Claude auth: store the Anthropic key in the keychain and point
  `~/.claude/settings.json` `apiKeyHelper` at a script that prints it.
  Verify with `env -u ANTHROPIC_API_KEY claude -p "say OK"`.
- Run `claude` once interactively (tmux is fine) to clear first-run
  onboarding (theme/trust prompts), or tmux workers hang with
  "no session file appeared".
- The Boss runs claude with `--permission-mode bypassPermissions`; Claude
  Code shows a one-time "Bypass Permissions mode" acceptance prompt that
  defaults to "No, exit", which kills the Boss and produces
  `boss.warm_failed` / "boss-mcp never connected" errors. Pre-setting
  `bypassPermissionsModeAccepted: true` in `~/.claude.json` may NOT stick
  (claude can rewrite the file); if `boss.warm_failed` appears, open the
  cond_boss workspace in cmux, select "Yes, I accept", press Enter, then
  speak another turn — the manager relaunches the Boss per turn.
- Audio: `brew install --cask blackhole-2ch` then REBOOT. BlackHole 2ch as
  default input AND output gives a loopback.
- cmux is auto-installed to /Applications/cmux.app but Gatekeeper blocks it
  on first run ("cmux None is too old"); approve the security dialog once.
- `.env` needs `OPENAI_API_KEY` with GPT Live access.

- Claude CLI's `--disallowedTools` is VARIADIC: a positional prompt placed
  directly after `--disallowedTools X` is swallowed as another tool name
  and silently discarded in interactive mode — the worker launches idle
  and no session file appears (measured on claude 2.1.251). Use the
  equals form `--disallowedTools=X` or put another flag between it and
  the prompt. If workers fail with "claude session did not start", check
  the worker argv (`pgrep -fl disallowedTools`) for this first.
- After a reboot /tmp is cleared — regenerate the fn_press.py driver and
  the `say -o /tmp/*.aiff` utterance files.

## Installer testing (private repo)
- The public one-liner 404s while the repo is private. Serve install.sh from
  the local clone (`python3 -m http.server 8765 --directory ~/repos/voice-agent`)
  and run `curl -fsSL http://localhost:8765/install.sh |
  VOICE_CONDUCTOR_REPO=https://git-manager.devin.ai/proxy/github.com/tamaratran/voice-agent.git bash`.
- Devin's Terminal zsh exports an OPENAI_API_KEY without GPT Live access, and
  conduct.py prefers os.environ over .env — launch with
  `env -u OPENAI_API_KEY conduct` to get the first-run key prompt / use .env.
- Typing the 167-char key into the hidden getpass prompt via computer-use gets
  mangled (~70 wrong chars). Type it once for the demo, then fix .env from the
  exec shell: `printf 'OPENAI_API_KEY=%s\n' "$OPENAI_GPT_LIVE_KEY" > .env`.
- `osascript -e 'tell app "Terminal" to do script "..." in front window'` is
  the reliable way to run visible commands (avoids slash mangling); but if that
  window is busy running conduct, use `do script` WITHOUT "in front window" to
  get a new window.
- Audio can wedge system-wide (afplay/say/sounddevice all hang or
  AudioQueueStart fails) on BlackHole 0.7.1 + macOS 26; killall coreaudiod did
  NOT fix it — only a reboot did. If playback hangs, reboot and re-create the
  /tmp aiffs and fn_press.py.
- TCC GUI "+" in System Settings asks for an account password we don't have,
  and sudo sqlite3 on TCC.db fails (SIP read-only). Terminal never got Input
  Monitoring — the synthetic-Fn path still worked without it.

## Simulating live speech (full spoken path)
- TCC matters: audio capture only works from a process with Microphone
  permission. Run `./conduct.sh` inside Terminal.app (has the grant); the
  remote exec shell records pure silence with no error.
- Generate utterances: `say -o /tmp/u.aiff "How are my tasks doing?"`.
- Synthetic Fn key: CGEventPost a kCGEventFlagsChanged event with flags
  0x800000 (down) / 0 (up) to kCGHIDEventTap — hotkey.py's listen-only tap
  sees it. Works from the exec shell (no extra permission needed).
- One turn: post Fn down → sleep 0.5 → `afplay /tmp/u.aiff` → sleep 0.3 →
  post Fn up. Transcription appears within ~2s.
- Verify in `~/.voice-conductor/logs/conductor-<pid>.jsonl`:
  `voice.utterance_completed` (transcript), `voice.reply_spoken`,
  `boss.turn` / `boss.tool_call`, `boss.window_shown` (fires once, needs a
  connected Boss). First Boss turn can take 1-3 min (uv resolve + MCP
  connect); `manager.turn_timeout` with a late answer is normal.
- Overlay HUD can be demoed standalone: run overlay.py via
  `uv run --python 3.13 overlay.py` and write NDJSON to stdin:
  `{"state":"listening","level":0.5}`, `{"state":"thinking"}`,
  `{"state":"heard","text":"..."}`, `{"state":"quit"}`.

## Showcasing cmux in a recording
- Bring cmux forward and size it via `osascript`:
  `tell application "cmux" to activate` + System Events set position/size.
- Computer-use `type` into Terminal can mangle `/` characters; cd first via
  exec shell or type slash-free commands (`bash conduct.sh` from the repo dir).
- Mid-run TCC dialogs appear the first time: "cmux wants to control System
  Events" (needed for window surfacing) and "cmux would like to access the
  Microphone" (worker unit tests touch sounddevice) — click Allow on both.
- A delegation utterance like "Create a task in the voice agent project to
  add a build badge to the readme" makes the Boss resolve+register the
  project and start a worker in its own cmux workspace (auto-raised).
  Worker questions can be answered by voice ("pick option two, badge only")
  — the Boss send_to_task's it verbatim; if the worker ignores it, the Boss
  flags and corrects it on the completion push.
- "Great, thank you" makes the Boss complete_task and close things out.

## Overlay delegation bar (HUD strip above the voice capsule)
- The bar sits directly above the voice capsule (~y 650 of 768 on this VM);
  hover effects are subtle (bg 253→249, border darkens, chevron bolds) —
  prove them by pixel-diffing two zoom screenshots, not by eye.
- The live verb+seconds render AFTER the title in one truncating line, so
  they are usually cut off; a very short task title still may not show the
  seconds. Verb rotates every 8s (Deliberating/Chipping away/Toiling/
  Beavering away) — capture two zooms 9s apart to prove liveness.
- Settled (green/red) rows retire after 45s; screenshot within that window.
- Restart replay reads persisted subagent status from
  ~/.voice-conductor/projects/<proj>/tasks/<task>/subagent.json; compare it
  against STATE_GLYPHS in conductor/delegation.py when the bar fails to
  reappear.
- To make a worker linger for hover/click tests, ask for a ~200-line study
  task "read the source carefully first" — lives several minutes.

## Gotchas found while retesting fixes
- Workers on ask-then-wait branches raise questions via Claude Code's
  AskUserQuestion options UI. That UI produces NO conductor event (no
  task.approval_required, no task.completed), so the Boss cannot see it:
  status questions get "nothing's waiting on you" and send_to_task ticks
  an option without submitting the form. Recovery that works: tell the
  Boss to interrupt the task and re-send the answer.
- To re-arm Claude's bypass-permissions dialog set
  `bypassPermissionsModeAccepted: false` in ~/.claude.json — but the
  dialog may still not appear (acceptance seems cached elsewhere too);
  verify boss.warmed/boss.mcp_connected instead.
- A full state reset is `mv ~/.voice-conductor ~/.voice-conductor.bak.*`;
  the next launch re-asks GUI permissions (opens System Settings panes)
  and starts with 0 projects.
- `pkill -f cmux` from an exec-shell one-liner kills the shell itself
  (self-match); use `pkill -f 'cmux[.]app'`.

## Computer-use tasks (TextEdit demo)
- The Boss refuses computer-use tasks until the terminal hosting the agents
  has BOTH Accessibility and Screen Recording in System Settings > Privacy &
  Security. Toggling them needs the local admin password (GUI prompt), and
  Screen Recording only takes effect after quitting/reopening Terminal
  (which kills conduct.sh + cmux + workers — plan for a restart-recovery).
- Even with a first refusal, repeating the same request makes the Boss
  create the task anyway ("Second time asked, so I did start it").
- The worker may not use the visual screen driver at all: in this build it
  fell back to AppleScript (`osascript` to TextEdit/System Events), which
  triggers per-app TCC dialogs "cmux wants access to control TextEdit /
  System Events" — click Allow. The task then completes and verifies text.
- Restart-recovery caveat: right after relaunching conduct.sh the watchdog
  can mark a still-alive worker task.worker_gone / interrupted; a voice
  turn asking to resume it (resume_task) recovers cleanly.
- Mid-run permission grants (measured on PR #132's fresh re-probe): the
  Screen Recording probe (CGPreflightScreenCaptureAccess) flips live, but
  AXIsProcessTrusted stays stale for the already-running conductor process,
  so a mid-run Accessibility grant still requires restarting conduct.sh.
- Grants are per-hosting-app: Terminal (conductor) AND cmux (workers) each
  need Accessibility + Screen Recording. Click "Later" on the "Quit &
  Reopen" dialog to avoid killing the running app; the worker may also pop
  a "cmux is requesting to bypass the system private window picker" dialog
  — click Allow.
- Boss error visibility: raising in boss_mcp surfaced only "Error executing
  tool create_task" to the Boss (fixed on PR #132 by returning "error: <reason>"
  as tool content). If the Boss ever relays "no reason given", check
  ~/.claude/projects/-Users-devin--voice-conductor-boss/*.jsonl tool_result
  contents to see what the Boss actually saw.
- To relaunch conduct.sh by typing into Terminal, use a `~/go.sh` helper
  that exports PATH="$HOME/.local/bin:$PATH" first (bash go.sh in a
  non-login shell otherwise fails with "Claude Code not found"); typing
  long paths directly into Terminal mangles slashes.

## Devin Secrets Needed
- ANTHROPIC_API_KEY (Claude Code auth via keychain helper)
- OPENAI_GPT_LIVE_KEY (GPT Live realtime API, goes in .env)
