import math
import struct

import numpy as np
import pytest

from openitrack.config import DEFAULT_CONFIG_TEXT, AxisConfig, Config, ConfigError, parse_config
from openitrack.features import GAZE_LANDMARKS, gaze, head_pose
from openitrack.filters import OneEuroFilter
from openitrack.hotkeys import parse_hotkey
from openitrack.mapping import RECENTER_FRAMES, Pose, PosePipeline, apply_curve, gaze_curve
from openitrack.outputs import pack_opentrack
from openitrack.sources.phone import parse_message
from openitrack.features import Observation


def rot(yaw=0.0, pitch_rx=0.0, roll_rz=0.0):
    y, p, r = (math.radians(v) for v in (yaw, pitch_rx, roll_rz))
    ry = np.array([[math.cos(y), 0, math.sin(y)], [0, 1, 0], [-math.sin(y), 0, math.cos(y)]])
    rx = np.array([[1, 0, 0], [0, math.cos(p), -math.sin(p)], [0, math.sin(p), math.cos(p)]])
    rz = np.array([[math.cos(r), -math.sin(r), 0], [math.sin(r), math.cos(r), 0], [0, 0, 1]])
    return ry @ rx @ rz


def matrix(yaw=0.0, pitch_rx=0.0, roll_rz=0.0, t=(1.0, 2.0, -45.0)):
    m = np.eye(4)
    m[:3, :3] = rot(yaw, pitch_rx, roll_rz)
    m[:3, 3] = t
    return m


# -- head pose ---------------------------------------------------------------

def test_head_pose_recovers_angles_and_translation():
    yaw, pitch, roll, x, y, z = head_pose(matrix(20, -10, 5))
    assert yaw == pytest.approx(20, abs=1e-6)
    assert pitch == pytest.approx(10, abs=1e-6)   # -Rx: face tipped up
    assert roll == pytest.approx(-5, abs=1e-6)
    assert (x, y, z) == pytest.approx((1.0, 2.0, -45.0))


def test_head_pose_accepts_column_major_flat_array():
    m = matrix(15, 5, -3)
    flat_column_major = m.T.reshape(-1)  # what the JS API hands us
    assert head_pose(flat_column_major) == pytest.approx(head_pose(m))


def test_head_pose_ignores_uniform_scale():
    m = matrix(30, 0, 0)
    m[:3, :3] *= 1.3
    assert head_pose(m)[0] == pytest.approx(30, abs=1e-6)


# -- gaze --------------------------------------------------------------------

def eye_points(iris_dx=0.0, iris_dy=0.0):
    """Two horizontal eyes, width 0.1 (normalised), iris offset by a fraction of width."""
    pts = []
    for cx in (0.4, 0.6):
        a, b = (cx - 0.05, 0.5), (cx + 0.05, 0.5)
        pts += [a, b, (cx, 0.485), (cx, 0.515), (cx + iris_dx * 0.1, 0.5 - iris_dy * 0.1)]
    assert len(pts) == len(GAZE_LANDMARKS)
    return pts


def test_gaze_center_and_directions():
    gx, gy, openness = gaze(eye_points(), 1.0, 1.0)
    assert (gx, gy) == pytest.approx((0, 0), abs=1e-9)
    assert openness == pytest.approx(0.3)
    gx, gy, _ = gaze(eye_points(0.2, 0.1), 1.0, 1.0)
    assert gx == pytest.approx(0.2) and gy == pytest.approx(0.1)


def test_gaze_is_aspect_corrected():
    # Same physical layout captured at 640x480: normalised y shrinks, pixels don't.
    pts = [(x, 0.5 + (y - 0.5) * 640 / 480) for x, y in eye_points(0.0, 0.1)]
    _, gy, _ = gaze(pts, 640, 480)
    assert gy == pytest.approx(0.1)


# -- curves & pipeline -------------------------------------------------------

def test_apply_curve():
    ax = AxisConfig(input_range=30, output=180, deadzone=2, exponent=2)
    assert apply_curve(1.5, ax) == 0
    assert apply_curve(30, ax) == pytest.approx(180)
    assert apply_curve(100, ax) == pytest.approx(180)  # saturates
    assert apply_curve(-16, ax) == pytest.approx(-180 * 0.25)
    ax.invert = True
    assert apply_curve(30, ax) == pytest.approx(-180)
    ax.enabled = False
    assert apply_curve(30, ax) == 0


def test_gaze_curve_deadzone_and_saturation():
    assert gaze_curve(0.05, 0.3, 0.35, 1.0) == 0
    assert gaze_curve(0.3, 0.3, 0.35, 1.0) == pytest.approx(1)
    assert gaze_curve(-0.9, 0.3, 0.35, 1.0) == pytest.approx(-1)


def obs(t, yaw=0.0, gx=0.0, blink=0.0):
    return Observation(t=t, yaw=yaw, pitch=0, roll=0, x=0, y=0, z=-45, gaze_x=gx, gaze_y=0, blink=blink)


def settle_center(p, yaw=0.0, t=0.0):
    """Feed the frames the pipeline needs to (re)center; returns the next time."""
    for i in range(RECENTER_FRAMES):
        pose = p.update(obs(t, yaw=yaw), t)
        t += 1 / 30
    assert p.centered and pose.yaw == 0
    return t


def test_pipeline_centers_and_tracks():
    cfg = Config()
    cfg.gaze.enabled = False
    p = PosePipeline(cfg)
    t = settle_center(p, yaw=10)  # auto-centered on the first face
    for _ in range(200):
        t += 1 / 30
        pose = p.update(obs(t, yaw=10 + 35), t)
    assert pose.yaw == pytest.approx(cfg.axes.yaw.output, rel=0.02)


