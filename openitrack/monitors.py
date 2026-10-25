"""Monitor layout, so the calibration overlay can go on the screen you play on.

Numbering used by `[calibration] monitor` and `openitrack monitors`:
0 is the primary monitor, 1, 2, 3, ... count the monitors from the left, and
"all" spans every monitor (the whole virtual desktop, for triple screens).
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import sys
from ctypes import wintypes
from dataclasses import dataclass

log = logging.getLogger(__name__)

FALLBACK_SIZE = (1280, 720)   # used where monitors can't be enumerated


@dataclass(frozen=True)
class Monitor:
    left: int
    top: int
    width: int
    height: int
    primary: bool = False
    name: str = ""


def _make_dpi_aware() -> None:
    """Real pixels, not scaled ones, also on secondary monitors with other scaling."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_PER_MONITOR_V2 = -4  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
_UNAWARE_GDISCALED = -5  # DPI_AWARENESS_CONTEXT_UNAWARE_GDISCALED, as the main window


def _set_thread_context(context):
    """Set this thread's DPI awareness; returns the previous context, or None if
    the call isn't available (older Windows)."""
    user32 = ctypes.windll.user32
    try:
        fn = user32.SetThreadDpiAwarenessContext
    except AttributeError:
        return None
    fn.argtypes = [ctypes.c_void_p]
    fn.restype = ctypes.c_void_p
    return fn(context)


def use_physical_pixels() -> None:
    """Make windows that *this thread* creates (calibration overlay, camera
    preview) work in real pixels on every monitor. Per thread, so the main
    window can still be scaled by Windows (see gui._dpi_aware)."""
    if sys.platform != "win32":
        return
    if _set_thread_context(_PER_MONITOR_V2) is None:
        _make_dpi_aware()


@contextlib.contextmanager
def scaled_by_windows():
    """Windows created inside this block are scaled by Windows, like the main window
    (gui._dpi_aware): the same size on every monitor, resized and redrawn correctly
    when dragged to a monitor with other scaling. For a thread that otherwise works
    in real pixels (use_physical_pixels), e.g. for the camera preview, whose OpenCV
    window doesn't handle a scaling change itself."""
    previous = _set_thread_context(_UNAWARE_GDISCALED) if sys.platform == "win32" else None
    try:
        yield
    finally:
        if previous:
            _set_thread_context(previous)


def _enumerate_windows() -> list[Monitor]:
    # Measure in real pixels whichever thread asks, then put the thread back.
    previous = _set_thread_context(_PER_MONITOR_V2)
    if previous is None:
        _make_dpi_aware()
    try:
        return _enumerate_monitors()
    finally:
        if previous:
            _set_thread_context(previous)


def _enumerate_monitors() -> list[Monitor]:
    user32 = ctypes.windll.user32

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32)]

    found: list[Monitor] = []
    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                   ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def callback(hmon, _hdc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            found.append(Monitor(r.left, r.top, r.right - r.left, r.bottom - r.top,
                                 bool(info.dwFlags & 1), info.szDevice))
        return True

    user32.EnumDisplayMonitors(None, None, proc_type(callback), 0)
    return found


def list_monitors() -> list[Monitor]:
    """All monitors, left to right (then top to bottom)."""
    monitors: list[Monitor] = []
    if sys.platform == "win32":
        try:
            monitors = _enumerate_windows()
        except Exception as exc:
            log.warning("Couldn't list monitors: %s", exc)
    if not monitors:
        monitors = [Monitor(0, 0, *FALLBACK_SIZE, primary=True)]
    return sorted(monitors, key=lambda m: (m.left, m.top))


def span(monitors: list[Monitor]) -> Monitor:
    """The bounding box of all monitors (the virtual desktop)."""
    left = min(m.left for m in monitors)
    top = min(m.top for m in monitors)
    right = max(m.left + m.width for m in monitors)
    bottom = max(m.top + m.height for m in monitors)
    return Monitor(left, top, right - left, bottom - top, name="all")


def pick(monitors: list[Monitor], setting: int | str) -> Monitor:
    """Resolve the `[calibration] monitor` setting against the monitor list."""
    if isinstance(setting, str) and setting.lower() == "all":
        return span(monitors)
    primary = next((m for m in monitors if m.primary), monitors[0])
    if setting == 0:
        return primary
    if isinstance(setting, int) and 1 <= setting <= len(monitors):
        return monitors[setting - 1]
    log.warning("calibration.monitor = %r: there is no such monitor (found %d), using the primary",
                setting, len(monitors))
    return primary
