"""The OpenAI key in the Keychain and the login shell (conductor/keystore),
and the key dialog's icon and Get a key button (conductor/dialogs)."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor import dialogs, keystore


def fake_run(stdout="", returncode=0, calls=None):
    def run(cmd, **kwargs):
        if calls is not None:
            calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")
    return run


class TheKeychain(unittest.TestCase):

    def setUp(self):
        mock.patch.object(keystore, "available", return_value=True).start()
        mock.patch.object(keystore, "application_log").start()
        self.addCleanup(mock.patch.stopall)

    def test_load_reads_the_generic_password(self):
        calls = []
        key = keystore.load(run=fake_run("sk-chain\n", calls=calls))
        self.assertEqual(key, "sk-chain")
        cmd = calls[0][0]
        self.assertEqual(cmd[:2], ["security", "find-generic-password"])
        self.assertIn(keystore.SERVICE, cmd)
        self.assertIn(keystore.ACCOUNT, cmd)
        self.assertIn("-w", cmd)

    def test_load_is_empty_when_there_is_none(self):
        self.assertEqual(keystore.load(run=fake_run(returncode=44)), "")

    def test_save_updates_in_place(self):
        calls = []
        self.assertTrue(keystore.save("sk-new", run=fake_run(calls=calls)))
        cmd = calls[0][0]
        self.assertEqual(cmd[:2], ["security", "add-generic-password"])
        self.assertIn("-U", cmd)
        self.assertEqual(cmd[-2:], ["-w", "sk-new"])

    def test_save_reports_a_keychain_that_would_not(self):
        self.assertFalse(keystore.save("sk-new", run=fake_run(returncode=1)))
        self.assertFalse(keystore.save("", run=fake_run()))

    def test_forget_deletes(self):
        calls = []
        keystore.forget(run=fake_run(calls=calls))
        self.assertEqual(calls[0][0][:2],
                         ["security", "delete-generic-password"])

    def test_a_missing_security_tool_is_no_key(self):
        def run(cmd, **kwargs):
            raise FileNotFoundError(cmd[0])
        self.assertEqual(keystore.load(run=run), "")
        self.assertFalse(keystore.save("sk", run=run))

    def test_off_a_mac_there_is_no_keychain(self):
        with mock.patch.object(keystore, "available", return_value=False):
            self.assertEqual(keystore.load(run=fake_run("sk\n")), "")
            self.assertFalse(keystore.save("sk", run=fake_run()))


class TheLoginShell(unittest.TestCase):

    def setUp(self):
        mock.patch.object(keystore, "application_log").start()
        self.addCleanup(mock.patch.stopall)

    def test_the_export_is_read_through_an_interactive_login_shell(self):
        calls = []
        key = keystore.from_login_shell(
            run=fake_run(f"{keystore._FENCE}sk-shell{keystore._FENCE}",
                         calls=calls),
            shell="/bin/zsh")
        self.assertEqual(key, "sk-shell")
        cmd, kwargs = calls[0]
        self.assertEqual(cmd[:2], ["/bin/zsh", "-lic"])
        self.assertIn("OPENAI_API_KEY", cmd[2])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_a_profiles_banner_does_not_pass_for_a_key(self):
        out = (f"Welcome back!\nlast login: today\n"
               f"{keystore._FENCE}{keystore._FENCE}\n")
        self.assertEqual(
            keystore.from_login_shell(run=fake_run(out), shell="/bin/zsh"),
            "")
        self.assertEqual(
            keystore.from_login_shell(run=fake_run("Welcome back!\n"),
                                      shell="/bin/zsh"), "")

    def test_a_hanging_profile_is_given_up_on(self):
        def run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        self.assertEqual(keystore.from_login_shell(run=run, shell="/bin/zsh"),
                         "")

    def test_no_shell_no_key(self):
        with mock.patch.dict("os.environ", {"SHELL": ""}):
            self.assertEqual(keystore.from_login_shell(run=fake_run("x")),
                             "")


class TheKeyDialog(unittest.TestCase):
    """Stock AppleScript, dressed: heygent's icon in place of the generic
    one, and a third button that opens OpenAI instead of answering."""

    def setUp(self):
        mock.patch.object(dialogs, "can_show", return_value=True).start()
        self.addCleanup(mock.patch.stopall)

    def test_the_icon_and_the_button_are_arguments_not_script(self):
        calls = []
        answer = dialogs.ask_secret("paste it", button="Get a key",
                                    icon="/tmp/heygent.icns",
                                    run=fake_run("sk-x\n", calls=calls))
        self.assertEqual(answer, "sk-x")
        cmd = calls[0][0]
        self.assertEqual(cmd[:2], ["osascript", "-"])
        self.assertEqual(cmd[2:], ["paste it", "/tmp/heygent.icns",
                                   "Get a key"])
        script = calls[0][1]["input"]
        self.assertIn("POSIX file icns", script)
        self.assertIn('return "button:" & extra', script)

    def test_without_an_icon_the_script_falls_back_to_the_stock_one(self):
        calls = []
        dialogs.ask_secret("paste it", icon="", run=fake_run(calls=calls))
        self.assertEqual(calls[0][0][2:], ["paste it", "", ""])

    def test_the_icon_is_the_outer_bundles_or_the_kept_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            kept = home / "heygent.app" / "Contents" / "Resources" / "heygent.icns"
            kept.parent.mkdir(parents=True)
            kept.write_bytes(b"icns")
            self.assertEqual(dialogs.icon_file(home), str(kept))

    def test_the_scripts_compile(self):
        if not Path("/usr/bin/osacompile").exists():
            self.skipTest("no osacompile")
        for script in (dialogs._DIALOG, dialogs._SECRET):
            with tempfile.TemporaryDirectory() as tmp:
                done = subprocess.run(
                    ["osacompile", "-o", f"{tmp}/x.scpt"], input=script,
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
