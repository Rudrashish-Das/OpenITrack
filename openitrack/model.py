"""Locate (or fetch once) the MediaPipe face landmarker model."""

from __future__ import annotations

import logging
import os
import urllib.request
from pathlib import Path

from .config import default_config_path

log = logging.getLogger(__name__)

MODEL_NAME = "face_landmarker.task"
MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
    "face_landmarker/float16/1/face_landmarker.task"
)
_MIN_SIZE = 1_000_000  # the real file is ~3.7 MB; anything tiny is a failed download


def _candidates() -> list[Path]:
    here = Path(__file__).resolve().parent
    return [
        here / "models" / MODEL_NAME,                  # bundled with the package / exe
        default_config_path().parent / "models" / MODEL_NAME,
    ]


def ensure_model() -> Path:
    for path in _candidates():
        if path.is_file() and path.stat().st_size >= _MIN_SIZE:
            return path
    target = _candidates()[-1]
    target.parent.mkdir(parents=True, exist_ok=True)
    log.info("Downloading face model (one time, ~3.7 MB) from %s", MODEL_URL)
    tmp = target.with_suffix(".part")
    try:
        with urllib.request.urlopen(MODEL_URL, timeout=30) as resp, open(tmp, "wb") as fh:
            while chunk := resp.read(1 << 16):
                fh.write(chunk)
        if tmp.stat().st_size < _MIN_SIZE:
            raise OSError("downloaded model is unexpectedly small")
        os.replace(tmp, target)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"Could not download the face model: {exc}\n"
            f"Download it manually from {MODEL_URL} and save it as {target}"
        ) from exc
    log.info("Model saved to %s", target)
    return target
