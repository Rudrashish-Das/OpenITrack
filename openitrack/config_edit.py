"""Change single settings in config.toml without losing the user's comments or
layout (the window saves settings this way; the running tracker hot-reloads)."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

from .config import ConfigError, parse_config

_HEADER = re.compile(r"^\s*\[([^\[\]]+)\]\s*(#.*)?$")
_KEY = re.compile(r"^(\s*)([A-Za-z0-9_-]+)(\s*=\s*)(.*)$")
_COMMENTED_KEY = re.compile(r"^(\s*)#\s?([A-Za-z0-9_-]+)(\s*=\s*)(.*)$")


def toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    raise TypeError(f"unsupported config value {value!r}")


def _split_comment(rest: str) -> tuple[str, str]:
    """Split 'value   # comment' outside of quotes."""
    in_str = False
    esc = False
    for i, ch in enumerate(rest):
        if esc:
            esc = False
        elif ch == "\\" and in_str:
            esc = True
        elif ch == '"':
            in_str = not in_str
        elif ch == "#" and not in_str:
            return rest[:i], rest[i:]
    return rest, ""


def set_values(path: Path, section: str, values: dict[str, object], *,
               validate: Callable[[str], object] = parse_config, uncomment: bool = False) -> None:
    """Set keys in [section], keeping comments and alignment. Validates the result
    before writing, so a bad value never reaches the file.

    With `uncomment`, a commented-out `# key = value` line in the section is
    replaced when the key isn't set yet (profile files list settings that way)."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    remaining = dict(values)

    start = end = None
    for i, line in enumerate(lines):
        m = _HEADER.match(line)
        if m:
            if start is not None:
                end = i
                break
            if m.group(1).strip() == section:
                start = i
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{section}]")
        start, end = len(lines) - 1, len(lines)
    elif end is None:
        end = len(lines)

    for pattern in (_KEY, _COMMENTED_KEY) if uncomment else (_KEY,):
        for i in range(start + 1, end):
            m = pattern.match(lines[i])
            if not m or m.group(2) not in remaining:
                continue
            indent, key, eq, rest = m.groups()
            old_value, comment = _split_comment(rest)
            new_value = toml_value(remaining.pop(key))
            pad = " " * max(1, len(old_value) - len(new_value)) if comment else ""
            lines[i] = f"{indent}{key}{eq}{new_value}{pad}{comment}".rstrip()

    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    for key, value in remaining.items():
        lines.insert(insert_at, f"{key} = {toml_value(value)}")
        insert_at += 1

    new_text = "\n".join(lines) + "\n"
    try:
        validate(new_text)
    except ConfigError as exc:
        raise ConfigError(f"Not saved: {exc}") from exc
    tmp = path.with_suffix(".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)


def set_value(path: Path, section: str, key: str, value: object) -> None:
    set_values(path, section, {key: value})
