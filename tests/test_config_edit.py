import pytest

from openitrack.config import DEFAULT_CONFIG_TEXT, ConfigError, parse_config
from openitrack.config_edit import set_value, set_values


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
    return p


def test_changes_value_and_keeps_comments(cfg):
    set_value(cfg, "hotkeys", "recenter", "ctrl+alt+R")
    text = cfg.read_text(encoding="utf-8")
    assert parse_config(text).hotkeys.recenter == "ctrl+alt+R"
    assert "# Shortcuts work while any app or game has focus." in text
    line = next(l for l in text.splitlines() if l.startswith("pause ="))
    assert "# view glides to center" in line  # neighbours untouched


def test_same_key_in_different_sections(cfg):
    set_value(cfg, "axes.pitch", "invert", True)
    c = parse_config(cfg.read_text(encoding="utf-8"))
    assert c.axes.pitch.invert and not c.axes.yaw.invert


def test_types_and_escaping(cfg):
    set_values(cfg, "source", {"type": "phone", "camera": 'http://x/"v"', "fps": 20})
    set_value(cfg, "output", "mouse_pixels_per_degree", 7.5)
    c = parse_config(cfg.read_text(encoding="utf-8"))
    assert (c.source.type, c.source.camera, c.source.fps) == ("phone", 'http://x/"v"', 20)
    assert c.output.mouse_pixels_per_degree == 7.5


def test_missing_key_and_section_are_added(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[source]\ntype = \"webcam\"\n", encoding="utf-8")
    set_value(p, "source", "fps", 25)
    set_value(p, "ui", "autostart", True)
    c = parse_config(p.read_text(encoding="utf-8"))
    assert c.source.fps == 25 and c.ui.autostart


def test_invalid_value_is_not_written(cfg):
    before = cfg.read_text(encoding="utf-8")
    with pytest.raises(ConfigError):
        set_value(cfg, "source", "type", "kinect")
    assert cfg.read_text(encoding="utf-8") == before


def test_hash_inside_string_is_not_a_comment(cfg):
    set_value(cfg, "source", "camera", "http://cam/#1")
    set_value(cfg, "source", "camera", "http://cam/#2")
    assert parse_config(cfg.read_text(encoding="utf-8")).source.camera == "http://cam/#2"
