#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pyobjc-framework-Cocoa>=10,<13",
#   "pyobjc-framework-WebKit>=10,<13",
# ]
# ///
# The window needs AppKit and nothing else: the claude-agent-sdk pulled
# mcp -> pyjwt[crypto] -> cryptography, whose fresh resolve needs a Rust
# build that fails on this machine (measured 2026-09-01: the probe's
# window died in maturin before drawing). The standalone mode imports
# the heavy half lazily and says so if it is missing.
"""The Codex conversation as a macOS app.

One native window - resizable, remembered between launches, no title
bar, just the traffic lights over the page - whose content is the same
page CodexWeb serves to a browser. The page hides its own header inside
the app (?app=1). AppKit owns the main thread; the codex app-server and
the loopback page live on an asyncio loop in a background thread, the
same split conduct.py makes for the overlay.

Run it from the repo root:

    uv run --script conductor/app_mac.py [cwd]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ""):                    # run as a uv script
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import objc
from AppKit import (NSApplication, NSApplicationActivationPolicyRegular,
                    NSBackingStoreBuffered, NSBitmapImageFileTypePNG,
                    NSBitmapImageRep, NSColor, NSDragOperationCopy,
                    NSDragOperationNone, NSMakeRect, NSMenu,
                    NSMenuItem, NSObject, NSPasteboardTypeFileURL,
                    NSPasteboardTypePNG, NSPasteboardTypeTIFF,
                    NSPasteboardURLReadingFileURLsOnlyKey,
                    NSScreen, NSView, NSWindow,
                    NSWindowStyleMaskClosable,
                    NSWindowStyleMaskFullSizeContentView,
                    NSWindowStyleMaskMiniaturizable,
                    NSWindowStyleMaskResizable, NSWindowStyleMaskTitled,
                    NSWindowTitleHidden)
from Foundation import NSURL, NSURLRequest, NSSize
from WebKit import WKWebView, WKWebViewConfiguration

WIDTH, HEIGHT = 760, 820


class Backend:
    """The codex side, on its own loop so AppKit can have the main thread."""

    def __init__(self, cwd: str, home: Path, kind: str = "codex") -> None:
        self.cwd = cwd
        self.home = home
        self.kind = kind
        self.url = ""
        self.error = ""
        self.ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        threading.Thread(target=lambda: asyncio.run(self._serve()),
                         daemon=True).start()

    def shutdown(self) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        try:                        # standalone mode's heavy half, lazily
            from conductor.app_web import (CodexWeb, _focus_with_cmux,
                                             make_backend)
        except Exception as exc:
            self.error = f"the standalone backend is unavailable: {exc}"
            self.ready.set()
            return
        app, thread_options = make_backend(self.cwd, self.kind)
        try:
            await app.start()
        except Exception as exc:
            self.error = str(exc)
            self.ready.set()
            return
        web = CodexWeb(app, home=self.home, focus=_focus_with_cmux)
        web.thread_options = thread_options
        try:
            await app.start_thread(**thread_options)
            await web.start()
            self.url = web.url + "?app=1"
            self.ready.set()
            await self._stop.wait()
        finally:
            self.ready.set()
            await web.stop()
            await app.stop()


class RemoteBackend:
    """A page someone else serves - the voice conductor's - just shown.

    The window owns nothing then: closing it closes a view, never the
    Boss, and the conductor keeps its page for the next window.
    """

    quiet = True          # the conductor raises the window on the first turn

    def __init__(self, url: str) -> None:
        self.url = url
        self.error = ""
        self.ready = threading.Event()
        self.ready.set()

    def start(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


class BridgeBackend:
    """The conductor feeds this window over stdio; the page is a file.

    No server, no port: the WKWebView's script-message bridge carries
    the page's requests out on stdout, and the conductor's answers and
    events come back on stdin as JavaScript to run. The window owns
    nothing - closing it closes a view (2026-09-01, "stay native")."""

    quiet = True          # the conductor raises the window when it wants to

    def __init__(self, page_path: str) -> None:
        self.page_path = page_path
        self.url = "file"                    # Delegate loads the file itself
        self.error = ""
        self.ready = threading.Event()
        self.ready.set()

    def start(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


class BridgeSink(NSObject):
    """Page -> conductor: each script message is one NDJSON line out."""

    def userContentController_didReceiveScriptMessage_(self, ucc,
                                                       message) -> None:
        try:
            sys.stdout.write(str(message.body()) + "\n")
            sys.stdout.flush()
        except Exception:
            pass


def stdin_pump(view, window) -> None:
    """Conductor -> page: envelopes on stdin, JavaScript into the view.

    {"js": "..."} runs in the page (replies, delivered events);
    {"raise": true} brings the window forward - the conductor asks for
    this instead of guessing pids with System Events. EOF means the
    conductor is gone, and a view of nothing closes itself."""
    from Foundation import NSOperationQueue

    def on_main(work) -> None:
        NSOperationQueue.mainQueue().addOperationWithBlock_(work)

    for line in sys.stdin:
        try:
            envelope = json.loads(line)
        except ValueError:
            continue

        def act(envelope=envelope) -> None:
            if envelope.get("raise"):
                window.makeKeyAndOrderFront_(None)
                NSApplication.sharedApplication() \
                    .activateIgnoringOtherApps_(True)
                try:                     # the ack is the proof it landed
                    sys.stdout.write('{"path": "/raised"}\n')
                    sys.stdout.flush()
                except Exception:
                    pass
            js = envelope.get("js")
            if js:
                view.evaluateJavaScript_completionHandler_(js, None)
        on_main(act)
    on_main(lambda: NSApplication.sharedApplication().terminate_(None))


class Delegate(NSObject):
    """The window and its web view; quits when the window goes."""

    def initWithBackend_(self, backend):
        self = objc_super_init(self)
        if self is None:
            return None
        self.backend = backend
        self.window = None
        return self

    def applicationDidFinishLaunching_(self, note) -> None:
        screen = NSScreen.mainScreen().visibleFrame()
        rect = NSMakeRect(screen.origin.x + (screen.size.width - WIDTH) / 2,
                          screen.origin.y + (screen.size.height - HEIGHT) / 2,
                          WIDTH, HEIGHT)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                 NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable |
                 NSWindowStyleMaskFullSizeContentView)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False)
        self.window.setTitle_("voice-agent")   # Mission Control, the Dock
        self.window.setTitleVisibility_(NSWindowTitleHidden)
        self.window.setMinSize_(NSSize(430, 480))
        self.window.setFrameAutosaveName_("CodexConversation")
        self.window.setTitlebarAppearsTransparent_(True)
        self.window.setBackgroundColor_(NSColor.textBackgroundColor())
        config = WKWebViewConfiguration.new()
        page_path = getattr(self.backend, "page_path", "")
        if page_path:
            self.sink = BridgeSink.alloc().init()
            config.userContentController().addScriptMessageHandler_name_(
                self.sink, "bridge")
        # DropWebView, not WKWebView: WebKit will not give a page the
        # path of a dropped file, so the view takes the drop itself and
        # hands the page the paths (see the class). It keeps the bridge
        # configuration - the window is the native app now, and every
        # call the page makes goes through it.
        view = DropWebView.alloc().initWithFrame_configuration_(
            self.window.contentView().bounds(), config)
        view.setAutoresizingMask_(18)            # width | height sizable
        try:                                     # no white flash before load
            view.setValue_forKey_(False, "drawsBackground")
        except Exception:
            pass
        try:                                     # macOS 12+: overscroll area
            view.setUnderPageBackgroundColor_(NSColor.textBackgroundColor())
        except AttributeError:
            pass
        self.window.contentView().addSubview_(view)
        bounds = self.window.contentView().bounds()
        grab = DragStrip.alloc().initWithFrame_over_(NSMakeRect(
            0, bounds.size.height - 28, bounds.size.width, 28), view)
        grab.setAutoresizingMask_(10)            # width sizable | minY margin
        self.window.contentView().addSubview_(grab)
        self.window.setDelegate_(self)
        self.window.makeKeyAndOrderFront_(None)
        if not getattr(self.backend, "quiet", False):
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        if page_path:
            here = NSURL.fileURLWithPath_(str(Path(page_path).parent))
            view.loadFileURL_allowingReadAccessToURL_(
                NSURL.fileURLWithPath_(page_path), here)
            threading.Thread(target=stdin_pump, args=(view, self.window),
                             daemon=True).start()
            return
        self.backend.ready.wait(timeout=30)
        if self.backend.error or not self.backend.url:
            alert_and_quit(self.backend.error or
                           "the Boss backend did not start")
            return
        view.loadRequest_(NSURLRequest.requestWithURL_(
            NSURL.URLWithString_(self.backend.url)))

    def windowShouldClose_(self, sender) -> bool:
        """In bridge mode the x hides: the conductor keeps running, and
        quitting here killed the only view of it - "if i x out of the
        app its hard for me to find it again" (2026-09-01). The Dock
        icon, a voice ask or a notification link brings it back.
        Standalone, closing the window still quits, rightly."""
        if getattr(self.backend, "page_path", ""):
            self.window.orderOut_(None)
            return False
        return True

    def applicationShouldHandleReopen_hasVisibleWindows_(
            self, app, has_visible) -> bool:
        if not has_visible and self.window is not None:
            self.window.makeKeyAndOrderFront_(None)   # the Dock way back
        return True

    def applicationShouldTerminateAfterLastWindowClosed_(self, app) -> bool:
        return not getattr(self.backend, "page_path", "")

    def applicationWillTerminate_(self, note) -> None:
        self.backend.shutdown()


class DropWebView(WKWebView):
    """A file dragged onto the window lands in the box as its path.

    WebKit keeps a dropped file's path from the page - the page is only
    handed the bytes - and left to itself it leaves the conversation
    for the file, which is what "drag and drop does not work" looked
    like. The window takes the drag first: real paths come off the
    pasteboard, a picture dragged out of another app (no file behind
    it) is written to one, and the page's own insertPaths puts them in
    the box, the way Claude Code takes a dragged file. Anything that is
    not a file is WebKit's again - dragged text still drops into the
    page.
    """

    def initWithFrame_configuration_(self, frame, configuration):
        self = objc.super(DropWebView, self).initWithFrame_configuration_(
            frame, configuration)
        if self is not None:
            wanted = [NSPasteboardTypeFileURL, NSPasteboardTypePNG,
                      NSPasteboardTypeTIFF]
            types = list(self.registeredDraggedTypes() or [])
            self.registerForDraggedTypes_(
                types + [t for t in wanted if t not in types])
        return self

    def draggingEntered_(self, info):
        if _dropped_files(info.draggingPasteboard()):
            _light(self, True)
            return NSDragOperationCopy
        return _webkits(self, "draggingEntered_", info, NSDragOperationNone)

    def draggingUpdated_(self, info):
        if _dropped_files(info.draggingPasteboard()):
            return NSDragOperationCopy
        return _webkits(self, "draggingUpdated_", info, NSDragOperationNone)

    def draggingExited_(self, info) -> None:
        _light(self, False)
        _webkits(self, "draggingExited_", info, None)

    def draggingEnded_(self, info) -> None:
        _light(self, False)
        _webkits(self, "draggingEnded_", info, None)

    def prepareForDragOperation_(self, info):
        if _dropped_files(info.draggingPasteboard()):
            return True
        return _webkits(self, "prepareForDragOperation_", info, False)

    def performDragOperation_(self, info):
        board = info.draggingPasteboard()
        if not _dropped_files(board):
            return _webkits(self, "performDragOperation_", info, False)
        _light(self, False)
        paths = _paths_on(board)
        if not paths:
            return False
        self.evaluateJavaScript_completionHandler_(
            _insert_script(paths), None)
        return True


def _insert_script(paths: list[str]) -> str:
    """The line the page runs with the dropped paths. A window whose
    page has not loaded yet does nothing rather than raising."""
    return "window.insertPaths && insertPaths(%s)" % json.dumps(paths)


def _light(view, on: bool) -> None:
    """The box says it will take the file, while one is over the window."""
    view.evaluateJavaScript_completionHandler_(
        "window.showDrop && showDrop(%s)" % ("true" if on else "false"), None)


def _webkits(view, name, info, default):
    """Hand a drag we do not want back to WebKit - which does not
    implement every one of these, so a missing one is simply a no."""
    try:
        return getattr(objc.super(DropWebView, view), name)(info)
    except AttributeError:
        return default


def _dropped_files(board) -> bool:
    """Is this drag files or a picture, rather than text for the page?"""
    return bool(board.availableTypeFromArray_(
        [NSPasteboardTypeFileURL, NSPasteboardTypePNG,
         NSPasteboardTypeTIFF]))


def _paths_on(board, folder: Path | None = None) -> list[str]:
    """The dropped files' paths - saving a pasted-in picture to get one."""
    urls = board.readObjectsForClasses_options_(
        [NSURL], {NSPasteboardURLReadingFileURLsOnlyKey: True})
    paths = [str(url.path()) for url in (urls or [])
             if url.isFileURL() and url.path()]
    if paths:
        return paths
    kept = _keep_picture(board, folder)
    return [kept] if kept else []


