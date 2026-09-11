"""The onboarding step machine, tested without AppKit or a Mac.

OnboardingFlow is pure (the FnGate pattern): permission steps move on
evidence from their probes, question steps move on the user's word, and
a "no" never moves anything.
"""

import tempfile
import unittest
from pathlib import Path

from onboarding import (ACCOUNT_STEPS, CLI_STAGES, CLI_STEPS, CMUX_STEPS,
                        COPY, STEPS, OnboardingFlow, logged_in,
                        openai_key_saved, save_openai_key, saved_openai_key)


def flow(input_monitoring=False, microphone=False,
         accessibility=True, screen_recording=True):
    granted = {"input_monitoring": input_monitoring,
               "microphone": microphone,
               "accessibility": accessibility,
               "screen_recording": screen_recording}
    return OnboardingFlow({step: (lambda s=step: granted[s])
                           for step in granted}), granted


def cli_flow(openai_key=False, claude=False, cmux=True):
    granted = {"input_monitoring": True, "microphone": True,
               "accessibility": True, "screen_recording": True,
               "install_cmux": cmux,
               "openai_key": openai_key, "claude_login": claude}
    return OnboardingFlow({step: (lambda s=step: granted[s])
                           for step in granted},
                          steps=CLI_STEPS), granted


class TestOnboardingFlow(unittest.TestCase):
    def test_starts_on_the_welcome_screen(self):
        f, _ = flow()
        self.assertEqual(f.step, "welcome")
        self.assertFalse(f.finished)

    def test_every_step_has_copy(self):
        for step in STEPS:
            self.assertIn(step, COPY)

    def test_a_permission_step_does_not_move_on_confirm(self):
        f, _ = flow()
        f.confirm()                       # leave welcome
        self.assertEqual(f.step, "input_monitoring")
        f.confirm()                       # clicking cannot grant anything
        self.assertEqual(f.step, "input_monitoring")

    def test_a_permission_step_moves_when_the_grant_appears(self):
        f, granted = flow()
        f.confirm()
        self.assertFalse(f.poll())
        granted["input_monitoring"] = True
        self.assertTrue(f.poll())
        self.assertEqual(f.step, "microphone")

    def test_a_grant_given_early_is_never_asked_for(self):
        f, _ = flow(input_monitoring=True, microphone=True)
        f.confirm()                       # leave welcome
        self.assertEqual(f.step, "mic_test")

    def test_question_steps_move_on_yes_and_stay_on_no(self):
        f, _ = flow(input_monitoring=True, microphone=True)
        f.confirm()
        self.assertEqual(f.step, "mic_test")
        f.deny()
        self.assertEqual(f.step, "mic_test")
        self.assertTrue(f.denied)
        f.confirm()
        self.assertEqual(f.step, "choose_key")
        self.assertFalse(f.denied)        # a new screen starts clean
        f.choose("control")
        self.assertEqual(f.step, "hotkey_test")
        f.confirm()
        self.assertTrue(f.finished)

    def test_the_picker_moves_only_on_a_real_choice(self):
        f, _ = flow(input_monitoring=True, microphone=True)
        f.confirm()
        f.confirm()                       # mic_test -> choose_key
        self.assertEqual(f.step, "choose_key")
        f.confirm()                       # Continue cannot pick a key
        self.assertEqual(f.step, "choose_key")
        f.choose("caps_lock")             # not a supported key
        self.assertEqual(f.step, "choose_key")
        self.assertEqual(f.key, "fn")
        f.choose("option")
        self.assertEqual(f.step, "hotkey_test")
        self.assertEqual(f.key, "option")

    def test_a_choice_off_the_picker_screen_changes_nothing(self):
        f, _ = flow()
        self.assertEqual(f.step, "welcome")
        f.choose("command")
        self.assertEqual(f.step, "welcome")
        self.assertEqual(f.key, "fn")

    def test_done_is_the_end(self):
        f, _ = flow(input_monitoring=True, microphone=True)
        for _ in range(10):
            f.confirm()
            f.choose("fn")
        self.assertEqual(f.step, "done")

    def test_a_missing_probe_reads_as_not_granted(self):
        f = OnboardingFlow({})
        f.confirm()
        self.assertEqual(f.step, "input_monitoring")
        self.assertFalse(f.poll())

    def test_back_lands_on_the_previous_manual_step(self):
        f, _ = flow(input_monitoring=True, microphone=True)
        f.confirm()                       # welcome -> mic_test
        f.confirm()                       # mic_test -> choose_key
        self.assertEqual(f.step, "choose_key")
        f.back()
        self.assertEqual(f.step, "mic_test")
        f.back()                          # skips the permission steps
        self.assertEqual(f.step, "welcome")
        f.back()                          # the beginning stays put
        self.assertEqual(f.step, "welcome")

    def test_back_clears_a_recorded_denial(self):
        f, _ = flow(input_monitoring=True, microphone=True)
        f.confirm()
        f.deny()
        self.assertTrue(f.denied)
        f.back()
        self.assertFalse(f.denied)

    def test_no_title_carries_emphasis_marks(self):
        for title, body in COPY.values():
            self.assertNotIn("*", title)
            self.assertNotIn("*", body)


