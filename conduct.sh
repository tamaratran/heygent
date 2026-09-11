#!/usr/bin/env bash
# The Voice Conductor: hold Fn, talk, manage many coding agents. See conduct.py.
#
set -euo pipefail
cd "$(dirname "$0")"
# Prefer a local arm64 uv; the Homebrew uv may be an Intel build and this
# dependency tree has no Intel macOS wheels.
UV="$HOME/.local/bin/uv"
[ -x "$UV" ] || UV="$(command -v uv || true)"
[ -n "$UV" ] || { echo "uv not found - install it from https://astral.sh/uv" >&2; exit 1; }
# Onboarding needs only uv, so it dispatches before the conductor's other
# preflights: a machine still being set up can onboard before Claude Code
# or tmux is installed. --onboard re-runs the flow on demand; --onboard-cli
# is the same flow entirely in the terminal.
if [ "${1:-}" = "--onboard" ]; then
  "$UV" run --python-preference only-managed --python 3.13 onboarding.py
  exit 0
fi
if [ "${1:-}" = "--onboard-cli" ]; then
  "$UV" run --python-preference only-managed --python 3.13 onboarding_cli.py
  exit 0
fi

command -v tmux >/dev/null || { echo "tmux not found - install it with: brew install tmux" >&2; exit 1; }
command -v claude >/dev/null || { echo "Claude Code not found - install it from https://code.claude.com/docs/en/setup" >&2; exit 1; }

# First run: onboarding (permissions, mic check, key picker) opens by itself,
# then the conductor starts. The key picker writes hotkey.json, so its absence
# means onboarding has never been completed. The install command is run from
# a terminal, so the first-run flow stays in it; the window only opens when
# there is no terminal to draw in.
if [ ! -f "$HOME/.voice-conductor/hotkey.json" ]; then
  if [ -t 0 ] && [ -t 1 ]; then
    "$UV" run --python-preference only-managed --python 3.13 onboarding_cli.py || exit
  else
    "$UV" run --python-preference only-managed --python 3.13 onboarding.py || exit
  fi
fi

# A stale ANTHROPIC_API_KEY silently overrides the claude.ai login and can
# hang every Manager/worker session; the stored login is what we want.
#
# The CLAUDE_CODE_* markers go for a different reason. This gets started
# from inside a Claude Code session more often than not - a session that
# just merged a fix restarts the conductor to pick it up (seen 2026-08-28:
# the running conductor carried that session's CLAUDE_CODE_CHILD_SESSION,
# CLAUDE_CODE_BRIDGE_SESSION_ID and CLAUDE_JOB_DIR). Every Claude Code
# process we start then looks like a child of a session it has nothing to
# do with. For a worker that is known to be fatal - the CLI stops writing
# the transcript our supervision reads - which is why the runtimes scrub
# these before launching one (conductor/tmux_runtime.py, the same list).
# Drop them here too, so nothing the conductor itself starts inherits
# them either.
#
# OPENAI_API_KEY is NOT dropped here: the conductor's voice needs it, and
# it may come from this environment rather than .env. conduct.py removes
# it from its own environment once read, and every worker launch strips
# it again (conductor/tmux_runtime.py, CONDUCTOR_SECRETS).
exec env -u ANTHROPIC_API_KEY \
  -u CLAUDE_CODE_CHILD_SESSION -u CLAUDE_CODE_SESSION_ID \
  -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_CODE_BRIDGE_SESSION_ID \
  "$UV" run \
  --python-preference only-managed --python 3.13 \
  conduct.py "$@"
