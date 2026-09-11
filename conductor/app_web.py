"""The Boss's conversation, drawn in a browser.

conductor/app_view.py draws the app-server's turns as terminal cards,
and the terminal is the ceiling: buttons are OSC 8 links that need a
⌘-click, colour is a 256-entry palette, and a card is box-drawing
characters. A browser has none of those limits, so this is the same
front end again - the same CodexApp transport, the same mention rule
for the toast, the same "an approval blocks until a button settles it"
- rendered as an actual page:

  - a turn is a chat bubble, streaming as the deltas arrive;
  - an approval is a card with real buttons that answer the request
    Codex is blocked on, one click, no modifier key;
  - a session the answer mentions gets a linked toast under the bubble,
    and its link focuses that worker's window (the /s/ path, same as
    conductor/jump.py).

The server answers on 127.0.0.1 and nowhere else, serves one page, and
streams events to it over SSE - no framework, no build step, nothing a
`python3 -m conductor.app_web` cannot do. Like the terminal view this
is a front end, not the Boss: nothing here is wired into the running
conductor.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import fcntl
import base64
import html as html_mod
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import termios
import tempfile
import time
import webbrowser
from pathlib import Path

from .codex_app import Approval, CodexApp, CodexUnavailable, Event
from .observability import application_log
from .tmux_runtime import final_screen_path, session_name, stream_path
from .turn_toast import mentioned, read_sessions

HOST = "127.0.0.1"
# A dropped file arrives base64'd in a JSON body, so the cap on /drop is
# the page's own 16 MB limit with room for the encoding and the name.
DROP_BODY_LIMIT = 24 << 20

# The terminal emulator the page runs (xterm.js and its fit addon),
# vendored so the app needs no network: the page loads them as sibling
# files, whether it came from disk (the native window) or the loopback.
ASSETS = Path(__file__).parent / "assets"
ASSET_FILES = ("xterm.js", "xterm.css", "xterm-addon-fit.js")
ASSET_TYPES = {".js": "application/javascript", ".css": "text/css"}

PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Heygent</title>
<link rel="stylesheet" href="assets/xterm.css">
<script src="assets/xterm.js"></script>
<script src="assets/xterm-addon-fit.js"></script>
<style>
  :root { color-scheme: light dark;
    --bg: #ffffff; --fg: #0d0d0d; --muted: #8f8f8f;
    --faint: #f4f4f4; --line: #e6e6e6; --ink: #0d0d0d;
    --ink-fg: #ffffff; --red: #c4320a; --amber: #b54708;
    --green: #067647; --accent: #444444; --clay: #c15f3c;
    --select: #0a60ff; --select-fg: #ffffff; }
  @media (prefers-color-scheme: dark) { :root {
    --bg: #1e1e1e; --fg: #ececec; --muted: #9a9a9a;
    --faint: #2a2a2a; --line: #383838; --ink: #ececec;
    --ink-fg: #1e1e1e; --red: #f97066; --amber: #f7b155;
    --green: #47cd89; --accent: #bbbbbb; --clay: #d97757;
    --select: #2f6fed; --select-fg: #ffffff; } }
  * { box-sizing: border-box; }
  html { overscroll-behavior: none; }
  body { margin: 0; background: var(--bg); color: var(--fg);
    font: 13px/1.2 ui-monospace, "SF Mono", SFMono-Regular, Menlo,
      monospace;
    -webkit-font-smoothing: antialiased; }
  header { position: fixed; top: 0; left: 0; right: 0; z-index: 2;
    background: var(--bg); border-bottom: 1px solid var(--line); }
  header .row { padding: .65rem 1rem;
    display: flex; align-items: baseline; gap: .55rem; }
  header .name { font-weight: 600; font-size: .9rem; }
  header .light { display: none; }
  header .state { font-size: .8rem; color: var(--muted); }
  main { padding: 3.2rem .5rem 8rem; }
  .msg { margin: 1.2em 0; display: flex; animation: rise .15s ease-out; }
  @keyframes rise { from { opacity: 0; transform: translateY(3px); } }
  .msg > div { white-space: pre-wrap; overflow-wrap: anywhere; }
  .you > div { width: 100%; color: var(--fg); font-weight: 600;
    background: var(--faint); padding: 0 .45rem;
    border-radius: .15rem; }
  .you > div::before { content: "> "; color: var(--muted);
    font-weight: 400; }
  .codex > div { width: 100%; }
  .codex .said { padding-left: 2ch; position: relative; }
  .codex .said::before { content: "\u23fa"; position: absolute; left: 0;
    color: var(--fg); }
  .codex .said.approval::before { content: none; }
  .error .said::before { color: var(--red); }
  .error .said { color: var(--red); }
  .worked { color: var(--muted); cursor: pointer;
    -webkit-user-select: none; user-select: none;
    margin-bottom: 1.2em; }
  .worked::before { content: "\u2733 "; }
  .worked::after { content: " ›"; }
  .worked.open::after { content: " ⌄"; }
  .tools { display: none; margin: 0 0 1.2em; }
  .worked.open + .tools { display: block; }
  .tool { color: var(--fg); }
  .tool .head::before { content: "\u23fa "; color: var(--green); }
  .tool pre.code { color: var(--muted); }
  .tool .head { -webkit-user-select: none; user-select: none; }
  .tool.has .head { cursor: pointer; }
  .tool.has .head::after { content: " \u203a"; opacity: .7; }
  .tool.open .head::after { content: " \u2304"; }
  .tool .exit { color: var(--red); }
  .tool pre.code { display: none; margin: 0 0 1.2em 2ch;
    padding: 0 0 0 2ch; position: relative; background: none;
    border: 0; border-radius: 0; font: inherit; color: var(--muted);
    white-space: pre-wrap; overflow-wrap: anywhere; }
  .tool pre.code::before { content: "\u23bf"; position: absolute;
    left: 0; top: 0; }
  .tool.open pre.code { display: block; }
  .meta { display: none; }
  .h { font-weight: 600; margin: .4rem 0 .1rem; }
  code.chip { font: .82em ui-monospace, SFMono-Regular, monospace;
    background: var(--faint); border: 1px solid var(--line);
    border-radius: .3rem; padding: .04rem .3rem; }
  pre.code { background: var(--faint); border: 1px solid var(--line);
    border-radius: .35rem; padding: .7rem .9rem; margin: .6rem 0;
    overflow-x: auto;
    font: .78rem/1.6 ui-monospace, SFMono-Regular, monospace; }
  #greet { color: var(--muted); }
  #greet::before { content: "\u2733 "; color: var(--clay); }
  .approval { border: 1px solid var(--line); border-radius: .35rem;
    padding: .9rem 1rem .6rem; }
  .approval.waiting { border-color: var(--amber); }
  .approval .tag { display: none; font-size: .72rem; font-weight: 600;
    color: var(--amber); text-transform: uppercase;
    letter-spacing: .05em; margin-bottom: .45rem; }
  .approval.waiting .tag { display: block; }
  .approval .q { margin-bottom: .55rem; }
  .approval .why { font: .78rem/1.7 ui-monospace, SFMono-Regular,
    monospace; color: var(--muted); margin-bottom: .55rem; }
  .buttons { display: flex; flex-direction: column; margin: .2rem 0 0; }
  .buttons button { display: flex; align-items: baseline; gap: .5rem;
    width: 100%; text-align: left; border: 0; background: none;
    color: var(--fg); font: inherit; font-size: .82rem;
    padding: .12rem .3rem; cursor: pointer; }
  .buttons button::before { content: "\u276f"; visibility: hidden; }
  .buttons button:hover:not(:disabled)::before { visibility: visible; }
  .buttons button:hover:not(:disabled) { color: var(--clay); }
  .buttons .k { flex: none; color: inherit; }
  .buttons button:disabled { opacity: .4; cursor: default; }
  .settled { font-size: .82rem; color: var(--muted); margin: .55rem 0; }
  .cut { font-size: .82rem; color: var(--red); padding-left: 1.05rem;
    position: relative; }
  .cut::before { content: "\u23bf"; position: absolute; left: 0; }
  .delegate { margin: -.1rem 0 1.2rem; font-size: .82rem;
    display: flex; align-items: center; gap: .55rem;
    padding: 1.15rem 1.1rem;
    border: 1px solid var(--line); border-radius: .8rem;
    animation: rise .15s ease-out; background: var(--faint);
    box-shadow: 0 1px 2px rgba(0,0,0,.1), 0 2px 6px rgba(0,0,0,.06);
    cursor: pointer;
    transition: border-color .12s, background .12s, box-shadow .12s,
      transform .12s; }
  .delegate:hover { border-color: var(--muted); background: var(--faint);
    box-shadow: 0 2px 8px rgba(0,0,0,.12); transform: translateY(-1px); }
  .delegate .words { flex: none; min-width: 0; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .delegate b { font-weight: 600; }
  .delegate a { flex: none; margin-left: auto; color: var(--muted);
    font-size: .95rem; line-height: 1; text-decoration: none;
    transition: color .12s, transform .12s; }
  .delegate:hover a { color: var(--fg); transform: translateX(2px); }
  .delegate .mark { flex: none; font-size: .55rem; }
  .delegate .mark.spin { font-size: .9rem; line-height: 1;
    color: var(--clay) !important;
    animation: turn 1.8s linear infinite; }
  .delegate .status { color: var(--muted); min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .delegate .status.live { color: var(--clay); }
  .delegate .status.live .sec { color: var(--muted); }
  .shimmer { background: linear-gradient(90deg,
      var(--muted) 35%, var(--fg) 50%, var(--muted) 65%);
    background-size: 200% 100%; -webkit-background-clip: text;
    background-clip: text; color: transparent;
    animation: sweep 2.2s linear infinite; }
  @keyframes sweep { from { background-position: 200% 0; }
    to { background-position: -200% 0; } }
  @keyframes turn { to { transform: rotate(360deg); } }
  .error > div { color: var(--red); }
  form { position: fixed; bottom: 0; left: 0; right: 0; z-index: 2;
    background: linear-gradient(transparent, var(--bg) 40%);
    padding: 1.6rem 1rem
      calc(1.2rem + env(safe-area-inset-bottom)); }
  #status { margin: 0 0 .5rem; display: none;
    align-items: center; gap: .5rem; font-size: .8rem;
    color: var(--muted); padding: 0 .2rem; }
  #status .dot { flex: none; width: .45rem; height: .45rem;
    border-radius: 50%; background: var(--amber); }
  .busy #status .dot { width: auto; height: auto; background: none;
    border-radius: 0; }
  .busy #status .dot::before { content: "\u2733"; display: inline-block;
    color: var(--clay); font-size: .9rem; line-height: 1;
    animation: turn 1.8s linear infinite; }
  #status .doing { font: .75rem/1.5 ui-monospace, SFMono-Regular,
    monospace; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; min-width: 0; }
  .busy #status, .waiting #status { display: flex; }
  .busy #status #verb.shimmer { background-image: linear-gradient(90deg,
      var(--clay) 35%, var(--fg) 50%, var(--clay) 65%); }
  .waiting #status { color: var(--amber); font-weight: 500; }
  .waiting #status .dot { width: .45rem; height: .45rem; border: 0;
    background: var(--amber); animation: none; }
  form .row { display: flex;
    gap: .5rem; align-items: center; background: var(--bg);
    border: 0; border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line); border-radius: 0;
    padding: .3rem .3rem; }
  form .row::before { content: "\u276f"; color: var(--fg); }
  /* while a file is over the window, the box says it will take it */
  .dropping form .row { border-color: var(--accent);
    box-shadow: inset 0 0 0 1px var(--accent); }
  .dropping #hint::after { content: " \u00b7 drop the file to put its "
    "path in the box"; color: var(--accent); }
  input[type=text] { flex: 1; border: 0; background: none;
    color: var(--fg); font: inherit; outline: none; padding: .3rem 0; }
  input::placeholder { color: var(--muted); }
  #send, #stop { display: none; }
  #hint { margin: .25rem 0 0; font-size: .74rem;
    color: var(--muted); padding: 0 .2rem; }
  .typing > div { font-size: .82rem; color: var(--muted); }
  .typing .b { display: inline-block; width: .3rem; height: .3rem;
    border-radius: 50%; background: var(--muted); margin-right: .25rem;
    animation: blink 1.2s infinite; }
  .typing .b:nth-child(2) { animation-delay: .2s; }
  .typing .b:nth-child(3) { animation-delay: .4s; }
  @keyframes blink { 0%, 80%, 100% { opacity: .25; } 40% { opacity: 1; } }
  #chrome { position: fixed; top: .5rem; right: .8rem; z-index: 6;
    display: flex; gap: .2rem; }
  #chrome button { border: 0; background: none; color: var(--muted);
    font-size: .95rem; width: 1.9rem; height: 1.9rem;
    border-radius: .45rem; cursor: pointer; display: grid;
    place-items: center; }
  #chrome button:hover { background: var(--faint); color: var(--fg); }
  /* cmux's sidebar, from its source: 240px wide, the system font (the
     terminal is the only monospace surface), 12.5px semibold titles over
     10.5px subtitles, 6px-radius rows, blue selection. */
  .panel { position: fixed; top: 0; bottom: 0; width: 240px; z-index: 5;
    background: var(--faint); padding: .8rem .5rem; overflow-y: auto;
    transition: transform .18s ease;
    font-family: -apple-system, system-ui, sans-serif; }
  #side { left: 0; border-right: 1px solid var(--line);
    transform: translateX(-105%); }
  #fleet { right: 0; border-left: 1px solid var(--line); width: 19.5rem;
    transform: translateX(105%); }
  .side-open #side { transform: none; }
  .fleet-open #fleet { transform: none;
    box-shadow: 0 6px 30px #00000022; }
  /* cmux keeps its workspace list as a permanent panel: the page
     fills the rest of the width like a terminal pane. */
  /* Not #status: it lives inside the form, whose shift carries it -
     listing it here moved the "Working..." line 480px from the left
     edge, twice the sidebar (measured 2026-09-01). */
  .side-open main, .side-open header .row, .side-open form
    { margin-left: 240px; }
  .panel .head-row { display: flex; align-items: center;
    justify-content: space-between; font-size: .72rem; font-weight: 600;
    color: var(--muted); text-transform: uppercase;
    letter-spacing: .05em; margin: .3rem .4rem .7rem; }
  #side .sect { font-size: 11px; font-weight: 600; color: var(--muted);
    margin: 1.1rem .6rem .3rem; }
  #newThread { display: flex; align-items: center; gap: .55rem;
    width: 100%; border: 0; background: none; color: var(--fg);
    font: inherit; font-size: 12.5px; text-align: left;
    padding: .4rem .6rem; border-radius: 6px; cursor: pointer; }
  #newThread:hover { background: var(--line); }
  #newThread .i { color: var(--muted); font-size: .95rem; }
  .thread, .sess { display: flex; align-items: center; gap: .55rem;
    padding: .4rem .6rem; border-radius: 6px;
    font-size: 12.5px; cursor: pointer;
    -webkit-user-select: none; user-select: none; }
  .thread span, .sess span.t { flex: 1; min-width: 0; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .thread:hover, .sess:hover { background: var(--line); }
  .thread .tag { flex: none; font-size: 9.5px; font-weight: 600;
    letter-spacing: .04em; text-transform: uppercase; color: var(--muted);
    background: var(--line); border-radius: 4px; padding: .5px .3rem; }
  /* One row is selected: the Boss's while its chat is on the page, a
     worker's while its terminal is (asked 2026-09-10: the Boss stayed
     blue with a worker open). */
  .thread.current .tag { background: #ffffff33; color: inherit; }
  .thread.current, .sess.current
    { background: var(--select); color: var(--select-fg); }
  .thread.current.off { background: none; color: inherit; }
  .thread.current.off:hover { background: var(--line); }
  .thread.current.off .tag { background: var(--line); color: var(--muted); }
  .thread.current:not(.off) .row2, .sess.current .row2,
  .thread.current:not(.off) .row2.wait,
  .thread.current:not(.off) .row2.fail,
  .sess.current .row2.fail, .sess.current .mark
    { color: var(--select-fg) !important; opacity: .75; }
  .sess, .thread.current { display: block; }
  .panel .row1 { display: flex; align-items: center; gap: .55rem;
    font-size: 12.5px; font-weight: 600; }
  /* Flush with the title: nothing in the sidebar is indented (asked
     2026-09-10). */
  .panel .row2 { font-size: 10.5px; color: var(--muted);
    margin: .05rem 0 0; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .panel .row2.wait { color: var(--green); }
  .panel .row2.fail { color: var(--red); }
  .sess .mark { flex: none; font-size: .5rem; }
  /* The remove button: only under the pointer, so the list still reads
     as a glance. */
  .sess .x { flex: none; display: none; place-items: center;
    width: 1.1rem; height: 1.1rem; padding: 0; border: 0;
    border-radius: 4px; background: none; color: var(--muted);
    font: inherit; font-size: 14px; line-height: 1; cursor: pointer; }
  .sess:hover .x { display: grid; }
  .sess .x:hover { background: var(--faint); color: var(--fg); }
  .sess.current .x { color: var(--select-fg); }
  .sess.current .x:hover { background: #ffffff33; }
  .sess .ask { margin: .4rem 0 .1rem; font-size: 10.5px; line-height: 1.35;
    white-space: normal; cursor: default; }
  .sess .ask .btns { display: flex; gap: .35rem; margin-top: .4rem; }
  .sess .ask button { border: 1px solid var(--line); background: var(--bg);
    color: var(--fg); font: inherit; font-size: 10.5px;
    padding: .15rem .55rem; border-radius: 5px; cursor: pointer; }
  .sess .mark.spin { font-size: .85rem; line-height: 1;
    color: var(--clay) !important;
    animation: turn 1.8s linear infinite; }
  .panel .empty { color: var(--muted); font-size: .82rem;
    padding: .2rem .4rem; }
  #fleet .delegate { margin: 0 0 .6rem; }
  #fleet .delegate .words { white-space: normal; }
  #fleet .delegate .status { white-space: normal; }
  .app header { display: none; }
  .app main { padding-top: 2.8rem; }
  .app #chrome { top: 2rem; }
  .app .panel { padding-top: 2.6rem; }
  .app { -webkit-user-select: none; user-select: none; cursor: default; }
  .app .msg > div, .app pre, .app code, .app input
    { -webkit-user-select: text; user-select: text; cursor: auto; }
  .app ::-webkit-scrollbar { display: none; }
  /* ...except the terminal, which is the one place a reader needs to
     know there is more above. */
  .app #termScreen::-webkit-scrollbar { display: block; width: 9px; }
  .app #termScreen::-webkit-scrollbar-thumb
    { background: var(--line); border-radius: 5px; }
  .app #termScreen::-webkit-scrollbar-track { background: transparent; }
  /* One scrollbar while the emulator is live: the outer container
     stops scrolling (its content is just the emulator now) and the
     emulator's own viewport bar is styled like the app's. Measured
     2026-09-01: two bars stacked on the right edge. */
  .term-live #termScreen { overflow: hidden; }
  .term-live #termPast { display: none; }
  .app .xterm-viewport::-webkit-scrollbar { width: 9px; }
  .app .xterm-viewport::-webkit-scrollbar-thumb
    { background: var(--line); border-radius: 5px; }
  .app .xterm-viewport::-webkit-scrollbar-track { background: transparent; }
  /* The embedded worker terminal: the session itself, drawn in the
     pane where the chat was. */
  #term { display: none; position: fixed; top: 0; right: 0; bottom: 0;
    left: 0; z-index: 4; background: var(--bg);
    flex-direction: column; }
  .side-open #term { left: 240px; }
  .term-open #term { display: flex; }
  .term-open main, .term-open form, .term-open #status,
  .term-open #chrome, .term-open #fleet { visibility: hidden; }
  #termbar { display: flex; align-items: center; gap: .6rem;
    padding: .45rem .8rem; border-bottom: 1px solid var(--line);
    font-size: 12.5px; }
  .app #termbar { padding-top: 2.4rem; }
  #termTitle { font-weight: 600; min-width: 0; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  #termbar .hint { margin-left: auto; color: var(--muted);
    font-size: 10.5px; white-space: nowrap; flex-shrink: 0; }
  #termPast { display: block; }
  #termPast .you { color: var(--accent); }
  #termPast .said { color: var(--fg); }
  #termPast .tool { color: var(--muted); }
  #termPast .end { color: var(--line); }
  #termPast .rule { display: block; color: var(--muted); opacity: .8;
    margin: .5rem 0 .35rem; }
  #termScreen { flex: 1; margin: 0; padding: .6rem .8rem; overflow: auto;
    font-size: 12px; line-height: 1.25;
    outline: none; -webkit-user-select: text; user-select: text;
    cursor: text; }
  #termPast, #termLive { margin: 0; font: inherit; white-space: pre; }
  #termXterm { display: none; }
  #termXterm .xterm { padding: 0; }
</style>
<header><div class="row">
  <span class="light"></span>
  <span class="name">Heygent</span>
  <span class="state" id="state"></span>
</div></header>
<div id="chrome">
  <button id="sideBtn" title="Threads (⇧⌘S)">☰</button>
  <button id="fleetBtn" title="Worker sessions">✳</button>
</div>
<nav id="side" class="panel">
  <div class="sect">Chats</div>
  <div id="threadList"></div>
  <div id="sessionList"></div>
</nav>
<aside id="fleet" class="panel">
  <div class="head-row">Worker sessions</div>
  <div id="fleetList"></div>
</aside>
<main id="log"><div id="greet">What should we work on?</div></main>
<div id="term">
  <div id="termbar">
    <span id="termTitle"></span>
  </div>
  <div id="termScreen" tabindex="0"><pre id="termPast"></pre><div id="termXterm"></div><pre id="termLive"></pre></div>
</div>
<form id="form">
  <div id="status"><span class="dot"></span>
    <span id="verb"></span><span class="doing" id="doing"></span></div>
  <div class="row">
    <input id="text" type="text" autocomplete="off"
           placeholder="Message Heygent" autofocus>
    <button id="send" type="submit" title="Send">↑</button>
    <button id="stop" type="button" title="Stop">■</button>
  </div>
  <div id="hint">? for shortcuts · esc to interrupt</div>
</form>
<script>
// Native bridge: inside the WKWebView the conductor spawned, the page
// and its backend talk over the script-message bridge and the spawned
// process's stdio - no HTTP, no port (2026-09-01, "stay native").
const NATIVE = !!(window.webkit && webkit.messageHandlers
                  && webkit.messageHandlers.bridge);
let bridgeSeq = 0;
const bridgeWaiting = new Map();
function bridgeSend(envelope) {
  webkit.messageHandlers.bridge.postMessage(JSON.stringify(envelope));
}
function bridgeCall(path, body) {
  return new Promise((resolve) => {
    const id = ++bridgeSeq;
    bridgeWaiting.set(id, resolve);
    bridgeSend({ id: id, path: path, body: body || {} });
  });
}
window.__reply = (id, data) => {
  const waiter = bridgeWaiting.get(id);
  bridgeWaiting.delete(id);
  if (waiter) waiter(data || {});
};
window.__deliver = (message) => on(message);
if (NATIVE) window.onerror = (m, src, line) => {
  // The view has no console anyone reads; a page error crosses the
  // bridge and lands in the conductor's log.
  try { bridgeSend({ path: "/jserror",
                     body: { m: String(m), line: line || 0 } }); }
  catch (e) {}
};
if (NATIVE || new URLSearchParams(location.search).has("app"))
  document.body.classList.add("app");   // a native title bar is above us
const inApp = document.body.classList.contains("app");
if (inApp) {
  document.addEventListener("contextmenu", (e) => {
    if (e.target.closest("input, a, pre, code")) return;
    if (getSelection().toString()) return;
    e.preventDefault();            // the page is an app, not a document
  });
}
const log = document.getElementById("log");
const form = document.getElementById("form");
const text = document.getElementById("text");
const glyphColour = { attention: "var(--amber)", failed: "var(--red)",
                      done: "var(--green)", working: "var(--accent)",
                      open: "var(--muted)" };
let streaming = null;   // the bubble the current turn's deltas fill
let typing = null;      // the three-dot bubble while codex is silent
let busySince = null;   // when the running turn started, for the status
let awaiting = 0;       // approvals waiting on the user right now
let doingNow = "";      // the command codex is running at this moment

function status() {
  document.body.classList.toggle("waiting", awaiting > 0);
  const verb = document.getElementById("verb");
  const doing = document.getElementById("doing");
  if (awaiting > 0) {
    verb.textContent = "Waiting for your approval";
    doing.textContent = "";
  } else if (busySince !== null) {
    const seconds = Math.max(0, Math.round((Date.now() - busySince) / 1000));
    const verbs = ["Working", "Thinking", "Deliberating", "Puzzling"];
    verb.textContent = verbs[Math.floor(seconds / 8) % verbs.length]
      + "\u2026 (" + seconds + "s \u00b7 esc to interrupt)";
    verb.className = "shimmer";
    doing.textContent = doingNow && "$ " + doingNow;
  } else {
    verb.className = "";
  }
  if (awaiting > 0) verb.className = "";
}
setInterval(status, 1000);

function thinking(is) {
  if (is && !typing && !streaming) {
    typing = bubble("codex", "typing");
    for (let i = 0; i < 3; i++) {
      const dot = document.createElement("span");
      dot.className = "b";
      typing.appendChild(dot);
    }
  } else if (!is && typing) {
    typing.parentElement.remove();
    typing = null;
  }
}

// Follow the conversation only while the reader is already at the
// bottom. A page that yanks to the end on every streamed update cannot
// be scrolled up while anything runs - measured on 2026-09-01. The
// terminal view has the same behaviour (the emulator's own viewport).
let pinned = true;
addEventListener("scroll", () => {
  pinned = window.innerHeight + window.scrollY
    >= document.body.scrollHeight - 60;
}, { passive: true });

function follow() {
  // To the document's bottom, not the element's: scrollIntoView put new
  // turns flush against the window edge, underneath the fixed composer
  // (measured 2026-09-01, "all crowded at the bottom of the screen").
  // The page reserves breathing room below the log; use it.
  if (pinned) window.scrollTo(0, document.body.scrollHeight);
}

function bubble(side, extra) {
  const row = document.createElement("div");
  row.className = "msg " + side + (extra ? " " + extra : "");
  const box = document.createElement("div");
  if (side === "codex" && extra !== "typing") box.className = "said";
  row.appendChild(box);
  log.appendChild(row);
  if (side === "you") pinned = true;   // sending returns to the bottom
  follow();
  return box;
}

function post(path, body) {
  if (NATIVE) return bridgeCall(path, body);
  return fetch(path, { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}) })
    .then((r) => r.json()).catch(() => ({}));
}

function api(path) {
  return NATIVE ? bridgeCall(path)
    : fetch(path).then((r) => r.json());
}

function render(box, said) {
  String(said || "").split("```").forEach((part, i) => {
    if (i % 2) {                        // inside a fence
      const pre = document.createElement("pre");
      pre.className = "code";
      pre.textContent = part.replace(/^[^\\n]*\\n/, "").replace(/\\n$/, "");
      box.appendChild(pre);
    } else {
      lines(box, i ? part.replace(/^\\n/, "") : part);
    }
  });
}

function lines(box, part) {
  part.split("\\n").forEach((line, i, all) => {
    const head = line.match(/^#{1,4} (.*)/);
    if (head) {
      const h = document.createElement("div");
      h.className = "h";
      spans(h, head[1]);
      box.appendChild(h);
    } else {
      spans(box, line.replace(/^(\\s*)[-*] /, "$1\u2022 "));
      if (i < all.length - 1)
        box.appendChild(document.createTextNode("\\n"));
    }
  });
}

function spans(box, line) {
  for (const piece of line.split(/(`[^`]+`|\\*\\*[^*]+\\*\\*)/)) {
    if (!piece) continue;
    let node;
    if (piece.startsWith("`") && piece.endsWith("`")) {
      node = document.createElement("code");
      node.className = "chip";
      node.textContent = piece.slice(1, -1);
    } else if (piece.startsWith("**") && piece.endsWith("**")) {
      node = document.createElement("b");
      node.textContent = piece.slice(2, -2);
    } else {
      node = document.createTextNode(piece);
    }
    box.appendChild(node);
  }
}

function on(msg) {
  if (msg.kind === "term_data") { termData(msg); return; }
  if (msg.kind === "term_exit") { termExit(msg); return; }
  if (msg.kind !== "state") {
    const greet = document.getElementById("greet");
    if (greet) greet.remove();
  }
  if (msg.kind === "you") {
    bubble("you").textContent = msg.text;
    streaming = null;
  } else if (msg.kind === "delta") {
    thinking(false);
    if (!streaming) streaming = bubble("codex");
    streaming.textContent += msg.text;
    follow();
  } else if (msg.kind === "turn") {
    thinking(false);
    const box = streaming || bubble("codex");
    streaming = null;
    box.classList.remove("said");
    const prior = box.textContent;   // an interrupted turn keeps its words
    box.textContent = "";
    const worked = document.createElement("div");   // "✳ Worked for Ns ›"
    worked.className = "worked";
    worked.textContent = "Worked for " + Math.round(msg.seconds) + "s";
    worked.title = msg.model || "";
    worked.onclick = () => worked.classList.toggle("open");
    box.appendChild(worked);
    const tools = document.createElement("div");
    tools.className = "tools";
    for (const tool of msg.tools.slice(0, 40)) tools.appendChild(toolRow(tool));
    box.appendChild(tools);
    const said = document.createElement("div");
    said.className = "said";
    render(said, msg.answer || prior || "(nothing said)");
    box.appendChild(said);
    follow();
  } else if (msg.kind === "tool") {
    if (!msg.type || msg.type === "command") doingNow = msg.text;
    status();
  } else if (msg.kind === "approval") {
    thinking(false);
    awaiting += 1;
    approvalCard(msg);
    status();
  } else if (msg.kind === "decision") {
    awaiting = Math.max(0, awaiting - 1);
    settle(msg.item_id, msg.decision);
    status();
  } else if (msg.kind === "toast") {
    toast(msg);
  } else if (msg.kind === "focus_session") {
    openTerm(msg.task_id, msg.title || msg.task_id);
  } else if (msg.kind === "session") {
    sessionUpdate(msg);
  } else if (msg.kind === "error") {
    thinking(false);
    const box = streaming || bubble("codex");   // no second copy of it
    streaming = null;
    if (box.textContent) {          // an interrupted stream keeps its words
      const cut = document.createElement("div");
      cut.className = "cut";
      cut.textContent = msg.text && msg.text !== box.textContent.trim()
        ? msg.text : "Interrupted by user";
      box.appendChild(cut);
    } else {
      box.parentElement.classList.add("error");
      box.textContent = msg.text;
    }
  } else if (msg.kind === "state") {
    document.body.classList.toggle("busy", msg.busy);
    document.getElementById("state").textContent =
      msg.busy ? "thinking" : "";
    busySince = msg.busy ? Date.now() : null;
    doingNow = "";
    thinking(msg.busy);
    if (!msg.busy && streaming) {   // a turn that never settled: cut short
      const cut = document.createElement("div");
      cut.className = "cut";
      cut.textContent = "Interrupted by user";
      streaming.appendChild(cut);
      streaming = null;
    }
    status();
  } else if (msg.kind === "open_term") {
    // A notification's link: this window, this session - not cmux.
    openTerm(msg.task_id, msg.title || msg.task_id);
  } else if (msg.kind === "reset") {
    // Another thread takes the page: redraw from its history.
    if (document.body.classList.contains("side-open")) loadThreads();
    log.replaceChildren();
    streaming = typing = null;
    awaiting = 0; busySince = null; doingNow = "";
    document.body.classList.remove("busy", "waiting");
    if (!msg.history.length) {
      const greet = document.createElement("div");
      greet.id = "greet";
      greet.textContent = "What should we work on?";
      log.appendChild(greet);
    }
    for (const m of msg.history) on(m);
    status();
  }
}

// The names Claude Code gives its tool lines: "Bash(cmd)", "Update(file)".
const toolNames = { command: "Bash", edit: "Update", thought: "Thinking",
                    search: "Search", plan: "Update Todos" };

function toolLine(row) {
  if (row.type === "plan")
    return "Update Todos (" + row.text.replace(/^plan \u00b7 /, "") + ")";
  if (row.type === "tool") return row.text;
  let text = row.text;   // codex wraps commands in a shell; the TUI doesn't
  const shell = text.match(/^\\S*\\/(?:zsh|bash|sh) -lc '([^]*)'$/);
  if (shell) text = shell[1];
  return (toolNames[row.type] || "Tool") + "(" + text + ")";
}

function toolRow(tool) {
  // One line of the turn's activity; a row with detail opens into it.
  const row = typeof tool === "string"
    ? { type: "command", text: tool } : tool;
  const t = document.createElement("div");
  t.className = "tool";
  const head = document.createElement("div");
  head.className = "head";
  const line = toolLine(row);
  const name = line.match(/^([A-Za-z][A-Za-z ]*)\\(/);
  if (name) {
    const b = document.createElement("b");
    b.textContent = name[1];
    head.appendChild(b);
    head.appendChild(document.createTextNode(line.slice(name[1].length)));
  } else {
    head.textContent = line;
  }
  if (row.exit) {
    const exit = document.createElement("span");
    exit.className = "exit";
    exit.textContent = " \u00b7 exit " + row.exit;
    head.appendChild(exit);
  }
  t.appendChild(head);
  if (row.detail) {
    t.classList.add("has");
    const pre = document.createElement("pre");
    pre.className = "code";
    pre.textContent = row.detail;
    t.appendChild(pre);
    head.onclick = (event) => {
      event.stopPropagation();      // not the "Worked for Ns" toggle
      t.classList.toggle("open");
    };
  }
  return t;
}

function approvalCard(msg) {
  streaming = null;
  const box = bubble("codex");
  box.parentElement.querySelector("div").classList.add("approval",
                                                       "waiting");
  box.dataset.itemId = msg.item_id;
  const tag = document.createElement("div");
  tag.className = "tag";
  tag.textContent = "Approval required";
  box.appendChild(tag);
  const q = document.createElement("div");
  q.className = "q";
  q.textContent = msg.question + " Do you want to proceed?";
  box.appendChild(q);
  for (const said of [msg.cwd && "in " + msg.cwd, msg.detail]) {
    if (!said) continue;
    const why = document.createElement("div");
    why.className = "why";
    why.textContent = said;
    box.appendChild(why);
  }
  const row = document.createElement("div");
  row.className = "buttons";
  for (const [decision, key, label, cls] of [
      ["accept", "1.", "Yes", "accept"],
      ["acceptForSession", "2.",
       "Yes, and don't ask again this session", "session"],
      ["decline", "3.", "No, and tell Heygent what to do differently",
       "decline"]]) {
    const b = document.createElement("button");
    b.className = cls;
    const k = document.createElement("span");
    k.className = "k";
    k.textContent = key;
    b.appendChild(k);
    b.appendChild(document.createTextNode(label));
    b.onclick = () => post("/answer",
                           { item_id: msg.item_id, decision: decision });
    row.appendChild(b);
  }
  box.appendChild(row);
  follow();
}

function settle(itemId, decision) {
  for (const box of log.querySelectorAll("[data-item-id]")) {
    if (box.dataset.itemId !== itemId) continue;
    box.classList.remove("waiting");
    const row = box.querySelector(".buttons");
    if (row) row.remove();
    const done = document.createElement("div");
    done.className = "settled";
    done.textContent = decision === "decline" ? "Declined"
      : "Approved" + (decision === "acceptForSession"
                      ? " for this session" : "");
    box.appendChild(done);
  }
}

function toast(msg) {
  for (const row of msg.rows) {
    // One card per session, however many announcements arrive.
    const drawn = [...log.querySelectorAll(".delegate")]
      .some((line) => line.dataset.taskId === row.task_id);
    if (drawn) { sessionUpdate({ ...row, kind: "session" }); continue; }
    const line = delegateCard(row);
    log.appendChild(line);
    follow();
  }
}

function delegateCard(row) {
  // The way Devin draws a spawned session: a captioned card with the
  // worker's title, a "View session" button, and a live status line.
  const line = document.createElement("div");
  line.className = "delegate";
  line.dataset.taskId = row.task_id || "";
  const mark = document.createElement("span");
  mark.className = "mark";
  line.appendChild(mark);
  const words = document.createElement("span");
  words.className = "words";
  words.appendChild(document.createTextNode("Delegated to "));
  const title = document.createElement("b");
  title.textContent = row.title || row.task_id;
  words.appendChild(title);
  line.appendChild(words);
  const status = document.createElement("span");
  status.className = "status";
  line.appendChild(status);
  const open = document.createElement("a");
  open.href = "#";
  open.textContent = "\u203a";
  open.title = "Open the session";
  open.onclick = (event) => {
    event.preventDefault();
    openTerm(row.task_id, row.title || row.task_id);
  };
  line.appendChild(open);
  line.onclick = (event) => {       // the whole card is the link
    if (event.target === open) return;
    openTerm(row.task_id, row.title || row.task_id);
  };
  paint(line, row);
  return line;
}

function paint(line, row) {
  const settled = row.glyph === "done" || row.glyph === "failed"
    || row.glyph === "open";
  const mark = line.querySelector(".mark");
  const status = line.querySelector(".status");
  mark.style.color = glyphColour[row.glyph] || glyphColour.working;
  if (settled) {
    mark.className = "mark";
    mark.textContent = "●";
    status.className = "status";
    status.textContent = row.status || row.glyph;
  } else {
    mark.className = "mark spin";
    mark.textContent = "\u2733";
    status.className = "status live";
    line.dataset.status = row.status || "";
    if (!line.dataset.since) line.dataset.since = sinceFor(line.dataset.taskId);
    delegateVerb(line);
  }
}

const firstSeen = {};   // a rebuilt card keeps its (Ns) counter
function sinceFor(taskId) {
  if (!firstSeen[taskId]) firstSeen[taskId] = Date.now();
  return firstSeen[taskId];
}

const workerVerbs = ["Deliberating", "Chipping away", "Toiling",
                     "Beavering away"];

function delegateVerb(line) {
  // The live card cycles between what the worker says it is doing and a
  // spinner verb, the way the agent cards do.
  const status = line.querySelector(".status");
  if (!status.classList.contains("live")) return;
  const beat = Math.floor(Date.now() / 8000);
  const said = line.dataset.status;
  const words = (beat % 2 === 0 && said)
    ? said[0].toUpperCase() + said.slice(1)
    : workerVerbs[Math.floor(beat / 2) % workerVerbs.length];
  const seconds = Math.max(0,
    Math.round((Date.now() - Number(line.dataset.since)) / 1000));
  status.textContent = words + "\u2026 ";
  const sec = document.createElement("span");
  sec.className = "sec";
  sec.textContent = "(" + seconds + "s)";
  status.appendChild(sec);
}

setInterval(() => {
  for (const line of document.querySelectorAll(".delegate"))
    delegateVerb(line);
}, 1000);

function sessionUpdate(msg) {
  for (const line of document.querySelectorAll(".delegate")) {
    if (line.dataset.taskId === msg.task_id) paint(line, msg);
  }
}

// -- the panels: threads on the left, every worker on the right --------
function bossTag() {
  // Which chat has the workers behind it. New chat makes an ordinary
  // coding chat now, so the two kinds sit in one list and only this
  // says which is which.
  const tag = document.createElement("span");
  tag.className = "tag";
  tag.textContent = "boss";
  return tag;
}

async function loadThreads() {
  // One flat Chats list, the way Devin's sidebar shows subagents: the
  // Boss chat sits flush at the top, its worker sessions nest under
  // it, and the other chats follow.
  const list = document.getElementById("threadList");
  const kids = document.getElementById("sessionList");
  const data = await api("/threads");
  kids.remove();                 // clearing the list must not destroy it
  list.replaceChildren();
  const threads = data.threads.slice().reverse();
  const current = threads.find((t) => t.current);
  const row = document.createElement("div");
  row.className = "thread current" + (termId ? " off" : "");
  row.id = "bossRow";
  const top = document.createElement("div");
  top.className = "row1";
  const name = document.createElement("span");
  name.className = "t";
  name.textContent = (current && (current.title || current.id)) || "New chat";
  top.appendChild(name);
  if (!current || current.kind !== "chat") top.appendChild(bossTag());
  row.appendChild(top);
  const state = document.createElement("div");
  state.className = "row2";
  state.id = "bossState";
  row.appendChild(state);
  row.title = name.textContent;
  row.onclick = closeTerm;   // the Boss row is the way back to the chat
  list.appendChild(row);
  list.appendChild(kids);
  for (const t of threads) {
    if (t.current) continue;
    const other = document.createElement("div");
    other.className = "thread";
    other.title = t.title || t.id;
    const label = document.createElement("span");
    label.textContent = t.title || t.id;
    other.appendChild(label);
    if (t.kind !== "chat") other.appendChild(bossTag());
    other.onclick = () => post("/thread", { id: t.id });
    list.appendChild(other);
  }
  loadSessions();
}

async function loadSessions() {
  // The worker sessions are real Claude Code sessions; each row shows a
  // live mark, its title, and a status line, and a click shows its
  // terminal here in the window.
  const list = document.getElementById("sessionList");
  const data = await api("/sessions");
  list.replaceChildren();
  let live = 0;
  for (const row of data.rows) {
    const line = document.createElement("div");
    line.className = "sess" + (row.task_id === termId ? " current" : "");
    line.dataset.task = row.task_id;
    const settled = row.glyph === "done" || row.glyph === "failed"
      || row.glyph === "open";
    if (!settled) live += 1;
    const top = document.createElement("div");
    top.className = "row1";
    const mark = document.createElement("span");
    mark.className = settled ? "mark" : "mark spin";
    mark.textContent = settled ? "\u25cf" : "\u2733";
    mark.style.color = glyphColour[row.glyph] || glyphColour.working;
    top.appendChild(mark);
    const name = document.createElement("span");
    name.className = "t";
    name.textContent = row.title || row.task_id;
    top.appendChild(name);
    line.appendChild(top);
    const state = document.createElement("div");
    state.className = "row2" + (row.glyph === "failed" ? " fail" : "");
    // The way Devin's sidebar reads at a glance: a working session says
    // Working, a settled one says how it ended.
    const head = row.glyph === "open" ? "Still open" : settled
      ? (row.glyph === "failed" ? "Failed" : "Finished") : "Working";
    state.textContent = head + (row.status && row.status !== head
      ? " \u00b7 " + row.status : "");
    line.appendChild(state);
    line.title = (row.title || row.task_id)
      + (row.status ? " \u00b7 " + row.status : "");
    const x = document.createElement("button");
    x.className = "x";
    x.title = "Remove from the sidebar (the worker is not stopped)";
    x.textContent = "×";
    x.onclick = (event) => {
      event.stopPropagation();
      removeSession(row.task_id, false);
    };
    top.appendChild(x);
    if (asking && asking.task_id === row.task_id)
      line.appendChild(removeQuestion(asking));
    line.onclick = () => openTerm(row.task_id, row.title || row.task_id);
    list.appendChild(line);
  }
  // A worker whose card was removed is still a worker the Boss waits for.
  live += data.hidden_running || 0;
  const boss = document.getElementById("bossState");
  if (boss) {
    boss.className = "row2" + (live ? " wait" : "");
    boss.textContent = live ? "Waiting for " + live + " worker"
      + (live > 1 ? "s" : "") : "Idle";
  }
}

// Removing a card takes it off the sidebar and nothing else: the worker,
// its worktree and its branch stay. A worker that may still be running
// is asked about first, in its row - the answer says it keeps running -
// and only a yes removes it. The question lives here, not in the row,
// because the list is redrawn every two seconds.
let asking = null;   // { task_id, message }: a removal waiting on a yes
async function removeSession(taskId, confirmed) {
  const reply = await post("/remove",
                           { task_id: taskId, confirmed: confirmed });
  if (reply && reply.running && !reply.ok) {
    asking = { task_id: taskId, message: reply.message || "" };
  } else {
    asking = null;
    if (reply && reply.ok && termId === taskId) closeTerm();
  }
  loadSessions();
}

function removeQuestion(ask) {
  const box = document.createElement("div");
  box.className = "ask";
  box.onclick = (event) => event.stopPropagation();
  const words = document.createElement("div");
  words.textContent = ask.message;
  box.appendChild(words);
  const buttons = document.createElement("div");
  buttons.className = "btns";
  const yes = document.createElement("button");
  yes.textContent = "Remove card";
  yes.onclick = () => removeSession(ask.task_id, true);
  const no = document.createElement("button");
  no.textContent = "Keep";
  no.onclick = () => { asking = null; loadSessions(); };
  buttons.appendChild(yes);
  buttons.appendChild(no);
  box.appendChild(buttons);
  return box;
}

let fleetTimer = null;
async function loadFleet() {
  const list = document.getElementById("fleetList");
  const data = await api("/sessions");
  list.replaceChildren();
  for (const row of data.rows) list.appendChild(delegateCard(row));
  if (!data.rows.length) {
    const none = document.createElement("div");
    none.className = "empty";
    none.textContent = "No worker sessions";
    list.appendChild(none);
  }
}

let sideTimer = null;
function toggleSide() {
  clearInterval(sideTimer);
  sideTimer = null;
  if (document.body.classList.toggle("side-open")) {
    loadThreads();
    loadSessions();
    sideTimer = setInterval(loadSessions, 2000);
  }
}
function toggleFleet() {
  clearInterval(fleetTimer);
  fleetTimer = null;
  if (document.body.classList.toggle("fleet-open")) {
    loadFleet();
    fleetTimer = setInterval(loadFleet, 2000);
  }
}
document.getElementById("sideBtn").onclick = toggleSide;
document.getElementById("fleetBtn").onclick = toggleFleet;
// The New-chat button is off the sidebar for now (asked 2026-09-01);
// a fresh chat still comes from the voice ("new chat") or a restart.
toggleSide();   // like cmux, the session list is a permanent panel

form.onsubmit = (event) => {
  event.preventDefault();
  const said = text.value.trim();
  if (!said) return;
  text.value = "";
  post("/say", { text: said });
  pinned = true;                 // sending is a return to the bottom
  window.scrollTo(0, document.body.scrollHeight);
};
document.getElementById("stop").onclick = () => post("/interrupt");
document.addEventListener("keydown", (event) => {
  if (document.body.classList.contains("term-open")) return;
  if (event.metaKey && event.shiftKey &&
      event.key.toLowerCase() === "s") {
    event.preventDefault();
    toggleSide();
    return;
  }
  if (event.key === "Escape") {
    if (document.body.classList.contains("fleet-open")) {
      toggleFleet();
      return;
    }
    if (document.body.classList.contains("busy")) post("/interrupt");
  }
});

// -- a dropped file: its path lands in the box -------------------------
// Claude Code takes a dragged file as a path in the prompt, and so does
// this box. Left to itself WebKit navigates the window to the file and
// the conversation disappears behind a picture, so every drop is taken
// here. A page is never told a dropped file's real path - WebKit keeps
// it from the DataTransfer - so the app's window intercepts the drag
// first and calls insertPaths with the true ones. In a browser there is
// no native side: the bytes are posted to /drop, saved under the
// conductor's home, and the path they were saved to goes in instead.
const DROP_LIMIT = 16 * 1024 * 1024;   // per file; /drop's own cap
                                       // is this, base64'd

function quotePath(path) {
  if (path.indexOf(" ") < 0 && path.indexOf('"') < 0) return path;
  return '"' + path.split('"').join('\\\\"') + '"';
}

function insertPaths(paths) {
  const pieces = (paths || []).filter(Boolean).map(quotePath);
  if (!pieces.length) return;
  if (document.body.classList.contains("term-open")) closeTerm();
  const at = text.selectionStart === null ? text.value.length
                                          : text.selectionStart;
  const to = text.selectionEnd === null ? at : text.selectionEnd;
  const before = text.value.slice(0, at), after = text.value.slice(to);
  const lead = before && !before.endsWith(" ") ? " " : "";
  const head = before + lead + pieces.join(" ") + " ";
  text.value = head + after;
  text.focus();
  text.setSelectionRange(head.length, head.length);
}
window.insertPaths = insertPaths;      // the app's window calls this

// The app's window takes the drag before the page sees it, so the box
// is told when to light up rather than noticing for itself.
window.showDrop = function (on) {
  if (on) document.body.classList.add("dropping"); else notDropping();
};

function droppedFiles(event) {
  const data = event.dataTransfer;
  return !!data && Array.from(data.types || []).indexOf("Files") >= 0;
}

async function saveDropped(file) {
  if (file.size > DROP_LIMIT) {
    render(bubble("codex"), file.name + " is too big to hand over ("
      + Math.round(file.size / 1048576) + " MB).");
    return "";
  }
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";                     // one chunk at a time: apply() on
  for (let i = 0; i < bytes.length; i += 0x8000)  // a whole big file
    binary += String.fromCharCode.apply(          // overflows the stack
      null, bytes.subarray(i, i + 0x8000));
  // post() hands back the parsed reply, the same object over HTTP and
  // over the native bridge - not a Response, so there is nothing to
  // await twice.
  const reply = await post("/drop", { name: file.name || "dropped",
                                      data: btoa(binary) });
  if (!reply || !reply.ok) {
    render(bubble("codex"), "That file could not be kept: "
      + String((reply && reply.error) || "it did not arrive").slice(0, 200));
    return "";
  }
  return reply.path || "";
}

// A drag that carries no file may still carry a path as text - a
// file:// URL from a Finder-aware app.
function pathsInText(data) {
  const said = data.getData("text/uri-list") || data.getData("text/plain");
  return String(said || "").split("\\n").map((line) => line.trim())
    .filter((line) => line.indexOf("file://") === 0)
    .map((line) => { try { return decodeURI(line.slice(7)); }
                     catch (e) { return ""; } });
}

async function takeDrop(data) {
  if (!data) return;
  const files = Array.from(data.files || []);
  if (!files.length) { insertPaths(pathsInText(data)); return; }
  const paths = [];
  for (const file of files) {
    const saved = await saveDropped(file);
    if (saved) paths.push(saved);
  }
  insertPaths(paths);
}

let overUs = 0;                        // dragenter/leave fire per element
function notDropping() {
  overUs = 0;
  document.body.classList.remove("dropping");
}
addEventListener("dragenter", (event) => {
  if (!droppedFiles(event)) return;
  event.preventDefault();
  overUs += 1;
  document.body.classList.add("dropping");
});
addEventListener("dragleave", (event) => {
  if (!droppedFiles(event)) return;
  overUs -= 1;
  if (overUs <= 0) notDropping();
});
addEventListener("dragover", (event) => {
  if (!droppedFiles(event)) return;
  event.preventDefault();              // without this no drop ever comes
  event.dataTransfer.dropEffect = "copy";
});
addEventListener("drop", (event) => {
  if (!droppedFiles(event)) return;    // dragged text is still the page's
  event.preventDefault();              // else the window leaves for the file
  notDropping();
  takeDrop(event.dataTransfer);
});

// -- the embedded terminal: the worker session, drawn here -------------
// A worker is a real Claude Code session in a tmux pane. Opening it
// attaches a real tmux client on a PTY (conductor side) and renders
// its byte stream in a real terminal emulator here - scrollback,
// colours, cursor and keys are the terminal's own, the way cmux draws
// its panes with terminal surfaces rather than screenshots.
let termId = null, lastEsc = 0;
let term = null, fitAddon = null, attachedId = null;
const termScreen = document.getElementById("termScreen");
const termPast = document.getElementById("termPast");
const termXterm = document.getElementById("termXterm");
const termLive = document.getElementById("termLive");

function toB64(s) {
  const bytes = new TextEncoder().encode(s);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}
function fromB64(s) {
  const bin = atob(s);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function ensureTerm() {
  if (term || typeof Terminal === "undefined") return term;
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  term = new Terminal({
    fontFamily: 'ui-monospace, "SF Mono", SFMono-Regular, Menlo, monospace',
    fontSize: 12, scrollback: 5000,
    theme: dark
      ? { background: "#1e1e1e", foreground: "#ececec",
          cursor: "#ececec" }
      : { background: "#ffffff", foreground: "#0d0d0d",
          cursor: "#0d0d0d", selectionBackground: "#b5d0ff" } });
  fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(termXterm);
  term.onData((data) => {
    if (attachedId) post("/stdin", { task_id: attachedId,
                                     data: toB64(data) });
  });
  term.onResize(({ cols, rows }) => {
    if (attachedId) post("/resize", { task_id: attachedId,
                                      cols: cols, rows: rows });
  });
  term.attachCustomKeyEventHandler((event) => {
    if (event.metaKey) return false;  // the app's shortcuts stay its own
    if (event.type === "keydown" && event.key === "Escape") {
      const now = Date.now();         // esc esc comes back; one esc goes in
      if (now - lastEsc < 600) { closeTerm(); return false; }
      lastEsc = now;
    }
    return true;
  });
  return term;
}

function sizeXterm() {
  termXterm.style.height = Math.max(120,
    termScreen.clientHeight - 16) + "px";   // the pane fills the view
  if (fitAddon && attachedId) fitAddon.fit();
}
addEventListener("resize", () => {
  if (document.body.classList.contains("term-open")) sizeXterm();
});

// What the worker did before this screen. A full-screen CLI repaints
// its rows in place, so tmux keeps no history and there is nothing
// above the pane to scroll into; the session's own transcript is where
// its past actually lives (conductor/worker_history.py).
async function loadHistory(taskId) {
  let data;
  try {
    data = await api("/history/" + taskId);
  } catch (err) { return; }
  if (termId !== taskId) return;
  termPast.replaceChildren();
  for (const row of data.rows || []) {
    const line = document.createElement("span");
    line.className = row.kind;
    line.textContent = (row.kind === "you" ? "› " :
                        row.kind === "tool" ? "  ⚙ " :
                        row.kind === "end" ? "" : "  ") + row.text + "\\n";
    termPast.appendChild(line);
  }
  if (data.rows && data.rows.length) {
    const rule = document.createElement("span");
    rule.className = "rule";
    rule.textContent = "── live ──────────────────────────────\\n";
    termPast.appendChild(rule);
  }
  termScreen.scrollTop = termScreen.scrollHeight;
}

async function openTerm(taskId, title) {
  if (!taskId) return;
  if (attachedId && attachedId !== taskId) {
    post("/detach", { task_id: attachedId });  // the pane moves whole
    attachedId = null;
  }
  termId = taskId;
  markSelected();
  document.getElementById("termTitle").textContent = title || taskId;
  document.body.classList.add("term-open");
  termPast.textContent = "";
  termLive.textContent = "";
  termLive.style.display = "none";
  termXterm.style.display = "none";
  loadHistory(taskId);
  if (ensureTerm()) {
    term.reset();
    termXterm.style.display = "block";
    sizeXterm();
    // Seed the scrollback with the pane's raw stream from birth: an
    // attached client only gets tmux's repaints from attach-time on,
    // and tmux keeps no pane history for a TUI that repaints in place.
    // The pipe-pane file is what a standalone terminal was fed.
    try {
      let at = 0;
      for (let hops = 0; hops < 40; hops += 1) {
        const seed = await api("/stream/" + taskId + "/" + at);
        if (termId !== taskId) return;
        if (!seed || !seed.have || !seed.b64) break;
        term.write(fromB64(seed.b64));
        at = seed.next;
      }
    } catch (err) {}
    const dims = (fitAddon && fitAddon.proposeDimensions())
      || { cols: 80, rows: 24 };
    async function tryAttach() {
      const opened = await post("/attach",
        { task_id: taskId, cols: dims.cols, rows: dims.rows });
      if (termId !== taskId || !(opened && opened.ok)) return false;
      termLive.style.display = "none";
      attachedId = taskId;
      document.body.classList.add("term-live");
      fitAddon.fit();
      term.focus();
      termScreen.scrollTop = termScreen.scrollHeight;
      return true;
    }
    if (await tryAttach()) return;
    if (termId !== taskId) return;
    // Failed: gone, or NOT YET? A worker still booting has no tmux
    // session and no last screen; one that ran and ended left one.
    // The loading state used to wear the tombstone ("it said the
    // session has ended. before it loaded", 2026-09-01).
    let probe = null;
    try { probe = await api("/term/" + taskId); } catch (err) {}
    if (termId !== taskId) return;
    if (!probe || !probe.html) {
      termLive.style.display = "block";
      termLive.innerHTML = '<span style="opacity:.6">Starting…</span>';
      for (let tries = 0; tries < 20; tries += 1) {
        await new Promise((wake) => setTimeout(wake, 700));
        if (termId !== taskId) return;
        if (await tryAttach()) return;
        if (termId !== taskId) return;
      }
    }
    termLive.style.display = "none";
    termXterm.style.display = "none";
  }
  // The session is gone (or the emulator failed to load): its saved
  // last screen, readable, no keys.
  attachedId = null;
  document.body.classList.remove("term-live");
  let data;
  try {
    data = await api("/term/" + taskId);
  } catch (err) { return; }
  if (termId !== taskId) return;
  termLive.style.display = "block";
  termLive.innerHTML = data.html
    ? data.html + '<div style="opacity:.6;padding:.4rem 0">' +
      'The session has ended \u2014 this is its last screen.</div>'
    : '<span style="opacity:.6">The session has ended.</span>';
  termScreen.scrollTop = termScreen.scrollHeight;
}

// The sidebar's selection follows what the pane shows: the Boss row while
// its chat is up, the worker's row while its terminal is.
function markSelected() {
  const boss = document.getElementById("bossRow");
  if (boss) boss.classList.toggle("off", !!termId);
  for (const line of document.querySelectorAll("#sessionList .sess"))
    line.classList.toggle("current", line.dataset.task === termId);
}

function closeTerm() {
  if (attachedId) post("/detach", { task_id: attachedId });
  attachedId = null;
  termId = null;
  markSelected();
  document.body.classList.remove("term-open");
  document.body.classList.remove("term-live");
  text.focus();
}

// Bytes from the worker's PTY land in the emulator; the stream ending
// is written into the pane rather than replacing it.
function termData(msg) {
  if (term && msg.task_id === attachedId) term.write(fromB64(msg.data));
}
function termExit(msg) {
  if (msg.task_id !== attachedId) return;
  attachedId = null;
  if (term) term.write(
    "\\r\\n\\x1b[2m\u2500\u2500 the session ended \u2500\u2500\\x1b[0m\\r\\n");
}


// Clicking the pane puts the keyboard back in the session - it is
// where the keys go, so it should be what has focus.
termScreen.addEventListener("mousedown", () => {
  if (attachedId && term && !getSelection().toString())
    setTimeout(() => term.focus(), 0);
});

// A dead pane has no terminal taking keys; esc esc still comes back.
document.addEventListener("keydown", (event) => {
  if (!document.body.classList.contains("term-open")) return;
  if (attachedId) return;         // the live pane's keys are the terminal's
  if (event.key === "Escape") {
    const now = Date.now();
    if (now - lastEsc < 600) {
      event.preventDefault();
      event.stopPropagation();
      closeTerm();
      return;
    }
    lastEsc = now;
  }
}, true);

if (NATIVE) bridgeSend({ path: "/ready" });   // history arrives pushed
else new EventSource("/events").onmessage =
  (event) => on(JSON.parse(event.data));
</script>
"""


