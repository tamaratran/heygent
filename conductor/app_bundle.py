"""heygent as a real macOS application.

Builds `heygent.app` - the bundle Finder, the Dock and Spotlight
know - around the repo's own launcher. The bundle is a shell, not a
copy: its executable execs `conduct.sh` where the repo lives, so a
`git pull` updates the app with no rebuild. What the bundle adds is
what only a bundle can have: an identity (bundle id, name, icon) that
macOS attaches the microphone, notification and automation permissions
to, instead of "Python".

Build it:

    python3 -m conductor.app_bundle              # ./dist/heygent.app
    python3 -m conductor.app_bundle --install    # /Applications
    python3 -m conductor.app_bundle --sign "Developer ID Application: ..."
    python3 -m conductor.app_bundle --standalone  # for other Macs

A standalone bundle carries a copy of the repo in Contents/Resources/app
and, on launch, unpacks it to ~/.voice-conductor/app before running
conduct.sh from that copy; nothing is ever written inside the signed
bundle. A git checkout at that path (install.sh's, or a developer's) is
left alone and not run: the copy goes to ~/.voice-conductor/release
instead, so the downloaded app always runs the downloaded code. A first
run with a tool missing runs install.sh in the background behind a
"setting up" dialog; the OpenAI key, the Claude login and the
permissions are asked for by conduct.py itself, in dialogs. Terminal is
offered only when the install fails, so the errors can be seen.

The bundle's executable is a small Mach-O stub that execs the launcher
script, Contents/Resources/heygent.sh: LaunchServices (Finder, `open`,
the Dock) refuses to launch a bundle whose CFBundleExecutable is a
shell script (-10669). The script lives under Resources, not beside the
stub, because codesign treats everything in Contents/MacOS as code and
signs a script there in extended attributes only - which a zip drops
(the notary service then finds it unsigned) or carries as a `._` file
(which Gatekeeper rejects). A resource is sealed in CodeResources and
survives either. Without a C compiler the script stands in as the
executable and the app can only be started from a terminal.
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

APP_NAME = "heygent"
BUNDLE_ID = "ai.heygent.conductor"

# Finder launches with almost no PATH; the launcher restores the places
# conduct.sh's tools (uv, tmux, claude) actually live.
LAUNCHER = """#!/bin/bash
# The app's launcher: hand straight to the repo's launcher, so a
# `git pull` there updates the app with no rebuild.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
LOG_DIR="$HOME/.voice-conductor/logs"
mkdir -p "$LOG_DIR"
exec "{conduct}" >>"$LOG_DIR/app-launch.log" 2>&1
"""

STANDALONE_LAUNCHER = """#!/bin/bash
# The app's launcher, standalone flavour: the repo travels inside the
# bundle and runs from a copy under the conductor home.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
CONTENTS="$(cd "$(dirname "$0")/.." && pwd)"
PAYLOAD="$CONTENTS/Resources/app"
CONDUCTOR_HOME="${VOICE_CONDUCTOR_HOME:-$HOME/.voice-conductor}"
APP_DIR="$CONDUCTOR_HOME/app"
LOG_DIR="$CONDUCTOR_HOME/logs"
mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/app-launch.log" 2>&1

