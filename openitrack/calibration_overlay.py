"""Fullscreen, always-on-top window that guides the user through calibration.

Works over desktop apps and borderless/windowed games. Exclusive-fullscreen
games hide other windows, so switch those to borderless while calibrating.
"""

from __future__ import annotations

import ctypes
import math
import sys

import cv2
import numpy as np

from .calibration import STEPS, TARGETS, CalibrationSession
from .monitors import list_monitors, pick

WINDOW = "OpenITrack calibration"
BG = (24, 20, 16)
FG = (240, 235, 231)
MUTED = (163, 149, 139)
ACCENT = (255, 141, 76)
GOOD = (142, 207, 62)
WARN = (75, 184, 242)
FONT = cv2.FONT_HERSHEY_DUPLEX


def _borderless(x: int, y: int, w: int, h: int) -> bool:
    """Turn the OpenCV window into a topmost borderless popup covering (x, y, w, h).

    Fullscreen mode only ever fills one monitor, so spanning several needs this.
    """
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p
    hwnd = user32.FindWindowW(None, WINDOW)
    if not hwnd:
        return False
    gwl_style, ws_popup, ws_visible = -16, 0x80000000, 0x10000000
    hwnd_topmost, swp_framechanged, swp_showwindow = -1, 0x0020, 0x0040
    user32.SetWindowLongW(ctypes.c_void_p(hwnd), gwl_style, ctypes.c_long(ws_popup | ws_visible))
    user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(hwnd_topmost), x, y, w, h,
                        swp_framechanged | swp_showwindow)
    return True


def _text(img, text, center_x, y, scale, color, thickness=1):
    (w, _), _ = cv2.getTextSize(text, FONT, scale, thickness)
    cv2.putText(img, text, (int(center_x - w / 2), int(y)), FONT, scale, color, thickness, cv2.LINE_AA)


