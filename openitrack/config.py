"""Configuration: a commented TOML file that is reloaded live while running."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_CONFIG_TEXT = """\
# OpenITrack configuration.
# This file is re-read automatically while OpenITrack runs, so you can tune
# curves and inversions live: save the file and the change applies within ~1s.

[source]
type = "webcam"        # "webcam" (PC does the tracking) or "phone" (phone does the tracking)
camera = 0             # webcam index (see `openitrack cameras`), or an IP-camera URL,
                       # e.g. "http://192.168.1.20:4747/video" for DroidCam
width = 640            # camera resolution. Lower (e.g. 320x240) saves some CPU; the face
height = 480           # model works on small images, but tracking gets a little less precise
fps = 30               # max tracking rate. 30 is plenty; lower it to save CPU
format = "mjpg"        # what the webcam sends: "mjpg" (compressed, works at any resolution),
                       # "yuy2" (uncompressed: no decoding, but USB bandwidth limits it to
                       # about 640x480 at 30 fps) or "auto" (the camera driver's choice)
phone_port = 443       # HTTPS port for phone mode. 443 gives the cleanest link
                       # (https://openitrack.local); falls back to 8443 if 443 is taken
phone_hostname = "openitrack"   # advertised on the network as <name>.local (mDNS)

[tracking]
min_confidence = 0.5
lost_hold_seconds = 0.4     # keep the last pose this long when the face is lost
lost_return_seconds = 1.0   # then glide back to center over this long
auto_center = true          # recenter automatically when a face is first found
blendshapes = true          # webcam mode: detect blinks with MediaPipe's expression model.
                            # false saves CPU; blinks are then judged from the eyelid gap

[filter]
# One Euro filter: min_cutoff lower = steadier at rest (more lag),
# beta higher = less lag during fast movement.
head_min_cutoff = 0.7
head_beta = 0.06
position_min_cutoff = 0.5
position_beta = 0.05
gaze_min_cutoff = 0.5
gaze_beta = 0.2

[gaze]
# Eye-assisted view: when you look toward the edge of the screen the view turns
# a bit further. Inside the deadzone, eye movement does nothing, so you can glance
# at instruments without the camera moving.
enabled = true
deadzone = 0.35        # fraction of eye travel ignored around center (0..1)
yaw_degrees = 30       # extra in-game yaw when looking fully left/right
pitch_degrees = 15     # extra in-game pitch when looking fully up/down
exponent = 1.5
horizontal_range = 0.30   # iris offset that counts as "fully left/right" (see preview)
vertical_range = 0.12     # iris offset that counts as "fully up/down"
blink_threshold = 0.45    # eyes closed above this: gaze is held, not updated

# Per-axis response curves.
#   input_range: real head movement (degrees / cm) that gives full output
#   output:      in-game movement (degrees / cm) at full input
#   deadzone:    movement ignored around center (same units as input)
#   exponent:    1 = linear, >1 = precise near center, fast at the edges
#   invert:      flip the direction if an axis moves the wrong way in game
[axes.yaw]
enabled = true
input_range = 35.0
output = 160.0
deadzone = 0.5
exponent = 1.6
invert = false

[axes.pitch]
enabled = true
input_range = 25.0
output = 90.0
deadzone = 0.5
exponent = 1.6
invert = false

[axes.roll]
enabled = true
input_range = 30.0
output = 30.0
deadzone = 1.0
exponent = 1.0
invert = false

[axes.x]
enabled = true
input_range = 15.0
output = 25.0
deadzone = 0.3
exponent = 1.0
invert = false

[axes.y]
enabled = true
input_range = 10.0
output = 15.0
deadzone = 0.3
exponent = 1.0
invert = false

[axes.z]
enabled = true
input_range = 15.0
output = 25.0
deadzone = 0.5
exponent = 1.0
invert = false

[output]
# How TrackIR / FreeTrack games (MSFS, DCS, X-Plane, IL-2, iRacing...) get the pose:
#   "direct"    built in: games read it straight from OpenITrack (no other app needed)
#   "opentrack" send it to opentrack's "UDP over network" input instead
#   "off"       neither
game = "direct"
trackir_process = true       # with "direct": keep a TrackIR.exe running (a few games look for it)
opentrack_host = "127.0.0.1"
opentrack_port = 4242
mouse = false                # move the mouse instead (games with mouse-look only)
mouse_pixels_per_degree = 12.0

