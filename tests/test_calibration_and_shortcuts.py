import pytest

from openitrack.calibration import STEPS, TARGETS, Calibration, CalibrationSession
from openitrack.config import Config
from openitrack.features import Observation, angles_from_rotation, rotation_from_angles
from openitrack.gaze_model import TARGET_REACH, Head, eye_point, eye_point_for
from openitrack.hotkeys import Binding, HotkeyListener, parse_binding
from openitrack.mapping import RECENTER_FRAMES, PosePipeline


def make_obs(t, yaw=0.0, pitch=0.0, gx=0.0, gy=0.0, blink=0.0, z=-50.0, x=0.0):
    return Observation(t=t, yaw=yaw, pitch=pitch, roll=0.0, x=x, y=0.0, z=z,
                       gaze_x=gx, gaze_y=gy, blink=blink)


# A simulated user with an off-axis neutral pose (5 deg yaw, -3 deg pitch).
NEUTRAL = dict(yaw=5.0, pitch=-3.0, gx=0.01, gy=-0.02)
EYES = {"left": (0.22, 0.0), "right": (-0.18, 0.0), "up": (0.0, 0.09), "down": (0.0, -0.06)}
EYES |= {f"{v}-{h}": (EYES[h][0], EYES[v][1]) for v in ("up", "down") for h in ("left", "right")}
HEAD = {"left": (32.0, 0.0), "right": (-28.0, 0.0), "up": (0.0, 20.0), "down": (0.0, -16.0)}


def user(session, t, head_drift=0.0):
    """What a cooperative user's face looks like for the current step."""
    step = session.step
    o = dict(NEUTRAL)
    if step is not None and session.phase in ("settle", "collect"):
        if step.kind == "gaze":
            dx, dy = EYES[step.direction]
            o["gx"] += dx
            o["gy"] += dy
            o["yaw"] += head_drift
        elif step.kind == "head" and session.phase == "collect":
            # The turn happens relative to the neutral head, not in camera angles.
            dyaw, dpitch = HEAD[step.direction]
            r = rotation_from_angles(o["yaw"], o["pitch"], 0.0) @ rotation_from_angles(dyaw, dpitch, 0.0)
            o["yaw"], o["pitch"], _ = angles_from_rotation(r)
    return make_obs(t, **o)


def run_session(drift_on=None, max_seconds=120.0):
    s = CalibrationSession(0.0)
    t = 0.0
    while not s.finished and t < max_seconds:
        t += 1 / 30
        drift = 15.0 if (drift_on and s.step and s.step.direction == drift_on
                         and s.step.kind == "gaze" and s.retries == 0) else 0.0
        s.update(user(s, t, drift), t)
    return s, t


def test_full_calibration_measures_user():
    s, t = run_session()
    assert s.phase == "done"
    assert t < 60  # the whole guided flow stays short
    r = s.result
    assert r.center["yaw"] == pytest.approx(5.0) and r.center["gaze_x"] == pytest.approx(0.01)
    assert r.gaze_left == pytest.approx(0.22)
    assert r.gaze_right == pytest.approx(0.18)
    assert r.gaze_up == pytest.approx(0.09)
    assert r.gaze_down == pytest.approx(0.06)
    assert r.head_yaw_range == pytest.approx(30.0)    # mean of 32 and 28
    assert r.head_pitch_range == pytest.approx(18.0)  # mean of 20 and 16
    assert r.gaze_fit["points"] == 9 and r.gaze_fit["rms"] < 0.02
    ranges = {d: getattr(r, f"gaze_{d}") for d in ("left", "right", "up", "down")}
    for direction, (dx, dy) in EYES.items():
        target = TARGETS[direction]
        assert eye_point(dx, dy, ranges, r.gaze_fit) == pytest.approx(target, abs=0.02)


def test_head_moved_during_eye_steps_is_taken_out_of_the_fit():
    # At the left dot the user also slid 6 cm to their left, so their eyes only
    # had to turn part of the way. Taking that out must give the same eye map
    # as a user who kept still.
    shift = 6.0
    needed = eye_point_for(TARGETS["left"], Head(x=shift), 50.0, 50.0, 16 / 9)[0]
    turn = needed / -TARGET_REACH
    assert turn < 0.8  # the shift really did save a lot of eye turn
    s = CalibrationSession(0.0)
    t = 0.0
    while not s.finished and t < 120:
        t += 1 / 30
        o = user(s, t)
        if s.step is not None and s.step.direction == "left" and s.step.kind == "gaze"                 and s.phase in ("settle", "collect"):
            o = make_obs(t, yaw=o.yaw, pitch=o.pitch, gx=NEUTRAL["gx"] + EYES["left"][0] * turn,
                         gy=o.gaze_y, x=shift)
        s.update(o, t)
    still, _ = run_session()
    for dx, dy in EYES.values():
        assert eye_point(dx, dy, {}, s.result.gaze_fit) == pytest.approx(
            eye_point(dx, dy, {}, still.result.gaze_fit), abs=0.03)