def _activity(event) -> dict | None:
    """One row of a turn's activity, carrying what the terminal shows:
    a command with its output and exit code, a file change with its
    diff, a thought, a search, a plan. `text` is the glanceable line;
    `detail`, when present, is what the row opens into."""
    item = event.data or {}
    kind = event.item_type
    if kind == "commandExecution":
        row = {"type": "command", "text": event.text}
        output = str(item.get("aggregatedOutput") or item.get("output")
                     or "").strip()
        if output:
            row["detail"] = output[-4000:]
        code = item.get("exitCode")
        if isinstance(code, int) and code != 0:
            row["exit"] = code
        return row
    if kind == "fileChange":
        pieces = []
        for change in item.get("changes") or []:
            what = change.get("kind")
            if isinstance(what, dict):
                what = what.get("type")
            head = " ".join(p for p in (str(what or ""),
                                        str(change.get("path") or "")) if p)
            diff = str(change.get("diff") or change.get("unifiedDiff")
                       or "").strip()
            pieces.append(head + ("\n" + diff if diff else ""))
        row = {"type": "edit", "text": event.text}
        detail = "\n".join(p for p in pieces if p.strip())
        if detail:
            row["detail"] = detail[:4000]
        return row
    if kind == "mcpToolCall":
        return {"type": "tool", "text": event.text}
    if kind == "reasoning":
        words = str(item.get("text") or item.get("title")
                    or event.text or "").strip()
        if not words:
            return None
        first = words.splitlines()[0]
        row = {"type": "thought", "text": first}
        if words != first:
            row["detail"] = words[:4000]
        return row
    if kind == "webSearch":
        query = str(item.get("query") or event.text or "").strip()
        return {"type": "search", "text": query} if query else None
    if kind == "todoList":
        steps = [("\u2713 " if step.get("completed") else "\u25cb ")
                 + str(step.get("text") or "")
                 for step in item.get("items") or []]
        if not steps:
            return None
        done = sum(1 for step in item.get("items") or []
                   if step.get("completed"))
        return {"type": "plan", "text": f"plan \u00b7 {done}/{len(steps)}",
                "detail": "\n".join(steps)[:4000]}
    return None



