"""CmuxSurface: one workspace per subagent, and never a second worker.

Built on what the spike measured rather than what the surface contract
assumes. The contract says create() attaches to an existing execution;
cmux cannot, because closing a workspace kills the process it hosts. What
survives is the SESSION, so attaching means resuming - proved end to end:
a workspace destroyed, rebuilt, and the conversation still there.

The invariant every test here defends: showing a worker again must never
start another one.

Run with:  python3 -m unittest tests.test_cmux_surface -v
"""

from __future__ import annotations

import unittest
from unittest import mock

from conductor.cmux_surface import CmuxSurface, CmuxUnavailableError
from conductor.surfaces import SurfaceHandle, SurfaceRequest

WORKSPACES = ("  workspace:2 WS-UUID  Posely · Fix login\n")
SURFACES = ("* surface:2 SF-UUID  Posely · Fix login  [selected]\n")


def request(session="prov-123", title="Posely · Fix login"):
    return SurfaceRequest(project_id="p", task_id="task_a", title=title,
                          working_directory="/w", provider="claude-code",
                          provider_session_id=session)


class Base(unittest.TestCase):
    def surface(self, outputs=None, exists=False):
        """A fake cmux. `exists` says whether the workspace is already
        there - the difference between showing a worker and creating one,
        which is the distinction most of these tests are about."""
        s = CmuxSurface(binary="/fake/cmux")
        self.calls = []
        script = list(outputs or [])
        state = {"made": exists, "title": "Posely · Fix login"}

        def run(*args):
            self.calls.append(args)
            if script:
                return script.pop(0)
            if args[0] == "new-workspace":
                state["made"] = True
                state["title"] = args[args.index("--name") + 1]
                return "OK workspace:2"
            if args[0] == "list-workspaces":
                return (f"  workspace:2 WS-UUID  {state['title']}\n"
                        if state["made"] else "")
            if args[0] == "list-pane-surfaces":
                return SURFACES
            if args[0] == "list-windows":
                return "* 0: WIN-UUID selected_workspace=WS-UUID workspaces=1"
            return "OK"
        s._run = run
        return s


class ShowingWorkNeverStartsIt(Base):
    def test_it_resumes_rather_than_launching(self):
        """A fresh `claude` would be a second worker wearing the first
        one's name. Only --resume continues the same session."""
        s = self.surface()
        s.create(request())
        sent = [a for a in self.calls if a[0] == "send"]
        self.assertTrue(sent)
        command = sent[0][-1]
        self.assertIn("--resume", command)
        self.assertIn("prov-123", command)

    def test_no_session_is_refused_rather_than_started(self):
        """This surface shows existing work. Without a session there is
        nothing to show, and starting one would invent a worker."""
        s = self.surface()
        with self.assertRaises(RuntimeError) as caught:
            s.create(request(session=None))
        self.assertIn("no provider session", str(caught.exception))

    def test_a_second_create_returns_the_first_workspace(self):
        """Section 30: ten follow-ups, one workspace."""
        s = self.surface()
        first = s.create(request())
        made_before = len([a for a in self.calls if a[0] == "new-workspace"])
        second = s.create(request())
        made_after = len([a for a in self.calls if a[0] == "new-workspace"])
        self.assertEqual(first.metadata["workspace_id"],
                         second.metadata["workspace_id"])
        self.assertEqual(made_before, made_after,
                         "showing it again made a second workspace")

    def test_the_stale_key_is_dropped_from_the_worker(self):
        s = self.surface()
        s.create(request())
        command = [a for a in self.calls if a[0] == "send"][0][-1]
        self.assertIn("env -u ANTHROPIC_API_KEY", command)


