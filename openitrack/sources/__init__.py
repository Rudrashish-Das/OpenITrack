"""Tracking sources. Each yields Samples: an Observation (or None when no face is
visible) plus optional data for the preview window."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..features import Observation


@dataclass
class Sample:
    obs: Observation | None
    frame: Any = None          # BGR image (webcam source only)
    landmarks: Any = None      # Nx2 normalised points for the preview overlay
    status: str = ""           # human-readable source state, e.g. "phone connected"
    fresh: bool = True         # False = nothing new yet; skip this tick
