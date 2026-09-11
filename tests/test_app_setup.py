"""The app's first run: the key, the grants, Claude Code - with fakes for
the network, the probes and the system prompts."""
import io
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from conductor import app_setup, gui_permissions

REPO = Path(__file__).resolve().parent.parent
GOOD = "sk-good-0123456789abcdef"


class Response:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_answering(status):
    seen = []

    def opener(request, timeout):
        seen.append(request)
        if status == "down":
            raise urllib.error.URLError("no route to host")
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "", {},
                                         io.BytesIO())
        return Response(status)
    opener.seen = seen
    return opener


class TheKey(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.home = Path(self.scratch.name)

    def test_it_is_saved_readable_by_the_user_alone(self):
        path = app_setup.save_key(self.home, GOOD)
        self.assertEqual(app_setup.read_key(self.home), GOOD)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_saving_replaces_the_key_and_keeps_every_other_line(self):
        (self.home / ".env").write_text(
            "# mine\nOPENAI_API_KEY=sk-old-000000000000000000\n"
            "CLAUDE_CODE_OAUTH_TOKEN=abc\n")
        app_setup.save_key(self.home, GOOD)
        text = (self.home / ".env").read_text()
        self.assertIn("# mine", text)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN=abc", text)
        self.assertEqual(text.count("OPENAI_API_KEY="), 1)
        self.assertEqual(app_setup.read_key(self.home), GOOD)

    def test_no_file_is_no_key(self):
        self.assertEqual(app_setup.read_key(self.home), "")

    def test_the_check_asks_for_the_voice_models_record(self):
        opener = opener_answering(200)
        result = app_setup.check_key(GOOD, "gpt-live-1", opener=opener,
                                     base="http://fake/v1")
        self.assertTrue(result.ok)
        request = opener.seen[0]
        self.assertEqual(request.full_url, "http://fake/v1/models/gpt-live-1")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {GOOD}")

    def test_what_each_answer_means(self):
        for status, ok, reached, words in (
                (401, False, True, "didn't accept"),
                (404, False, True, "no access to gpt-live-1"),
                (403, False, True, "no access to gpt-live-1"),
                (500, False, True, "500"),
                ("down", False, False, "Couldn't reach OpenAI")):
            result = app_setup.check_key(GOOD, "gpt-live-1",
                                         opener=opener_answering(status))
            self.assertEqual((result.ok, result.reached), (ok, reached),
                             status)
            self.assertIn(words, result.message)

    def test_something_that_is_not_a_key_never_leaves_the_machine(self):
        for text in ("", "hello", "sk-short", "sk-has space 0123456789"):
            opener = opener_answering(200)
            result = app_setup.check_key(text, "gpt-live-1", opener=opener)
            self.assertFalse(result.ok)
            self.assertEqual(opener.seen, [], text)


class TheWholeState(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.home = Path(self.scratch.name)
        self.signed_in = {"path": "/bin/claude", "signed_in": True}

    def test_a_key_and_claude_are_what_start_waits_for(self):
        grants = {"microphone": "granted"}
        view = app_setup.state(self.home, grants, {"path": ""})
        self.assertEqual(view["blocking"], ["key", "claude"])
        app_setup.save_key(self.home, GOOD)
        view = app_setup.state(self.home, grants,
                               {"path": "/bin/claude", "signed_in": False})
        self.assertEqual(view["blocking"], ["claude"])
        view = app_setup.state(self.home, grants, self.signed_in)
        self.assertEqual(view["blocking"], [])
        self.assertEqual(view["key"]["hint"], "sk-…cdef")
        self.assertNotIn(GOOD, json.dumps(view))

    def test_missing_grants_are_said_but_never_block(self):
        app_setup.save_key(self.home, GOOD)
        view = app_setup.state(self.home, {"microphone": "denied"},
                               self.signed_in)
        self.assertEqual(view["blocking"], [])
        self.assertEqual(view["missing_required"],
                         ["microphone", "input_monitoring"])

    def test_setup_is_needed_until_a_key_and_a_start(self):
        self.assertTrue(app_setup.needs_setup(self.home))
        app_setup.save_key(self.home, GOOD)
        self.assertTrue(app_setup.needs_setup(self.home))
        app_setup.mark_completed(self.home, {})
        self.assertFalse(app_setup.needs_setup(self.home))
        (self.home / ".env").unlink()
        self.assertTrue(app_setup.needs_setup(self.home))

    def test_a_probe_that_fails_reads_as_unknown(self):
        def broken(*args, **kwargs):
            return subprocess.CompletedProcess(args, 1, "Traceback", "")
        answers = app_setup.probe_in_child(run=broken)
        self.assertEqual(set(answers.values()), {"unknown"})
        self.assertEqual(set(answers), set(app_setup.GRANTS_BY_ID))

    def test_the_probe_runs_in_a_new_process_of_this_app(self):
        seen = []

        def run(argv, **kwargs):
            seen.append(argv)
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"microphone": "granted"}), "")
        answers = app_setup.probe_in_child(run=run)
        self.assertEqual(seen[0][1:], ["-m", "conductor.app_setup", "--probe"])
        self.assertEqual(answers["microphone"], "granted")
        self.assertEqual(answers["accessibility"], "unknown")


