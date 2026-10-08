from __future__ import annotations

import fcntl
import os
import sys
from pathlib import Path


class InputError(RuntimeError):
    pass


class QuartzInput:
    """Minimal event adapter. Callers must gate all events on focus/calibration state."""

    MODIFIER_KEYS = {56: 'kCGEventFlagMaskShift', 60: 'kCGEventFlagMaskShift',
                     59: 'kCGEventFlagMaskControl', 62: 'kCGEventFlagMaskControl',
                     58: 'kCGEventFlagMaskAlternate', 61: 'kCGEventFlagMaskAlternate',
                     55: 'kCGEventFlagMaskCommand', 54: 'kCGEventFlagMaskCommand'}

    def __init__(self) -> None:
        if os.environ.get("SAGE_WOW_DISABLE_LIVE_INPUT") == "1":
            raise InputError("Live input is disabled by SAGE_WOW_DISABLE_LIVE_INPUT=1")
        if sys.platform != "darwin":
            raise InputError("Live input is available only on macOS")
        self._ownership_lock = self._acquire_ownership_lock()
        try:
            import Quartz
        except Exception as exc:
            self._release_ownership_lock()
            if isinstance(exc, ImportError):
                raise InputError("Install the macOS extra with `uv sync --extra macos`") from exc
            raise
        self.q = Quartz
        self.held_keys: set[int] = set()
        self._cleanup_keys: set[int] = set()
        self.held_buttons: dict[int, tuple[int, int]] = {}

    @staticmethod
    def _acquire_ownership_lock():
        """Exclusively own native input across all Sage WoW data directories."""
        lock_path = Path.home() / ".local" / "state" / "sage-wow" / "input-owner.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            lock_path.parent.chmod(0o700)
        except OSError:
            pass
        handle = lock_path.open("a+")
        try:
            os.chmod(lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise InputError("Another Sage WoW process owns native input") from exc
        except OSError:
            handle.close()
            raise
        return handle

    def _release_ownership_lock(self) -> None:
        handle = getattr(self, "_ownership_lock", None)
        if handle is not None:
            self._ownership_lock = None
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _ensure_owner(self) -> None:
        if self._ownership_lock is None or self._ownership_lock.closed:
            raise InputError("This backend no longer owns native input")

    def close(self) -> None:
        """Release held inputs before relinquishing this process's ownership."""
        try:
            self.release_all()
        finally:
            self._release_ownership_lock()

    def key(self, keycode: int, down: bool) -> None:
        self._ensure_owner()
        event = self.q.CGEventCreateKeyboardEvent(None, keycode, down)
        owned = self.held_keys | self._cleanup_keys
        intended = owned | {keycode} if down else owned - {keycode}
        if keycode in self.MODIFIER_KEYS or any(key in self.MODIFIER_KEYS for key in owned):
            # Encode only owned modifiers; ambient physical modifiers cannot
            # alter this chord. Preserve unrelated event flags and ordinary keys.
            mask = 0
            flags = 0
            for key, name in self.MODIFIER_KEYS.items():
                bit = getattr(self.q, name)
                mask |= bit
                if key in intended:flags |= bit
            self.q.CGEventSetFlags(event, (self.q.CGEventGetFlags(event) & ~mask) | flags)
        if down:self._cleanup_keys.add(keycode)
        self.q.CGEventPost(self.q.kCGHIDEventTap, event)
        if down:
            self.held_keys.add(keycode)
        else:
            self.held_keys.discard(keycode)
        self._cleanup_keys.discard(keycode)

    def text(self, value: str) -> None:
        self._ensure_owner()
        # Unicode keyboard events leave the user's clipboard untouched.
        for down in (True, False):
            event = self.q.CGEventCreateKeyboardEvent(None, 0, down)
            self.q.CGEventKeyboardSetUnicodeString(event, len(value), value)
            self.q.CGEventPost(self.q.kCGHIDEventTap, event)

    def mouse_move(self, x: int, y: int) -> None:
        self._ensure_owner()
        q = self.q
        q.CGEventPost(q.kCGHIDEventTap, q.CGEventCreateMouseEvent(
            None, q.kCGEventMouseMoved, q.CGPointMake(x, y), 0))

    def mouse_button(self, x: int, y: int, button: int, down: bool) -> None:
        self._ensure_owner()
        q = self.q
        point = q.CGPointMake(x, y)
        event_type = {(0, True): q.kCGEventLeftMouseDown, (0, False): q.kCGEventLeftMouseUp,
                      (1, True): q.kCGEventRightMouseDown, (1, False): q.kCGEventRightMouseUp}.get((button, down))
        if event_type is None:
            raise InputError(f"Unsupported mouse button: {button}")
        q.CGEventPost(q.kCGHIDEventTap, q.CGEventCreateMouseEvent(None, event_type, point, button))
        if down:
            self.held_buttons[button] = (x, y)
        else:
            self.held_buttons.pop(button, None)

    def release_all(self) -> None:
        self._ensure_owner()
        error = None
        # Letters release while the owned modifier is still represented.
        for keycode in sorted(self.held_keys | self._cleanup_keys,
                              key=lambda key: (key in self.MODIFIER_KEYS, key)):
            try:self.key(keycode, False)
            except BaseException as exc:error = error or exc
        for button, (x, y) in tuple(self.held_buttons.items()):
            try:self.mouse_button(x, y, button, False)
            except BaseException as exc:error = error or exc
        if error is not None:raise error