class CalibrationOverlay:
    def __init__(self, size: tuple[int, int] | None = None, show: bool = True,
                 monitor: int | str = 0) -> None:
        """monitor: 0 = primary, 1.. = counted from the left, "all" = every monitor."""
        area = pick(list_monitors(), monitor)
        self.w, self.h = size or (area.width, area.height)
        # Scale text to one screen's height, also when spanning several.
        self._scale = self.h / 1080.0
        self.escape_pressed = False
        self.open = show
        if not show:
            return
        cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
        if area.name == "all":
            cv2.resizeWindow(WINDOW, self.w, self.h)
            cv2.moveWindow(WINDOW, area.left, area.top)
            cv2.imshow(WINDOW, np.full((self.h, self.w, 3), BG, np.uint8))
            cv2.waitKey(1)
            _borderless(area.left, area.top, self.w, self.h)
        else:
            # Fullscreen fills the monitor the window is on, so put it there first.
            cv2.resizeWindow(WINDOW, max(1, area.width // 2), max(1, area.height // 2))
            cv2.moveWindow(WINDOW, area.left + area.width // 4, area.top + area.height // 4)
            cv2.setWindowProperty(WINDOW, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        try:
            cv2.setWindowProperty(WINDOW, cv2.WND_PROP_TOPMOST, 1)
        except cv2.error:
            pass

    def close(self) -> None:
        if self.open:
            cv2.destroyWindow(WINDOW)
            self.open = False

    def render(self, s: CalibrationSession, now: float, cancel_key: str) -> None:
        if not self.open:
            return
        cv2.imshow(WINDOW, self.draw(s, now, cancel_key))
        if cv2.waitKey(1) & 0xFF == 27:
            self.escape_pressed = True

    def draw(self, s: CalibrationSession, now: float, cancel_key: str) -> np.ndarray:
        img = np.full((self.h, self.w, 3), BG, np.uint8)
        k = self._scale
        cx, cy = self.w / 2, self.h / 2
        step = s.step

        # Progress bar along the top.
        cv2.rectangle(img, (0, 0), (int(self.w * s.progress()), max(4, int(6 * k))), ACCENT, -1)
        _text(img, f"Esc or {cancel_key} to cancel", cx, self.h - 30 * k, 0.6 * k, MUTED)
        if step is not None and s.phase not in ("intro", "done", "failed"):
            _text(img, f"Step {s.index + 1} of {len(STEPS)}", cx, 50 * k, 0.7 * k, MUTED)

        if s.phase == "intro":
            if s.live is None:
                _text(img, "Looking for your face...", cx, cy - 40 * k, 1.4 * k, WARN, 2)
                _text(img, "Sit in front of the camera with your face well lit.", cx, cy + 20 * k, 0.8 * k, MUTED)
            else:
                remaining = max(0, math.ceil(s.INTRO_SECONDS - (now - s.phase_t)))
                _text(img, "Calibration", cx, cy - 120 * k, 1.8 * k, FG, 2)
                _text(img, "Follow the instructions. It takes about 40 seconds.", cx, cy - 50 * k, 0.9 * k, MUTED)
                _text(img, f"Starting in {remaining}", cx, cy + 40 * k, 1.3 * k, ACCENT, 2)
        elif s.phase == "message":
            _text(img, s.message, cx, cy, 1.1 * k, GOOD if s.message_ok else WARN, 2)
        elif s.phase == "done":
            _text(img, "Calibration saved", cx, cy - 40 * k, 1.8 * k, GOOD, 2)
            _text(img, s.result.summary(), cx, cy + 30 * k, 0.7 * k, MUTED)
        elif s.phase == "failed":
            _text(img, s.message or "Calibration stopped", cx, cy, 1.4 * k, WARN, 2)
        elif step is not None:
            collecting = s.phase == "collect"
            if step.kind in ("center", "gaze"):
                tx, ty = TARGETS[step.direction]
                px, py = cx + tx * cx, cy - ty * cy
                self._target(img, px, py, now, collecting, s, step)
                ty_text = cy + 170 * k if step.direction != "down" else cy - 170 * k
                _text(img, step.title, cx, ty_text, 1.3 * k, FG, 2)
                _text(img, step.hint, cx, ty_text + 50 * k, 0.8 * k, MUTED)
            else:
                self._head_step(img, s, step, collecting)
        return img

    def _target(self, img, px, py, now, collecting, s, step) -> None:
        k = self._scale
        pulse = 0.5 + 0.5 * math.sin(now * 6.0)
        r = int((14 + 10 * pulse) * k) if not collecting else int(12 * k)
        color = GOOD if collecting else ACCENT
        cv2.circle(img, (int(px), int(py)), int(34 * k), color, max(1, int(2 * k)), cv2.LINE_AA)
        cv2.circle(img, (int(px), int(py)), r, color, -1, cv2.LINE_AA)
        cv2.circle(img, (int(px), int(py)), max(2, int(4 * k)), BG, -1, cv2.LINE_AA)
        if collecting:
            frac = min(1.0, (now - s.phase_t) / step.collect)
            cv2.ellipse(img, (int(px), int(py)), (int(46 * k), int(46 * k)), -90, 0, 360 * frac,
                        FG, max(2, int(3 * k)), cv2.LINE_AA)

    def _head_step(self, img, s, step, collecting) -> None:
        k = self._scale
        cx, cy = self.w / 2, self.h / 2
        vec = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}[step.direction]
        start = (int(cx - vec[0] * 60 * k), int(cy - vec[1] * 60 * k))
        end = (int(cx + vec[0] * 200 * k), int(cy + vec[1] * 200 * k))
        cv2.arrowedLine(img, start, end, GOOD if collecting else ACCENT, max(3, int(10 * k)),
                        cv2.LINE_AA, tipLength=0.3)
        _text(img, step.title, cx, cy + 300 * k, 1.3 * k, FG, 2)
        _text(img, step.hint, cx, cy + 350 * k, 0.8 * k, MUTED)
        off = s.live_offset()
        if off is not None:
            value = off[0] if step.direction in ("left", "right") else off[1]
            sign = 1 if step.direction in ("left", "up") else -1
            _text(img, f"{max(0.0, value * sign):.0f} deg", cx, cy - 260 * k, 1.2 * k,
                  GOOD if collecting else MUTED, 2)