def test_head_drift_during_eye_step_asks_for_retry():
    s = CalibrationSession(0.0)
    t, saw_retry = 0.0, False
    while not s.finished and t < 120:
        t += 1 / 30
        drift = 15.0 if (s.step and s.step.direction == "left" and s.step.kind == "gaze"
                         and s.retries == 0) else 0.0
        s.update(user(s, t, drift), t)
        if s.phase == "message" and not s.message_ok and "head moved" in s.message:
            saw_retry = True
    assert saw_retry
    assert s.phase == "done" and s.result.gaze_left == pytest.approx(0.22)


def test_calibration_waits_for_face_and_can_cancel():
    s = CalibrationSession(0.0)
    for i in range(300):  # 10 s without a face: still on the intro
        s.update(None, i / 30)
    assert s.phase == "intro"
    s.cancel(10.0)
    assert s.finished and s.phase == "failed"


def test_blinks_are_ignored_while_measuring_eyes():
    s = CalibrationSession(0.0)
    t = 0.0
    while not s.finished and t < 120:
        t += 1 / 30
        o = user(s, t)
        if int(t * 30) % 4 == 0:  # every 4th frame: eyes shut, iris reading is junk
            o = make_obs(t, yaw=o.yaw, pitch=o.pitch, gx=-0.5, gy=-0.5, blink=0.9)
        s.update(o, t)
    assert s.result.gaze_left == pytest.approx(0.22)


def test_calibration_save_load_roundtrip(tmp_path):
    s, _ = run_session()
    path = tmp_path / "calibration.json"
    s.result.save(path)
    loaded = Calibration.load(path)
    assert loaded == s.result
    assert Calibration.load(tmp_path / "missing.json") is None
    (tmp_path / "bad.json").write_text("{not json")
    assert Calibration.load(tmp_path / "bad.json") is None


def test_steps_cover_every_direction():
    kinds = {(st.kind, st.direction) for st in STEPS}
    for d in ("left", "right", "up", "down"):
        assert ("gaze", d) in kinds and ("head", d) in kinds
    gaze_dots = {st.direction for st in STEPS if st.kind in ("center", "gaze")}
    assert gaze_dots == set(TARGETS) and len(gaze_dots) == 9


# -- calibration applied to the pipeline ---------------------------------------

def centered_pipeline(cfg, cal=None):
    p = PosePipeline(cfg, cal)
    t = 0.0
    for _ in range(RECENTER_FRAMES):
        p.update(make_obs(t), t)
        t += 1 / 30
    return p, t


def settle(p, t, n=150, **kw):
    pose = None
    for _ in range(n):
        t += 1 / 30
        pose = p.update(make_obs(t, **kw), t)
    return pose, t


def test_calibrated_head_range_sets_full_turn():
    cfg = Config()
    cfg.gaze.enabled = False
    p, t = centered_pipeline(cfg, Calibration(head_yaw_range=20.0))
    pose, _ = settle(p, t, yaw=20.0)
    assert pose.yaw == pytest.approx(cfg.axes.yaw.output, rel=0.02)
    cfg.calibration.use_head = False
    p.set_config(cfg)
    pose, _ = settle(p, t + 10, yaw=20.0)
    assert pose.yaw < cfg.axes.yaw.output * 0.8  # back to the 35 deg config range


def test_calibrated_gaze_ranges_are_per_direction():
    cfg = Config()
    cfg.axes.yaw.deadzone = 0.0
    p, t = centered_pipeline(cfg, Calibration(gaze_left=0.1, gaze_right=0.3))
    pose, t = settle(p, t, gx=0.1)
    assert pose.yaw == pytest.approx(cfg.gaze.yaw_degrees, rel=0.02)
    # Right has a 0.3 range: 0.1 is inside the deadzone, 0.2 is about a third of full.
    pose, t = settle(p, t, gx=-0.1)
    assert pose.yaw == 0
    pose, t = settle(p, t, gx=-0.2)
    assert -cfg.gaze.yaw_degrees * 0.5 < pose.yaw < -cfg.gaze.yaw_degrees * 0.2


# -- shortcut-driven modes -------------------------------------------------------

