"""Per-game profiles: TOML files in <config dir>/profiles/ that change part of
config.toml for one game or app. A profile is picked by hand, or automatically
when a program listed in its `apps` is the window in front."""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from .config import Config, ConfigError, load_toml
from .config_edit import set_values, toml_value

log = logging.getLogger(__name__)

PROFILE_DIR = "profiles"
# Sections a profile can change. The camera, calibration, shortcuts and window
# settings belong to the PC and the person, not the game, so they stay global.
PROFILE_SECTIONS = ("tracking", "filter", "gaze", "axes", "output")

_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .()+-]{0,39}$")
_RESERVED = {"con", "prn", "aux", "nul", *(f"{d}{i}" for d in ("com", "lpt") for i in range(1, 10))}


def check_name(name: str) -> str:
    name = name.strip()
    if not _NAME.match(name) or name.endswith(".") or name.split(".")[0].lower() in _RESERVED:
        raise ConfigError("A profile name can have letters, digits, spaces and . _ - ( ) +, "
                          "up to 40 characters")
    if name.lower() == "default":
        raise ConfigError("\"Default\" is taken: it means config.toml on its own")
    return name


def app_key(app: str) -> str:
    """'C:\\Games\\DCS.exe', 'dcs.exe' and 'DCS' all name the same program."""
    return re.split(r"[\\/]", app.strip())[-1].lower().removesuffix(".exe")


@dataclass
class Profile:
    name: str
    path: Path
    mtime: float
    apps: list[str]
    settings: dict[str, Any]   # what the profile changes, laid out like config.toml


def parse_profile(text: str, name: str = "") -> tuple[list[str], dict[str, Any]]:
    """Profile file text -> (apps, settings)."""
    data = load_toml(text)
    meta = data.pop("profile", {})
    if not isinstance(meta, dict):
        raise ConfigError("[profile] must be a table")
    apps = meta.get("apps", [])
    if not isinstance(apps, list) or not all(isinstance(a, str) for a in apps):
        raise ConfigError("'profile.apps' must be a list of program names, e.g. [\"DCS.exe\"]")
    for key in meta:
        if key != "apps":
            log.warning("Unknown key 'profile.%s' in profile '%s' ignored", key, name)
    settings = {}
    for key, value in data.items():
        if key in PROFILE_SECTIONS:
            settings[key] = value
        else:
            log.warning("Profile '%s': [%s] can't be changed per profile (only %s); ignored",
                        name, key, ", ".join(PROFILE_SECTIONS))
    return [a.strip() for a in apps if a.strip()], settings


def profile_template(name: str, base: Config, apps: Iterable[str] = ()) -> str:
    """A new profile: every setting it can change, commented out, with the
    current Default value."""
    lines = [
        f"# OpenITrack profile \"{name}\".",
        "# While this profile is in use, settings in this file replace the ones in config.toml.",
        "# They all start commented out, so the profile works exactly like Default. To change",
        "# one for this game, remove the # in front of it (config.toml explains each setting).",
        "# Saved changes apply within a second, even while tracking.",
        "",
        "[profile]",
        f"apps = {toml_value(list(apps))}    # programs that switch to this profile when they're in "
        "front, e.g. [\"DCS.exe\"]",
    ]

    def section(header: str, obj: Any) -> None:
        lines.extend(["", f"[{header}]"])
        lines.extend(f"# {f.name} = {toml_value(getattr(obj, f.name))}" for f in fields(obj))

    for name_ in PROFILE_SECTIONS:
        obj = getattr(base, name_)
        if name_ == "axes":
            for f in fields(obj):
                section(f"axes.{f.name}", getattr(obj, f.name))
        else:
            section(name_, obj)
    return "\n".join(lines) + "\n"


class ProfileStore:
    def __init__(self, config_dir: Path):
        self.dir = config_dir / PROFILE_DIR

    def path(self, name: str) -> Path:
        return self.dir / f"{name}.toml"

    def names(self) -> list[str]:
        try:
            return sorted((p.stem for p in self.dir.glob("*.toml")), key=str.lower)
        except OSError:
            return []

    def resolve(self, name: str) -> str | None:
        """The saved name matching `name`, ignoring case (as Windows does)."""
        low = name.strip().lower()
        return next((n for n in self.names() if n.lower() == low), None)

    def load(self, name: str) -> Profile:
        real = self.resolve(name)
        if real is None:
            raise ConfigError(f"No profile named '{name}' (profiles: {', '.join(self.names()) or 'none yet'})")
        path = self.path(real)
        try:
            mtime = path.stat().st_mtime
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"Profile '{real}' can't be read: {exc}") from exc
        try:
            apps, settings = parse_profile(text, real)
        except ConfigError as exc:
            raise ConfigError(f"Profile '{real}': {exc}") from exc
        return Profile(real, path, mtime, apps, settings)

    def list(self) -> list[Profile]:
        """Every profile that loads; broken ones are logged and skipped."""
        out = []
        for name in self.names():
            try:
                out.append(self.load(name))
            except ConfigError as exc:
                log.error("%s", exc)
        return out

    def find_for_app(self, app: str) -> str | None:
        """The profile listing this program, if any."""
        key = app_key(app)
        return next((p.name for p in self.list() if any(app_key(a) == key for a in p.apps)), None)

    def create(self, name: str, base: Config, apps: Iterable[str] = (), copy_of: str | None = None) -> str:
        """New profile from the template, or a copy of another one (without its
        apps, so two profiles don't claim the same game). Returns its name."""
        name = check_name(name)
        if self.resolve(name):
            raise ConfigError(f"There's already a profile named '{name}'")
        self.dir.mkdir(parents=True, exist_ok=True)
        if copy_of:
            shutil.copyfile(self.load(copy_of).path, self.path(name))
            self.set_apps(name, list(apps))
        else:
            self.path(name).write_text(profile_template(name, base, apps), encoding="utf-8")
        return name

    def rename(self, old: str, new: str) -> str:
        real = self.load(old).name
        new = check_name(new)
        other = self.resolve(new)
        if other and other.lower() != real.lower():
            raise ConfigError(f"There's already a profile named '{other}'")
        os.replace(self.path(real), self.path(new))
        return new

    def delete(self, name: str) -> None:
        self.load(name).path.unlink()

    def set_values(self, name: str, section: str, values: dict[str, object],
                   check: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        """Change settings in a profile file, keeping its comments. `check` gets
        the new settings and raises ConfigError to refuse them (ConfigFile.check_profile)."""
        prof = self.load(name)

        def validate(text: str) -> None:
            _, settings = parse_profile(text, prof.name)
            if check is not None:
                check(prof.name, settings)

        set_values(prof.path, section, values, validate=validate, uncomment=True)

    def set_apps(self, name: str, apps: list[str]) -> None:
        self.set_values(name, "profile", {"apps": apps})
