"""The first-run window: conductor/assets/setup.html in a native window.

    <app> -m conductor.app_setup_window --home ~/.voice-conductor

Exits 0 when the user presses Start (setup is recorded), 1 when they
close the window or quit. Everything the page asks for is handled by
app_setup.SetupController; this file only binds it to AppKit: the web
view and its script bridge, the timer that re-reads the grants, and the
system's own permission requests.

Started from the app, this process is the app to macOS - it is the
bundle's executable - so the prompts it raises name the app, and what
the user grants here is what the conductor holds afterwards.

For testing without a person: `--offscreen` never shows a window or
takes focus, `--snapshot PNG` writes the rendered page and exits,
`--js CODE` runs in the page once it has drawn, and `--fake-grants` /
`--fake-claude` replace the probes (and turn the system requests into
lines on stdout), so no prompt can appear.

`--alert TITLE [--detail TEXT]` is the app's other window: a plain alert
for when it cannot start at all.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from AppKit import (NSAlert, NSApplication,
                    NSApplicationActivationPolicyAccessory,
                    NSApplicationActivationPolicyRegular,
                    NSBackingStoreBuffered, NSBitmapImageFileTypePNG,
                    NSBitmapImageRep, NSMakeRect, NSMenu, NSMenuItem,
                    NSObject, NSWindow, NSWindowStyleMaskClosable,
                    NSWindowStyleMaskMiniaturizable,
                    NSWindowStyleMaskTitled, NSWorkspace)
from Foundation import NSURL
from PyObjCTools import AppHelper
from WebKit import WKWebView, WKWebViewConfiguration

from conductor import app_bundle, app_setup

PAGE = Path(__file__).resolve().parent / "assets" / "setup.html"
WIDTH, HEIGHT = 620, 760
POLL_S = 3.0


def request_grant(grant_id: str) -> bool:
    """Raise macOS's own prompt for one grant. False when there is no
    prompt left to raise - the answer was given before, and only the
    Settings pane can change it."""
    if grant_id == "microphone":
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
        if AVCaptureDevice.authorizationStatusForMediaType_(
                AVMediaTypeAudio) != 0:
            return False
        AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVMediaTypeAudio, lambda granted: None)
        return True
    if grant_id == "input_monitoring":
        import Quartz
        return bool(Quartz.CGRequestListenEventAccess())
    if grant_id == "screen_recording":
        import Quartz
        return bool(Quartz.CGRequestScreenCaptureAccess())
    if grant_id == "accessibility":
        from ApplicationServices import (AXIsProcessTrustedWithOptions,
                                         kAXTrustedCheckOptionPrompt)
        AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
        return True
    return False


def emit(**event) -> None:
    """A line on stdout for whoever started the window (tests, mostly)."""
    try:
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


class ScriptSink(NSObject):
    """Page -> controller: each posted message, handled off the main
    thread so a slow handler never stalls the window."""

    controller = None

    def userContentController_didReceiveScriptMessage_(self, ucc, message):
        body = message.body()
        try:
            payload = json.loads(json.dumps(dict(body)))
        except Exception:
            return
        threading.Thread(target=self.controller.handle, args=(payload,),
                         daemon=True).start()


class WindowDelegate(NSObject):
    """Closing the window and Quit both end setup, through finish() and
    its own exit code - never NSApplication's terminate:, which exits 0
    and would read as a finished setup to the launcher."""
    on_close = None

    def windowWillClose_(self, notification) -> None:
        if self.on_close is not None:
            self.on_close()

    def applicationShouldTerminate_(self, app) -> int:
        if self.on_close is not None:
            self.on_close()
        return 0                                   # NSTerminateCancel


def build_menu(app) -> None:
    """Quit, and the Edit actions a text field needs (paste, above all)."""
    bar = NSMenu.alloc().init()
    app_item = NSMenuItem.alloc().init()
    bar.addItem_(app_item)
    app_menu = NSMenu.alloc().init()
    app_menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"Quit {app_bundle.APP_NAME}", "terminate:", "q"))
    app_item.setSubmenu_(app_menu)
    edit_item = NSMenuItem.alloc().init()
    bar.addItem_(edit_item)
    edit = NSMenu.alloc().initWithTitle_("Edit")
    for title, action, key in (("Undo", "undo:", "z"), ("Redo", "redo:", "Z"),
                               ("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
                               ("Paste", "paste:", "v"),
                               ("Select All", "selectAll:", "a")):
        edit.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, action, key))
    edit_item.setSubmenu_(edit)
    app.setMainMenu_(bar)


def write_png(image, path: str) -> bool:
    tiff = image.TIFFRepresentation()
    rep = NSBitmapImageRep.imageRepWithData_(tiff) if tiff else None
    data = rep.representationUsingType_properties_(
        NSBitmapImageFileTypePNG, {}) if rep else None
    return bool(data) and bool(data.writeToFile_atomically_(path, True))


def run_window(args) -> int:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory
                             if args.offscreen
                             else NSApplicationActivationPolicyRegular)
    build_menu(app)
    result = {"code": 1}
    finished = threading.Event()

    def finish(code: int) -> None:
        if finished.is_set():
            return
        result["code"] = code
        finished.set()
        emit(event="finish", code=code)

        def leave() -> None:
            # AppHelper.stopEventLoop is terminate: under runEventLoop,
            # which the delegate cancels; nothing is left to tear down.
            sys.stdout.flush()
            os._exit(code)
        AppHelper.callAfter(leave)

    config = WKWebViewConfiguration.alloc().init()
    sink = ScriptSink.alloc().init()
    config.userContentController().addScriptMessageHandler_name_(sink,
                                                                 "setup")
    style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
             | NSWindowStyleMaskMiniaturizable)
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, WIDTH, HEIGHT), style, NSBackingStoreBuffered, False)
    window.setTitle_(f"Set up {app_bundle.APP_NAME}")
    window.setReleasedWhenClosed_(False)
    view = WKWebView.alloc().initWithFrame_configuration_(
        NSMakeRect(0, 0, WIDTH, HEIGHT), config)
    window.setContentView_(view)
    delegate = WindowDelegate.alloc().init()
    delegate.on_close = lambda: finish(result["code"])
    window.setDelegate_(delegate)
    app.setDelegate_(delegate)

    def take_snapshot() -> None:
        def done(image, error) -> None:
            ok = image is not None and write_png(image, args.snapshot)
            emit(event="snapshot", path=args.snapshot, ok=ok,
                 error=str(error) if error else "")
            finish(0 if ok else 3)
        view.takeSnapshotWithConfiguration_completionHandler_(None, done)

    def push(state: dict) -> None:
        script = f"window.render({json.dumps(state)})"
        AppHelper.callAfter(view.evaluateJavaScript_completionHandler_,
                            script, None)

    def open_url(url: str) -> None:
        emit(event="open", url=url)
        if args.fake_grants is None:
            AppHelper.callAfter(
                NSWorkspace.sharedWorkspace().openURL_,
                NSURL.URLWithString_(url))

    if args.fake_grants is not None:
        fake = json.loads(args.fake_grants)

        def request(grant_id: str) -> bool:
            emit(event="request", grant=grant_id)
            return True

        def probe() -> dict:
            return {grant.id: fake.get(grant.id, "unknown")
                    for grant in app_setup.GRANTS}
    else:
        probe = app_setup.probe_in_child

        def request(grant_id: str) -> bool:
            box: dict = {}
            ready = threading.Event()

            def ask() -> None:
                try:
                    box["ok"] = request_grant(grant_id)
                finally:
                    ready.set()
            AppHelper.callAfter(ask)
            ready.wait(10)
            emit(event="request", grant=grant_id, prompted=box.get("ok"))
            return bool(box.get("ok"))

    claude = (lambda: dict(json.loads(args.fake_claude))) \
        if args.fake_claude else app_setup.claude_status

    def poller() -> None:
        first = True
        while not finished.is_set():
            controller.poll()
            if first:
                first = False
                if args.js:
                    AppHelper.callAfter(
                        view.evaluateJavaScript_completionHandler_,
                        args.js, None)
                if args.snapshot:
                    threading.Timer(args.settle, lambda: AppHelper.callAfter(
                        take_snapshot)).start()
            finished.wait(POLL_S)

    import boss
    controller = app_setup.SetupController(
        Path(args.home).expanduser(), model=boss.LIVE_MODEL, push=push,
        request=request, open_url=open_url, finish=finish,
        api_base=args.api_base, probe=probe, claude=claude,
        on_ready=lambda: threading.Thread(target=poller,
                                          daemon=True).start())
    sink.controller = controller

    view.loadFileURL_allowingReadAccessToURL_(
        NSURL.fileURLWithPath_(str(PAGE)),
        NSURL.fileURLWithPath_(str(PAGE.parent)))
    if args.offscreen:
        # Drawn, so WebKit renders and snapshots, but where no screen is.
        window.setFrameOrigin_((-30000, -30000))
        window.orderBack_(None)
    else:
        window.center()
        window.makeKeyAndOrderFront_(None)
        app.activateIgnoringOtherApps_(True)
    AppHelper.runEventLoop()
    return result["code"]


def run_alert(title: str, detail: str) -> int:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    app.activateIgnoringOtherApps_(True)
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(detail)
    alert.runModal()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="the app's setup window")
    parser.add_argument("--home", default="~/.voice-conductor")
    parser.add_argument("--api-base", default=app_setup.API_BASE)
    parser.add_argument("--offscreen", action="store_true")
    parser.add_argument("--snapshot", metavar="PNG")
    parser.add_argument("--settle", type=float, default=1.5,
                        help="seconds between --js and the snapshot")
    parser.add_argument("--js")
    parser.add_argument("--fake-grants", metavar="JSON")
    parser.add_argument("--fake-claude", metavar="JSON")
    parser.add_argument("--alert", metavar="TITLE")
    parser.add_argument("--detail", default="")
    args = parser.parse_args(argv)
    if args.alert:
        return run_alert(args.alert, args.detail)
    return run_window(args)


if __name__ == "__main__":
    raise SystemExit(main())
