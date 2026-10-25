"""[source] width/height/format: config, what the webcam is asked for, and the Settings tab."""

import logging
import queue
from tkinter import ttk

import cv2
import pytest

from openitrack import gui
from openitrack.config import CAMERA_FORMATS, Config, ConfigError, ConfigFile, parse_config
from openitrack.config_edit import set_values
from openitrack.gui import FORMAT_CHOICES, FPS_CHOICES, RESOLUTION_CHOICES, App, choice_text
from openitrack.sources import webcam
from openitrack.sources.webcam import fourcc_name


# -- config ---------------------------------------------------------------------------------------
def test_format_default_and_choices():
    assert Config().source.format == "mjpg"
    for fmt in CAMERA_FORMATS:
        assert parse_config(f'[source]\nformat = "{fmt}"\n').source.format == fmt


@pytest.mark.parametrize("text", [
    '[source]\nformat = "h264"\n',
    '[source]\nformat = "MJPG"\n',
    '[source]\nwidth = 0\n',
    '[source]\nheight = 100\n',
    '[source]\nwidth = 8000\n',
])
def test_bad_camera_settings_are_refused(text):
    with pytest.raises(ConfigError):
        parse_config(text)


# -- what the camera is asked for -------------------------------------------------------------------
class FakeCapture:
    """Records the cv2.VideoCapture calls; reports `reported` sizes back."""

    instances: list = []

    def __init__(self, cam, backend=None, reported=None):
        self.calls: list = []
        self.reported = reported or {}
        FakeCapture.instances.append(self)

    def isOpened(self):
        return True

    def set(self, prop, value):
        self.calls.append((prop, value))
        return True

    def get(self, prop):
        return self.reported.get(prop, 0.0)

    def getBackendName(self):
        return "FAKE"

    def release(self):
        pass


@pytest.fixture
def fake_cv2(monkeypatch):
    FakeCapture.instances = []
    monkeypatch.setattr(webcam.cv2, "VideoCapture", FakeCapture)
    monkeypatch.setattr(webcam, "_delivers_real_frames", lambda cap: True)


@pytest.mark.parametrize("fmt", ["mjpg", "yuy2", "auto"])
def test_camera_is_asked_for_the_configured_format_before_the_size(fake_cv2, fmt):
    cfg = Config().source
    cfg.format, cfg.width, cfg.height = fmt, 320, 240
    cap = webcam._open_capture(cfg)
    props = [p for p, _ in cap.calls]
    fourccs = [v for p, v in cap.calls if p == cv2.CAP_PROP_FOURCC]
    if fmt == "auto":
        assert fourccs == []
    else:
        assert fourccs == [cv2.VideoWriter_fourcc(*CAMERA_FORMATS[fmt])]
        assert props.index(cv2.CAP_PROP_FOURCC) < props.index(cv2.CAP_PROP_FRAME_WIDTH)
    assert (cv2.CAP_PROP_FRAME_WIDTH, 320) in cap.calls and (cv2.CAP_PROP_FRAME_HEIGHT, 240) in cap.calls


def test_ip_cameras_are_not_asked_for_a_format(fake_cv2):
    cfg = Config().source
    cfg.camera = "http://192.168.1.20:4747/video"
    cap = webcam._open_capture(cfg)
    assert all(p != cv2.CAP_PROP_FOURCC for p, _ in cap.calls)


def test_fourcc_name():
    assert fourcc_name(cv2.VideoWriter_fourcc(*"MJPG")) == "MJPG"
    assert fourcc_name(cv2.VideoWriter_fourcc(*"YUY2")) == "YUY2"
    assert fourcc_name(0) == "" and fourcc_name(-1) == "" and fourcc_name(1) == ""


def _opened_source(cv2, webcam, cfg, width, height, fourcc=0.0):
    src = webcam.WebcamSource.__new__(webcam.WebcamSource)  # no MediaPipe needed for this
    src.cfg = cfg
    src._cap = FakeCapture(0, reported={cv2.CAP_PROP_FRAME_WIDTH: width, cv2.CAP_PROP_FRAME_HEIGHT: height,
                                        cv2.CAP_PROP_FOURCC: fourcc})
    return src


