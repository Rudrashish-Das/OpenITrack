"""Optional tuning window: camera feed (webcam mode) with landmarks, live values
and per-axis output bars. Closing it (or pressing Esc) just hides the preview."""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

import cv2
import numpy as np

from .features import GAZE_LANDMARKS
from .mapping import AXES, Pose
from .monitors import scaled_by_windows

WINDOW = "OpenITrack preview"
ICON = Path(__file__).resolve().parent / "assets" / "icon.ico"
_GREEN = (120, 220, 120)
_GREY = (150, 150, 150)
_AMBER = (60, 180, 240)
PANEL_WIDTH, PANEL_HEIGHT = 301, 30 + 22 * 9 + 1   # pixels, top-left corner
PANEL_KEEP = 0.45   # share of the camera image left showing through the panel


class Preview:
    def __init__(self) -> None:
        # The tracking thread works in real pixels (for the calibration overlay), but
        # OpenCV's window can't follow a move to a monitor with other scaling: Windows
        # would resize it and the image would stay small in a corner. Scaled by
        # Windows instead, it behaves like the main window on every monitor.
        with scaled_by_windows():
            cv2.namedWindow(WINDOW, cv2.WINDOW_AUTOSIZE)
            self._icons = _set_icon(WINDOW, ICON)
        self.open = True

    def close(self) -> None:
        if self.open:
            cv2.destroyWindow(WINDOW)
            self.open = False
            _free_icons(self._icons)
            self._icons = []

    def show(self, frame, landmarks, pose: Pose, raw: dict | None, status: str, flags: str) -> None:
        if not self.open:
            return
        if frame is None:
            canvas = np.zeros((360, 480, 3), np.uint8)
        else:
            canvas = cv2.flip(frame, 1)  # mirror so it feels like a mirror
            h, w = canvas.shape[:2]
            if landmarks is not None:
                pts = landmarks.copy()
                pts[:, 0] = 1.0 - pts[:, 0]
                for x, y in (pts[::4] * (w, h)).astype(int):
                    cv2.circle(canvas, (int(x), int(y)), 1, _GREY, -1, cv2.LINE_AA)
                for i in GAZE_LANDMARKS:
                    x, y = (pts[i] * (w, h)).astype(int)
                    cv2.circle(canvas, (int(x), int(y)), 2, _GREEN, -1, cv2.LINE_AA)

        self._panel(canvas, pose, raw, status, flags)
        cv2.imshow(WINDOW, canvas)
        key = cv2.waitKey(1) & 0xFF
        visible = cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) >= 1
        if key == 27 or not visible:
            self.close()

    @staticmethod
    def _panel(img, pose: Pose, raw: dict | None, status: str, flags: str) -> None:
        h, w = img.shape[:2]
        # Darken just the panel area (the same as blending in a 55% black box),
        # without copying and blending the whole frame.
        pw = min(w, PANEL_WIDTH)
        img[:PANEL_HEIGHT, :pw] = cv2.convertScaleAbs(img[:PANEL_HEIGHT, :pw], alpha=PANEL_KEEP)
        put = lambda text, y, color=(235, 235, 235): cv2.putText(  # noqa: E731
            img, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
        put(f"{status}  {flags}", 20, _AMBER)
        limits = {"yaw": 180, "pitch": 90, "roll": 90, "x": 30, "y": 30, "z": 30}
        for row, axis in enumerate(AXES):
            y = 44 + row * 22
            val = getattr(pose, axis)
            src = "" if raw is None else f"in {raw[axis]:+6.1f}"
            put(f"{axis:>5} {val:+7.1f}  {src}", y)
            cx, half = 255, 35
            frac = max(-1.0, min(1.0, val / limits[axis]))
            cv2.line(img, (cx - half, y - 4), (cx + half, y - 4), _GREY, 1)
            cv2.line(img, (cx, y - 4), (cx + int(frac * half), y - 4), _GREEN, 3)
        if raw is not None:
            put(f" gaze  x {raw['gx']:+.2f}  y {raw['gy']:+.2f}", 44 + 6 * 22)


# -- window icon (Windows) ----------------------------------------------------------
_WM_SETICON = 0x0080
_ICON_SMALL, _ICON_BIG = 0, 1
_IMAGE_ICON, _LR_LOADFROMFILE = 1, 0x0010
_SM_CXICON, _SM_CXSMICON = 11, 49


def _set_icon(title: str, ico: Path) -> list:
    """Give the OpenCV window titled `title` our icon instead of OpenCV's (title bar
    and Alt+Tab). Returns the icon handles, to free when the window closes."""
    if sys.platform != "win32" or not ico.is_file():
        return []
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        return []
    icons = []
    for which, metric in ((_ICON_SMALL, _SM_CXSMICON), (_ICON_BIG, _SM_CXICON)):
        size = user32.GetSystemMetrics(metric)  # this thread's scaling: the window's
        handle = user32.LoadImageW(None, str(ico), _IMAGE_ICON, size, size, _LR_LOADFROMFILE)
        if handle:
            user32.SendMessageW(hwnd, _WM_SETICON, which, handle)
            icons.append(handle)
    return icons


def _free_icons(icons: list) -> None:
    for handle in icons:
        ctypes.windll.user32.DestroyIcon(ctypes.c_void_p(handle))
