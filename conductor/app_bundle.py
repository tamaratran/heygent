"""Heygent as a real macOS application.

Builds `Heygent.app` - the bundle Finder, the Dock and Spotlight
know - around the repo's own launcher. The bundle is a shell, not a
copy: its executable execs `conduct.sh` where the repo lives, so a
`git pull` updates the app with no rebuild. What the bundle adds is
what only a bundle can have: an identity (bundle id, name, icon) that
macOS attaches the microphone, notification and automation permissions
to, instead of "Python".

Build it:

    python3 -m conductor.app_bundle              # ./dist/Heygent.app
    python3 -m conductor.app_bundle --install    # /Applications
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

APP_NAME = "Heygent"
BUNDLE_ID = "ai.voice-agent.conductor"

# Finder launches with almost no PATH; the launcher restores the places
# conduct.sh's tools (uv, tmux, claude) actually live.
LAUNCHER = """#!/bin/bash
# The app's executable: hand straight to the repo's launcher, so a
# `git pull` there updates the app with no rebuild.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
LOG_DIR="$HOME/.voice-conductor/logs"
mkdir -p "$LOG_DIR"
exec "{conduct}" >>"$LOG_DIR/app-launch.log" 2>&1
"""


def info_plist(repo: Path) -> dict:
    return {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": "voice-agent",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "CFBundleIconFile": "voice-agent.icns",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription":
            "Heygent listens while you hold the push-to-talk key.",
        "NSAppleEventsUsageDescription":
            "Heygent raises its own window and terminal sessions.",
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
        iconset = Path(scratch) / "voice-agent.iconset"
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


def build_bundle(repo: Path, dest: Path) -> Path:
    """Assemble `Heygent.app` under dest and return its path."""
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

    launcher = macos / "voice-agent"
    launcher.write_text(LAUNCHER.format(conduct=conduct))
    launcher.chmod(0o755)

    make_icns(repo / "assets" / "icon.png",
              resources / "voice-agent.icns")

    # An ad-hoc signature is enough for a local app; without any
    # signature Gatekeeper on Apple silicon refuses to launch it.
    codesign = shutil.which("codesign")
    if codesign is not None:
        subprocess.run([codesign, "--force", "--deep", "--sign", "-",
                        str(app)], capture_output=True, timeout=60)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="build Heygent.app around this repo")
    parser.add_argument("--install", action="store_true",
                        help="build into /Applications instead of ./dist")
    parser.add_argument("--dest", type=Path, default=None,
                        help="build into this directory")
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parent.parent
    dest = args.dest or (Path("/Applications") if args.install
                         else repo / "dist")
    dest.mkdir(parents=True, exist_ok=True)
    app = build_bundle(repo, dest)
    print(f"built {app}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