class TestAccountSteps(unittest.TestCase):
    """The CLI flow's account screens: skipped when already signed in,
    advanced by confirm after the screen's action, never by poll."""

    def finish_setup(self, f):
        f.confirm()                       # welcome -> mic_test
        f.confirm()                       # mic_test -> choose_key
        f.choose("fn")                    # -> hotkey_test
        f.confirm()

    def test_every_cli_step_has_copy_and_a_stage(self):
        staged = [s for _, steps in CLI_STAGES for s in steps]
        for step in CLI_STEPS:
            self.assertIn(step, COPY)
            self.assertIn(step, staged)

    def test_the_default_flow_carries_no_account_steps(self):
        for step in ACCOUNT_STEPS + CMUX_STEPS:
            self.assertNotIn(step, STEPS)

    def test_account_steps_follow_the_hotkey_check(self):
        f, _ = cli_flow()
        self.finish_setup(f)
        self.assertEqual(f.step, "openai_key")
        self.assertFalse(f.finished)

    def test_an_account_already_connected_is_never_asked_for(self):
        f, _ = cli_flow(openai_key=True, claude=True)
        self.finish_setup(f)
        self.assertTrue(f.finished)

    def test_confirm_moves_an_account_step(self):
        f, _ = cli_flow()
        self.finish_setup(f)
        self.assertFalse(f.poll())        # no probe change, no movement
        f.confirm()
        self.assertEqual(f.step, "claude_login")
        f.confirm()
        self.assertTrue(f.finished)

    def test_a_key_saved_mid_screen_skips_the_next_connected_account(self):
        f, granted = cli_flow(claude=True)
        self.finish_setup(f)
        granted["openai_key"] = True
        f.confirm()
        self.assertTrue(f.finished)


class TestComputerUseSteps(unittest.TestCase):
    """The computer-use grants advance like the other permission steps,
    and the cmux install is skipped on a machine that already has it."""

    def test_computer_use_grants_are_permission_steps(self):
        for step in ("accessibility", "screen_recording"):
            self.assertIn(step, STEPS)
            self.assertIn(step, OnboardingFlow.AUTO)

    def test_a_missing_computer_use_grant_waits_for_evidence(self):
        f, granted = flow(input_monitoring=True, microphone=True,
                          accessibility=False)
        f.confirm()                       # leave welcome
        self.assertEqual(f.step, "accessibility")
        f.confirm()                       # clicking cannot grant anything
        self.assertEqual(f.step, "accessibility")
        granted["accessibility"] = True
        self.assertTrue(f.poll())
        self.assertEqual(f.step, "mic_test")

    def test_an_installed_cmux_is_never_asked_about(self):
        f, _ = cli_flow(cmux=True)
        f.confirm()                       # welcome -> mic_test
        f.confirm()                       # mic_test -> choose_key
        f.choose("fn")
        f.confirm()                       # hotkey_test -> past cmux
        self.assertEqual(f.step, "openai_key")

    def test_a_missing_cmux_is_installed_on_confirm(self):
        f, _ = cli_flow(cmux=False)
        f.confirm()
        f.confirm()
        f.choose("fn")
        f.confirm()
        self.assertEqual(f.step, "install_cmux")
        self.assertFalse(f.poll())        # no probe change, no movement
        f.confirm()
        self.assertEqual(f.step, "openai_key")


class TestOpenAIKeyFile(unittest.TestCase):
    def path(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name) / ".env"

    def test_a_missing_file_reads_as_no_key(self):
        path = self.path()
        self.assertEqual(saved_openai_key(path), "")
        self.assertFalse(openai_key_saved(path))

    def test_a_saved_key_reads_back(self):
        path = self.path()
        save_openai_key("sk-test-123", path)
        self.assertEqual(saved_openai_key(path), "sk-test-123")
        self.assertTrue(openai_key_saved(path))

    def test_saving_replaces_the_old_key_and_keeps_the_rest(self):
        path = self.path()
        path.write_text("OPENAI_API_KEY=old\nOTHER=kept\n")
        save_openai_key("new", path)
        self.assertEqual(saved_openai_key(path), "new")
        self.assertIn("OTHER=kept", path.read_text())
        self.assertEqual(path.read_text().count("OPENAI_API_KEY"), 1)

    def test_quotes_and_comments_read_the_way_load_env_reads_them(self):
        path = self.path()
        path.write_text('# a comment\nOPENAI_API_KEY="quoted"\n')
        self.assertEqual(saved_openai_key(path), "quoted")

    def test_a_blank_value_reads_as_no_key(self):
        path = self.path()
        path.write_text("OPENAI_API_KEY=\n")
        self.assertFalse(openai_key_saved(path))


class TestLoggedIn(unittest.TestCase):
    def test_a_true_logged_in_reads_signed_in(self):
        self.assertTrue(logged_in('{"loggedIn": true}'))

    def test_false_null_and_absent_read_signed_out(self):
        for text in ('{"loggedIn": false}', '{"loggedIn": null}', "{}"):
            self.assertFalse(logged_in(text))

    def test_anything_that_is_not_json_reads_signed_out(self):
        for text in ("", "not json", "[]", "true"):
            self.assertFalse(logged_in(text))


if __name__ == "__main__":
    unittest.main()
