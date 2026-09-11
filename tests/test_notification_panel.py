"""NotificationPanel tests: chat scroll semantics - newest at the bottom,
"Latest" exactly when at the bottom, "+N" exactly when newer cards sit
below the viewport. The spec's section 26 acceptance walk as unit tests.

Run with:  python3 -m unittest tests.test_notification_panel -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

from conductor.notification_panel import (CARD_KEYS, NotificationPanel,
                                          panel_card)
from conductor.session_card import project_card


def note(nid: str, status: str = "done", glyph: str = "") -> dict:
    return {"id": nid, "task_id": f"task_{nid}", "title": nid,
            "body": f"body {nid}", "status": status, "glyph": glyph}


class AcceptanceWalkTest(unittest.TestCase):
    """Five cards, exactly the spec's walk."""

    def setUp(self) -> None:
        self.panel = NotificationPanel(max_visible=3)
        for i in range(1, 6):                 # n1 oldest ... n5 newest
            self.panel.upsert(note(f"n{i}"))

    def test_at_bottom_latest_visible_plus_n_hidden(self) -> None:
        # Render order: older at top, newest at the bottom.
        self.assertEqual([n["id"] for n in self.panel.visible()],
                         ["n3", "n4", "n5"])
        self.assertTrue(self.panel.at_bottom())        # Latest visible
        self.assertEqual(self.panel.hidden_below(), 0)  # +N hidden

    def test_scroll_up_hides_latest_and_shows_plus_n(self) -> None:
        self.panel.scroll_up()
        self.assertFalse(self.panel.at_bottom())        # Latest gone
        self.assertEqual(self.panel.hidden_below(), 1)  # +1
        self.panel.scroll_up()
        self.assertEqual(self.panel.hidden_below(), 2)  # +2
        self.assertEqual([n["id"] for n in self.panel.visible()],
                         ["n1", "n2", "n3"])
        self.panel.scroll_up()                          # clamped at history
        self.assertEqual(self.panel.hidden_below(), 2)

    def test_scroll_down_walks_plus_n_to_latest(self) -> None:
        self.panel.scroll_up(2)
        self.panel.scroll_down()
        self.assertEqual(self.panel.hidden_below(), 1)   # +2 -> +1
        self.panel.scroll_down()
        self.assertEqual(self.panel.hidden_below(), 0)   # +1 disappears
        self.assertTrue(self.panel.at_bottom())          # Latest appears

    def test_scroll_to_bottom_jump(self) -> None:
        self.panel.scroll_up(2)
        self.panel.scroll_to_bottom()
        self.assertTrue(self.panel.at_bottom())
        self.assertEqual(self.panel.visible()[-1]["id"], "n5")


class ArrivalTest(unittest.TestCase):
    def test_at_bottom_viewport_follows(self) -> None:
        panel = NotificationPanel(max_visible=3)
        for i in range(1, 6):
            panel.upsert(note(f"n{i}"))
        panel.upsert(note("n6"))
        self.assertTrue(panel.at_bottom())               # pinned, chat-like
        self.assertEqual(panel.visible()[-1]["id"], "n6")

    def test_scrolled_up_preserves_viewport_and_increments(self) -> None:
        panel = NotificationPanel(max_visible=3)
        for i in range(1, 6):
            panel.upsert(note(f"n{i}"))
        panel.scroll_up(2)                               # reading n1-n3
        before = [n["id"] for n in panel.visible()]
        panel.upsert(note("n6"))
        self.assertEqual([n["id"] for n in panel.visible()], before)
        self.assertEqual(panel.hidden_below(), 3)        # +N incremented
        self.assertFalse(panel.at_bottom())              # never yanked


