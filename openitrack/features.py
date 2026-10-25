"""Turn face-landmarker output into head pose + gaze.

Both the webcam path (Python MediaPipe) and the phone path (MediaPipe in the
browser) feed the same functions here, so they behave identically.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

# Landmarks needed for gaze, in the order the phone sends them.
# Per eye: image-left corner, image-right corner, upper lid, lower lid, iris center.
GAZE_LANDMARKS: tuple[int, ...] = (
    33, 133, 159, 145, 468,   # subject's right eye (image left)
    362, 263, 386, 374, 473,  # subject's left eye (image right)
)


@dataclass
class Observation:
    """One tracked frame, in camera-independent units, before centering/curves."""

    t: float                 # seconds, monotonic clock of the receiving PC
    yaw: float               # degrees, + = head turned to the subject's left
    pitch: float             # degrees, + = looking up
    roll: float              # degrees, + = head tilted to the subject's left
    x: float                 # cm, + = subject moved to their left
    y: float                 # cm, + = up
    z: float                 # cm, + = toward the camera
    gaze_x: float            # iris offset, + = looking to the subject's left
    gaze_y: float            # iris offset, + = looking up
    blink: float             # 0 = eyes open, 1 = closed
    extra: dict[str, Any] = field(default_factory=dict)
    # Head rotation in camera space (3x3). None = rebuild it from yaw/pitch/roll.
    rotation: np.ndarray | None = field(default=None, repr=False, compare=False)

    @property
    def matrix(self) -> np.ndarray:
        """The head rotation, exact when known, else rebuilt from the angles."""
        if self.rotation is not None:
            return self.rotation
        return rotation_from_angles(self.yaw, self.pitch, self.roll)


def _as_matrix(m: Any) -> np.ndarray:
    a = np.asarray(m, dtype=np.float64).reshape(4, 4)
    # Accept row- or column-major: the translation (tens of cm) must end up in
    # the last column, and the bottom row is (0, 0, 0, 1).
    if np.abs(a[3, :3]).sum() > np.abs(a[:3, 3]).sum():
        a = a.T
    return a


def _rx(deg: float) -> np.ndarray:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]])


def _ry(deg: float) -> np.ndarray:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


def _rz(deg: float) -> np.ndarray:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def angles_from_rotation(r: np.ndarray) -> tuple[float, float, float]:
    """(yaw, pitch, roll) in degrees from a rotation R = Ry(yaw) * Rx(pitch) * Rz(roll).

    Positive Rx tips the face down and positive Rz tips the head top toward
    image-left (the subject's right); we report both the other way round so
    every axis is "+ = up / subject's left". Image-right is the subject's left.
    """
    pitch_rx = math.degrees(math.asin(max(-1.0, min(1.0, -r[1, 2]))))
    yaw = math.degrees(math.atan2(r[0, 2], r[2, 2]))
    roll = math.degrees(math.atan2(r[1, 0], r[1, 1]))
    return yaw, -pitch_rx, -roll


def rotation_from_angles(yaw: float, pitch: float, roll: float) -> np.ndarray:
    """Inverse of angles_from_rotation."""
    return _ry(yaw) @ _rx(-pitch) @ _rz(-roll)


def head_pose(matrix: Any) -> tuple[float, float, float, float, float, float]:
    """Return (yaw, pitch, roll, x, y, z) from MediaPipe's facial transformation matrix.

    MediaPipe uses a right-handed camera space: X image-right, Y up, Z toward
    the viewer. Decomposed as R = Ry(yaw) * Rx(pitch) * Rz(roll).
    """
    a = _as_matrix(matrix)
    yaw, pitch, roll = angles_from_rotation(_rotation(a))
    tx, ty, tz = (float(v) for v in a[:3, 3])
    return yaw, pitch, roll, tx, ty, tz


def _rotation(a: np.ndarray) -> np.ndarray:
    """The 3x3 rotation of a 4x4 transform, with any scale removed so the angle math is exact."""
    r = a[:3, :3]
    scale = np.linalg.norm(r, axis=0)
    scale[scale == 0] = 1.0
    return r / scale


def relative_pose(center: Observation, obs: Observation) -> dict[str, float]:
    """Head motion since `center`, measured in the neutral head frame.

    Rotations are composed (R_center^T * R_now) instead of subtracting Euler
    angles, and the position change is rotated into the same frame. That makes
    the result independent of where the camera sits or how it is tilted: turning
    the head is pure yaw, nodding is pure pitch and leaning toward the screen
    only changes z, for a camera above, below or beside the monitor.
    """
    rc = center.matrix
    yaw, pitch, roll = angles_from_rotation(rc.T @ obs.matrix)
    x, y, z = rc.T @ np.array([obs.x - center.x, obs.y - center.y, obs.z - center.z])
    return {"yaw": yaw, "pitch": pitch, "roll": roll, "x": float(x), "y": float(y), "z": float(z)}


def _eye_gaze(pts: np.ndarray) -> tuple[float, float, float]:
    """pts: 5x2 pixel coords [left corner, right corner, upper lid, lower lid, iris].

    Returns (horizontal, vertical, openness), offsets normalised by eye width.
    """
    a, b, up, lo, iris = pts
    axis = b - a
    width = float(np.linalg.norm(axis))
    if width < 1e-6:
        return 0.0, 0.0, 0.0
    u = axis / width
    n = np.array([u[1], -u[0]])  # perpendicular, pointing image-up (image y grows down)
    mid = (a + b) / 2.0
    rel = iris - mid
    horiz = float(rel @ u) / width
    vert = float(rel @ n) / width
    openness = float(np.linalg.norm(up - lo)) / width
    return horiz, vert, openness


def gaze(landmarks: Any, width: float, height: float) -> tuple[float, float, float]:
    """Iris offset from GAZE_LANDMARKS-ordered points (normalised image coords).

    Returns (gaze_x, gaze_y, openness): + x = toward image right = subject's left.
    """
    p = np.asarray(landmarks, dtype=np.float64)[:, :2] * np.array([width, height])
    rh, rv, ro = _eye_gaze(p[0:5])
    lh, lv, lo = _eye_gaze(p[5:10])
    return (rh + lh) / 2.0, (rv + lv) / 2.0, (ro + lo) / 2.0


def blink_from_blendshapes(blendshapes: dict[str, float] | None, openness: float) -> float:
    if blendshapes:
        left = blendshapes.get("eyeBlinkLeft")
        right = blendshapes.get("eyeBlinkRight")
        if left is not None and right is not None:
            return (left + right) / 2.0
    # Fallback: lid gap relative to eye width is ~0.3 open, <0.1 closed.
    return float(min(1.0, max(0.0, (0.25 - openness) / 0.17)))


def median_observation(observations: list[Observation]) -> Observation:
    """Per-field median: a noise-robust single pose from a short burst of frames.

    The rotation is rebuilt from the median angles. The frames are of a still
    head, so the angles are close together and their median is a good average.
    """
    if not observations:
        raise ValueError("no observations")
    names = ("yaw", "pitch", "roll", "x", "y", "z", "gaze_x", "gaze_y", "blink")
    med = {n: float(np.median([getattr(o, n) for o in observations])) for n in names}
    return Observation(t=observations[-1].t, extra=dict(observations[-1].extra), **med)


def build_observation(
    t: float,
    matrix: Any,
    gaze_landmarks: Any,
    width: float,
    height: float,
    blendshapes: dict[str, float] | None = None,
) -> Observation:
    a = _as_matrix(matrix)
    r = _rotation(a)
    yaw, pitch, roll = angles_from_rotation(r)
    x, y, z = (float(v) for v in a[:3, 3])
    gx, gy, openness = gaze(gaze_landmarks, width, height)
    return Observation(
        t=t, yaw=yaw, pitch=pitch, roll=roll, x=x, y=y, z=z,
        gaze_x=gx, gaze_y=gy, blink=blink_from_blendshapes(blendshapes, openness),
        rotation=r,
    )
