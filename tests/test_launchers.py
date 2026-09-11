"""The launchers, and the one environment variable that breaks them.

Claude Code prefers ANTHROPIC_API_KEY over the stored claude.ai login when
one is set. So a stale key does not degrade, it fails - and it fails as a
hang rather than an auth error, because the CLI sits waiting on a TTY it
does not have to ask about signing in. That is what left main.py hanging
for ninety seconds with no output.

conduct.sh has stripped it from the start, and it must keep doing so.

Run with:  python3 -m unittest tests.test_launchers -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STRIP = "env -u ANTHROPIC_API_KEY"


def script(name: str) -> str:
    return (ROOT / name).read_text()


class LaunchersThatSpawnClaude(unittest.TestCase):
    """Every launcher that reaches Claude Code must drop the key first."""

    CLAUDE_LAUNCHERS = ("conduct.sh",)

    def test_they_strip_the_key(self) -> None:
        for name in self.CLAUDE_LAUNCHERS:
            self.assertIn(STRIP, script(name), f"{name} does not strip it")

    def test_they_strip_it_on_the_line_that_execs(self) -> None:
        """Mentioning it in a comment is not stripping it."""
        for name in self.CLAUDE_LAUNCHERS:
            execs = [ln for ln in script(name).splitlines()
                     if ln.strip().startswith("exec ")]
            self.assertTrue(execs, f"{name} has no exec line")
            self.assertIn(STRIP, execs[0], f"{name} strips it too late")

    def test_conduct_sh_strips_the_child_markers_the_runtimes_strip(self):
        """The conductor is usually restarted from inside a Claude Code
        session, and inherits that session's child markers. The runtimes
        scrub them before launching a worker; the launcher scrubs them
        before anything else can inherit them. One list, so the two
        cannot drift apart again."""
        from conductor.tmux_runtime import CHILD_MARKERS
        lines = script("conduct.sh").splitlines()
        start = next(i for i, ln in enumerate(lines)
                     if ln.strip().startswith("exec "))
        # The exec spans continuation lines; read the whole command.
        block, i = [], start
        while i < len(lines):
            block.append(lines[i].rstrip("\\").strip())
            if not lines[i].rstrip().endswith("\\"):
                break
            i += 1
        command = " ".join(block)
        for marker in CHILD_MARKERS:
            self.assertIn(f"-u {marker}", command,
                          f"conduct.sh does not strip {marker}")

    def test_the_targets_really_do_spawn_claude(self) -> None:
        """If one stops using Claude, this list should shrink rather than
        keep asserting something pointless."""
        targets = {"conduct.sh": "conduct.py"}
        for launcher, target in targets.items():
            source = (ROOT / target).read_text()
            self.assertTrue(
                "claude_agent_sdk" in source or "claude" in source.lower(),
                f"{target} no longer uses Claude; drop it from the list")


class TheOnlyLauncher(unittest.TestCase):
    def test_conduct_sh_is_the_only_launcher(self) -> None:
        """One entry point: a second .sh launcher at the root is the
        confusion this repo already removed once (talk.sh)."""
        launchers = sorted(p.name for p in ROOT.glob("*.sh")
                           if p.name != "install.sh")
        self.assertEqual(launchers, ["conduct.sh"])

    def test_the_installer_points_at_it(self) -> None:
        installer = script("install.sh")
        self.assertIn("conduct.sh", installer)
        self.assertNotIn("talk.sh", installer)


if __name__ == "__main__":
    unittest.main()