class DataTest(unittest.TestCase):
    def test_same_id_mutates_in_place(self) -> None:
        panel = NotificationPanel()
        panel.upsert(note("appr_1", status="working"))
        panel.upsert(dict(note("appr_1", status="working"),
                          body="updated"))
        self.assertEqual(len(panel.items), 1)
        self.assertEqual(panel.visible()[0]["body"], "updated")

    def test_dismiss_reflows_data_never_windows(self) -> None:
        panel = NotificationPanel(max_visible=3)
        for i in range(1, 5):
            panel.upsert(note(f"n{i}"))
        self.assertTrue(panel.dismiss("n3"))
        self.assertFalse(panel.dismiss("n3"))            # idempotent
        self.assertEqual([n["id"] for n in panel.visible()],
                         ["n1", "n2", "n4"])

    def test_removal_below_viewport_keeps_position(self) -> None:
        panel = NotificationPanel(max_visible=2)
        for i in range(1, 6):
            panel.upsert(note(f"n{i}"))
        panel.scroll_up(2)                     # viewing n2, n3
        viewing = [n["id"] for n in panel.visible()]
        panel.dismiss("n5")                    # newest dismissed below
        self.assertEqual([n["id"] for n in panel.visible()], viewing)
        self.assertEqual(panel.hidden_below(), 1)

    def test_attention_is_not_overflow(self) -> None:
        panel = NotificationPanel(max_visible=3)
        panel.upsert(note("a1", status="working", glyph="attention"))
        for i in range(4):
            panel.upsert(note(f"c{i}"))
        self.assertEqual(panel.hidden_below(), 0)        # at bottom
        self.assertEqual(panel.attention_count(), 1)     # separate state

    def test_attention_is_not_mere_activity(self) -> None:
        """A running worker is the system doing its job; only a blocked or
        failed one is the user's turn. Counting "working" cards made the
        collapsed badge amber whenever anything ran at all - so the one
        time a worker was genuinely stuck it looked like every other."""
        panel = NotificationPanel(max_visible=3)
        panel.upsert(note("w1", status="working", glyph="working"))
        panel.upsert(note("w2", status="working"))
        self.assertEqual(panel.attention_count(), 0)
        panel.upsert(note("a1", status="working", glyph="attention"))
        panel.upsert(note("f1", status="working", glyph="failed"))
        self.assertEqual(panel.attention_count(), 2)


class HiddenAttentionTest(unittest.TestCase):
    """A question the user cannot see is the panel's worst case: three
    visible rows, and the blocked card scrolled away above them. The panel
    reports blocked cards outside the viewport by direction, and can
    scroll the newest one back into view."""

    def stacked(self) -> NotificationPanel:
        panel = NotificationPanel(max_visible=3)
        panel.upsert(note("a1", status="working", glyph="attention"))
        for i in range(1, 5):                 # a1 pushed out of the viewport
            panel.upsert(note(f"n{i}"))
        return panel

    def test_attention_above_the_viewport_is_counted(self) -> None:
        panel = self.stacked()
        self.assertTrue(panel.at_bottom())
        self.assertEqual(panel.hidden_attention_above(), 1)
        self.assertEqual(panel.hidden_attention_below(), 0)

    def test_attention_below_the_viewport_is_counted(self) -> None:
        panel = NotificationPanel(max_visible=3)
        for i in range(1, 5):
            panel.upsert(note(f"n{i}"))
        panel.scroll_up()                     # reading history
        panel.upsert(note("a1", status="working", glyph="attention"))
        self.assertEqual(panel.hidden_attention_below(), 1)
        self.assertEqual(panel.hidden_attention_above(), 0)

    def test_a_visible_blocked_card_is_not_hidden(self) -> None:
        panel = NotificationPanel(max_visible=3)
        panel.upsert(note("a1", status="working", glyph="attention"))
        self.assertEqual(panel.hidden_attention_above(), 0)
        self.assertEqual(panel.hidden_attention_below(), 0)

    def test_reveal_scrolls_the_blocked_card_into_view(self) -> None:
        panel = self.stacked()
        self.assertTrue(panel.reveal_attention())
        self.assertIn("a1", [n["id"] for n in panel.visible()])
        self.assertEqual(panel.hidden_attention_above(), 0)

    def test_reveal_with_nothing_hidden_is_a_no_op(self) -> None:
        panel = NotificationPanel(max_visible=3)
        panel.upsert(note("n1"))
        self.assertFalse(panel.reveal_attention())
        self.assertTrue(panel.at_bottom())


