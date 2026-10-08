from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from typing import Callable


MODIFIERS = {"ctrl": "kCGEventFlagMaskControl", "control": "kCGEventFlagMaskControl",
             "alt": "kCGEventFlagMaskAlternate", "option": "kCGEventFlagMaskAlternate",
             "shift": "kCGEventFlagMaskShift", "cmd": "kCGEventFlagMaskCommand",
             "command": "kCGEventFlagMaskCommand"}
KEYS = {"escape": 53, "esc": 53}


@dataclass(frozen=True)
class HotkeySpec:
    keycode: int
    modifier_names: frozenset[str]


def parse_hotkey(value: str) -> HotkeySpec:
    modifiers: set[str] = set()
    keycode = None
    for token in (item.strip().lower() for item in value.split("+")):
        if token in MODIFIERS:
            modifiers.add(MODIFIERS[token])
        elif token in KEYS:
            if keycode is not None:
                raise ValueError("hotkey must have exactly one non-modifier key")
            keycode = KEYS[token]
        elif token.startswith("keycode:"):
            if keycode is not None:
                raise ValueError("hotkey must have exactly one non-modifier key")
            try:
                keycode = int(token.split(":", 1)[1])
            except ValueError as exc:
                raise ValueError("keycode must be an integer") from exc
            if keycode < 0:
                raise ValueError("keycode must be non-negative")
        else:
            raise ValueError(f"unsupported stop-hotkey token {token!r}; use modifiers plus Escape or keycode:N")
    if not modifiers or keycode is None:
        raise ValueError("stop hotkey needs at least one modifier and one key")
    return HotkeySpec(keycode, frozenset(modifiers))


def matches_hotkey(spec: HotkeySpec, keycode: int, flags: int, quartz) -> bool:
    return keycode == spec.keycode and all(flags & getattr(quartz, name) for name in spec.modifier_names)


class GlobalStopHotkey:
    """Listen-only macOS event tap; requires user-granted Input Monitoring."""

    def __init__(self, binding: str, on_stop: Callable[[], None]):
        self.spec = parse_hotkey(binding)
        self.on_stop = on_stop
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._run_loop = None
        self._tap = None
        self._source = None
        self._callback = None

    def start(self, timeout: float = 2.0) -> bool:
        if sys.platform != "darwin":
            return False
        try:
            import Quartz
            if not Quartz.CGPreflightListenEventAccess():
                return False
        except (ImportError, AttributeError):
            return False
        self._thread = threading.Thread(target=self._run, name="sage-wow-stop-hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self._error is None and self._tap is not None

    def _run(self) -> None:
        try:
            import CoreFoundation
            import Quartz
            mask = 1 << Quartz.kCGEventKeyDown

            def callback(_proxy, event_type, event, _refcon):
                if event_type == Quartz.kCGEventTapDisabledByTimeout:
                    Quartz.CGEventTapEnable(self._tap, True)
                    return event
                if event_type == Quartz.kCGEventKeyDown:
                    keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                    flags = Quartz.CGEventGetFlags(event)
                    if matches_hotkey(self.spec, keycode, flags, Quartz):
                        self.on_stop()
                return event

            self._callback = callback
            self._tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionListenOnly, mask, callback, None,
            )
            if self._tap is None:
                raise RuntimeError("macOS refused the global hotkey event tap")
            self._source = CoreFoundation.CFMachPortCreateRunLoopSource(None, self._tap, 0)
            self._run_loop = CoreFoundation.CFRunLoopGetCurrent()
            CoreFoundation.CFRunLoopAddSource(self._run_loop, self._source, CoreFoundation.kCFRunLoopCommonModes)
            self._ready.set()
            CoreFoundation.CFRunLoopRun()
        except BaseException as exc:
            self._error = exc
            self._ready.set()

    def stop(self) -> None:
        if self._run_loop is not None:
            try:
                import CoreFoundation
                CoreFoundation.CFRunLoopStop(self._run_loop)
            except (ImportError, AttributeError):
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
