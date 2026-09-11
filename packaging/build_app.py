#!/usr/bin/env -S uv run --python 3.13 --no-project --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Build the app: one signed .app with the runtime and everything inside.

    uv run --script packaging/build_app.py              # dist/Voice Agent.app
    uv run --script packaging/build_app.py --dmg        # ... and a .dmg
    uv run --script packaging/build_app.py --install    # into /Applications

What goes in, and where:

    Contents/MacOS/Voice Agent     launcher.c: CPython embedded, so the
                                   process is the app (name, icon, grants)
    Contents/Resources/python/     CPython 3.13 (uv's standalone build),
                                   with every dependency the scripts'
                                   PEP 723 blocks declare installed into it
    Contents/Resources/app/        the code: conduct.py, boss.py,
                                   conductor/, prompts/, assets/ ...
    Contents/Resources/bin/        tmux (built here, arm64) and claude
                                   (the one claude-agent-sdk ships) - the
                                   fallbacks for a Mac that has neither

Nothing is fetched at run time and nothing is written into the bundle
after it is signed: bytecode is compiled here, and the launcher turns
bytecode writing off.

Signing: every Mach-O file inside-out, then the bundle. The default is
ad-hoc (`--sign -`), which needs nothing from the keychain. `--sign
auto` takes a Developer ID Application identity if the keychain has one,
then an Apple Development one; `--sign "<name>"` names one. Using a
certificate's private key makes macOS ask for the keychain password the
first time (a SecurityAgent dialog codesign waits on), so it is never
the default. A certificate signature is what keeps macOS privacy grants
across rebuilds - to TCC an ad-hoc build is a new app every time - and
it turns on the hardened runtime. Only Developer ID can be notarized
(`--notarize <notarytool profile>`).
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from conductor.app_bundle import (APP_NAME, BUNDLE_ID,  # noqa: E402
                                  EXECUTABLE, PYTHON_VERSION)

PACKAGING = REPO / "packaging"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) \
    / "voice-agent-build"

# The scripts whose `# /// script` dependencies make up the app's one
# environment. A script not listed here runs in the app without its deps.
SCRIPTS = ("conduct.py", "voice_agent.py", "overlay.py", "hotkey.py",
           "conductor/app_mac.py", "conductor/computer.py")
# What the app needs of the repo. Tests, evals, docs and the dev
# launchers stay out.
APP_FILES = ("boss.py", "conduct.py", "voice_agent.py", "overlay.py",
             "hotkey.py", "conductor/", "prompts/", "assets/")
# Parts of the standard build the app never touches.
STDLIB_PRUNE = ("idlelib", "tkinter", "turtledemo", "turtle.py", "ensurepip",
                "lib-dynload/_tkinter.cpython-313-darwin.so",
                "config-3.13-darwin", "EXTERNALLY-MANAGED")

TMUX_VERSION = "3.5a"
TMUX_URL = (f"https://github.com/tmux/tmux/releases/download/"
            f"{TMUX_VERSION}/tmux-{TMUX_VERSION}.tar.gz")
LIBEVENT_VERSION = "2.1.12-stable"
LIBEVENT_URL = (f"https://github.com/libevent/libevent/releases/download/"
                f"release-{LIBEVENT_VERSION}/libevent-{LIBEVENT_VERSION}.tar.gz")
MIN_MACOS = "13.0"

MACHO_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
               b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}


def say(text: str) -> None:
    print(f"==> {text}", flush=True)


def run(*argv, **kw) -> subprocess.CompletedProcess:
    kw.setdefault("check", True)
    return subprocess.run([str(a) for a in argv], **kw)


def find_uv() -> str:
    uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
    if not Path(uv).exists():
        sys.exit("uv is needed to build the app: https://astral.sh/uv")
    return uv


# -- the runtime ----------------------------------------------------------

def managed_python(uv: str) -> Path:
    """uv's standalone CPython for PYTHON_VERSION, installed if missing.
    Its install directory is what gets copied: it is relocatable, and
    lib/libpython is there for the launcher to embed."""
    run(uv, "python", "install", PYTHON_VERSION, capture_output=True)
    listed = json.loads(run(
        uv, "python", "list", PYTHON_VERSION, "--only-installed",
        "--managed-python", "--output-format", "json",
        capture_output=True, text=True).stdout)
    for entry in listed:
        if entry.get("arch") != "aarch64" or entry.get("variant") not in (
                None, "default"):
            continue
        exe = Path(entry["path"])
        root = exe.parent.parent
        if (root / "lib" / "libpython3.13.dylib").exists():
            return root
    sys.exit(f"no managed arm64 CPython {PYTHON_VERSION} with libpython")


