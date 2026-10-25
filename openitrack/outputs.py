"""Pose outputs: TrackIR/FreeTrack games (built in, or through opentrack over UDP)
and mouse-look."""

from __future__ import annotations

import ctypes
import logging
import socket
import struct
import sys

from . import freetrack
from .config import OutputConfig
from .mapping import Pose

log = logging.getLogger(__name__)


class Output:
    game = ""  # name of the game reading the pose, when the output knows it

    def send(self, pose: Pose) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


def pack_opentrack(pose: Pose) -> bytes:
    """opentrack 'UDP over network' packet: 6 little-endian doubles,
    x, y, z in cm then yaw, pitch, roll in degrees.

    opentrack (matching its own webcam tracker) uses yaw + = right and
    z + = away from the screen; our pose uses yaw + = left and z + = toward
    the camera, so those two are flipped. Pitch, roll, x and y already agree
    (+ = up, tilt left, move left, up).
    """
    return struct.pack("<6d", pose.x, pose.y, -pose.z, -pose.yaw, pose.pitch, pose.roll)


class OpenTrackUDP(Output):
    def __init__(self, host: str, port: int):
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)

    def send(self, pose: Pose) -> None:
        try:
            self.sock.sendto(pack_opentrack(pose), self.addr)
        except (BlockingIOError, ConnectionResetError):
            # Windows reports ICMP "port unreachable" from an earlier packet as a
            # reset when opentrack isn't running yet; that's not an error for us.
            pass
        except OSError as exc:
            log.debug("UDP send failed: %s", exc)

    def close(self) -> None:
        self.sock.close()


class DirectOutput(Output):
    """TrackIR / FreeTrack games without opentrack: the FreeTrack shared memory
    opentrack's "freetrack 2.0 Enhanced" output writes, with the registry pointing
    games at our bundled copy of opentrack's client DLLs while it runs, and our
    stand-in TrackIR.exe running if `trackir_process`."""

    def __init__(self, trackir_process: bool = True):
        self.shm = freetrack.SharedMemory()
        self.writer = freetrack.FreeTrackWriter(self.shm.heap, freetrack.load_game_keys())
        self.registry = freetrack.RegistryRedirect(freetrack.CLIENTS_DIR)
        try:
            self.registry.apply()
        except OSError as exc:
            # Games still work if the registry already points at opentrack's DLLs.
            log.warning("Couldn't point TrackIR/FreeTrack games at OpenITrack: %s", exc)
        self.trackir = freetrack.TrackIRProcess() if trackir_process else None
        if self.trackir:
            try:
                self.trackir.start()
            except OSError as exc:
                log.warning("Couldn't check for a running TrackIR.exe: %s", exc)

    @property
    def game(self) -> str:
        return self.writer.game

    def send(self, pose: Pose) -> None:
        with self.shm.lock():
            self.writer.write(pose)

    def close(self) -> None:
        with self.shm.lock():
            self.writer.clear()
        self.shm.close()
        self.registry.restore()
        if self.trackir:
            self.trackir.stop()


class MouseOutput(Output):
    """Relative mouse movement that follows yaw/pitch. For games without
    TrackIR support that use mouse-look (view follows the cursor delta)."""

    def __init__(self, pixels_per_degree: float):
        if sys.platform != "win32":
            raise RuntimeError("Mouse output is only supported on Windows")
        self.ppd = pixels_per_degree
        self._sent_x = 0
        self._sent_y = 0
        self._user32 = ctypes.windll.user32

    def send(self, pose: Pose) -> None:
        # Target cursor offset is absolute; we send only the integer delta from
        # what we've already sent, so rounding never accumulates drift.
        tx = round(-pose.yaw * self.ppd)  # yaw + = left -> mouse moves left
        ty = round(-pose.pitch * self.ppd)  # pitch + = up -> mouse moves up
        dx, dy = tx - self._sent_x, ty - self._sent_y
        if dx or dy:
            self._user32.mouse_event(0x0001, dx, dy, 0, 0)  # MOUSEEVENTF_MOVE
            self._sent_x, self._sent_y = tx, ty


def build_outputs(cfg: OutputConfig) -> list[Output]:
    outs: list[Output] = []
    if cfg.game == "direct":
        try:
            outs.append(DirectOutput(cfg.trackir_process))
            log.info("Sending to TrackIR/FreeTrack games directly")
        except (OSError, RuntimeError) as exc:
            log.error("Direct game output not available: %s", exc)
    else:
        freetrack.restore_stale()
    if cfg.game == "opentrack":
        outs.append(OpenTrackUDP(cfg.opentrack_host, cfg.opentrack_port))
        log.info("Sending to opentrack at %s:%d (UDP)", cfg.opentrack_host, cfg.opentrack_port)
    if cfg.mouse:
        outs.append(MouseOutput(cfg.mouse_pixels_per_degree))
        log.info("Mouse output enabled (%.1f px/deg)", cfg.mouse_pixels_per_degree)
    if not outs:
        log.warning("No outputs enabled in [output]; tracking will not reach any game")
    return outs
