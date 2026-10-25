"""The per-frame and per-tick shortcuts: they must save work without changing results."""

import collections
import queue
import tkinter as tk
from tkinter import ttk

import cv2
import numpy as np
import pytest
from mediapipe.tasks.python import vision

import openitrack.mapping as mapping
from openitrack.config import Config, ConfigFile, parse_config
from openitrack.config_edit import set_values
from openitrack.features import Observation, relative_pose, rotation_from_angles
from openitrack.gui import App, set_item_options, set_options
from openitrack.hotkeys import Binding, InputPoller
from openitrack.mapping import FLIPPED_ROLL_DEG, RECENTER_FRAMES, Pose, PosePipeline
from openitrack.preview import Preview
from openitrack.sources.phone import FAILURE_WINDOW, MAX_FAILURES, _Limits
from openitrack.sources.webcam import WebcamSource


def obs(t, yaw=0.0, pitch=0.0, roll=0.0, x=0.0):
    return Observation(t=t, yaw=yaw, pitch=pitch, roll=roll, x=x, y=0, z=-45, gaze_x=0, gaze_y=0, blink=0)


def centered(yaw=0.0, pitch=0.0):
    p = PosePipeline(Config())
    t = 0.0
    for _ in range(RECENTER_FRAMES):
        p.update(obs(t, yaw, pitch), t)
        t += 1 / 30
    assert p.centered
    return p, t


# -- pose pipeline ----------------------------------------------------------------------------
def test_center_keeps_its_rotation():
    p, _ = centered(yaw=10, pitch=-5)
    c = p.center
    assert c.rotation is not None  # the median center only had angles; now built once
    np.testing.assert_allclose(c.rotation, rotation_from_angles(c.yaw, c.pitch, c.roll))


def test_set_center_keeps_a_given_rotation_and_leaves_the_caller_alone():
    p = PosePipeline(Config())
    r = rotation_from_angles(5, 0, 0)
    exact = Observation(t=0, yaw=5, pitch=0, roll=0, x=0, y=0, z=-45, gaze_x=0, gaze_y=0, blink=0, rotation=r)
    p.set_center(exact)
    assert p.center is exact
    plain = obs(0, yaw=7)
    p.set_center(plain)
    assert plain.rotation is None and p.center.rotation is not None


def test_relative_pose_is_computed_once_per_frame(monkeypatch):
    p, t = centered()
    calls = []
    real = mapping.relative_pose
    monkeypatch.setattr(mapping, "relative_pose", lambda c, o: calls.append(o) or real(c, o))
    o = obs(t, yaw=20, pitch=5)
    p.update(o, t)                 # flip check + tracking
    raw = p.raw_relative(o)        # preview
    assert calls == [o]
    assert raw == real(p.center, o) | {"gx": 0.0, "gy": 0.0}
    p.update(obs(t + 0.03, yaw=21), t + 0.03)
    assert len(calls) == 2


def test_relative_pose_follows_a_new_center():
    p, t = centered()
    o = obs(t, yaw=20)
    assert p.raw_relative(o)["yaw"] == pytest.approx(20)
    p.set_center(obs(t, yaw=15))
    assert p.raw_relative(o)["yaw"] == pytest.approx(5)
    assert p.raw_relative(o)["yaw"] == pytest.approx(relative_pose(p.center, o)["yaw"])


def test_upside_down_camera_still_recenters():
    p, t = centered()
    p.update(obs(t, roll=FLIPPED_ROLL_DEG + 30), t)
    assert p.needs_center


def test_tracking_result_is_unchanged_by_the_cache():
    """Same poses as computing everything from scratch for every frame."""
    p, t = centered(yaw=3)
    q, _ = centered(yaw=3)
    q._relative = lambda o: dict(relative_pose(q.center, o), gx=0.0, gy=0.0)  # uncached
    for i in range(60):
        t += 1 / 30
        o = obs(t, yaw=3 + i * 0.7, pitch=np.sin(i / 7) * 10, x=i * 0.1)
        assert p.update(o, t).as_tuple() == pytest.approx(q.update(o, t).as_tuple())


# -- preview panel --------------------------------------------------------------------------------
@pytest.mark.parametrize("size", [(480, 640), (360, 240)])
def test_preview_panel_matches_full_frame_blend(size):
    rng = np.random.default_rng(1)
    img = rng.integers(0, 256, (*size, 3), dtype=np.uint8)

    # What the preview did before: blend a 55% black box over a copy of the whole frame.
    expected = img.copy()
    overlay = expected.copy()
    cv2.rectangle(overlay, (0, 0), (300, 30 + 22 * 9), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.55, expected, 0.45, 0, expected)

    got = img.copy()
    Preview._panel(got, Pose(), None, "", "")
    # Text and bars are drawn on top; compare the pixels they leave alone
    # (on a black frame those are the only non-black pixels).
    marks = np.zeros_like(img)
    Preview._panel(marks, Pose(), None, "", "")
    untouched = (marks == 0).all(axis=2)
    assert untouched.mean() > 0.8
    assert np.abs(got.astype(int) - expected.astype(int))[untouched].max() <= 1