def run_modes(p, t, seconds, yaw, **flags):
    pose = None
    for _ in range(int(seconds * 30)):
        t += 1 / 30
        pose = p.update(make_obs(t, yaw=yaw), t, **flags)
    return pose, t


def test_freeze_hold_keeps_view_then_resumes():
    cfg = Config()
    cfg.gaze.enabled = False
    p, t = centered_pipeline(cfg)
    live, t = run_modes(p, t, 3, yaw=20)
    frozen, t = run_modes(p, t, 2, yaw=-20, freeze=True)
    assert frozen.yaw == pytest.approx(live.yaw, rel=0.01)
    resumed, t = run_modes(p, t, 2, yaw=-20)
    assert resumed.yaw < 0


def test_center_hold_and_calibrating_output_zero():
    cfg = Config()
    p, t = centered_pipeline(cfg)
    run_modes(p, t, 3, yaw=20)
    held, t = run_modes(p, t + 3, 1, yaw=20, center_hold=True)
    assert held.yaw == pytest.approx(0)
    cal, t = run_modes(p, t, 1, yaw=20, calibrating=True)
    assert cal.yaw == pytest.approx(0)


def test_toggles_fade_axes_and_sensitivity_is_clamped():
    cfg = Config()
    p, t = centered_pipeline(cfg)
    p.toggle_translation()
    p.toggle_roll()
    pose = None
    for _ in range(30):
        t += 1 / 30
        o = make_obs(t, z=-40.0)  # leaned in 10 cm
        o.roll = 20.0
        pose = p.update(o, t)
    assert pose.z == 0 and pose.roll == 0
    for _ in range(40):
        p.change_sensitivity(True)
    assert p.sensitivity == 3.0
    for _ in range(80):
        p.change_sensitivity(False)
    assert p.sensitivity == 0.3


def test_sensitivity_scales_rotation():
    cfg = Config()
    cfg.gaze.enabled = False
    p, t = centered_pipeline(cfg)
    base, t = run_modes(p, t, 3, yaw=10)
    p.change_sensitivity(True)
    boosted, t = run_modes(p, t, 1, yaw=10)
    assert boosted.yaw == pytest.approx(base.yaw * 1.1, rel=0.01)


def test_per_axis_sensitivity_scales_only_its_axis():
    cfg = Config()
    cfg.gaze.enabled = False
    p, t = centered_pipeline(cfg)
    base, t = settle(p, t, 90, yaw=10, pitch=5)
    cfg.axes.yaw.sensitivity = 2.0
    p.set_config(cfg)
    boosted, t = settle(p, t, 30, yaw=10, pitch=5)
    assert boosted.yaw == pytest.approx(base.yaw * 2.0, rel=0.01)
    assert boosted.pitch == pytest.approx(base.pitch, rel=0.01)
    p.change_sensitivity(True)  # the session-wide one multiplies on top
    both, t = settle(p, t, 30, yaw=10, pitch=5)
    assert both.yaw == pytest.approx(base.yaw * 2.2, rel=0.01)


# -- bindings ------------------------------------------------------------------------

def test_parse_bindings():
    assert parse_binding("mouse5") == Binding("mouse", 0, 0x06)
    assert parse_binding("ctrl+mouse4") == Binding("mouse", 0x0002, 0x05)
    assert parse_binding("joy2:button12") == Binding("joy", 0, 11, 1)
    assert parse_binding("joy1:b3") == Binding("joy", 0, 2, 0)
    assert parse_binding("ctrl+pageup") == Binding("key", 0x0002, 0x21)
    for spec in ("ctrl+F9", "mouse5", "joy3:button7", "shift+numpad5", "pageup"):
        assert str(parse_binding(str(parse_binding(spec)))) == str(parse_binding(spec))
    for bad in ("mouse1", "joy0:button1", "joy1:button33", "joy1", "ctrl+", "hyper+F1"):
        with pytest.raises(ValueError):
            parse_binding(bad)


def test_listener_routes_and_rejects_duplicates():
    hl = HotkeyListener({
        "recenter": "F9", "pause": "f9", "freeze_hold": "F8",
        "toggle_gaze": "mouse5", "calibrate": "joy1:button2", "toggle_roll": "",
    })
    assert set(hl._registered) == {"recenter"}                    # keyboard press: captured
    assert set(hl._polled) == {"freeze_hold", "toggle_gaze", "calibrate"}  # polled
    # "pause" duplicated F9 and was dropped; empty bindings are disabled.
    assert "pause" not in hl._registered and "pause" not in hl._polled