[calibration]
# Results of `openitrack calibrate` (or the calibrate hotkey) are stored next to
# this file in calibration.json. When present they replace gaze.horizontal_range /
# vertical_range and the yaw/pitch input_range values above.
use_gaze = true
use_head = true
# Where the calibration overlay is shown: the monitor you play on, so the eye
# targets match where you actually look.
#   0 = primary monitor, 1, 2, 3 = monitors counted from the left
#   (`openitrack monitors` lists them), "all" = span every monitor (triple screens)
monitor = 0

[overlay]
# Gaze dot: a ring on screen where OpenITrack thinks you're looking. It's a rough
# guide (a webcam sees each eye in only a few pixels), drawn on top of everything
# and click-through, on the [calibration] monitor. Calibrate first for best results.
# Windows only, and not visible over exclusive-fullscreen games.
enabled = false
follow_head = true        # add head turns to eye movement (false = eyes only)
screen_degrees = 50.0     # how wide the screen (or all screens, with monitor = "all")
                          # looks from your seat: about 50 for a 27" screen at 60 cm
size = 40                 # ring diameter in pixels on a 1080p screen

[hotkeys]
# Shortcuts work while any app or game has focus. Set a binding to "" to disable it.
#
#   Keyboard:  "F9", "ctrl+F9", "ctrl+shift+R", "numpad5", "pause", "scrolllock"
#              Modifiers: ctrl, alt, shift, win.
#   Mouse:     "mouse3" (middle), "mouse4", "mouse5" (side buttons), e.g. "ctrl+mouse4"
#   Joystick:  "joy1:button5" (HOTAS / wheel / button box). Run `openitrack buttons`
#              and press a button to see its exact name.
#
# Defaults are all multi-key combos (Ctrl+Shift+..., and Ctrl+Alt+Shift+... for
# rare actions) so a single keypress meant for the sim never triggers OpenITrack.
# Keyboard shortcuts for the press actions are captured: the game won't see them.
# Mouse/joystick buttons and the *_hold actions are not captured, so the game
# still gets them too. (F12 can't be used: Windows reserves it.)
recenter = "ctrl+shift+F9"
pause = "ctrl+shift+F10"                  # view glides to center and stays there
toggle_gaze = "ctrl+shift+F11"            # eye-assist on/off
toggle_translation = "ctrl+shift+F8"      # x/y/z (leaning) on/off
toggle_roll = ""                          # head tilt on/off
sensitivity_up = "ctrl+shift+pageup"      # rotation x1.1 (this session only)
sensitivity_down = "ctrl+shift+pagedown"
calibrate = "ctrl+alt+shift+F9"           # start/cancel the calibration overlay
toggle_preview = "ctrl+alt+shift+F10"     # show/hide the tuning window
toggle_overlay = "ctrl+alt+shift+F11"     # gaze dot on/off (this session only)
freeze_hold = ""                          # hold: view stays where it is
center_hold = ""                          # hold: view snaps to straight ahead
next_profile = ""                         # switch to the next profile (see [profiles])
sounds = true                             # short beeps to confirm shortcuts

[profiles]
# Profiles give a game or app its own curves, eye-assist, filtering and outputs.
# Each one is a file in the "profiles" folder next to this file. It only holds the
# settings it changes; everything else comes from this file ("Default").
# Make them in the window's Profiles tab, or with `openitrack profiles --new NAME`.
active = ""            # profile to use ("" = Default, just this file)
auto_switch = true     # switch when a program listed in a profile's `apps` is in front

