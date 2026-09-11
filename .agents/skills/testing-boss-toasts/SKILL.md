---
name: testing-boss-toasts
description: How to live-test the codex app-server view (conductor/app_view.py), turn toasts (conductor/turn_toast.py) and jump links on the macOS box
---

# Live-testing app_view / turn_toast / jump links (macOS box)

## Prereqs
- `codex` (brew) logged in: `printenv OPENAI_API_KEY | codex login --with-api-key` (secret OPENAI_GPT_LIVE_KEY).
- Claude Code: `curl -fsSL https://claude.ai/install.sh | bash`; auths from `ANTHROPIC_API_KEY` env (a "Detected a custom API key" prompt appears on first run — choose Yes).
- cmux: first `open -a cmux` shows a Gatekeeper "downloaded from the Internet" dialog — click Open on the desktop. Its socket then needs a password: run `python3 -c "from conductor.cmux_setup import ensure_socket_access; ensure_socket_access()"` in the repo, then `cmux reload-config`, and export `CMUX_SOCKET_PASSWORD=$(cat ~/.config/cmux/.voice-agent-socket-password)` in every shell that drives cmux. cmux can lose this password across restarts — if a /s/ focus silently no-ops (still answers 200), re-run `ensure_socket_access()` + `cmux reload-config`.

## Gotchas
- computer-use `type` in Terminal.app mangles `/ _ $` (keyboard layout). Write commands into `~/runN.sh` via the write tool and type only `bash runN.sh`.
- Terminal.app does not follow OSC-8 links; press button/toast URLs with `curl` in a second tab (the URLs are printable — app_view prints them inside the card; a small driver can also print them plainly).
- `conduct.py` cannot be imported without aiohttp; to reproduce `toast_hook()` build the settings JSON by hand: Stop hook command `python3 <repo>/conductor/turn_toast.py <home>`, timeout 10, passed to `claude --settings <file>`.
- The jump `/s/<task_id>` link only focuses a cmux workspace that `_lookup` considers "ours": title `cond_<task_id>` (tmux_runtime.session_name) AND description containing `conductor-managed agent session` (cmux_runtime.MANAGED_MARK) — create test workspaces with
  `cmux new-workspace --name cond_task_x --description "conductor-managed agent session: cond_task_x" --focus false`.
  A workspace without the description makes focus a silent no-op (jump still answers 200 "Bringing ... forward" — best-effort by design).
- Populate the toast file with `conductor.turn_toast.write_sessions(home, rows)`; a title must be ≥12 chars to match by title.
- The web UI's `$`-command status events fire when a command *completes* — to see the `$` line in the status bar, use a prompt where a command finishes mid-turn (e.g. run `date` then ask for a long prose answer).
- `kill_shell` on a nohup'd web-server shell does not kill the server — `pgrep -f app_web` and kill the PID explicitly.
- Toast dot colors can be exercised by rewriting `turn_sessions.json` with glyph `done`/`attention` between mentions.
- codex `acceptForSession` is scoped per exact command line: a different command re-asks. Test "no re-ask" by repeating the same command.
- The official Codex desktop app is the cask `codex-app` (`brew reinstall --cask codex-app` if `/Applications/Codex.app` vanishes) — useful as the visual reference for side-by-side comparisons.
- `codex app-server` 0.151 requires `turnId` in `turn/interrupt`; CodexApp.interrupt() sending only threadId gets `-32600 missing field 'turnId'`.

