"""The app's first run: the OpenAI key, the macOS grants, Claude Code.

From a terminal, conduct.sh asked for the key on stdin and printed which
Settings panes to open. Opened from Finder there is no terminal, so the
app shows a window first (conductor/app_setup_window.py) and this module
is everything that window knows - kept free of AppKit so the tests can
ask it everything on any machine.

What the app needs before its first voice session:

- an OpenAI key that can open gpt-live-1, checked against the API and
  kept in ~/.voice-conductor/.env (0600) - never inside the bundle, which
  is signed and read-only;
- Microphone and Input Monitoring, without which it never hears you or
  sees the Fn key, and optionally Accessibility and Screen Recording for
  tasks allowed to use the Mac;
- Claude Code signed in: the Boss and every worker are Claude Code
  sessions. The app carries a claude of its own for a Mac without one,
  but the sign-in is the user's.

A grant's answer is read in a fresh process each time (`--probe`): in a
long-lived one macOS keeps answering what it answered first, so a switch
flipped in System Settings would never show as done.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from . import app_bundle

SETUP_FILE = "app-setup.json"
ENV_FILE = ".env"
KEY_NAME = "OPENAI_API_KEY"
KEYS_PAGE = "https://platform.openai.com/api-keys"
API_BASE = "https://api.openai.com/v1"
SETUP_VERSION = 1


@dataclass(frozen=True)
class Grant:
    id: str
    label: str
    purpose: str
    required: bool
    pane: str


_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_"
GRANTS = (
    Grant("microphone", "Microphone", "Hear you while you hold the key.",
          True, _PANE + "Microphone"),
    Grant("input_monitoring", "Input Monitoring",
          "See the push-to-talk key (Fn) from any app.",
          True, _PANE + "ListenEvent"),
    Grant("accessibility", "Accessibility",
          "Click and type, for tasks you let use this Mac.",
          False, _PANE + "Accessibility"),
    Grant("screen_recording", "Screen Recording",
          "See the screen, for tasks you let use this Mac.",
          False, _PANE + "ScreenCapture"),
)
GRANTS_BY_ID = {grant.id: grant for grant in GRANTS}


# -- the key --------------------------------------------------------------

def env_path(home: Path) -> Path:
    return Path(home) / ENV_FILE


def read_key(home: Path) -> str:
    path = env_path(home)
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        name, _, value = line.strip().partition("=")
        if name.strip() == KEY_NAME:
            return value.strip().strip('"').strip("'")
    return ""


def save_key(home: Path, key: str) -> Path:
    """Replace the key line, keep every other line, readable by the user
    alone. Written whole to a temporary file first, so a crash mid-write
    never leaves half a key."""
    path = env_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        kept = [line for line in path.read_text().splitlines()
                if line.strip().partition("=")[0].strip() != KEY_NAME]
    except OSError:
        kept = []
    kept.append(f"{KEY_NAME}={key}")
    partial = path.with_name(path.name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write("\n".join(kept) + "\n")
    os.replace(partial, path)
    os.chmod(path, 0o600)
    return path


@dataclass
class KeyCheck:
    ok: bool
    # False only when OpenAI could not be reached: the key is unjudged,
    # and saving it anyway is the user's call.
    reached: bool
    message: str


def check_key(key: str, model: str, *, base: str = API_BASE,
              opener=urllib.request.urlopen, timeout: float = 15.0
              ) -> KeyCheck:
    """Whether this key can open the voice model, asked of the API the
    cheapest way there is: reading the model's own record."""
    key = key.strip()
    if not key.startswith("sk-") or len(key) < 20 or any(
            ch.isspace() for ch in key):
        return KeyCheck(False, True,
                        "That doesn't look like an OpenAI key - they start "
                        "with sk-.")
    request = urllib.request.Request(
        f"{base.rstrip('/')}/models/{model}",
        headers={"Authorization": f"Bearer {key}"})
    try:
        with opener(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        return KeyCheck(False, False,
                        f"Couldn't reach OpenAI to check the key ({reason}).")
    if status == 200:
        return KeyCheck(True, True, f"The key works, with access to {model}.")
    if status == 401:
        return KeyCheck(False, True, "OpenAI didn't accept this key.")
    if status in (403, 404):
        return KeyCheck(False, True,
                        f"The key works, but it has no access to {model}, "
                        "the voice model this app talks through.")
    return KeyCheck(False, True, f"OpenAI answered {status}; try again.")


# -- the grants -----------------------------------------------------------

def probe_grants() -> dict[str, str]:
    """This process's app's answer for each grant, never prompting:
    "granted"; "denied" (the user said no - only the pane can change it);
    "missing" (not granted, and macOS won't say whether it ever asked);
    "unknown" (not asked yet, or unknowable here)."""
    answers = {grant.id: "unknown" for grant in GRANTS}
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
        status = AVCaptureDevice.authorizationStatusForMediaType_(
            AVMediaTypeAudio)
        answers["microphone"] = {3: "granted", 2: "denied",
                                 1: "denied"}.get(status, "unknown")
    except Exception:
        pass
    try:
        import Quartz
        answers["input_monitoring"] = (
            "granted" if Quartz.CGPreflightListenEventAccess() else "missing")
        answers["screen_recording"] = (
            "granted" if Quartz.CGPreflightScreenCaptureAccess() else "missing")
    except Exception:
        pass
    try:
        from ApplicationServices import AXIsProcessTrusted
        answers["accessibility"] = (
            "granted" if AXIsProcessTrusted() else "missing")
    except Exception:
        pass
    return answers


def probe_in_child(run=subprocess.run, timeout: float = 20.0
                   ) -> dict[str, str]:
    """probe_grants() in a new process of this same app."""
    try:
        done = run([sys.executable, "-m", "conductor.app_setup", "--probe"],
                   capture_output=True, text=True, timeout=timeout)
        answers = json.loads(done.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {grant.id: "unknown" for grant in GRANTS}
    return {grant.id: str(answers.get(grant.id, "unknown"))
            for grant in GRANTS}


# -- Claude Code ----------------------------------------------------------

def claude_status(which=shutil.which, run=subprocess.run) -> dict:
    """Which claude the app will use, and whether it is signed in."""
    path = which("claude") or ""
    bundled = app_bundle.bundled_bin()
    status = {"path": path,
              "bundled": bool(path and bundled
                              and Path(path).parent == bundled),
              "signed_in": None, "method": ""}
    if not path:
        return status
    try:
        done = run([path, "auth", "status", "--json"], capture_output=True,
                   text=True, timeout=30)
        answer = json.loads(done.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return status
    status["signed_in"] = bool(answer.get("loggedIn"))
    status["method"] = str(answer.get("authMethod") or "")
    return status


# -- the whole of it ------------------------------------------------------

def completed(home: Path) -> bool:
    try:
        state = json.loads((Path(home) / SETUP_FILE).read_text())
    except (OSError, ValueError):
        return False
    return state.get("version", 0) >= SETUP_VERSION


def needs_setup(home: Path) -> bool:
    """The window is shown until a key is saved and the user has once
    pressed Start; after that only a lost key brings it back."""
    return not read_key(home) or not completed(home)


def mark_completed(home: Path, grants: dict[str, str]) -> None:
    path = Path(home) / SETUP_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": SETUP_VERSION,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "grants": grants}, indent=2) + "\n")


def state(home: Path, grants: dict[str, str], claude: dict,
          key_message: str = "", key_ok: bool | None = None) -> dict:
    """Everything the setup page draws, as one JSON-able dict."""
    key = read_key(home)
    rows = []
    for grant in GRANTS:
        rows.append({**asdict(grant), "status": grants.get(grant.id,
                                                           "unknown")})
    blocking = []
    if not key:
        blocking.append("key")
    if claude.get("signed_in") is False or not claude.get("path"):
        blocking.append("claude")
    return {
        "app": app_bundle.APP_NAME,
        "key": {"saved": bool(key),
                "hint": f"sk-…{key[-4:]}" if key else "",
                "message": key_message, "ok": key_ok},
        "grants": rows,
        "claude": claude,
        "blocking": blocking,
        # Grants are not blocking: a stale "denied" (Input Monitoring only
        # reaches new processes) must never lock the user out of Start.
        "missing_required": [row["id"] for row in rows
                             if row["required"] and row["status"] != "granted"],
    }


class SetupController:
    """The page's other half: each message it posts, handled; each change,
    pushed back as one whole state. Slow work (the key check, a probe,
    the Claude sign-in) runs off the caller's thread, so the window never
    beachballs; `push` must be safe to call from any thread.

    The system prompts themselves (`request`) and opening a URL (`open`)
    are passed in, so the window gives it AppKit's and a test gives it
    fakes that record."""

    def __init__(self, home: Path, *, model: str, push, request, open_url,
                 finish, api_base: str = API_BASE, probe=probe_in_child,
                 claude=claude_status, key_checker=check_key,
                 spawn=subprocess.Popen, background=None,
                 on_ready=None) -> None:
        self.home = Path(home)
        self.model = model
        self.push = push
        self.request = request
        self.open_url = open_url
        self.finish = finish
        self.api_base = api_base
        self.probe = probe
        self.claude = claude
        self.key_checker = key_checker
        self.spawn = spawn
        self.background = background or (
            lambda work: threading.Thread(target=work, daemon=True).start())
        # What the page's first "ready" starts: by default one poll; the
        # window starts its polling timer instead.
        self.on_ready = on_ready or (lambda: self.background(self.poll))
        self.started = False
        self.grants = {grant.id: "unknown" for grant in GRANTS}
        self.claude_state: dict = {"path": "", "signed_in": None}
        self.key_message = ""
        self.key_ok: bool | None = None
        self.key_unreached = False
        self.checking = False
        self.asked: list[str] = []
        self.login: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def snapshot(self) -> dict:
        with self.lock:
            view = state(self.home, self.grants, dict(self.claude_state),
                         self.key_message, self.key_ok)
            view["key"]["unreached"] = self.key_unreached
            view["key"]["checking"] = self.checking
            view["claude"]["signing_in"] = (self.login is not None
                                            and self.login.poll() is None)
            view["asked"] = list(self.asked)
        return view

    def refresh(self) -> None:
        self.push(self.snapshot())

    def poll(self) -> None:
        """Grants and Claude re-read; called on a timer by the window."""
        grants = self.probe()
        claude = self.claude()
        with self.lock:
            self.grants = grants
            self.claude_state = claude
        self.refresh()

    def handle(self, message: dict) -> None:
        action = message.get("action")
        if action == "ready":
            self.refresh()
            if not self.started:
                self.started = True
                self.on_ready()
        elif action == "check_key":
            self._check_key(str(message.get("key", "")))
        elif action == "save_key":
            key = str(message.get("key", "")).strip()
            if key:
                save_key(self.home, key)
                with self.lock:
                    self.key_message = "Saved without checking."
                    self.key_ok, self.key_unreached = None, False
            self.refresh()
        elif action == "change_key":
            with self.lock:
                self.key_message, self.key_ok = "", None
                self.key_unreached = False
            self.refresh()
        elif action == "grant":
            self._ask_grant(str(message.get("grant", "")))
        elif action == "claude_login":
            self._claude_login()
        elif action == "open" and message.get("what") == "keys":
            self.open_url(KEYS_PAGE)
        elif action == "start":
            snapshot = self.snapshot()
            if not snapshot["blocking"]:
                mark_completed(self.home, dict(self.grants))
                # The window was the ask, pressed or not: the conductor
                # opens no pane for a grant the user saw and passed over.
                remember_asks(self.home, [
                    grant for grant, status in self.grants.items()
                    if status != "granted"])
                self.finish(0)
            else:
                self.refresh()

    def _check_key(self, key: str) -> None:
        with self.lock:
            self.checking = True
            self.key_message = "Checking with OpenAI…"
            self.key_ok, self.key_unreached = None, False
        self.refresh()

        def work() -> None:
            result = self.key_checker(key, self.model, base=self.api_base)
            if result.ok:
                save_key(self.home, key.strip())
            with self.lock:
                self.checking = False
                self.key_message, self.key_ok = result.message, (
                    result.ok if result.reached else None)
                self.key_unreached = not result.reached
            self.refresh()
        self.background(work)

    def _ask_grant(self, grant_id: str) -> None:
        grant = GRANTS_BY_ID.get(grant_id)
        if grant is None:
            return
        # macOS shows its own prompt once; after an answer, only the pane
        # can change it. A second press means the prompt did not do it.
        again = grant_id in self.asked
        if grant_id not in self.asked:
            self.asked.append(grant_id)
        if again or self.grants.get(grant_id) == "denied" \
                or not self.request(grant_id):
            self.open_url(grant.pane)
        self.refresh()
        self.background(self.poll)

    def _claude_login(self) -> None:
        path = self.claude_state.get("path")
        if not path or (self.login is not None and self.login.poll() is None):
            return
        self.login = self.spawn([path, "auth", "login"],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.refresh()

        def wait() -> None:
            try:
                self.login.wait(timeout=600)
            except subprocess.TimeoutExpired:
                self.login.kill()
            self.poll()
        self.background(wait)


def remember_asks(home: Path, asked: list[str]) -> None:
    """Grants shown here are not asked for again by the conductor's own
    startup check (gui_permissions), which would otherwise open their
    Settings panes one after another right after Start."""
    from . import gui_permissions
    path = Path(home) / gui_permissions.STATE_FILE
    known, introduced = gui_permissions._load(path)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    for grant in asked:
        known.setdefault(grant, stamp)
    gui_permissions._save(path, known, introduced
                          | set(gui_permissions.PANES))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--probe"]:
        print(json.dumps(probe_grants()))
        return 0
    print("usage: python -m conductor.app_setup --probe", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
