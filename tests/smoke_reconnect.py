"""Live check: a REAL dropped WebSocket comes back, twice.

The unit tests fake _one_connection, so they prove the loop's decisions
and not the transport underneath them. This runs the real aiohttp path -
real ws_connect, a real CLOSE frame, a real reconnect - against a local
server that hangs up on the first two connections and stays open on the
third. No API key, no network.

Run with:  python3 tests/smoke_reconnect.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Before the import: the transcript path is read when voice_agent loads,
# and this run's fake sessions must not land in the real conversation.
os.environ["VOICE_TRANSCRIPT_LOG"] = os.path.join(
    tempfile.mkdtemp(prefix="smoke-reconnect-"), "session.jsonl")

import aiohttp
from aiohttp import web

import voice_agent
from voice_agent import VoiceAgent

HANGUPS = 2                      # connections the server drops on purpose


async def main() -> int:
    connections = {"count": 0}
    third_is_up = asyncio.Event()

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        connections["count"] += 1
        n = connections["count"]
        # the client always opens with session.update
        first = await ws.receive()
        assert first.type == aiohttp.WSMsgType.TEXT, first.type
        await ws.send_json({"type": "session.started",
                            "session": {"id": f"fake-{n}"}})
        if n <= HANGUPS:
            await asyncio.sleep(0.2)
            await ws.close()          # a real CLOSE frame, like tonight's
            return ws
        third_is_up.set()
        async for _ in ws:            # stay open until the client leaves
            pass
        return ws

    app = web.Application()
    app.router.add_get("/live", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    agent = VoiceAgent.__new__(VoiceAgent)
    agent.api_key = "fake"
    agent.ws = None
    agent.stopping = False
    agent.running = True
    agent.holding = False
    agent.muted_turn = False
    agent.awaiting_first_audio = False
    agent.released_at = 0.0
    agent.trace_id = ""
    agent.event_id = 0
    agent.mic = None
    agent.mic_q = asyncio.Queue()
    agent.speaker = mock.Mock()
    agent.ui = mock.Mock()
    agent.ui.proc.stdout = None
    agent._open_mic = lambda loop: None
    agent._pump_mic = mock.AsyncMock()
    agent._pump_ui = mock.AsyncMock()
    agent._watch_audio = mock.AsyncMock()
    reconnects = []
    agent._emit = lambda event_type, *a, **k: (
        reconnects.append(k.get("data")) if event_type == "voice.session_reconnected" else None)

    with mock.patch.object(voice_agent, "LIVE_WS", f"ws://127.0.0.1:{port}/live"), \
         mock.patch.object(voice_agent.sd, "query_devices", return_value={"name": "x"}), \
         mock.patch.object(voice_agent, "close_quietly", lambda s: None), \
         mock.patch.object(VoiceAgent, "RECONNECT_FLOOR", 0.05):
        run = asyncio.create_task(agent.run())
        try:
            await asyncio.wait_for(third_is_up.wait(), timeout=10)
        except asyncio.TimeoutError:
            print(f"FAIL: never reached a third connection "
                  f"(server saw {connections['count']})")
            run.cancel()
            await runner.cleanup()
            return 1
        agent.stopping = True
        if agent.ws is not None and not agent.ws.closed:
            await agent.ws.close()
        await asyncio.wait_for(run, timeout=5)

    await runner.cleanup()
    print(f"server hung up {HANGUPS} times; client reconnected "
          f"{len(reconnects)} times; connections seen: {connections['count']}")
    if connections["count"] != HANGUPS + 1 or len(reconnects) != HANGUPS:
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
