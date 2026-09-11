"""The .app bundle builder: a real macOS application around conduct.sh."""
import plistlib
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
        self.assertEqual(app.name, "Voice Agent.app")
        with (app / "Contents" / "Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
        self.assertEqual(info["CFBundleIdentifier"], app_bundle.BUNDLE_ID)
        self.assertEqual(info["CFBundleExecutable"], "voice-agent")
        self.assertIn("NSMicrophoneUsageDescription", info)

    def test_the_launcher_execs_the_repos_conduct_sh(self):
        app = self.build()
        launcher = app / "Contents" / "MacOS" / "voice-agent"
        body = launcher.read_text()
        self.assertIn(str(self.repo / "conduct.sh"), body)
        self.assertTrue(launcher.stat().st_mode & 0o111,
                        "the executable is not executable")

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