def _keep_picture(board, folder: Path | None = None) -> str:
    """A picture with no file behind it, written into the drops folder."""
    data = board.dataForType_(NSPasteboardTypePNG)
    if data is None:
        tiff = board.dataForType_(NSPasteboardTypeTIFF)
        if tiff is None:
            return ""
        rep = NSBitmapImageRep.imageRepWithData_(tiff)
        if rep is None:
            return ""
        data = rep.representationUsingType_properties_(
            NSBitmapImageFileTypePNG, {})
        if data is None:
            return ""
    if folder is None:
        # Lazily, like the backend above: app_mac runs as a --script
        # with its own dependencies, and importing the web module at
        # module scope would drag them in before AppKit is up.
        from conductor.app_web import drops_folder
        folder = drops_folder(Path.home() / ".voice-conductor")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / time.strftime("dropped-%Y%m%d-%H%M%S.png")
        n = 1
        while target.exists():
            target = folder / time.strftime(f"dropped-%Y%m%d-%H%M%S-{n}.png")
            n += 1
        target.write_bytes(bytes(data))
    except OSError as exc:
        print(f"could not keep the dropped picture: {exc}", file=sys.stderr)
        return ""
    return str(target)


class DragStrip(NSView):
    """The top of the page moves the window, as a title bar would.

    It lies over the web view, so a file dropped on the top of the
    window would otherwise land on nothing - a dead band right where
    someone aiming at the window drops one. Every drag on it is the
    view's underneath.
    """

    def initWithFrame_over_(self, frame, view):
        self = objc.super(DragStrip, self).initWithFrame_(frame)
        if self is not None:
            self.view = view
            self.registerForDraggedTypes_(view.registeredDraggedTypes())
        return self

    def mouseDown_(self, event) -> None:
        self.window().performWindowDragWithEvent_(event)

    def draggingEntered_(self, info):
        return self.view.draggingEntered_(info)

    def draggingUpdated_(self, info):
        return self.view.draggingUpdated_(info)

    def draggingExited_(self, info) -> None:
        self.view.draggingExited_(info)

    def prepareForDragOperation_(self, info):
        return self.view.prepareForDragOperation_(info)

    def performDragOperation_(self, info):
        return self.view.performDragOperation_(info)