[ui]
autostart = false      # start tracking as soon as the OpenITrack window opens
setup_done = false     # false = show the step-by-step setup when the window opens
"""


# Webcam pixel formats for [source] format, as the FOURCC to ask the camera for
# (None: don't ask, the driver picks).
CAMERA_FORMATS = {"mjpg": "MJPG", "yuy2": "YUY2", "auto": None}
# [output] game: how TrackIR / FreeTrack games get the pose.
GAME_OUTPUTS = ("direct", "opentrack", "off")


@dataclass
class SourceConfig:
    type: str = "webcam"
    camera: int | str = 0
    width: int = 640
    height: int = 480
    fps: int = 30
    format: str = "mjpg"
    phone_port: int = 443
    phone_hostname: str = "openitrack"


@dataclass
class TrackingConfig:
    min_confidence: float = 0.5
    lost_hold_seconds: float = 0.4
    lost_return_seconds: float = 1.0
    auto_center: bool = True
    blendshapes: bool = True


@dataclass
class FilterConfig:
    head_min_cutoff: float = 0.7
    head_beta: float = 0.06
    position_min_cutoff: float = 0.5
    position_beta: float = 0.05
    gaze_min_cutoff: float = 0.5
    gaze_beta: float = 0.2


@dataclass
class GazeConfig:
    enabled: bool = True
    deadzone: float = 0.35
    yaw_degrees: float = 30.0
    pitch_degrees: float = 15.0
    exponent: float = 1.5
    horizontal_range: float = 0.30
    vertical_range: float = 0.12
    blink_threshold: float = 0.45


@dataclass
class AxisConfig:
    enabled: bool = True
    input_range: float = 30.0
    output: float = 90.0
    deadzone: float = 0.5
    exponent: float = 1.0
    invert: bool = False


def _axis(**kw: Any) -> Any:
    return field(default_factory=lambda: AxisConfig(**kw))


@dataclass
class AxesConfig:
    yaw: AxisConfig = _axis(input_range=35.0, output=160.0, deadzone=0.5, exponent=1.6)
    pitch: AxisConfig = _axis(input_range=25.0, output=90.0, deadzone=0.5, exponent=1.6)
    roll: AxisConfig = _axis(input_range=30.0, output=30.0, deadzone=1.0, exponent=1.0)
    x: AxisConfig = _axis(input_range=15.0, output=25.0, deadzone=0.3, exponent=1.0)
    y: AxisConfig = _axis(input_range=10.0, output=15.0, deadzone=0.3, exponent=1.0)
    z: AxisConfig = _axis(input_range=15.0, output=25.0, deadzone=0.5, exponent=1.0)


@dataclass
class OutputConfig:
    game: str = "direct"
    trackir_process: bool = True
    opentrack_host: str = "127.0.0.1"
    opentrack_port: int = 4242
    mouse: bool = False
    mouse_pixels_per_degree: float = 12.0


@dataclass
class CalibrationConfig:
    use_gaze: bool = True
    use_head: bool = True
    monitor: int | str = 0


@dataclass
class OverlayConfig:
    enabled: bool = False
    follow_head: bool = True
    screen_degrees: float = 50.0
    size: int = 40


@dataclass
class HotkeyConfig:
    recenter: str = "ctrl+shift+F9"
    pause: str = "ctrl+shift+F10"
    toggle_gaze: str = "ctrl+shift+F11"
    calibrate: str = "ctrl+alt+shift+F9"
    toggle_preview: str = "ctrl+alt+shift+F10"
    toggle_overlay: str = "ctrl+alt+shift+F11"
    toggle_translation: str = "ctrl+shift+F8"
    toggle_roll: str = ""
    sensitivity_up: str = "ctrl+shift+pageup"
    sensitivity_down: str = "ctrl+shift+pagedown"
    freeze_hold: str = ""
    center_hold: str = ""
    next_profile: str = ""
    sounds: bool = True

    def bindings(self) -> dict[str, str]:
        """Action name -> binding spec (everything except the sounds flag)."""
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "sounds"}


@dataclass
class ProfilesConfig:
    active: str = ""
    auto_switch: bool = True


@dataclass
class UiConfig:
    autostart: bool = False
    setup_done: bool = False


@dataclass
class Config:
    source: SourceConfig = field(default_factory=SourceConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    filter: FilterConfig = field(default_factory=FilterConfig)
    gaze: GazeConfig = field(default_factory=GazeConfig)
    axes: AxesConfig = field(default_factory=AxesConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)
    profiles: ProfilesConfig = field(default_factory=ProfilesConfig)
    ui: UiConfig = field(default_factory=UiConfig)


class ConfigError(ValueError):
    pass


def _merge(obj: Any, data: dict[str, Any], path: str) -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        where = f"{path}.{key}" if path else key
        if key not in known:
            log.warning("Unknown config key '%s' ignored", where)
            continue
        current = getattr(obj, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ConfigError(f"'{where}' must be a table")
            _merge(current, value, where)
            continue
        setattr(obj, key, _coerce(current, value, where))


def _coerce(current: Any, value: Any, where: str) -> Any:
    if where == "source.camera":
        if isinstance(value, (int, str)) and not isinstance(value, bool):
            return value
        raise ConfigError(f"'{where}' must be a number or a URL string")
    if where == "calibration.monitor":
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        if isinstance(value, str) and value.lower() == "all":
            return "all"
        raise ConfigError(f"'{where}' must be 0 (primary), a monitor number or \"all\"")
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        raise ConfigError(f"'{where}' must be true or false")
    if isinstance(current, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"'{where}' must be a number")
        return type(current)(value)
    if isinstance(current, str):
        if not isinstance(value, str):
            raise ConfigError(f"'{where}' must be a string")
        return value
    return value


def _validate(cfg: Config) -> None:
    if cfg.source.type not in ("webcam", "phone"):
        raise ConfigError("source.type must be \"webcam\" or \"phone\"")
    if not 1 <= cfg.source.fps <= 120:
        raise ConfigError("source.fps must be between 1 and 120")
    if not (160 <= cfg.source.width <= 4096 and 120 <= cfg.source.height <= 4096):
        raise ConfigError("source.width/height must be between 160x120 and 4096x4096")
    if cfg.source.format not in CAMERA_FORMATS:
        raise ConfigError("source.format must be " + ", ".join(f'"{f}"' for f in CAMERA_FORMATS))
    for f in fields(cfg.axes):
        ax: AxisConfig = getattr(cfg.axes, f.name)
        if ax.input_range <= ax.deadzone or ax.input_range <= 0:
            raise ConfigError(f"axes.{f.name}.input_range must be larger than its deadzone")
        if ax.exponent <= 0:
            raise ConfigError(f"axes.{f.name}.exponent must be positive")
    if not 0 <= cfg.gaze.deadzone < 1:
        raise ConfigError("gaze.deadzone must be in [0, 1)")
    if cfg.gaze.horizontal_range <= 0 or cfg.gaze.vertical_range <= 0:
        raise ConfigError("gaze ranges must be positive")
    if not 10 <= cfg.overlay.screen_degrees <= 170:
        raise ConfigError("overlay.screen_degrees must be between 10 and 170")
    if cfg.output.game not in GAME_OUTPUTS:
        raise ConfigError("output.game must be " + ", ".join(f'"{g}"' for g in GAME_OUTPUTS))
    if not 8 <= cfg.overlay.size <= 400:
        raise ConfigError("overlay.size must be between 8 and 400 pixels")


def load_toml(text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML: {exc}") from exc


def _migrate(data: dict[str, Any]) -> dict[str, Any]:
    """Older files have `opentrack = true/false` in [output] instead of `game`."""
    out = data.get("output")
    if not isinstance(out, dict) or "opentrack" not in out:
        return data
    out = dict(out)
    legacy = out.pop("opentrack")
    if "game" not in out and isinstance(legacy, bool):
        out["game"] = "opentrack" if legacy else "off"
    return {**data, "output": out}


def config_from_data(data: dict[str, Any]) -> Config:
    cfg = Config()
    _merge(cfg, _migrate(data), "")
    _validate(cfg)
    return cfg


def parse_config(text: str) -> Config:
    return config_from_data(load_toml(text))


def deep_merge(base: dict[str, Any], *layers: dict[str, Any]) -> dict[str, Any]:
    """Copy of `base` with each layer's tables merged in and its values replacing."""
    out = dict(base)
    for layer in layers:
        for key, value in layer.items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = deep_merge(out[key], value)
            else:
                out[key] = value
    return out