# A sidebar card whose worker is known to be done with: removing it asks
# nothing. Any other glyph - working, waiting on the user, or a session
# the host still has open - may be a worker still running.
SETTLED_GLYPHS = ("done", "failed")
# What brings a removed card back: a result, a failure, a question.
NEWS_GLYPHS = ("done", "failed", "attention")
REMOVED_KEEP = 200


def _still_running(row: dict) -> str:
    """What the user is told before a live worker's card is removed."""
    title = row.get("title") or row.get("task_id")
    glyph = row.get("glyph")
    if glyph == "attention":
        head = f"{title} is waiting for you."
    elif glyph == "open":
        head = f"{title} still has its session open and may be running."
    else:
        head = f"{title} is still working."
    return (head + " Removing the card will not stop it: the worker keeps"
            " running, and its card comes back when it finishes or needs"
            " you.")


def _first_words(history: list) -> str:
    """A thread's title: the first thing the user said in it, or ""."""
    for item in history:
        if isinstance(item, dict) and item.get("kind") == "you":
            text = " ".join(str(item.get("text") or "").split())
            if text:
                return text[:60]
    return ""

FOCUS_PAGE = ("<!doctype html><meta charset=utf-8><title>{title}</title>"
              "<style>body{{font:14px -apple-system,system-ui;"
              "margin:3rem auto;max-width:22rem;color:#333}}</style>"
              "<p>{body}</p>"
              "<script>setTimeout(()=>window.close(),400)</script>")


