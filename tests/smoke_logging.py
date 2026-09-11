#!/usr/bin/env python3
"""Real subprocesses, real log file: the part unit tests cannot prove.

conduct.py pipes overlay.py's and hotkey.py's stderr into the debugging log
while leaving their stdout alone, because stdout is an NDJSON protocol. That
wiring is the riskiest thing about the logging change and no unit test
touches it - the fakes there are pipes, not the real AppKit and Quartz
children.

This launches them exactly as conduct.py does, for a couple of seconds, and
checks that their stderr reaches the log and their stdout does not.

    python3 tests/smoke_logging.py

It briefly shows the overlay on screen. It needs no API key and opens no
network connection, so it is safe to run any time; it is kept out of the
unittest suite because it spawns real UI processes.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from conductor.observability import (application_log,  # noqa: E402
                                     configure_logging,
                                     drain_subprocess_stderr)

HOLD_SECONDS = 2.5


def spawn_command() -> list[str]:
    """The same interpreter selection conduct.py uses."""
    local_uv = Path.home() / ".local/bin/uv"
    uv = str(local_uv) if local_uv.exists() else (shutil.which("uv") or "uv")
    return [uv, "run", "--python-preference", "only-managed",
            "--python", "3.13"]


async def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="smoke-logging-"))
    log_path = configure_logging(home, console=False)
    print(f"log: {log_path}")

    spawn = spawn_command()
    children, readers = {}, []
    for name in ("overlay", "hotkey"):
        proc = await asyncio.create_subprocess_exec(
            *spawn, str(HERE.parent / f"{name}.py"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        children[name] = proc
        readers.append(asyncio.create_task(
            drain_subprocess_stderr(proc.stderr, name)))
        print(f"{name}: pid {proc.pid}")

    # Drive the overlay the way Ui.send does, so its stdout protocol is
    # exercised rather than merely opened.
    overlay = children["overlay"]
    for message in ({"state": "listening", "level": 0.4},
                    {"card": {"title": "smoke", "body": "logging smoke test",
                              "status": "working"}},
                    {"state": "hidden"}):
        overlay.stdin.write((json.dumps(message) + "\n").encode())
    await overlay.stdin.drain()

    await asyncio.sleep(HOLD_SECONDS)

    for name, proc in children.items():
        if proc.returncode is None:
            proc.terminate()
    for reader in readers:
        reader.cancel()
    await asyncio.sleep(0.3)

    entries = [json.loads(line)
               for line in log_path.read_text().splitlines() if line.strip()]
    stderr_lines = [e for e in entries if e["event"].endswith(".stderr")]

    print(f"\n{len(entries)} log entries, "
          f"{len(stderr_lines)} from child stderr")
    for entry in stderr_lines[:10]:
        print(f"  [{entry['component']}] {entry['message'][:90]}")

    # SIGTERM is how this test stops them. asyncio reports -15 directly;
    # through `uv run` it arrives as 143 (128 + 15) instead.
    TERMINATED = (None, 0, -15, 143)

    ok = True
    for name, proc in children.items():
        if proc.returncode not in TERMINATED:
            print(f"FAIL: {name} exited {proc.returncode} - "
                  "its stderr above should say why")
            ok = False
    # The protocol pipe must not have been consumed as log lines.
    if any('"state"' in e["message"] or '"card"' in e["message"]
           for e in stderr_lines):
        print("FAIL: overlay stdout leaked into the log")
        ok = False
    if not any(e["event"] == "logging.configured" for e in entries):
        print("FAIL: the log was never configured")
        ok = False

    application_log("conductor", "smoke.finished", "smoke run complete",
                    ok=ok, entries=len(entries))
    print("\nPASS" if ok else "\nFAIL")
    print(f"(log kept at {log_path})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
