"""Tracking must not depend on where the camera is mounted or how it is tilted.

Each mount is a camera rotation C (world/screen frame -> camera frame) plus where
the head sits in camera space. The user faces the screen at neutral, so the face
rotation MediaPipe reports is C, and after a head motion H it is C @ H.
"""

import math

import numpy as np
import pytest

from openitrack import calibration_overlay
from openitrack.calibration import Calibration, CalibrationSession
from openitrack.config import Config, ConfigError, parse_config
from openitrack.features import (
    _rx, _ry, _rz, angles_from_rotation, build_observation, relative_pose, rotation_from_angles,
)
from openitrack.mapping import RECENTER_FRAMES, PosePipeline, orientation_changed
from openitrack.monitors import Monitor, pick, span
from openitrack.sources.phone import parse_message

# Open eyes looking straight ahead (gaze doesn't matter for most of these tests).
EYES = [p for cx in (0.45, 0.6) for p in (
    (cx - 0.05, 0.5), (cx + 0.05, 0.5), (cx, 0.49), (cx, 0.51), (cx, 0.5))]

# name: camera rotation (MediaPipe's camera-space convention, R = C @ head)
MOUNTS = {
    "centered": np.eye(3),
    "above, tilted down 20": _rx(20),
    "below, tilted up 25": _rx(-25),
    "above side monitor, yaw 30 tilt 15": _rx(15) @ _ry(-30),
    "rolled 10": _rz(10),
}
HEAD_POSITION = np.array([2.0, -3.0, -60.0])  # cm, in the world/screen frame


def mediapipe_matrix(camera, head=np.eye(3), offset=(0.0, 0.0, 0.0)):
    """The 4x4 facial transformation matrix the camera would report."""
    m = np.eye(4)
    m[:3, :3] = camera @ head
    m[:3, 3] = camera @ (HEAD_POSITION + np.asarray(offset))
    return m.T.reshape(-1)  # column-major, like the JS API


def observe(camera, t=0.0, **kw):
    return build_observation(t, mediapipe_matrix(camera, **kw), EYES, 640, 480)


def naive(center, obs):
    """The old per-angle subtraction, to show the test actually catches crosstalk."""
    return {a: getattr(obs, a) - getattr(center, a) for a in ("yaw", "pitch", "roll")}


@pytest.mark.parametrize("mount", MOUNTS)
def test_head_turn_reads_the_same_from_any_mount(mount):
    camera = MOUNTS[mount]
    center = observe(camera)
    turn = rotation_from_angles(40, 20, 0)
    rel = relative_pose(center, observe(camera, head=turn))
    assert rel["yaw"] == pytest.approx(40, abs=1e-6)
    assert rel["pitch"] == pytest.approx(20, abs=1e-6)
    assert abs(rel["roll"]) < 1.0

    for yaw, pitch in ((40, 0), (0, 20), (-40, -20)):
        rel = relative_pose(center, observe(camera, head=rotation_from_angles(yaw, pitch, 0)))
        assert rel["yaw"] == pytest.approx(yaw, abs=1.0)
        assert rel["pitch"] == pytest.approx(pitch, abs=1.0)
        assert abs(rel["roll"]) < 1.0


def test_tilted_camera_used_to_leak_yaw_into_roll():
    camera = MOUNTS["above, tilted down 20"]
    center = observe(camera)
    old = naive(center, observe(camera, head=rotation_from_angles(40, 0, 0)))
    assert abs(old["roll"]) > 5  # what the Euler subtraction got wrong


@pytest.mark.parametrize("mount", MOUNTS)
def test_leaning_moves_only_z(mount):
    camera = MOUNTS[mount]
    center = observe(camera)
    rel = relative_pose(center, observe(camera, offset=(0, 0, 8)))  # toward the screen
    assert rel["z"] == pytest.approx(8, abs=1e-6)
    assert rel["x"] == pytest.approx(0, abs=1e-6) and rel["y"] == pytest.approx(0, abs=1e-6)
    for name in ("yaw", "pitch", "roll"):
        assert rel[name] == pytest.approx(0, abs=1e-6)
    rel = relative_pose(center, observe(camera, offset=(5, 0, 0)))  # to the subject's left
    assert rel["x"] == pytest.approx(5, abs=1e-6)
    assert rel["z"] == pytest.approx(0, abs=1e-6)


