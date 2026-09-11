"""The app, when it is an app: identity, and how to start a script.

packaging/build_app.py builds `Voice Agent.app` with CPython and every
dependency inside it. Its executable, Contents/MacOS/Voice Agent, is
that interpreter (packaging/launcher.c), so ``sys.executable`` is the
tell: a process whose interpreter sits in an .app's Contents/MacOS is
running from the bundle.

That matters wherever the conductor starts another Python process - the
overlay, the hotkey tap, the Boss window, boss-mcp, the computer-use CLI
a worker runs. From a checkout each is ``uv run`` on the script, which
resolves its PEP 723 dependencies. In the app there is no uv and nothing
to resolve: the same executable runs the script, and because it is the
bundle's executable the child is the app too - one name and icon in the
Dock, one set of privacy grants.

Nothing here imports AppKit: the questions are about paths, and the
tests ask them on a machine with no bundle at all.
"""
from __future__ import annotations

import sys
from pathlib import Path

APP_NAME = "Voice Agent"
# Changing either resets every macOS privacy grant the app holds.
BUNDLE_ID = "ai.voice-agent.conductor"
EXECUTABLE = APP_NAME
PYTHON_VERSION = "3.13"


def bundle_root(executable: str | None = None) -> Path | None:
    """The ``.../Foo.app`` this interpreter is the executable of, or None."""
    exe = Path(sys.executable if executable is None else executable)
    macos = exe.parent
    if macos.name != "MacOS" or macos.parent.name != "Contents":
        return None
    app = macos.parent.parent
    return app if app.suffix == ".app" else None


def inside_bundle(executable: str | None = None) -> bool:
    return bundle_root(executable) is not None


def bundled_bin(executable: str | None = None) -> Path | None:
    """Contents/Resources/bin: the tmux and claude the app carries."""
    app = bundle_root(executable)
    return None if app is None else app / "Contents" / "Resources" / "bin"


def script_argv(script: str | Path, uv_argv: list[str],
                executable: str | None = None) -> list[str]:
    """How to run a script: this very executable in the app, otherwise
    the uv command the caller has always used (``uv_argv`` ends before
    the script path)."""
    if inside_bundle(executable):
        return [sys.executable if executable is None else executable,
                str(script)]
    return [*uv_argv, str(script)]
