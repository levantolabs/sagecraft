"""Read-only spectator display. No capture, game commands, or input ownership.

The native shell hosts only bundled local HTML. Its one-way JavaScript bridge
publishes evidence; it never receives or executes actions from the page.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sys
import time


POLL_SECONDS = .25
BRIDGE_TIMEOUT = 2.
ASSETS = Path(__file__).resolve().parents[2] / "dashboard" / "spectator_assets"
# The frozen input gate already recognizes this click-through floating HUD role.
# Changing its native title would make the unchanged runner detect an occluder.
PANEL_TITLE = "Sage Live Decisions"


def update_script(snapshot: dict) -> str:
    """Data is parsed as JSON, never interpolated as executable page content."""
    payload = json.dumps(snapshot, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    return ('typeof window.updateSageHUD === "function" && '
            '(window.updateSageHUD(JSON.parse(' + json.dumps(payload) + ')), true)')


def cocoa_bounds(bounds, primary_top: float) -> tuple[float, float, float, float]:
    """WindowServer top-left points -> AppKit bottom-left points, including other screens."""
    values = (float(bounds.x), float(bounds.y), float(bounds.width), float(bounds.height), float(primary_top))
    if not all(math.isfinite(v) for v in values) or min(values[2:4]) <= 0:
        raise ValueError("Invalid selected window bounds")
    x, y, width, height, top = values
    return x, top - y - height, width, height


@dataclass(frozen=True)
class DisplayGate:
    visible: bool
    reason: str
    bounds: tuple[float, float, float, float] | None = None


class ProfileWindowProbe:
    """Fresh exact native identity/focus reads; no cached permission to display."""
    def __init__(self, *, profile_path=None, load_profile=None, current_gate=None, primary_top=None):
        if load_profile is None:
            from sage_wow.config import load_profile
        if current_gate is None:
            from sage_wow.platform.macos.window_gate import current_gate
        self.load_profile = load_profile
        self.current_gate = current_gate
        self.primary_top = primary_top
        self.fallback_profile = Path(profile_path).resolve() if profile_path else None
        self.cache = None

    def __call__(self, snapshot):
        selected = snapshot.get("window_id")
        if type(selected) is not int or selected <= 0:
            return DisplayGate(False, "session_window_unavailable")
        supplied = snapshot.get("profile_path")
        path = Path(supplied) if supplied else self.fallback_profile
        if path is None or not path.is_absolute():
            return DisplayGate(False, "identity_profile_unavailable")
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            expected = snapshot.get("profile_sha256") if supplied else None
            if supplied and (not expected or digest != expected):
                return DisplayGate(False, "identity_profile_hash_mismatch")
            key = (str(path), digest)
            if self.cache is None or self.cache[0] != key:
                self.cache = key, self.load_profile(path)
            profile = self.cache[1]
            if profile.window_id != selected:
                return DisplayGate(False, "selected_window_mismatch")
            gate = self.current_gate(profile)
            if gate.window_id != selected or not gate.client_identity_verified:
                return DisplayGate(False, "native_identity_unconfirmed")
            if not gate.foreground:
                return DisplayGate(False, "game_not_foreground")
            if gate.bounds is None:
                return DisplayGate(False, "window_bounds_unavailable")
            # Display geometry follows the exact window. A moved/resized window
            # does not grant gameplay calibration or any form of input authority.
            return DisplayGate(True, "foreground_exact_identity", cocoa_bounds(gate.bounds, self.primary_top()))
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            return DisplayGate(False, "identity_probe_" + type(exc).__name__)


class OverlayBridge:
    """Testable asynchronous publication/focus boundary for the native panel."""
    def __init__(self, renderer, probe, *, clock=time.monotonic):
        self.renderer = renderer
        self.probe = probe
        self.clock = clock
        self.identity = None
        self.rendered_identity = None
        self.last_success = None
        self.pending = None
        self.serial = 0
        self.visible = False
        self.reason = "waiting_for_feed"
        self.last_frame = None

    def hide(self, reason):
        self.renderer.hide()
        self.visible = False
        self.reason = reason

    def tick(self, snapshot, *, observed_at=None):
        now = self.clock()
        if not isinstance(snapshot, dict) or observed_at is None or now - observed_at > BRIDGE_TIMEOUT:
            self.hide("feed_unavailable_or_delayed")
            return
        identity = (snapshot.get("source"), snapshot.get("session_id"), snapshot.get("window_id"),
                    snapshot.get("source_revision"))
        if identity != self.identity:
            self.identity = identity
            self.rendered_identity = None
            self.pending = None
            self.hide("session_changed")
        # Native focus/identity is checked on every main-loop tick, regardless
        # of background database work or a pending WebKit callback.
        gate = self.probe(snapshot)
        if not gate.visible:
            self.hide(gate.reason)
        if self.pending and now - self.pending[1] > BRIDGE_TIMEOUT:
            self.pending = None
            self.rendered_identity = None
            self.hide("bridge_timeout")
        if self.pending is None:
            self.serial += 1
            ticket = self.serial
            self.pending = ticket, now

            def complete(result, error):
                if self.pending is None or self.pending[0] != ticket or self.identity != identity:
                    return
                self.pending = None
                # WebKit may bridge JS true as an NSNumber instead of the
                # singleton Python True; equality handles both representations.
                if error is not None or result != True:
                    self.rendered_identity = None
                    self.hide("page_not_ready" if error is None else "bridge_error")
                    return
                self.rendered_identity = identity
                self.last_success = self.clock()

            try:
                self.renderer.submit(update_script(snapshot), complete)
            except (TypeError, ValueError, RuntimeError):
                self.pending = None
                self.rendered_identity = None
                self.hide("bridge_publish_failed")
        ready = (self.rendered_identity == identity and self.last_success is not None
                 and now - self.last_success <= BRIDGE_TIMEOUT)
        if gate.visible and ready:
            if gate.bounds != self.last_frame:
                self.renderer.set_frame(gate.bounds)
                self.last_frame = gate.bounds
            if not self.visible:
                self.renderer.show()
            self.visible = True
            self.reason = "visible"
        elif gate.visible:
            self.hide("waiting_for_current_render")


def safe_state_directory(path, *session_directories):
    path = Path(path).resolve()
    for directory in session_directories:
        if directory and path.is_relative_to(Path(directory).resolve()):
            raise ValueError("HUD status/lock files must be outside consumed session evidence")
    return path


def write_status(path, payload):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n")
    temporary.replace(path)


def run_overlay(*, pointer, directory=None, window_id=None, profile_path=None, state_directory=None):
    # Imports are deferred so unit tests and --help never create native UI.
    import AppKit as A
    import Foundation as F
    import WebKit as W
    from sage_wow.dashboard.spectator import SpectatorFeed

    pointer = Path(pointer).resolve()
    try:
        selected_directory = json.loads(pointer.read_text()).get("directory")
    except (OSError, ValueError, TypeError):
        selected_directory = None
    state_directory = safe_state_directory(state_directory or pointer.parent / "spectator-hud",
                                          directory, selected_directory)
    state_directory.mkdir(parents=True, exist_ok=True)
    lock = (state_directory / "spectator-hud.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError("A spectator HUD already owns this display lock")
    (state_directory / "ready.json").unlink(missing_ok=True)
    feed = SpectatorFeed(pointer, fallback_directory=directory, window_id=window_id)
    worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sage-spectator-feed")
    app = A.NSApplication.sharedApplication()
    app.setActivationPolicy_(A.NSApplicationActivationPolicyAccessory)

    class SpectatorPanel(A.NSPanel):
        def canBecomeKeyWindow(self): return False
        def canBecomeMainWindow(self): return False

    panel = SpectatorPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        A.NSMakeRect(0, 0, 1, 1), A.NSWindowStyleMaskBorderless | A.NSWindowStyleMaskNonactivatingPanel,
        A.NSBackingStoreBuffered, False)
    panel.setTitle_(PANEL_TITLE)
    panel.setLevel_(A.NSFloatingWindowLevel)
    panel.setFloatingPanel_(True)
    panel.setHidesOnDeactivate_(False)
    panel.setIgnoresMouseEvents_(True)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(A.NSColor.clearColor())
    panel.setHasShadow_(False)
    panel.setCollectionBehavior_(A.NSWindowCollectionBehaviorCanJoinAllSpaces | A.NSWindowCollectionBehaviorFullScreenAuxiliary)
    config = W.WKWebViewConfiguration.alloc().init()
    config.setWebsiteDataStore_(W.WKWebsiteDataStore.nonPersistentDataStore())
    config.preferences().setJavaScriptCanOpenWindowsAutomatically_(False)
    web = W.WKWebView.alloc().initWithFrame_configuration_(A.NSMakeRect(0, 0, 1, 1), config)
    # WebKit's documented implementation workaround for transparent WKWebView:
    # https://bugs.webkit.org/show_bug.cgi?id=221663
    web.setValue_forKey_(False, "drawsBackground")
    web.setAutoresizingMask_(A.NSViewWidthSizable | A.NSViewHeightSizable)
    panel.setContentView_(web)
    entry = ASSETS / "index.html"
    if not entry.is_file():
        raise ValueError("Bundled spectator index.html is unavailable")

    class NavigationDelegate(F.NSObject):
        def webView_decidePolicyForNavigationAction_decisionHandler_(self, view, action, handler):
            url = action.request().URL()
            allowed = False
            if url is not None and url.isFileURL():
                try: allowed = Path(str(url.path())).resolve() == entry.resolve()
                except (OSError, ValueError): pass
            handler(W.WKNavigationActionPolicyAllow if allowed else W.WKNavigationActionPolicyCancel)

    navigation = NavigationDelegate.alloc().init()
    web.setNavigationDelegate_(navigation)
    web.loadFileURL_allowingReadAccessToURL_(F.NSURL.fileURLWithPath_(str(entry)), F.NSURL.fileURLWithPath_(str(ASSETS)))

    class Renderer:
        def submit(self, script, callback): web.evaluateJavaScript_completionHandler_(script, callback)
        def hide(self): panel.orderOut_(None)
        def show(self): panel.orderFrontRegardless()
        def set_frame(self, rect): panel.setFrame_display_(A.NSMakeRect(*rect), True)

    def primary_top():
        frame = A.NSScreen.screens()[0].frame()
        return frame.origin.y + frame.size.height

    bridge = OverlayBridge(Renderer(), ProfileWindowProbe(profile_path=profile_path, primary_top=primary_top))
    state = {"future": None, "future_started_at": None, "snapshot": None, "observed_at": None,
             "write_at": 0., "stop": False, "last_log": None, "ready": False, "status_safe": True}

    def status():
        snap = state["snapshot"] or {}
        return {"pid": os.getpid(), "updated_at": time.time(), "native_ready": True,
                "bridge_ready": bridge.rendered_identity == bridge.identity and bridge.last_success is not None,
                "visible": bridge.visible, "reason": bridge.reason, "panel_window_id": int(panel.windowNumber()),
                "window_id": snap.get("window_id"), "session_id": snap.get("session_id"), "source": snap.get("source"),
                "state": snap.get("state", "UNAVAILABLE"),
                "pointer": str(pointer), "click_through": True, "input_owner": False, "native_capture": False}

    class Poller(F.NSObject):
        def tick_(self, timer):
            if state["stop"]:
                bridge.hide("stopping")
                app.stop_(None)
                return
            try:
                future = state["future"]
                if future is not None and future.done():
                    state["future"] = None
                    state["snapshot"] = future.result()
                    state["observed_at"] = state["future_started_at"]
                    try:
                        safe_state_directory(state_directory, state["snapshot"].get("source"))
                    except ValueError:
                        state["status_safe"] = False
                        state["stop"] = True
                        bridge.hide("status_directory_overlaps_session")
                        app.stop_(None)
                        return
                if state["future"] is None:
                    state["future_started_at"] = time.monotonic()
                    state["future"] = worker.submit(feed.poll)
                bridge.tick(state["snapshot"], observed_at=state["observed_at"])
            except Exception as exc:
                state["snapshot"] = None
                state["observed_at"] = None
                bridge.hide("poll_error_" + type(exc).__name__)
            now = time.monotonic()
            if now - state["write_at"] >= 1:
                data = status()
                write_status(state_directory / "status.json", data)
                if data["bridge_ready"] and not state["ready"]:
                    write_status(state_directory / "ready.json", data)
                    state["ready"] = True
                key = data["source"], data["window_id"], data["visible"], data["reason"]
                if key != state["last_log"]:
                    print(json.dumps(data), flush=True)
                    state["last_log"] = key
                state["write_at"] = now

    poller = Poller.alloc().init()
    timer = F.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        POLL_SECONDS, poller, "tick:", None, True)
    previous_handlers = {}

    def stop(*_): state["stop"] = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[sig] = signal.signal(sig, stop)
    try:
        poller.tick_(timer)
        app.run()
    finally:
        timer.invalidate()
        bridge.hide("stopped")
        web.setNavigationDelegate_(None)
        worker.submit(feed.close)
        worker.shutdown(wait=False)
        if state["status_safe"]:
            write_status(state_directory / "status.json", {**status(), "native_ready": False, "bridge_ready": False})
            (state_directory / "ready.json").unlink(missing_ok=True)
        lock.close()
        for sig, handler in previous_handlers.items(): signal.signal(sig, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pointer", type=Path, required=True, help="Explicit active-session.json to follow read-only")
    parser.add_argument("--data-dir", type=Path, help="Fallback session directory; never created or written")
    parser.add_argument("--window-id", type=int, help="Fallback exact game window id")
    parser.add_argument("--profile", type=Path, help="Optional identity profile only when source manifest omits it")
    parser.add_argument("--state-dir", type=Path, help="Independent HUD lock/ready/status directory outside run evidence")
    parser.add_argument("--check", action="store_true", help="Check bridge and bundled assets without starting native UI")
    args = parser.parse_args(argv)
    if sys.platform != "darwin": parser.error("Native spectator HUD requires macOS")
    if args.check:
        import WebKit
        if not (ASSETS / "index.html").is_file(): parser.error("Bundled spectator HTML is unavailable")
        print(json.dumps({"webkit": WebKit.__file__, "assets": str(ASSETS), "native_ui_started": False}))
        return
    run_overlay(pointer=args.pointer, directory=args.data_dir, window_id=args.window_id,
                profile_path=args.profile, state_directory=args.state_dir)


if __name__ == "__main__":
    main()