@pytest.mark.parametrize("mount", MOUNTS)
def test_pipeline_output_is_mount_independent(mount):
    camera = MOUNTS[mount]
    cfg = Config()
    cfg.gaze.enabled = False
    p = PosePipeline(cfg)
    t = 0.0
    for _ in range(RECENTER_FRAMES):
        p.update(observe(camera, t), t)
        t += 1 / 30
    rel = p.raw_relative(observe(camera, t, head=rotation_from_angles(40, 20, 0), offset=(0, 0, 5)))
    assert rel["yaw"] == pytest.approx(40, abs=1e-3) and rel["pitch"] == pytest.approx(20, abs=1e-3)
    assert abs(rel["roll"]) < 1.0
    assert rel["z"] == pytest.approx(5, abs=1e-3)
    assert abs(rel["x"]) < 0.01 and abs(rel["y"]) < 0.01


def test_angles_and_rotation_roundtrip():
    for yaw, pitch, roll in ((0, 0, 0), (40, 20, 0), (-70, -35, 12), (170, 10, -20)):
        assert angles_from_rotation(rotation_from_angles(yaw, pitch, roll)) == pytest.approx(
            (yaw, pitch, roll), abs=1e-9)


# -- calibration ----------------------------------------------------------------

def test_calibration_head_range_is_mount_independent():
    camera = MOUNTS["above side monitor, yaw 30 tilt 15"]
    s = CalibrationSession(0.0)
    t = 0.0
    while not s.finished and t < 120:
        t += 1 / 30
        step = s.step
        head = np.eye(3)
        if step is not None and step.kind == "head" and s.phase == "collect":
            yaw = {"left": 30, "right": -30}.get(step.direction, 0)
            pitch = {"up": 18, "down": -18}.get(step.direction, 0)
            head = rotation_from_angles(yaw, pitch, 0)
        dx = dy = 0.0
        if step is not None and step.kind == "gaze" and s.phase in ("settle", "collect"):
            # iris offsets of 0.2 eye widths sideways, 0.1 up/down
            parts = step.direction.split("-")
            dx = sum({"left": 0.02, "right": -0.02}.get(d, 0.0) for d in parts)
            dy = sum({"up": -0.01, "down": 0.01}.get(d, 0.0) for d in parts)
        eyes = [p for cx in (0.45, 0.6) for p in (
            (cx - 0.05, 0.5), (cx + 0.05, 0.5), (cx, 0.49), (cx, 0.51), (cx + dx, 0.5 + dy))]
        s.update(build_observation(t, mediapipe_matrix(camera, head=head), eyes, 1, 1), t)
    assert s.phase == "done"
    assert s.result.head_yaw_range == pytest.approx(30, abs=0.5)
    assert s.result.head_pitch_range == pytest.approx(18, abs=0.5)


def test_calibration_file_keeps_center_rotation(tmp_path):
    camera = MOUNTS["above, tilted down 20"]
    center = observe(camera)
    cal = Calibration(center={k: getattr(center, k) for k in
                              ("yaw", "pitch", "roll", "x", "y", "z", "gaze_x", "gaze_y", "blink")}
                      | {"rotation": center.matrix.tolist()})
    cal.save(tmp_path / "calibration.json")
    loaded = Calibration.load(tmp_path / "calibration.json").center_observation()
    assert np.allclose(loaded.matrix, center.matrix)
    rel = relative_pose(loaded, observe(camera, head=rotation_from_angles(40, 20, 0)))
    assert rel["yaw"] == pytest.approx(40, abs=1e-6) and abs(rel["roll"]) < 1.0


def test_old_calibration_file_without_rotation_still_loads():
    cal = Calibration(center=dict(yaw=10.0, pitch=-5.0, roll=2.0, x=0.0, y=0.0, z=-50.0,
                                  gaze_x=0.0, gaze_y=0.0, blink=0.0))
    c = cal.center_observation()
    assert c.rotation is None
    assert angles_from_rotation(c.matrix) == pytest.approx((10.0, -5.0, 2.0))


