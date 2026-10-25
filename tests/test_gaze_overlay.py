import math
import sys

import pytest

from openitrack.calibration import Calibration
from openitrack.config import Config, ConfigError, parse_config
from openitrack.features import Observation
from openitrack.gaze_model import (DEFAULT_DISTANCE_CM, TARGET_REACH, Head, eye_point, eye_point_for,
                                   fit_eye_map, screen_point, viewing_distance)
from openitrack.gaze_overlay import GazeDot
from openitrack.mapping import RECENTER_FRAMES, PosePipeline
from openitrack.monitors import Monitor

RANGES = {"left": 0.2, "right": 0.25, "up": 0.1, "down": 0.08}


def obs(t, yaw=0.0, gx=0.0, gy=0.0, blink=0.0, x=0.0):
    return Observation(t=t, yaw=yaw, pitch=0, roll=0, x=x, y=0, z=-45, gaze_x=gx, gaze_y=gy, blink=blink)


def run(p, frames=150, t=0.0, **kw):
    for _ in range(frames):
        p.update(obs(t, **kw), t)
        t += 1 / 30
    return t


def point(gx=0.0, gy=0.0, head=Head(), fit=None, degrees=50):
    return screen_point(eye_point(gx, gy, RANGES, fit), head, 60.0, degrees, 16 / 9)


def test_calibration_targets_land_on_their_dots():
    # The offsets measured at each calibration dot put the gaze dot back on it.
    assert point() == (0, 0)
    assert point(0.2) == pytest.approx((-TARGET_REACH, 0))
    assert point(-0.25) == pytest.approx((TARGET_REACH, 0))
    assert point(gy=0.1) == pytest.approx((0, TARGET_REACH))
    assert point(gy=-0.08) == pytest.approx((0, -TARGET_REACH))


def test_head_turn_moves_the_point_and_eyes_can_cancel_it():
    left_turn = point(head=Head(yaw=10))[0]
    assert left_turn < -0.3
    # Turning the head left while the eyes swing back right keeps you on center.
    eye_right = -0.25 * 10 / 22.0  # about 10 deg of eye turn (target is ~22.8 deg)
    assert abs(point(eye_right, head=Head(yaw=10))[0]) < 0.05


def test_head_position_moves_the_point():
    # Half-screen at 60 cm and 50 deg is 28 cm: a 7 cm shift is a quarter of it.
    half = 60 * math.tan(math.radians(25))
    assert point(head=Head(x=7))[0] == pytest.approx(-7 / half)   # subject's left = screen left
    assert point(head=Head(y=-3))[1] == pytest.approx(-3 * 16 / 9 / half)
    # Leaning in shrinks how far the same eye turn reaches.
    near = point(0.2, head=Head(z=20))[0]
    assert near == pytest.approx(-TARGET_REACH * 40 / 60)


def test_eye_point_for_inverts_screen_point():
    for target in ((0.9, 0.9), (-0.9, 0.0), (0.3, -0.9)):
        for head in (Head(), Head(4, -3, 2, -1, 5), Head(-6, 2, -3, 2, -8)):
            eye = eye_point_for(target, head, 55.0, 60, 16 / 10)
            assert screen_point(eye, head, 55.0, 60, 16 / 10) == pytest.approx(target)


def _uneven_eyes(ex, ey):
    """A made-up user whose iris offset is not a straight scale of where they look:
    each direction travels differently and looking up also shifts gaze_x."""
    gx = -ex * (0.20 if ex < 0 else 0.25) / TARGET_REACH + 0.02 * ey
    gy = ey * (0.10 if ey > 0 else 0.07) / TARGET_REACH - 0.01 * ex * ey
    return gx, gy


def test_fit_puts_all_nine_dots_back_on_target():
    dots = [(x, y) for x in (-0.9, 0, 0.9) for y in (-0.9, 0, 0.9)]
    fit = fit_eye_map([(*_uneven_eyes(*d), *d) for d in dots])
    assert fit["points"] == 9 and len(fit["x"]) == 6 and fit["rms"] < 0.05
    for d in dots:
        assert point(*_uneven_eyes(*d), fit=fit) == pytest.approx(d, abs=0.06)
    # Corners are what the straight per-axis scale gets wrong.
    corner = (0.9, 0.9)
    no_fit = point(*_uneven_eyes(*corner))
    assert math.dist(no_fit, corner) > math.dist(point(*_uneven_eyes(*corner), fit=fit), corner)