# -- hotkey polling ---------------------------------------------------------------------------------
class FakePoller(InputPoller):
    def __init__(self, masks):
        self._retry_at = {}
        self.masks = masks
        self.reads = collections.Counter()

    def vk_down(self, vk):
        return False

    def joy_buttons(self, joy):
        self.reads[joy] += 1
        return self.masks.get(joy)


def test_each_joystick_is_read_once_per_poll():
    poller = FakePoller({0: 0b101})
    bindings = [Binding("joy", 0, 0, 0), Binding("joy", 0, 1, 0), Binding("joy", 0, 2, 0),
                Binding("joy", 0, 3, 1)]   # joystick 1 isn't connected
    masks: dict = {}
    assert [poller.is_down(b, masks) for b in bindings] == [True, False, True, False]
    assert poller.reads == {0: 1, 1: 1}


def test_is_down_without_a_cache_still_reads_the_joystick():
    poller = FakePoller({0: 0b10})
    assert poller.is_down(Binding("joy", 0, 1, 0))
    assert poller.is_down(Binding("joy", 0, 1, 0))
    assert poller.reads[0] == 2


# -- phone server failure table --------------------------------------------------------------------
def test_failure_table_forgets_expired_addresses():
    lim = _Limits()
    for i in range(100):
        lim.fail(f"10.0.0.{i}", 0.0)
    lim.fail("10.0.1.1", FAILURE_WINDOW + 1)
    assert list(lim.failures) == ["10.0.1.1"]


def test_failure_table_still_blocks_within_the_window():
    lim = _Limits()
    for k in range(MAX_FAILURES):
        lim.fail("10.0.0.9", k * 1.0)
    assert lim.blocked("10.0.0.9", MAX_FAILURES)
    assert not lim.blocked("10.0.0.9", FAILURE_WINDOW + MAX_FAILURES + 1)
    assert "10.0.0.9" not in lim.failures


# -- blendshapes setting ------------------------------------------------------------------------------
def test_blendshapes_setting():
    assert Config().tracking.blendshapes is True
    assert parse_config("[tracking]\nblendshapes = false\n").tracking.blendshapes is False


@pytest.mark.parametrize("enabled", [True, False])
def test_webcam_asks_mediapipe_for_blendshapes_only_when_enabled(monkeypatch, tmp_path, enabled):
    seen = []
    monkeypatch.setattr(vision.FaceLandmarker, "create_from_options", lambda options: seen.append(options))
    cfg = Config()
    cfg.tracking.blendshapes = enabled
    WebcamSource(cfg.source, cfg.tracking, tmp_path / "model.task")
    assert seen[0].output_face_blendshapes is enabled


# -- window refresh -------------------------------------------------------------------------------------
def test_set_options_skips_unchanged_values(tk_root):
    label = ttk.Label(tk_root, text="a")
    button = ttk.Button(tk_root, text="Go")
    assert not set_options(label, text="a")
    assert set_options(label, text="b") and label.cget("text") == "b"
    assert not set_options(label, text="b")
    assert set_options(button, state="disabled") and str(button.cget("state")) == "disabled"
    assert not set_options(button, state="disabled")
    assert set_options(button, state="normal", text="Go") and str(button.cget("state")) == "normal"

    canvas = tk.Canvas(tk_root)
    dot = canvas.create_oval(0, 0, 5, 5, fill="#ff0000")
    assert not set_item_options(canvas, dot, fill="#ff0000")
    assert set_item_options(canvas, dot, fill="#00ff00") and canvas.itemcget(dot, "fill") == "#00ff00"


def test_blendshapes_switch_saves_to_config_toml_even_with_a_profile(tk_root, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    path = tmp_path / "config.toml"
    cf = ConfigFile(path)
    cf.profiles.create("Game", cf.base)
    set_values(path, "profiles", {"active": "Game"})

    app = App(tk_root, path, queue.Queue())
    assert app.cf.profile == "Game" and app.blendshapes_var.get() is True

    def find(widget):
        for child in widget.winfo_children():
            if isinstance(child, ttk.Checkbutton) and "blink" in str(child.cget("text")):
                return child
            found = find(child)
            if found is not None:
                return found

    switch = find(tk_root)
    assert switch is not None
    switch.invoke()

    assert app.blendshapes_var.get() is False
    assert ConfigFile(path, "").base.tracking.blendshapes is False
    assert "blendshapes" not in cf.profiles.load("Game").settings.get("tracking", {})
    assert app.cf.profile == "Game"
