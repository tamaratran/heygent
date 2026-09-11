"""The app as a bundle: where it is, how its children start, what the
build puts in it."""
import importlib.util
import plistlib
import re
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from conductor import app_bundle

REPO = Path(__file__).resolve().parent.parent
APP_EXE = "/Applications/Voice Agent.app/Contents/MacOS/Voice Agent"


def load_build_app():
    spec = importlib.util.spec_from_file_location(
        "build_app", REPO / "packaging" / "build_app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WhereTheAppIs(unittest.TestCase):
    def test_the_bundles_executable_is_the_app(self):
        self.assertEqual(app_bundle.bundle_root(APP_EXE),
                         Path("/Applications/Voice Agent.app"))
        self.assertTrue(app_bundle.inside_bundle(APP_EXE))
        self.assertEqual(
            app_bundle.bundled_bin(APP_EXE),
            Path("/Applications/Voice Agent.app/Contents/Resources/bin"))

    def test_a_checkouts_interpreter_is_not(self):
        for exe in ("/Users/x/.cache/uv/environments-v2/conduct/bin/python3",
                    "/usr/bin/python3",
                    "/Somewhere/Contents/MacOS/python"):   # no .app around it
            self.assertIsNone(app_bundle.bundle_root(exe), exe)
            self.assertIsNone(app_bundle.bundled_bin(exe), exe)

    def test_in_the_app_a_script_runs_on_its_own_executable(self):
        uv = ["/uv", "run", "--script"]
        self.assertEqual(app_bundle.script_argv("/app/overlay.py", uv, APP_EXE),
                         [APP_EXE, "/app/overlay.py"])

    def test_from_a_checkout_the_callers_uv_command_is_kept(self):
        uv = ["/uv", "run", "--python-preference", "only-managed"]
        self.assertEqual(
            app_bundle.script_argv("/repo/overlay.py", uv, "/usr/bin/python3"),
            [*uv, "/repo/overlay.py"])


class WhatTheBuildPutsIn(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = load_build_app()

    def test_every_script_with_dependencies_is_part_of_the_one_environment(self):
        declared = {str(path.relative_to(REPO))
                    for path in [*REPO.glob("*.py"),
                                 *(REPO / "conductor").glob("*.py")]
                    if "# /// script" in path.read_text()
                    and self.build.script_dependencies(path)}
        self.assertEqual(declared, set(self.build.SCRIPTS))

    def test_the_scripts_agree_on_one_pyobjc(self):
        """One environment holds every script's dependencies, so a pin one
        script disagrees on would silently resolve for all of them."""
        pins = {re.sub(r"^pyobjc-framework-\w+", "", dep).split(";")[0]
                for dep in self.build.requirements()
                if dep.startswith("pyobjc")}
        self.assertEqual(len(pins), 1, pins)

    def test_the_app_code_covers_what_the_entry_points_import(self):
        files = self.build.APP_FILES
        for name in ("boss.py", "conduct.py", "voice_agent.py", "overlay.py",
                     "hotkey.py", "conductor/", "prompts/", "assets/"):
            self.assertIn(name, files)
        self.assertFalse(any(name.startswith("tests") for name in files))

    def test_info_plist_names_the_app_and_why_it_listens(self):
        info = self.build.info_plist()
        self.assertEqual(info["CFBundleIdentifier"], app_bundle.BUNDLE_ID)
        self.assertEqual(info["CFBundleExecutable"], app_bundle.EXECUTABLE)
        self.assertEqual(info["CFBundleName"], app_bundle.APP_NAME)
        self.assertTrue(info["LSUIElement"])
        self.assertIn("NSMicrophoneUsageDescription", info)
        self.assertIn("NSAppleEventsUsageDescription", info)
        plistlib.dumps(info)                        # it serialises

    def test_the_entitlements_let_the_hardened_app_use_the_microphone(self):
        with (REPO / "packaging" / "entitlements.plist").open("rb") as handle:
            entitlements = plistlib.load(handle)
        self.assertIs(entitlements["com.apple.security.device.audio-input"],
                      True)

    def test_the_launcher_starts_the_launch_module(self):
        source = (REPO / "packaging" / "launcher.c").read_text()
        match = re.search(r'"-m";\s*args\[kept\+\+\] = "([\w.]+)"', source)
        self.assertIsNotNone(match)
        self.assertTrue((REPO / (match.group(1).replace(".", "/") + ".py"))
                        .is_file())

    def test_signing_defaults_to_ad_hoc(self):
        """A certificate's key makes macOS show a keychain dialog that
        codesign waits on; a default build must never raise one."""
        with unittest.mock.patch.object(self.build, "build") as build, \
             unittest.mock.patch.object(self.build, "run"), \
             unittest.mock.patch.object(self.build, "say"), \
             unittest.mock.patch.object(self.build.os, "uname") as uname, \
             unittest.mock.patch.object(self.build.sys, "platform", "darwin"):
            uname.return_value.machine = "arm64"
            build.return_value = Path("/dist/Voice Agent.app")
            self.build.main([])
        self.assertEqual(build.call_args.args[1], "-")
        self.assertEqual(self.build.signing_identity("-"), "-")


class TheDependencyBlockParser(unittest.TestCase):
    def test_it_reads_a_pep_723_block_with_comments(self):
        build = load_build_app()
        text = ("#!/usr/bin/env -S uv run --script\n"
                "# /// script\n"
                "# requires-python = \">=3.11\"\n"
                "# dependencies = [\n"
                "#   # why this one\n"
                "#   \"aiohttp>=3.10,<4\",\n"
                "#   \"pyobjc-framework-Quartz>=10,<13; sys_platform == 'darwin'\",\n"
                "# ]\n"
                "# ///\n"
                "print('hi')\n")
        with tempfile.TemporaryDirectory() as scratch:
            script = Path(scratch) / "script.py"
            script.write_text(text)
            self.assertEqual(build.script_dependencies(script), [
                "aiohttp>=3.10,<4",
                "pyobjc-framework-Quartz>=10,<13; sys_platform == 'darwin'"])


if __name__ == "__main__":
    unittest.main()
