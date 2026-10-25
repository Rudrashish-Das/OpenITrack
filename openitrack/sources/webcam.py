"""Webcam / IP-camera source: OpenCV capture + MediaPipe face landmarker on the PC.

Runs capture and inference on one thread. Frames beyond the configured rate
are grabbed but never decoded, which keeps CPU use low.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import cv2
import numpy as np

from ..config import CAMERA_FORMATS, SourceConfig, TrackingConfig
from ..features import GAZE_LANDMARKS, build_observation
from . import Sample

log = logging.getLogger(__name__)

REOPEN_INTERVAL = 2.0


def _backends() -> list[int]:
    # Media Foundation first: on many laptop cameras DirectShow opens fine but
    # delivers black frames at ~1 fps. DirectShow stays as a fallback for older
    # USB webcams and virtual cameras.
    return [cv2.CAP_MSMF, cv2.CAP_DSHOW] if sys.platform == "win32" else [cv2.CAP_ANY]


def _delivers_real_frames(cap: cv2.VideoCapture) -> bool:
    """Reject backends that 'work' but only return black frames."""
    for _ in range(5):
        ok, frame = cap.read()
        if ok and frame is not None and float(frame.mean()) > 2.0:
            return True
    return False


def fourcc_name(code: float) -> str:
    """CAP_PROP_FOURCC value -> "MJPG"; "" when the backend doesn't report one."""
    raw = int(code).to_bytes(4, "little", signed=False) if 0 < code < 2 ** 32 else b""
    text = raw.decode("ascii", "replace").strip("\x00 ")
    return text if len(text) == 4 and text.isprintable() else ""


def _open_capture(cfg: SourceConfig) -> cv2.VideoCapture | None:
    cam = cfg.camera
    if isinstance(cam, str) and cam.strip().isdigit():
        cam = int(cam)
    if isinstance(cam, int):
        for backend in _backends():
            cap = cv2.VideoCapture(cam, backend)
            if cap.isOpened():
                # MJPG lets USB webcams deliver 30 fps at 640x480+ without saturating USB;
                # YUY2 skips decoding but needs more bandwidth. Set before the size.
                fourcc = CAMERA_FORMATS[cfg.format]
                if fourcc:
                    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
                cap.set(cv2.CAP_PROP_FPS, cfg.fps)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                if _delivers_real_frames(cap):
                    log.debug("Camera %d using %s backend", cam, cap.getBackendName())
                    return cap
                log.debug("Camera %d: %s backend gives no usable image", cam, cap.getBackendName())
            cap.release()
        log.warning(
            "Camera %d opened but only returned black frames. Check the privacy shutter, "
            "Windows Settings > Privacy > Camera (allow desktop apps), and that no other "
            "app is using it.", cam)
        return None
    cap = cv2.VideoCapture(cam)
    if cap.isOpened():
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap
    cap.release()
    return None


def list_cameras(max_index: int = 6) -> list[tuple[int, int, int]]:
    return [(i, w, h) for i, w, h, _ in probe_cameras(max_index, thumbnail_width=0)]


def probe_cameras(max_index: int = 6, thumbnail_width: int = 240) -> list[tuple[int, int, int, bytes]]:
    """Working cameras as (index, width, height, PNG snapshot). The snapshot is
    mirrored like a mirror and only kept in memory, for picking the right camera."""
    found = []
    for i in range(max_index):
        for backend in _backends():
            cap = cv2.VideoCapture(i, backend)
            ok, frame = cap.read() if cap.isOpened() else (False, None)
            usable = ok and _delivers_real_frames(cap)
            if usable:
                ok, latest = cap.read()  # a frame after auto-exposure settled a little
                frame = latest if ok and latest is not None else frame
            cap.release()
            if usable:
                png = b""
                if thumbnail_width:
                    h, w = frame.shape[:2]
                    small = cv2.resize(cv2.flip(frame, 1), (thumbnail_width, int(h * thumbnail_width / w)))
                    png = cv2.imencode(".png", small)[1].tobytes()
                found.append((i, frame.shape[1], frame.shape[0], png))
                break
    return found