def script_dependencies(path: Path) -> list[str]:
    """The `dependencies` list of a PEP 723 `# /// script` block."""
    text = path.read_text()
    block = re.search(r"(?m)^# /// script\s*$\n((?:^#(?! ///).*$\n)*)^# ///\s*$",
                      text)
    if block is None:
        return []
    body = "\n".join(line[2:] if line.startswith("# ") else line[1:]
                     for line in block.group(1).splitlines())
    import tomllib
    return list(tomllib.loads(body).get("dependencies", []))


def requirements() -> list[str]:
    seen: list[str] = []
    for script in SCRIPTS:
        for dep in script_dependencies(REPO / script):
            if dep not in seen:
                seen.append(dep)
    return seen


def copy_runtime(source: Path, dest: Path) -> None:
    shutil.copytree(source, dest, symlinks=True)
    stdlib = dest / "lib" / "python3.13"
    for name in STDLIB_PRUNE:
        target = stdlib / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    for pattern in ("tcl9*", "tk9*", "itcl*", "thread*", "libtcl*",
                    "pkgconfig"):
        for target in (dest / "lib").glob(pattern):
            shutil.rmtree(target) if target.is_dir() else target.unlink()
    for target in ("include", "share"):
        shutil.rmtree(dest / target, ignore_errors=True)
    # The embedded library is found through the launcher's rpath; uv's
    # build names it by the absolute path it was installed at.
    run("install_name_tool", "-id", "@rpath/libpython3.13.dylib",
        dest / "lib" / "libpython3.13.dylib", capture_output=True)


def install_dependencies(uv: str, python_home: Path) -> None:
    reqs = requirements()
    say(f"installing {len(reqs)} requirement(s)")
    run(uv, "pip", "install", "--quiet", "--python",
        python_home / "bin" / "python3.13", "--break-system-packages",
        "--link-mode", "copy", "--compile-bytecode", *reqs)
    site = python_home / "lib" / "python3.13" / "site-packages"
    # pyobjc-core ships its own test suite, with debug symbols for every
    # test extension: hundreds of binaries to sign that nothing imports.
    for junk in [site / "PyObjCTest", *site.rglob("*.dSYM")]:
        shutil.rmtree(junk, ignore_errors=True)


def copy_app(dest: Path) -> None:
    tracked = run("git", "-C", REPO, "ls-files", "--cached", "--others",
                  "--exclude-standard", capture_output=True,
                  text=True).stdout.splitlines()
    for rel in tracked:
        if not any(rel == p or (p.endswith("/") and rel.startswith(p))
                   for p in APP_FILES):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, target)


def compile_bytecode(python: Path, *trees: Path) -> None:
    run(python, "-m", "compileall", "-q", "-j", "0",
        "--invalidation-mode", "unchecked-hash", *trees,
        capture_output=True)


def build_launcher(python_source: Path, out: Path) -> None:
    run("clang", "-O2", "-arch", "arm64", f"-mmacosx-version-min={MIN_MACOS}",
        f"-I{python_source / 'include' / 'python3.13'}",
        PACKAGING / "launcher.c",
        f"-L{python_source / 'lib'}", "-lpython3.13",
        "-Wl,-rpath,@executable_path/../Resources/python/lib",
        "-o", out)


# -- tmux -----------------------------------------------------------------

