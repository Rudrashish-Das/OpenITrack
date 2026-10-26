"""Game output runs at its own fixed rate, gliding between the per-frame poses."""

import threading
import time

import pytest

from openitrack.config import DEFAULT_CONFIG_TEXT, ConfigFile
from openitrack.engine import OUTPUT_HZ, Engine
from openitrack.mapping import Pose, PoseGlide
from openitrack.outputs import Output


def ticks(start, end, hz=OUTPUT_HZ):
    n = int(round((end - start) * hz))
    return [start + i / hz for i in range(n + 1)]


def test_first_pose_is_used_as_is():
    g = PoseGlide()
    assert g.at(0.0) == Pose()
    g.push(Pose(yaw=10), 1.0)
    assert g.at(1.0).yaw == 10 and g.at(5.0).yaw == 10


def test_steady_turn_comes_out_at_a_steady_speed():
    g = PoseGlide(1 / 30)
    yaws = []
    for t in ticks(0.0, 0.9):                 # 30 fps camera, 30 deg/s turn
        if round(t * OUTPUT_HZ) % (OUTPUT_HZ // 30) == 0:
            g.push(Pose(yaw=t * 30), t)
        if t >= 0.5:
            yaws.append(g.at(t).yaw)
    steps = [b - a for a, b in zip(yaws, yaws[1:])]
    assert steps == pytest.approx([30 / OUTPUT_HZ] * len(steps), abs=1e-6)


def test_uneven_frames_never_jump_or_overshoot():
    g = PoseGlide(1 / 30)
    t, yaw = 0.0, 0.0
    frames = []
    for gap in [1 / 30, 1 / 30, 0.05, 0.02, 1 / 30, 0.06, 0.01, 1 / 30] * 4:
        t += gap
        yaw += 1.0
        frames.append((t, yaw))
    g.push(Pose(), 0.0)
    out, last = [], 0.0
    for tick in ticks(0.0, frames[-1][0] + 0.2):
        while frames and frames[0][0] <= tick:
            g.push(Pose(yaw=frames[0][1]), frames[0][0])
            last = frames.pop(0)[1]
        out.append(g.at(tick).yaw)
    steps = [b - a for a, b in zip(out, out[1:])]
    assert min(steps) >= 0                    # always forward
    assert max(steps) < 0.5                   # each frame is 1 deg: never all at once
    assert max(out) <= last + 1e-9 and out[-1] == pytest.approx(last)


def test_a_stall_does_not_stretch_the_glide():
    g = PoseGlide(1 / 30)
    g.push(Pose(), 0.0)
    g.push(Pose(yaw=10), 2.0)                 # camera came back after 2 s
    assert g.interval == pytest.approx(1 / 30)
    assert g.at(2.0 + 1 / 30).yaw == pytest.approx(10)


class Recorder(Output):
    def __init__(self):
        self.poses = []
        self.sent = threading.Event()

    def send(self, pose):
        self.poses.append(pose)
        if len(self.poses) >= 10:
            self.sent.set()


def test_engine_sends_to_outputs_from_its_own_thread(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
    engine = Engine(ConfigFile(path))
    rec = Recorder()
    engine.outputs = [rec]
    engine._glide.push(Pose(yaw=5), time.monotonic())
    engine._out_thread = threading.Thread(target=engine._output_loop, daemon=True)
    engine._out_thread.start()
    start = time.monotonic()
    try:
        assert rec.sent.wait(2.0)
    finally:
        engine.close()
    assert not engine._out_thread.is_alive()
    assert all(p.yaw == 5 for p in rec.poses)
    # 10 sends at OUTPUT_HZ: well under a second, not one per camera frame.
    assert time.monotonic() - start < 10 / OUTPUT_HZ + 0.5
