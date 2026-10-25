"""Where on screen you're looking: eye map, head geometry and the calibration fit.

Two parts. The eye map turns the centered iris offset into the point the eyes
alone point at, as if the head were still in its center pose. It is a
least-squares fit to the 9 calibration dots, or, without one, a straight scale
from the per-direction eye ranges. The head geometry then adds the head turn
and the head's position: the line of sight starts where the head is now and
the flat screen turns its angle into a point.

Screen points are (x, y) in [-1, 1], +x = right, +y = up.
"""

from __future__ import annotations

import math
from typing import NamedTuple

import numpy as np

# Calibration dots sit this far from the screen center toward each edge.
TARGET_REACH = 0.9
DEFAULT_DISTANCE_CM = 60.0
_DISTANCE_RANGE_CM = (15.0, 300.0)   # outside this, the face model's distance is not believable
_FIT_MIN_POINTS = 5                  # fewer and the fit falls back to the eye ranges
_QUADRATIC_MIN_POINTS = 8            # enough to also fit curvature and the corners
_RIDGE = 1e-2                        # keeps the fit solvable when dots line up
_INPUT_LIMIT = 1.5                   # don't extrapolate the curve far past the dots


class Head(NamedTuple):
    """Head pose relative to the center pose: degrees (+ = subject's left / up)
    and cm (+ = subject's left / up / toward the camera)."""

    yaw: float = 0.0
    pitch: float = 0.0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


def viewing_distance(center_z: float) -> float:
    """Eye-to-screen distance in cm from the center pose (the camera sits by the screen)."""
    d = -center_z
    lo, hi = _DISTANCE_RANGE_CM
    return d if lo <= d <= hi else DEFAULT_DISTANCE_CM


def _half_tans(screen_degrees: float, aspect: float) -> tuple[float, float]:
    tan_h = math.tan(math.radians(screen_degrees / 2))
    return tan_h, tan_h / aspect


def _axes(head: Head, screen_degrees: float, aspect: float):
    """Per screen axis (+x = right, +y = up): head angle, head shift (cm), tan of the half-screen."""
    tan_h, tan_v = _half_tans(screen_degrees, aspect)
    # + yaw and + x are the subject's left, which is screen left.
    return ((-head.yaw, -head.x, tan_h), (head.pitch, head.y, tan_v))


def _eye_distance(head: Head, distance: float) -> float:
    return max(0.3 * distance, distance - head.z)


def screen_point(eye: tuple[float, float], head: Head, distance: float,
                 screen_degrees: float, aspect: float) -> tuple[float, float]:
    """Where on screen you're looking.

    eye: the eye map's point (eyes alone, head at center). head: the head pose
    relative to center (Head() to ignore it). distance: eye-to-screen at center,
    cm. screen_degrees: how wide the screen looks from the seat; aspect: its
    width / height. The line of sight is the head turn plus the eye turn,
    starting from where the head is now."""
    dist = _eye_distance(head, distance)
    point = []
    for e, (angle, shift, tan_half) in zip(eye, _axes(head, screen_degrees, aspect)):
        a = max(-89.0, min(89.0, angle + math.degrees(math.atan(e * tan_half))))
        pos = (shift + dist * math.tan(math.radians(a))) / (distance * tan_half)
        point.append(max(-1.0, min(1.0, pos)))
    return point[0], point[1]


def eye_point_for(target: tuple[float, float], head: Head, distance: float,
                  screen_degrees: float, aspect: float) -> tuple[float, float]:
    """The eye-map point that makes `screen_point` land on `target` with this head
    pose: what the eyes were really doing while you looked at a calibration dot."""
    dist = _eye_distance(head, distance)
    point = []
    for t, (angle, shift, tan_half) in zip(target, _axes(head, screen_degrees, aspect)):
        total = math.degrees(math.atan((t * distance * tan_half - shift) / dist))
        eye = max(-89.0, min(89.0, total - angle))
        point.append(math.tan(math.radians(eye)) / tan_half)
    return point[0], point[1]


def _features(u: float, v: float, terms: int) -> list[float]:
    f = [1.0, u, v]
    if terms > 3:
        f += [u * v, u * u, v * v]
    return f


def fit_eye_map(points: list[tuple[float, float, float, float]]) -> dict | None:
    """Least-squares eye map from (gaze_x, gaze_y, eye_x, eye_y) calibration points.

    Quadratic in the iris offset (so corners and uneven eye travel fit) with
    enough dots, linear with fewer, None with too few. Inputs are scaled to
    about [-1, 1] by the largest offset seen, so the small ridge penalty treats
    both axes alike. The result is plain lists, ready for calibration.json."""
    if len(points) < _FIT_MIN_POINTS:
        return None
    p = np.asarray(points, dtype=np.float64)
    scale = [max(float(np.abs(p[:, i]).max()), 1e-3) for i in (0, 1)]
    terms = 6 if len(p) >= _QUADRATIC_MIN_POINTS else 3
    a = np.array([_features(gx / scale[0], gy / scale[1], terms) for gx, gy in p[:, :2]])
    ridge = _RIDGE * np.eye(terms)
    ridge[0, 0] = 0.0  # don't pull the offset toward 0
    coef = np.linalg.solve(a.T @ a + ridge, a.T @ p[:, 2:])
    residual = a @ coef - p[:, 2:]
    return {
        "scale": scale,
        "x": coef[:, 0].tolist(),
        "y": coef[:, 1].tolist(),
        "points": len(p),
        "rms": float(np.sqrt(np.mean(residual ** 2))),
    }


def eye_point(gx: float, gy: float, ranges: dict[str, float], fit: dict | None = None) -> tuple[float, float]:
    """The eye map: centered iris offset -> screen point with the head at center.

    fit: from `fit_eye_map`. Without one, `ranges` (the offset that reaches each
    calibration dot) scale each direction straight to its dot."""
    if fit:
        sx, sy = fit["scale"]
        u = max(-_INPUT_LIMIT, min(_INPUT_LIMIT, gx / sx))
        v = max(-_INPUT_LIMIT, min(_INPUT_LIMIT, gy / sy))
        f = _features(u, v, len(fit["x"]))
        return (sum(c * k for c, k in zip(fit["x"], f)),
                sum(c * k for c, k in zip(fit["y"], f)))
    # + gaze_x is the subject's left, which is screen left.
    ex = -gx / ranges["left" if gx > 0 else "right"] * TARGET_REACH
    ey = gy / ranges["up" if gy > 0 else "down"] * TARGET_REACH
    return ex, ey