def fetch(url: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / url.rsplit("/", 1)[1]
    if not path.exists():
        say(f"downloading {url}")
        partial = path.with_suffix(".part")
        with urllib.request.urlopen(url, timeout=120) as response, \
                partial.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        partial.rename(path)
    return path


def built_tmux() -> Path:
    """tmux for arm64, with libevent linked in statically: one file that
    needs nothing but what every Mac has. Built once, then cached."""
    out = CACHE / f"tmux-{TMUX_VERSION}-arm64"
    if out.exists():
        return out
    CACHE.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, CFLAGS=f"-O2 -arch arm64 -mmacosx-version-min={MIN_MACOS}",
               MACOSX_DEPLOYMENT_TARGET=MIN_MACOS)
    with tempfile.TemporaryDirectory(dir=CACHE) as scratch:
        work = Path(scratch)
        prefix = work / "prefix"
        for url in (LIBEVENT_URL, TMUX_URL):
            with tarfile.open(fetch(url)) as archive:
                archive.extractall(work, filter="data")
        libevent = work / f"libevent-{LIBEVENT_VERSION}"
        say("building libevent")
        run("./configure", f"--prefix={prefix}", "--disable-shared",
            "--enable-static", "--disable-openssl", "--disable-samples",
            "--disable-libevent-regress", cwd=libevent, env=env,
            capture_output=True)
        run("make", "-j8", "install", cwd=libevent, env=env,
            capture_output=True)
        say("building tmux")
        tmux = work / f"tmux-{TMUX_VERSION}"
        run("./configure", "--disable-utf8proc",
            f"LIBEVENT_CORE_CFLAGS=-I{prefix}/include",
            f"LIBEVENT_CORE_LIBS={prefix}/lib/libevent_core.a",
            f"LIBEVENT_CFLAGS=-I{prefix}/include",
            f"LIBEVENT_LIBS={prefix}/lib/libevent.a",
            cwd=tmux, env=env, capture_output=True)
        run("make", "-j8", cwd=tmux, env=env, capture_output=True)
        shutil.copy2(tmux / "tmux", out)
    return out


# -- the bundle -----------------------------------------------------------

def version() -> tuple[str, str, str]:
    count = run("git", "-C", REPO, "rev-list", "--count", "HEAD",
                capture_output=True, text=True).stdout.strip()
    commit = run("git", "-C", REPO, "rev-parse", "--short", "HEAD",
                 capture_output=True, text=True).stdout.strip()
    return f"1.0.{count}", count, commit


def info_plist() -> dict:
    short, build, commit = version()
    return {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": EXECUTABLE,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": short,
        "CFBundleVersion": build,
        "CFBundleIconFile": "AppIcon",
        "CFBundleInfoDictionaryVersion": "6.0",
        "VoiceAgentCommit": commit,
        "LSMinimumSystemVersion": MIN_MACOS,
        "LSArchitecturePriority": ["arm64"],
        "LSApplicationCategoryType": "public.app-category.developer-tools",
        # The launched process is the conductor, which has no window; the
        # Boss window it starts turns itself into a regular app. Without
        # this the Dock bounces a tile for a process that never checks in.
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "NSSupportsAutomaticGraphicsSwitching": True,
        "NSMicrophoneUsageDescription":
            f"{APP_NAME} listens while you hold the push-to-talk key.",
        "NSAppleEventsUsageDescription":
            f"{APP_NAME} brings its own window forward, and a task you "
            "allowed to use this Mac can drive other apps.",
    }


def make_icns(png: Path, icns: Path) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        iconset = Path(scratch) / "AppIcon.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale, suffix in ((1, ""), (2, "@2x")):
                px = size * scale
                run("sips", "-z", px, px, png, "--out",
                    iconset / f"icon_{size}x{size}{suffix}.png",
                    capture_output=True)
        run("iconutil", "-c", "icns", iconset, "-o", icns)