# -- phone orientation ----------------------------------------------------------

def phone_message(camera, w, h, head=np.eye(3), angle=None):
    g = [c for p in EYES for c in p]
    msg = {"t": 1, "face": 1, "w": w, "h": h, "m": mediapipe_matrix(camera, head=head).tolist(), "g": g}
    if angle is not None:
        msg["o"] = angle
    return msg


def centered_on(p, camera, w, h, angle=None, t=0.0):
    for _ in range(RECENTER_FRAMES):
        p.update(parse_message(phone_message(camera, w, h, angle=angle), t), t)
        t += 1 / 30
    assert p.centered and not p.needs_center
    return t


def test_phone_in_portrait_or_landscape_tracks_correctly():
    cfg = Config()
    for w, h, camera in ((480, 640, _rx(15)), (640, 480, _rx(15) @ _rz(90))):
        p = PosePipeline(cfg)
        t = 0.0
        for _ in range(RECENTER_FRAMES):
            p.update(parse_message(phone_message(camera, w, h), t), t)
            t += 1 / 30
        obs = parse_message(phone_message(camera, w, h, rotation_from_angles(40, 20, 0)), t)
        assert obs.extra["orientation"] == ("portrait" if h > w else "landscape")
        rel = p.raw_relative(obs)
        assert rel["yaw"] == pytest.approx(40, abs=1e-3) and rel["pitch"] == pytest.approx(20, abs=1e-3)


def test_turning_the_phone_recenters():
    p = PosePipeline(Config())
    t = 0.0
    portrait = _rx(15)
    for _ in range(RECENTER_FRAMES):
        p.update(parse_message(phone_message(portrait, 480, 640), t), t)
        t += 1 / 30
    assert p.centered and p.center.extra["orientation"] == "portrait"
    landscape = portrait @ _rz(90)
    p.update(parse_message(phone_message(landscape, 640, 480), t), t)
    assert p.needs_center
    for _ in range(RECENTER_FRAMES):
        t += 1 / 30
        p.update(parse_message(phone_message(landscape, 640, 480), t), t)
    assert p.center.extra["orientation"] == "landscape"
    obs = parse_message(phone_message(landscape, 640, 480, rotation_from_angles(30, 0, 0)), t)
    assert p.raw_relative(obs)["yaw"] == pytest.approx(30, abs=1e-3)


def test_screen_angle_is_the_orientation_key():
    camera = _rx(15)
    assert parse_message(phone_message(camera, 640, 480, angle=90), 0).extra["orientation"] == 90
    assert parse_message(phone_message(camera, 640, 480, angle=-90), 0).extra["orientation"] == 270
    assert parse_message(phone_message(camera, 480, 640, angle=0), 0).extra["orientation"] == 0
    # older phone pages send no angle: portrait/landscape from the frame shape
    assert parse_message(phone_message(camera, 480, 640), 0).extra["orientation"] == "portrait"
    assert parse_message(phone_message(camera, 640, 480, angle="x"), 0).extra["orientation"] == "landscape"


@pytest.mark.parametrize("w, h, before, after", [(640, 480, 90, 270), (480, 640, 0, 180)])
def test_180_degree_flip_recenters(w, h, before, after):
    p = PosePipeline(Config())
    upright = _rx(15)
    t = centered_on(p, upright, w, h, angle=before)
    flipped = upright @ _rz(180)  # same frame shape, image upside down
    p.update(parse_message(phone_message(flipped, w, h, angle=after), t), t)
    assert p.needs_center
    t = centered_on(p, flipped, w, h, angle=after, t=t)
    assert p.center.extra["orientation"] == after
    obs = parse_message(phone_message(flipped, w, h, rotation_from_angles(30, 10, 0), angle=after), t)
    rel = p.raw_relative(obs)
    assert rel["yaw"] == pytest.approx(30, abs=1e-3) and rel["pitch"] == pytest.approx(10, abs=1e-3)


def test_upside_down_camera_recenters_even_without_an_angle_change():
    """Safety net for browsers whose screen angle doesn't follow the camera frames."""
    p = PosePipeline(Config())
    upright = _rx(15)
    t = centered_on(p, upright, 640, 480, angle=90)
    p.update(parse_message(phone_message(upright @ _rz(180), 640, 480, angle=90), t), t)
    assert p.needs_center


