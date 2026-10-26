"""Per-axis sensitivity: [axes.*] sensitivity in the config and its sliders in the window."""

import queue

import pytest

from openitrack.config import AXIS_SENSITIVITY_RANGE, Config, ConfigError, ConfigFile, parse_config
from openitrack.gui import AXIS_SENS_ROWS, App


def test_sensitivity_defaults_to_one_and_is_range_checked():
    assert all(getattr(Config().axes, a).sensitivity == 1.0 for a in ("yaw", "pitch", "roll", "x", "y", "z"))
    assert parse_config("[axes.yaw]\nsensitivity = 2.5\n").axes.yaw.sensitivity == 2.5
    lo, hi = AXIS_SENSITIVITY_RANGE
    for bad in (0, lo / 2, hi + 1):
        with pytest.raises(ConfigError):
            parse_config(f"[axes.pitch]\nsensitivity = {bad}\n")


@pytest.fixture
def app(tk_root, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    return App(tk_root, tmp_path / "config.toml", queue.Queue())


def test_sliders_save_to_the_config_and_reset(app):
    app.open_axis_sensitivity()
    var, label = app.axis_sens["Yaw"]
    assert var.get() == 1.0 and label.cget("text") == "×1.00"
    var.set(2.33)
    app.save_axis_sensitivity("Yaw")
    app.axis_sens["Leaning"][0].set(0.5)
    app.save_axis_sensitivity("Leaning")
    axes = ConfigFile(app.config_path, "").base.axes
    assert axes.yaw.sensitivity == 2.35  # steps of 0.05
    assert (axes.x.sensitivity, axes.y.sensitivity, axes.z.sensitivity) == (0.5, 0.5, 0.5)
    assert axes.pitch.sensitivity == 1.0
    app.reset_axis_sensitivity()
    axes = ConfigFile(app.config_path, "").base.axes
    assert {getattr(axes, a).sensitivity for rows in AXIS_SENS_ROWS.values() for a in rows} == {1.0}


def test_sliders_follow_the_profile_in_use(app):
    app.cf.profiles.create("Fast", app.cf.base)
    app.cf.profiles.set_values("Fast", "axes.pitch", {"sensitivity": 3.0}, check=app.cf.check_profile)
    app.open_axis_sensitivity()
    app._show_profile("Fast")
    assert app.axis_sens["Pitch"][0].get() == 3.0
    app.axis_sens["Roll"][0].set(0.2)
    app.save_axis_sensitivity("Roll")
    assert ConfigFile(app.config_path, "Fast").config.axes.roll.sensitivity == 0.2
    assert ConfigFile(app.config_path, "").config.axes.roll.sensitivity == 1.0  # Default untouched