def default_config_path() -> Path:
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / ".config"
    return root / "OpenITrack" / "config.toml"


class ConfigFile:
    """Loads config.toml (creating it from defaults if missing) and the profile in
    use, and hot-reloads both.

    `config` is the effective config: config.toml, then the profile's settings,
    then `override()` values (command line options) on top. `base` is the same
    without the profile."""

    def __init__(self, path: Path, profile: str | None = None):
        """`profile`: name to use, "" for none, or None for the one config.toml
        selects (falling back to none, with an error logged, if it is broken)."""
        from .profiles import ProfileStore  # profiles.py imports this module

        self.path = path
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
            log.info("Created default config at %s", path)
        self.profiles = ProfileStore(path.parent)
        self._mtime = path.stat().st_mtime
        self._data = load_toml(path.read_text(encoding="utf-8"))
        self._runtime: dict[str, Any] = {}
        self.base = config_from_data(self._data)
        self.config = self.base
        self.profile = ""
        self._profile_data: dict[str, Any] = {}
        self._profile_mtime: float | None = None
        if profile is None:
            try:
                self.use_profile(self.base.profiles.active)
            except ConfigError as exc:
                log.error("%s. Using Default.", exc)
        else:
            self.use_profile(profile)

    def _build(self, profile_data: dict[str, Any], name: str) -> Config:
        if not profile_data:
            return config_from_data(deep_merge(self._data, self._runtime))
        try:
            return config_from_data(deep_merge(self._data, profile_data, self._runtime))
        except ConfigError as exc:
            raise ConfigError(f"Profile '{name}': {exc}") from exc

    def check_profile(self, name: str, profile_data: dict[str, Any]) -> None:
        """Raise ConfigError if these profile settings don't work on top of config.toml."""
        self._build(profile_data, name)

    def override(self, data: dict[str, Any]) -> None:
        """Settings for this run only (e.g. `--phone`). They survive reloads and
        profile switches."""
        self._runtime = deep_merge(self._runtime, data)
        self.base = config_from_data(deep_merge(self._data, self._runtime))
        self.config = self._build(self._profile_data, self.profile)

    def use_profile(self, name: str) -> None:
        """Switch to a profile ("" = Default). A missing or broken profile raises
        ConfigError and the current one stays in use."""
        if not name:
            self.config = self._build({}, "")
            self.profile, self._profile_data, self._profile_mtime = "", {}, None
            return
        prof = self.profiles.load(name)
        self.config = self._build(prof.settings, prof.name)
        self.profile, self._profile_data = prof.name, prof.settings
        self._profile_mtime = prof.mtime

    def poll(self) -> bool:
        """Reload whatever changed: config.toml, the profile file, or which profile
        config.toml selects. Returns True when a new config was applied.

        A broken edit is reported and the previous config stays in effect.
        """
        base = self._poll_base()
        if base == "switched":
            return True
        profile = self._poll_profile()
        if not (base or profile):
            return False
        try:
            self.config = self._build(self._profile_data, self.profile)
        except ConfigError as exc:
            log.error("Config not reloaded, keeping previous settings: %s", exc)
            return False
        log.info("Config reloaded")
        return True

    def _poll_base(self) -> bool | str:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        if mtime == self._mtime:
            return False
        self._mtime = mtime
        try:
            data = load_toml(self.path.read_text(encoding="utf-8"))
            base = config_from_data(deep_merge(data, self._runtime))
        except (OSError, ConfigError) as exc:
            log.error("Config not reloaded, keeping previous settings: %s", exc)
            return False
        old_active = self.base.profiles.active
        self._data, self.base = data, base
        active = base.profiles.active
        if active != old_active and active.lower() != self.profile.lower():
            try:
                self.use_profile(active)
            except ConfigError as exc:
                log.error("%s. Keeping profile %s.", exc, self.profile or "Default")
                return True
            log.info("Config reloaded, profile: %s", self.profile or "Default")
            return "switched"
        return True

    def _poll_profile(self) -> bool:
        if not self.profile:
            return False
        try:
            mtime = self.profiles.path(self.profile).stat().st_mtime
        except OSError:
            log.warning("Profile '%s' was removed, using Default", self.profile)
            self.profile, self._profile_data, self._profile_mtime = "", {}, None
            return True
        if mtime == self._profile_mtime:
            return False
        self._profile_mtime = mtime
        try:
            self._profile_data = self.profiles.load(self.profile).settings
        except ConfigError as exc:
            log.error("Profile not reloaded, keeping previous settings: %s", exc)
            return False
        return True