class CodexWeb:
    """One conversation with Codex, served as a page on the loopback.

    Everything the page shows arrives over /events, so a reload replays
    the whole conversation from history and reconnects to the live
    stream - the browser holds no state the server does not.
    """

    def __init__(self, app: CodexApp, home=None, focus=None,
                 plain=None) -> None:
        # Two kinds of chat share one page. `app` is what the window was
        # built around - the Boss, when the conductor opens it - and
        # `plain` makes an ordinary coding chat with no orchestration
        # behind it: New chat starts one of those, because a new chat is
        # a new conversation, not a new Boss. Without a factory the page
        # behaves as it always did, and New chat is a fresh thread of
        # the one app.
        self.app = app
        self.boss_app = app
        self.plain = plain
        self._plain_app = None
        self.home = home
        self.focus = focus              # async fn(task_id): raise the window
        self.port = 0
        self.pending: dict[str, Approval] = {}
        self.history: list[dict] = []
        self.busy = False
        self.thread_options: dict = {}  # how a fresh thread is started
        self._titles: dict[str, str] = {}
        # thread id -> "boss" or "chat". What a row is, not who is
        # answering it right now: the sidebar tags the Boss's chat so
        # the two are told apart at a glance, and clicking a row picks
        # the backend that owns it.
        self._kinds: dict[str, str] = {}
        self._load_titles()
        self._watchers: list[asyncio.Queue] = []
        self._prompts: asyncio.Queue[str] = asyncio.Queue()
        self._server: asyncio.AbstractServer | None = None
        self._turns: asyncio.Task | None = None
        self.delegated: dict[str, dict] = {}
        self.poll = 2.0                 # how often a delegation is re-read
        self._watch: asyncio.Task | None = None
        self._spawns: asyncio.Task | None = None
        self._history_loaded = False    # the saved transcript, read once
        self._history_thread = ""       # ...for this thread
        # Where the open workspaces are asked after. A seam: tests hand
        # in their own, and a box with no cmux answers with nothing.
        self.list_cmux = _open_task_ids
        # task_id -> the name the session was given, kept on disk: the
        # sessions file trims and restarts empty, but a session that was
        # ever named should never fall back to its raw task id.
        self._session_names: dict[str, str] = {}
        self._load_session_names()
        # task_id -> the glyph and status its card had when the user took
        # it off the sidebar, kept on disk so a restart does not bring
        # it back.
        self._removed: dict[str, dict] = {}
        self._load_removed()
        self.hidden_running = 0         # removed cards whose worker is live
        # The live terminal streams behind the page's embedded panes.
        self.terms = TermStreams(self._send_all)

    # -- the stream ------------------------------------------------------
    def push(self, message: dict) -> None:
        """One thing for the page to draw, now and on every reload."""
        self.history.append(message)
        for queue in self._watchers:
            queue.put_nowait(message)

    def _send_all(self, message: dict) -> None:
        """To every open page, but not into history."""
        for queue in self._watchers:
            queue.put_nowait(message)

    def show_terminal(self, task_id: str) -> None:
        """A notification's deep link: the session's terminal is drawn in
        this window, so opening the session means opening it here."""
        self._send_all({"kind": "focus_session", "task_id": task_id,
                        "title": self._session_names.get(task_id, "")})

    # -- threads -----------------------------------------------------------
    def _thread_file(self, thread_id: str) -> Path | None:
        if self.home is None or not thread_id or len(thread_id) > 128 or \
                not all(c.isalnum() or c in "_-." for c in thread_id):
            return None
        return Path(self.home) / "web_threads" / (thread_id + ".json")

    def _load_titles(self) -> None:
        if self.home is None:
            return
        folder = Path(self.home) / "web_threads"
        if not folder.is_dir():
            return
        for file in sorted(folder.glob("*.json"),
                           key=lambda f: f.stat().st_mtime):
            try:
                saved = json.loads(file.read_text())
            except (OSError, ValueError):
                application_log("ui", "app_web.thread_unreadable",
                                f"could not read {file.name}",
                                severity="warning", exc_info=True)
                continue
            if file.stem == "boss":
                continue              # the placeholder id is not a chat
            self._kinds[file.stem] = "chat" \
                if str(saved.get("kind") or "boss") == "chat" else "boss"
            title = str(saved.get("title") or "")
            if not title or title == file.stem:
                # An untitled thread used to be named for its id, and
                # that name was then saved as its title - the sidebar
                # read "boss_4edd02c0" over a 31-line conversation. A
                # thread is named for the first thing said in it.
                title = _first_words(saved.get("history") or [])
            self._titles[file.stem] = title

    def _save_thread(self) -> None:
        if (self.app.thread_id or "boss") == "boss":
            return          # the placeholder id is not a chat to keep
        file = self._thread_file(self.app.thread_id or "")
        if file is None:
            return
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(json.dumps(
                {"title": self._titles.get(self.app.thread_id, ""),
                 "kind": self.kind_of(self.app.thread_id),
                 "history": self.history}))
        except OSError:
            application_log("ui", "app_web.thread_unwritable",
                            "could not save the thread",
                            severity="warning", exc_info=True)

    def _restore_history(self) -> None:
        """The conversation as it was: a page served by a fresh process
        starts from the thread's saved transcript, not from nothing.

        Read once per thread, not once. The window bridge restores at
        app start, when the Boss session does not exist yet and the
        thread is the "boss" placeholder; the page attaches two minutes
        later on the real session. Measured 2026-09-01: 243 entries on
        disk, and the window said "What should we work on?" over the
        placeholder's one state line.
        """
        tid = self.app.thread_id or ""
        if self._history_loaded and self._history_thread == tid:
            return
        if self._history_loaded and self._history_thread not in ("", "boss"):
            return          # a real thread's history; switching goes through switch_thread
        self._history_loaded = True
        self._history_thread = tid
        if self._history_thread == "boss":
            return          # the placeholder is not a chat; wait for the Boss
        saved = self._load_history(tid)
        if saved:
            self.history = saved
            self._freshen_cards()

    def _load_history(self, thread_id: str) -> list[dict]:
        file = self._thread_file(thread_id)
        if file is None or not file.is_file():
            return []
        try:
            history = json.loads(file.read_text()).get("history")
        except (OSError, ValueError):
            application_log("ui", "app_web.thread_unreadable",
                            f"could not read {file.name}",
                            severity="warning", exc_info=True)
            return []
        return history if isinstance(history, list) else []

    def _freshen_cards(self) -> None:
        """The saved transcript's delegation cards, repainted from the
        sessions that exist now.

        A card is saved with the status it had at the time, so a
        transcript written by an earlier process holds cards mid-spin
        for workers that have long since settled - replayed as they
        were, they spin for ever. Each card is given the session's
        current words; one no host knows any more is settled as ended;
        one saved already settled keeps the words it answered with.
        Cards still live are put back under the delegation watch so
        they keep following their sessions."""
        current = {row.get("task_id"): row for row in self.sessions()}
        live: dict[str, dict] = {}
        for message in self.history:
            kind = message.get("kind")
            if kind == "toast":
                rows = message.get("rows") or []
            elif kind == "session":
                rows = [message]
            else:
                continue
            for row in rows:
                task_id = row.get("task_id")
                if not task_id:
                    continue
                if row.get("glyph") in ("done", "failed"):
                    continue        # a settled card keeps its words
                now = current.get(task_id)
                if now is not None:
                    row["status"] = now.get("status")
                    row["glyph"] = now.get("glyph")
                else:
                    row["status"] = "Session ended"
                    row["glyph"] = "open"
                if row.get("glyph") not in ("done", "failed", "open"):
                    live[task_id] = {"status": row.get("status"),
                                     "glyph": row.get("glyph")}
        for task_id, seen in live.items():
            self.delegated.setdefault(task_id, seen)
        if live and (self._watch is None or self._watch.done()):
            self._watch = asyncio.create_task(self._watch_delegations())

    def threads(self) -> list[dict]:
        """The sidebar's list, oldest first, the open one marked.

        The placeholder id ("boss", before any session exists) is not a
        chat: it saved a phantom row that could never resume (measured
        2026-09-01, KeyError: 'boss' on click). A current chat with no
        words yet reads "New chat", not its raw session id; a thread
        nobody has said anything in is not listed until it is the open
        one."""
        current = self.app.thread_id
        rows = []
        for tid, title in self._titles.items():
            if tid == "boss":
                continue
            if tid == current and (not title or title == tid):
                title = "New chat"
            if not title:
                continue          # nothing said in it yet: not a thread
            rows.append({"id": tid, "title": title,
                         "kind": self.kind_of(tid),
                         "current": tid == current})
        if current and current != "boss" and \
                current not in self._titles:
            rows.append({"id": current, "title": "New chat",
                         "kind": self.kind_of(current), "current": True})
        return rows

    def kind_of(self, thread_id: str | None) -> str:
        """What a thread is. The Boss's own chat is the default: every
        thread was one before an ordinary chat was possible."""
        return self._kinds.get(thread_id or "", "boss")

    async def _plain_chat(self):
        """The ordinary chat's backend, made once and kept: a second
        one would be a second process for the same window."""
        if self._plain_app is None:
            app = self.plain()
            await app.start()
            self._plain_app = app
        return self._plain_app

    async def switch_thread(self, thread_id: str | None) -> None:
        """Put another conversation on the page: the current one is
        saved, the named one resumes (or a fresh one starts), and every
        open page redraws from that thread's history.

        New chat (no id) is an ordinary chat when the window was given a
        way to make one - a coding session of its own, not another Boss.
        An old thread goes back to whichever kind it was.
        """
        self._save_thread()
        self.pending.clear()
        if thread_id:
            kind = self.kind_of(thread_id)
            self.app = await self._plain_chat() if kind == "chat" \
                else self.boss_app
            await self.app.resume_thread(thread_id)
            self.history = self._load_history(thread_id)
            self._freshen_cards()
        elif self.plain is not None:
            self.app = await self._plain_chat()
            await self.app.start_thread()
            self._kinds[self.app.thread_id or ""] = "chat"
            self.history = []
        else:
            self.app = self.boss_app
            await self.app.start_thread(**self.thread_options)
            self.history = []
        self._history_loaded = True
        self._history_thread = self.app.thread_id or ""
        self._send_all({"kind": "reset", "history": list(self.history)})

    # -- the sessions ------------------------------------------------------
    def _names_file(self) -> Path | None:
        if self.home is None:
            return None
        return Path(self.home) / "boss" / "session_titles.json"

    def _load_session_names(self) -> None:
        file = self._names_file()
        if file is None or not file.is_file():
            return
        try:
            names = json.loads(file.read_text())
        except (OSError, ValueError):
            application_log("ui", "app_web.names_unreadable",
                            "the session names file could not be read",
                            severity="warning", exc_info=True)
            return
        if isinstance(names, dict):
            self._session_names = {str(k): str(v) for k, v in names.items()}

    def _remember_names(self, rows: list[dict]) -> None:
        """Keep every session's given name, for after the file forgets."""
        learned = False
        for row in rows:
            task_id, title = row.get("task_id"), row.get("title")
            if task_id and title and title != task_id and \
                    self._session_names.get(task_id) != title:
                self._session_names[task_id] = title
                learned = True
        file = self._names_file()
        if not learned or file is None:
            return
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(json.dumps(self._session_names))
        except OSError:
            application_log("ui", "app_web.names_unwritable",
                            "the session names could not be kept",
                            severity="warning", exc_info=True)

    def sessions(self) -> list[dict]:
        """Every worker session there is, as the delegation cards show
        one: title, status, glyph, link.

        Two sources, because neither alone survives everything. The
        conductor's sessions file has the titles, statuses and glyphs,
        but it is the memory of one conductor process: a restart begins
        it again, empty. The sessions themselves live in their host -
        a tmux session or a cmux workspace - which keeps them until
        someone closes them, so the hosts are asked too, and a session
        the file has forgotten still gets a row. A row the file
        remembers keeps its words; one only the host knows is simply
        "Still open".
        """
        rows: list[dict] = []
        if self.home is not None:
            try:
                rows = read_sessions(self.home)
            except Exception:
                application_log("ui", "app_web.sessions_unreadable",
                                "the sessions file could not be read",
                                severity="warning", exc_info=True)
        out = [{"task_id": row.get("task_id"), "title": row.get("title"),
                "status": row.get("status"), "glyph": row.get("glyph")}
               for row in rows
               # Only workers: the Boss itself is never a session row.
               if str(row.get("task_id") or "") not in ("", "boss")]
        self._remember_names(out)
        known = {row["task_id"] for row in out}
        for task_id in self.list_cmux():
            if task_id not in known:
                out.append({"task_id": task_id,
                            "title": self._session_names.get(task_id,
                                                             task_id),
                            "status": "Still open", "glyph": "open"})
        out = self._without_removed(out)
        # What is working sits above what has settled - the sidebar is a
        # glance, and the glance is at the live work.
        out.sort(key=lambda row: row.get("glyph") in ("done", "failed",
                                                      "open"))
        return out

    # -- a card the user took off the sidebar ------------------------------
    def _removed_file(self) -> Path | None:
        if self.home is None:
            return None
        return Path(self.home) / "boss" / "sidebar_removed.json"

    def _load_removed(self) -> None:
        file = self._removed_file()
        if file is None or not file.is_file():
            return
        try:
            removed = json.loads(file.read_text())
        except (OSError, ValueError):
            application_log("ui", "app_web.removed_unreadable",
                            "the removed cards file could not be read",
                            severity="warning", exc_info=True)
            return
        if isinstance(removed, dict):
            self._removed = {str(k): v for k, v in removed.items()
                             if isinstance(v, dict)}

    def _save_removed(self) -> None:
        file = self._removed_file()
        if file is None:
            return
        # Newest kept: a list of ids nobody lists any more must not grow
        # for ever.
        if len(self._removed) > REMOVED_KEEP:
            newest = sorted(self._removed.items(),
                            key=lambda item: item[1].get("at") or 0)
            self._removed = dict(newest[-REMOVED_KEEP:])
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            tmp = file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._removed))
            tmp.replace(file)
        except OSError:
            application_log("ui", "app_web.removed_unwritable",
                            "the removed cards could not be kept",
                            severity="warning", exc_info=True)

    def _without_removed(self, rows: list[dict]) -> list[dict]:
        """The rows, less the cards the user removed - except a card
        with news since: a worker that finished, failed or asked for the
        user after its card was taken away is shown again, so removing a
        card never hides a result or a question."""
        kept, returned, hidden_running = [], False, 0
        for row in rows:
            task_id = row.get("task_id") or ""
            gone = self._removed.get(task_id)
            now = (row.get("glyph"), row.get("status"))
            if gone is not None and now[0] in NEWS_GLYPHS and \
                    now != (gone.get("glyph"), gone.get("status")):
                self._removed.pop(task_id, None)
                returned = True
                application_log("ui", "app_web.card_returned",
                                "a removed card came back with news",
                                severity="info", task_id=task_id,
                                glyph=now[0])
                gone = None
            if gone is None:
                kept.append(row)
            elif now[0] not in SETTLED_GLYPHS:
                hidden_running += 1
        # Still counted where the page says how many workers the Boss is
        # waiting for: a removed card is not a stopped worker.
        self.hidden_running = hidden_running
        if returned:
            self._save_removed()
        return kept

    def remove_session(self, task_id: str, confirmed: bool = False) -> dict:
        """The user taking a worker's card off the sidebar.

        Only the card goes. Nothing here stops, closes or deletes
        anything: the worker's session, worktree and branch stay as they
        are. A worker that may still be running is asked about first,
        because a card that disappears reads as work that stopped - the
        answer says the worker keeps going, and the card is removed only
        once that has been confirmed."""
        if not _safe_task_id(task_id):
            return {"ok": False, "error": "no such session"}
        row = next((r for r in self.sessions()
                    if r.get("task_id") == task_id), None)
        if row is None:
            return {"ok": True, "removed": False}     # not on the sidebar
        running = row.get("glyph") not in SETTLED_GLYPHS
        if running and not confirmed:
            return {"ok": False, "running": True,
                    "message": _still_running(row)}
        self._removed[task_id] = {"glyph": row.get("glyph"),
                                  "status": row.get("status"),
                                  "at": time.time()}
        self._save_removed()
        application_log("ui", "app_web.card_removed",
                        "the user removed a card from the sidebar",
                        severity="info", task_id=task_id,
                        glyph=row.get("glyph"), running=running)
        return {"ok": True, "removed": True, "running": running}

    # -- the conversation --------------------------------------------------
    async def run_turn(self, text: str) -> None:
        self._restore_history()
        tid = self.app.thread_id
        if tid and not self._titles.get(tid):
            self._titles[tid] = " ".join(text.split())[:60]
        self.busy = True
        self.push({"kind": "you", "text": text})
        self.push({"kind": "state", "busy": True})
        started = asyncio.get_running_loop().time()
        answer, tools = "", []
        try:
            async for event in self.app.turn(text):
                if event.kind == "delta":
                    self.push({"kind": "delta", "text": event.text,
                               "item_id": event.item_id})
                elif event.kind == "item":
                    if event.item_type == "agentMessage":
                        answer = event.text
                    else:
                        row = _activity(event)
                        if row is not None:
                            tools.append(row)
                            self.push({"kind": "tool", **row})
                elif event.kind == "approval":
                    approval = event.data["approval"]
                    self.pending[approval.item_id] = approval
                    self.push({"kind": "approval",
                               "item_id": approval.item_id,
                               "question": approval.question,
                               "detail": approval.detail,
                               "cwd": approval.cwd})
                elif event.kind == "turn_done":
                    answer = event.text or answer
                elif event.kind == "error":
                    self.push({"kind": "error", "text": event.text})
                    return
            # The turn settles before busy drops: the page must never see
            # an idle state while its last bubble is still streaming.
            elapsed = asyncio.get_running_loop().time() - started
            self.push({"kind": "turn", "answer": answer, "tools": tools,
                       "model": self.app.model, "seconds": elapsed})
        finally:
            self.busy = False
            self.push({"kind": "state", "busy": False})
            self._save_thread()
        self._toast_for(answer)
        self._save_thread()

    def _toast_for(self, answer: str) -> None:
        """A turn that mentions a session gets its delegation cards."""
        rows = mentioned(answer, read_sessions(self.home)) \
            if self.home else []
        rows = [row for row in rows           # one card per session: the
                if row.get("task_id") not in self.delegated]  # spawn watch
        if not rows:                          # may have drawn it already
            return
        self._remember_names(rows)
        self.push({"kind": "toast", "rows": [
            {"task_id": row.get("task_id"), "title": row.get("title"),
             "status": row.get("status"), "glyph": row.get("glyph")}
            for row in rows]})
        for row in rows:
            if row.get("task_id"):
                self.delegated[row["task_id"]] = {
                    "status": row.get("status"),
                    "glyph": row.get("glyph")}
        if self._watch is None or self._watch.done():
            self._watch = asyncio.create_task(self._watch_delegations())

    # -- the voice's turns ---------------------------------------------------
    def mirror_prompt(self, text: str) -> None:
        """A spoken utterance, drawn the way a typed one is: the voice's
        turn runs through the conductor, so the page only mirrors it."""
        self._restore_history()
        tid = self.app.thread_id
        if tid and not self._titles.get(tid):
            self._titles[tid] = " ".join(text.split())[:60]
        self.busy = True
        self.push({"kind": "you", "text": text})
        self.push({"kind": "state", "busy": True})

    def mirror_delta(self, text: str) -> None:
        """A paragraph of the Boss's reply, drawn as it is written - the
        turn's answer replaces it when the turn settles. Only while a
        turn is on the page: prose answering a pushed worker update has
        no bubble to go in."""
        if self.busy and text.strip():
            self.push({"kind": "delta", "text": text.strip() + "\n\n",
                       "item_id": ""})

    def mirror_answer(self, answer: str, seconds: float = 0.0) -> None:
        """The Boss's reply to a spoken utterance, settled on the page -
        with the delegation cards a mentioned session gets."""
        self.push({"kind": "turn", "answer": answer, "tools": [],
                   "model": getattr(self.app, "model", ""),
                   "seconds": seconds})
        self.busy = False
        self.push({"kind": "state", "busy": False})
        self._toast_for(answer)
        self._save_thread()

    async def _watch_delegations(self) -> None:
        """A delegation bar follows its session until the work settles."""
        while any(seen.get("glyph") not in ("done", "failed")
                  for seen in self.delegated.values()):
            await asyncio.sleep(self.poll)
            try:
                rows = {row.get("task_id"): row
                        for row in read_sessions(self.home)}
            except Exception:
                application_log("ui", "app_web.sessions_unreadable",
                                "the sessions file could not be read",
                                severity="warning", exc_info=True)
                continue
            for task_id, seen in self.delegated.items():
                row = rows.get(task_id)
                if row is None:
                    continue
                now = {"status": row.get("status"),
                       "glyph": row.get("glyph")}
                if now != seen:
                    self.delegated[task_id] = now
                    self.push({"kind": "session", "task_id": task_id,
                               "title": row.get("title"), **now})

    async def _watch_spawns(self) -> None:
        """A delegation card the moment a worker session exists.

        The sessions file is the conductor's record of every worker; a
        task id it gains is a worker that just started, and its card is
        drawn at once - whether or not any turn's answer names it.
        Sessions that predate this page get no card."""
        if self.home is None:
            return
        known: set[str] | None = None
        while True:
            try:
                rows = await asyncio.to_thread(read_sessions, self.home)
            except Exception:
                application_log("ui", "app_web.sessions_unreadable",
                                "the sessions file could not be read",
                                severity="warning", exc_info=True)
                rows = None
            if rows is not None:
                ids = {row.get("task_id") for row in rows
                       if str(row.get("task_id") or "")
                       not in ("", "boss")}
                if known is None:
                    known = ids
                elif ids - known:
                    # A session a turn's answer already drew a card for
                    # (it is in delegated) does not get a second one.
                    fresh = [row for row in rows
                             if row.get("task_id") in ids - known
                             and row.get("task_id") not in self.delegated]
                    known |= ids
                    self._remember_names(fresh)
                    self.push({"kind": "toast", "rows": [
                        {"task_id": row.get("task_id"),
                         "title": row.get("title"),
                         "status": row.get("status"),
                         "glyph": row.get("glyph")} for row in fresh]})
                    for row in fresh:
                        self.delegated[row["task_id"]] = {
                            "status": row.get("status"),
                            "glyph": row.get("glyph")}
                    if self._watch is None or self._watch.done():
                        self._watch = asyncio.create_task(
                            self._watch_delegations())
                    self._save_thread()
            await asyncio.sleep(self.poll)

    async def _turn_loop(self) -> None:
        """Turns run one at a time; what arrives mid-turn waits its turn."""
        while True:
            prompt = await self._prompts.get()
            try:
                await self.run_turn(prompt)
            except Exception as exc:
                application_log("ui", "app_web.turn_failed",
                                "a turn failed", severity="error",
                                exc_info=True)
                self.push({"kind": "error", "text": str(exc)[:300]})
                self.push({"kind": "state", "busy": False})

    def answer(self, item_id: str, decision: str) -> bool:
        approval = self.pending.pop(item_id, None)
        if approval is None:
            return False
        approval.answer(decision or "accept")
        self.push({"kind": "decision", "item_id": item_id,
                   "decision": decision or "accept"})
        return True

    # -- the server --------------------------------------------------------
    async def open(self) -> None:
        """The living half without the server: the turn loop and the
        spawn watcher. A WindowBridge front needs these and no port."""
        if self._turns is None:
            self._turns = asyncio.create_task(self._turn_loop())
        if self._spawns is None:
            self._spawns = asyncio.create_task(self._watch_spawns())

    async def start(self, port: int = 0) -> int:
        self._server = await asyncio.start_server(self._client, HOST, port)
        self.port = self._server.sockets[0].getsockname()[1]
        await self.open()
        application_log("ui", "app_web.listening",
                        f"the codex page is on http://{HOST}:{self.port}/",
                        severity="debug", port=self.port)
        return self.port

    async def stop(self) -> None:
        if self._turns is not None:
            self._turns.cancel()
            self._turns = None
        if self._watch is not None:
            self._watch.cancel()
            self._watch = None
        if self._spawns is not None:
            self._spawns.cancel()
            self._spawns = None
        await self.terms.close()
        for queue in list(self._watchers):
            queue.put_nowait(None)          # ends that event stream
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}/" if self.port else ""

    async def _client(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            method, path, body = await self._read_request(reader)
            if method == "GET" and path == "/":
                await self._respond(writer, "200 OK", "text/html", PAGE)
            elif method == "GET" and path.startswith("/assets/"):
                await self._asset(writer, path[len("/assets/"):])
            elif method == "GET" and path == "/events":
                await self._events(writer)
            elif method == "POST" and path == "/say":
                said = str((body or {}).get("text") or "").strip()
                if said:
                    self._prompts.put_nowait(said)
                await self._respond(writer, "200 OK", "application/json",
                                    '{"ok": true}')
            elif method == "POST" and path == "/answer":
                found = self.answer(str((body or {}).get("item_id") or ""),
                                    str((body or {}).get("decision") or ""))
                await self._respond(
                    writer, "200 OK" if found else "404 Not Found",
                    "application/json", json.dumps({"ok": found}))
            elif method == "POST" and path == "/interrupt":
                try:
                    await self.app.interrupt()
                except Exception as exc:
                    application_log("ui", "app_web.interrupt_failed",
                                    "could not interrupt the turn",
                                    severity="warning", exc_info=True)
                    await self._respond(writer, "500 Internal Server Error",
                                        "application/json",
                                        json.dumps({"error": str(exc)[:200]}))
                    return
                await self._respond(writer, "200 OK", "application/json",
                                    '{"ok": true}')
            elif method == "GET" and path == "/threads":
                await self._respond(writer, "200 OK", "application/json",
                                    json.dumps({"threads": self.threads()}))
            elif method == "POST" and path == "/thread":
                await self._switch(writer,
                                   str((body or {}).get("id") or "") or None)
            elif method == "GET" and path == "/sessions":
                rows = await asyncio.to_thread(self.sessions)
                await self._respond(writer, "200 OK", "application/json",
                                    json.dumps({"rows": rows,
                                                "hidden_running":
                                                    self.hidden_running}))
            elif method == "POST" and path == "/remove":
                result = await asyncio.to_thread(
                    self.remove_session,
                    str((body or {}).get("task_id") or ""),
                    bool((body or {}).get("confirmed")))
                await self._respond(writer, "200 OK", "application/json",
                                    json.dumps(result))
            elif method == "GET" and path.startswith("/history/"):
                await self._history(writer, path[len("/history/"):].strip("/"))
            elif method == "GET" and path.startswith("/stream/"):
                rest = path[len("/stream/"):].strip("/")
                task_id, _, start = rest.partition("/")
                if not _safe_task_id(task_id):
                    await self._respond(writer, "404 Not Found",
                                        "application/json",
                                        '{"error": "no such session"}')
                else:
                    chunk = await asyncio.to_thread(
                        _stream_chunk, task_id,
                        int(start) if start.isdigit() else 0)
                    await self._respond(writer, "200 OK",
                                        "application/json",
                                        json.dumps(chunk))
            elif method == "GET" and path.startswith("/term/"):
                await self._term(writer, path[len("/term/"):].strip("/"))
            elif method == "POST" and path == "/key":
                await self._key(writer, body or {})
            elif method == "POST" and path in ("/attach", "/detach",
                                                "/stdin", "/resize"):
                result = await self._term_door(path, body or {})
                await self._respond(writer, "200 OK", "application/json",
                                    json.dumps(result))
            elif method == "POST" and path == "/drop":
                await self._drop(writer, body or {})
            elif method == "GET" and path.startswith("/s/"):
                await self._jump(writer, path[3:].strip("/"))
            else:
                await self._respond(writer, "404 Not Found", "text/plain",
                                    "nothing here")
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _asset(self, writer: asyncio.StreamWriter,
                     name: str) -> None:
        """The vendored terminal emulator files the page loads."""
        if name not in ASSET_FILES:
            await self._respond(writer, "404 Not Found", "text/plain",
                                "nothing here")
            return
        await self._respond(writer, "200 OK",
                            ASSET_TYPES[Path(name).suffix],
                            (ASSETS / name).read_text())

    async def _term_door(self, path: str, body: dict) -> dict:
        """The four live-terminal doors, shared with the bridge: attach
        a real tmux client, feed it keys, resize it, let it go."""
        task_id = str(body.get("task_id") or "")
        if path == "/attach":
            return {"ok": await self.terms.attach(
                task_id, _as_int(body.get("cols"), 80),
                _as_int(body.get("rows"), 24))}
        if path == "/detach":
            await self.terms.detach(task_id)
            return {"ok": True}
        if path == "/stdin":
            try:
                data = base64.b64decode(str(body.get("data") or ""),
                                        validate=True)
            except (binascii.Error, ValueError):
                return {"ok": False}
            return {"ok": self.terms.write(task_id, data)}
        if path == "/resize":
            return {"ok": self.terms.resize(
                task_id, _as_int(body.get("cols"), 80),
                _as_int(body.get("rows"), 24))}
        return {"ok": False}

    async def _history(self, writer: asyncio.StreamWriter,
                       task_id: str) -> None:
        """What the worker did before the screen it is showing now: its
        own transcript, which is the only place its past is kept (see
        conductor/worker_history.py)."""
        if not _safe_task_id(task_id) or self.home is None:
            await self._respond(writer, "404 Not Found", "application/json",
                                '{"error": "no such session"}')
            return
        rows = await asyncio.to_thread(_history_rows, self.home, task_id)
        await self._respond(writer, "200 OK", "application/json",
                            json.dumps({"rows": rows}))

    async def _term(self, writer: asyncio.StreamWriter,
                    task_id: str) -> None:
        """The worker's terminal, as it stands: the tmux pane's capture,
        colours kept, for the page's embedded view."""
        if not _safe_task_id(task_id):
            await self._respond(writer, "404 Not Found", "application/json",
                                '{"error": "no such session"}')
            return
        screen, alive = await asyncio.to_thread(_capture_pane, task_id)
        await self._respond(writer, "200 OK", "application/json",
                            json.dumps({"html": screen, "alive": alive}))

    @property
    def drops(self) -> Path:
        """Where a file dropped on the page is kept."""
        return drops_folder(self.home)

    async def _drop(self, writer: asyncio.StreamWriter,
                    body: dict) -> None:
        """A file dropped on the page, saved where the Boss can read it.

        A browser hands the page bytes and a name, never a path, so the
        bytes are written here and the box is given the path they were
        written to. The app's window knows the real path and never asks.
        """
        name = _drop_name(str(body.get("name") or ""))
        try:
            raw = base64.b64decode(str(body.get("data") or ""), validate=True)
        except (ValueError, TypeError):
            raw = b""
        if not raw:
            # The status stays honest and the body stays JSON: fetch
            # does not reject on 4xx, so the page reads {ok:false} the
            # same way it reads the bridge's reply.
            await self._respond(writer, "400 Bad Request", "application/json",
                                json.dumps({"ok": False,
                                            "error": "the file did not arrive"}))
            return
        folder = self.drops
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target = await asyncio.to_thread(_keep_file, folder, name, raw)
        except OSError as exc:
            application_log("ui", "app_web.drop_failed",
                            f"could not keep {name}",
                            severity="warning", exc_info=True)
            await self._respond(writer, "500 Internal Server Error",
                                "application/json",
                                json.dumps({"ok": False,
                                            "error": str(exc)[:200]}))
            return
        application_log("ui", "app_web.file_dropped",
                        f"kept a dropped file at {target}",
                        severity="debug", bytes_kept=len(raw))
        await self._respond(writer, "200 OK", "application/json",
                            json.dumps({"ok": True, "path": str(target)}))

    async def _key(self, writer: asyncio.StreamWriter,
                   body: dict) -> None:
        """A keystroke from the embedded terminal into the worker's own
        tmux pane - typing there IS typing into the session."""
        task_id = str(body.get("task_id") or "")
        if not _safe_task_id(task_id):
            await self._respond(writer, "404 Not Found", "application/json",
                                '{"error": "no such session"}')
            return
        sent = await asyncio.to_thread(
            _send_to_pane, task_id,
            str(body.get("text") or ""), str(body.get("key") or ""))
        await self._respond(writer, "200 OK" if sent else "400 Bad Request",
                            "application/json", json.dumps({"ok": sent}))

    async def _switch(self, writer: asyncio.StreamWriter,
                      thread_id: str | None) -> None:
        if self.busy:
            await self._respond(writer, "409 Conflict", "application/json",
                                '{"error": "a turn is running"}')
            return
        try:
            await self.switch_thread(thread_id)
        except Exception as exc:
            application_log("ui", "app_web.switch_failed",
                            "could not switch threads",
                            severity="warning", exc_info=True)
            await self._respond(writer, "500 Internal Server Error",
                                "application/json",
                                json.dumps({"error": str(exc)[:200]}))
            return
        await self._respond(writer, "200 OK", "application/json",
                            '{"ok": true}')

    async def _events(self, writer: asyncio.StreamWriter) -> None:
        """SSE: the history so far, then everything as it happens."""
        self._restore_history()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     b"Cache-Control: no-cache\r\nConnection: keep-alive\r\n"
                     b"\r\n")
        queue: asyncio.Queue = asyncio.Queue()
        for message in self.history:
            queue.put_nowait(message)
        self._watchers.append(queue)
        try:
            while True:
                message = await queue.get()
                if message is None:          # stop() ending the stream
                    return
                writer.write(f"data: {json.dumps(message)}\n\n".encode())
                await writer.drain()
        except (ConnectionError, RuntimeError):
            pass
        finally:
            if queue in self._watchers:
                self._watchers.remove(queue)

    async def _jump(self, writer: asyncio.StreamWriter,
                    task_id: str) -> None:
        """The toast's open link: same contract as conductor/jump.py."""
        if not task_id or len(task_id) > 64 or \
                not all(c.isalnum() or c in "_-" for c in task_id):
            await self._respond(writer, "404 Not Found", "text/html",
                                FOCUS_PAGE.format(
                                    title="Nothing here",
                                    body="That link does not name a session."))
            return
        if self.focus is None:
            self.show_terminal(task_id)
            await self._respond(writer, "200 OK", "text/html",
                                FOCUS_PAGE.format(
                                    title="Opening",
                                    body=f"Bringing {task_id} forward."))
            return
        try:
            await self.focus(task_id)
        except Exception as exc:
            application_log("ui", "app_web.focus_failed",
                            f"could not focus {task_id}",
                            severity="warning", exc_info=True,
                            task_id=task_id)
            await self._respond(writer, "500 Internal Server Error",
                                "text/html",
                                FOCUS_PAGE.format(
                                    title="Could not open it",
                                    body=f"{task_id} did not come forward: "
                                         f"{exc}"))
            return
        await self._respond(writer, "200 OK", "text/html",
                            FOCUS_PAGE.format(
                                title="Opening",
                                body=f"Bringing {task_id} forward."))

    @staticmethod
    async def _read_request(reader: asyncio.StreamReader):
        line = await asyncio.wait_for(reader.readline(), 5)
        parts = line.decode("latin-1", "replace").split()
        method = parts[0] if parts else ""
        path = parts[1].split("?", 1)[0] if len(parts) > 1 else ""
        length = 0
        while True:
            header = await asyncio.wait_for(reader.readline(), 5)
            if header in (b"\r\n", b"\n", b""):
                break
            name, _, value = header.decode("latin-1", "replace").partition(":")
            if name.strip().lower() == "content-length":
                cap = DROP_BODY_LIMIT if path == "/drop" else 1 << 20
                try:
                    length = min(int(value.strip()), cap)
                except ValueError:
                    length = 0
        body = None
        if length:
            raw = await asyncio.wait_for(
                reader.readexactly(length),
                30 if length > (1 << 20) else 5)   # a file takes longer
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
        return method, path, body

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: str,
                       content_type: str, text: str) -> None:
        payload = text.encode()
        writer.write(f"HTTP/1.1 {status}\r\n"
                     f"Content-Type: {content_type}; charset=utf-8\r\n"
                     f"Content-Length: {len(payload)}\r\n"
                     f"Connection: close\r\n\r\n".encode() + payload)
        try:
            await writer.drain()
        except Exception:
            pass


