"""Global shortcuts that work while any app or game has focus.

Two mechanisms, both dependency-free Win32 calls:
- Keyboard press actions use RegisterHotKey: reliable, needs no admin rights,
  and the key is captured so the game doesn't also act on it.
- Mouse buttons, joystick/HOTAS buttons and hold actions (which need to know
  when the button is released) are polled ~100x per second with
  GetAsyncKeyState / joyGetPosEx. Polling never captures the input.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

MODIFIERS = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002, "shift": 0x0004, "win": 0x0008}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

# Virtual-key codes used to check held modifiers while polling.
_MOD_VKS = {0x0001: (0x12,), 0x0002: (0x11,), 0x0004: (0x10,), 0x0008: (0x5B, 0x5C)}

_NAMED_KEYS = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "pause": 0x13, "capslock": 0x14,
    "escape": 0x1B, "esc": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E, "scrolllock": 0x91,
    "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
}
# Left/right click are deliberately not bindable: every click would fire them.
_MOUSE_BUTTONS = {"mouse3": 0x04, "mouse4": 0x05, "mouse5": 0x06}

HOLD_ACTIONS = frozenset({"freeze_hold", "center_hold"})
MAX_JOYSTICKS = 16
JOY_BUTTONS = 32


@dataclass(frozen=True)
class Binding:
    kind: str        # "key", "mouse" or "joy"
    mods: int        # MODIFIERS bit flags
    code: int        # virtual key / mouse VK / joystick button index (0-based)
    joy: int = -1    # joystick id (0-based) for kind == "joy"

    def __str__(self) -> str:
        names = [n for n, bit in (("ctrl", 2), ("alt", 1), ("shift", 4), ("win", 8)) if self.mods & bit]
        if self.kind == "joy":
            names.append(f"joy{self.joy + 1}:button{self.code + 1}")
        elif self.kind == "mouse":
            names.append(next(n for n, vk in _MOUSE_BUTTONS.items() if vk == self.code))
        else:
            names.append(_key_name(self.code))
        return "+".join(names)


def _key_name(vk: int) -> str:
    for name, code in _NAMED_KEYS.items():
        if code == vk:
            return name
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    if 0x60 <= vk <= 0x69:
        return f"numpad{vk - 0x60}"
    return chr(vk)


def _parse_key(key: str, spec: str) -> int:
    if key in _NAMED_KEYS:
        return _NAMED_KEYS[key]
    if len(key) >= 2 and key[0] == "f" and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return 0x70 + int(key[1:]) - 1
    if key.startswith("numpad") and key[6:].isdigit() and len(key) == 7:
        return 0x60 + int(key[6:])
    if len(key) == 1 and key.isalnum():
        return ord(key.upper())
    raise ValueError(f"unknown key '{key}' in '{spec}'")


def _parse_joy(key: str, spec: str) -> tuple[int, int]:
    # joy1:button5, joy1:b5
    try:
        dev, btn = key[3:].split(":", 1)
        btn = btn[6:] if btn.startswith("button") else btn[1:] if btn.startswith("b") else btn
        joy, button = int(dev), int(btn)
    except ValueError:
        raise ValueError(f"joystick buttons look like 'joy1:button5', got '{spec}'") from None
    if not 1 <= joy <= MAX_JOYSTICKS or not 1 <= button <= JOY_BUTTONS:
        raise ValueError(f"joystick 1-{MAX_JOYSTICKS}, button 1-{JOY_BUTTONS} in '{spec}'")
    return joy - 1, button - 1


def parse_binding(spec: str) -> Binding:
    """'ctrl+shift+F9' / 'mouse5' / 'joy1:button3' -> Binding."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty shortcut")
    mods = 0
    for p in parts[:-1]:
        if p not in MODIFIERS:
            raise ValueError(f"unknown modifier '{p}' in '{spec}'")
        mods |= MODIFIERS[p]
    key = parts[-1]
    if key in _MOUSE_BUTTONS:
        return Binding("mouse", mods, _MOUSE_BUTTONS[key])
    if key.startswith("joy"):
        joy, button = _parse_joy(key, spec)
        return Binding("joy", mods, button, joy)
    return Binding("key", mods, _parse_key(key, spec))


def parse_hotkey(spec: str) -> tuple[int, int]:
    """Keyboard-only form: (modifier flags, virtual key)."""
    b = parse_binding(spec)
    if b.kind != "key":
        raise ValueError(f"'{spec}' is not a keyboard key")
    return b.mods, b.code


class _JOYINFOEX(ctypes.Structure):
    _fields_ = [(n, wt.DWORD) for n in (
        "dwSize", "dwFlags", "dwXpos", "dwYpos", "dwZpos", "dwRpos", "dwUpos", "dwVpos",
        "dwButtons", "dwButtonNumber", "dwPOV", "dwReserved1", "dwReserved2")]


JOY_RETURNBUTTONS = 0x80


