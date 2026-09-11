"""The capability snapshot has to be probed, not declared.

A hardcoded list keeps claiming a capability for exactly as long as it
takes someone to remove it, so these tests take capabilities away and check
the snapshot notices. If a test here can be made to pass by editing a
constant, the snapshot is not doing its job.

Run with:  python3 -m unittest tests.test_capabilities -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor import capabilities
from conductor.capabilities import capability_block, snapshot
from conductor.global_conductor import GlobalConductor
from conductor.testing import FakeCodingAgentRuntime, FakeWorkspaceManager
from conductor.surfaces import FakeSurface


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.runtime = FakeCodingAgentRuntime()
        self.conductor = GlobalConductor(
            home=base / "home", runtime=self.runtime,
            search_roots=[base / "code"], surface=FakeSurface(),
            workspace_factory=lambda project: FakeWorkspaceManager())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def state(self, name: str) -> bool:
        return snapshot(self.conductor)[name][0]


class ProbedNotDeclared(Base):
    def test_a_full_runtime_reports_its_powers(self) -> None:
        for name in ("project discovery", "session creation",
                     "session messaging", "session focus",
                     "approval supervision",
                     "historical session search/resume"):
            self.assertTrue(self.state(name), f"{name} should be available")

    def test_removing_the_runtime_removes_the_session_powers(self) -> None:
        self.conductor.runtime = None
        self.assertFalse(self.state("session creation"))
        self.assertFalse(self.state("session messaging"))
        self.assertFalse(self.state("approval supervision"))
        # ...but history lives on the conductor, not the runtime.
        self.assertTrue(self.state("historical session search/resume"))

    def test_a_runtime_without_approvals_reports_no_supervision(self) -> None:
        self.runtime.pending_approvals = None   # shadows the method
        self.assertFalse(self.state("approval supervision"))
        self.assertTrue(self.state("session creation"))   # unaffected

    def test_no_surface_means_no_focus(self) -> None:
        self.conductor.surfaces = {}
        self.assertFalse(self.state("session focus"))

    def test_no_locator_means_no_discovery(self) -> None:
        self.conductor.locator = None
        self.assertFalse(self.state("project discovery"))


class ProvidersFollowThePath(Base):
    """A provider is available only if its binary exists AND the runtime
    drives it. Codex ships in PROVIDERS long before a Codex runtime does,
    so the snapshot must not confuse "known" with "usable"."""

    def test_no_binary_means_unavailable(self) -> None:
        with mock.patch.object(capabilities.shutil, "which",
                               return_value=None):
            ok, why = snapshot(self.conductor)["Claude Code"]
        self.assertFalse(ok)
        self.assertIn("PATH", why)

    def test_binary_present_but_runtime_does_not_drive_it(self) -> None:
        with mock.patch.object(capabilities.shutil, "which",
                               return_value="/usr/bin/codex"):
            ok, why = snapshot(self.conductor)["Codex"]
        self.assertFalse(ok)
        self.assertIn("does not drive", why)

    def test_a_runtime_that_declares_providers_is_believed(self) -> None:
        self.runtime.providers = ["claude-code", "codex"]
        with mock.patch.object(capabilities.shutil, "which",
                               return_value="/usr/bin/codex"):
            ok, _ = snapshot(self.conductor)["Codex"]
        self.assertTrue(ok)

    def test_cursor_answers_to_either_of_its_names(self) -> None:
        """The CLI installs as cursor-agent or as agent depending on the
        installer; the adapter accepts both, so the probe must too."""
        self.runtime.providers = ["claude-code", "cursor"]
        with mock.patch.object(
                capabilities.shutil, "which",
                side_effect=lambda b: "/usr/bin/agent" if b == "agent"
                else None):
            ok, _ = snapshot(self.conductor)["Cursor"]
        self.assertTrue(ok)

    def test_a_missing_cursor_names_both_binaries(self) -> None:
        with mock.patch.object(capabilities.shutil, "which",
                               return_value=None):
            _, why = snapshot(self.conductor)["Cursor"]
        self.assertIn("cursor-agent or agent", why)

    def test_the_reason_survives_into_the_rendered_block(self) -> None:
        """"not available" alone invites a retry; the reason is the part
        the Manager can actually tell the user."""
        with mock.patch.object(capabilities.shutil, "which",
                               return_value=None):
            block = capability_block(self.conductor)
        self.assertIn("Claude Code: not available (no claude on PATH)", block)


class RenderedShape(Base):
    def test_it_reads_as_the_documented_block(self) -> None:
        block = capability_block(self.conductor)
        self.assertTrue(block.startswith("Current capabilities:"))
        for name in ("project discovery", "Claude Code", "Codex",
                     "session creation", "session messaging",
                     "session focus", "approval supervision",
                     "historical session search/resume"):
            self.assertIn(f"- {name}: ", block)

    def test_every_line_says_available_or_why_not(self) -> None:
        for line in capability_block(self.conductor).splitlines()[1:]:
            self.assertTrue(line.endswith("available")
                            or ": not available (" in line
                            or ": needs " in line, line)

    def test_it_stays_small_enough_to_send_every_turn(self) -> None:
        self.assertLess(len(capability_block(self.conductor)), 600)


COMPUTER = "computer use (a worker driving this machine's GUI)"


def fake_driver(**permissions):
    driver = mock.Mock()
    driver.permissions.return_value = permissions
    driver.remedy = ("grant Accessibility and Screen Recording in System "
                     "Settings > Privacy & Security to the terminal app "
                     "that runs the workers and the conductor")
    return driver


class ComputerUseExplainsItsGrants(Base):
    """A missing grant is one Settings pane away, so the capability line
    must say what to turn on and where - never a flat "not available",
    which the Manager repeats to the user as a refusal."""

    def line(self) -> str:
        block = capability_block(self.conductor)
        return next(line for line in block.splitlines()
                    if line.startswith(f"- {COMPUTER}:"))

    def test_a_missing_grant_never_reads_as_unavailable(self) -> None:
        driver = fake_driver(accessibility=True, screen_recording=False)
        with mock.patch("conductor.computer.make_driver",
                        return_value=driver):
            line = self.line()
        self.assertNotIn("not available", line)
        self.assertIn("needs Screen Recording", line)
        self.assertIn("System Settings > Privacy & Security", line)
        self.assertIn("then ask again", line)

    def test_only_the_missing_grants_are_named(self) -> None:
        driver = fake_driver(accessibility=False, screen_recording=False)
        with mock.patch("conductor.computer.make_driver",
                        return_value=driver):
            line = self.line()
        self.assertIn("needs Accessibility and Screen Recording", line)

    def test_no_backend_at_all_is_still_not_available(self) -> None:
        from conductor import computer
        with mock.patch("conductor.computer.make_driver",
                        side_effect=computer.ComputerError(
                            "computer use is not supported on sunos5")):
            line = self.line()
        self.assertIn("not available (computer use is not supported", line)

    def test_every_grant_in_place_is_available(self) -> None:
        driver = fake_driver(accessibility=True, screen_recording=True)
        with mock.patch("conductor.computer.make_driver",
                        return_value=driver):
            self.assertEqual(self.line(), f"- {COMPUTER}: available")


if __name__ == "__main__":
    unittest.main()


class AnswersWhatItCanDo(unittest.TestCase):
    """Capabilities are answered on request, not announced.

    An unprompted introduction is noise on every turn it appears; what the
    user actually needs is a real answer when they ask, drawn from the
    probed snapshot rather than from the model's idea of what a coding
    manager generally does.
    """

    def prompt(self) -> str:
        from conductor.claude_manager import load_manager_prompt
        return load_manager_prompt()

    def test_the_prompt_says_to_answer_from_the_snapshot(self) -> None:
        p = self.prompt()
        self.assertIn("When they ask what you can do", p)
        self.assertIn("from the capability snapshot", p)

    def test_it_forbids_claiming_what_is_unavailable(self) -> None:
        self.assertIn("do not claim anything it says is unavailable",
                      self.prompt())

    def test_the_boss_is_the_gate_for_worker_updates(self) -> None:
        p = self.prompt()
        self.assertIn("## Worker updates", p)
        self.assertIn("tell_user", p)
        self.assertIn("plainly satisfies the task's goal", p)
        self.assertIn("What you say back to a worker update IS what the user hears", p)
        self.assertIn("note_for_voice is NOT heard", p)
        self.assertNotIn("nothing but the user saying so ever", p)

    def test_it_introduces_the_job_once_per_new_conversation(self) -> None:
        """v12's note promised a one-time greeting and its rule forbade it;
        the user opened a fresh conversation and was told 'not much'."""
        p = self.prompt()
        self.assertIn("The first thing you say in a conversation", p)
        self.assertIn("introduce the job once", p)
        self.assertIn("Never again in the same conversation", p)
        self.assertIn("never when they open with a request", p)
        self.assertNotIn("Do not introduce yourself unprompted", p)

    def test_no_first_exchange_plumbing_remains(self) -> None:
        """The signal was removed; nothing should still be feeding it."""
        from pathlib import Path
        src = Path("conductor/claude_manager.py").read_text()
        self.assertNotIn("first exchange since the app started", src)
        self.assertNotIn("_turns = 0", src)

    def test_the_prompt_states_the_role_and_the_reach(self) -> None:
        p = self.prompt()
        self.assertIn("You supervise coding agents", p)
        self.assertIn("Trust\nit over anything you assume", p)
        self.assertIn("explaining the manual steps", p)