## Web UI (conductor/app_web.py, PR 129)
- Run `python3 -m conductor.app_web /tmp/somework` from the repo root with
  `CMUX_SOCKET_PASSWORD` exported (the toast's /s/ link focuses cmux directly).
  Startup output is buffered when stdout is a pipe — find the port with
  `lsof -nP -iTCP -sTCP:LISTEN | grep -i python` and `open http://127.0.0.1:<port>/`.
- Safari is the only browser on this box. computer-use `type` into the Safari
  input mangles some chars (`+%Y` → garbage, sometimes drops a letter) — codex
  usually recovers, but prefer prompts without shell metacharacters, or paste
  via `pbcopy` + Cmd+V.
- The toast home for the web UI is `~/.voice-conductor` (hardcoded in main());
  seed it with `conductor.turn_toast.write_sessions("/Users/devin/.voice-conductor", rows)`.
- The /s/ focus page auto-closes its tab after 400ms — screenshot cmux itself,
  not the page.
- First `osascript` targeting Safari pops an automation-permission dialog on
  the desktop; click OK before the shell call can finish.
- Dark-mode check: `osascript -e 'tell application "System Events" to tell
  appearance preferences to set dark mode to true'` — Safari follows the
  system appearance live, no reload needed.

## Native macOS app (conductor/app_mac.py)
- Launch from repo root: `~/.local/bin/uv run --script conductor/app_mac.py /tmp/somework`
  with `CMUX_SOCKET_PASSWORD` exported (same as web UI). Warm deps first:
  `uv run --with 'pyobjc-framework-Cocoa>=10,<13' --with 'pyobjc-framework-WebKit>=10,<13' python3 -c "import AppKit, WebKit"`.
- The page loads with `?app=1`: in-page header hidden, toast open ↗ fetches
  (no popup). Frame autosave name is "CodexConversation" (in the python
  defaults domain) — delete with `defaults delete org.python.python` if you
  need a fresh centered launch.
- Type prompts via `pbcopy` + Cmd+V into the composer (computer-use `type`
  mangles shell chars here too).
- Dark mode: same System Events osascript as Safari; WKWebView follows live.
- Closing the window or Cmd+Q quits the whole app and shuts the backend —
  verify with `pgrep -fl 'codex app-server'`.
- The sessions/home dir for app_mac is ALWAYS `~/.voice-conductor` (the
  argv path is only the codex cwd). Seed delegation/toast rows at
  `~/.voice-conductor/boss/turn_sessions.json`, not under the argv dir —
  seeding the wrong file makes the bar render stale rows and live updates
  (`kind:"session"`) never fire because the watcher reads the real home.
- Delegation-bar live updates stop once every delegated glyph is
  done/failed (by design). To see a green transition after a red one,
  re-seed the file to `working` and send a NEW turn mentioning the task id
  — the fresh toast re-arms `_watch_delegations`, and a later flip to
  `done` repaints ALL bars with that task id.
- open ↗ focuses the workspace named `cond_<task_id>` exactly
  (tmux_runtime.session_name). Real task ids look like `task_xxx`, so the
  workspace is `cond_task_xxx`. If you seed a row whose task_id already
  starts with `cond_`, the lookup wants `cond_cond_...` and the click is a
  silent no-op — use task ids of the form `task_xxx` in test rows, or
  rename the workspace (`cmux workspace rename workspace:N --title ...`;
  note the flag is `--title`, a bare positional renames the WRONG target).
- For a "real long-running work" demo: `cmux workspace create --name
  cond_task_x --description "conductor-managed agent session: cond_task_x"
  --command "bash /tmp/job.sh" --focus false` runs a visible job; have the
  job touch a marker file on exit and a tiny watcher loop flip
  turn_sessions.json to done only when the marker appears — the bar then
  flips green within ~2-4s of real completion.
- The menu bar app name shows "Codex" (app_mac sets CFBundleName before
  AppKit starts); if it says "Python" the rename ran too late.
- In-app (`?app=1`) native-feel checks: CSS hides persistent scrollbars, but
  macOS's native overlay thumb still flashes briefly *while* scrolling in
  WKWebView — that transient thumb is expected, not a regression. Right-click
  on message text auto-selects the word first, so a context menu there is the
  live-selection exception, not a suppression failure; test suppression on
  truly blank background. A right-click on an empty unfocused input may show
  no menu on the first try — focus it and add text before judging.
- Since "The window loses its title bar" the window has no title bar/text —
  drag it by the top ~28px strip (right of the traffic lights); Option-click
  the green light to zoom without entering fullscreen. Content should start
  below the lights (page pads 2.8rem in app mode).
- Computer-use `type` into Safari's address bar mangles ':' to ';' — set the
  URL via `osascript -e 'tell application "Safari" to set URL of front
  document to "http://127.0.0.1:PORT"'` instead.
- To check what item types real codex actually emitted in a turn, grep the
  newest rollout under `~/.codex/sessions/YYYY/MM/DD/*.jsonl` for
  `item_completed`. With the default codex config, reasoning items arrive
  with empty `summary_text` (so no ✳ thought row can render) and `update_plan`
  calls are not surfaced as `todoList` items to the client — plan ☰ / thought ✳
  UI rows may be untestable end-to-end without a reasoning-summary config flag.

- cmux can lose its socket password across restarts — at the start of every
  session run the re-arm (`ensure_socket_access()` + `cmux reload-config`)
  before trusting workspace commands.
- There is no `cmux workspace delete`; remove a duplicate with
  `cmux workspace close workspace:N`.
- `/tmp/webwork` (or any /tmp cwd) can be wiped by the OS — `mkdir -p` it
  before launching, or app_mac shows a missing-directory error.
- An approval card partially clipped under the composer can swallow "1 Yes"
  clicks — scroll the card fully into view before clicking.
- Since "The session list stands open like cmux's" (bc1e27f) the sidebar
  opens by default (`toggleSide()` at load) and shifts the page content via
  margin rather than overlaying it. For cmux-focus tests, pre-select a
  different workspace (`cmux workspace select workspace:1`) before clicking
  a nested session row so the focus change is provable. The fleet button is
  the ✳ icon top-right; the ☰ next to it toggles the sidebar.
- computer-use `zoom` only returns the LAST action's image — capture
  rest-vs-hover comparisons as separate calls (move mouse away, zoom;
  hover, zoom), never two zooms in one action list.
- Re-seeding `turn_sessions.json` back to `working` does not re-arm the
  delegation watcher — send a fresh mention turn to get a live card again
  (e.g. for light-mode re-shots).
- To prove a page's JS polling timer stopped without devtools (the native
  WKWebView has no accessible console and Chrome is not installed), measure
  TCP socket churn on the app's port: `netstat -an | grep <port> |
  grep -v LISTEN | wc -l` sampled over 8s intervals — growth means active
  polling, pure TIME_WAIT drain means the timer is cleared.

## Devin Secrets Needed
- OPENAI_GPT_LIVE_KEY (codex login)
- ANTHROPIC_API_KEY (Claude Code)
