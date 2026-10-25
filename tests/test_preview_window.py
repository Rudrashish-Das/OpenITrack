"""The camera preview is scaled by Windows, so it looks right on every monitor."""

import ctypes
import sys
import threading

import numpy as np
import pytest

from openitrack import monitors, preview
from openitrack.mapping import Pose

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows DPI handling")

UNAWARE, PER_MONITOR = 0, 2  # DPI_AWARENESS values


def _awareness(context) -> int:
    fn = ctypes.windll.user32.GetAwarenessFromDpiAwarenessContext
    fn.argtypes = [ctypes.c_void_p]
    return fn(context)


def _window_awareness(hwnd) -> int:
    fn = ctypes.windll.user32.GetWindowDpiAwarenessContext
    fn.argtypes, fn.restype = [ctypes.c_void_p], ctypes.c_void_p
    return _awareness(fn(hwnd))


def _window_icon(hwnd, which):
    send = ctypes.windll.user32.SendMessageW
    send.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    send.restype = ctypes.c_void_p
    return send(hwnd, 0x007F, which, 0)  # WM_GETICON


def _thread_awareness() -> int:
    fn = ctypes.windll.user32.GetThreadDpiAwarenessContext
    fn.restype = ctypes.c_void_p
    return _awareness(fn())


def test_preview_window_is_scaled_by_windows_on_a_real_pixels_thread():
    seen = {}

    def tracking_thread():
        monitors.use_physical_pixels()  # as Engine.run does, for the calibration overlay
        p = preview.Preview()
        try:
            p.show(np.zeros((48, 64, 3), np.uint8), None, Pose(), None, "test", "")
            find = ctypes.windll.user32.FindWindowW
            find.restype = ctypes.c_void_p
            hwnd = find(None, preview.WINDOW)
            seen["window"] = _window_awareness(hwnd) if hwnd else None
            seen["icons"] = [_window_icon(hwnd, which) for which in (0, 1)]  # small, big
            seen["ours"] = list(p._icons)
            seen["thread after"] = _thread_awareness()
        finally:
            p.close()

    t = threading.Thread(target=tracking_thread)
    t.start()
    t.join(30)
    # The preview: scaled by Windows, so dragging it to a monitor with other scaling
    # can't leave the image small in a corner of a resized window.
    assert seen["window"] == UNAWARE
    # The thread itself stays in real pixels for windows it makes later (the overlay).
    assert seen["thread after"] == PER_MONITOR
    # OpenITrack's icon in the title bar and Alt+Tab, not OpenCV's.
    assert len(seen["ours"]) == 2 and seen["icons"] == seen["ours"]


def test_scaled_by_windows_restores_the_thread_even_on_error():
    def thread():
        monitors.use_physical_pixels()
        with pytest.raises(RuntimeError), monitors.scaled_by_windows():
            assert _thread_awareness() == UNAWARE
            raise RuntimeError
        results.append(_thread_awareness())

    results = []
    t = threading.Thread(target=thread)
    t.start()
    t.join(10)
    assert results == [PER_MONITOR]
