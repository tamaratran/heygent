"""Spec section 24: the geometry assertions.

The state machine was already covered; what was not is the rendered layout -
which is exactly where the misaligned, unevenly spaced stack of separate
cards came from. These drive the real NSView and measure the frames it would
draw, so a regression in alignment or spacing fails here rather than in a
screenshot.

Run with:  python3 -m unittest tests.test_panel_geometry -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

try:
    import overlay
    from overlay import NotificationPanelView, PANEL_EDGE, PANEL_GAP, CARD_W
    from AppKit import NSMakeRect
    HAVE_APPKIT = True
except Exception:                       # headless CI without pyobjc
    HAVE_APPKIT = False

from conductor.notification_panel import NotificationPanel

# Source-reading tests need no AppKit: the file, not the module.
OVERLAY_SOURCE = Path(__file__).resolve().parent.parent / "overlay.py"


def card(i: int, body: str = "Needs your approval.") -> dict:
    return {"id": f"n{i}", "title": f"proj · Task {i}", "body": body,
            "status": "working" if i % 2 else "done"}


@unittest.skipUnless(HAVE_APPKIT, "pyobjc/AppKit not available")
class PanelGeometryTest(unittest.TestCase):
    def view(self, n: int, bodies: list[str] | None = None):
        v = NotificationPanelView.alloc().initWithFrame_(
            NSMakeRect(0, 0, CARD_W, 400))
        v.cards = [card(i, (bodies or {}) and bodies[i] or "Needs your approval.")
                   for i in range(n)]
        return v

    def frames(self, v):
        return [f for _c, f in v._card_frames()]

    # -- alignment --------------------------------------------------------
    def test_every_card_shares_one_x(self) -> None:
        xs = {f.origin.x for f in self.frames(self.view(5))}
        self.assertEqual(len(xs), 1, f"cards drifted horizontally: {xs}")
        self.assertEqual(xs.pop(), PANEL_EDGE)

    def test_every_card_shares_one_width(self) -> None:
        widths = {f.size.width for f in self.frames(self.view(5))}
        self.assertEqual(len(widths), 1, f"ragged widths: {widths}")

    def test_left_and_right_edges_line_up(self) -> None:
        frames = self.frames(self.view(4))
        lefts = {f.origin.x for f in frames}
        rights = {f.origin.x + f.size.width for f in frames}
        self.assertEqual(len(lefts), 1)
        self.assertEqual(len(rights), 1)

    def test_alignment_survives_bodies_of_different_length(self) -> None:
        v = self.view(3, bodies=["short.",
                                 "a considerably longer body that will wrap "
                                 "onto a second line in the card",
                                 "medium length body here"])
        frames = self.frames(v)
        self.assertEqual(len({f.origin.x for f in frames}), 1)
        self.assertEqual(len({f.size.width for f in frames}), 1)

    # -- spacing ----------------------------------------------------------
    def test_gaps_are_identical_and_equal_the_token(self) -> None:
        frames = self.frames(self.view(5))
        gaps = [round(frames[i + 1].origin.y
                      - (frames[i].origin.y + frames[i].size.height), 4)
                for i in range(len(frames) - 1)]
        self.assertEqual(set(gaps), {PANEL_GAP},
                         f"uneven vertical spacing: {gaps}")

    def test_no_unexplained_vertical_gap(self) -> None:
        """Panel height is the cards plus their gaps plus fixed chrome."""
        v = self.view(3)
        frames = self.frames(v)
        spanned = (frames[-1].origin.y + frames[-1].size.height
                   - frames[0].origin.y)
        cards = sum(f.size.height for f in frames)
        self.assertAlmostEqual(spanned - cards, PANEL_GAP * 2, places=4)

    def test_every_cards_height_is_constant(self) -> None:
        """A live worker rewrites its body on every tick, and a card that
        finishes swaps its body for a final message; any height that followed
        the text moved the stack. Every card is one fixed size: the head plus
        a single ellipsised body line, whatever the body or status."""
        v = self.view(1)
        long = ("a considerably longer body that wraps onto a second line "
                "in the card and keeps going")
        heights = {v.card_height({"id": "w", "title": "T", "body": body,
                                  "status": status})
                   for body in ("", "short.", long, "short.", "", long)
                   for status in ("working", "done", "failed")}
        self.assertEqual(len(heights), 1,
                         f"a card's height followed its content: {heights}")
        self.assertEqual(heights.pop(),
                         overlay.PANEL_HEAD_H + overlay.PANEL_BODY_LINE)

    def test_finishing_does_not_resize_a_card(self) -> None:
        """The working-to-done transition keeps the same footprint, so the
        stack never moves when a task ends."""
        v = self.view(1)
        long = ("a considerably longer body that wraps onto a second line "
                "in the card and keeps going")
        working = v.card_height({"id": "w", "title": "T", "body": long,
                                 "status": "working"})
        settled = v.card_height({"id": "w", "title": "T", "body": long,
                                 "status": "done"})
        self.assertEqual(working, settled)

    # -- bounded height ---------------------------------------------------

    def test_five_notices_do_not_grow_the_panel(self) -> None:
        panel = NotificationPanel()
        for i in range(5):
            panel.upsert(card(i))
        tall = self.view(len(panel.visible())).panel_height()
        panel2 = NotificationPanel()
        for i in range(20):
            panel2.upsert(card(i))
        taller = self.view(len(panel2.visible())).panel_height()
        self.assertEqual(tall, taller, "panel grew with the backlog")

    # -- alignment ACROSS the two windows ---------------------------------
    def test_panel_cards_align_with_the_voice_card(self) -> None:
        """The gap the first pass missed.

        Panel cards were measured only against each other, so they could be
        internally perfect and still sit at a different x and width from the
        voice card directly below - which is what the stack actually looked
        like. Both windows are the same width and centred alike, so the drawn
        cards must share an x and a width too.
        """
        frames = self.frames(self.view(3))
        xs = {f.origin.x for f in frames} | {overlay.CARD_INSET}
        widths = {f.size.width for f in frames} | {overlay.CARD_W}
        self.assertEqual(len(xs), 1,
                         f"panel and voice card start at different x: {xs}")
        self.assertEqual(len(widths), 1,
                         f"panel and voice card differ in width: {widths}")

    def test_panel_and_voice_card_share_one_inset(self) -> None:
        self.assertEqual(overlay.PANEL_EDGE, overlay.CARD_INSET)

    # -- one window -------------------------------------------------------
    def test_only_one_notification_window_exists(self) -> None:
        """Section 1: notifications live in one window, never one each."""
        source = Path(overlay.__file__).read_text()
        self.assertEqual(source.count("self.panel_window = NSWindow.alloc()"), 1)
        # No per-notice window creation anywhere.
        for banned in ("notice_window", "toast_window", "self.notices["):
            self.assertNotIn(banned + " = NSWindow", source)


class PanelStateTest(unittest.TestCase):
    """Section 24's scroll assertions. No AppKit needed, so these run
    everywhere - a skipped assertion protects nothing."""

    def test_panel_shows_at_most_three_cards(self) -> None:
        self.assertLessEqual(NotificationPanel().max_visible, 3)

    def test_latest_visible_exactly_at_bottom(self) -> None:
        panel = NotificationPanel()
        for i in range(6):
            panel.upsert(card(i))
        self.assertTrue(panel.at_bottom())
        self.assertEqual(panel.hidden_below(), 0)
        panel.scroll_up()
        self.assertFalse(panel.at_bottom())
        self.assertGreater(panel.hidden_below(), 0)

    def test_plus_n_counts_cards_below_not_unread(self) -> None:
        panel = NotificationPanel()
        for i in range(6):
            panel.upsert(card(i))
        panel.scroll_up(2)
        self.assertEqual(panel.hidden_below(), 2)
        panel.scroll_down()
        self.assertEqual(panel.hidden_below(), 1)
        panel.scroll_down()
        self.assertEqual(panel.hidden_below(), 0)
        self.assertTrue(panel.at_bottom())


@unittest.skipUnless(HAVE_APPKIT, "pyobjc/AppKit not available")
class CloseButtonTest(unittest.TestCase):
    """One dismiss control, one size.

    The panel and the voice card each drew their own, at 22pt and 26pt, so
    three stacked cards showed three different x buttons.
    """

    def test_only_one_place_draws_the_dismiss_control(self) -> None:
        source = Path(overlay.__file__).read_text()
        self.assertEqual(source.count("def draw_close"), 1)
        # No call site may hand-roll its own circle or glyph.
        self.assertEqual(source.count("cross.setLineWidth_"), 1,
                         "a second dismiss glyph is drawn somewhere")

    def test_every_caller_uses_the_shared_drawing(self) -> None:
        source = Path(overlay.__file__).read_text()
        self.assertGreaterEqual(source.count("draw_close("), 3,
                                "a card is not using the shared control")

    def test_the_control_has_one_set_of_dimensions(self) -> None:
        self.assertGreater(overlay.CLOSE_D, 0)
        self.assertGreater(overlay.CLOSE_ARM, 0)
        self.assertLess(overlay.CLOSE_ARM * 2, overlay.CLOSE_D,
                        "the glyph does not fit inside its circle")


@unittest.skipUnless(HAVE_APPKIT, "pyobjc/AppKit not available")
class HoverAffordanceTest(unittest.TestCase):
    """A card is a door to its session; the cursor should say so.

    Tapping a card focuses its worker, but nothing on screen suggested that,
    so the behaviour was invisible until discovered by accident.
    """

    def view(self):
        v = NotificationPanelView.alloc().initWithFrame_(
            NSMakeRect(0, 0, overlay.WINDOW_W, 400))
        v.cards = [{"id": f"t{i}", "task_id": f"t{i}", "title": f"Task {i}",
                    "body": "doing something", "status": "working"}
                   for i in range(3)]
        v.convertPoint_fromView_ = lambda p, _v: p
        return v

    def move_to(self, view, point):
        class Event:
            def locationInWindow(self):
                return point
        view.mouseMoved_(Event())

    def test_hovering_a_card_marks_it(self) -> None:
        v = self.view()
        _card, frame = v._card_frames()[1]
        self.move_to(v, overlay.NSMakePoint(frame.origin.x + 40,
                                            frame.origin.y + 10))
        self.assertEqual(v.hovered, "t1")

    def test_leaving_the_panel_clears_it(self) -> None:
        v = self.view()
        _card, frame = v._card_frames()[0]
        self.move_to(v, overlay.NSMakePoint(frame.origin.x + 40,
                                            frame.origin.y + 10))
        self.assertIsNotNone(v.hovered)
        v.mouseExited_(None)
        self.assertIsNone(v.hovered)

    def test_the_gap_between_cards_hovers_nothing(self) -> None:
        v = self.view()
        first = v._card_frames()[0][1]
        gap_y = first.origin.y + first.size.height + overlay.PANEL_GAP / 2
        self.move_to(v, overlay.NSMakePoint(first.origin.x + 40, gap_y))
        self.assertIsNone(v.hovered)


@unittest.skipUnless(HAVE_APPKIT, "pyobjc/AppKit not available")
class StackOverlapTest(unittest.TestCase):
    """The panel and the voice card must never occupy the same pixels.

    They are separate windows, and the panel anchors itself above the card by
    reading card_visible. show_card set that flag after laying out, so the
    first card of a turn was positioned as though it did not exist and the
    two briefly overlapped before the next event corrected it.
    """

    def test_visibility_is_set_before_layout(self) -> None:
        source = Path(overlay.__file__).read_text()
        body = source[source.index("def show_card"):]
        body = body[:body.index("def pin_card")]
        set_at = body.index("self.card_visible = True")
        laid_at = body.index("self.layout_card()")
        self.assertLess(set_at, laid_at,
                        "layout runs before the card is marked visible, so "
                        "the panel anchors to the wrong height")

    def test_hiding_the_card_re_anchors_the_panel(self) -> None:
        source = Path(overlay.__file__).read_text()
        body = source[source.index("def hide_card"):]
        body = body[:body.index("\n    def ", 10)]
        self.assertIn("render_panel", body,
                      "the panel keeps a gap for a card that is gone")

    def test_panel_keeps_the_one_gap_above_the_reply_card(self) -> None:
        """PANEL_GAP is the one spacing token, and the reply card gets the
        same gap the panel keeps between its own cards - whose overhanging
        x buttons live inside that gap too. Widening it to clear the x made
        the reply card float apart from the stack.
        """
        source = Path(overlay.__file__).read_text()
        self.assertIn("card_top + PANEL_GAP\n", source,
                      "the reply card's gap drifted from PANEL_GAP")
        body = source[source.index("def stack_base"):]
        body = body[:body.index("\n    def ", 10)]
        self.assertNotIn("CLOSE_D", body,
                         "the panel pads extra air for the overhanging x")


@unittest.skipUnless(HAVE_APPKIT, "pyobjc/AppKit not available")
class StackAsOneObjectTest(unittest.TestCase):
    """Capsule, caption, panel and reply card are one thing on screen."""

    def test_every_window_honours_the_shared_offset(self) -> None:
        """Capsule, caption, panel, reply card and the collapsed badge."""
        source = Path(overlay.__file__).read_text()
        # render_panel's and layout_card's window placements live in
        # _place_panel and _place_card, which the settle glide in tick_
        # shares.
        positioners = ("_resize", "show_caption", "_place_panel",
                       "_place_card", "render_badge")
        for name in positioners:
            start = source.index(f"def {name}")
            end = source.find("\n    def ", start + 10)
            body = source[start:end if end > 0 else len(source)]
            self.assertIn("self.stack_offset", body,
                          f"{name} positions its window without the offset")

    def test_dragging_the_panel_moves_the_stack(self) -> None:
        source = Path(overlay.__file__).read_text()
        self.assertIn("self.panel_view.on_move = self.move_stack", source)

    def test_the_offset_is_wired_after_the_view_exists(self) -> None:
        """It was set before construction and raised on launch."""
        source = Path(overlay.__file__).read_text()
        created = source.index("self.panel_view = NotificationPanelView")
        wired = source.index("self.panel_view.on_move")
        self.assertLess(created, wired)


class CollapsedBadgeTest(unittest.TestCase):
    """The chevron folds the stack into one counted circle."""

    def test_badge_matches_the_chevron_it_replaces(self) -> None:
        if not HAVE_APPKIT:
            self.skipTest("AppKit")
        self.assertEqual(overlay.BADGE_D, 38.0)

    def test_folding_hides_the_panel_and_the_reply(self) -> None:
        source = OVERLAY_SOURCE.read_text()
        body = source[source.index("def collapse_stack"):]
        body = body[:body.index("def expand_stack")]
        self.assertIn("self.panel_window.orderOut_", body)
        self.assertIn("self.card_window.orderOut_", body)

    def test_arrivals_while_folded_do_not_reopen_it(self) -> None:
        source = OVERLAY_SOURCE.read_text()
        body = source[source.index("def render_panel"):]
        body = body[:body.index("def ", 20)]
        self.assertIn("if self.collapsed_stack:", body,
                      "a new card would spring the panel back open")


@unittest.skipUnless(HAVE_APPKIT, "pyobjc/AppKit not available")
class PillPlacementTest(unittest.TestCase):
    """Two indicators, two edges.

    "Latest" labels the top of the stack - you are at the newest and nothing
    is below you. "+N" hangs off the bottom, because that is the direction
    the unseen cards are in. Both drew at the bottom before, which made them
    read as one control that changed its mind.
    """

    def view(self, at_bottom, below, above):
        v = NotificationPanelView.alloc().initWithFrame_(
            NSMakeRect(0, 0, overlay.WINDOW_W, 400))
        v.cards = [{"id": f"p{i}", "task_id": f"p{i}", "title": f"S{i}",
                    "body": "x", "status": "done"} for i in range(3)]
        v.at_bottom, v.hidden_below, v.hidden_above = at_bottom, below, above
        return v

    def test_latest_sits_on_the_top_edge(self) -> None:
        v = self.view(True, 0, 2)
        self.assertTrue(v.shows_latest())
        self.assertFalse(v.shows_more())
        # Room is reserved above the first card for it to straddle.
        self.assertGreater(v._card_frames()[0][1].origin.y,
                           overlay.PANEL_EDGE)

    def test_plus_n_sits_on_the_bottom_edge(self) -> None:
        v = self.view(False, 2, 0)
        self.assertTrue(v.shows_more())
        self.assertFalse(v.shows_latest())
        # Room below the last card for it to straddle.
        last = v._card_frames()[-1][1]
        self.assertGreater(v.panel_height() - (last.origin.y + last.size.height),
                           overlay.PANEL_EDGE / 2)

    def test_the_cards_do_not_move_when_a_pill_comes_or_goes(self) -> None:
        """Scrolling one card up used to shift every card by half a pill
        and change the panel's height - the stack jittered to make space
        for +1, +2. Card frames and height depend on the cards alone."""
        def geometry(v):
            return ([(f.origin.x, f.origin.y, f.size.width, f.size.height)
                     for _, f in v._card_frames()], v.panel_height())
        latest = geometry(self.view(True, 0, 2))
        more = geometry(self.view(False, 2, 0))
        neither = geometry(self.view(True, 0, 0))
        self.assertEqual(latest, more)
        self.assertEqual(latest, neither)

    def test_neither_when_everything_fits(self) -> None:
        v = self.view(True, 0, 0)
        self.assertFalse(v.shows_latest())
        self.assertFalse(v.shows_more())

    def test_the_two_are_never_shown_together(self) -> None:
        for at_bottom in (True, False):
            for below in (0, 1, 3):
                for above in (0, 1, 3):
                    v = self.view(at_bottom, below, above)
                    self.assertFalse(v.shows_latest() and v.shows_more(),
                                     f"both pills at once: {at_bottom} "
                                     f"{below} {above}")


@unittest.skipUnless(HAVE_APPKIT, "pyobjc/AppKit not available")
class ChevronOwnershipTest(unittest.TestCase):
    """The chevron belongs to the column, not to any card in it.

    It was drawn by the reply card, so it vanished whenever that card did -
    including the moment a finished answer started retiring itself, which
    left no way to fold the stack at all.
    """

    def test_the_reply_card_no_longer_draws_one(self) -> None:
        source = Path(overlay.__file__).read_text()
        body = source[source.index("class CardView"):]
        body = body[:body.index("\nclass ")]
        self.assertNotIn("Chevron, hanging below the card", body)

    def test_it_has_its_own_surface(self) -> None:
        source = Path(overlay.__file__).read_text()
        self.assertEqual(source.count("class ChevronView"), 1)
        self.assertEqual(
            source.count("self.chevron_window = NSWindow.alloc()"), 1,
            "the chevron window is built more than once")

    def test_it_shows_whenever_there_is_a_column_to_fold(self) -> None:
        source = Path(overlay.__file__).read_text()
        body = source[source.index("def render_chevron"):]
        body = body[:body.index("\n    def ", 10)]
        self.assertIn("self.panel.items or self.card_visible", body,
                      "the chevron is tied to one surface again")


@unittest.skipUnless(HAVE_APPKIT, "pyobjc/AppKit not available")
class ReplyRetirementTest(unittest.TestCase):
    """A finished answer takes itself down to make room for the rows."""

    def test_the_overlay_owns_the_clock(self) -> None:
        """It lived in voice_agent, so anything else driving the overlay -
        a demo, a test - left finished replies on screen for ever."""
        source = Path(overlay.__file__).read_text()
        self.assertIn("self.card_expires_at", source)

    def test_only_a_finished_reply_expires(self) -> None:
        source = Path(overlay.__file__).read_text()
        body = source[source.index("def show_card"):]
        body = body[:body.index("\n    def ", 10)]
        self.assertIn('self.card_view.status == "done"', body,
                      "a still-streaming answer would be pulled mid-sentence")

    def test_the_linger_is_a_couple_of_seconds(self) -> None:
        self.assertGreaterEqual(overlay.REPLY_LINGER, 1.0)
        self.assertLessEqual(overlay.REPLY_LINGER, 5.0)


@unittest.skipUnless(HAVE_APPKIT, "pyobjc/AppKit not available")
class ChevronRoutingTest(unittest.TestCase):
    """What the one down-arrow does depends on what is under it.

    Collapsing trades many cards for one counted circle. With an empty
    panel there is no count: render_badge bails on no items, so folding
    the lone reply card hid it, drew no badge, and left collapsed_stack
    latched - every later notification then rendered as a badge instead
    of a panel. With nothing to fold, the arrow is the dismiss.
    """

    class Stub:
        def __init__(self, items, collapsed=False):
            self.panel = NotificationPanel()
            self.panel.items = items
            self.collapsed_stack = collapsed
            self.did = []
        def expand_stack(self): self.did.append("expand")
        def collapse_stack(self): self.did.append("collapse")
        def dismiss_card(self): self.did.append("dismiss")

    def toggle(self, items, collapsed=False):
        stub = self.Stub(items, collapsed)
        overlay.Controller.toggle_stack(stub)
        return stub.did

    def test_empty_panel_dismisses(self):
        self.assertEqual(self.toggle([]), ["dismiss"])

    def test_panel_with_cards_collapses(self):
        self.assertEqual(self.toggle([{"id": "a"}]), ["collapse"])

    def test_collapsed_always_expands(self):
        self.assertEqual(self.toggle([], collapsed=True), ["expand"])
        self.assertEqual(self.toggle([{"id": "a"}], collapsed=True),
                         ["expand"])


@unittest.skipUnless(HAVE_APPKIT, "pyobjc/AppKit not available")
class ATitleAtTheCapFitsTheCard(unittest.TestCase):
    """The title is capped where it is generated (TITLE_MAX_CHARS) rather
    than trimmed where it is drawn, so the cap has to be one the card can
    actually hold: a full-length title of ordinary prose, in the panel's
    title font, must fit between the card's padding and its status glyph.
    This is the measurement the number was chosen from."""

    def test_full_length_prose_titles_fit_the_title_width(self):
        from conductor.boss_tools import TITLE_MAX_CHARS
        from AppKit import NSAttributedString
        width = CARD_W - 2 * overlay.PANEL_PAD - overlay.PANEL_ICON
        for title in ("Investigate ZeroMQ reconnect storm on M2",
                      "Fix transcript spacing at punctuation OK",
                      "Add PR #711 review notes to CHANGELOG.md",
                      "Replace Boss gate with a typed inbox, v2"):
            self.assertEqual(len(title), TITLE_MAX_CHARS)
            measured = NSAttributedString.alloc().initWithString_attributes_(
                title, overlay.panel_title_attrs()).size().width
            self.assertLessEqual(measured, width, title)


if __name__ == "__main__":
    unittest.main()


class TheTitleStopsAtTheGlyph(unittest.TestCase):
    """A long request ran under the status glyph and off the card's edge
    ("...punctuation and noise ta") while the body beneath it ellipsised.
    The title was drawn at a point, with no width; now it gets the body's
    width and one truncated line."""

    def draw_card(self) -> str:
        source = OVERLAY_SOURCE.read_text()
        body = source[source.index("def _draw_card"):]
        return body[:body.index("# Status glyph")]

    def test_the_title_is_drawn_in_a_rect_not_at_a_point(self) -> None:
        body = self.draw_card()
        title = body[:body.index("panel_body_attrs")]
        self.assertIn("drawWithRect_options_", title)
        self.assertNotIn("drawAtPoint_", title)

    def test_title_and_body_share_the_width_that_clears_the_glyph(self) -> None:
        body = self.draw_card()
        self.assertIn("text_w = frame.size.width - 2 * PANEL_PAD - PANEL_ICON", body)
        self.assertEqual(body.count("text_w"), 3, "defined once, used by both")

    def test_the_title_is_one_ellipsised_line(self) -> None:
        if not HAVE_APPKIT:
            self.skipTest("AppKit")
        from AppKit import NSParagraphStyleAttributeName, NSLineBreakByTruncatingTail
        style = overlay.panel_title_attrs()[NSParagraphStyleAttributeName]
        self.assertEqual(style.lineBreakMode(), NSLineBreakByTruncatingTail)
        self.assertEqual(overlay.PANEL_TITLE_H, 20.0)

