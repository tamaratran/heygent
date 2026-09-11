"""Live check: a dead input stream is noticed and rebuilt, against real
PortAudio and the real default devices.

The unit tests fake the device layer, so they prove the logic and not the
assumption underneath it - that a stream which stops delivering can be
replaced in-process without restarting the session. This runs it for real:
open the microphone, kill it the way a disconnected device does (callbacks
simply stop), and check the watchdog brings it back.

Run with:  python3 tests/smoke_audio_recovery.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import voice_agent
from voice_agent import VoiceAgent, close_quietly


async def main() -> int:
    agent = VoiceAgent.__new__(VoiceAgent)
    agent.running = True
    agent.audio_recovering = False
    agent.last_mic_at = 0.0
    agent.mic = None
    agent.mic_device = ""
    agent.mic_level = 0.0
    agent.holding = False
    agent.mic_q = asyncio.Queue(maxsize=200)
    agent.speaker = voice_agent.Speaker()
    agent.speaker.stream.start()

    loop = asyncio.get_running_loop()
    agent._open_mic(loop)
    print(f"opened on: {agent.mic_device!r} / {agent.speaker.device_name!r}")
    await asyncio.sleep(0.6)
    if time.monotonic() - agent.last_mic_at > 0.5:
        print("FAIL: no audio blocks from a freshly opened microphone")
        return 1
    first = agent.mic

    # What losing a device looks like from in here: no error, no exception,
    # the callback just stops being called.
    close_quietly(agent.mic)
    stalled_at = time.monotonic()
    await asyncio.sleep(VoiceAgent.MIC_STALL_SECONDS + 0.2)
    if agent.last_mic_at > stalled_at:
        print("FAIL: a closed stream kept delivering; the test proves nothing")
        return 1

    watch = asyncio.create_task(agent._watch_audio(loop))
    await asyncio.sleep(VoiceAgent.AUDIO_CHECK_SECONDS * 3)
    agent.running = False
    watch.cancel()

    if agent.mic is first:
        print("FAIL: the stream was never replaced")
        return 1
    await asyncio.sleep(0.6)
    silent_for = time.monotonic() - agent.last_mic_at
    if silent_for > 0.5:
        print(f"FAIL: reopened stream is not delivering ({silent_for:.1f}s)")
        return 1
    print(f"recovered on: {agent.mic_device!r}, "
          f"blocks arriving {silent_for*1000:.0f}ms ago")
    close_quietly(agent.mic)
    close_quietly(agent.speaker.stream)
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
