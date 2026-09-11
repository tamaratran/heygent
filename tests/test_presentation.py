"""Sections 26-27: one region, one expanded mode, state restored not replayed.

Each transition case from the spec is a test here, because the failures they
describe - a status bubble covering notifications, a stale approval card
coming back, the panel flashing between turns - are invisible to any
assertion about a single surface.

Run with:  python3 -m unittest tests.test_presentation -v
"""

from __future__ import annotations

import unittest

from conductor.presentation import (CONVERSATION, IDLE, NOTIFICATIONS, STATUS,
                                    PresentationCoordinator)


class OneRegionTest(unittest.TestCase):
    def test_only_one_surface_is_ever_expanded(self) -> None:
        c = PresentationCoordinator()
        for setup in (lambda: None,
                      lambda: c.notifications_changed(3),
                      lambda: c.set_status("Working on 3 tasks"),
                      lambda: c.user_turn_started(0.0),
                      lambda: c.user_opened_panel()):
            setup()
            expanded = [k for k, v in c.expanded().items() if v]
            self.assertLessEqual(len(expanded), 1,
                                 f"{expanded} expanded at once")

    def test_status_never_covers_notifications(self) -> None:
        c = PresentationCoordinator()
        c.notifications_changed(2)
        c.set_status("Working on that...")
        self.assertEqual(c.foreground(), NOTIFICATIONS)

    def test_idle_when_there_is_nothing_to_say(self) -> None:
        self.assertEqual(PresentationCoordinator().foreground(), IDLE)


class TransitionCaseTest(unittest.TestCase):
    """The lettered cases in section 26."""

    def test_case_a_notifications_to_conversation_and_back(self) -> None:
        c = PresentationCoordinator(grace=0.5)
        c.notifications_changed(2)
        c.save_view(expanded=True, scroll_offset=1, at_latest=False)
        self.assertEqual(c.foreground(), NOTIFICATIONS)

        c.user_turn_started(10.0)
        self.assertEqual(c.foreground(), CONVERSATION)
        self.assertEqual(c.compact_count(), 2)      # still known about

        c.user_turn_finished(11.0)
        c.assistant_started(11.1)
        c.assistant_finished(13.0)
        c.tick(13.4)                                # inside the grace period
        self.assertEqual(c.foreground(), CONVERSATION)
        c.tick(13.6)                                # past it
        self.assertEqual(c.foreground(), NOTIFICATIONS)
        view = c.restore_view()
        self.assertEqual((view.scroll_offset, view.at_latest), (1, False))

    def test_case_b_arrival_during_speech_only_increments(self) -> None:
        c = PresentationCoordinator(grace=0.5)
        c.notifications_changed(2)
        c.user_turn_started(0.0)
        c.user_turn_finished(0.9)
        c.assistant_started(1.0)
        c.notifications_changed(3)                  # arrives mid-sentence
        self.assertEqual(c.foreground(), CONVERSATION)
        self.assertEqual(c.compact_count(), 3)
        c.assistant_finished(3.0)
        c.tick(4.0)
        self.assertEqual(c.foreground(), NOTIFICATIONS)

    def test_case_c_status_recomputed_when_still_valid(self) -> None:
        c = PresentationCoordinator()
        c.set_status("Working on 3 tasks", "tasks-3")
        c.notifications_changed(1)
        self.assertEqual(c.foreground(), NOTIFICATIONS)
        c.notifications_changed(0)
        c.set_status("Working on 2 remaining tasks", "tasks-2")
        self.assertEqual(c.foreground(), STATUS)
        self.assertEqual(c.status_text, "Working on 2 remaining tasks")

    def test_case_d_obsolete_status_goes_idle(self) -> None:
        c = PresentationCoordinator()
        c.set_status("Checking PR", "pr")
        c.notifications_changed(1)
        c.notifications_changed(0)
        c.clear_status()                            # no longer true
        self.assertEqual(c.foreground(), IDLE)

    def test_case_g_manual_open_survives_assistant_speech(self) -> None:
        c = PresentationCoordinator()
        c.notifications_changed(2)
        c.assistant_started(0.0)
        self.assertEqual(c.foreground(), CONVERSATION)
        c.user_opened_panel()                       # explicit override
        self.assertEqual(c.foreground(), NOTIFICATIONS)

    def test_case_h_collapse_is_respected_until_something_urgent(self) -> None:
        c = PresentationCoordinator()
        c.notifications_changed(2)
        c.user_collapsed_panel()
        self.assertNotEqual(c.foreground(), NOTIFICATIONS)
        c.notifications_changed(3)                  # routine arrival
        self.assertNotEqual(c.foreground(), NOTIFICATIONS)
        c.notifications_changed(4, attention=True)  # approval
        self.assertEqual(c.foreground(), NOTIFICATIONS)


class PriorityTest(unittest.TestCase):
    def test_user_turn_beats_a_manually_opened_panel(self) -> None:
        c = PresentationCoordinator()
        c.notifications_changed(2)
        c.user_opened_panel()
        self.assertEqual(c.foreground(), NOTIFICATIONS)
        c.user_turn_started(0.0)                    # the stronger action
        self.assertEqual(c.foreground(), CONVERSATION)

    def test_grace_period_does_not_flash_between_turns(self) -> None:
        c = PresentationCoordinator(grace=1.0)
        c.notifications_changed(1)
        c.assistant_started(0.0)
        c.assistant_finished(1.0)
        c.tick(1.3)
        self.assertEqual(c.foreground(), CONVERSATION)
        c.user_turn_started(1.4)                    # they speak again
        c.tick(2.5)
        self.assertEqual(c.foreground(), CONVERSATION)


if __name__ == "__main__":
    unittest.main()