def _cmux_runtime():
    """A runtime that only reads and focuses - no conductor behind it."""
    from .cmux_runtime import CmuxClaudeRuntime
    runtime = CmuxClaudeRuntime.__new__(CmuxClaudeRuntime)
    runtime.cmux = shutil.which("cmux")
    runtime._password = None
    runtime.places, runtime._workspaces_cache = {}, None
    runtime.transcript = None
    return runtime


def _cmux_task_ids() -> list[str]:
    """The task ids of every conductor workspace open in cmux, newest
    last - the workspaces are the sessions, so their being listed is
    what "the session still exists" means."""
    runtime = _cmux_runtime()
    if not runtime.cmux:
        return []
    out = []
    for workspace in runtime._workspaces():
        name = workspace.get("custom_title") or ""
        if name.startswith("cond_") and runtime._is_ours(workspace):
            out.append(name[len("cond_"):])
    return out


def _tmux_task_ids() -> list[str]:
    """The task ids of every conductor worker living in a tmux session -
    the surface the embedded terminal draws."""
    try:
        done = subprocess.run(["tmux", "list-sessions", "-F",
                               "#{session_name}"],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if done.returncode != 0:
        return []
    return [line[len("cond_"):] for line in done.stdout.splitlines()
            if line.startswith("cond_task_")]


def _open_task_ids() -> list[str]:
    """Every worker session that still exists, wherever it is hosted:
    a cmux workspace or a tmux session. Their existing is what "the
    session still exists" means."""
    out = _cmux_task_ids()
    seen = set(out)
    for task_id in _tmux_task_ids():
        if task_id not in seen:
            out.append(task_id)
    return out


# -- the embedded terminal: a tmux pane drawn as HTML -------------------

_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_OTHER_ESC = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)"
                        r"|\[[0-9;?]*[A-Za-z]|[()][B0]|[=>])")