class ClaudeStatus(unittest.TestCase):
    def test_signed_in_is_read_from_auth_status(self):
        def run(argv, **kwargs):
            self.assertEqual(argv, ["/u/claude", "auth", "status", "--json"])
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"loggedIn": True,
                                     "authMethod": "claude.ai"}), "")
        status = app_setup.claude_status(which=lambda name: "/u/claude",
                                         run=run)
        self.assertEqual((status["signed_in"], status["method"]),
                         (True, "claude.ai"))

    def test_no_claude_at_all(self):
        status = app_setup.claude_status(which=lambda name: None)
        self.assertEqual((status["path"], status["signed_in"]), ("", None))

    def test_an_answer_that_is_not_json_is_unknown(self):
        status = app_setup.claude_status(
            which=lambda name: "/u/claude",
            run=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", ""))
        self.assertIsNone(status["signed_in"])

    def test_the_apps_own_claude_is_named_as_such(self):
        with mock.patch("conductor.app_bundle.bundled_bin",
                        return_value=Path("/A.app/Contents/Resources/bin")):
            status = app_setup.claude_status(
                which=lambda name: "/A.app/Contents/Resources/bin/claude",
                run=lambda argv, **kw: subprocess.CompletedProcess(
                    argv, 0, '{"loggedIn": false}', ""))
        self.assertTrue(status["bundled"])
        self.assertFalse(status["signed_in"])


class Controller(unittest.TestCase):
    """The page's messages, handled synchronously with fakes."""

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.home = Path(self.scratch.name)
        self.pushed, self.requested, self.opened, self.finished = [], [], [], []
        self.grants = {"microphone": "unknown", "input_monitoring": "missing",
                       "accessibility": "missing", "screen_recording": "denied"}
        self.claude = {"path": "/u/claude", "signed_in": True}
        self.prompt_answers = {}
        self.statuses = {GOOD: 200}
        self.spawned = []
        self.controller = app_setup.SetupController(
            self.home, model="gpt-live-1", push=self.pushed.append,
            request=self.request, open_url=self.opened.append,
            finish=self.finished.append,
            probe=lambda: dict(self.grants), claude=lambda: dict(self.claude),
            key_checker=self.check, spawn=self.spawn,
            background=lambda work: work())

    def request(self, grant):
        self.requested.append(grant)
        return self.prompt_answers.get(grant, True)

    def check(self, key, model, base):
        status = self.statuses.get(key, 401)
        return app_setup.check_key(key, model,
                                   opener=opener_answering(status))

    def spawn(self, argv, **kwargs):
        self.spawned.append(argv)
        process = mock.Mock()
        process.poll.return_value = 0
        process.wait.return_value = 0
        return process

    @property
    def last(self):
        return self.pushed[-1]

    def test_ready_draws_then_reads_the_grants(self):
        self.controller.handle({"action": "ready"})
        self.assertEqual(self.last["grants"][3]["status"], "denied")
        self.assertEqual(self.last["claude"]["path"], "/u/claude")

    def test_a_good_key_is_checked_then_saved(self):
        self.controller.handle({"action": "check_key", "key": GOOD})
        self.assertEqual(app_setup.read_key(self.home), GOOD)
        self.assertTrue(self.last["key"]["saved"])
        self.assertTrue(self.last["key"]["ok"])
        self.assertIn("Checking", self.pushed[0]["key"]["message"])

    def test_a_refused_key_is_never_saved(self):
        self.controller.handle({"action": "check_key",
                                "key": "sk-wrong-0123456789abcdef"})
        self.assertEqual(app_setup.read_key(self.home), "")
        self.assertIs(self.last["key"]["ok"], False)
        self.assertIn("didn't accept", self.last["key"]["message"])

    def test_offline_the_user_may_save_it_unchecked(self):
        self.statuses[GOOD] = "down"
        self.controller.handle({"action": "check_key", "key": GOOD})
        self.assertEqual(app_setup.read_key(self.home), "")
        self.assertTrue(self.last["key"]["unreached"])
        self.controller.handle({"action": "save_key", "key": GOOD})
        self.assertEqual(app_setup.read_key(self.home), GOOD)
        self.assertFalse(self.last["key"]["unreached"])

    def test_changing_the_key_forgets_the_last_verdict(self):
        self.controller.handle({"action": "check_key", "key": GOOD})
        self.controller.handle({"action": "change_key"})
        self.assertIsNone(self.last["key"]["ok"])
        self.assertEqual(self.last["key"]["message"], "")

    def test_the_first_press_asks_macos_and_the_second_opens_settings(self):
        self.controller.handle({"action": "ready"})
        self.controller.handle({"action": "grant", "grant": "accessibility"})
        self.assertEqual((self.requested, self.opened), (["accessibility"], []))
        self.controller.handle({"action": "grant", "grant": "accessibility"})
        self.assertEqual(self.requested, ["accessibility"])
        self.assertEqual(self.opened, [app_setup.GRANTS_BY_ID[
            "accessibility"].pane])
        self.assertEqual(self.last["asked"], ["accessibility"])

    def test_a_denied_grant_goes_straight_to_its_pane(self):
        self.controller.handle({"action": "ready"})
        self.controller.handle({"action": "grant", "grant": "screen_recording"})
        self.assertEqual(self.requested, [])
        self.assertIn("Privacy_ScreenCapture", self.opened[0])

    def test_no_prompt_left_to_raise_opens_the_pane(self):
        self.prompt_answers["input_monitoring"] = False
        self.controller.handle({"action": "grant", "grant": "input_monitoring"})
        self.assertIn("Privacy_ListenEvent", self.opened[0])

    def test_an_unknown_grant_is_ignored(self):
        self.controller.handle({"action": "grant", "grant": "camera"})
        self.assertEqual((self.requested, self.opened), ([], []))

    def test_start_waits_for_a_key(self):
        self.controller.handle({"action": "start"})
        self.assertEqual(self.finished, [])
        self.assertFalse(app_setup.completed(self.home))

    def test_start_records_setup_and_the_asks(self):
        self.controller.handle({"action": "ready"})
        self.controller.handle({"action": "check_key", "key": GOOD})
        self.controller.handle({"action": "grant", "grant": "accessibility"})
        self.controller.handle({"action": "start"})
        self.assertEqual(self.finished, [0])
        self.assertTrue(app_setup.completed(self.home))
        asked, introduced = gui_permissions._load(
            self.home / gui_permissions.STATE_FILE)
        # Everything not granted when Start was pressed, clicked or not.
        self.assertEqual(sorted(asked), ["accessibility", "input_monitoring",
                                         "microphone", "screen_recording"])
        self.assertEqual(introduced, set(gui_permissions.PANES))

    def test_signing_in_runs_claudes_own_login(self):
        self.claude["signed_in"] = False
        self.controller.handle({"action": "ready"})
        self.claude["signed_in"] = True          # what the browser did
        self.controller.handle({"action": "claude_login"})
        self.assertEqual(self.spawned, [["/u/claude", "auth", "login"]])
        self.assertTrue(self.last["claude"]["signed_in"])

    def test_the_keys_link_opens_openais_page(self):
        self.controller.handle({"action": "open", "what": "keys"})
        self.assertEqual(self.opened, [app_setup.KEYS_PAGE])

    def test_every_action_the_page_sends_is_handled(self):
        page = (REPO / "conductor" / "assets" / "setup.html").read_text()
        sent = set(re.findall(r'action: "(\w+)"', page))
        source = (REPO / "conductor" / "app_setup.py").read_text()
        handled = set(re.findall(r'action == "(\w+)"', source))
        self.assertTrue(sent)
        self.assertLessEqual(sent, handled)


if __name__ == "__main__":
    unittest.main()
