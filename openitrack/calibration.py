"""Guided calibration: neutral pose, eye range and gaze fit, comfortable head range.

`CalibrationSession` is a time-driven state machine fed raw observations; it has
no UI so it can be tested directly. `calibration_overlay` draws it.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import numpy as np

from .features import Observation, median_observation, relative_pose
from .gaze_model import TARGET_REACH, Head, eye_point_for, fit_eye_map, viewing_distance

log = logging.getLogger(__name__)

# Gaze targets sit TARGET_REACH of the way from the screen center toward each
# edge. Looking at an edge target = full eye-assist in that direction.
MAX_HEAD_DRIFT_DEG = 7.0     # head motion allowed while measuring eye range
MIN_GAZE_H = 0.02            # smallest believable eye travel (fraction of eye width)
MIN_GAZE_V = 0.008
MIN_HEAD_YAW = 8.0           # smallest believable comfortable head turn (deg)
MIN_HEAD_PITCH = 5.0
BLINK_LIMIT = 0.45


@dataclass
class Calibration:
    created: str = ""
    center: dict | None = None          # neutral Observation fields, plus its 3x3
                                        # "rotation" and the phone "orientation"
    gaze_left: float | None = None      # iris offset reaching the left target (> 0)
    gaze_right: float | None = None
    gaze_up: float | None = None
    gaze_down: float | None = None
    head_yaw_range: float | None = None   # comfortable turn while still seeing the screen
    head_pitch_range: float | None = None
    gaze_fit: dict | None = None          # the gaze dot's eye map (gaze_model.fit_eye_map)

    def center_observation(self) -> Observation | None:
        if not self.center:
            return None
        values = dict(self.center)
        # Files from older versions have no rotation: it is rebuilt from the angles.
        rotation = values.pop("rotation", None)
        orientation = values.pop("orientation", None)
        obs = Observation(t=0.0, **values)
        if rotation is not None:
            obs.rotation = np.asarray(rotation, dtype=np.float64).reshape(3, 3)
        if orientation is not None:  # a screen angle (0 is valid) or "portrait"/"landscape"
            obs.extra["orientation"] = orientation
        return obs

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> "Calibration | None":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable calibration file %s: %s", path, exc)
            return None
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def summary(self) -> str:
        def f(v, unit=""):
            return "-" if v is None else f"{v:.3f}" if not unit else f"{v:.0f}{unit}"
        return (f"eyes L {f(self.gaze_left)} R {f(self.gaze_right)} "
                f"U {f(self.gaze_up)} D {f(self.gaze_down)} | "
                f"head yaw +/-{f(self.head_yaw_range, ' deg')} pitch +/-{f(self.head_pitch_range, ' deg')}"
                + (f" | gaze fit {self.gaze_fit['points']} dots, error {self.gaze_fit['rms']:.2f}"
                   if self.gaze_fit else ""))


@dataclass(frozen=True)
class Step:
    kind: str            # "center", "gaze" or "head"
    direction: str       # "center", "left", "right", "up", "down", or a corner like "up-left"
    title: str
    hint: str
    settle: float        # seconds to get into position before measuring
    collect: float       # seconds of measuring
    min_samples: int


_CORNER_NAMES = {"up-left": "TOP LEFT", "up-right": "TOP RIGHT",
                 "down-left": "BOTTOM LEFT", "down-right": "BOTTOM RIGHT"}


def _gaze(direction: str) -> Step:
    where = (f"in the {_CORNER_NAMES[direction]} corner" if direction in _CORNER_NAMES
             else f"on the {direction.upper()}")
    return Step("gaze", direction, f"Look at the dot {where}",
                "Move only your eyes. Keep your head still.", 1.2, 1.0, 12)


def _head(direction: str) -> Step:
    verb = "Turn" if direction in ("left", "right") else "Tilt"
    return Step("head", direction, f"{verb} your head {direction.upper()}",
                "As far as is comfortable while you can still see the screen, then hold.",
                0.4, 2.6, 15)


STEPS: tuple[Step, ...] = (
    Step("center", "center", "Look at the dot in the CENTER",
         "Sit how you normally play. Keep still.", 1.5, 1.2, 15),
    _gaze("left"), _gaze("right"), _gaze("up"), _gaze("down"),
    _gaze("up-left"), _gaze("up-right"), _gaze("down-right"), _gaze("down-left"),
    _head("left"), _head("right"), _head("up"), _head("down"),
)

# Where the dot is drawn, as (x, y) in [-1, 1] screen coordinates (+y = up).
TARGETS = {
    "center": (0.0, 0.0),
    "left": (-TARGET_REACH, 0.0), "right": (TARGET_REACH, 0.0),
    "up": (0.0, TARGET_REACH), "down": (0.0, -TARGET_REACH),
    "up-left": (-TARGET_REACH, TARGET_REACH), "up-right": (TARGET_REACH, TARGET_REACH),
    "down-left": (-TARGET_REACH, -TARGET_REACH), "down-right": (TARGET_REACH, -TARGET_REACH),
}


class CalibrationSession:
    INTRO_SECONDS = 3.0
    MESSAGE_SECONDS = 1.6
    MAX_RETRIES = 2

    def __init__(self, now: float, screen_degrees: float = 50.0, aspect: float = 16 / 9):
        """screen_degrees, aspect: the overlay screen's size as seen from the seat
        (see `gaze_model.screen_point`), to undo head drift at each gaze dot."""
        self.started = now
        self.screen_degrees = screen_degrees
        self.aspect = aspect
        self.phase = "intro"          # intro, settle, collect, message, done, failed
        self.index = 0
        self.phase_t = now
        self.samples: list[Observation] = []
        self.retries = 0
        self.message = ""
        self.message_ok = True
        self.center: Observation | None = None
        self.result = Calibration()
        self.live: Observation | None = None
        self._after_message = "settle"
        self._head_extremes: dict[str, float] = {}
        # (gaze_x, gaze_y, eye_x, eye_y) per measured dot, for the gaze fit.
        self._fit_points: list[tuple[float, float, float, float]] = []

    # -- state ---------------------------------------------------------------
    @property
    def step(self) -> Step | None:
        return STEPS[self.index] if self.index < len(STEPS) else None

    @property
    def finished(self) -> bool:
        return self.phase in ("done", "failed")

    def progress(self) -> float:
        return min(1.0, self.index / len(STEPS))

    def _enter(self, phase: str, now: float) -> None:
        self.phase = phase
        self.phase_t = now

    def _show(self, message: str, ok: bool, now: float, then: str) -> None:
        self.message, self.message_ok = message, ok
        self._after_message = then
        self._enter("message", now)

    def _next_step(self, now: float, message: str = "") -> None:
        self.index += 1
        self.retries = 0
        if self.index >= len(STEPS):
            self._finish(now)
        elif message:
            self._show(message, True, now, "settle")
        else:
            self._enter("settle", now)

    def _retry(self, reason: str, now: float, give_up: bool = True) -> None:
        """Ask the user to redo the step; after MAX_RETRIES skip it (if allowed)."""
        self.retries += 1
        if self.retries > self.MAX_RETRIES and give_up:
            self._next_step(now, f"Skipped: {reason}")
            return
        self._show(reason + " Let's try that again.", False, now, "settle")

    def _finish(self, now: float) -> None:
        self.result.gaze_fit = fit_eye_map(self._fit_points)
        self.result.created = dt.datetime.now().isoformat(timespec="seconds")
        self._enter("done", now)

    # -- feeding -------------------------------------------------------------
    def update(self, obs: Observation | None, now: float) -> None:
        self.live = obs
        if self.finished:
            return
        elapsed = now - self.phase_t
        step = self.step

        if self.phase == "intro":
            if obs is None:
                self.phase_t = now  # countdown only runs while the face is visible
            elif elapsed >= self.INTRO_SECONDS:
                self._enter("settle", now)
            return

        if self.phase == "message":
            if elapsed >= self.MESSAGE_SECONDS:
                self._enter(self._after_message, now)
            return

        assert step is not None
        if self.phase == "settle":
            if elapsed >= step.settle:
                self.samples = []
                self._enter("collect", now)
            return

        # collect
        if obs is not None and (step.kind == "head" or obs.blink < BLINK_LIMIT):
            self.samples.append(obs)
        if elapsed >= step.collect and len(self.samples) >= step.min_samples:
            self._evaluate(step, now)
        elif elapsed >= step.collect * 3:
            self._retry("Couldn't see your face clearly. Check lighting.", now,
                        give_up=step.kind != "center")

    def cancel(self, now: float) -> None:
        self.message = "Calibration cancelled"
        self._enter("failed", now)

    # -- evaluation ----------------------------------------------------------
    def _evaluate(self, step: Step, now: float) -> None:
        if step.kind == "center":
            yaws = np.array([o.yaw for o in self.samples])
            if np.ptp(yaws) > 6.0:
                self._retry("You moved during the measurement.", now, give_up=False)
                return
            self.center = median_observation(self.samples)
            c = self.center
            self.result.center = {k: getattr(c, k) for k in
                                  ("yaw", "pitch", "roll", "x", "y", "z", "gaze_x", "gaze_y", "blink")}
            self.result.center["rotation"] = c.matrix.tolist()
            if c.extra.get("orientation") is not None:
                self.result.center["orientation"] = c.extra["orientation"]
            self._fit_points = [(0.0, 0.0, 0.0, 0.0)]
            self._next_step(now)
            return

        c = self.center
        assert c is not None
        if step.kind == "gaze":
            m = median_observation(self.samples)
            head = relative_pose(c, m)
            drift = max(abs(head["yaw"]), abs(head["pitch"]))
            if drift > MAX_HEAD_DRIFT_DEG and self.retries < self.MAX_RETRIES:
                self._retry(f"Your head moved {drift:.0f} deg. Move only your eyes.", now, give_up=False)
                return
            dx, dy = m.gaze_x - c.gaze_x, m.gaze_y - c.gaze_y
            # + gaze_x = subject's left = screen left (the user faces the screen).
            # A corner must show eye travel both ways.
            parts = step.direction.split("-")
            for part in parts:
                value = {"left": dx, "right": -dx, "up": dy, "down": -dy}[part]
                minimum = MIN_GAZE_H if part in ("left", "right") else MIN_GAZE_V
                if value < minimum:
                    self._retry("Eye movement was too small to measure.", now)
                    return
            if len(parts) == 1:
                setattr(self.result, f"gaze_{step.direction}", float(value))
            # Fit what the eyes did, with any head drift taken out.
            drift = Head(head["yaw"], head["pitch"], head["x"], head["y"], head["z"])
            eye = eye_point_for(TARGETS[step.direction], drift, viewing_distance(c.z),
                                self.screen_degrees, self.aspect)
            self._fit_points.append((dx, dy, *eye))
            self._next_step(now)
            return

        # head: comfortable extreme = 90th percentile in the asked direction
        axis = "yaw" if step.direction in ("left", "right") else "pitch"
        rel = np.array([relative_pose(c, o)[axis] for o in self.samples])
        if axis == "yaw":
            sign, minimum = (1.0 if step.direction == "left" else -1.0), MIN_HEAD_YAW
        else:
            sign, minimum = (1.0 if step.direction == "up" else -1.0), MIN_HEAD_PITCH
        extreme = float(np.percentile(rel * sign, 90))
        if extreme < minimum:
            self._retry(f"Only {max(0.0, extreme):.0f} deg of movement detected.", now)
            return
        self._head_extremes[step.direction] = extreme
        yaw = [self._head_extremes[d] for d in ("left", "right") if d in self._head_extremes]
        pitch = [self._head_extremes[d] for d in ("up", "down") if d in self._head_extremes]
        self.result.head_yaw_range = float(np.mean(yaw)) if yaw else None
        self.result.head_pitch_range = float(np.mean(pitch)) if pitch else None
        self._next_step(now)

    # -- for the overlay -------------------------------------------------------
    def live_offset(self) -> tuple[float, float] | None:
        """Current head yaw/pitch relative to the measured center (for head steps)."""
        if self.live is None or self.center is None:
            return None
        rel = relative_pose(self.center, self.live)
        return rel["yaw"], rel["pitch"]
