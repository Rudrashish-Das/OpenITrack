import sys
import tkinter as tk
import uuid

import pytest

from openitrack import freetrack

if sys.platform == "win32":
    import winreg

TEST_REGISTRY_ROOT = r"Software\OpenITrackTests"


def delete_registry_tree(key: str) -> None:
    """Delete an HKCU key and everything under it, if it exists."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_ALL_ACCESS) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                delete_registry_tree(f"{key}\\{sub}")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
    except FileNotFoundError:
        pass


@pytest.fixture(autouse=True)
def no_real_game_output(monkeypatch, tmp_path):
    """Keep tests away from the real TrackIR/FreeTrack registry entries and shared
    memory: the direct game output uses throwaway names in every test."""
    tag = uuid.uuid4().hex
    root = f"{TEST_REGISTRY_ROOT}\\{tag}"
    monkeypatch.setattr(freetrack, "HEAP_NAME", f"OpenITrackTest_{tag}")
    monkeypatch.setattr(freetrack, "MUTEX_NAME", f"OpenITrackTestMutex_{tag}")
    monkeypatch.setattr(freetrack, "REGISTRY_KEYS", (f"{root}\\NPClient Location", f"{root}\\FreeTrackClient"))
    monkeypatch.setattr(freetrack, "registry_backup_path", lambda: tmp_path / "registry-backup.json")
    # No stand-in TrackIR.exe unless a test brings its own (see test_trackir_process.py).
    monkeypatch.setattr(freetrack, "TRACKIR_EXE", tmp_path / "TrackIR-not-built.exe")
    yield root
    if sys.platform == "win32":
        delete_registry_tree(root)
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, TEST_REGISTRY_ROOT)   # only if empty
        except OSError:
            pass


@pytest.fixture
def tk_root(capfd):
    """A hidden Tk root window; skips the test where Tk can't open one."""
    # Tcl sets up its stdout/stderr channels when the window opens. With pytest's
    # output capture in place it then sometimes fails to load its own startup
    # scripts ("Can't find a usable tk.tcl"), so open it with capture paused.
    with capfd.disabled():
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            pytest.skip(f"Tk can't open a window: {exc}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def shown_root(tk_root):
    """A root that counts as shown (Tk lays out only shown windows) but is see-through."""
    tk_root.attributes("-alpha", 0.0)
    tk_root.deiconify()
    return tk_root