class EscalationSurfacesTheCard(unittest.TestCase):
    """A long-running worker sits in scrolled-away history; when it asks a
    question, mutating the buried row in place leaves the question
    invisible. The transition into needing the user moves the card to the
    newest slot - once, on the edge, so a waiting card re-observed by the
    sweep stays put."""

    def test_becoming_blocked_moves_the_card_to_the_newest_slot(self) -> None:
        panel = NotificationPanel(max_visible=3)
        panel.upsert(note("a1", status="working", glyph="working"))
        for i in range(1, 5):
            panel.upsert(note(f"n{i}"))
        self.assertNotIn("a1", [n["id"] for n in panel.visible()])
        panel.upsert(note("a1", status="working", glyph="attention"))
        self.assertEqual(panel.visible()[-1]["id"], "a1")

    def test_a_waiting_card_reobserved_stays_put(self) -> None:
        panel = NotificationPanel(max_visible=3)
        panel.upsert(note("a1", status="working", glyph="attention"))
        panel.upsert(note("n1"))
        panel.upsert(note("a1", status="working", glyph="attention"))
        self.assertEqual([n["id"] for n in panel.items], ["a1", "n1"])

    def test_scrolled_up_the_move_never_yanks(self) -> None:
        panel = NotificationPanel(max_visible=3)
        panel.upsert(note("a1", status="working", glyph="working"))
        for i in range(1, 5):
            panel.upsert(note(f"n{i}"))
        panel.scroll_up()                     # reading history
        before = [n["id"] for n in panel.visible()]
        panel.upsert(note("a1", status="working", glyph="attention"))
        self.assertEqual([n["id"] for n in panel.visible()], before)
        self.assertEqual(panel.hidden_attention_below(), 1)


class DismissalSticks(unittest.TestCase):
    """The bug: the last card standing could not be dismissed.

    Cards are keyed on the task, and a live worker emits telemetry
    continuously. The x removed the card; the next progress tick upserted
    the same key and it reappeared. Finished tasks stop emitting, so they
    stayed dismissed - which is why only the last, still-running card
    looked broken.
    """

    def setUp(self):
        self.panel = NotificationPanel(max_visible=3)
        self.panel.upsert({"id": "task_a", "title": "Auth", "status": "working"})

    def test_activity_does_not_resurrect_a_dismissed_card(self):
        self.panel.dismiss("task_a")
        self.assertEqual(self.panel.items, [])
        for _ in range(5):            # telemetry keeps arriving
            self.panel.upsert({"id": "task_a", "title": "Auth",
                               "status": "working"})
        self.assertEqual(self.panel.items, [], "dismissed card came back")

    def test_escalation_overrides_a_dismissal(self):
        self.panel.dismiss("task_a")
        self.panel.upsert({"id": "task_a", "title": "Auth",
                           "status": "done"}, force=True)
        self.assertEqual(len(self.panel.items), 1)
        # And having reopened it, it is dismissable again.
        self.panel.dismiss("task_a")
        self.panel.upsert({"id": "task_a", "title": "Auth", "status": "working"})
        self.assertEqual(self.panel.items, [])

    def test_other_cards_are_unaffected(self):
        self.panel.upsert({"id": "task_b", "title": "Billing",
                           "status": "working"})
        self.panel.dismiss("task_a")
        self.panel.upsert({"id": "task_a", "status": "working"})
        self.panel.upsert({"id": "task_b", "status": "working"})
        self.assertEqual([i["id"] for i in self.panel.items], ["task_b"])

    def test_resolve_clears_the_dismissal_so_a_rerun_shows(self):
        self.panel.dismiss("task_a")
        self.panel.resolve("task_a")
        self.panel.upsert({"id": "task_a", "title": "Auth", "status": "working"})
        self.assertEqual(len(self.panel.items), 1)

    def test_resolve_keeps_the_card_where_it_is(self):
        """An answered question does not move the session. Removing the
        row on resolve and re-adding it on the worker's next tick sent
        the same card to the bottom of the stack every time."""
        self.panel.upsert({"id": "task_b", "title": "Billing",
                           "status": "working"})
        self.assertTrue(self.panel.resolve("task_a"))
        self.panel.upsert({"id": "task_a", "title": "Auth",
                           "status": "working", "body": "back to work"})
        self.assertEqual([i["id"] for i in self.panel.items],
                         ["task_a", "task_b"], "the card jumped")
        self.assertEqual(self.panel.items[0]["body"], "back to work")
        self.assertFalse(self.panel.resolve("task_nope"))


