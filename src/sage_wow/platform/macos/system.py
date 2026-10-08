from __future__ import annotations

import platform
import math
import sys
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass
class Check:
    name: str
    status: str
    detail: str


def host_checks() -> list[Check]:
    checks = [
        Check("operating_system", "ok" if sys.platform == "darwin" else "unavailable", platform.mac_ver()[0] or platform.system()),
        Check("architecture", "ok" if platform.machine() in {"arm64", "aarch64"} else "notice", platform.machine()),
        Check("python", "ok" if sys.version_info >= (3, 12) else "unavailable", platform.python_version()),
    ]
    if sys.platform != "darwin":
        checks.extend([
            Check("screen_recording", "unavailable", "macOS permission check requires macOS"),
            Check("accessibility", "unavailable", "macOS permission check requires macOS"),
            Check("input_monitoring", "unavailable", "macOS permission check requires macOS"),
        ])
        return checks
    try:
        import ApplicationServices

        trusted = bool(ApplicationServices.AXIsProcessTrusted())
        checks.append(Check("accessibility", "ok" if trusted else "needs_permission", _permission_detail("Accessibility", trusted)))
    except (ImportError, AttributeError) as exc:
        checks.append(Check("accessibility", "unavailable", f"Install the macOS extra: {exc}"))
    try:
        import Quartz

        screen_ok = bool(Quartz.CGPreflightScreenCaptureAccess())
        checks.append(Check("screen_recording", "ok" if screen_ok else "needs_permission", _permission_detail("Screen Recording", screen_ok)))
    except (ImportError, AttributeError) as exc:
        checks.append(Check("screen_recording", "unavailable", f"Install the macOS extra: {exc}"))
    try:
        import Quartz

        hotkey_ok = bool(Quartz.CGPreflightListenEventAccess())
        checks.append(Check("input_monitoring", "ok" if hotkey_ok else "needs_permission",
                            _permission_detail("Input Monitoring", hotkey_ok)))
    except (ImportError, AttributeError) as exc:
        checks.append(Check("input_monitoring", "unavailable", f"Global stop hotkey check unavailable: {exc}"))
    return checks


def _permission_detail(name: str, granted: bool) -> str:
    if granted:
        return "Granted to the current process."
    return (
        f"Grant {name} to the host app that launches sage-wow in System Settings > Privacy & Security. "
        "Quit and relaunch that host app after changing permission."
    )


def list_windows() -> list[dict[str, object]]:
    if sys.platform != "darwin":
        return []
    windows: list[dict[str, object]] = []
    for row in _frontmost_window_snapshot():
        if row["layer"] != 0 or not row["bounds"]:
            continue
        windows.append({key: row[key] for key in
                        ("window_id", "owner", "owner_pid", "title", "bounds", "onscreen")})
    return windows


def window_details(window_id: int) -> dict[str, object] | None:
    return next((window for window in list_windows() if window["window_id"] == window_id), None)


def selected_window_is_frontmost(window: dict[str, object]) -> bool:
    if sys.platform != "darwin":
        return False
    try:
        # Query fresh WindowServer data, including system-modal layers. Filtering
        # to app windows here lets Force Quit and other modal sheets look focused.
        windows = _frontmost_window_snapshot()
        selected_id = int(window["window_id"])
        selected_pid = int(window.get("owner_pid", 0))
        # Window ordering and keyboard focus are independent. An off-display
        # foreign app can remain active even when the game is visibly on top.
        if not selected_pid or _frontmost_application_pid() != selected_pid:
            return False
        selected_index = next((i for i, row in enumerate(windows)
                               if row["window_id"] == selected_id
                               and (not selected_pid or row["owner_pid"] == selected_pid)), None)
        if selected_index is None:
            return False
        selected = windows[selected_index]
        if not selected["onscreen"] or selected["layer"] != 0 or not selected["bounds"]:
            return False
        first_app = next((row for row in windows if row["layer"] == 0 and row["onscreen"]), None)
        if first_app is None or first_app["window_id"] != selected_id:
            return False
        floating_proofs = {}
        proof_deadline = None
        for row in windows[:selected_index]:
            if not row["onscreen"]:
                continue
            # The dashboard HUD is an intentionally floating, click-through
            # panel; it may sit above the game without taking input focus.
            if (row["owner"] == "Python" and row["layer"] == 3
                    and row["title"] == "Sage Live Decisions"):
                continue
            # WindowServer's cursor sprite and screen-recording status indicator
            # use this exact compositor identity/layer; neither takes keyboard focus.
            if (row["owner"] == "Window Server"
                    and row["title"] in {"Cursor", "StatusIndicator"}
                    and row["layer"] == 2147483630):
                continue
            # Only independently proven nonmodal floating windows may be ignored.
            raw = row.get('raw_bounds'); game_raw = selected.get('raw_bounds')
            pid = row['owner_pid']
            if (row['layer'] == 3 and pid > 0 and pid != selected_pid
                    and _valid_raw_bounds(raw) and _valid_raw_bounds(game_raw)
                    and not _bounds_overlap(raw, game_raw)):
                if proof_deadline is None:
                    import time
                    proof_deadline = time.monotonic() + .25
                if pid not in floating_proofs:
                    floating_proofs[pid] = _nonmodal_floating_proofs(pid, deadline=proof_deadline, wanted=[
                        candidate['raw_bounds'] for candidate in windows[:selected_index]
                        if candidate['owner_pid'] == pid and candidate['layer'] == 3
                        and _valid_raw_bounds(candidate.get('raw_bounds'))
                        and not _bounds_overlap(candidate['raw_bounds'], game_raw)])
                matches = [proof for proof in floating_proofs[pid]
                           if all(abs(proof['bounds'][k]-raw[k]) <= .5 for k in raw)]
                if (len(matches) == 1 and matches[0]['safe']
                        and (matches[0]['window_id'] is None or matches[0]['window_id'] == row['window_id'])):
                    continue
            return False
        return _frontmost_application_pid() == selected_pid
    except (ImportError, AttributeError, KeyError, TypeError, ValueError, OSError):
        return False


