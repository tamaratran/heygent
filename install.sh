#!/usr/bin/env bash
# One-line installer for the Voice Conductor:
#
#   curl -fsSL https://raw.githubusercontent.com/tamaratran/heygent/main/install.sh | bash
#
# It clones (or updates) the app under ~/.voice-conductor/app, installs the
# tools the launcher needs (uv, tmux, Claude Code), and puts a `conduct`
# command on PATH. Safe to run again: every step is a no-op once done.
set -euo pipefail

REPO="${VOICE_CONDUCTOR_REPO:-https://github.com/tamaratran/heygent.git}"
APP_DIR="${VOICE_CONDUCTOR_HOME:-$HOME/.voice-conductor}/app"
BIN_DIR="$HOME/.local/bin"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
fail() { printf 'install: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "this app is macOS-only"

# git ships with the Xcode Command Line Tools; asking for those is the one
# step macOS insists a human click through.
if ! command -v git >/dev/null 2>&1; then
  say "git is needed first - macOS will offer the Command Line Tools:"
  xcode-select --install || true
  fail "re-run this installer once the Command Line Tools finish installing"
fi

if [ -d "$APP_DIR/.git" ]; then
  say "Updating $APP_DIR ..."
  git -C "$APP_DIR" pull --ff-only
elif [ -f "$APP_DIR/conduct.sh" ]; then
  # Unpacked there by heygent.app, which keeps it current itself.
  say "Using the app already under $APP_DIR"
else
  say "Cloning into $APP_DIR ..."
  mkdir -p "$(dirname "$APP_DIR")"
  git clone "$REPO" "$APP_DIR"
fi

# uv: an arm64 build at ~/.local/bin/uv, the location the launcher prefers
# (a Homebrew uv can be an Intel build with no wheels for this tree).
if [ ! -x "$BIN_DIR/uv" ]; then
  say "Installing uv ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

if ! command -v tmux >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    say "Installing tmux ..."
    brew install tmux
  else
    fail "tmux is needed - install Homebrew (https://brew.sh) and re-run, or: brew install tmux"
  fi
fi

if ! command -v claude >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/claude" ]; then
  say "Installing Claude Code ..."
  curl -fsSL https://claude.ai/install.sh | bash
fi

mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/conduct" <<EOF
#!/usr/bin/env bash
exec "$APP_DIR/conduct.sh" "\$@"
EOF
chmod +x "$BIN_DIR/conduct"

say "Installed."
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "Add $BIN_DIR to your PATH first:"
     printf '  echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> ~/.zprofile && exec zsh\n' ;;
esac
say "Then start it with:  conduct"
say "First run asks for your OpenAI API key and walks through the macOS permissions."