def test_log_says_what_the_camera_delivers(fake_cv2, caplog):
    cfg = Config().source
    src = _opened_source(cv2, webcam, cfg, 640, 480, cv2.VideoWriter_fourcc(*"MJPG"))
    with caplog.at_level(logging.INFO, logger=webcam.log.name):
        src._log_opened()
    assert "opened at 640x480 (MJPG)" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_log_warns_when_the_camera_ignores_the_size(fake_cv2, caplog):
    cfg = Config().source
    cfg.width, cfg.height, cfg.format = 1280, 720, "yuy2"
    src = _opened_source(cv2, webcam, cfg, 640, 480)
    with caplog.at_level(logging.INFO, logger=webcam.log.name):
        src._log_opened()
    assert "opened at 640x480" in caplog.text and "(" not in caplog.records[0].getMessage()
    warning = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warning and "1280x720 as YUY2" in warning[0] and "using 640x480" in warning[0]


# -- Settings tab -------------------------------------------------------------------------------------
def test_every_choice_says_what_it_does():
    for choices in (FPS_CHOICES, RESOLUTION_CHOICES, FORMAT_CHOICES):
        for _, text in choices:
            name, _, effect = text.partition(": ")
            assert name and len(effect) >= 10, text
    assert {v for v, _ in FORMAT_CHOICES} == set(CAMERA_FORMATS)
    assert (Config().source.width, Config().source.height) in [v for v, _ in RESOLUTION_CHOICES]
    assert Config().source.fps in [v for v, _ in FPS_CHOICES]


def test_choice_text_for_values_only_in_the_config_file():
    assert choice_text(FPS_CHOICES, 30) == "30 fps: recommended"
    assert choice_text(FPS_CHOICES, 25) == "25 fps: set in the config file"
    assert choice_text(RESOLUTION_CHOICES, (800, 600)) == "800×600: set in the config file"


@pytest.fixture
def app(tk_root, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    return App(tk_root, tmp_path / "config.toml", queue.Queue())


def _combobox_showing(root, text):
    def walk(w):
        for child in w.winfo_children():
            if isinstance(child, ttk.Combobox) and child.get() == text:
                return child
            found = walk(child)
            if found is not None:
                return found

    return walk(root)


def _label(choices_name, value):
    return gui.choice_text(getattr(gui, choices_name), value)


@pytest.mark.parametrize("choices, current, pick, saved", [
    ("RESOLUTION_CHOICES", (640, 480), (320, 240), {"width": 320, "height": 240}),
    ("FORMAT_CHOICES", "mjpg", "yuy2", {"format": "yuy2"}),
    ("FPS_CHOICES", 30, 20, {"fps": 20}),
])
def test_picking_a_camera_choice_saves_it(tk_root, app, choices, current, pick, saved):
    current, pick = _label(choices, current), _label(choices, pick)
    box = _combobox_showing(tk_root, current)
    assert box is not None, f"no dropdown showing {current!r}"
    assert pick in box.cget("values")
    box.set(pick)
    box.event_generate("<<ComboboxSelected>>")
    src = ConfigFile(app.config_path, "").base.source
    assert {k: getattr(src, k) for k in saved} == saved
    assert box.get() == pick


def test_a_refused_save_shows_the_setting_in_effect_again(tk_root, app, monkeypatch):
    monkeypatch.setattr(app, "save", lambda *a, **k: False)
    box = _combobox_showing(tk_root, _label("RESOLUTION_CHOICES", (640, 480)))
    box.set(_label("RESOLUTION_CHOICES", (1280, 720)))
    box.event_generate("<<ComboboxSelected>>")
    assert box.get() == _label("RESOLUTION_CHOICES", (640, 480))


def test_settings_tab_shows_config_file_values(tk_root, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    path = tmp_path / "config.toml"
    App(tk_root, path, queue.Queue())  # writes the default config
    for child in tk_root.winfo_children():
        child.destroy()
    set_values(path, "source", {"width": 800, "height": 600, "format": "auto"})
    App(tk_root, path, queue.Queue())
    assert _combobox_showing(tk_root, "800×600: set in the config file") is not None
    assert _combobox_showing(tk_root, "Automatic: let the camera driver choose") is not None