def _valid_raw_bounds(bounds):
    return (isinstance(bounds, dict) and set(bounds) == {'X', 'Y', 'Width', 'Height'}
            and all(type(value) in (int, float) and math.isfinite(value) for value in bounds.values())
            and bounds['Width'] > 0 and bounds['Height'] > 0)


def _nonmodal_floating_proofs(pid, *, deadline=None, wanted=None):
    """Fresh public AX proof; no titles, private window APIs or cached permission.

    Geometry association tolerance is 0.5px. At most 32 windows / 64 immediate
    children, 20ms AX messaging timeout and 250ms total proof work across all candidate PIDs.
    """
    import time
    try:
        import ApplicationServices as AX
        from AppKit import NSRunningApplication
        with _native_autorelease_pool():
            deadline = deadline if deadline is not None else time.monotonic() + .25
            running = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if running is None or int(running.processIdentifier()) != pid or running.isActive() != False:
                return []
            app = AX.AXUIElementCreateApplication(pid)
            if AX.AXUIElementSetMessagingTimeout(app, .02) != 0:
                return []
            def attribute(element, name):
                if time.monotonic() >= deadline:
                    raise OSError('AX proof deadline')
                remaining = deadline-time.monotonic()
                if AX.AXUIElementSetMessagingTimeout(element, min(.02, remaining)) != 0:
                    raise OSError('AX timeout setup unavailable')
                error, value = AX.AXUIElementCopyAttributeValue(element, name, None)
                if error != 0:
                    raise OSError('AX attribute unavailable')
                return value
            windows = attribute(app, 'AXWindows')
            if windows is None or len(windows) > 32:
                return []
            proofs = []
            for window in windows:
                if time.monotonic() >= deadline or AX.AXUIElementSetMessagingTimeout(window, min(.02, deadline-time.monotonic())) != 0:
                    return []
                error, owner = AX.AXUIElementGetPid(window, None)
                if error or owner != pid:
                    return []
                ok, position = AX.AXValueGetValue(attribute(window, 'AXPosition'), AX.kAXValueCGPointType, None)
                sized, size = AX.AXValueGetValue(attribute(window, 'AXSize'), AX.kAXValueCGSizeType, None)
                bounds = {'X': float(position.x), 'Y': float(position.y),
                          'Width': float(size.width), 'Height': float(size.height)}
                if not ok or not sized or not _valid_raw_bounds(bounds):
                    return []
                if wanted is not None and not any(all(abs(bounds[k]-box[k]) <= .5 for k in bounds) for box in wanted):
                    continue
                role = str(attribute(window, 'AXRole'))
                subrole = str(attribute(window, 'AXSubrole'))
                modal = attribute(window, 'AXModal'); focused = attribute(window, 'AXFocused')
                minimized = attribute(window, 'AXMinimized')
                # Chrome can omit AXSheets. A complete immediate-child inventory
                # provides explicit sheet/dialog absence; unsupported is no proof.
                children = attribute(window, 'AXChildren')
                if children is None or len(children) > 64:
                    return []
                roles = [attribute(child, 'AXRole') for child in children]
                if any(not isinstance(role, str) or (not role.startswith('AX') or role == 'AXUnknown') for role in roles):
                    return []
                window_attribute = getattr(AX, 'kAXWindowNumberAttribute', None)
                window_id = int(attribute(window, window_attribute)) if window_attribute else None
                safe = (role == 'AXWindow' and subrole in {'AXFloatingWindow', 'AXStandardWindow'}
                        and modal == False and focused == False and minimized == False
                        and not {'AXSheet', 'AXDialog'}.intersection(roles))
                proofs.append({'bounds': bounds, 'safe': safe, 'window_id': window_id})
            if time.monotonic() >= deadline:
                return []
            return proofs
    except (ImportError, AttributeError, KeyError, TypeError, ValueError, OSError):
        return []

