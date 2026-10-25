"""Observation -> game pose: centering, filtering, eye-assist, response curves,
face-loss handling, and the shortcut-driven modes (pause, freeze, center hold,
axis toggles, sensitivity)."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace

from .calibration import Calibration
from .config import AxisConfig, Config
from .features import Observation, median_observation, relative_pose
from .filters import OneEuroFilter
from .gaze_model import Head, eye_point, screen_point, viewing_distance

log = logging.getLogger(__name__)

AXES = ("yaw", "pitch", "roll", "x", "y", "z")
_ROTATION_LIMITS = {"yaw": 180.0, "pitch": 90.0, "roll": 180.0}
RECENTER_FRAMES = 5           # median of this many frames becomes the new center
# A head can't roll this far; past it the camera itself was turned (e.g. a phone
# flipped 180 deg on a browser that misreports its screen angle).
FLIPPED_ROLL_DEG = 120.0
SENSITIVITY_STEP = 1.1
SENSITIVITY_RANGE = (0.3, 3.0)
# Seconds to glide into each output mode, instead of snapping.
_BLEND_SECONDS = {"live": 0.25, "zero": 0.35, "frozen": 0.08, "center": 0.12}
_TOGGLE_SECONDS = 0.25


@dataclass
class Pose:
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def as_tuple(self) -> tuple[float, float, float, float, float, float]:
        return (self.yaw, self.pitch, self.roll, self.x, self.y, self.z)

    def scaled(self, k: float) -> "Pose":
        return Pose(*(v * k for v in self.as_tuple()))

    def lerp(self, other: "Pose", k: float) -> "Pose":
        return Pose(*(a + (b - a) * k for a, b in zip(self.as_tuple(), other.as_tuple())))


def apply_curve(value: float, ax: AxisConfig) -> float:
    """Deadzone + power curve, saturating at ax.output."""
    if not ax.enabled:
        return 0.0
    mag = abs(value) - ax.deadzone
    if mag <= 0:
        return 0.0
    u = min(1.0, mag / (ax.input_range - ax.deadzone))
    out = math.copysign(ax.output * (u ** ax.exponent), value)
    return -out if ax.invert else out


def gaze_curve(value: float, full_range: float, deadzone: float, exponent: float) -> float:
    """Normalised iris offset -> [-1, 1] with a deadzone around center."""
    u = min(1.0, abs(value) / full_range)
    if u <= deadzone:
        return 0.0
    return math.copysign(((u - deadzone) / (1.0 - deadzone)) ** exponent, value)


def orientation_changed(was: object, new: object) -> bool:
    """Phone orientation keys are a screen angle (int) or, from older phone pages
    and calibration files, "portrait"/"landscape". Only compare like with like:
    a mix of the two styles is unknown, not a change."""
    if was is None or new is None or isinstance(was, str) != isinstance(new, str):
        return False
    return was != new


def _smoothstep(k: float) -> float:
    k = max(0.0, min(1.0, k))
    return k * k * (3 - 2 * k)


class PosePipeline:
    def __init__(self, config: Config, calibration: Calibration | None = None):
        self.config = config
        self.calibration = calibration
        self._center: Observation | None = None
        self._rel_cache: tuple[Observation, Observation, dict[str, float]] | None = None
        self._center_buf: list[Observation] = []
        self._filters: dict[str, OneEuroFilter] = {}
        self._held_gaze = (0.0, 0.0)
        self._head = Head()           # filtered head pose, for the gaze dot
        self._last_pose = Pose()
        self._last_seen: float | None = None
        self._lost_from: Pose | None = None
        self.needs_center = config.tracking.auto_center

        # Shortcut-driven state.
        self.paused = False
        self.gaze_enabled = True
        self.translation_enabled = True
        self.roll_enabled = True
        self.sensitivity = 1.0
        self._weights = {"translation": 1.0, "roll": 1.0}
        self._mode = "live"
        self._mode_from = Pose()
        self._mode_t = -math.inf
        self._frozen = Pose()
        self._out = Pose()
        self._last_t: float | None = None

        self._build_filters()
        self._resolve_axes()
        if calibration is not None and calibration.center_observation() is not None and not self.needs_center:
            self.set_center(calibration.center_observation())

    # -- configuration -----------------------------------------------------
    def set_config(self, config: Config) -> None:
        self.config = config
        self._build_filters()
        self._resolve_axes()

    def set_calibration(self, calibration: Calibration | None) -> None:
        self.calibration = calibration
        self._resolve_axes()

    def _build_filters(self) -> None:
        f = self.config.filter
        params = {
            "yaw": (f.head_min_cutoff, f.head_beta),
            "pitch": (f.head_min_cutoff, f.head_beta),
            "roll": (f.head_min_cutoff, f.head_beta),
            "x": (f.position_min_cutoff, f.position_beta),
            "y": (f.position_min_cutoff, f.position_beta),
            "z": (f.position_min_cutoff, f.position_beta),
            "gx": (f.gaze_min_cutoff, f.gaze_beta),
            "gy": (f.gaze_min_cutoff, f.gaze_beta),
        }
        for name, (mc, beta) in params.items():
            flt = self._filters.get(name)
            if flt is None:
                self._filters[name] = OneEuroFilter(mc, beta)
            else:  # keep filter state so a live reload doesn't jump
                flt.min_cutoff, flt.beta = mc, beta

    def _resolve_axes(self) -> None:
        """Axis curves with calibrated head ranges substituted where available."""
        axes = {a: getattr(self.config.axes, a) for a in AXES}
        cal = self.calibration
        if cal is not None and self.config.calibration.use_head:
            for axis, rng in (("yaw", cal.head_yaw_range), ("pitch", cal.head_pitch_range)):
                if rng:
                    axes[axis] = replace(axes[axis], input_range=max(rng, axes[axis].deadzone + 1.0))
        self._axes = axes

        g = self.config.gaze
        h, v = g.horizontal_range, g.vertical_range
        ranges = {"left": h, "right": h, "up": v, "down": v}
        if cal is not None and self.config.calibration.use_gaze:
            for d in ranges:
                measured = getattr(cal, f"gaze_{d}")
                if measured:
                    ranges[d] = measured
        self._gaze_ranges = ranges
        self._gaze_fit = cal.gaze_fit if cal is not None and self.config.calibration.use_gaze else None

    # -- commands ----------------------------------------------------------
    def recenter(self) -> None:
        self.needs_center = True
        self._center_buf = []

    def set_center(self, obs: Observation) -> None:
        # Every frame is measured against the center, so build its rotation once
        # (a median or calibration center only has angles).
        self._center = obs if obs.rotation is not None else replace(obs, rotation=obs.matrix)
        self.needs_center = False
        self._center_buf = []
        for flt in self._filters.values():
            flt.reset(0.0)
        self._held_gaze = (0.0, 0.0)
        self._head = (0.0, 0.0)

    def toggle_pause(self) -> bool:
        self.paused = not self.paused
        return self.paused

    def toggle_gaze(self) -> bool:
        self.gaze_enabled = not self.gaze_enabled
        return self.gaze_enabled

    def toggle_translation(self) -> bool:
        self.translation_enabled = not self.translation_enabled
        return self.translation_enabled

    def toggle_roll(self) -> bool:
        self.roll_enabled = not self.roll_enabled
        return self.roll_enabled

    def change_sensitivity(self, up: bool) -> float:
        lo, hi = SENSITIVITY_RANGE
        k = self.sensitivity * (SENSITIVITY_STEP if up else 1 / SENSITIVITY_STEP)
        self.sensitivity = round(max(lo, min(hi, k)), 3)
        return self.sensitivity

    @property
    def centered(self) -> bool:
        return self._center is not None

    @property
    def center(self) -> Observation | None:
        return self._center

    # -- processing --------------------------------------------------------
    def _relative(self, obs: Observation) -> dict[str, float]:
        """Pose relative to the center. Several steps (flip check, tracking,
        preview) ask about the same frame, so the last answer is reused."""
        c = self._center
        assert c is not None
        cached = self._rel_cache
        if cached is not None and cached[0] is c and cached[1] is obs:
            return cached[2]
        rel = relative_pose(c, obs)
        rel["gx"] = obs.gaze_x - c.gaze_x
        rel["gy"] = obs.gaze_y - c.gaze_y
        self._rel_cache = (c, obs, rel)
        return rel

    def update(self, obs: Observation | None, now: float, *, freeze: bool = False,
               center_hold: bool = False, calibrating: bool = False) -> Pose:
        """Feed one observation (None when no face is visible); returns the pose to send."""
        if obs is not None and self._center is not None and not self.needs_center:
            was = self._center.extra.get("orientation")
            new = obs.extra.get("orientation")
            if orientation_changed(was, new):
                # The phone was turned: the camera image is rotated, so the old
                # center means nothing.
                log.info("Camera orientation changed to %s, recentering", new)
                self.recenter()
            elif abs(self._relative(obs)["roll"]) > FLIPPED_ROLL_DEG:
                log.info("Camera turned upside down, recentering")
                self.recenter()
        if obs is not None and self.needs_center:
            self._center_buf.append(obs)
            if len(self._center_buf) >= RECENTER_FRAMES:
                self.set_center(median_observation(self._center_buf))

        if obs is not None and self._center is not None:
            pose = self._track(obs)
            self._last_seen = now
            self._lost_from = None
        else:
            pose = self._lost(now)
        self._last_pose = pose

        dt = 0.0 if self._last_t is None else max(0.0, min(0.1, now - self._last_t))
        self._last_t = now
        live = self._modifiers(pose, dt)

        if self.paused or calibrating:
            mode = "zero"
        elif center_hold:
            mode = "center"
        elif freeze:
            mode = "frozen"
        else:
            mode = "live"
        if mode != self._mode:
            if mode == "frozen":
                self._frozen = self._out
            self._mode, self._mode_from, self._mode_t = mode, self._out, now
        target = live if mode == "live" else self._frozen if mode == "frozen" else Pose()
        k = _smoothstep((now - self._mode_t) / _BLEND_SECONDS[mode])
        self._out = self._mode_from.lerp(target, k)
        return self._out

    def _track(self, obs: Observation) -> Pose:
        cfg = self.config
        rel = self._relative(obs)
        sm = {k: self._filters[k](v, obs.t) for k, v in rel.items() if k in AXES}
        self._head = Head(sm["yaw"], sm["pitch"], sm["x"], sm["y"], sm["z"])

        if obs.blink < cfg.gaze.blink_threshold:
            gx = self._filters["gx"](rel["gx"], obs.t)
            gy = self._filters["gy"](rel["gy"], obs.t)
            self._held_gaze = (gx, gy)
        gx, gy = self._held_gaze

        out = {a: apply_curve(sm[a], self._axes[a]) for a in AXES}

        if cfg.gaze.enabled and self.gaze_enabled:
            g = cfg.gaze
            r = self._gaze_ranges
            ex = gaze_curve(gx, r["left"] if gx > 0 else r["right"], g.deadzone, g.exponent) * g.yaw_degrees
            ey = gaze_curve(gy, r["up"] if gy > 0 else r["down"], g.deadzone, g.exponent) * g.pitch_degrees
            # Eye-assist follows the same direction conventions as the head axes.
            if self._axes["yaw"].enabled:
                out["yaw"] += -ex if self._axes["yaw"].invert else ex
            if self._axes["pitch"].enabled:
                out["pitch"] += -ey if self._axes["pitch"].invert else ey
        return Pose(**out)

    def _modifiers(self, pose: Pose, dt: float) -> Pose:
        """Sensitivity, smooth axis toggles and physical limits."""
        step = dt / _TOGGLE_SECONDS
        for name, on in (("translation", self.translation_enabled), ("roll", self.roll_enabled)):
            w = self._weights[name]
            self._weights[name] = min(1.0, w + step) if on else max(0.0, w - step)
        wt, wr = _smoothstep(self._weights["translation"]), _smoothstep(self._weights["roll"])
        s = self.sensitivity
        out = Pose(pose.yaw * s, pose.pitch * s, pose.roll * s * wr, pose.x * wt, pose.y * wt, pose.z * wt)
        # Head + eye-assist must not pass the physical limits: yaw beyond ±180
        # would wrap the view to the opposite side in game.
        for axis, limit in _ROTATION_LIMITS.items():
            setattr(out, axis, max(-limit, min(limit, getattr(out, axis))))
        return out

    def _lost(self, now: float) -> Pose:
        tr = self.config.tracking
        if self._last_seen is None:
            return Pose()
        gone = now - self._last_seen
        if gone <= tr.lost_hold_seconds:
            return self._last_pose
        if self._lost_from is None:
            self._lost_from = self._last_pose
        k = 1.0 - _smoothstep((gone - tr.lost_hold_seconds) / max(1e-3, tr.lost_return_seconds))
        return self._lost_from.scaled(k)

    def gaze_point(self, aspect: float) -> tuple[float, float] | None:
        """Where on screen you're looking (see `gaze_model.screen_point`), from the
        latest filtered eye and head values; None until centered."""
        if self._center is None:
            return None
        o = self.config.overlay
        eye = eye_point(*self._held_gaze, self._gaze_ranges, self._gaze_fit)
        head = self._head if o.follow_head else Head()
        return screen_point(eye, head, viewing_distance(self._center.z), o.screen_degrees, aspect)

    def raw_relative(self, obs: Observation) -> dict[str, float] | None:
        """Centered, unfiltered values, for the preview/tuning display."""
        return self._relative(obs) if self._center is not None else None