def is_macho(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as handle:
        return handle.read(4) in MACHO_MAGIC


def signing_identity(choice: str) -> str:
    if choice != "auto":
        return choice
    listed = run("security", "find-identity", "-v", "-p", "codesigning",
                 capture_output=True, text=True).stdout
    names = re.findall(r'"([^"]+)"', listed)
    for prefix in ("Developer ID Application:", "Apple Development:"):
        for name in names:
            if name.startswith(prefix):
                return name
    return "-"


def foreign_signed(path: Path) -> bool:
    """A binary someone else signed with a Developer ID, which we ship as
    they built it: re-signing would strip entitlements it depends on
    (claude is a Bun executable that needs its JIT entitlement)."""
    shown = run("codesign", "-dv", path, capture_output=True, text=True,
                check=False).stderr
    return "Authority=Developer ID Application" in shown \
        and "TeamIdentifier=" in shown and "adhoc" not in shown


def sign(app: Path, identity: str) -> None:
    hardened = identity != "-"
    base = ["codesign", "--force", "--sign", identity]
    if hardened:
        base += ["--options", "runtime"]
        base += ["--timestamp"] if identity.startswith("Developer ID") \
            else ["--timestamp=none"]
    executable = app / "Contents" / "MacOS" / EXECUTABLE
    nested = [p for p in (app / "Contents").rglob("*")
              if is_macho(p) and p != executable]
    kept = [p for p in nested if foreign_signed(p)]
    say(f"signing {len(nested) - len(kept)} binaries as {identity!r}"
        + (f" (kept {len(kept)} vendor-signed)" if kept else ""))
    # Deepest first, so each signature seals already-signed contents.
    ordered = sorted(set(nested) - set(kept),
                     key=lambda p: len(p.parts), reverse=True)
    for start in range(0, len(ordered), 50):
        run(*base, *ordered[start:start + 50], capture_output=True)
    run(*base, "--entitlements", PACKAGING / "entitlements.plist", app,
        capture_output=True)
    run("codesign", "--verify", "--strict", "--deep", app)


def build(dest: Path, identity: str, with_tmux: bool) -> Path:
    uv = find_uv()
    python_source = managed_python(uv)
    say(f"runtime: {python_source.name}")
    dest.mkdir(parents=True, exist_ok=True)
    app = dest / f"{APP_NAME}.app"
    staging = dest / f".{APP_NAME}.app.building"
    shutil.rmtree(staging, ignore_errors=True)
    contents = staging / "Contents"
    resources = contents / "Resources"
    (contents / "MacOS").mkdir(parents=True)
    resources.mkdir()

    python_home = resources / "python"
    copy_runtime(python_source, python_home)
    install_dependencies(uv, python_home)
    say("copying the app")
    copy_app(resources / "app")
    compile_bytecode(python_home / "bin" / "python3.13", resources / "app")
    # The standalone interpreter was only needed to install into; in the
    # app the launcher is the interpreter.
    shutil.rmtree(python_home / "bin")

    say("building the launcher")
    build_launcher(python_source, contents / "MacOS" / EXECUTABLE)

    bin_dir = resources / "bin"
    bin_dir.mkdir()
    if with_tmux:
        shutil.copy2(built_tmux(), bin_dir / "tmux")
    claude = next((python_home / "lib" / "python3.13" / "site-packages"
                   / "claude_agent_sdk" / "_bundled").glob("claude"), None)
    if claude is not None:
        (bin_dir / "claude").symlink_to(os.path.relpath(claude, bin_dir))

    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(info_plist(), handle)
    (contents / "PkgInfo").write_text("APPL????")
    make_icns(REPO / "assets" / "icon.png", resources / "AppIcon.icns")

    sign(staging, identity)
    shutil.rmtree(app, ignore_errors=True)
    staging.rename(app)
    return app


def make_dmg(app: Path) -> Path:
    dmg = app.with_suffix(".dmg")
    with tempfile.TemporaryDirectory() as scratch:
        folder = Path(scratch) / APP_NAME
        folder.mkdir()
        run("ditto", app, folder / app.name)
        (folder / "Applications").symlink_to("/Applications")
        dmg.unlink(missing_ok=True)
        run("hdiutil", "create", "-quiet", "-volname", APP_NAME,
            "-srcfolder", folder, "-format", "UDZO", dmg)
    return dmg


def notarize(target: Path, profile: str) -> None:
    say(f"notarizing {target.name} (profile {profile})")
    run("xcrun", "notarytool", "submit", target, "--keychain-profile",
        profile, "--wait")
    run("xcrun", "stapler", "staple", target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dest", type=Path, default=REPO / "dist")
    parser.add_argument("--sign", default="-", metavar="IDENTITY",
                        help="codesign identity; '-' (ad-hoc, the default) "
                             "or auto (the keychain's Developer ID, then "
                             "Apple Development - asks for the keychain)")
    parser.add_argument("--no-tmux", action="store_true",
                        help="leave tmux out (it is built from source)")
    parser.add_argument("--dmg", action="store_true")
    parser.add_argument("--notarize", metavar="PROFILE",
                        help="notarytool keychain profile (Developer ID only)")
    parser.add_argument("--install", action="store_true",
                        help="copy the built app into /Applications")
    args = parser.parse_args(argv)
    if sys.platform != "darwin" or os.uname().machine != "arm64":
        sys.exit("the app is built on, and for, Apple silicon Macs")

    identity = signing_identity(args.sign)
    app = build(args.dest.resolve(), identity, with_tmux=not args.no_tmux)
    size = run("du", "-sh", app, capture_output=True, text=True).stdout.split()[0]
    say(f"built {app} ({size}, signed {identity!r})")
    target = make_dmg(app) if args.dmg else app
    if args.dmg:
        say(f"disk image {target}")
    if args.notarize:
        if not identity.startswith("Developer ID Application"):
            sys.exit("notarizing needs a Developer ID Application identity")
        notarize(target, args.notarize)
    if args.install:
        installed = Path("/Applications") / app.name
        shutil.rmtree(installed, ignore_errors=True)
        run("ditto", app, installed)
        say(f"installed {installed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