def _frontmost_window_snapshot() -> list[dict[str, object]]:
    """Return a fresh ordered snapshot with every visible WindowServer layer."""
    # Keep Quartz' autoreleased arrays, dictionaries, and strings inside a short
    # pool, returning only copied Python primitives to the focus gate.
    try:
        with _native_autorelease_pool():
            return _copy_frontmost_window_snapshot()
    except (ImportError, AttributeError, KeyError, TypeError, ValueError, OSError):
        # All callers (including window_details/current_gate) must see an
        # unusable snapshot rather than let a native query kill the watchdog.
        return []


def _frontmost_application_pid() -> int | None:
    from AppKit import NSWorkspace
    with _native_autorelease_pool():
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(app.processIdentifier()) if app else None


def _active_display_bounds() -> list[dict[str, float]]:
    """Quartz global coordinates, including displays left/above the main one."""
    import Quartz
    error, _, count = Quartz.CGGetActiveDisplayList(0, None, None)
    if error or not count:
        raise OSError('active display enumeration unavailable')
    error, displays, actual_count = Quartz.CGGetActiveDisplayList(count, None, None)
    if error or actual_count != count or len(displays) != count:
        raise OSError('active display enumeration changed or failed')
    result = []
    for display in displays:
        rect = Quartz.CGDisplayBounds(display)
        bounds = {'X': float(rect.origin.x), 'Y': float(rect.origin.y),
                  'Width': float(rect.size.width), 'Height': float(rect.size.height)}
        if not all(math.isfinite(value) for value in bounds.values()) or bounds['Width'] <= 0 or bounds['Height'] <= 0:
            raise ValueError('invalid active display geometry')
        result.append(bounds)
    return result


@contextmanager
def _native_autorelease_pool():
    """Use PyObjC's pool on macOS while keeping fake-Quartz tests portable."""
    try:
        import objc
    except ImportError:
        yield
        return
    with objc.autorelease_pool():
        yield


def _copy_frontmost_window_snapshot() -> list[dict[str, object]]:
    """Copy WindowServer output to plain Python values inside the pool."""
    import Quartz

    options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    rows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
    displays = _active_display_bounds()
    result = []
    for row in rows:
        bounds = row.get(Quartz.kCGWindowBounds, {})
        onscreen = bool(row.get(Quartz.kCGWindowIsOnscreen, False))
        if bounds and all(key in bounds for key in ('X', 'Y', 'Width', 'Height')):
            # OnScreenOnly can include Chrome strips ending exactly at y=0.
            # Keep uncertain geometry conservative; use raw floats so even a
            # fractional visible overlap is not rounded away by calibration.
            raw_bounds = {key: float(bounds[key]) for key in ('X', 'Y', 'Width', 'Height')}
            if not all(math.isfinite(value) for value in raw_bounds.values()):
                raise ValueError('invalid window geometry')
            onscreen = onscreen and any(_bounds_overlap(raw_bounds, display) for display in displays)
        result.append({
            "window_id": int(row.get(Quartz.kCGWindowNumber, 0)),
            "owner": str(row.get(Quartz.kCGWindowOwnerName, "") or ""),
            "owner_pid": int(row.get(Quartz.kCGWindowOwnerPID, 0)),
            "title": str(row.get(Quartz.kCGWindowName, "") or ""),
            "bounds": _plain_bounds(bounds) if bounds else {},
            "raw_bounds": raw_bounds if bounds and all(key in bounds for key in ('X', 'Y', 'Width', 'Height')) else {},
            "layer": int(row.get(Quartz.kCGWindowLayer, -1)),
            "onscreen": onscreen,
        })
    return result


def _bounds_overlap(a: dict[str, int], b: dict[str, int]) -> bool:
    return (a.get('Width', 0) > 0 and a.get('Height', 0) > 0
            and b.get('Width', 0) > 0 and b.get('Height', 0) > 0
            and a.get("X", 0) < b.get("X", 0) + b.get("Width", 0)
            and b.get("X", 0) < a.get("X", 0) + a.get("Width", 0)
            and a.get("Y", 0) < b.get("Y", 0) + b.get("Height", 0)
            and b.get("Y", 0) < a.get("Y", 0) + a.get("Height", 0))


def _plain_bounds(bounds: dict[object, object]) -> dict[str, int]:
    return {str(key): int(round(float(value))) for key, value in bounds.items()}


def dependencies() -> list[Check]:
    results = []
    for name, module in (("mss", "mss"), ("Pillow", "PIL"), ("HTTPX", "httpx"),
                         ("python-dotenv", "dotenv"), ("PyObjC Quartz", "Quartz"),
                         ("PyObjC AppKit", "AppKit"), ("PyObjC Vision", "Vision")):
        try:
            __import__(module)
            results.append(Check(name, "ok", "import available"))
        except ImportError:
            status = "optional" if name.startswith("PyObjC") else "missing"
            results.append(Check(name, status, "install project dependencies"))
    return results