def objc_super_init(obj):
    return objc.super(Delegate, obj).init()


def alert_and_quit(message: str) -> None:
    from AppKit import NSAlert
    alert = NSAlert.alloc().init()
    alert.setMessageText_("voice-agent is not available")
    alert.setInformativeText_(message)
    alert.runModal()
    NSApplication.sharedApplication().terminate_(None)


def build_menu(app) -> None:
    """A real menu bar: Quit, and the Edit actions the web view needs."""
    bar = NSMenu.alloc().init()
    app_item = NSMenuItem.alloc().init()
    bar.addItem_(app_item)
    app_menu = NSMenu.alloc().init()
    app_menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Quit voice-agent", "terminate:", "q"))
    app_item.setSubmenu_(app_menu)
    edit_item = NSMenuItem.alloc().init()
    bar.addItem_(edit_item)
    edit = NSMenu.alloc().initWithTitle_("Edit")
    for title, action, key in (("Undo", "undo:", "z"),
                               ("Redo", "redo:", "Z"),
                               ("Cut", "cut:", "x"),
                               ("Copy", "copy:", "c"),
                               ("Paste", "paste:", "v"),
                               ("Select All", "selectAll:", "a")):
        edit.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, action, key))
    edit_item.setSubmenu_(edit)
    app.setMainMenu_(bar)