def test_fit_needs_enough_dots():
    dots = [(0, 0), (-0.9, 0), (0.9, 0), (0, 0.9), (0, -0.9)]
    fit = fit_eye_map([(*_uneven_eyes(*d), *d) for d in dots])
    assert len(fit["x"]) == 3  # linear only without the corners
    assert fit_eye_map([(*_uneven_eyes(*d), *d) for d in dots[:4]]) is None


def test_point_is_clamped_to_the_screen():
    assert point(5, -5, Head(80, -80)) == (-1, -1)


def test_viewing_distance_from_center_pose():
    assert viewing_distance(-45.0) == 45.0
    assert viewing_distance(0.0) == DEFAULT_DISTANCE_CM   # no believable distance


def test_pipeline_gaze_point_follows_eyes_and_head():
    cfg = Config()
    p = PosePipeline(cfg, Calibration(gaze_left=0.2, gaze_right=0.25, gaze_up=0.1, gaze_down=0.08))
    assert p.gaze_point(16 / 9) is None  # not centered yet
    t = run(p, RECENTER_FRAMES)
    assert p.gaze_point(16 / 9) == pytest.approx((0, 0), abs=1e-6)
    t = run(p, t=t, gx=0.2)
    assert p.gaze_point(16 / 9) == pytest.approx((-TARGET_REACH, 0), abs=0.01)
    run(p, t=t, gx=0.2, yaw=-5)
    with_head = p.gaze_point(16 / 9)[0]
    assert with_head > -TARGET_REACH + 0.1
    cfg.overlay.follow_head = False
    assert p.gaze_point(16 / 9) == pytest.approx((-TARGET_REACH, 0), abs=0.01)


def test_pipeline_gaze_point_uses_the_fit_and_head_position():
    dots = [(x, y) for x in (-0.9, 0, 0.9) for y in (-0.9, 0, 0.9)]
    fit = fit_eye_map([(*_uneven_eyes(*d), *d) for d in dots])
    cfg = Config()
    p = PosePipeline(cfg, Calibration(gaze_fit=fit))
    t = run(p, RECENTER_FRAMES)
    corner = (-0.9, 0.9)
    t = run(p, t=t, gx=_uneven_eyes(*corner)[0], gy=_uneven_eyes(*corner)[1])
    assert p.gaze_point(16 / 9) == pytest.approx(corner, abs=0.06)
    # Sliding to the left (eyes unchanged) moves the dot left too.
    run(p, t=t, gx=_uneven_eyes(0, 0)[0], x=5.0)
    assert p.gaze_point(16 / 9)[0] < -0.15
    cfg.overlay.follow_head = False
    assert p.gaze_point(16 / 9) == pytest.approx((0, 0), abs=0.06)
    cfg.calibration.use_gaze = False
    p.set_config(cfg)
    run(p, t=t + 10, gx=0.2)
    # Back to the configured eye range.
    assert p.gaze_point(16 / 9)[0] == pytest.approx(-0.2 / cfg.gaze.horizontal_range * TARGET_REACH, abs=0.01)


def test_gaze_point_holds_through_blinks():
    p = PosePipeline(Config())
    t = run(p, RECENTER_FRAMES)
    t = run(p, t=t, gx=0.15)
    before = p.gaze_point(16 / 9)
    run(p, 20, t=t, gx=-0.3, blink=0.9)
    assert p.gaze_point(16 / 9) == pytest.approx(before)


def test_overlay_config():
    assert not Config().overlay.enabled
    cfg = parse_config("[overlay]\nenabled = true\nscreen_degrees = 120\n")
    assert cfg.overlay.enabled and cfg.overlay.screen_degrees == 120
    with pytest.raises(ConfigError):
        parse_config("[overlay]\nscreen_degrees = 0\n")
    with pytest.raises(ConfigError):
        parse_config("[overlay]\nsize = 2\n")


def test_dot_window_position_and_lifecycle():
    area = Monitor(1920, 0, 2560, 1440)
    dot = GazeDot(area, 40)
    try:
        assert dot.size == 53  # scaled for 1440p
        cx, cy = dot.position(0, 0)
        assert (cx + dot.size / 2, cy + dot.size / 2) == pytest.approx((1920 + 1280, 720), abs=1)
        assert dot.position(-1, 1) == pytest.approx((1920 - dot.size / 2, -dot.size / 2), abs=1)
        if sys.platform == "win32":
            assert dot.hwnd
        dot.hide()  # never shown: tests must not flash windows
    finally:
        dot.close()
    assert dot.hwnd is None
    dot.show_at(0, 0)  # closed: does nothing