# Double-clicked inside the disk image's window instead of dragged out
# of it: the permissions macOS would grant go to a path that is gone
# once the image is ejected, so ask for the drag instead.
case "$CONTENTS" in /Volumes/*)
  volume="$(df -P "$CONTENTS" | sed -nE '2s/^.*[0-9]+% +//p')"
  if [ -n "$volume" ] && hdiutil info 2>/dev/null | grep -qF "	$volume"; then
    echo "running from the disk image at $volume; asking for the drag to Applications"
    osascript -e 'display dialog "heygent is still on the disk image. Drag heygent onto the Applications folder next to it, then open it from Applications." with title "heygent" buttons {"OK"} default button "OK" with icon caution' >/dev/null 2>&1
    exit 1
  fi
  ;;
esac

# A git checkout at app/ (install.sh's, or a developer's) is the user's
# and is not written into - but it is not run either: the app that was
# downloaded runs the code that was downloaded, from release/ instead.
# Anything else at app/ is ours: refreshed whenever the bundle carries
# a different version. .env is never in the bundle, so it survives the
# refresh; a release/ that has none starts with the checkout's.
if [ -d "$APP_DIR/.git" ]; then
  echo "$APP_DIR is a git checkout; running the bundled release from $CONDUCTOR_HOME/release"
  CHECKOUT="$APP_DIR"
  APP_DIR="$CONDUCTOR_HOME/release"
  if [ ! -f "$APP_DIR/.env" ] && [ -f "$CHECKOUT/.env" ]; then
    mkdir -p "$APP_DIR"
    cp "$CHECKOUT/.env" "$APP_DIR/.env"
  fi
fi
if ! cmp -s "$PAYLOAD/{stamp}" "$APP_DIR/{stamp}"; then
  mkdir -p "$APP_DIR"
  ditto "$PAYLOAD" "$APP_DIR"
fi

missing=""
for tool in uv tmux claude; do
  command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done

if [ -n "$missing" ]; then
  # install.sh needs no typing in the usual case (uv, tmux via Homebrew,
  # Claude Code all install unattended), so it runs here, behind a
  # dialog that says what is happening and goes away when it is done.
  echo "first run: installing$missing"
  osascript -e 'display dialog "heygent is setting up: installing what it needs (uv, tmux, Claude Code). This takes a minute or two, and heygent opens by itself when it is done." with title "heygent" buttons {"OK"} default button "OK" with icon note giving up after 1800' >/dev/null 2>&1 &
  waiting=$!
  if bash "$APP_DIR/install.sh" >"$LOG_DIR/install.log" 2>&1; then
    kill "$waiting" 2>/dev/null
  else
    kill "$waiting" 2>/dev/null
    echo "install.sh failed; see $LOG_DIR/install.log"
    # The errors are in the log; Terminal is where they can be read and
    # the install retried by hand.
    osascript - "$APP_DIR" "$LOG_DIR/install.log" "$(tail -n 6 "$LOG_DIR/install.log" | perl -pe 's/\\e\\[[0-9;]*m//g')" <<'EOF'
on run argv
  set appDir to item 1 of argv
  set logPath to item 2 of argv
  set tailText to item 3 of argv
  set msg to "heygent could not finish setting up." & return & return & tailText & return & return & "The full log is at " & logPath & ". Open Terminal to run the install where you can see it, then open heygent again."
  set answer to display dialog msg with title "heygent" buttons {"Quit", "Open Terminal"} default button "Open Terminal" with icon caution
  if button returned of answer is "Open Terminal" then
    set cmd to "export PATH=\\"$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH\\"; bash " & quoted form of (appDir & "/install.sh")
    tell application "Terminal"
      activate
      do script cmd
    end tell
  end if
end run
EOF
    exit 1
  fi
fi

exec "$APP_DIR/conduct.sh"
"""

STAMP = ".bundle-version"

# The Mach-O executable: exec the launcher script under ../Resources.
STUB = r"""#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(int argc, char **argv) {
    char self[PATH_MAX];
    uint32_t size = sizeof(self);
    if (_NSGetExecutablePath(self, &size) != 0) return 1;
    char real[PATH_MAX];
    if (realpath(self, real) == NULL) return 1;
    char script[PATH_MAX];
    snprintf(script, sizeof(script), "%s/../Resources/%s", dirname(real),
             "{script}");
    execv(script, argv);
    perror(script);
    return 1;
}
"""


def compile_stub(source: str, out: Path) -> bool:
    """Build the Mach-O stub with the system C compiler (Xcode Command
    Line Tools). Without one there is no stub."""
    cc = shutil.which("cc")
    if cc is None:
        return False
    with tempfile.TemporaryDirectory() as scratch:
        src = Path(scratch) / "stub.c"
        src.write_text(source)
        done = subprocess.run([cc, "-O2", "-o", str(out), str(src)],
                              capture_output=True, timeout=120)
    return done.returncode == 0 and out.is_file()


def payload_files(repo: Path) -> list[Path]:
    """The repo files a standalone bundle carries: what git tracks, or
    (outside a checkout) everything but the obvious local state."""
    git = shutil.which("git")
    if git is not None:
        done = subprocess.run([git, "-C", str(repo), "ls-files", "-z"],
                              capture_output=True, timeout=60)
        if done.returncode == 0:
            return [repo / name for name in
                    done.stdout.decode().split("\0") if name]
    skip = {".git", ".env", ".venv", "dist", "__pycache__"}
    files = []
    for root, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in skip]
        files += [Path(root) / n for n in names if n not in skip]
    return files


def bundle_version(repo: Path) -> str:
    git = shutil.which("git")
    if git is not None:
        done = subprocess.run([git, "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=60)
        if done.returncode == 0 and done.stdout.strip():
            return done.stdout.strip()
    return str(int(time.time()))


def copy_payload(repo: Path, into: Path) -> None:
    if into.exists():
        shutil.rmtree(into)
    for source in payload_files(repo):
        target = into / source.relative_to(repo)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    (into / STAMP).write_text(bundle_version(repo) + "\n")


def info_plist(repo: Path) -> dict:
    return {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": "heygent",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "CFBundleIconFile": "heygent.icns",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription":
            "heygent listens while you hold the push-to-talk key.",
        "NSAppleEventsUsageDescription":
            "heygent raises its own window and terminal sessions.",
    }


def make_icns(png: Path, icns: Path) -> bool:
    """The Finder icon. `sips` + `iconutil` ship with macOS; anywhere
    else (or with either missing) there is simply no icon file."""
    if not png.is_file():
        return False
    sips = shutil.which("sips")
    iconutil = shutil.which("iconutil")
    if sips is None or iconutil is None:
        return False
    with tempfile.TemporaryDirectory() as scratch:
        iconset = Path(scratch) / "heygent.iconset"
        iconset.mkdir()
        for size in (16, 32, 64, 128, 256, 512, 1024):
            for scale, suffix in ((1, ""), (2, "@2x")):
                px = size * scale
                if px > 1024:
                    continue
                out = iconset / f"icon_{size}x{size}{suffix}.png"
                done = subprocess.run(
                    [sips, "-z", str(px), str(px), str(png),
                     "--out", str(out)],
                    capture_output=True, timeout=30)
                if done.returncode != 0:
                    return False
        done = subprocess.run(
            [iconutil, "-c", "icns", str(iconset), "-o", str(icns)],
            capture_output=True, timeout=30)
        return done.returncode == 0 and icns.is_file()


def build_bundle(repo: Path, dest: Path, identity: str = "-",
                 standalone: bool = False) -> Path:
    """Assemble `heygent.app` under dest and return its path.

    `identity` is what codesign signs with: "-" (ad-hoc) is enough for
    an app that stays on this Mac; a "Developer ID Application" identity
    (signed with the hardened runtime and a timestamp, ready for
    notarization) is what Gatekeeper accepts on other Macs.

    A `standalone` bundle carries the repo inside it instead of pointing
    at this checkout - the one to hand to someone else."""
    repo = repo.resolve()
    conduct = repo / "conduct.sh"
    if not conduct.is_file():
        raise FileNotFoundError(f"no conduct.sh under {repo}")
    app = dest / f"{APP_NAME}.app"
    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)

    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(info_plist(repo), handle)

    executable = macos / "heygent"
    script = resources / "heygent.sh"
    for stale in (executable, script, macos / "heygent.sh"):
        stale.unlink(missing_ok=True)
    if compile_stub(STUB.replace("{script}", script.name), executable):
        launcher = script
    else:
        launcher = executable
    payload = resources / "app"
    if standalone:
        copy_payload(repo, payload)
        launcher.write_text(STANDALONE_LAUNCHER.replace("{stamp}", STAMP))
    else:
        if payload.exists():
            shutil.rmtree(payload)
        launcher.write_text(LAUNCHER.format(conduct=conduct))
    launcher.chmod(0o755)

    make_icns(repo / "assets" / "icon.png",
              resources / "heygent.icns")

    # Without any signature Gatekeeper on Apple silicon refuses to
    # launch it.
    codesign = shutil.which("codesign")
    if codesign is not None:
        command = [codesign, "--force", "--deep"]
        if identity != "-":
            command += ["--options", "runtime", "--timestamp"]
        command += ["--sign", identity, str(app)]
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=600)
        if done.returncode != 0:
            raise RuntimeError(f"codesign failed: {done.stderr.strip()}")
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="build heygent.app around this repo")
    parser.add_argument("--install", action="store_true",
                        help="build into /Applications instead of ./dist")
    parser.add_argument("--dest", type=Path, default=None,
                        help="build into this directory")
    parser.add_argument("--sign", default="-", metavar="IDENTITY",
                        help="codesign identity (default: ad-hoc); give a "
                             "'Developer ID Application' identity to sign "
                             "for other Macs")
    parser.add_argument("--standalone", action="store_true",
                        help="carry a copy of the repo inside the bundle "
                             "instead of pointing at this checkout")
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parent.parent
    dest = args.dest or (Path("/Applications") if args.install
                         else repo / "dist")
    dest.mkdir(parents=True, exist_ok=True)
    app = build_bundle(repo, dest, identity=args.sign,
                       standalone=args.standalone)
    print(f"built {app}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
