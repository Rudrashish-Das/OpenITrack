"""Which program is in front, so profiles can switch with the game (Windows only)."""

from __future__ import annotations

import ctypes
import ntpath
import os
import sys
from ctypes import wintypes

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# Windows' own shell windows: never a game, so not offered in the app picker.
_SHELL = {"explorer.exe", "applicationframehost.exe", "textinputhost.exe", "shellexperiencehost.exe",
          "searchhost.exe", "startmenuexperiencehost.exe", "systemsettings.exe"}

_dlls = None


def _api():
    """Private kernel32/user32 handles, so the argtypes set here don't affect
    other modules' calls."""
    global _dlls
    if _dlls is None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                   ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        u32.GetForegroundWindow.restype = wintypes.HWND
        u32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        u32.IsWindowVisible.argtypes = (wintypes.HWND,)
        u32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        _dlls = (k32, u32)
    return _dlls


def _window_pid(hwnd) -> int:
    pid = wintypes.DWORD()
    _api()[1].GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _process_exe(pid: int) -> str | None:
    k32 = _api()[0]
    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return ntpath.basename(buf.value)
    finally:
        k32.CloseHandle(handle)


def foreground_app() -> str | None:
    """Program file name of the window in front, e.g. "DCS.exe"."""
    if sys.platform != "win32":
        return None
    hwnd = _api()[1].GetForegroundWindow()
    pid = _window_pid(hwnd) if hwnd else 0
    return _process_exe(pid) if pid else None


def running_apps() -> list[str]:
    """Programs with a visible, titled window, to pick one for a profile."""
    if sys.platform != "win32":
        return []
    u32 = _api()[1]
    pids: set[int] = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if u32.IsWindowVisible(hwnd) and u32.GetWindowTextLengthW(hwnd) > 0:
            pids.add(_window_pid(hwnd))
        return True

    u32.EnumWindows(each, 0)
    pids.discard(os.getpid())
    names = {exe for pid in pids if (exe := _process_exe(pid)) and exe.lower() not in _SHELL}
    return sorted(names, key=str.lower)
