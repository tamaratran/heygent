"""The sidebar answers "where did the Boss route that to?".

WorkspaceDecor follows the lifecycle events the bus already carries and
dresses workers' cmux workspaces: the task's own words for a title, a
colour that says how it is going, a flash on the workspace words were
just routed into. Identity stays in the description stamp, so none of
this can lose a worker - these tests cover only what is painted.
"""

import unittest
from unittest import mock

from conductor.observability import ObservabilityBus, ObservabilityEvent
from conductor.workspace_decor import WorkspaceDecor


class FakeRuntime:
    def __init__(self, found=True, found_after=0):
        self.found = found
        self.found_after = found_after
        self.dressed = []

    def dress(self, name, title=None, state=None, flash=False, pin=False,
              status=None):
        self.dressed.append((name, title, state, flash, pin, status))
        if self.found_after:
            self.found_after -= 1
            return self.found_after == 0
        return self.found


class FakeConductor:
    def __init__(self):
        self.bus = ObservabilityBus()
        task = mock.Mock(project_id="proj_1")
        task.title = "Fix login flake"
        self.tasks = {"task_a": task}
        project = mock.Mock()
        project.display_name = "posely"
        self.projects = mock.Mock()
        self.projects.get = lambda pid: project if pid == "proj_1" else None

    def _find_task(self, task_id):
        return None, self.tasks[task_id]


class Painting(unittest.TestCase):
    def decor(self, found=True, found_after=0):
        self.conductor = FakeConductor()
        self.runtime = FakeRuntime(found=found, found_after=found_after)
        for patcher in (mock.patch("conductor.workspace_decor.time.strftime",
                                   return_value="14:32"),
                        mock.patch.object(WorkspaceDecor, "RETRY_DELAY", 0)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return WorkspaceDecor(self.conductor, self.runtime)

    def emit(self, decor, type_, task_id="task_a"):
        decor.handle_event(ObservabilityEvent(
            type=type_, component="task", task_id=task_id))

    def worker_jobs(self, name="cond_task_a"):
        return [d for d in self.runtime.dressed if d[0] == name]

    def boss_jobs(self):
        return [d for d in self.runtime.dressed if d[0] == "cond_boss"]

    def test_a_new_task_gets_its_own_words_and_the_working_colour(self):
        decor = self.decor()
        self.emit(decor, "task.created")
        decor.close()
        self.assertEqual(self.worker_jobs(), [
            ("cond_task_a", "posely: Fix login flake", "working", False,
             False, "\u25c0 Boss 14:32")])

    def test_routed_words_flash_the_workspace(self):
        decor = self.decor()
        self.emit(decor, "task.created")
        self.emit(decor, "task.message_sent")
        decor.close()
        flashed = [d for d in self.runtime.dressed if d[3]]
        self.assertEqual(len(flashed), 1)
        self.assertEqual(flashed[0][0], "cond_task_a")

    def test_the_colour_follows_the_task(self):
        decor = self.decor()
        for event, state in (("task.approval_required", "attention"),
                             ("approval.resolved", "working"),
                             ("task.completed", "done"),
                             ("task.failed", "failed")):
            self.emit(decor, event)
        decor.close()
        self.assertEqual([d[2] for d in self.worker_jobs()],
                         ["attention", "working", "done", "failed"])

    def test_the_title_is_set_once_then_left_to_the_user(self):
        """The user may rename a workspace; re-stamping the title on
        every event would fight them for it."""
        decor = self.decor()
        self.emit(decor, "task.created")
        self.emit(decor, "task.completed")
        decor.close()
        titles = [d[1] for d in self.worker_jobs()]
        self.assertEqual(titles, ["posely: Fix login flake", None])

    def test_an_undressed_workspace_is_titled_on_the_next_event(self):
        """dress() finding no workspace (still being created, or cmux
        momentarily gone) must not burn the one chance at a title."""
        decor = self.decor(found=False)
        self.emit(decor, "task.created")
        self.emit(decor, "task.completed")
        decor.close()
        titles = {d[1] for d in self.worker_jobs()}
        self.assertEqual(titles, {"posely: Fix login flake"})

    def test_a_workspace_still_being_created_is_dressed_by_retry(self):
        """Live, a fresh task's workspace lists ~5s after task.created;
        the first dress waits for it rather than losing the spinner and
        title until the next event."""
        decor = self.decor(found_after=3)
        self.emit(decor, "task.created")
        decor.close()
        jobs = self.worker_jobs()
        self.assertEqual(len(jobs), 3)
        self.assertEqual({d[1] for d in jobs}, {"posely: Fix login flake"})

    def test_an_unknown_task_is_painted_without_a_title(self):
        decor = self.decor()
        self.emit(decor, "task.created", task_id="task_zz")
        decor.close()
        self.assertEqual(self.worker_jobs("cond_task_zz"),
                         [("cond_task_zz", None, "working", False, False,
                           "\u25c0 Boss 14:32")])
        self.assertEqual([d[5] for d in self.boss_jobs()],
                         ["\u25b6 worker 14:32"])

    def test_the_loop_is_pinned_on_both_ends(self):
        """Routing in stamps both workspaces with the hour; the worker
        handing the ball back restamps both with the return."""
        decor = self.decor()
        self.emit(decor, "task.created")
        self.emit(decor, "task.completed")
        decor.close()
        self.assertEqual([d[5] for d in self.worker_jobs()],
                         ["\u25c0 Boss 14:32", "\u25b6 Boss 14:32"])
        self.assertEqual([d[5] for d in self.boss_jobs()],
                         ["\u25b6 Fix login flake 14:32",
                          "\u25c0 Fix login flake 14:32"])
        # The Boss's own workspace keeps its title and colour: the pill
        # is the only thing a worker's event may touch on it.
        self.assertEqual([(d[1], d[2]) for d in self.boss_jobs()],
                         [(None, None), (None, None)])

    def test_the_bosses_own_events_route_nothing(self):
        decor = self.decor()
        self.emit(decor, "task.created", task_id="boss")
        decor.close()
        self.assertEqual(self.runtime.dressed, [])

    def test_events_that_say_nothing_visual_are_ignored(self):
        decor = self.decor()
        self.emit(decor, "task.progress")
        self.emit(decor, "runtime.progress")
        decor.handle_event(ObservabilityEvent(
            type="task.created", component="task", task_id=None))
        decor.close()
        self.assertEqual(self.runtime.dressed, [])

    def test_a_failing_dress_never_reaches_the_bus(self):
        decor = self.decor()
        self.runtime.dress = mock.Mock(side_effect=RuntimeError("boom"))
        with mock.patch("conductor.workspace_decor.application_log") as log:
            self.emit(decor, "task.created")
            decor.close()
        self.assertTrue(log.called)

    def test_close_unsubscribes(self):
        decor = self.decor()
        decor.close()
        self.conductor.bus.emit(ObservabilityEvent(
            type="task.created", component="task", task_id="task_a"))
        self.assertEqual(self.runtime.dressed, [])


if __name__ == "__main__":
    unittest.main()