def test_pipeline_gaze_adds_to_yaw_and_blink_holds():
    cfg = Config()
    cfg.axes.yaw.deadzone = 0.0
    p = PosePipeline(cfg)
    t = settle_center(p)
    for _ in range(120):
        t += 1 / 30
        pose = p.update(obs(t, gx=0.5), t)
    assert pose.yaw == pytest.approx(cfg.gaze.yaw_degrees, rel=0.02)
    for _ in range(30):  # eyes closed: iris reading is garbage, must be ignored
        t += 1 / 30
        pose = p.update(obs(t, gx=-0.5, blink=0.9), t)
    assert pose.yaw == pytest.approx(cfg.gaze.yaw_degrees, rel=0.02)


def test_pipeline_face_loss_holds_then_returns_to_center():
    cfg = Config()
    cfg.gaze.enabled = False
    p = PosePipeline(cfg)
    t = settle_center(p)
    for _ in range(200):
        t += 1 / 30
        held = p.update(obs(t, yaw=50), t)
    lost_at = t
    assert p.update(None, lost_at + 0.2).yaw == pytest.approx(held.yaw)
    mid = p.update(None, lost_at + cfg.tracking.lost_hold_seconds + cfg.tracking.lost_return_seconds / 2).yaw
    assert 0 < mid < held.yaw
    assert p.update(None, lost_at + 5).yaw == pytest.approx(0)


def test_pipeline_pause_blends_to_zero_and_back():
    cfg = Config()
    p = PosePipeline(cfg)
    t = settle_center(p)
    for _ in range(100):
        t += 1 / 30
        live = p.update(obs(t, yaw=50), t)
    p.toggle_pause()
    for _ in range(30):
        t += 1 / 30
        pose = p.update(obs(t, yaw=50), t)
    assert pose.yaw == pytest.approx(0)
    p.toggle_pause()
    for _ in range(30):
        t += 1 / 30
        pose = p.update(obs(t, yaw=50), t)
    assert pose.yaw == pytest.approx(live.yaw, rel=0.01)


def test_recenter_uses_median_of_next_frames():
    cfg = Config()
    p = PosePipeline(cfg)
    t = settle_center(p)
    p.recenter()
    t = settle_center(p, yaw=25, t=t)
    assert p.center.yaw == 25


# -- filter ------------------------------------------------------------------

def test_one_euro_converges_and_smooths():
    f = OneEuroFilter(1.0, 0.0)
    rng = np.random.default_rng(1)
    out = [f(5.0 + rng.normal(0, 1), i / 30) for i in range(300)]
    assert np.std(out[150:]) < 0.5
    assert np.mean(out[150:]) == pytest.approx(5.0, abs=0.3)


# -- outputs -----------------------------------------------------------------

def test_opentrack_packet_layout_and_signs():
    data = pack_opentrack(Pose(yaw=10, pitch=5, roll=-3, x=1, y=2, z=4))
    assert len(data) == 48
    # x, y, z, yaw, pitch, roll; opentrack wants yaw + = right and z + = away.
    assert struct.unpack("<6d", data) == (1, 2, -4, -10, 5, -3)


# -- config / hotkeys / phone ------------------------------------------------

def test_default_config_text_matches_dataclass_defaults():
    assert parse_config(DEFAULT_CONFIG_TEXT) == Config()


def test_config_validation():
    with pytest.raises(ConfigError):
        parse_config("[source]\ntype = 'kinect'\n")
    with pytest.raises(ConfigError):
        parse_config("[axes.yaw]\ninput_range = 0.1\ndeadzone = 1\n")
    with pytest.raises(ConfigError):
        parse_config("[output]\nopentrack_port = 'x'\n")
    cfg = parse_config("[source]\ncamera = 'http://10.0.0.2:4747/video'\n[axes.yaw]\ninvert = true\n")
    assert cfg.source.camera.startswith("http") and cfg.axes.yaw.invert


def test_parse_hotkey():
    assert parse_hotkey("F9") == (0, 0x78)
    assert parse_hotkey("ctrl+shift+R") == (0x0006, ord("R"))
    assert parse_hotkey("numpad5") == (0, 0x65)
    with pytest.raises(ValueError):
        parse_hotkey("hyper+F1")


def test_parse_phone_message():
    m = matrix(10, 0, 0).T.reshape(-1).tolist()
    g = [c for p in eye_points(0.1, 0) for c in p]
    o = parse_message({"t": 1, "face": 1, "w": 1, "h": 1, "m": m, "g": g, "b": [0.1, 0.2]}, 3.0)
    assert o.yaw == pytest.approx(10) and o.gaze_x == pytest.approx(0.1) and o.blink == pytest.approx(0.15)
    assert parse_message({"t": 1, "face": 0}, 3.0) is None
    with pytest.raises(ValueError):
        parse_message({"t": 1, "face": 1, "w": 1, "h": 1, "m": m[:4], "g": g}, 3.0)
    with pytest.raises(ValueError):
        parse_message({"t": 1, "face": 1, "w": 1, "h": 1, "m": [float("nan")] * 16, "g": g}, 3.0)


def test_head_plus_gaze_never_exceeds_180_yaw():
    cfg = Config()
    p = PosePipeline(cfg)
    t = settle_center(p)
    for _ in range(200):
        t += 1 / 30
        pose = p.update(obs(t, yaw=60, gx=0.9), t)
    assert pose.yaw == pytest.approx(180.0)
