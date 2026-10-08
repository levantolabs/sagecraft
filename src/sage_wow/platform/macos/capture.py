from __future__ import annotations

import sys
import os
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Iterator

from sage_wow.models import Frame
from sage_wow.platform.macos.geometry import Rect
from sage_wow.platform.macos.system import selected_window_is_frontmost, window_details


class CaptureError(RuntimeError):
    pass


class ForegroundCaptureInterrupted(CaptureError):
    """Selected-window pixels were unavailable solely because focus was lost."""


def capture_window(window_id: int, output_dir: Path | None = None) -> Frame:
    """Capture the selected window surface, excluding other apps and the Dock."""
    if sys.platform != "darwin":
        raise CaptureError("Live capture is available only on macOS")
    with _autorelease_pool():
        return _capture_window(window_id, output_dir)


@contextmanager
def _autorelease_pool() -> Iterator[None]:
    """Bound Objective-C temporary-object lifetime on persistent worker threads."""
    try:
        import objc
    except ImportError:
        # Keep platform-independent imports/tests usable; the native capture
        # imports below will report the missing macOS dependencies as before.
        yield
        return
    with objc.autorelease_pool():
        yield


def _capture_window(window_id: int, output_dir: Path | None = None) -> Frame:
    # Keep the guard inside the real native implementation boundary so tests
    # can still replace this function with a fake frame producer. This must run
    # before Quartz window enumeration or screen capture APIs are called.
    if os.environ.get("SAGE_WOW_DISABLE_LIVE_CAPTURE") == "1":
        raise CaptureError("Live window capture is disabled by SAGE_WOW_DISABLE_LIVE_CAPTURE=1")
    window = window_details(window_id)
    if not window:
        raise CaptureError(f"Selected window {window_id} is not visible; foreground it and reselect it")
    if not selected_window_is_frontmost(window):
        raise ForegroundCaptureInterrupted("Selected game window is not foreground; bring it forward before capture or control")
    bounds = window["bounds"]
    rect = Rect(int(bounds["X"]), int(bounds["Y"]), int(bounds["Width"]), int(bounds["Height"]))
    if rect.width <= 0 or rect.height <= 0:
        raise CaptureError("Selected window has invalid bounds")
    try:
        import Quartz
        from AppKit import NSBitmapImageRep, NSBitmapImageFileTypePNG
        from PIL import Image
    except ImportError as exc:
        raise CaptureError(f"Capture dependencies missing; install with `uv sync --extra macos`: {exc}") from exc
    try:
        surface = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow, window_id,
            Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageNominalResolution,
        )
        if surface is None:
            raise CaptureError("Window server did not provide the selected window surface")
        bitmap = NSBitmapImageRep.alloc().initWithCGImage_(surface)
        encoded = bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
        image = Image.open(BytesIO(bytes(encoded))).convert("RGB")
        if image.size != (rect.width, rect.height):
            image = image.resize((rect.width, rect.height), Image.Resampling.LANCZOS)
    except Exception as exc:
        raise CaptureError(f"Selected-window capture failed: {exc}") from exc
    if not selected_window_is_frontmost(window):
        raise ForegroundCaptureInterrupted("Selected window lost foreground during capture; image discarded")
    if image.width < 100 or image.height < 100:
        raise CaptureError("Captured region is unexpectedly small; check window/display scaling")
    path = None
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        path_obj = output_dir / f"frame-{Frame.create('macos', image.width, image.height).frame_id}.jpg"
        image.save(path_obj, format="JPEG", quality=90)
        path = str(path_obj)
    return Frame.create("macos_window", image.width, image.height, image_path=path)