class IdentityTravelsByIdNotTitle(Base):
    def test_the_title_is_what_a_person_reads(self):
        s = self.surface()
        handle = s.create(request())
        self.assertEqual(handle.metadata["title"], "Posely · Fix login")

    def test_the_handle_carries_stable_ids(self):
        s = self.surface()
        handle = s.create(request())
        self.assertEqual(handle.metadata["workspace_id"], "WS-UUID")
        self.assertEqual(handle.metadata["surface_id"], "SF-UUID")

    def test_focus_targets_the_ids_not_whatever_is_frontmost(self):
        s = self.surface()
        raised = []
        s._raise_app = lambda: raised.append(True)
        s.focus(SurfaceHandle(type="cmux-workspace", application="cmux",
                              native_window_id="WS-UUID",
                              metadata={"workspace_id": "WS-UUID",
                                        "surface_id": "SF-UUID"}))
        self.assertIn(("select-workspace", "--workspace", "WS-UUID"),
                      self.calls)
        # Measured: select-workspace alone leaves cmux behind whatever
        # app is in front, so the click looks like it did nothing.
        # focus-window activates it; focus-pane does not.
        self.assertIn(("focus-window", "--window", "WIN-UUID"), self.calls)
        self.assertTrue(raised, "focus-window alone does not always activate")

    def test_focus_raises_the_app_even_with_no_window_to_target(self):
        """Better to raise cmux showing the right workspace than to
        select it behind another app and look like nothing happened."""
        s = self.surface()
        raised = []
        s._raise_app = lambda: raised.append(True)
        real = s._run
        s._run = lambda *a: "" if a[0] == "list-windows" else real(*a)
        s.focus(SurfaceHandle(type="cmux-workspace", application="cmux",
                              native_window_id="WS-UUID",
                              metadata={"workspace_id": "WS-UUID"}))
        self.assertTrue(raised)

    def test_a_task_with_no_title_still_gets_a_name(self):
        s = self.surface()
        handle = s.create(request(title=""))
        self.assertIn("task_a", handle.metadata["title"])


class SurfaceHealthIsNotProviderHealth(Base):
    def handle(self):
        return SurfaceHandle(type="cmux-workspace", application="cmux",
                             native_window_id="WS-UUID",
                             metadata={"workspace_id": "WS-UUID",
                                       "surface_id": "SF-UUID"})

    def test_a_present_surface_is_available(self):
        self.assertTrue(self.surface(exists=True).is_available(self.handle()))

    def test_a_missing_surface_is_not(self):
        s = self.surface(outputs=["  surface:9 OTHER  something else\n"])
        self.assertFalse(s.is_available(self.handle()))

    def test_an_unreachable_cmux_is_not_available_and_not_a_crash(self):
        s = self.surface()
        s._run = mock.Mock(side_effect=CmuxUnavailableError("not running"))
        self.assertFalse(s.is_available(self.handle()))

    def test_recover_reuses_a_surface_that_is_still_there(self):
        """Rebuilding a window that exists would abandon the worker in it."""
        s = self.surface(exists=True)
        handle = self.handle()
        recovered = s.recover(request(), handle)
        self.assertIs(recovered, handle)
        self.assertNotIn("new-workspace", [a[0] for a in self.calls])

    def test_recover_rebuilds_by_resuming_when_the_surface_is_gone(self):
        """The process died with its workspace, so recovery is a resume.
        Verified end to end: destroyed, rebuilt, conversation intact."""
        s = self.surface(outputs=["  surface:9 OTHER  gone\n"])
        s.recover(request(), self.handle())
        command = [a for a in self.calls if a[0] == "send"][0][-1]
        self.assertIn("--resume", command)
        self.assertIn("prov-123", command)

    def test_closing_something_already_gone_is_not_an_error(self):
        s = self.surface()
        s._run = mock.Mock(side_effect=RuntimeError("no such workspace"))
        s.close(self.handle())          # must not raise


class WhenCmuxIsNotThere(unittest.TestCase):
    def test_no_binary_is_unavailable_not_a_mystery(self):
        with mock.patch("conductor.cmux_surface.find_executable",
                        return_value=None):
            s = CmuxSurface()
        with self.assertRaises(CmuxUnavailableError):
            s._run("ping")

    def test_the_surface_says_input_reaches_the_worker(self):
        """It hosts the real process, so it must not be mistaken for a
        read-only view."""
        self.assertTrue(CmuxSurface(binary="/fake/cmux").interactive)


if __name__ == "__main__":
    unittest.main()
