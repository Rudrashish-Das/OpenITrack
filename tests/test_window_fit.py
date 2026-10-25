"""The window has one fixed size, so everything on every tab and setup page must fit it;
on a screen too small for it the window shrinks and its tabs scroll instead."""

import os
import queue
import subprocess
import sys
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk

import pytest

from openitrack import gui
from openitrack.config import DEFAULT_CONFIG_TEXT
from openitrack.config_edit import set_values
from openitrack.gui import WINDOW_FRAME, WINDOW_SIZE, WINDOW_TITLE, App, fitted_size, wrapping

ROOT = Path(__file__).resolve().parent.parent


def run_fit_check(tmp_path, *args):
    # Its own process: it starts up like the app (DPI mode, then theme) and stubs App methods.
    return subprocess.run([sys.executable, str(ROOT / "tests" / "window_fit.py"), *args], capture_output=True,
                          text=True, timeout=240, cwd=ROOT, env={**os.environ, "APPDATA": str(tmp_path)})


@pytest.mark.skipif(sys.platform != "win32", reason="laid out for Windows fonts and scaling")
def test_everything_fits_the_window(tmp_path):
    out = run_fit_check(tmp_path)
    assert f"window {WINDOW_SIZE[0]}x{WINDOW_SIZE[1]}" in out.stdout, out.stdout + out.stderr
    assert out.returncode == 0, "\n" + out.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="laid out for Windows fonts and scaling")
def test_the_fit_check_catches_a_window_too_small(tmp_path):
    out = run_fit_check(tmp_path, "860x560")
    assert out.returncode == 1
    assert "cut off:" in out.stdout or "squeezed:" in out.stdout


@pytest.fixture
def app_with_screen(shown_root, tmp_path, monkeypatch):
    """Build the window as if the screen's usable area were `work` (None: unknown)."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    path = tmp_path / "config.toml"
    path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
    set_values(path, "ui", {"setup_done": True})  # no setup guide opening on top

    def build(work):
        monkeypatch.setattr(gui, "_work_area", lambda: work)
        return App(shown_root, path, queue.Queue())
    return build


def test_window_has_a_fixed_size(shown_root, app_with_screen):
    app_with_screen((0, 0, 2560, 1392))
    shown_root.update_idletasks()
    assert shown_root.resizable() == (False, False)
    assert shown_root.title() == WINDOW_TITLE  # with the version
    assert shown_root.geometry().split("+")[0] == "{}x{}".format(*WINDOW_SIZE)


def test_window_fits_a_1080p_laptop_at_150_percent():
    work = (0, 0, 1280, 720 - 48)  # logical pixels, minus a Windows 11 taskbar
    assert fitted_size(WINDOW_SIZE, work) == WINDOW_SIZE


def test_fitted_size_shrinks_only_what_does_not_fit():
    assert fitted_size((940, 630), None) == (940, 630)
    assert fitted_size((940, 630), (0, 0, 2560, 1392)) == (940, 630)
    # 1080p at 175%: about 1097x617 usable.
    assert fitted_size((940, 630), (0, 0, 1097, 617)) == (940, 617 - WINDOW_FRAME[1])
    assert fitted_size((940, 630), (100, 50, 900, 650)) == (900 - 100 - WINDOW_FRAME[0], 650 - 50 - WINDOW_FRAME[1])
    assert fitted_size((940, 630), (0, 0, 10, 10)) == (320, 240)  # never absurdly small


def test_on_a_small_screen_the_window_shrinks_and_tabs_scroll(shown_root, app_with_screen):
    work = (0, 0, 911, 464)  # a 1366x768 laptop at 150%, minus the taskbar
    app = app_with_screen(work)
    w, h = fitted_size(WINDOW_SIZE, work)
    assert (w, h) < WINDOW_SIZE
    assert shown_root.geometry() == f"{w}x{h}+0+0"  # top-left: the title bar stays on screen
    for tab in (app.tab_track, app.tab_settings, app.tab_phone):
        app.nb.select(tab)  # as the user does; the scroll state is set as it's laid out
        shown_root.update()
        assert tab.scrollable and tab.bar.winfo_manager() == "place", str(tab)
        tab.canvas.yview_moveto(1)
        top, bottom = tab.canvas.yview()
        assert top > 0 and bottom == pytest.approx(1)  # the bottom of the tab can be reached


# -- no flicker when a tab is first opened ---------------------------------------------------
def _layout(app):
    labels, stack = {}, [app.root]
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        if isinstance(w, ttk.Label) and str(w.cget("wraplength")) not in ("", "0"):
            labels[str(w)] = str(w.cget("wraplength"))
    areas = {str(t): (t.body.winfo_reqheight(), bool(t.bar.winfo_manager()))
             for t in (app.tab_track, app.tab_profiles, app.tab_phone, app.tab_keys, app.tab_settings)}
    return labels, areas


@pytest.mark.skipif(sys.platform != "win32", reason="the invisible warm-up needs window transparency")
def test_tabs_are_laid_out_before_they_are_first_opened(shown_root, app_with_screen):
    app = app_with_screen(None)
    before = _layout(app)
    assert app.pair_msg.cget("wraplength") and app.cal_status.cget("wraplength")  # wrapped already
    assert app.tab_settings.canvas.winfo_height() > 1 and app.tab_track.canvas.winfo_height() > 1
    for tab in app.nb.tabs():
        app.nb.select(tab)
        shown_root.update()
    assert _layout(app) == before  # opening a tab re-wraps nothing and moves nothing


def test_a_wrapping_label_takes_its_width_from_its_container(shown_root):
    frame = ttk.Frame(shown_root, width=300, height=400)
    frame.pack_propagate(False)  # a fixed-size container
    frame.pack()
    label = wrapping(ttk.Label(frame, text="word " * 80))
    label.pack(fill="x")
    shown_root.update()
    assert label.winfo_width() == 300
    # It asks for (almost) no width, so its column can't grow and re-wrap it in a loop.
    assert label.winfo_reqwidth() < 50
    assert int(str(label.cget("wraplength"))) == label.winfo_width() - 4
    assert label.winfo_reqheight() >= 3 * tkfont.nametofont("TkDefaultFont").metrics("linespace")  # many lines
