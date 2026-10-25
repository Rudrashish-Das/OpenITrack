import os

import pytest

from openitrack.cli import main as cli_main
from openitrack.config import DEFAULT_CONFIG_TEXT, ConfigError, ConfigFile, parse_config
from openitrack.config_edit import set_value
from openitrack.engine import Engine
from openitrack.profiles import ProfileStore, app_key, check_name, parse_profile, profile_template


@pytest.fixture
def cfg_path(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
    return p


def write_profile(cfg_path, name, body):
    d = cfg_path.parent / "profiles"
    d.mkdir(exist_ok=True)
    path = d / f"{name}.toml"
    path.write_text(body, encoding="utf-8")
    return path


def touch_later(path):
    """Make sure the next poll sees a new mtime, however coarse the clock."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


# -- layering ----------------------------------------------------------------------

def test_profile_overrides_only_what_it_sets(cfg_path):
    write_profile(cfg_path, "DCS", '[profile]\napps = ["DCS.exe"]\n[axes.yaw]\noutput = 180.0\n'
                                   '[gaze]\nenabled = false\n')
    set_value(cfg_path, "axes.yaw", "exponent", 2.0)
    cf = ConfigFile(cfg_path, "dcs")      # names ignore case, like Windows
    assert cf.profile == "DCS"
    assert cf.config.axes.yaw.output == 180.0 and not cf.config.gaze.enabled
    assert cf.config.axes.yaw.exponent == 2.0          # from config.toml
    assert cf.base.axes.yaw.output == 160.0 and cf.base.gaze.enabled


def test_new_profile_template_changes_nothing(cfg_path):
    cf = ConfigFile(cfg_path)
    cf.profiles.create("Race sims", cf.base, apps=["acs.exe"])
    cf.use_profile("Race sims")
    assert cf.config == cf.base
    apps, _ = parse_profile(cf.profiles.path("Race sims").read_text(encoding="utf-8"))
    assert apps == ["acs.exe"]


def test_template_lists_current_default_values(cfg_path):
    set_value(cfg_path, "axes.pitch", "output", 70.0)
    text = profile_template("X", parse_config(cfg_path.read_text(encoding="utf-8")))
    pitch = text.split("[axes.pitch]")[1].split("[")[0]
    assert "# output = 70.0" in pitch
    assert "[source]" not in text and "[hotkeys]" not in text


def test_global_sections_cannot_be_overridden(cfg_path, caplog):
    write_profile(cfg_path, "X", '[source]\ntype = "phone"\n[hotkeys]\nrecenter = "F1"\n[filter]\nhead_beta = 0.2\n')
    cf = ConfigFile(cfg_path, "X")
    assert cf.config.source.type == "webcam" and cf.config.hotkeys.recenter == "ctrl+shift+F9"
    assert cf.config.filter.head_beta == 0.2
    assert "can't be changed per profile" in caplog.text


def test_active_profile_from_config_and_fallbacks(cfg_path, caplog):
    write_profile(cfg_path, "MSFS", "[axes.yaw]\noutput = 120.0\n")
    set_value(cfg_path, "profiles", "active", "MSFS")
    assert ConfigFile(cfg_path).profile == "MSFS"
    assert ConfigFile(cfg_path, "").profile == ""                 # explicit Default
    set_value(cfg_path, "profiles", "active", "Gone")
    assert ConfigFile(cfg_path).profile == ""                     # missing: Default, logged
    assert "No profile named 'Gone'" in caplog.text
    with pytest.raises(ConfigError):
        ConfigFile(cfg_path, "Gone")                              # asked for by name: error


def test_broken_profile_is_refused_and_current_kept(cfg_path):
    write_profile(cfg_path, "Bad", "[axes.yaw]\ninput_range = 0.1\n")   # below the deadzone
    write_profile(cfg_path, "Typo", "[axes.yaw\n")
    cf = ConfigFile(cfg_path, "")
    for name in ("Bad", "Typo"):
        with pytest.raises(ConfigError, match=f"Profile '{name}'"):
            cf.use_profile(name)
        assert cf.profile == ""


def test_runtime_overrides_survive_profile_switches(cfg_path):
    write_profile(cfg_path, "X", "[gaze]\nenabled = false\n")
    cf = ConfigFile(cfg_path)
    cf.override({"source": {"type": "phone"}})
    cf.use_profile("X")
    assert cf.config.source.type == "phone" and not cf.config.gaze.enabled
    cf.use_profile("")
    assert cf.config.source.type == "phone"


# -- live reload ---------------------------------------------------------------------

def test_poll_reloads_profile_edits_and_removal(cfg_path):
    path = write_profile(cfg_path, "X", "[axes.yaw]\noutput = 100.0\n")
    cf = ConfigFile(cfg_path, "X")
    assert not cf.poll()
    path.write_text("[axes.yaw]\noutput = 110.0\n", encoding="utf-8")
    touch_later(path)
    assert cf.poll() and cf.config.axes.yaw.output == 110.0
    path.write_text("[axes.yaw]\noutput = \n", encoding="utf-8")   # broken edit: keep previous
    touch_later(path)
    assert not cf.poll() and cf.config.axes.yaw.output == 110.0
    path.unlink()
    assert cf.poll() and cf.profile == "" and cf.config.axes.yaw.output == 160.0


def test_poll_follows_active_profile_in_config(cfg_path):
    write_profile(cfg_path, "X", "[axes.yaw]\noutput = 100.0\n")
    cf = ConfigFile(cfg_path)
    set_value(cfg_path, "profiles", "active", "X")
    touch_later(cfg_path)
    assert cf.poll() and cf.profile == "X" and cf.config.axes.yaw.output == 100.0
    set_value(cfg_path, "axes.yaw", "exponent", 2.5)        # base edits still apply under it
    touch_later(cfg_path)
    assert cf.poll() and cf.config.axes.yaw.exponent == 2.5 and cf.config.axes.yaw.output == 100.0


# -- store -------------------------------------------------------------------------------

def test_app_matching():
    assert app_key("C:\\Games\\DCS World\\bin\\DCS.exe") == app_key("dcs.EXE") == app_key("DCS") == "dcs"


def test_find_for_app(cfg_path):
    write_profile(cfg_path, "DCS", '[profile]\napps = ["DCS.exe", "DCS_server.exe"]\n')
    write_profile(cfg_path, "MSFS", '[profile]\napps = ["FlightSimulator.exe"]\n')
    store = ProfileStore(cfg_path.parent)
    assert store.find_for_app("dcs.exe") == "DCS"
    assert store.find_for_app("FlightSimulator.exe") == "MSFS"
    assert store.find_for_app("notepad.exe") is None


def test_names_are_checked():
    assert check_name("  IL-2 (VR)  ") == "IL-2 (VR)"
    for bad in ("", "Default", "a/b", "..", "con", "x" * 41, "trailing."):
        with pytest.raises(ConfigError):
            check_name(bad)


def test_create_duplicate_rename_delete(cfg_path):
    cf = ConfigFile(cfg_path)
    store = cf.profiles
    store.create("DCS", cf.base, apps=["DCS.exe"])
    with pytest.raises(ConfigError, match="already"):
        store.create("dcs", cf.base)
    store.set_values("DCS", "gaze", {"enabled": False}, check=cf.check_profile)
    store.create("DCS VR", cf.base, copy_of="DCS")
    copy = store.load("DCS VR")
    assert copy.apps == [] and copy.settings["gaze"]["enabled"] is False   # settings kept, game not
    assert store.rename("DCS VR", "DCS (VR)") == "DCS (VR)"
    assert store.names() == ["DCS", "DCS (VR)"]
    store.delete("DCS (VR)")
    assert store.names() == ["DCS"]


def test_set_values_fills_in_the_commented_setting(cfg_path):
    cf = ConfigFile(cfg_path)
    cf.profiles.create("X", cf.base)
    cf.profiles.set_values("X", "output", {"mouse": True}, check=cf.check_profile)
    cf.profiles.set_apps("X", ["a.exe", 'odd "name".exe'])
    text = cf.profiles.path("X").read_text(encoding="utf-8")
    out = text.split("[output]")[1]
    assert "mouse = true" in out and "# mouse = " not in out and "# opentrack = true" in out
    prof = cf.profiles.load("X")
    assert prof.apps == ["a.exe", 'odd "name".exe'] and prof.settings["output"] == {"mouse": True}


def test_set_values_refuses_settings_that_break_on_top_of_default(cfg_path):
    cf = ConfigFile(cfg_path)
    cf.profiles.create("X", cf.base)
    before = cf.profiles.path("X").read_text(encoding="utf-8")
    with pytest.raises(ConfigError, match="Not saved"):
        cf.profiles.set_values("X", "axes.yaw", {"input_range": 0.1}, check=cf.check_profile)
    assert cf.profiles.path("X").read_text(encoding="utf-8") == before


# -- engine and command line ----------------------------------------------------------------

def quiet_engine(cfg_path):
    engine = Engine(ConfigFile(cfg_path))
    engine.sounds.enabled = False
    return engine


def test_engine_switches_profiles_on_command(cfg_path):
    write_profile(cfg_path, "A", "[axes.yaw]\noutput = 100.0\n")
    write_profile(cfg_path, "B", "[axes.yaw]\noutput = 120.0\n")
    engine = quiet_engine(cfg_path)
    engine._handle_action("use_profile:b", 0.0)
    assert engine.cf.profile == "B" and engine.pipeline.config.axes.yaw.output == 120.0
    engine._handle_action("use_profile:missing", 0.0)          # refused, logged
    assert engine.cf.profile == "B"
    seen = []
    for _ in range(3):
        engine._handle_action("next_profile", 0.0)
        seen.append(engine.cf.profile)
    assert seen == ["", "A", "B"]
    assert engine.cfg.axes.yaw.output == 120.0


def test_engine_follows_the_program_in_front(cfg_path, monkeypatch):
    write_profile(cfg_path, "DCS", '[profile]\napps = ["DCS.exe"]\n[axes.yaw]\noutput = 100.0\n')
    engine = quiet_engine(cfg_path)
    front = ["DCS.exe"]
    monkeypatch.setattr("openitrack.engine.foreground_app", lambda: front[0])
    engine._follow_front_app()
    assert engine.cf.profile == "DCS"
    front[0] = "explorer.exe"                                  # no profile: keep the game's
    engine._follow_front_app()
    assert engine.cf.profile == "DCS"
    engine._handle_action("use_profile:", 0.0)                 # picked by hand while DCS is behind
    front[0] = "explorer.exe"
    engine._follow_front_app()
    assert engine.cf.profile == ""


def test_cli_profiles(cfg_path, capsys):
    assert cli_main(["-c", str(cfg_path), "profiles", "--new", "DCS", "--app", "DCS.exe"]) == 0
    assert cli_main(["-c", str(cfg_path), "profiles", "--new", "dcs"]) == 2
    capsys.readouterr()
    assert cli_main(["-c", str(cfg_path), "profiles"]) == 0
    out = capsys.readouterr().out
    assert "* Default" in out and "DCS" in out and "DCS.exe" in out
