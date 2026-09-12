"""Where the OpenAI key lives on this Mac, and where else it might already be.

The key is kept in the login keychain as a generic password (service
`heygent`, account `OPENAI_API_KEY`) rather than in a .env file next to
the code: the code directory is replaced by every update, and a file of
secrets is what Keychain exists to avoid. .env stays as the fallback for
a Mac without `security` and for other platforms.

A first run also looks where a developer already keeps the key. A Finder
launch does not run the user's shell profile, so an `export
OPENAI_API_KEY=...` in ~/.zshrc is invisible to the app until it is asked
for from the login shell itself.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

from .observability import application_log

SERVICE = "heygent"
ACCOUNT = "OPENAI_API_KEY"
_FENCE = "@@heygent@@"


def available(platform: str | None = None) -> bool:
    return (platform or sys.platform) == "darwin"


def _security(*args: str, run=subprocess.run,
              **kwargs) -> subprocess.CompletedProcess | None:
    try:
        return run(["security", *args], capture_output=True, text=True,
                   timeout=30, **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        application_log("conductor", "keystore.failed",
                        "the security tool could not be run",
                        severity="warning", exc_info=True)
        return None


def load(*, run=subprocess.run) -> str:
    """The saved key, "" when there is none."""
    if not available():
        return ""
    done = _security("find-generic-password", "-s", SERVICE, "-a", ACCOUNT,
                     "-w", run=run)
    if done is None or done.returncode != 0:
        return ""
    return done.stdout.strip()


def save(key: str, *, run=subprocess.run) -> bool:
    """Keep the key; False when the keychain would not take it (the .env
    fallback is then the caller's)."""
    if not available() or not key:
        return False
    done = _security("add-generic-password", "-U", "-s", SERVICE,
                     "-a", ACCOUNT, "-l", "heygent - OpenAI API key",
                     "-w", key, run=run)
    return done is not None and done.returncode == 0


def forget(*, run=subprocess.run) -> None:
    if not available():
        return
    _security("delete-generic-password", "-s", SERVICE, "-a", ACCOUNT,
              run=run)


def from_login_shell(*, run=subprocess.run, shell: str | None = None) -> str:
    """OPENAI_API_KEY as the user's interactive login shell sees it -
    what ~/.zshrc, ~/.zprofile or ~/.bash_profile export - or ""."""
    shell = shell or os.environ.get("SHELL") or ""
    if not shell:
        return ""
    # A profile that prints a banner puts it on stdout too, so the value
    # is fenced.
    try:
        done = run([shell, "-lic", f'printf "{_FENCE}%s{_FENCE}" "${ACCOUNT}"'],
                   capture_output=True, text=True, timeout=20,
                   stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        application_log("conductor", "keystore.shell_failed",
                        f"{shell} did not answer for {ACCOUNT}",
                        severity="warning", exc_info=True)
        return ""
    found = re.search(f"{_FENCE}(.*?){_FENCE}", done.stdout, re.S)
    return found.group(1).strip() if found else ""