class WebcamSource:
    def __init__(self, cfg: SourceConfig, tracking: TrackingConfig, model_path: Path):
        import mediapipe as mp  # slow to import; list_cameras() doesn't need it
        from mediapipe.tasks.python import BaseOptions, vision

        self._mp = mp
        self.cfg = cfg
        cv2.setNumThreads(1)
        options = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
            min_face_detection_confidence=tracking.min_confidence,
            min_face_presence_confidence=tracking.min_confidence,
            min_tracking_confidence=tracking.min_confidence,
            output_face_blendshapes=tracking.blendshapes,
            output_facial_transformation_matrixes=True,
        )
        self._landmarker = vision.FaceLandmarker.create_from_options(options)
        self._cap: cv2.VideoCapture | None = None
        self._last_ts = -1
        self.want_frames = False  # set by the engine when the preview is open

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
        self._landmarker.close()

    def samples(self) -> Iterator[Sample]:
        period = 1.0 / self.cfg.fps
        last_processed = -1.0
        last_attempt = -REOPEN_INTERVAL
        warned = False
        while True:
            if self._cap is None:
                now = time.monotonic()
                if now - last_attempt < REOPEN_INTERVAL:
                    time.sleep(0.1)
                    yield Sample(None, status="camera unavailable")
                    continue
                last_attempt = now
                self._cap = _open_capture(self.cfg)
                if self._cap is None:
                    if not warned:
                        log.error("Cannot open camera %r; retrying every %.0fs", self.cfg.camera, REOPEN_INTERVAL)
                        warned = True
                    continue
                self._log_opened()
                warned = False

            # Every frame is grabbed (cheap, no decode) so the camera never backs up;
            # only frames we actually process get decoded. The 0.75 factor tolerates
            # camera jitter so a 30 fps limit keeps every frame of a 30 fps camera.
            if not self._cap.grab():
                self._drop_camera()
                continue
            t = time.monotonic()
            if t - last_processed < period * 0.75:
                continue
            ok, frame = self._cap.retrieve()
            if not ok or frame is None:
                self._drop_camera()
                continue
            last_processed = t
            yield self._process(frame, t)

    def _log_opened(self) -> None:
        """Say what the camera actually delivers: drivers quietly ignore sizes and
        formats they don't support."""
        cap, cfg = self._cap, self.cfg
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fmt = fourcc_name(cap.get(cv2.CAP_PROP_FOURCC))
        log.info("Camera %r opened at %dx%d%s", cfg.camera, w, h, f" ({fmt})" if fmt else "")
        if isinstance(cfg.camera, int) or str(cfg.camera).strip().isdigit():
            if (w, h) != (cfg.width, cfg.height):
                log.warning("The camera doesn't offer %dx%d%s; using %dx%d. Pick another size in "
                            "[source] width/height.", cfg.width, cfg.height,
                            f" as {cfg.format.upper()}" if CAMERA_FORMATS[cfg.format] else "", w, h)

    def _drop_camera(self) -> None:
        log.warning("Camera stopped delivering frames; reconnecting")
        if self._cap is not None:
            self._cap.release()
        self._cap = None

    def _process(self, frame: np.ndarray, t: float) -> Sample:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        ts = max(self._last_ts + 1, int(t * 1000))
        self._last_ts = ts
        result = self._landmarker.detect_for_video(image, ts)
        preview = frame if self.want_frames else None
        if not result.face_landmarks or not result.facial_transformation_matrixes:
            return Sample(None, frame=preview, status="no face")
        lms = result.face_landmarks[0]
        if len(lms) <= max(GAZE_LANDMARKS):
            return Sample(None, frame=preview, status="no iris landmarks")
        gaze_pts = [(lms[i].x, lms[i].y) for i in GAZE_LANDMARKS]
        blend = None
        if result.face_blendshapes:
            blend = {c.category_name: c.score for c in result.face_blendshapes[0]}
        h, w = frame.shape[:2]
        obs = build_observation(t, result.facial_transformation_matrixes[0], gaze_pts, w, h, blend)
        pts = None
        if self.want_frames:
            pts = np.array([(p.x, p.y) for p in lms], dtype=np.float32)
        return Sample(obs, frame=preview, landmarks=pts, status="tracking")
