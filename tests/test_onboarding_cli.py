"""The CLI onboarding's line producers, tested without a terminal.

The flow itself is onboarding.OnboardingFlow (tested in test_onboarding);
here is everything the terminal front end computes: the meter line, the
keycap line, the step rail, the keycap cards, and what a picker answer
means.
"""

import re
import unittest

from hotkey import KEY_LABELS, KEY_MASKS
from onboarding import CLI_STAGES, CLI_STEPS
from onboarding_cli import (METER_WIDTH, PURPLE, key_cards, keycap_line,
                            meter_line, parse_choice, rail, transcript_line)


def plain(text: str) -> str:
    return re.sub("\x1b\\[[0-9;]*m", "", text)


class TestMeterLine(unittest.TestCase):
    def test_silence_is_all_empty_cells(self):
        self.assertEqual(meter_line(0.0), "\u2591" * METER_WIDTH)

    def test_full_scale_is_all_filled_cells(self):
        self.assertEqual(meter_line(1.0), "\u2588" * METER_WIDTH)

    def test_the_line_is_always_the_same_width(self):
        for level in (-1.0, 0.0, 0.33, 0.5, 1.0, 7.0):
            self.assertEqual(len(meter_line(level)), METER_WIDTH)

    def test_louder_is_never_shorter(self):
        fills = [meter_line(level).count("\u2588")
                 for level in (0.0, 0.25, 0.5, 0.75, 1.0)]
        self.assertEqual(fills, sorted(fills))


class TestTranscriptLine(unittest.TestCase):
    def test_empty_stays_empty(self):
        self.assertEqual(transcript_line(""), "")

    def test_short_text_passes_whole(self):
        self.assertEqual(transcript_line("testing one two"),
                         "testing one two")

    def test_long_text_keeps_its_tail(self):
        words = " ".join(f"word{i}" for i in range(40))
        line = transcript_line(words)
        self.assertEqual(len(line), 72)
        self.assertTrue(words.endswith(line))

    def test_runs_of_whitespace_collapse(self):
        self.assertEqual(transcript_line("a  b\n c"), "a b c")


class TestKeycapLine(unittest.TestCase):
    def test_every_key_names_itself(self):
        for key in KEY_MASKS:
            self.assertIn(key.capitalize() if key != "fn" else "Fn",
                          keycap_line(key, False))

    def test_held_and_up_read_differently(self):
        self.assertNotEqual(keycap_line("control", True),
                            keycap_line("control", False))
        self.assertIn("held", keycap_line("control", True))
        self.assertIn("up", keycap_line("control", False))


class TestRail(unittest.TestCase):
    def test_every_stage_label_appears_in_order(self):
        labels = [label for label, _ in CLI_STAGES]
        self.assertEqual(plain(rail("welcome")),
                         " \u203a ".join(labels))

    def test_the_active_stage_is_the_purple_one(self):
        for step in CLI_STEPS:
            label = next(name for name, steps in CLI_STAGES
                         if step in steps)
            self.assertIn(f"{PURPLE}\x1b[1m{label}", rail(step))


class TestKeyCards(unittest.TestCase):
    def test_four_lines_of_equal_width(self):
        lines = key_cards()
        self.assertEqual(len(lines), 4)
        self.assertEqual(len({len(line) for line in lines}), 1)

    def test_every_key_and_its_number_are_on_the_cards(self):
        lines = key_cards()
        for i, name in enumerate(KEY_MASKS):
            self.assertIn(KEY_LABELS[name], lines[2])
            self.assertIn(str(i + 1), lines[3])

    def test_only_the_selected_card_is_highlighted(self):
        for i in range(len(KEY_MASKS)):
            lines = key_cards(selected=i)
            self.assertEqual("".join(lines).count(PURPLE), 4)
            self.assertEqual([plain(line) for line in lines], key_cards())


class TestParseChoice(unittest.TestCase):
    def test_a_number_names_the_key_in_that_menu_slot(self):
        for i, key in enumerate(KEY_MASKS):
            self.assertEqual(parse_choice(str(i + 1)), key)

    def test_a_key_name_is_also_an_answer(self):
        for key in KEY_MASKS:
            self.assertEqual(parse_choice(key), key)
            self.assertEqual(parse_choice(f"  {key} "), key)

    def test_anything_else_is_no_answer(self):
        for answer in ("", "0", "5", "caps_lock", "yes", "⌘"):
            self.assertIsNone(parse_choice(answer))


if __name__ == "__main__":
    unittest.main()