class InputPoller:
    """Reads mouse/keyboard/joystick button state on demand (Windows only)."""

    def __init__(self) -> None:
        self._user32 = ctypes.windll.user32
        self._winmm = ctypes.windll.winmm
        self._info = _JOYINFOEX()
        self._info.dwSize = ctypes.sizeof(_JOYINFOEX)
        self._info.dwFlags = JOY_RETURNBUTTONS
        self._retry_at: dict[int, float] = {}

    def vk_down(self, vk: int) -> bool:
        return bool(self._user32.GetAsyncKeyState(vk) & 0x8000)

    def mods_down(self, mods: int) -> bool:
        return all(any(self.vk_down(vk) for vk in vks) for bit, vks in _MOD_VKS.items() if mods & bit)

    def joy_buttons(self, joy: int) -> int | None:
        """Button bitmask, or None if that joystick isn't connected."""
        now = time.monotonic()
        if self._retry_at.get(joy, 0.0) > now:
            return None
        if self._winmm.joyGetPosEx(joy, ctypes.byref(self._info)) != 0:
            # Querying a missing device is slow-ish; don't hammer it.
            self._retry_at[joy] = now + 2.0
            return None
        return int(self._info.dwButtons)

    def is_down(self, b: Binding, joy_masks: dict[int, int | None] | None = None) -> bool:
        """`joy_masks` caches each joystick's buttons for one polling pass, so
        several bindings on the same stick read the driver once."""
        if not self.mods_down(b.mods):
            return False
        if b.kind == "joy":
            if joy_masks is None:
                mask = self.joy_buttons(b.joy)
            else:
                if b.joy not in joy_masks:
                    joy_masks[b.joy] = self.joy_buttons(b.joy)
                mask = joy_masks[b.joy]
            return mask is not None and bool(mask & (1 << b.code))
        return self.vk_down(b.code)


class HotkeyListener:
    """Fires press actions into `events` and tracks held actions in `held`."""

    def __init__(self, bindings: dict[str, str], hold_actions: frozenset[str] = HOLD_ACTIONS):
        self.events: queue.Queue[str] = queue.Queue()
        self.held: frozenset[str] = frozenset()
        self._registered: dict[str, Binding] = {}
        self._polled: dict[str, Binding] = {}
        self._hold_actions = hold_actions
        self._threads: list[threading.Thread] = []
        self._msg_thread_id = 0
        self._stop = threading.Event()

        seen: dict[Binding, str] = {}
        for action, spec in bindings.items():
            if not spec:
                continue
            try:
                b = parse_binding(spec)
            except ValueError as exc:
                log.error("Shortcut for %s ignored: %s", action, exc)
                continue
            if b in seen:
                log.error("Shortcut %s is set for both %s and %s; %s ignored", b, seen[b], action, action)
                continue
            seen[b] = action
            if b.kind == "key" and not b.mods:
                log.warning("Shortcut %s for %s is a single key: %s", b, action,
                            "the game won't receive that key while OpenITrack runs"
                            if action not in hold_actions else "pressing it in game also triggers it")
            if b.kind == "key" and action not in hold_actions:
                self._registered[action] = b
            else:
                self._polled[action] = b

    def start(self) -> None:
        if sys.platform != "win32":
            log.warning("Global shortcuts are only supported on Windows")
            return
        if self._registered:
            ready = threading.Event()
            t = threading.Thread(target=self._message_loop, args=(ready,), daemon=True, name="hotkeys")
            t.start()
            ready.wait(2.0)
            self._threads.append(t)
        if self._polled:
            for action, b in self._polled.items():
                log.info("Shortcut %-14s -> %s", b, action)
            t = threading.Thread(target=self._poll_loop, daemon=True, name="hotkey-poll")
            t.start()
            self._threads.append(t)

    def _message_loop(self, ready: threading.Event) -> None:
        user32 = ctypes.windll.user32
        self._msg_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        ids: dict[int, str] = {}
        for i, (action, b) in enumerate(self._registered.items(), start=1):
            if user32.RegisterHotKey(None, i, b.mods | MOD_NOREPEAT, b.code):
                ids[i] = action
                log.info("Shortcut %-14s -> %s", b, action)
            else:
                log.error("Could not register %s for %s (another app already uses it); "
                          "pick another key in [hotkeys]", b, action)
        ready.set()
        msg = wt.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam in ids:
                    self.events.put(ids[msg.wParam])
        finally:
            for i in ids:
                user32.UnregisterHotKey(None, i)

    def _poll_loop(self) -> None:
        poller = InputPoller()
        was_down: dict[str, bool] = {}
        while not self._stop.wait(0.01):
            held = set()
            joy_masks: dict[int, int | None] = {}
            for action, b in self._polled.items():
                down = poller.is_down(b, joy_masks)
                if action in self._hold_actions:
                    if down:
                        held.add(action)
                elif down and not was_down.get(action, False):
                    self.events.put(action)
                was_down[action] = down
            if held != self.held:
                self.held = frozenset(held)

    def stop(self) -> None:
        self._stop.set()
        if self._msg_thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._msg_thread_id, WM_QUIT, 0, 0)
        for t in self._threads:
            t.join(1.0)
        self._threads.clear()


def watch_buttons(seconds: float = 60.0) -> None:
    """Print the binding name of every mouse/joystick button pressed (for `openitrack buttons`)."""
    if sys.platform != "win32":
        print("Only supported on Windows.")
        return
    poller = InputPoller()
    prev_joy: dict[int, int] = {}
    prev_mouse: dict[str, bool] = {}
    found = [j for j in range(MAX_JOYSTICKS) if poller.joy_buttons(j) is not None]
    print(f"Joysticks/wheels/HOTAS detected: {len(found)}"
          + (f" ({', '.join(f'joy{j + 1}' for j in found)})" if found else ""))
    print(f"Press the buttons you want to use (watching for {seconds:.0f}s, Ctrl+C to stop)...")
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for name, vk in _MOUSE_BUTTONS.items():
            down = poller.vk_down(vk)
            if down and not prev_mouse.get(name):
                print(f'  "{name}"')
            prev_mouse[name] = down
        for j in found:
            mask = poller.joy_buttons(j)
            if mask is None:
                continue
            new = mask & ~prev_joy.get(j, 0)
            for bit in range(JOY_BUTTONS):
                if new & (1 << bit):
                    print(f'  "joy{j + 1}:button{bit + 1}"')
            prev_joy[j] = mask
        time.sleep(0.01)