def test_normal_head_movement_never_triggers_a_recenter():
    p = PosePipeline(Config())
    camera = _rx(15) @ _rz(90)
    t = centered_on(p, camera, 640, 480, angle=90)
    for yaw, pitch, roll in ((70, 0, 0), (-70, 40, 0), (0, -50, 45), (60, 30, -60), (-80, -40, 60)):
        t += 1 / 30
        p.update(parse_message(phone_message(camera, 640, 480, rotation_from_angles(yaw, pitch, roll),
                                             angle=90), t), t)
        assert not p.needs_center, (yaw, pitch, roll)


def test_old_and_new_orientation_styles_are_not_a_change():
    assert orientation_changed(90, 270) and orientation_changed("portrait", "landscape")
    assert not orientation_changed("landscape", 90) and not orientation_changed(0, "portrait")
    assert not orientation_changed(None, 90) and not orientation_changed(0, 0)


@pytest.mark.parametrize("saved", ["landscape", "portrait", 0, 270])
def test_calibration_orientation_loads_old_and_new_styles(tmp_path, saved):
    camera = _rx(15)
    center = dict(yaw=0.0, pitch=0.0, roll=0.0, x=0.0, y=0.0, z=-50.0, gaze_x=0.0, gaze_y=0.0, blink=0.0,
                  orientation=saved)
    Calibration(center=center).save(tmp_path / "calibration.json")
    cal = Calibration.load(tmp_path / "calibration.json")
    assert cal.center_observation().extra["orientation"] == saved
    cfg = Config()
    cfg.tracking.auto_center = False
    p = PosePipeline(cfg, cal)
    assert p.centered
    # A new phone page sends angles: an old "landscape" center is unknown, not a change.
    obs = parse_message(phone_message(camera, 640, 480, angle=saved if isinstance(saved, int) else 90), 0)
    p.update(obs, 0.0)
    assert not p.needs_center


def test_calibration_saves_screen_angle_zero():
    s = CalibrationSession(0.0)
    t = 0.0
    while s.center is None and t < 30:
        t += 1 / 30
        s.update(parse_message(phone_message(_rx(15), 480, 640, angle=0), t), t)
    assert s.result.center["orientation"] == 0


# -- monitors -------------------------------------------------------------------

SCREENS = [Monitor(-1920, 0, 1920, 1080), Monitor(0, 0, 2560, 1440, primary=True),
           Monitor(2560, 100, 1920, 1080)]


def test_monitor_setting_picks_the_right_screen():
    assert pick(SCREENS, 0) == SCREENS[1]
    assert pick(SCREENS, 1) == SCREENS[0] and pick(SCREENS, 3) == SCREENS[2]
    assert pick(SCREENS, 9) == SCREENS[1]  # unknown: primary
    everything = pick(SCREENS, "all")
    assert everything == span(SCREENS)
    assert (everything.left, everything.top, everything.width, everything.height) == (-1920, 0, 6400, 1440)


def test_monitor_config_values():
    assert Config().calibration.monitor == 0
    assert parse_config("[calibration]\nmonitor = 2\n").calibration.monitor == 2
    assert parse_config("[calibration]\nmonitor = 'All'\n").calibration.monitor == "all"
    for bad in ("-1", "'left'", "true"):
        with pytest.raises(ConfigError):
            parse_config(f"[calibration]\nmonitor = {bad}\n")


def test_overlay_uses_chosen_monitor_size(monkeypatch):
    monkeypatch.setattr(calibration_overlay, "list_monitors", lambda: SCREENS)
    assert (calibration_overlay.CalibrationOverlay(show=False, monitor=1).w,
            calibration_overlay.CalibrationOverlay(show=False, monitor=1).h) == (1920, 1080)
    wide = calibration_overlay.CalibrationOverlay(show=False, monitor="all")
    assert (wide.w, wide.h) == (6400, 1440)
    assert calibration_overlay.CalibrationOverlay(show=False).w == 2560
    assert not math.isnan(wide._scale)
