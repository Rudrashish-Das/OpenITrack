"""Delete everything OpenITrack has saved, so the user can start over."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Downloaded, non-personal files: kept unless the user asks, since they would only
# be fetched again (~30 MB).
DOWNLOADS = ("models", "web-vendor")


@dataclass
class ResetReport:
    removed: list[Path]
    failed: list[tuple[Path, str]]


def saved_items(data_dir: Path, include_downloads: bool = False) -> list[Path]:
    """What a reset would delete: settings, calibration, paired phones and their
    keys, TLS certificate, profiles, logs (and optionally downloads)."""
    if not data_dir.is_dir():
        return []
    return sorted(p for p in data_dir.iterdir() if include_downloads or p.name not in DOWNLOADS)


def delete_user_data(data_dir: Path, include_downloads: bool = False) -> ResetReport:
    """Delete saved data. Open log files are closed first so Windows can delete them."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler):
            try:
                if Path(handler.baseFilename).resolve().is_relative_to(data_dir.resolve()):
                    handler.close()
            except (OSError, ValueError):
                pass
    removed: list[Path] = []
    failed: list[tuple[Path, str]] = []
    for item in saved_items(data_dir, include_downloads):
        try:
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()
            removed.append(item)
        except OSError as exc:
            failed.append((item, exc.strerror or str(exc)))
    if include_downloads and not failed:
        try:
            data_dir.rmdir()
        except OSError:
            pass
    return ResetReport(removed, failed)