def call_it_voice_agent() -> None:
    """The menu bar names the process; a script's is "Python". Inside
    the app, Info.plist already names it."""
    from Foundation import NSBundle
    from conductor import app_bundle
    if app_bundle.inside_bundle():
        return
    info = NSBundle.mainBundle().infoDictionary()
    if info is not None:
        info["CFBundleName"] = "voice-agent"


def set_app_icon(app) -> None:
    """The Dock shows the app's own icon; a script's is the Python rocket."""
    from AppKit import NSImage
    path = Path(__file__).resolve().parent.parent / "assets" / "icon.png"
    image = NSImage.alloc().initWithContentsOfFile_(str(path))
    if image is not None:
        app.setApplicationIconImage_(image)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    kind = "codex"
    if "--claude" in argv:
        argv = [a for a in argv if a != "--claude"]
        kind = "claude"
    if "--codex" in argv:
        argv = [a for a in argv if a != "--codex"]
    url = ""
    if "--url" in argv:
        at = argv.index("--url")
        url = argv[at + 1] if at + 1 < len(argv) else ""
        argv = argv[:at] + argv[at + 2:]
    page = ""
    if "--page" in argv:
        at = argv.index("--page")
        page = argv[at + 1] if at + 1 < len(argv) else ""
        argv = argv[:at] + argv[at + 2:]
    if "--stdio" in argv:
        argv = [a for a in argv if a != "--stdio"]
    cwd = os.path.abspath(argv[0]) if argv else os.getcwd()
    backend = BridgeBackend(page) if page \
        else RemoteBackend(url) if url \
        else Backend(cwd, Path.home() / ".voice-conductor", kind)
    backend.start()
    call_it_voice_agent()
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    set_app_icon(app)
    build_menu(app)
    delegate = Delegate.alloc().initWithBackend_(backend)
    app.setDelegate_(delegate)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