_ANSI16 = ("#000000", "#cc4b40", "#4fa568", "#c2a038", "#4f83c6",
           "#a06bbf", "#4aa5a8", "#c7c7c7", "#7a7a7a", "#f28b82",
           "#7bd88f", "#e6c95c", "#82b1ff", "#cf9fff", "#76e0e3",
           "#ffffff")


def _xterm_colour(n: int) -> str:
    if 0 <= n < 16:
        return _ANSI16[n]
    if 16 <= n < 232:
        n -= 16
        parts = (n // 36, (n // 6) % 6, n % 6)
        return "#%02x%02x%02x" % tuple(
            0 if v == 0 else 55 + v * 40 for v in parts)
    if 232 <= n < 256:
        grey = 8 + (n - 232) * 10
        return "#%02x%02x%02x" % (grey, grey, grey)
    return ""


def _sgr_apply(state: dict, codes: list[int]) -> None:
    at = 0
    while at < len(codes):
        code = codes[at]
        if code == 0:
            state.clear()
        elif code == 1:
            state["bold"] = True
        elif code == 2:
            state["dim"] = True
        elif code == 3:
            state["italic"] = True
        elif code == 4:
            state["underline"] = True
        elif code == 7:
            state["reverse"] = True
        elif code in (21, 22):
            state.pop("bold", None), state.pop("dim", None)
        elif code == 23:
            state.pop("italic", None)
        elif code == 24:
            state.pop("underline", None)
        elif code == 27:
            state.pop("reverse", None)
        elif 30 <= code <= 37:
            state["fg"] = _ANSI16[code - 30]
        elif code == 39:
            state.pop("fg", None)
        elif 40 <= code <= 47:
            state["bg"] = _ANSI16[code - 40]
        elif code == 49:
            state.pop("bg", None)
        elif 90 <= code <= 97:
            state["fg"] = _ANSI16[code - 90 + 8]
        elif 100 <= code <= 107:
            state["bg"] = _ANSI16[code - 100 + 8]
        elif code in (38, 48) and at + 1 < len(codes):
            which = "fg" if code == 38 else "bg"
            if codes[at + 1] == 5 and at + 2 < len(codes):
                colour = _xterm_colour(codes[at + 2])
                if colour:
                    state[which] = colour
                at += 2
            elif codes[at + 1] == 2 and at + 4 < len(codes):
                state[which] = "#%02x%02x%02x" % tuple(
                    max(0, min(255, v)) for v in codes[at + 2:at + 5])
                at += 4
        at += 1


def _sgr_style(state: dict) -> str:
    fg, bg = state.get("fg", ""), state.get("bg", "")
    if state.get("reverse"):
        fg, bg = (bg or "var(--bg)"), (fg or "var(--fg)")
    style = []
    if fg:
        style.append(f"color:{fg}")
    if bg:
        style.append(f"background:{bg}")
    if state.get("bold"):
        style.append("font-weight:600")
    if state.get("dim"):
        style.append("opacity:.6")
    if state.get("italic"):
        style.append("font-style:italic")
    if state.get("underline"):
        style.append("text-decoration:underline")
    return ";".join(style)


def ansi_html(text: str) -> str:
    """tmux's pane capture, colours kept, as safe HTML."""
    out, state, at = [], {}, 0
    for match in _SGR.finditer(text):
        _emit_span(out, state, text[at:match.start()])
        _sgr_apply(state, [int(n or 0)
                           for n in (match.group(1) or "0").split(";")])
        at = match.end()
    _emit_span(out, state, text[at:])
    return "".join(out)


def _emit_span(out: list, state: dict, raw: str) -> None:
    if not raw:
        return
    plain = html_mod.escape(_OTHER_ESC.sub("", raw))
    if not plain:
        return
    style = _sgr_style(state)
    out.append(f'<span style="{style}">{plain}</span>' if style else plain)


_TERM_KEYS = {"Enter", "Escape", "Tab", "BTab", "BSpace", "DC",
              "Up", "Down", "Left", "Right", "PageUp", "PageDown",
              "Home", "End",
              "C-a", "C-b", "C-c", "C-d", "C-e", "C-k", "C-l", "C-n",
              "C-o", "C-p", "C-r", "C-t", "C-u", "C-w", "C-z"}


def _as_int(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def drops_folder(home) -> Path:
    """Where a file dropped on the Boss window is kept.

    One folder for both ways in: the page posts bytes it has no path
    for, and the app's window saves a picture dragged out of another
    app. A box with no conductor home still takes drops.
    """
    if home:
        return Path(home) / "drops"
    return Path(tempfile.gettempdir()) / "voice-agent-drops"


def _drop_name(name: str) -> str:
    """A dropped file's name, made safe to join onto a folder."""
    stem = os.path.basename(str(name or "").replace("\\", "/")).strip()
    kept = "".join(c if (c.isalnum() or c in "._- ") else "_" for c in stem)
    kept = kept.strip(". ")[:120]
    return kept or "dropped"


def _keep_file(folder: Path, name: str, raw: bytes) -> Path:
    """Write the bytes under `name`, never over a file already there."""
    target = folder / name
    if target.exists():
        stem, dot, suffix = name.partition(".")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = folder / f"{stem}-{stamp}{dot}{suffix}"
        n = 1
        while target.exists():
            target = folder / f"{stem}-{stamp}-{n}{dot}{suffix}"
            n += 1
    target.write_bytes(raw)
    return target


def _safe_task_id(task_id: str) -> bool:
    return bool(task_id) and len(task_id) <= 64 and \
        all(c.isalnum() or c in "_-" for c in task_id)


def _history_rows(home, task_id: str) -> list[dict]:
    """The worker's transcript as rows the page can draw. Never raises:
    a session with no readable history simply has none, and the live
    screen still opens."""
    try:
        from .worker_history import lines
        return [{"kind": kind, "text": text}
                for kind, text in lines(home, task_id)]
    except Exception:
        application_log("ui", "app_web.history_failed",
                        f"could not read {task_id}'s history",
                        severity="debug", exc_info=True, task_id=task_id)
        return []


def _capture_pane(task_id: str) -> tuple[str, bool]:
    """The worker's tmux pane with its scrollback, as HTML.

    A failed capture is not a dead session: tmux answers slowly or not
    at all under load, and only its own word that the session does not
    exist counts as ended."""
    name = session_name(task_id)
    try:
        # -S -2000: the pane's history too, not just the visible screen -
        # with only the screen there was nothing to scroll into, and the
        # view's scrollbar was dead (measured 2026-09-01).
        done = subprocess.run(["tmux", "capture-pane", "-e", "-p",
                               "-S", "-2000", "-t", name],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return "", True
    if done.returncode != 0:
        try:
            there = subprocess.run(["tmux", "has-session", "-t",
                                    "=" + name],
                                   capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return "", True
        if there.returncode == 0:
            return "", True
        return _final_screen(task_id)
    return ansi_html(done.stdout), True


def _stream_chunk(task_id: str, start: int) -> dict:
    """Bytes `start:` of the worker's raw output stream, base64, with
    where to resume and whether the session still runs.

    The stream (tmux_runtime.stream_path, piped from launch) is what a
    standalone terminal would have been fed. An attached client only
    gets tmux's repaints from attach-time on, and tmux keeps no pane
    history for a TUI that repaints in place - so this file is the only
    scrollback the emulator can seed itself with (2026-09-01)."""
    name = session_name(task_id)
    path = stream_path(name)
    try:
        size = path.stat().st_size
    except OSError:
        return {"b64": "", "next": 0, "have": False, "alive": False}
    start = max(0, min(int(start or 0), size))
    data = b""
    if size > start:
        with open(path, "rb") as handle:
            handle.seek(start)
            data = handle.read(512 * 1024)
    try:
        alive = subprocess.run(["tmux", "has-session", "-t", name],
                               capture_output=True, timeout=5
                               ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        alive = False
    return {"b64": base64.b64encode(data).decode(), "have": True,
            "next": start + len(data), "alive": alive}


def _final_screen(task_id: str) -> tuple[str, bool]:
    """A finished worker's saved last screen, if the runtime left one.

    The pane is killed when a task completes or is cancelled; the
    runtime saves its final capture on the way out, so the card still
    opens onto what the worker did rather than onto "The session has
    ended." over nothing (measured 2026-09-01)."""
    try:
        text = final_screen_path(session_name(task_id)).read_text()
    except OSError:
        return "", False
    return ansi_html(text), False


def _send_to_pane(task_id: str, text: str, key: str) -> bool:
    """One keystroke (or a run of typed characters) into the worker's
    tmux pane - the same pane, the same session, no copy."""
    argv = ["tmux", "send-keys", "-t", session_name(task_id)]
    if text:
        argv += ["-l", "--", text]
    elif key in _TERM_KEYS:
        argv.append(key)
    else:
        return False
    try:
        done = subprocess.run(argv, capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class TermStreams:
    """Live worker terminals the way cmux draws its panes: one real tmux
    client on a PTY per open pane, its bytes streamed to the page's
    terminal emulator, keys and resizes written straight back. The pane
    is the session itself - scrollback, colours, cursor and mouse are
    the terminal's own, not a periodic capture-pane screenshot."""

    def __init__(self, send) -> None:
        self._send = send               # one message to every open page
        self._open: dict[str, tuple[int, asyncio.subprocess.Process]] = {}

    async def attach(self, task_id: str, cols: int, rows: int) -> bool:
        """A tmux client for this worker's session, sized to the page."""
        if not _safe_task_id(task_id):
            return False
        await self.detach(task_id)
        name = session_name(task_id)
        probe = await asyncio.create_subprocess_exec(
            "tmux", "has-session", "-t", name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        if await probe.wait() != 0:
            return False
        master, slave = os.openpty()
        cols = max(20, min(500, cols or 80))
        rows = max(5, min(300, rows or 24))
        try:
            _set_winsize(master, cols, rows)
            process = await asyncio.create_subprocess_exec(
                "tmux", "attach-session", "-t", name,
                stdin=slave, stdout=slave, stderr=slave,
                env=dict(os.environ, TERM="xterm-256color"),
                start_new_session=True)
        except OSError:
            application_log("ui", "term_stream.attach_failed",
                            f"could not attach a client to {name}",
                            severity="error", exc_info=True,
                            task_id=task_id)
            os.close(master)
            os.close(slave)
            return False
        os.close(slave)
        self._open[task_id] = (master, process)
        asyncio.get_running_loop().add_reader(
            master, self._readable, task_id, master)
        asyncio.ensure_future(self._reap(task_id, master, process))
        application_log("ui", "term_stream.attached",
                        f"a live client is on {name}", severity="debug",
                        task_id=task_id, data={"cols": cols, "rows": rows})
        return True

    def _readable(self, task_id: str, master: int) -> None:
        try:
            data = os.read(master, 65536)
        except OSError:
            data = b""
        if not data:
            asyncio.ensure_future(self._ended(task_id, master))
            return
        self._send({"kind": "term_data", "task_id": task_id,
                    "data": base64.b64encode(data).decode()})

    async def _reap(self, task_id: str, master: int,
                    process: asyncio.subprocess.Process) -> None:
        await process.wait()
        await self._ended(task_id, master)

    async def _ended(self, task_id: str, master: int) -> None:
        """The client ended (the session died, or tmux detached us):
        the page's pane is told, once, if this attach is still current.
        Both the PTY's EOF and the process's exit land here; whichever
        comes first takes the entry and speaks."""
        if self._open.get(task_id, (None,))[0] != master:
            return
        await self.detach(task_id, master=master)
        self._send({"kind": "term_exit", "task_id": task_id})

    def write(self, task_id: str, data: bytes) -> bool:
        entry = self._open.get(task_id)
        if entry is None:
            return False
        try:
            os.write(entry[0], data)
        except OSError:
            return False
        return True

    def resize(self, task_id: str, cols: int, rows: int) -> bool:
        entry = self._open.get(task_id)
        if entry is None:
            return False
        master, process = entry
        try:
            _set_winsize(master, max(20, min(500, cols or 80)),
                         max(5, min(300, rows or 24)))
            process.send_signal(signal.SIGWINCH)
        except (OSError, ProcessLookupError):
            return False
        return True

    async def detach(self, task_id: str, master: int | None = None) -> None:
        entry = self._open.get(task_id)
        if entry is None or (master is not None and entry[0] != master):
            return
        del self._open[task_id]
        fd, process = entry
        try:
            asyncio.get_running_loop().remove_reader(fd)
        except (OSError, ValueError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass

    async def close(self) -> None:
        for task_id in list(self._open):
            await self.detach(task_id)


def _cmux_in_front() -> bool:
    front = subprocess.run(["lsappinfo", "front"], capture_output=True,
                           text=True).stdout.strip()
    if not front:
        return False
    info = subprocess.run(["lsappinfo", "info", "-only", "bundleid", front],
                          capture_output=True, text=True).stdout
    return "com.cmuxterm.app" in info


async def _focus_with_cmux(task_id: str) -> None:
    """Bring a worker's window forward - one cmux call, no conductor.

    Focusing is best effort all the way down (_bring_forward swallows
    its failures so a click can never hurt the worker), and the very
    first focus can be spent entirely on macOS's automation-permission
    prompt - granted, but the window never moved. So the result is
    checked: if cmux is not in front, one more try.
    """
    from .tmux_runtime import final_screen_path, session_name, stream_path
    runtime = _cmux_runtime()
    name = session_name(task_id)
    await asyncio.to_thread(runtime._bring_forward, name)
    if not await asyncio.to_thread(_cmux_in_front):
        await asyncio.sleep(0.5)
        await asyncio.to_thread(runtime._bring_forward, name)


class WindowBridge:
    """The Boss window over the native bridge: the same page, loaded
    into the WKWebView from a file, talking over the spawned process's
    stdio. No HTTP, no port (2026-09-01, "stay native").

    Out: {"js": ...} to run in the page, {"raise": true} to front the
    window. In: one JSON line per script message - {"path", "body",
    "id"?}; "/ready" subscribes the page and replays history."""

    def __init__(self, web: CodexWeb) -> None:
        self.web = web
        self.process: asyncio.subprocess.Process | None = None
        self._queue: asyncio.Queue | None = None
        self._reader: asyncio.Task | None = None
        self._pump: asyncio.Task | None = None

    async def start(self, uv: str, script, page_dir) -> None:
        page = Path(page_dir) / "boss_window.html"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(PAGE)
        assets = page.parent / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        for name in ASSET_FILES:
            shutil.copyfile(ASSETS / name, assets / name)
        self.process = await asyncio.create_subprocess_exec(
            uv, "run", "--script", str(script), "--stdio",
            "--page", str(page),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        self._reader = asyncio.create_task(self.serve(self.process.stdout))

    async def serve(self, reader) -> None:
        """Every line the window sends, until it closes."""
        while True:
            line = await reader.readline()
            if not line:
                return
            try:
                envelope = json.loads(line)
            except ValueError:
                continue
            try:
                await self._call(envelope)
            except Exception:
                application_log("ui", "window_bridge.call_failed",
                                "a page request failed",
                                severity="warning", exc_info=True)

    async def _call(self, envelope: dict) -> None:
        path = str(envelope.get("path") or "")
        if path == "/ready":
            # The one beacon that proves the whole outward leg: the
            # file loaded, the page went NATIVE, and a script message
            # crossed the bridge.
            application_log("ui", "window_bridge.page_ready",
                            "the window's page loaded and spoke",
                            severity="debug")
            self._attach()
            return
        if path == "/raised":
            application_log("ui", "window_bridge.window_raised",
                            "the raise envelope reached the window",
                            severity="debug")
            return
        if path == "/jserror":
            body = envelope.get("body") or {}
            application_log("ui", "window_bridge.page_error",
                            f"the page threw: {str(body.get('m'))[:200]} "
                            f"(line {body.get('line')})",
                            severity="warning")
            return
        data = await self.handle(path, envelope.get("body") or {})
        if envelope.get("id") is not None:
            self._js(f"__reply({json.dumps(envelope['id'])}, "
                     f"{json.dumps(data)})")

    def _attach(self) -> None:
        """The page loaded: replay history, then follow the stream."""
        self.web._restore_history()
        if self._queue is None:
            self._queue = asyncio.Queue()
            self.web._watchers.append(self._queue)
            self._pump = asyncio.create_task(self._pump_events())
        for message in self.web.history:
            self.deliver(message)

    async def _pump_events(self) -> None:
        while True:
            message = await self._queue.get()
            if message is None:
                return
            self.deliver(message)

    def deliver(self, message: dict) -> None:
        self._js(f"__deliver({json.dumps(message)})")

    def raise_window(self) -> None:
        """Front the window - the process that owns it, no pid games."""
        self._send({"raise": True})

    def open_task(self, task_id: str, title: str = "") -> None:
        """A notification's link: this window forward, that session
        open - not a cmux workspace (asked 2026-09-01)."""
        self.raise_window()
        self.deliver({"kind": "open_term", "task_id": task_id,
                      "title": title})

    def _js(self, js: str) -> None:
        self._send({"js": js})

    def _send(self, envelope: dict) -> None:
        if self.process is None or self.process.stdin is None:
            return
        try:
            self.process.stdin.write((json.dumps(envelope) + "\n").encode())
        except Exception:
            pass

    async def handle(self, path: str, body: dict) -> dict:
        """Every door _client answers over HTTP, as calls.

        Two tables for one page is a trap: /history reached the HTTP
        router and not this one, so a worker's scrollback was empty in
        the app and full in a browser. The test below walks the page's
        own calls and asks this method about each, so a door added to
        one side cannot be forgotten on the other."""
        web = self.web
        if path == "/say":
            said = str(body.get("text") or "").strip()
            if said:
                web._prompts.put_nowait(said)
            return {"ok": True}
        if path == "/answer":
            return {"ok": web.answer(str(body.get("item_id") or ""),
                                     str(body.get("decision") or ""))}
        if path == "/interrupt":
            try:
                await web.app.interrupt()
            except Exception as exc:
                return {"ok": False, "error": str(exc)[:200]}
            return {"ok": True}
        if path == "/threads":
            return {"threads": web.threads()}
        if path == "/thread":
            if web.busy:
                return {"ok": False, "error": "a turn is running"}
            try:
                await web.switch_thread(str(body.get("id") or "") or None)
            except Exception as exc:
                return {"ok": False, "error": str(exc)[:200]}
            return {"ok": True}
        if path == "/sessions":
            rows = await asyncio.to_thread(web.sessions)
            return {"rows": rows, "hidden_running": web.hidden_running}
        if path == "/remove":
            return await asyncio.to_thread(
                web.remove_session, str(body.get("task_id") or ""),
                bool(body.get("confirmed")))
        if path == "/drop":
            # The browser's way in: bytes and a name, no path. The app's
            # own window drops by path and never comes here - but the
            # page is the same page in both, so the door has to be.
            return await self._drop(body)
        if path.startswith("/stream/"):
            rest = path[len("/stream/"):].strip("/")
            task_id, _, start = rest.partition("/")
            if not _safe_task_id(task_id):
                return {"b64": "", "next": 0, "have": False,
                        "alive": False}
            return await asyncio.to_thread(
                _stream_chunk, task_id,
                int(start) if start.isdigit() else 0)
        if path.startswith("/history/"):
            # The worker's past. #170 added it to the HTTP router only,
            # so the native window - the one that ships - had nothing to
            # scroll ("the working agent scroll hasnt been fixed yet").
            task_id = path[len("/history/"):].strip("/")
            if not _safe_task_id(task_id) or web.home is None:
                return {"rows": []}
            return {"rows": await asyncio.to_thread(_history_rows, web.home,
                                                    task_id)}
        if path.startswith("/term/"):
            task_id = path[len("/term/"):].strip("/")
            if not _safe_task_id(task_id):
                return {"html": "", "alive": False}
            screen, alive = await asyncio.to_thread(_capture_pane, task_id)
            return {"html": screen, "alive": alive}
        if path == "/key":
            task_id = str(body.get("task_id") or "")
            if not _safe_task_id(task_id):
                return {"ok": False}
            return {"ok": await asyncio.to_thread(
                _send_to_pane, task_id, str(body.get("text") or ""),
                str(body.get("key") or ""))}
        if path in ("/attach", "/detach", "/stdin", "/resize"):
            return await web._term_door(path, body)
        return {"error": f"nothing at {path}"}

    async def _drop(self, body: dict) -> dict:
        """/drop, as a call rather than a request."""
        name = _drop_name(str(body.get("name") or ""))
        try:
            raw = base64.b64decode(str(body.get("data") or ""), validate=True)
        except (ValueError, TypeError):
            raw = b""
        if not raw:
            return {"ok": False, "error": "the file did not arrive"}
        folder = self.web.drops
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target = await asyncio.to_thread(_keep_file, folder, name, raw)
        except OSError as exc:
            application_log("ui", "window_bridge.drop_failed",
                            "a dropped file could not be kept",
                            severity="warning", exc_info=True)
            return {"ok": False, "error": str(exc)[:200]}
        return {"ok": True, "path": str(target)}

    async def stop(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
        if self._queue is not None and self._queue in self.web._watchers:
            self.web._watchers.remove(self._queue)
            self._queue = None
        if self.process is not None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            self.process = None


class BossApp:
    """The voice's own Boss, behind the page's backend seam.

    The page (CodexWeb) needs what CodexApp gives it: a thread id, turns
    that yield events, an interrupt. Here a typed turn goes to the same
    Boss the voice talks to - conductor.handle_user_message - and the
    reply comes back as one turn_done event. Spoken turns never pass
    through here: the voice runs them itself and the page mirrors them
    (mirror_prompt / mirror_answer).
    """

    def __init__(self, conductor) -> None:
        self.conductor = conductor
        self.model = "boss"

    @property
    def thread_id(self) -> str:
        return getattr(self.conductor, "current_boss_session_id",
                       None) or "boss"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def start_thread(self, **options) -> None:
        """New chat: a distinct conversation; the next turn opens a
        fresh Boss. The old conversation and its workers stay on disk."""
        self.conductor.new_conversation()
        self._rebind()

    async def resume_thread(self, thread_id: str) -> None:
        """An old chat clicked in the sidebar: its conversation becomes
        current again and the next turn resumes that Boss."""
        store = getattr(self.conductor, "boss_store", None)
        record = store.get(thread_id) if store is not None else None
        if record is None:
            raise KeyError(thread_id)
        store.resume_conversation(record.conversation_id)
        self._rebind()

    def _rebind(self) -> None:
        manager = getattr(self.conductor, "manager", None)
        if hasattr(manager, "rebind"):
            manager.rebind()

    async def interrupt(self) -> None:
        pass                     # a Boss turn is not codex's to cancel

    async def turn(self, text: str):
        turn = await self.conductor.handle_user_message(text, source="text")
        reply = "" if getattr(turn, "folded", False) \
            else (turn.reply or "Done.")
        yield Event(kind="turn_done", text=reply)


def make_backend(cwd: str, backend: str = "codex"):
    """The selectable Boss: `codex` speaks to a codex app-server,
    `claude` to Claude Code. Same Events either way; same page."""
    if backend == "claude":
        from .claude_app import ClaudeApp
        return ClaudeApp(cwd=cwd, on_approval=lambda a: None), {}
    return CodexApp(cwd=cwd, on_approval=lambda a: None), \
        {"approval_policy": "untrusted", "sandbox": {"type": "readOnly"}}


async def serve(cwd: str, home, port: int = 0,
                open_page: bool = True, backend: str = "codex") -> int:
    """The page, until interrupted. Not the Boss - see the docstring."""
    app, thread_options = make_backend(cwd, backend)
    try:
        await app.start()
    except Exception as exc:
        print(f"the {backend} backend is not available: {exc}",
              file=sys.stderr)
        return 1
    web = CodexWeb(app, home=home, focus=_focus_with_cmux)
    web.thread_options = thread_options
    try:
        await app.start_thread(**web.thread_options)
        await web.start(port)
        print(f"talking to {backend} at {web.url}", flush=True)
        if open_page:
            webbrowser.open(web.url)
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await web.stop()
        await app.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    from pathlib import Path
    home = Path.home() / ".voice-conductor"
    backend = "codex"
    if "--claude" in argv:
        argv = [a for a in argv if a != "--claude"]
        backend = "claude"
    if "--codex" in argv:
        argv = [a for a in argv if a != "--codex"]
    cwd = argv[0] if argv else os.getcwd()
    try:
        return asyncio.run(serve(cwd, home, backend=backend))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
