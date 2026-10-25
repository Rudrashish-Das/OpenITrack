"""Find widgets that don't fit the OpenITrack window, on every tab and setup page.

Run on its own (tests/test_window_fit.py does), so it starts up exactly like the
app: DPI mode first, then the window and its theme. Handy when changing the layout:

    python tests/window_fit.py            # report for the window's own size
    python tests/window_fit.py 900x600    # report for another size
"""

from __future__ import annotations

import os
import queue
import sys
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import cv2
import numpy as np

from openitrack import gui
from openitrack.config import DEFAULT_CONFIG_TEXT
from openitrack.config_edit import set_values
from openitrack.setup_wizard import SetupWizard
from openitrack.sources import webcam

# Widgets that scroll or stretch: being smaller than they ask for is fine, as
# long as they stay inside their container. (Canvases are not: the live bars and
# view are drawn at fixed positions.)
FLEXIBLE = (tk.Text, ttk.Treeview, tk.Listbox, ttk.Scrollbar, tk.Scrollbar,
            ttk.Separator, ttk.Notebook, ttk.Frame, ttk.LabelFrame, tk.Frame, ttk.Panedwindow)

LONG_PROFILE = "Microsoft Flight Simulator 2024 (VR) - 1"   # 40 characters, the maximum
LONG_ERROR = "Could not open camera 0: the device is being used by another applicat"  # 70, as shown
PHONE = {"urls": ["https://openitrack-DESKTOP-GAMING01.local", "https://192.168.100.200",
                  "https://172.16.254.101"],
         "hostname": "openitrack-DESKTOP-GAMING01.local",
         "pending": [("Android phone", "192.168.100.200"), ("iPhone", "192.168.100.201")],
         "active": "Android phone"}


def open_root() -> tk.Tk:
    """A Tk root set up the way gui.main() does it."""
    gui._dpi_aware()
    root = tk.Tk()
    try:
        gui.sv_ttk.set_theme("dark")
    except Exception:
        pass
    return root


def make_app(root: tk.Tk, config_dir: Path, setup_done: bool = True):
    """The real window, with everything that would touch hardware or the network stubbed out."""
    path = config_dir / "config.toml"
    path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
    set_values(path, "ui", {"setup_done": setup_done})
    root.bell = lambda *a: None
    gui.App.start = lambda self: None
    gui.App.stop = lambda self: None
    gui.App.send = lambda self, action: None
    gui._flash = lambda root: None
    return gui.App(root, path, queue.Queue())


def settle(root: tk.Tk) -> None:
    # Wrapping labels re-wrap after the first layout, which lays out again.
    for _ in range(4):
        root.update_idletasks()
        root.update()


def problems(root: tk.Tk) -> list[str]:
    """Visible widgets that are cut off: outside their container (or the window),
    or a fixed-size widget given less room than it needs."""
    out = []
    rx, ry = root.winfo_rootx(), root.winfo_rooty()
    rw, rh = root.winfo_width(), root.winfo_height()

    def visit(w):
        for child in w.winfo_children():
            if isinstance(child, tk.Toplevel) or not child.winfo_ismapped():
                continue
            x, y = child.winfo_rootx() - rx, child.winfo_rooty() - ry
            cw, ch = child.winfo_width(), child.winfo_height()
            px, py = w.winfo_rootx() - rx, w.winfo_rooty() - ry
            pw, ph = w.winfo_width(), w.winfo_height()
            name = describe(child)
            if x + cw > min(px + pw, rw) + 1 or y + ch > min(py + ph, rh) + 1 or x < px - 1 or y < py - 1:
                out.append(f"cut off: {name} at {x},{y} {cw}x{ch}, container ends at "
                           f"{min(px + pw, rw)},{min(py + ph, rh)}")
            elif not isinstance(child, FLEXIBLE) and (cw < child.winfo_reqwidth() - 1
                                                      or ch < child.winfo_reqheight() - 1):
                out.append(f"squeezed: {name} is {cw}x{ch}, needs "
                           f"{child.winfo_reqwidth()}x{child.winfo_reqheight()}")
            else:
                visit(child)

    visit(root)
    return out


def describe(w) -> str:
    text = ""
    try:
        text = str(w.cget("text"))
    except tk.TclError:
        pass
    return f"{w.winfo_class()} {text[:50]!r}" if text else f"{w.winfo_class()} {w}"


def worst_case_main(app) -> None:
    """Fill the main window with the longest content it can show."""
    app._closing = True  # stop the 10 Hz refresh putting the idle state back
    app._status("Phone wants to pair", LONG_ERROR, "bad")
    app._render_phone(PHONE)
    app.profile_box.set(LONG_PROFILE)
    # Not calibrated is the longer text in Settings, a calibration date the longer one
    # on the Tracking tab.
    app._update_calibration_status(None)
    app.calib_lbl.configure(text="Calibrated 2026-10-25 18:11")
    app.engine_error = LONG_ERROR


def main_pages(app):
    """(name, callable that shows it) for every tab, phone tab in both modes."""
    pages = []
    for tab in app.nb.tabs():
        name = app.nb.tab(tab, "text").strip()
        pages.append((name, lambda tab=tab: app.nb.select(tab)))
        if name == "Phone":
            def phone_off(tab=tab):
                app._render_phone(None)
                app.nb.select(tab)
            pages.append(("Phone (webcam mode)", phone_off))
    return pages


def wizard_pages(app):
    """(name, callable) for every setup step, webcam and phone versions of the camera steps."""
    png = cv2.imencode(".png", np.full((180, 240, 3), 90, np.uint8))[1].tobytes()
    webcam.probe_cameras = lambda *a, **k: [(i, 1280, 720, png) for i in range(4)]  # four cameras
    wiz = SetupWizard(app, on_done=lambda: None)
    pages = []
    for i, (name, _) in enumerate(wiz.steps):
        for source in (("webcam", "phone") if name in ("Camera", "Connect", "Position") else ("webcam",)):
            def show(i=i, source=source):
                wiz.source = source
                wiz.show(i)
                for _ in range(3):  # let a camera scan's result arrive
                    app.root.update()
                    while not app.ui_queue.empty():
                        app.ui_queue.get_nowait()()
            pages.append((f"Setup: {name}" + (f" ({source})" if name in ("Camera", "Connect", "Position") else ""),
                          show))
    return wiz, pages


def check(root: tk.Tk, config_dir: Path, size: str | None = None) -> dict[str, list[str]]:
    """{page: problems} for the main window and the setup guide at `size` (default: the window's own)."""
    app = make_app(root, config_dir)
    if size:
        root.geometry(size)
    root.deiconify()
    worst_case_main(app)
    found = {}
    for name, show in main_pages(app):
        show()
        settle(root)
        found[name] = problems(root)
    wiz, pages = wizard_pages(app)
    for name, show in pages:
        show()
        settle(root)
        found[name] = problems(root)
    wiz.frame.destroy()
    return found


if __name__ == "__main__":
    d = Path(tempfile.mkdtemp())
    os.environ["APPDATA"] = str(d / "appdata")
    root = open_root()
    result = check(root, d, sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"window {root.winfo_width()}x{root.winfo_height()}")
    for page, issues in result.items():
        print(f"{page}: {'ok' if not issues else ''}")
        for line in issues:
            print("   ", line)
    root.destroy()
    sys.exit(1 if any(result.values()) else 0)