class TheRendererKeepsTheFlags(unittest.TestCase):
    """The overlay's copy of a card carries the projection's `dismissed`
    and `force`. Measured 2026-08-30 (conductor-94883): five cards
    dismissed, all five back on the next restart - the overlay rebuilt
    the card by hand and left `dismissed` out, so the panel never heard
    that the user had seen them."""

    def projected(self, dismissed_at: int, result_at: int = 3,
                  status: str = "idle") -> dict:
        return project_card({
            "task_id": "task_a", "id": "sub_task_a", "title": "Auth",
            "status": status, "result": {"summary": "done"},
            "revision": max(dismissed_at, result_at),
            "result_revision": result_at, "attention_revision": 0,
            "dismissed_at_revision": dismissed_at})

    def test_a_dismissed_card_replayed_at_restart_stays_away(self):
        panel = NotificationPanel(max_visible=3)
        card = panel_card(self.projected(dismissed_at=5))
        self.assertTrue(card["dismissed"])
        self.assertFalse(card["force"])
        panel.upsert(card, force=card["force"])
        self.assertEqual(panel.items, [], "the dismissed card came back")
        # And the panel now remembers it without being told again.
        panel.upsert(panel_card(dict(self.projected(dismissed_at=5),
                                     dismissed=False)))
        self.assertEqual(panel.items, [])

    def test_news_after_the_dismissal_reopens_it(self):
        panel = NotificationPanel(max_visible=3)
        panel.upsert(panel_card(self.projected(dismissed_at=5)))
        card = panel_card(self.projected(dismissed_at=5, result_at=7))
        self.assertTrue(card["force"])
        self.assertFalse(card["dismissed"])
        panel.upsert(card, force=card["force"])
        self.assertEqual([i["id"] for i in panel.items], ["task_a"])

    def test_the_copy_is_the_same_shape_every_time(self):
        card = panel_card({"id": "x", "title": None, "glyph": "working"})
        self.assertEqual(set(card), set(CARD_KEYS))
        self.assertEqual(card["title"], "")
        self.assertEqual(card["status"], "done")
        self.assertIs(card["dismissed"], False)
        self.assertIs(card["force"], False)

    def test_the_overlay_does_not_build_the_card_by_hand(self):
        """Source check, AppKit-free: show_notice hands the notice to
        panel_card rather than picking keys itself."""
        source = (Path(__file__).resolve().parent.parent / "overlay.py").read_text()
        body = source[source.index("def show_notice"):]
        body = body[:body.index("\n    def ", 10)]
        self.assertIn("panel_card(notice)", body)
        self.assertNotIn('"title": str(notice', body)


if __name__ == "__main__":
    unittest.main()


class CardsDoNotRepeatThemselves(unittest.TestCase):
    """A card that prints its own heading again is not a card, it's a bug.

    task.created carries the task's own title as data["title"], and that is
    also what names the card - so a starting worker rendered its name as
    heading and again as body, the second copy truncated with an ellipsis.
    """

    def notice(self, **kw):
        from conductor.notification_panel import notice_payload
        from conductor.notifications import TaskNotification
        base = dict(project_id="proj_a", task_id="task_a", type="info",
                    title="Working", body="",
                    task_title="Sys prompt: coding agents and capabilities",
                    project_name="voice-agent")
        base.update(kw)
        return notice_payload(TaskNotification(**base))

    def test_the_exact_echo_is_replaced_by_the_headline(self):
        card = self.notice(body="Sys prompt: coding agents and capabilities")
        self.assertEqual(card["title"],
                         "Sys prompt: coding agents and capabilities")
        self.assertEqual(card["body"], "Working")

    def test_an_empty_body_does_not_fall_back_onto_the_title(self):
        card = self.notice(body="", title="Working")
        self.assertEqual(card["body"], "Working")
        self.assertNotEqual(card["body"], card["title"])

    def test_a_truncated_echo_still_counts(self):
        card = self.notice(body="Sys prompt: coding agents and cap")
        self.assertEqual(card["body"], "Working")

    def test_case_and_punctuation_do_not_hide_an_echo(self):
        card = self.notice(body="sys prompt: coding agents and capabilities.")
        self.assertEqual(card["body"], "Working")

    def test_a_headline_that_also_echoes_leaves_the_body_empty(self):
        card = self.notice(title="Sys prompt: coding agents and capabilities",
                           body="Sys prompt: coding agents and capabilities")
        self.assertEqual(card["body"], "")

    def test_a_real_body_is_untouched(self):
        card = self.notice(body="Found the race in session.py.")
        self.assertEqual(card["body"], "Found the race in session.py.")

    def test_a_body_that_merely_starts_similarly_is_kept(self):
        """Prefix matching must not eat a body that genuinely continues."""
        card = self.notice(
            task_title="Auth",
            body="Auth turned out to be a session race, now fixed.")
        self.assertEqual(card["body"],
                         "Auth turned out to be a session race, now fixed.")

    def test_with_no_task_title_the_body_becomes_the_heading(self):
        card = self.notice(task_title="", project_name="",
                           title="", body="Something happened.")
        self.assertEqual(card["title"], "Something happened.")
        self.assertEqual(card["body"], "")
