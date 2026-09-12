"""The .app bundle builder: a real macOS application around conduct.sh."""
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor import app_bundle


class TheBundle(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.repo = Path(self.scratch.name) / "repo"
        self.repo.mkdir()
        (self.repo / "conduct.sh").write_text("#!/bin/bash\n")
        (self.repo / "assets").mkdir()
        self.dest = Path(self.scratch.name) / "dist"
        self.dest.mkdir()

    def build(self):
        with mock.patch.object(app_bundle, "make_icns", return_value=False), \
             mock.patch.object(app_bundle.shutil, "which",
                               return_value=None):
            return app_bundle.build_bundle(self.repo, self.dest)

    def test_it_is_a_complete_app_bundle(self):
        app = self.build()
        self.assertEqual(app.name, "heygent.app")
        with (app / "Contents" / "Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
        self.assertEqual(info["CFBundleIdentifier"], app_bundle.BUNDLE_ID)
        self.assertEqual(info["CFBundleExecutable"], "heygent")
        self.assertIn("NSMicrophoneUsageDescription", info)

    def test_the_launcher_execs_the_repos_conduct_sh(self):
        app = self.build()
        launcher = app / "Contents" / "MacOS" / "heygent"
        body = launcher.read_text()
        self.assertIn(str(self.repo / "conduct.sh"), body)
        self.assertTrue(launcher.stat().st_mode & 0o111,
                        "the executable is not executable")

    @unittest.skipUnless(shutil.which("cc"), "needs a C compiler")
    def test_with_a_compiler_the_executable_is_a_mach_o_stub(self):
        with mock.patch.object(app_bundle, "make_icns", return_value=False):
            app = app_bundle.build_bundle(self.repo, self.dest)
        macos = app / "Contents" / "MacOS"
        with (macos / "heygent").open("rb") as handle:
            self.assertIn(handle.read(4), (b"\xcf\xfa\xed\xfe",
                                           b"\xca\xfe\xba\xbe"),
                          "the executable is not Mach-O")
        script = macos / "heygent.sh"
        self.assertIn(str(self.repo / "conduct.sh"), script.read_text())
        self.assertTrue(script.stat().st_mode & 0o111)
        # A run of the stub execs the script: give it a script that
        # leaves a mark, and check the mark.
        mark = Path(self.scratch.name) / "ran"
        script.write_text(f"#!/bin/bash\ntouch '{mark}'\n")
        subprocess.run([str(macos / "heygent")], check=True,
                       timeout=30)
        self.assertTrue(mark.is_file())

    def test_a_standalone_bundle_carries_the_repo(self):
        (self.repo / "conductor").mkdir()
        (self.repo / "conductor" / "x.py").write_text("")
        (self.repo / ".env").write_text("OPENAI_API_KEY=secret\n")
        with mock.patch.object(app_bundle, "make_icns", return_value=False), \
             mock.patch.object(app_bundle.shutil, "which",
                               return_value=None):
            app = app_bundle.build_bundle(self.repo, self.dest,
                                          standalone=True)
        payload = app / "Contents" / "Resources" / "app"
        self.assertTrue((payload / "conduct.sh").is_file())
        self.assertTrue((payload / "conductor" / "x.py").is_file())
        self.assertTrue((payload / app_bundle.STAMP).is_file())
        self.assertFalse((payload / ".env").exists(),
                         "a secret travelled inside the bundle")
        body = (app / "Contents" / "MacOS" / "heygent").read_text()
        self.assertNotIn(str(self.repo), body,
                         "the standalone launcher points at this checkout")
        self.assertIn("Resources/app", body)

    def test_the_standalone_first_run_installs_quietly(self):
        """No Terminal window dumped on the user: with a tool missing the
        launcher runs install.sh itself (behind a dialog) and then
        conduct.sh; with everything there, conduct.sh alone. The OpenAI
        key is conduct.py's to ask for, not a reason to open Terminal."""
        home = Path(self.scratch.name) / "home"
        app_dir = home / "app"
        app_dir.mkdir(parents=True)
        log = Path(self.scratch.name) / "calls.log"
        (app_dir / "install.sh").write_text(
            f"#!/bin/bash\necho install >> '{log}'\n")
        (app_dir / "conduct.sh").write_text(
            f"#!/bin/bash\necho conduct >> '{log}'\n")
        (app_dir / app_bundle.STAMP).write_text("v")
        for name in ("install.sh", "conduct.sh"):
            (app_dir / name).chmod(0o755)
        fake_bin = Path(self.scratch.name) / "bin"
        fake_bin.mkdir()
        for tool in ("uv", "tmux", "claude", "osascript"):
            (fake_bin / tool).write_text(
                f"#!/bin/bash\necho {tool} >> '{log}'\n")
            (fake_bin / tool).chmod(0o755)
        bundle = Path(self.scratch.name) / "heygent.app" / "Contents"
        (bundle / "Resources" / "app").mkdir(parents=True)
        (bundle / "Resources" / "app" / app_bundle.STAMP).write_text("v")
        (bundle / "MacOS").mkdir()
        launcher = bundle / "MacOS" / "heygent"
        # The launcher hard-codes Homebrew's bin, where this machine may
        # well have a real tmux; the fake bin stands in for it.
        script = app_bundle.STANDALONE_LAUNCHER.replace(
            "{stamp}", app_bundle.STAMP).replace(
            "/opt/homebrew/bin:/usr/local/bin", str(fake_bin))
        launcher.write_text(script)
        env = {"HOME": str(home), "VOICE_CONDUCTOR_HOME": str(home),
               "PATH": "/usr/bin:/bin"}

        def run():
            log.write_text("")
            subprocess.run(["bash", str(launcher)], env=env, check=True,
                           timeout=30)
            return log.read_text().split()

        self.assertEqual(run(), ["conduct"], "everything there, no fuss")
        (fake_bin / "tmux").unlink()
        calls = run()
        self.assertIn("install", calls)
        self.assertEqual(calls[-1], "conduct")
        quiet_path = script.split("missing=")[1].split("install.sh failed")[0]
        self.assertNotIn("Terminal", quiet_path,
                         "Terminal is for a failed install only")

    def test_a_repo_without_conduct_sh_is_refused(self):
        bare = Path(self.scratch.name) / "bare"
        bare.mkdir()
        with self.assertRaises(FileNotFoundError):
            app_bundle.build_bundle(bare, self.dest)

    def test_rebuilding_over_an_existing_bundle_succeeds(self):
        self.build()
        self.build()


if __name__ == "__main__":
    unittest.main()
