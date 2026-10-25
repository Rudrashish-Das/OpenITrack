"""One Euro filter (Casiez et al., 2012): adaptive low-pass that is steady at rest
and responsive during fast motion. Cheap enough to run per axis per frame."""

from __future__ import annotations

import math


def _alpha(cutoff: float, dt: float) -> float:
    tau = 1.0 / (2.0 * math.pi * cutoff)
    return 1.0 / (1.0 + tau / dt)


class OneEuroFilter:
    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.0, d_cutoff: float = 1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._x: float | None = None
        self._dx = 0.0
        self._t: float | None = None

    def reset(self, value: float | None = None) -> None:
        self._x = value
        self._dx = 0.0
        self._t = None

    def __call__(self, x: float, t: float) -> float:
        if self._x is None or self._t is None:
            self._x, self._t = x, t
            return x
        dt = t - self._t
        if dt <= 0:
            return self._x
        # Long gaps (face lost, stalls) shouldn't produce a huge derivative spike.
        dt = min(dt, 0.25)
        self._t = t
        dx = (x - self._x) / dt
        self._dx += _alpha(self.d_cutoff, dt) * (dx - self._dx)
        cutoff = self.min_cutoff + self.beta * abs(self._dx)
        self._x += _alpha(cutoff, dt) * (x - self._x)
        return self._x
