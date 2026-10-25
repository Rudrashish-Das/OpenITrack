"""Gaze dot: a small ring drawn on top of everything where you're looking.

It is a layered Win32 popup that never takes focus and lets every click through.
A window region cuts it into a ring with a center dot, so there is nothing to
paint: Windows fills the shape with the class brush. Windows only; elsewhere it
does nothing.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

from .monitors import Monitor

log = logging.getLogger(__name__)

CLASS_NAME = "OpenITrackGazeDot"
COLOR = (76, 141, 255)      # RGB, the window's accent blue
ALPHA = 210                 # 0..255

_WS_POPUP = 0x80000000
_WS_EX = 0x8 | 0x20 | 0x80 | 0x80000 | 0x08000000   # topmost, transparent, toolwindow, layered, noactivate
_LWA_ALPHA = 0x2
_HWND_TOPMOST = -1
_SWP_NOSIZE, _SWP_NOACTIVATE, _SWP_SHOWWINDOW = 0x1, 0x10, 0x40
_SW_HIDE = 0
_PM_REMOVE = 0x1
_RGN_OR, _RGN_DIFF = 2, 4
_ERROR_CLASS_ALREADY_EXISTS = 1410

if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32")
    _kernel32 = ctypes.WinDLL("kernel32")
    _LRESULT = ctypes.c_ssize_t
    _WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class _WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR)]

    _user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.DefWindowProcW.restype = _LRESULT
    _user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    _user32.RegisterClassW.restype = wintypes.ATOM
    _user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, wintypes.BYTE,
                                                   wintypes.DWORD]
    _user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
    _user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, wintypes.UINT]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                                     wintypes.UINT, wintypes.UINT]
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _gdi32.CreateEllipticRgn.restype = wintypes.HRGN
    _gdi32.CreateRectRgn.restype = wintypes.HRGN
    _gdi32.CombineRgn.argtypes = [wintypes.HRGN, wintypes.HRGN, wintypes.HRGN, ctypes.c_int]
    _gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
    _gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
    _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    # The class (and so this callback) lives as long as the process.
    _wndproc = _WNDPROC(lambda hwnd, msg, wp, lp: _user32.DefWindowProcW(hwnd, msg, wp, lp))
    _class_registered = False


def _register_class() -> None:
    global _class_registered
    if _class_registered:
        return
    r, g, b = COLOR
    wc = _WNDCLASSW(lpfnWndProc=_wndproc, hInstance=_kernel32.GetModuleHandleW(None),
                    hbrBackground=_gdi32.CreateSolidBrush(r | g << 8 | b << 16),
                    lpszClassName=CLASS_NAME)
    if not _user32.RegisterClassW(ctypes.byref(wc)) and ctypes.get_last_error() != _ERROR_CLASS_ALREADY_EXISTS:
        raise ctypes.WinError(ctypes.get_last_error())
    _class_registered = True


def _ring_region(size: int):
    """A ring with a dot in the middle, `size` pixels across."""
    width = max(2, size // 8)
    dot = max(2, size // 10)
    mid = size // 2
    rgn = _gdi32.CreateRectRgn(0, 0, 0, 0)
    outer = _gdi32.CreateEllipticRgn(0, 0, size + 1, size + 1)
    inner = _gdi32.CreateEllipticRgn(width, width, size - width + 1, size - width + 1)
    center = _gdi32.CreateEllipticRgn(mid - dot, mid - dot, mid + dot + 1, mid + dot + 1)
    _gdi32.CombineRgn(rgn, outer, inner, _RGN_DIFF)
    _gdi32.CombineRgn(rgn, rgn, center, _RGN_OR)
    for part in (outer, inner, center):
        _gdi32.DeleteObject(part)
    return rgn


class GazeDot:
    """Call `show_at` / `hide` from the thread that created it (it pumps the
    window's messages there)."""

    def __init__(self, area: Monitor, size: int) -> None:
        self.area = area
        # Like the calibration overlay, scale with the screen height.
        self.size = max(8, round(size * area.height / 1080))
        self.hwnd = None
        self._pos: tuple[int, int] | None = None
        self._visible = False
        if sys.platform != "win32":
            log.warning("The gaze dot only works on Windows")
            return
        try:
            _register_class()
            hwnd = _user32.CreateWindowExW(_WS_EX, CLASS_NAME, "OpenITrack gaze dot", _WS_POPUP,
                                           area.left, area.top, self.size, self.size,
                                           None, None, _kernel32.GetModuleHandleW(None), None)
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError as exc:
            log.warning("Couldn't create the gaze dot: %s", exc)
            return
        _user32.SetLayeredWindowAttributes(hwnd, 0, ALPHA, _LWA_ALPHA)
        _user32.SetWindowRgn(hwnd, _ring_region(self.size), True)  # Windows owns the region now
        self.hwnd = hwnd

    def position(self, x: float, y: float) -> tuple[int, int]:
        """Window top-left for a screen point in [-1, 1] (+y = up)."""
        a = self.area
        px = a.left + (x + 1) / 2 * a.width
        py = a.top + (1 - y) / 2 * a.height
        return round(px - self.size / 2), round(py - self.size / 2)

    def show_at(self, x: float, y: float) -> None:
        if self.hwnd is None:
            return
        pos = self.position(x, y)
        if pos != self._pos or not self._visible:
            # Re-asserting topmost keeps it above windows that went topmost later.
            _user32.SetWindowPos(self.hwnd, _HWND_TOPMOST, pos[0], pos[1], 0, 0,
                                 _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW)
            self._pos, self._visible = pos, True
        self._pump()

    def hide(self) -> None:
        if self.hwnd is None:
            return
        if self._visible:
            _user32.ShowWindow(self.hwnd, _SW_HIDE)
            self._visible = False
        self._pump()

    def close(self) -> None:
        if self.hwnd is not None:
            _user32.DestroyWindow(self.hwnd)
            self.hwnd = None
            self._visible = False

    def _pump(self) -> None:
        msg = wintypes.MSG()
        while _user32.PeekMessageW(ctypes.byref(msg), self.hwnd, 0, 0, _PM_REMOVE):
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))
