"""OpenITrack window: start/stop tracking, live view, phone pairing, shortcuts
and settings. Tkinter + the Sun Valley theme keeps it light: the window only
redraws ~10 times a second and the tracker runs on its own thread."""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes
import io
import json
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, simpledialog, ttk

import sv_ttk

from . import __version__
from .config import ConfigError, ConfigFile, HotkeyConfig, default_config_path
from .config_edit import set_value, set_values
from .devices import DeviceStore
from .foreground import running_apps
from .hotkeys import HOLD_ACTIONS, MAX_JOYSTICKS, InputPoller, _key_name, parse_binding
from .profiles import PROFILE_SECTIONS, app_key
from .scrollarea import ScrollArea
from .reset import delete_user_data, saved_items
from .setup_wizard import GAME_OUTPUT_CHOICES, SetupWizard

log = logging.getLogger("openitrack.gui")

ASSETS = Path(__file__).resolve().parent / "assets"
WINDOW_TITLE = f"OpenITrack {__version__}"  # also how a second launch finds the open window
# Groups the window on the taskbar as its own app, with its own icon: without it,
# Windows shows the program that started it (python.exe or the pip launcher).
APP_USER_MODEL_ID = "OpenITrack.OpenITrack"
WEB = Path(__file__).resolve().parent / "web"
CALIBRATION_FILE = "calibration.json"
DEFAULT_PROFILE = "Default"
DEFAULT_IID = "::default"   # profile names can't contain ':'

ACTIONS = [
    ("recenter", "Recenter view"),
    ("pause", "Pause / resume"),
    ("toggle_gaze", "Eye-assist on/off"),
    ("toggle_translation", "Leaning (x/y/z) on/off"),
    ("toggle_roll", "Head tilt (roll) on/off"),
    ("sensitivity_up", "Sensitivity up"),
    ("sensitivity_down", "Sensitivity down"),
    ("calibrate", "Calibrate"),
    ("toggle_preview", "Camera preview"),
    ("toggle_overlay", "Gaze dot on/off"),
    ("freeze_hold", "Freeze view (hold)"),
    ("center_hold", "Look straight ahead (hold)"),
    ("next_profile", "Next profile"),
]
ACTION_LABELS = dict(ACTIONS)

COLORS = {"ok": "#3ecf8e", "warn": "#f2b84b", "bad": "#ef5d5d", "info": "#4c8dff", "idle": "#8b95a3"}
AXIS_LIMITS = {"yaw": 180.0, "pitch": 90.0, "roll": 90.0, "x": 30.0, "y": 30.0, "z": 30.0}
AXIS_UNITS = {"yaw": "°", "pitch": "°", "roll": "°", "x": " cm", "y": " cm", "z": " cm"}
# Fits a 1080p laptop screen at 150% scaling (1280x720 usable, minus taskbar and
# title bar), the smallest common setup. Everything fits down to about 917x616.
WINDOW_SIZE = (940, 630)
BAR_TOP, BAR_ROW = 18, 25   # live output bars: first row's centre and row spacing, pixels
MODIFIER_VKS = {0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}


_PRETTY = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win", "pageup": "PageUp",
           "pagedown": "PageDown", "scrolllock": "ScrollLock", "capslock": "CapsLock",
           "backspace": "Backspace", "pause": "Pause", "insert": "Insert", "delete": "Delete",
           "home": "Home", "end": "End", "space": "Space", "enter": "Enter", "tab": "Tab",
           "mouse3": "Middle mouse", "mouse4": "Mouse 4", "mouse5": "Mouse 5"}


def _pretty_part(p: str) -> str:
    low = p.lower()
    if low in _PRETTY:
        return _PRETTY[low]
    if low.startswith("numpad"):
        return "Num " + low[6:]
    if low.startswith("joy") and ":" in low:
        dev, btn = low[3:].split(":", 1)
        return f"Joystick {dev} button {btn.removeprefix('button').removeprefix('b')}"
    return p.upper()


def pretty_binding(spec: str, sep: str = " + ") -> str:
    if not spec:
        return "—"
    return sep.join(_pretty_part(p) for p in spec.split("+"))


# -- Windows helpers ------------------------------------------------------------------

def _dpi_aware() -> None:
    """Make the window the same size on every monitor, whatever its scaling.

    Tk lays everything out in pixels once and can't rescale when the window moves
    to a monitor with different scaling (e.g. a 100% desktop screen and a 200%
    laptop panel). So Windows does the scaling for us, per monitor and live while
    dragging, and with GDI scaling text is rendered sharp at the higher size.
    The tracking thread opts back into real pixels for the calibration overlay
    and camera preview (monitors.use_physical_pixels)."""
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-5)):  # UNAWARE_GDISCALED
            return
    except AttributeError:
        pass
    # Older Windows 10: plain DPI-unaware also gets the same size everywhere.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(0)
    except (AttributeError, OSError):
        pass


def _taskbar_identity() -> None:
    """Give the process its own taskbar identity, before any window exists, so the
    taskbar shows OpenITrack's icon instead of the icon of whatever started it."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except (AttributeError, OSError):
        pass


def _single_instance() -> bool:
    """False if another OpenITrack window is already open (it gets focused)."""
    if sys.platform != "win32":
        return True
    kernel32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
    _single_instance.handle = kernel32.CreateMutexW(None, False, "Local\\OpenITrack.Window")
    if kernel32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
        return True
    user32.FindWindowW.restype = ctypes.c_void_p  # a window handle is pointer-sized
    hwnd = user32.FindWindowW(None, WINDOW_TITLE)
    if hwnd:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    return False


def _flash(root: tk.Tk) -> None:
    if sys.platform != "win32":
        return
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        ctypes.windll.user32.FlashWindow(hwnd, True)
    except Exception:
        pass


# Settings choices as (config value, what the user sees). Each label says what
# picking it does, not just its name.
FPS_CHOICES = [
    (15, "15 fps: least CPU, view reacts later"),
    (20, "20 fps: less CPU, a little more lag"),
    (24, "24 fps: a bit less CPU"),
    (30, "30 fps: recommended"),
    (60, "60 fps: smoothest, twice the CPU (60 fps camera)"),
]
RESOLUTION_CHOICES = [
    ((320, 240), "320×240: a little less CPU, slightly less precise"),
    ((640, 480), "640×480: recommended"),
    ((1280, 720), "1280×720: sharper, several times the camera CPU"),
]
FORMAT_CHOICES = [
    ("mjpg", "MJPG: compressed, any resolution (recommended)"),
    ("yuy2", "YUY2: uncompressed, no decoding; often ≤ 640×480"),
    ("auto", "Automatic: let the camera driver choose"),
]
# One width for all the dropdowns, so they line up (they also stretch to their box).
CHOICE_WIDTH = max(len(text) for choices in (FPS_CHOICES, RESOLUTION_CHOICES, FORMAT_CHOICES)
                   for _, text in choices) - 4


def choice_text(choices: list[tuple], value) -> str:
    """The label for `value`, or a plain description of a value set in the config file."""
    for v, text in choices:
        if v == value:
            return text
    if isinstance(value, tuple):
        shown = "×".join(map(str, value))
    elif isinstance(value, int):
        shown = f"{value} fps"
    else:
        shown = str(value)
    return f"{shown}: set in the config file"


# Window frame around the content: left + right border, title bar + top/bottom border.
WINDOW_FRAME = (16, 39)
OFF_SCREEN = -32000  # a window position no monitor covers


def fitted_size(size: tuple[int, int], work_area: tuple[int, int, int, int] | None,
                frame: tuple[int, int] = WINDOW_FRAME) -> tuple[int, int]:
    """`size`, shrunk where needed so the window with its frame fits `work_area`
    (left, top, right, bottom; None = unknown, keep `size`)."""
    if work_area is None:
        return size
    left, top, right, bottom = work_area
    return (min(size[0], max(320, right - left - frame[0])), min(size[1], max(240, bottom - top - frame[1])))


def _work_area() -> tuple[int, int, int, int] | None:
    """The main screen minus the taskbar, in this window's pixels (Windows only)."""
    if sys.platform != "win32":
        return None
    rect = ctypes.wintypes.RECT()
    if not ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def ui_font_family(root: tk.Misc) -> str:
    """Windows 11's UI font, else Windows 10's (Tk would otherwise fall back to Arial)."""
    if sys.platform != "win32":
        return "TkDefaultFont"
    installed = set(tkfont.families(root))
    return next((f for f in ("Segoe UI Variable Text", "Segoe UI") if f in installed), "TkDefaultFont")


def set_options(widget, **options) -> bool:
    """configure() only the options that differ from what the widget shows now.
    The window refreshes 10x a second; unchanged values then cost no redraw or
    relayout. Returns True when something changed."""
    changed = {k: v for k, v in options.items() if str(widget.cget(k)) != str(v)}
    if changed:
        widget.configure(**changed)
    return bool(changed)


def set_item_options(canvas: tk.Canvas, item: int, **options) -> bool:
    """set_options() for a canvas item."""
    changed = {k: v for k, v in options.items() if str(canvas.itemcget(item, k)) != str(v)}
    if changed:
        canvas.itemconfigure(item, **changed)
    return bool(changed)


def _without_element(layout: list, name: str) -> list:
    """A ttk layout with one element removed; its children take its place."""
    out = []
    for elem, opts in layout:
        children = _without_element(opts.get("children", []), name)
        if elem == name:
            out.extend(children)
            continue
        opts = dict(opts)
        if children or "children" in opts:
            opts["children"] = children
        out.append((elem, opts))
    return out


def _clear_combobox_selection(event) -> None:
    box = event.widget
    box.selection_clear()
    box.icursor("end")


def wrapping(label: ttk.Label) -> ttk.Label:
    """Make a label re-wrap its text to the width its container gives it (place it
    with fill="x" or sticky="ew"). It asks for no width of its own: if it asked for
    the width it wrapped to, its column would resize, it would re-wrap, and the tab
    would reflow back and forth (visible flicker) before settling."""
    label.configure(width=1)
    label._rewraps = True  # App._lay_out_tabs lays out tabs holding one before they're opened

    def rewrap(e) -> None:
        wrap = max(120, e.width - 4)
        if str(label.cget("wraplength")) != str(wrap):
            label.configure(wraplength=wrap)

    label.bind("<Configure>", rewrap)
    return label


def _rewraps(widget: tk.Misc) -> bool:
    """True if `widget` or anything inside it is a wrapping() label."""
    return getattr(widget, "_rewraps", False) or any(_rewraps(c) for c in widget.winfo_children())


def _open(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - opens the user's own file/folder


class _QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put_nowait(self.format(record))
        except queue.Full:
            pass


# -- the window ---------------------------------------------------------------------

class App:
    TICK_MS = 100

    def __init__(self, root: tk.Tk, config_path: Path, log_queue: queue.Queue):
        self.root = root
        self.config_path = config_path
        self.config_dir = config_path.parent
        self.cf = ConfigFile(config_path)
        self.log_queue = log_queue
        self.ui_queue: queue.Queue = queue.Queue()     # callables from worker threads
        self.engine = None
        self.engine_thread: threading.Thread | None = None
        self.engine_error: str | None = None
        self.devices = DeviceStore(self.config_dir)
        self._sync_pause_until = 0.0
        self._last_pending: list = []
        self._shown_pose: tuple | None = None   # what the bars and view dot show now
        self._qr_url = ""
        self._cameras: list[tuple[int, int, int]] = []
        self._profiles: dict = {}
        self._engine_profile: str | None = None
        self._closing = False

        root.title(WINDOW_TITLE)
        # A fixed size: every tab and setup page is laid out to fit it (tests/test_window_fit.py
        # checks), and resizing a Tk window on Windows redraws slowly.
        work = _work_area()
        w, h = fitted_size(WINDOW_SIZE, work)
        # On a screen too small for the window, shrink it (its tabs then scroll) and
        # put it in the top-left corner so the title bar is reachable.
        root.geometry(f"{w}x{h}" + (f"+{work[0]}+{work[1]}" if (w, h) != WINDOW_SIZE and work else ""))
        root.resizable(False, False)
        self._placement = (w, h, work)
        ico = ASSETS / "icon.ico"
        if ico.exists():
            try:
                root.iconbitmap(default=str(ico))
            except tk.TclError:
                pass
        self._fonts()
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._show_configured_camera()  # scanning opens every camera (~15 s): only on Refresh
        self.refresh_devices()
        self._lay_out_tabs()
        self.root.after(self.TICK_MS, self.tick)
        self.wizard = None
        if not self.cf.config.ui.setup_done:
            self.root.after(50, self.run_setup)  # first run: guide the user step by step
        elif self.cf.config.ui.autostart:
            self.root.after(300, self.start)

    # ---- layout ----------------------------------------------------------------
    def _lay_out_tabs(self) -> None:
        """Lay out the tabs with wrapping text once, off-screen, before the window
        appears. A tab is otherwise only laid out when first opened, and its text
        wrapping into place then shows as a flicker. Other tabs lay out in a single
        pass and are left alone: each one done here adds ~50-130 ms to startup."""
        root = self.root
        if root.state() == "withdrawn":
            return  # hidden by the caller: nothing will be seen
        w, h, work = self._placement
        if root.winfo_ismapped():  # rebuilt in place (e.g. after deleting all data)
            x, y = root.winfo_x(), root.winfo_y()
        elif (w, h) != WINDOW_SIZE and work:
            x, y = work[0], work[1]  # shrunk to fit: top-left, title bar reachable
        elif work:
            x = work[0] + max(0, (work[2] - work[0] - w - WINDOW_FRAME[0]) // 2)
            y = work[1] + max(0, (work[3] - work[1] - h - WINDOW_FRAME[1]) // 2)
        else:
            x, y = 100, 100
        # Tk lays out only windows that are shown, so show it where no screen is.
        root.geometry(f"{w}x{h}+{OFF_SCREEN}+{OFF_SCREEN}")
        try:
            current = self.nb.select()
            root.update()  # the tab shown at start: laid out now instead of on screen
            for tab in self.nb.tabs():
                if tab != current and _rewraps(self.root.nametowidget(tab)):
                    self.nb.select(tab)
                    root.update()  # the layout and the <Configure> events that re-wrap labels
            self.nb.select(current)
            root.update()
        finally:
            root.geometry(f"{w}x{h}+{x}+{y}")

    def _fonts(self) -> None:
        family = ui_font_family(self.root)
        self.f_title = (family, 17, "bold")
        self.f_h = (family, 11, "bold")
        self.f_body = (family, 10)
        self.f_small = (family, 9)
        self.f_big = (family, 15, "bold")
        self.f_code = ("Consolas", 22, "bold")
        style = ttk.Style()
        style.configure("Muted.TLabel", foreground=COLORS["idle"])
        style.configure("H.TLabel", font=self.f_h)
        style.configure("Big.TLabel", font=self.f_big)
        # The theme draws a dotted focus box around a tab after it's clicked, which
        # looks like a stray outline. Drop that element; tabs stay keyboard-reachable
        # with Ctrl+Tab / Ctrl+Shift+Tab (enable_traversal on the notebook).
        style.layout("TNotebook.Tab", _without_element(style.layout("TNotebook.Tab"), "Notebook.focus"))
        # Tk selects a dropdown's whole text after you pick an item, leaving it
        # highlighted. Clear that for every dropdown (class binding, added to any
        # per-widget handlers) and leave the cursor at the end for editable ones.
        self.root.bind_class("TCombobox", "<<ComboboxSelected>>", _clear_combobox_selection, add="+")

    def _build(self) -> None:
        root = self.root
        outer = ttk.Frame(root, padding=(16, 12, 16, 12))
        outer.pack(fill="both", expand=True)

        # Header: logo, title, status, start/stop.
        head = ttk.Frame(outer)
        head.pack(fill="x")
        self._logo = None
        if (WEB / "icon.png").exists():
            try:
                self._logo = tk.PhotoImage(file=str(WEB / "icon.png")).subsample(6, 6)
                ttk.Label(head, image=self._logo).pack(side="left", padx=(0, 10))
            except tk.TclError:
                pass
        titles = ttk.Frame(head)
        titles.pack(side="left")
        ttk.Label(titles, text="OpenITrack", font=self.f_title).pack(anchor="w")
        ttk.Label(titles, text="Head & eye tracking for sims", style="Muted.TLabel").pack(anchor="w")

        self.start_btn = ttk.Button(head, text="Start tracking", style="Accent.TButton",
                                    command=self.toggle_running, width=16)
        self.start_btn.pack(side="right")
        pill = ttk.Frame(head)
        pill.pack(side="right", padx=16)
        self.status_dot = tk.Canvas(pill, width=14, height=14, highlightthickness=0, bd=0)
        self.status_dot.pack(side="left", padx=(0, 6))
        self._dot = self.status_dot.create_oval(2, 2, 12, 12, fill=COLORS["idle"], outline="")
        status_text = ttk.Frame(pill)
        status_text.pack(side="left")
        self.status_lbl = ttk.Label(status_text, text="Stopped", font=self.f_h)
        self.status_lbl.pack(anchor="w")
        # Wraps, so a long error takes two lines instead of squeezing the header.
        self.status_sub = ttk.Label(status_text, text="", style="Muted.TLabel", wraplength=230, justify="left")
        self.status_sub.pack(anchor="w")
        prof = ttk.Frame(head)
        prof.pack(side="right", padx=(0, 8))
        ttk.Label(prof, text="Profile", style="Muted.TLabel").pack(anchor="w")
        self.profile_var = tk.StringVar(value=self.cf.profile or DEFAULT_PROFILE)
        self.profile_box = ttk.Combobox(prof, textvariable=self.profile_var, width=18, state="readonly")
        self.profile_box.pack(anchor="w")
        self.profile_box.bind("<<ComboboxSelected>>", lambda e: self.select_profile(
            "" if self.profile_var.get() == DEFAULT_PROFILE else self.profile_var.get()))

        # A phone waiting to pair is shown in the header status, and the Phone tab
        # opens with the code box focused (_render_phone), so no banner is needed.

        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True, pady=(12, 0))
        self.nb.enable_traversal()  # Ctrl+Tab / Ctrl+Shift+Tab switch tabs
        # Tabs scroll if their content ever turns out taller than the window (a
        # small screen, unexpected fonts); normally it fits and no scrollbar shows.
        self.tab_track = ScrollArea(self.nb, padding=12)
        self.tab_profiles = ScrollArea(self.nb, padding=12)
        self.tab_phone = ScrollArea(self.nb, padding=12)
        self.tab_keys = ScrollArea(self.nb, padding=12)
        self.tab_settings = ScrollArea(self.nb, padding=12)
        self.tab_log = ttk.Frame(self.nb, padding=12)  # just the log text, which scrolls itself
        for tab, name in ((self.tab_track, "Tracking"), (self.tab_profiles, "Profiles"), (self.tab_phone, "Phone"),
                          (self.tab_keys, "Shortcuts"), (self.tab_settings, "Settings"),
                          (self.tab_log, "Log")):
            self.nb.add(tab, text=f"  {name}  ")
        self._build_tracking()
        self._build_profiles()
        self._build_phone()
        self._build_shortcuts()
        self._build_settings()
        self._build_log()

    # Tracking tab ---------------------------------------------------------------
    def _build_tracking(self) -> None:
        t = self.tab_track.body
        t.columnconfigure(0, weight=3)
        t.columnconfigure(1, weight=2)

        src = ttk.LabelFrame(t, text="Camera", padding=(12, 8))
        src.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.source_var = tk.StringVar(value=self.cf.config.source.type)
        self.rb_webcam = ttk.Radiobutton(src, text="Webcam", value="webcam", variable=self.source_var,
                                         command=self.on_source_change)
        self.rb_phone = ttk.Radiobutton(src, text="Phone", value="phone", variable=self.source_var,
                                        command=self.on_source_change)
        self.rb_webcam.pack(side="left")
        self.rb_phone.pack(side="left", padx=(12, 24))
        ttk.Label(src, text="Webcam:").pack(side="left")
        self.camera_var = tk.StringVar()
        self.camera_box = ttk.Combobox(src, textvariable=self.camera_var, width=30)
        self.camera_box.pack(side="left", padx=6)
        self.camera_box.bind("<<ComboboxSelected>>", lambda e: self.on_camera_change())
        self.camera_box.bind("<Return>", lambda e: self.on_camera_change())
        self.cam_refresh = ttk.Button(src, text="Refresh", command=self.refresh_cameras)
        self.cam_refresh.pack(side="left")
        self.src_hint = ttk.Label(src, text="", style="Muted.TLabel")
        self.src_hint.pack(side="left", padx=12)

        live = ttk.LabelFrame(t, text="Live output (what the game receives)", padding=12)
        live.grid(row=1, column=0, sticky="nsew", pady=10, padx=(0, 12))
        self.bars = tk.Canvas(live, height=BAR_TOP + BAR_ROW * (len(AXIS_LIMITS) - 1) + BAR_TOP,
                              highlightthickness=0, bd=0)
        self.bars.pack(fill="both", expand=True)
        self.bars.bind("<Configure>", lambda e: self._draw_bars_base())
        self._bar_items: dict = {}

        view = ttk.LabelFrame(t, text="Where you're looking", padding=12)
        view.grid(row=1, column=1, sticky="nsew", pady=10)
        self.view = tk.Canvas(view, width=220, height=int(self.bars.cget("height")), highlightthickness=0, bd=0)
        self.view.pack(fill="both", expand=True)
        self.view.bind("<Configure>", lambda e: self._draw_view_base())

        ctl = ttk.LabelFrame(t, text="Controls", padding=12)
        ctl.grid(row=2, column=0, columnspan=2, sticky="ew")
        row1 = ttk.Frame(ctl)
        row1.pack(fill="x")
        self.btn_recenter = ttk.Button(row1, text="Recenter", command=lambda: self.send("recenter"))
        self.btn_pause = ttk.Button(row1, text="Pause", command=lambda: self.send("pause"))
        self.btn_calibrate = ttk.Button(row1, text="Calibrate…", command=lambda: self.send("calibrate"))
        self.btn_preview = ttk.Button(row1, text="Camera preview", command=lambda: self.send("toggle_preview"))
        for b in (self.btn_recenter, self.btn_pause, self.btn_calibrate, self.btn_preview):
            b.pack(side="left", padx=(0, 8))
        self.calib_lbl = ttk.Label(row1, text="", style="Muted.TLabel")
        self.calib_lbl.pack(side="left", padx=8)

        row2 = ttk.Frame(ctl)
        row2.pack(fill="x", pady=(8, 0))
        self.gaze_var = tk.BooleanVar(value=True)
        self.trans_var = tk.BooleanVar(value=True)
        self.roll_var = tk.BooleanVar(value=True)
        self.dot_var = tk.BooleanVar(value=False)
        for text, var, action in (("Eye-assist", self.gaze_var, "toggle_gaze"),
                                  ("Leaning (x/y/z)", self.trans_var, "toggle_translation"),
                                  ("Head tilt (roll)", self.roll_var, "toggle_roll"),
                                  ("Gaze dot", self.dot_var, "toggle_overlay")):
            ttk.Checkbutton(row2, text=text, variable=var, style="Switch.TCheckbutton",
                            command=lambda a=action: self.send(a, sync_pause=True)).pack(side="left", padx=(0, 18))
        # Sensitivity gets its own row: with four switches it no longer fits beside them.
        row3 = ttk.Frame(ctl)
        row3.pack(fill="x", pady=(8, 0))
        ttk.Label(row3, text="Sensitivity").pack(side="left", padx=(0, 8))
        self.sens_var = tk.DoubleVar(value=1.0)
        self.sens = ttk.Scale(row3, from_=0.3, to=3.0, variable=self.sens_var, length=260,
                              command=lambda v: self.on_sensitivity())
        self.sens.pack(side="left")
        self.sens_lbl = ttk.Label(row3, text="×1.00", width=6)
        self.sens_lbl.pack(side="left", padx=8)
        ttk.Button(row3, text="Reset", command=lambda: (self.sens_var.set(1.0), self.on_sensitivity())).pack(side="left")

        self.keys_hint = ttk.Label(t, text="", style="Muted.TLabel")
        self.keys_hint.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        t.rowconfigure(1, weight=1)
        self._update_keys_hint()

    def _draw_bars_base(self) -> None:
        c = self.bars
        c.delete("all")
        self._bar_items = {}
        self._shown_pose = None
        w = max(c.winfo_width(), 300)
        bg = ttk.Style().lookup("TFrame", "background") or "#1c1c1c"
        c.configure(background=bg)
        x0, x1 = 70, w - 90
        mid = (x0 + x1) / 2
        for i, axis in enumerate(AXIS_LIMITS):
            y = BAR_TOP + i * BAR_ROW
            c.create_text(10, y, text=axis.capitalize(), anchor="w", fill="#c9d1d9", font=self.f_body)
            c.create_rectangle(x0, y - 5, x1, y + 5, fill="#2b2f36", outline="")
            c.create_line(mid, y - 9, mid, y + 9, fill="#57606a")
            bar = c.create_rectangle(mid, y - 5, mid, y + 5, fill=COLORS["info"], outline="")
            val = c.create_text(w - 10, y, text="0", anchor="e", fill="#e7ebf0", font=self.f_body)
            self._bar_items[axis] = (bar, val, x0, x1, mid, y)

    def _draw_view_base(self) -> None:
        c = self.view
        c.delete("all")
        bg = ttk.Style().lookup("TFrame", "background") or "#1c1c1c"
        c.configure(background=bg)
        w, h = max(c.winfo_width(), 100), max(c.winfo_height(), 100)
        pad = 14
        # The caption gets its own strip under the box, sized from the font's real
        # line height so it never overlaps the box at any display scaling.
        caption_h = tkfont.Font(font=self.f_small).metrics("linespace") + 6
        x0, y0, x1, y1 = pad, pad, w - pad, h - caption_h - 4
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        c.create_rectangle(x0, y0, x1, y1, outline="#57606a")
        c.create_line(cx, y0, cx, y1, fill="#2b2f36")
        c.create_line(x0, cy, x1, cy, fill="#2b2f36")
        c.create_text(cx, h - 2, text="yaw ← →   pitch ↑ ↓", fill="#8b95a3", font=self.f_small, anchor="s")
        self._view_dot = c.create_oval(cx - 7, cy - 7, cx + 7, cy + 7, fill=COLORS["info"], outline="")
        self._view_geom = (x0, y0, x1, y1)
        self._shown_pose = None

    # Profiles tab ---------------------------------------------------------------
    def _build_profiles(self) -> None:
        t = self.tab_profiles.body
        t.columnconfigure(0, weight=3)
        t.columnconfigure(1, weight=2)
        t.rowconfigure(1, weight=1)
        ttk.Label(t, text="A profile gives one game or app its own response curves, eye-assist, filtering and "
                  "outputs. It only keeps what you change while it's in use; everything else comes from Default.",
                  style="Muted.TLabel", wraplength=840, justify="left").grid(row=0, column=0, columnspan=2, sticky="w")

        lst = ttk.LabelFrame(t, text="Profiles", padding=12)
        lst.grid(row=1, column=0, sticky="nsew", padx=(0, 12), pady=12)
        self.prof_tree = ttk.Treeview(lst, columns=("name", "apps"), show="headings", height=8, selectmode="browse")
        for col, text, width in (("name", "Profile", 150), ("apps", "Used when in front", 220)):
            self.prof_tree.heading(col, text=text)
            self.prof_tree.column(col, width=width, anchor="w")
        self.prof_tree.bind("<<TreeviewSelect>>", lambda e: self._on_profile_pick())
        self.prof_tree.bind("<Double-1>", lambda e: self.use_selected_profile())
        side = ttk.Frame(lst)
        side.pack(side="right", fill="y", padx=(12, 0))
        self.btn_prof_use = ttk.Button(side, text="Use", style="Accent.TButton", width=13,
                                       command=self.use_selected_profile)
        self.btn_prof_use.pack(fill="x")
        ttk.Button(side, text="New…", command=self.new_profile).pack(fill="x", pady=(8, 0))
        self.btn_prof_dup = ttk.Button(side, text="Duplicate…", command=self.duplicate_profile)
        self.btn_prof_dup.pack(fill="x", pady=(8, 0))
        self.btn_prof_ren = ttk.Button(side, text="Rename…", command=self.rename_profile)
        self.btn_prof_ren.pack(fill="x", pady=(8, 0))
        self.btn_prof_del = ttk.Button(side, text="Delete", command=self.delete_profile)
        self.btn_prof_del.pack(fill="x", pady=(8, 0))
        ttk.Button(side, text="Edit file", command=self.edit_profile_file).pack(fill="x", pady=(8, 0))
        self.prof_tree.pack(side="left", fill="both", expand=True)  # after `side`: the list gives way

        apps = ttk.LabelFrame(t, text="Switch to it when this is in front", padding=12)
        apps.grid(row=1, column=1, sticky="nsew", pady=12)
        self.apps_tree = ttk.Treeview(apps, columns=("app",), show="headings", height=5, selectmode="browse")
        self.apps_tree.heading("app", text="Program")
        self.apps_tree.pack(fill="both", expand=True)
        row = ttk.Frame(apps)
        row.pack(fill="x", pady=(8, 0))
        self.app_var = tk.StringVar()
        self.app_box = ttk.Combobox(row, textvariable=self.app_var, width=8, postcommand=self._fill_running_apps)
        self.app_box.bind("<Return>", lambda e: self.add_app())
        self.btn_app_rm = ttk.Button(row, text="Remove", command=self.remove_app)
        self.btn_app_rm.pack(side="right", padx=(8, 0))
        self.btn_app_add = ttk.Button(row, text="Add", command=self.add_app)
        self.btn_app_add.pack(side="right", padx=(8, 0))
        self.app_box.pack(side="left", fill="x", expand=True)
        self.apps_hint = ttk.Label(apps, text="", style="Muted.TLabel", wraplength=320, justify="left")
        self.apps_hint.pack(anchor="w", pady=(8, 0))

        self.auto_var = tk.BooleanVar(value=self.cf.base.profiles.auto_switch)
        ttk.Checkbutton(t, text="Switch profiles automatically when a listed program is in front",
                        variable=self.auto_var, style="Switch.TCheckbutton",
                        command=lambda: self.save("profiles", {"auto_switch": self.auto_var.get()})).grid(
            row=2, column=0, columnspan=2, sticky="w")
        ttk.Label(t, text="Outputs and eye-assist (Settings tab) are saved to the profile in use. For curves and "
                  "filtering, use Edit file: a new profile lists every setting it can change, commented out. "
                  "Remove the # in front of one to change it for that game. Saved changes apply within a "
                  "second, even while tracking.", style="Muted.TLabel", wraplength=840,
                  justify="left").grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.refresh_profiles()

    # Phone tab ------------------------------------------------------------------
    def _build_phone(self) -> None:
        t = self.tab_phone.body
        t.columnconfigure(0, weight=1)
        t.columnconfigure(1, weight=1)

        addr = ttk.LabelFrame(t, text="1  Open this on your phone", padding=12)
        addr.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self.phone_off = ttk.Frame(addr)
        ttk.Label(self.phone_off, text="Phone mode isn't running.", font=self.f_h).pack(anchor="w")
        wrapping(ttk.Label(self.phone_off, text="Your phone does the tracking and sends only head angles, "
                           "encrypted, to this PC. Video never leaves the phone.",
                           style="Muted.TLabel", justify="left")).pack(anchor="w", fill="x", pady=(4, 10))
        ttk.Button(self.phone_off, text="Start phone mode", style="Accent.TButton",
                   command=self.start_phone_mode).pack(anchor="w")
        self.phone_on = ttk.Frame(addr)
        self.qr_lbl = ttk.Label(self.phone_on)
        self.qr_lbl.pack(side="left", padx=(0, 14))
        info = ttk.Frame(self.phone_on)
        info.pack(side="left", fill="both", expand=True)
        ttk.Label(info, text="Scan the code, or type:", style="Muted.TLabel").pack(anchor="w")
        self.url_var = tk.StringVar()
        url = ttk.Entry(info, textvariable=self.url_var, state="readonly", font=self.f_big, width=24)
        url.pack(anchor="w", fill="x", pady=(2, 6))
        ttk.Button(info, text="Copy address", command=self.copy_url).pack(anchor="w")
        self.alt_url = wrapping(ttk.Label(info, text="", style="Muted.TLabel", justify="left"))
        self.alt_url.pack(anchor="w", fill="x", pady=(8, 0))
        wrapping(ttk.Label(info, text="Same Wi-Fi as this PC. The first time, the phone warns about the "
                           "certificate: tap Advanced → Proceed (expected for a local address).",
                           style="Muted.TLabel", justify="left")).pack(anchor="w", fill="x", pady=(8, 0))
        self.phone_off.pack(fill="both", expand=True)

        pair = ttk.LabelFrame(t, text="2  Pair it (first time only)", padding=12)
        pair.grid(row=0, column=1, sticky="nsew")
        self.pair_msg = wrapping(ttk.Label(pair, text="No phone is waiting to pair.", justify="left"))
        self.pair_msg.pack(anchor="w", fill="x")
        row = ttk.Frame(pair)
        row.pack(anchor="w", pady=10)
        self.code_var = tk.StringVar()
        self.code_var.trace_add("write", lambda *a: self._normalize_code())
        self.code_entry = ttk.Entry(row, textvariable=self.code_var, width=8, font=self.f_code, justify="center")
        self.code_entry.pack(side="left")
        self.code_entry.bind("<Return>", lambda e: self.pair())
        self.pair_btn = ttk.Button(row, text="Pair", style="Accent.TButton", command=self.pair)
        self.pair_btn.pack(side="left", padx=10)
        self.pair_result = wrapping(ttk.Label(pair, text="", justify="left"))
        self.pair_result.pack(anchor="w", fill="x")
        wrapping(ttk.Label(pair, text="The 6-character code is shown on the phone. It never travels over "
                           "the network, so only someone who can see your phone can pair.",
                           style="Muted.TLabel", justify="left")).pack(anchor="w", fill="x", pady=(10, 0))

        dev = ttk.LabelFrame(t, text="Paired phones", padding=12)
        dev.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        t.rowconfigure(1, weight=1)
        cols = ("name", "id", "paired", "seen")
        self.dev_tree = ttk.Treeview(dev, columns=cols, show="headings", height=2, selectmode="browse")
        for col, text, width in (("name", "Phone", 160), ("id", "ID", 150), ("paired", "Paired", 170),
                                 ("seen", "Last connected", 170)):
            self.dev_tree.heading(col, text=text)
            self.dev_tree.column(col, width=width, anchor="w")
        self.dev_tree.pack(side="left", fill="both", expand=True)
        side = ttk.Frame(dev)
        side.pack(side="left", fill="y", padx=(12, 0))
        ttk.Button(side, text="Revoke", command=self.revoke_selected).pack(fill="x")
        ttk.Button(side, text="Refresh", command=self.refresh_devices).pack(fill="x", pady=8)
        self.active_lbl = ttk.Label(side, text="", style="Muted.TLabel", wraplength=140, justify="left")
        self.active_lbl.pack(fill="x")

    # Shortcuts tab ------------------------------------------------------------------
    def _build_shortcuts(self) -> None:
        t = self.tab_keys.body
        ttk.Label(t, text="Shortcuts work while your game has focus. Double-click one to change it.",
                  style="Muted.TLabel").pack(anchor="w")
        body = ttk.Frame(t)
        body.pack(fill="both", expand=True, pady=10)
        self.key_tree = ttk.Treeview(body, columns=("action", "keys", "kind"), show="headings",
                                     height=11, selectmode="browse")
        for col, text, width in (("action", "Action", 260), ("keys", "Shortcut", 240), ("kind", "Type", 180)):
            self.key_tree.heading(col, text=text)
            self.key_tree.column(col, width=width, anchor="w")
        self.key_tree.pack(side="left", fill="both", expand=True)
        self.key_tree.bind("<Double-1>", lambda e: self.change_shortcut())
        side = ttk.Frame(body)
        side.pack(side="left", fill="y", padx=(12, 0))
        ttk.Button(side, text="Change…", style="Accent.TButton", command=self.change_shortcut).pack(fill="x")
        ttk.Button(side, text="Clear", command=self.clear_shortcut).pack(fill="x", pady=8)
        ttk.Button(side, text="Default", command=self.default_shortcut).pack(fill="x")
        self.sounds_var = tk.BooleanVar(value=self.cf.config.hotkeys.sounds)
        ttk.Checkbutton(t, text="Beep to confirm shortcuts (useful in fullscreen games)",
                        variable=self.sounds_var, style="Switch.TCheckbutton",
                        command=lambda: self.save("hotkeys", {"sounds": self.sounds_var.get()})).pack(anchor="w")
        wrapping(ttk.Label(t, text="Keyboard shortcuts are captured so the game doesn't also see them. Mouse "
                           "and joystick buttons, and hold actions, pass through to the game too.",
                           style="Muted.TLabel", justify="left")).pack(anchor="w", fill="x", pady=(8, 0))
        self.refresh_shortcuts()

    # Settings tab -----------------------------------------------------------------
    def _build_settings(self) -> None:
        t = self.tab_settings.body
        t.columnconfigure(0, weight=1)
        t.columnconfigure(1, weight=1)
        c = self.cf.config

        self.settings_note = ttk.Label(t, text="", style="Muted.TLabel")
        self.settings_note.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        box = (12, 10)  # padding of this tab's boxes: a little tighter, so both columns fit
        # Two independent columns, so the tall Tracking box doesn't push the
        # left column's boxes down.
        left, right = ttk.Frame(t), ttk.Frame(t)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        right.grid(row=1, column=1, sticky="nsew")
        out = ttk.LabelFrame(left, text="Send head movement to", padding=box)
        out.pack(fill="x")
        ttk.Label(out, text="TrackIR / FreeTrack games").grid(row=0, column=0, columnspan=4, sticky="w")
        self.game_out_var = tk.StringVar(value=c.output.game)
        for r, (value, text) in enumerate(GAME_OUTPUT_CHOICES, start=1):
            ttk.Radiobutton(out, text=text, value=value, variable=self.game_out_var).grid(
                row=r, column=0, columnspan=4, sticky="w", padx=(8, 0), pady=1)
        ttk.Label(out, text="opentrack address").grid(row=4, column=0, sticky="w", pady=4)
        self.ot_host = tk.StringVar(value=c.output.opentrack_host)
        ttk.Entry(out, textvariable=self.ot_host, width=16).grid(row=4, column=1, sticky="w", padx=6)
        ttk.Label(out, text="Port").grid(row=4, column=2, sticky="w")
        self.ot_port = tk.StringVar(value=str(c.output.opentrack_port))
        ttk.Entry(out, textvariable=self.ot_port, width=7).grid(row=4, column=3, sticky="w", padx=6)
        self.mouse_var = tk.BooleanVar(value=c.output.mouse)
        ttk.Checkbutton(out, text="Mouse-look (games without head tracking)", variable=self.mouse_var,
                        style="Switch.TCheckbutton").grid(row=5, column=0, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Label(out, text="Pixels per degree").grid(row=6, column=0, columnspan=2, sticky="w", pady=4)
        self.mouse_ppd = tk.StringVar(value=str(c.output.mouse_pixels_per_degree))
        ttk.Entry(out, textvariable=self.mouse_ppd, width=7).grid(row=6, column=2, sticky="w")
        ttk.Button(out, text="Save", style="Accent.TButton", command=self.save_outputs).grid(
            row=6, column=3, sticky="e", padx=(6, 0))

        trk = ttk.LabelFrame(right, text="Tracking", padding=box)
        trk.pack(fill="x")
        self.gaze_cfg_var = tk.BooleanVar(value=c.gaze.enabled)
        ttk.Checkbutton(trk, text="Eye-assist (look toward screen edges to turn further)",
                        variable=self.gaze_cfg_var, style="Switch.TCheckbutton",
                        command=lambda: self.save("gaze", {"enabled": self.gaze_cfg_var.get()})).pack(anchor="w")
        self.dot_cfg_var = tk.BooleanVar(value=c.overlay.enabled)
        ttk.Checkbutton(trk, text="Gaze dot (show roughly where you're looking)",
                        variable=self.dot_cfg_var, style="Switch.TCheckbutton",
                        command=lambda: self.save("overlay", {"enabled": self.dot_cfg_var.get()})).pack(
            anchor="w", pady=(8, 0))
        self.autostart_var = tk.BooleanVar(value=c.ui.autostart)
        ttk.Checkbutton(trk, text="Start tracking when OpenITrack opens", variable=self.autostart_var,
                        style="Switch.TCheckbutton",
                        command=lambda: self.save("ui", {"autostart": self.autostart_var.get()})).pack(
            anchor="w", pady=(8, 0))

        def save_camera(values: dict) -> None:
            if not self.save("source", values, restart_note=True):
                self._show_camera_settings()  # not saved: show what's in effect again

        src = self.cf.base.source
        self.fps_var = self._choice_row(trk, "Webcam tracking rate", FPS_CHOICES, src.fps,
                                        lambda fps: save_camera({"fps": fps}))
        self.resolution_var = self._choice_row(
            trk, "Webcam resolution", RESOLUTION_CHOICES, (src.width, src.height),
            lambda size: save_camera({"width": size[0], "height": size[1]}))
        self.format_var = self._choice_row(trk, "Webcam picture format", FORMAT_CHOICES, src.format,
                                           lambda fmt: save_camera({"format": fmt}))
        # A machine-wide CPU choice, so it goes to config.toml even when a profile is in use.
        self.blendshapes_var = tk.BooleanVar(value=self.cf.base.tracking.blendshapes)
        ttk.Checkbutton(trk, text="Precise blink detection: eye-assist ignores\nblinks more reliably "
                        "(off = less CPU)",
                        variable=self.blendshapes_var, style="Switch.TCheckbutton",
                        command=lambda: self.save("tracking", {"blendshapes": self.blendshapes_var.get()},
                                                  restart_note=True, to_profile=False)).pack(anchor="w", pady=(8, 0))
        ttk.Label(trk, text="Webcam settings apply the next time tracking starts.",
                  style="Muted.TLabel").pack(anchor="w", pady=(6, 0))

        cal = ttk.LabelFrame(left, text="Calibration", padding=box)
        cal.pack(fill="x", pady=(10, 0))
        self.cal_status = wrapping(ttk.Label(cal, text="", justify="left"))
        self.cal_status.pack(anchor="w", fill="x")
        row = ttk.Frame(cal)
        row.pack(anchor="w", pady=(10, 0))
        self.btn_cal_now = ttk.Button(row, text="Calibrate now", command=lambda: self.send("calibrate"))
        self.btn_cal_now.pack(side="left")
        ttk.Button(row, text="Reset calibration", command=self.reset_calibration).pack(side="left", padx=8)

        # Files and starting over share a box: the delete dialog lists exactly what goes.
        data = ttk.LabelFrame(left, text="Your data", padding=box)
        data.pack(fill="x", pady=(10, 0))
        row = ttk.Frame(data)
        row.pack(anchor="w")
        ttk.Button(row, text="Open config file", command=lambda: _open(self.config_path)).pack(side="left")
        ttk.Button(row, text="Open data folder", command=lambda: _open(self.config_dir)).pack(side="left", padx=8)
        wrapping(ttk.Label(data, text="Every setting is in the config file, with explanations.",
                           style="Muted.TLabel", justify="left")).pack(anchor="w", fill="x", pady=(6, 10))
        row = ttk.Frame(data)
        row.pack(anchor="w")
        ttk.Button(row, text="Run setup again", command=self.run_setup).pack(side="left")
        ttk.Button(row, text="Delete all my data…", command=self.delete_all_data).pack(side="left", padx=8)
        ttk.Label(right, text=f"OpenITrack {__version__}", style="Muted.TLabel").pack(anchor="w", pady=(10, 0))
        self._update_calibration_status()
        self._update_settings_note()

    def _build_log(self) -> None:
        frame = ttk.Frame(self.tab_log)
        frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(frame, height=10, wrap="word", state="disabled", relief="flat",
                                font=("Consolas", 9), padx=8, pady=8)
        scroll = ttk.Scrollbar(frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")

    # ---- engine lifecycle -------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self.engine_thread is not None and self.engine_thread.is_alive()

    def toggle_running(self) -> None:
        if self.running:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        if self.running:
            return
        from .engine import Engine  # pulls in numpy and the tracking stack

        try:
            cf = ConfigFile(self.config_path)
        except ConfigError as exc:
            messagebox.showerror("OpenITrack", f"The config file has an error:\n\n{exc}")
            return
        self.engine_error = None
        self.engine = Engine(cf, console_pairing=False)
        engine = self.engine

        def run() -> None:
            try:
                engine.run()
            except Exception as exc:  # shown in the window
                log.exception("Tracking stopped with an error")
                self.engine_error = str(exc)

        self.engine_thread = threading.Thread(target=run, daemon=True, name="tracking")
        self.engine_thread.start()
        log.info("Starting %s tracking", cf.config.source.type)

    def stop(self) -> None:
        if self.engine is not None:
            self.engine.stop()
            log.info("Stopping tracking")

    def send(self, action: str, sync_pause: bool = False) -> None:
        if sync_pause:
            self._sync_pause_until = time.monotonic() + 0.6
        if self.engine is not None and self.running:
            self.engine.command(action)

    def start_phone_mode(self) -> None:
        self.source_var.set("phone")
        self.on_source_change()
        if self.running:
            if self.engine is not None and self.engine.cfg.source.type == "phone":
                return
            self.stop()
            self._restart_when_stopped()
        else:
            self.start()

    def _restart_when_stopped(self) -> None:
        if self.running:
            self.root.after(150, self._restart_when_stopped)
        else:
            self.start()

    # ---- settings actions ---------------------------------------------------------
    # ---- setup guide and starting over ----------------------------------------------
    def run_setup(self) -> None:
        if self.wizard is not None and self.wizard.frame.winfo_exists():
            return
        self.wizard = SetupWizard(self, on_done=self._setup_finished)

    def _setup_finished(self) -> None:
        self.wizard = None
        c = self.cf.config
        self.source_var.set(c.source.type)
        self._show_configured_camera()
        self.game_out_var.set(c.output.game)
        self.mouse_var.set(c.output.mouse)
        self.autostart_var.set(c.ui.autostart)
        self.blendshapes_var.set(self.cf.base.tracking.blendshapes)
        self._show_camera_settings()
        self.refresh_devices()
        self.refresh_shortcuts()
        self._update_calibration_status()
        self.nb.select(self.tab_track)
        log.info("Setup finished")

    def delete_all_data(self) -> None:
        top = tk.Toplevel(self.root)
        top.title("Delete all my data")
        top.transient(self.root)
        top.resizable(False, False)
        frame = ttk.Frame(top, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Delete everything OpenITrack has saved?", font=self.f_h).pack(anchor="w")
        items = saved_items(self.config_dir)
        listing = "\n".join(f"•  {p.name}" for p in items) or "(nothing saved yet)"
        ttk.Label(frame, text=f"From {self.config_dir}:\n\n{listing}", justify="left",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 8))
        ttk.Label(frame, text="Paired phones will need to pair again. This can't be undone.",
                  justify="left").pack(anchor="w")
        downloads = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Also delete downloaded tracker files (about 30 MB, fetched again when needed)",
                        variable=downloads).pack(anchor="w", pady=(10, 0))
        btns = ttk.Frame(frame)
        btns.pack(fill="x", pady=(16, 0))

        def confirm():
            top.destroy()
            self._reset_after_stop(downloads.get())
        ttk.Button(btns, text="Delete everything", style="Accent.TButton", command=confirm).pack(side="right")
        ttk.Button(btns, text="Cancel", command=top.destroy).pack(side="right", padx=8)
        top.grab_set()
        self.reset_dialog = top  # for tests

    def _reset_after_stop(self, include_downloads: bool) -> None:
        if self.running:
            self.stop()
            self.root.after(150, lambda: self._reset_after_stop(include_downloads))
            return
        report = delete_user_data(self.config_dir, include_downloads)
        _add_file_log(self.config_dir)
        log.info("Deleted %d saved item(s)", len(report.removed))
        if report.failed:
            messagebox.showwarning("OpenITrack", "Some files couldn't be deleted:\n\n" + "\n".join(
                f"{p.name}: {why}" for p, why in report.failed))
        # Rebuild the window from fresh defaults; setup starts again by itself.
        self._closing = True
        for child in self.root.winfo_children():
            child.destroy()
        self.root.successor = App(self.root, self.config_path, self.log_queue)

    def save(self, section: str, values: dict, restart_note: bool = False, to_profile: bool = True) -> bool:
        """Save settings: to the profile in use for the sections a profile can
        change (unless `to_profile` is False), otherwise to config.toml."""
        profile = self.cf.profile
        try:
            if to_profile and profile and section.split(".")[0] in PROFILE_SECTIONS:
                self.cf.profiles.set_values(profile, section, values, check=self.cf.check_profile)
            else:
                set_values(self.config_path, section, values)
            self._load_config(profile)
        except (ConfigError, OSError) as exc:
            messagebox.showerror("OpenITrack", str(exc))
            return False
        if restart_note and self.running:
            log.info("Saved. This takes effect the next time tracking starts.")
        return True

    def on_source_change(self) -> None:
        self.save("source", {"type": self.source_var.get()})
        self._update_source_hint()

    def on_camera_change(self) -> None:
        text = self.camera_var.get().strip()
        if not text:
            return
        if text.startswith("Camera "):
            value: int | str = int(text.split()[1])
        elif text.isdigit():
            value = int(text)
        else:
            value = text  # IP-camera URL
        self.save("source", {"camera": value}, restart_note=True)

    def save_outputs(self) -> None:
        try:
            port = int(self.ot_port.get())
            ppd = float(self.mouse_ppd.get())
        except ValueError:
            messagebox.showerror("OpenITrack", "Port must be a whole number and pixels per degree a number.")
            return
        if self.save("output", {"game": self.game_out_var.get(), "opentrack_host": self.ot_host.get().strip(),
                                "opentrack_port": port, "mouse": self.mouse_var.get(),
                                "mouse_pixels_per_degree": ppd}):
            log.info("Output settings saved")

    def on_sensitivity(self) -> None:
        v = round(float(self.sens_var.get()), 2)
        self.sens_lbl.configure(text=f"×{v:.2f}")
        self._sync_pause_until = time.monotonic() + 0.6
        self.send(f"set_sensitivity:{v}")

    def reset_calibration(self) -> None:
        path = self.config_dir / CALIBRATION_FILE
        if not path.exists():
            return
        if messagebox.askyesno("OpenITrack", "Delete the saved calibration? You can calibrate again any time."):
            path.unlink(missing_ok=True)
            log.info("Calibration reset (takes effect the next time tracking starts)")
            self._update_calibration_status()

    def _update_calibration_status(self, created: str | None = None) -> None:
        path = self.config_dir / CALIBRATION_FILE
        if created is None and path.exists():
            try:
                created = json.loads(path.read_text(encoding="utf-8")).get("created") or "yes"
            except (OSError, ValueError):
                created = None
        when = created.replace("T", " ") if created else None
        text = (f"Calibrated {when}." if when else
                "Not calibrated yet. Calibrating takes about 40 seconds and fits eye-assist, "
                "head range and the gaze dot to you.")
        set_options(self.cal_status, text=text)
        set_options(self.calib_lbl, text=f"Calibrated {when[:16]}" if when else "Not calibrated yet")

    def _choice_row(self, parent, label: str, choices: list[tuple], current, on_pick) -> tk.StringVar:
        """A label with a read-only dropdown of described choices; `on_pick` gets the config value."""
        ttk.Label(parent, text=label).pack(anchor="w", pady=(8, 0))
        texts = [text for _, text in choices]
        var = tk.StringVar(value=choice_text(choices, current))
        box = ttk.Combobox(parent, textvariable=var, values=texts, state="readonly", width=CHOICE_WIDTH)
        box.pack(anchor="w", fill="x", pady=(2, 0))
        box.bind("<<ComboboxSelected>>", lambda e: on_pick(choices[texts.index(var.get())][0]))
        return var

    def _show_camera_settings(self) -> None:
        src = self.cf.base.source
        self.fps_var.set(choice_text(FPS_CHOICES, src.fps))
        self.resolution_var.set(choice_text(RESOLUTION_CHOICES, (src.width, src.height)))
        self.format_var.set(choice_text(FORMAT_CHOICES, src.format))

    def _update_source_hint(self) -> None:
        phone = self.source_var.get() == "phone"
        set_options(self.camera_box, state="disabled" if phone or self.running else "normal")
        set_options(self.cam_refresh, state="disabled" if phone or self.running else "normal")
        state = "disabled" if self.running else "normal"
        set_options(self.rb_webcam, state=state)
        set_options(self.rb_phone, state=state)
        if self.running:
            set_options(self.src_hint, text="Stop to change")
        elif phone:
            set_options(self.src_hint, text="Set up in the Phone tab")
        else:
            set_options(self.src_hint, text="")

    def _update_keys_hint(self) -> None:
        hk = self.cf.config.hotkeys
        names = {"recenter": "recenter", "pause": "pause", "toggle_gaze": "eye-assist"}
        parts = [f"{pretty_binding(getattr(hk, a), '+')} {n}" for a, n in names.items() if getattr(hk, a)]
        self.keys_hint.configure(text="In game:  " + "  ·  ".join(parts) + "    (Shortcuts tab to change)")

    # ---- profiles ------------------------------------------------------------------
    def _load_config(self, profile: str | None = None) -> None:
        """Re-read config.toml with `profile` on top (None: the one config.toml picks).
        A profile that's gone or broken falls back to Default."""
        try:
            self.cf = ConfigFile(self.config_path, profile)
        except ConfigError as exc:
            if not profile:
                raise
            log.warning("%s", exc)
            self.cf = ConfigFile(self.config_path, "")

    def _show_profile(self, name: str) -> None:
        """Show this profile's settings in the window, and save window changes to it."""
        try:
            self._load_config(name)
        except ConfigError as exc:
            log.error("%s", exc)
            return
        self.profile_var.set(self.cf.profile or DEFAULT_PROFILE)
        c = self.cf.config
        self.game_out_var.set(c.output.game)
        self.ot_host.set(c.output.opentrack_host)
        self.ot_port.set(str(c.output.opentrack_port))
        self.mouse_var.set(c.output.mouse)
        self.mouse_ppd.set(str(c.output.mouse_pixels_per_degree))
        self.gaze_cfg_var.set(c.gaze.enabled)
        self.dot_cfg_var.set(c.overlay.enabled)
        self.blendshapes_var.set(self.cf.base.tracking.blendshapes)
        self._update_settings_note()
        self.refresh_profiles()

    def _update_settings_note(self) -> None:
        if self.cf.profile:
            text = (f"Outputs and eye-assist are saved to the profile “{self.cf.profile}”. "
                    "Everything else applies to every profile.")
        else:
            text = "Using Default. Outputs and eye-assist can also be set per game: see the Profiles tab."
        self.settings_note.configure(text=text)

    def select_profile(self, name: str) -> None:
        """Use a profile now, and when tracking next starts."""
        if self.save("profiles", {"active": name}):
            self.send(f"use_profile:{name}")
            self._show_profile(name)
            log.info("Profile: %s", name or DEFAULT_PROFILE)

    def _selected_profile(self) -> str | None:
        """Selected row of the Profiles list: a name, "" for Default, or None."""
        sel = self.prof_tree.selection()
        if not sel:
            return None
        return "" if sel[0] == DEFAULT_IID else sel[0]

    def refresh_profiles(self) -> None:
        tree = self.prof_tree
        keep = self._selected_profile()
        tree.delete(*tree.get_children())
        self._profiles = {p.name: p for p in self.cf.profiles.list()}
        current = self.cf.profile
        in_use = "   · in use"
        tree.insert("", "end", iid=DEFAULT_IID, values=(DEFAULT_PROFILE + ("" if current else in_use),
                                                        "anything without its own profile"))
        for name, prof in self._profiles.items():
            tree.insert("", "end", iid=name, values=(name + (in_use if name == current else ""),
                                                     ", ".join(prof.apps) or "— (pick it by hand)"))
        self.profile_box.configure(values=[DEFAULT_PROFILE, *self._profiles])
        if keep is None or (keep and keep not in self._profiles):
            keep = current
        tree.selection_set(keep or DEFAULT_IID)
        self._on_profile_pick()

    def _on_profile_pick(self) -> None:
        name = self._selected_profile()
        prof = self._profiles.get(name) if name else None
        state = "normal" if prof else "disabled"
        for b in (self.btn_prof_dup, self.btn_prof_ren, self.btn_prof_del, self.btn_app_add, self.btn_app_rm):
            b.configure(state=state)
        self.app_box.configure(state=state)
        self.btn_prof_use.configure(state="normal" if name is not None else "disabled")
        self.apps_tree.delete(*self.apps_tree.get_children())
        if prof is None:
            self.apps_hint.configure(text="Default is used for every program that has no profile of its own.")
            return
        for i, app in enumerate(prof.apps):
            self.apps_tree.insert("", "end", iid=str(i), values=(app,))
        self.apps_hint.configure(text="Pick a running program from the list (start the game first), "
                                      "or type its file name, e.g. DCS.exe.")

    def _ask_profile_name(self, title: str, initial: str = "") -> str | None:
        name = simpledialog.askstring(title, "Profile name (e.g. the game's name):", parent=self.root,
                                      initialvalue=initial)
        return name.strip() if name and name.strip() else None

    def _profile_error(self, exc: Exception) -> None:
        messagebox.showerror("OpenITrack", str(exc))

    def use_selected_profile(self) -> None:
        name = self._selected_profile()
        if name is not None:
            self.select_profile(name)

    def new_profile(self) -> None:
        name = self._ask_profile_name("New profile")
        if not name:
            return
        try:
            name = self.cf.profiles.create(name, self.cf.base)
        except (ConfigError, OSError) as exc:
            self._profile_error(exc)
            return
        log.info("Made profile %s", name)
        self.refresh_profiles()
        self.prof_tree.selection_set(name)

    def duplicate_profile(self) -> None:
        src = self._selected_profile()
        if not src:
            return
        name = self._ask_profile_name("Duplicate profile", f"{src} copy")
        if not name:
            return
        try:
            name = self.cf.profiles.create(name, self.cf.base, copy_of=src)
        except (ConfigError, OSError) as exc:
            self._profile_error(exc)
            return
        log.info("Made profile %s from %s", name, src)
        self.refresh_profiles()
        self.prof_tree.selection_set(name)

    def rename_profile(self) -> None:
        old = self._selected_profile()
        if not old:
            return
        new = self._ask_profile_name("Rename profile", old)
        if not new or new == old:
            return
        try:
            new = self.cf.profiles.rename(old, new)
        except (ConfigError, OSError) as exc:
            self._profile_error(exc)
            return
        log.info("Renamed profile %s to %s", old, new)
        self._profile_moved(old, new)
        self.prof_tree.selection_set(new)

    def delete_profile(self) -> None:
        name = self._selected_profile()
        if not name or not messagebox.askyesno("OpenITrack", f"Delete the profile “{name}”? Its file is removed."):
            return
        try:
            self.cf.profiles.delete(name)
        except (ConfigError, OSError) as exc:
            self._profile_error(exc)
            return
        log.info("Deleted profile %s", name)
        self._profile_moved(name, "")

    def _profile_moved(self, old: str, new: str) -> None:
        """Keep the window, the tracker and config.toml on a renamed profile, or
        on Default when `new` is "" (deleted)."""
        if self.cf.profile.lower() == old.lower():
            self.send(f"use_profile:{new}")
            self._show_profile(new)
        if self.cf.base.profiles.active.lower() == old.lower():
            self.save("profiles", {"active": new})
        self.refresh_profiles()

    def _fill_running_apps(self) -> None:
        try:
            apps = running_apps()
        except OSError:
            apps = []
        self.app_box.configure(values=apps or ["(start the game, then open this list)"])

    def _set_apps(self, name: str, apps: list[str]) -> bool:
        try:
            self.cf.profiles.set_apps(name, apps)
        except (ConfigError, OSError) as exc:
            self._profile_error(exc)
            return False
        return True

    def add_app(self) -> None:
        name = self._selected_profile()
        app = self.app_var.get().strip()
        prof = self._profiles.get(name) if name else None
        if prof is None or not app or app.startswith("("):
            return
        if "." not in app.replace("/", "\\").split("\\")[-1]:
            app += ".exe"
        if any(app_key(a) == app_key(app) for a in prof.apps):
            self.app_var.set("")
            return
        owner = self.cf.profiles.find_for_app(app)
        if owner and owner != name:
            if not messagebox.askyesno("OpenITrack", f"{app} already switches to “{owner}”. "
                                                     f"Switch it to “{name}” instead?"):
                return
            if not self._set_apps(owner, [a for a in self._profiles[owner].apps if app_key(a) != app_key(app)]):
                return
        if self._set_apps(name, [*prof.apps, app]):
            log.info("%s now switches to profile %s", app, name)
            self.app_var.set("")
        self.refresh_profiles()

    def remove_app(self) -> None:
        name = self._selected_profile()
        prof = self._profiles.get(name) if name else None
        sel = self.apps_tree.selection()
        if prof is None or not sel:
            return
        index = int(sel[0])
        self._set_apps(name, [a for i, a in enumerate(prof.apps) if i != index])
        self.refresh_profiles()

    def edit_profile_file(self) -> None:
        name = self._selected_profile()
        _open(self.cf.profiles.path(name) if name else self.config_path)

    # ---- cameras ---------------------------------------------------------------------
    def _show_configured_camera(self) -> None:
        cur = self.cf.config.source.camera
        self.camera_var.set(f"Camera {cur}" if isinstance(cur, int) else str(cur))

    def refresh_cameras(self) -> None:
        if self.running:
            return
        self.camera_box.configure(values=["Searching…"])
        self.cam_refresh.configure(text="Searching…")
        self._show_configured_camera()

        def work() -> None:
            try:
                from .sources.webcam import list_cameras  # pulls in OpenCV

                cams = list_cameras()
            except Exception as exc:
                log.warning("Couldn't list cameras: %s", exc)
                cams = []
            self.ui_queue.put(lambda: self._set_cameras(cams))

        threading.Thread(target=work, daemon=True, name="camera-scan").start()

    def _set_cameras(self, cams) -> None:
        self._cameras = cams
        self.cam_refresh.configure(text="Refresh")
        values = [f"Camera {i}  ({w}×{h})" for i, w, h in cams] or ["No webcam found"]
        self.camera_box.configure(values=values)
        cur = self.cf.config.source.camera
        if isinstance(cur, int):
            match = next((v for v in values if v.startswith(f"Camera {cur} ")), None)
            self.camera_var.set(match or f"Camera {cur}")

    # ---- phone -------------------------------------------------------------------------
    def _normalize_code(self) -> None:
        v = self.code_var.get()
        clean = "".join(ch for ch in v.upper() if ch.isalnum())[:6]
        if clean != v:
            self.code_var.set(clean)

    def pair(self) -> None:
        code = self.code_var.get().strip()
        if len(code) != 6:
            self.pair_result.configure(text="Type all 6 characters shown on the phone.", foreground=COLORS["warn"])
            return
        src = getattr(self.engine, "source", None)
        if not self.running or not hasattr(src, "submit_code"):
            self.pair_result.configure(text="Start phone mode first.", foreground=COLORS["warn"])
            return
        self.pair_btn.configure(state="disabled")
        self.pair_result.configure(text="Checking with the phone…", foreground=COLORS["idle"])

        def work() -> None:
            msg = src.submit_code(code)
            self.ui_queue.put(lambda: self._paired(msg))

        threading.Thread(target=work, daemon=True, name="pairing").start()

    def _paired(self, msg: str) -> None:
        ok = msg.startswith("Paired")
        self.pair_btn.configure(state="normal")
        self.pair_result.configure(text=msg, foreground=COLORS["ok"] if ok else COLORS["bad"])
        log.info("%s", msg)
        if ok:
            self.code_var.set("")
            self.refresh_devices()

    def refresh_devices(self) -> None:
        tree = self.dev_tree
        tree.delete(*tree.get_children())
        for d in self.devices.list():
            tree.insert("", "end", iid=d.id, values=(d.name, d.id, d.paired.replace("T", " "),
                                                      (d.last_seen or "never").replace("T", " ")))

    def revoke_selected(self) -> None:
        sel = self.dev_tree.selection()
        if not sel:
            return
        name = self.dev_tree.item(sel[0], "values")[0]
        if messagebox.askyesno("OpenITrack", f"Revoke '{name}'? It will need a new pairing code to connect."):
            self.devices.revoke(sel[0])
            log.info("Revoked %s", name)
            self.refresh_devices()

    def copy_url(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.url_var.get())

    def _show_qr(self, url: str) -> None:
        if url == self._qr_url:
            return
        self._qr_url = url
        try:
            import segno  # only needed once a phone link is shown

            buf = io.BytesIO()
            segno.make(url, error="m").save(buf, kind="png", scale=5, border=2, dark="#000000", light="#ffffff")
            self._qr_img = tk.PhotoImage(data=base64.b64encode(buf.getvalue()))
            self.qr_lbl.configure(image=self._qr_img)
        except Exception as exc:
            log.debug("QR code unavailable: %s", exc)

    # ---- shortcuts ------------------------------------------------------------------------
    def refresh_shortcuts(self) -> None:
        tree = self.key_tree
        tree.delete(*tree.get_children())
        hk = self.cf.config.hotkeys
        for action, label in ACTIONS:
            spec = getattr(hk, action)
            if not spec:
                kind = ""
            elif action in HOLD_ACTIONS:
                kind = "hold · passes through"
            else:
                try:
                    b = parse_binding(spec)
                    kind = "press · captured" if b.kind == "key" else f"{b.kind} button · passes through"
                except ValueError:
                    kind = "invalid"
            tree.insert("", "end", iid=action, values=(label, pretty_binding(spec), kind))
        self._update_keys_hint()

    def _selected_action(self) -> str | None:
        sel = self.key_tree.selection()
        return sel[0] if sel else None

    def change_shortcut(self) -> None:
        action = self._selected_action()
        if action:
            ShortcutDialog(self, action)

    def clear_shortcut(self) -> None:
        action = self._selected_action()
        if action and self.save("hotkeys", {action: ""}):
            self.refresh_shortcuts()

    def default_shortcut(self) -> None:
        action = self._selected_action()
        if action and self.save("hotkeys", {action: getattr(HotkeyConfig(), action)}):
            self.refresh_shortcuts()

    # ---- periodic refresh --------------------------------------------------------------
    def tick(self) -> None:
        if self._closing:
            return
        while True:
            try:
                self.ui_queue.get_nowait()()
            except queue.Empty:
                break
        self._drain_log()
        self._render_state()
        self.root.after(self.TICK_MS, self.tick)

    def _drain_log(self) -> None:
        lines = []
        while True:
            try:
                lines.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        t = self.log_text
        t.configure(state="normal")
        t.insert("end", "\n".join(lines) + "\n")
        excess = int(t.index("end-1c").split(".")[0]) - 1000
        if excess > 0:
            t.delete("1.0", f"{excess}.0")
        t.see("end")
        t.configure(state="disabled")

    def _status(self, text: str, sub: str, color: str) -> None:
        set_options(self.status_lbl, text=text)
        set_options(self.status_sub, text=sub)
        set_item_options(self.status_dot, self._dot, fill=COLORS[color])

    def _render_state(self) -> None:
        running = self.running
        snap = self.engine.snapshot if self.engine is not None else {}
        live = running and snap.get("running")
        set_options(self.start_btn, text="Stop tracking" if running else "Start tracking")
        self._update_source_hint()
        for b in (self.btn_recenter, self.btn_pause, self.btn_calibrate, self.btn_cal_now):
            set_options(b, state="normal" if live else "disabled")
        set_options(self.btn_preview, state="normal" if live and snap.get("mode") == "webcam" else "disabled")

        if not running:
            if self.engine_error:
                self._status("Stopped", self.engine_error[:70], "bad")
            else:
                self._status("Stopped", "Press Start tracking", "idle")
        elif self.engine is not None and self.engine.stopping:
            self._status("Stopping…", "Releasing the camera", "idle")
        elif not live:
            self._status("Starting…", "", "info")
        else:
            phone = snap.get("phone")
            fps = f"{snap.get('fps', 0):.0f} fps"
            if snap.get("calibrating"):
                self._status("Calibrating", "Follow the dots on screen", "info")
            elif phone and phone.get("pending"):
                self._status("Phone wants to pair", "Enter its code in the Phone tab", "info")
            elif snap.get("paused"):
                self._status("Paused", "View held at center", "warn")
            elif phone is not None and not phone.get("active"):
                self._status("Waiting for phone", phone["urls"][0] if phone.get("urls") else "", "warn")
            elif not snap.get("face"):
                self._status("No face", "Look at the camera; check lighting", "warn")
            else:
                held = snap.get("held") or []
                extra = " · frozen" if "freeze_hold" in held else " · centered" if "center_hold" in held else ""
                self._status("Tracking", fps + extra, "ok")

        if live:
            self._render_pose(snap.get("pose", (0,) * 6))
            set_options(self.btn_pause, text="Resume" if snap.get("paused") else "Pause")
            set_options(self.btn_calibrate, text="Cancel calibration" if snap.get("calibrating") else "Calibrate…")
            if time.monotonic() > self._sync_pause_until:
                self.gaze_var.set(bool(snap.get("gaze")))
                self.trans_var.set(bool(snap.get("translation")))
                self.roll_var.set(bool(snap.get("roll")))
                self.dot_var.set(bool(snap.get("overlay")))
                sens = float(snap.get("sensitivity", 1.0))
                if abs(self.sens_var.get() - sens) > 0.005:
                    self.sens_var.set(sens)
                    self.sens_lbl.configure(text=f"×{sens:.2f}")
            if snap.get("calibrated"):
                self._update_calibration_status(snap["calibrated"])
        else:
            self._render_pose((0,) * 6)

        self._render_phone(snap.get("phone") if live else None)
        self._follow_engine_profile(snap.get("profile") if live else None, running)

    def _follow_engine_profile(self, profile: str | None, running: bool) -> None:
        """The tracker switches profiles with the game: show the one it uses, and
        the configured one again once tracking stops."""
        if profile is not None and profile != self._engine_profile:
            self._engine_profile = profile
            if profile != self.cf.profile:
                self._show_profile(profile)
        elif not running and self._engine_profile is not None:
            self._engine_profile = None
            active = self.cf.base.profiles.active
            if active.lower() != self.cf.profile.lower():
                self._show_profile(active)

    def _render_pose(self, pose) -> None:
        pose = tuple(pose)
        if not self._bar_items or pose == self._shown_pose:
            return
        self._shown_pose = pose
        for axis, value in zip(AXIS_LIMITS, pose):
            bar, val, x0, x1, mid, y = self._bar_items[axis]
            frac = max(-1.0, min(1.0, value / AXIS_LIMITS[axis]))
            end = mid + frac * (x1 - x0) / 2
            self.bars.coords(bar, min(mid, end), y - 5, max(mid, end), y + 5)
            self.bars.itemconfigure(val, text=f"{value:+.1f}{AXIS_UNITS[axis]}")
        if hasattr(self, "_view_geom"):
            x0, y0, x1, y1 = self._view_geom
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            yaw, pitch = pose[0], pose[1]
            # Keep the whole dot (radius 7) inside the box at full deflection.
            x = cx - max(-1, min(1, yaw / 180)) * ((x1 - x0) / 2 - 7)  # + yaw = left
            y = cy - max(-1, min(1, pitch / 90)) * ((y1 - y0) / 2 - 7)
            self.view.coords(self._view_dot, x - 7, y - 7, x + 7, y + 7)

    def _render_phone(self, phone: dict | None) -> None:
        if phone is None:
            if self.phone_on.winfo_ismapped():
                self.phone_on.pack_forget()
                self.phone_off.pack(fill="both", expand=True)
            set_options(self.pair_msg, text="No phone is waiting to pair.")
            set_options(self.active_lbl, text="")
            self._last_pending = []
            return
        if not self.phone_on.winfo_ismapped():
            self.phone_off.pack_forget()
            self.phone_on.pack(fill="both", expand=True)
        urls = phone.get("urls") or []
        if urls:
            if self.url_var.get() != urls[0]:
                self.url_var.set(urls[0])
            self._show_qr(urls[0])
            set_options(self.alt_url, text=("If that doesn't open on your phone, use:\n" + "\n".join(urls[1:]))
                                   if len(urls) > 1 else "")
        pending = phone.get("pending") or []
        if pending:
            names = ", ".join(f"{n} ({ip})" for n, ip in pending)
            set_options(self.pair_msg, text=f"Waiting to pair: {names}.\nType the code shown on your phone:")
            if pending != self._last_pending:
                self.nb.select(self.tab_phone)
                self.code_entry.focus_set()
                _flash(self.root)
                self.root.bell()
        else:
            set_options(self.pair_msg, text="No phone is waiting to pair. Open the address on your phone; "
                                         "a new phone shows a 6-character code here to type in.")
        if self._last_pending and not pending:
            self.refresh_devices()
        self._last_pending = pending
        active = phone.get("active")
        set_options(self.active_lbl, text=f"Connected now:\n{active}" if active else "No phone connected")

    # ---- shutdown ---------------------------------------------------------------------------
    def on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.stop()
        self._finish_close(time.monotonic() + 4.0)

    def _finish_close(self, deadline: float) -> None:
        if self.running and time.monotonic() < deadline:
            self.root.after(100, lambda: self._finish_close(deadline))
            return
        self.root.destroy()


class ShortcutDialog:
    """Records a new shortcut: keyboard combo, mouse button 3-5 or joystick button."""

    def __init__(self, app: App, action: str):
        self.app = app
        self.action = action
        self.result: str | None = None
        self.poller = None
        self._prev_joy: dict[int, int] = {}
        self._prev_mouse: dict[int, bool] = {}
        top = self.top = tk.Toplevel(app.root)
        top.title("Change shortcut")
        top.transient(app.root)
        top.resizable(False, False)
        frame = ttk.Frame(top, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=ACTION_LABELS[action], font=app.f_h).pack(anchor="w")
        ttk.Label(frame, text="Press the new shortcut: a key combination (e.g. Ctrl+Shift+F9), "
                  "mouse button 3–5, or a joystick/HOTAS button.  Esc cancels.",
                  style="Muted.TLabel", wraplength=420, justify="left").pack(anchor="w", pady=(4, 12))
        self.shown = ttk.Label(frame, text="Waiting…", font=app.f_big)
        self.shown.pack(anchor="w")
        self.note = ttk.Label(frame, text="", wraplength=420, justify="left")
        self.note.pack(anchor="w", pady=(8, 0))
        btns = ttk.Frame(frame)
        btns.pack(fill="x", pady=(16, 0))
        self.save_btn = ttk.Button(btns, text="Save", style="Accent.TButton", state="disabled", command=self.save)
        self.save_btn.pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.close).pack(side="right", padx=8)
        top.bind("<KeyPress>", self.on_key)
        top.protocol("WM_DELETE_WINDOW", self.close)
        top.grab_set()
        top.focus_force()
        # The current shortcuts must not fire (or swallow keys) while recording.
        app.send("suspend_hotkeys")
        if sys.platform == "win32":
            self.poller = InputPoller()
            self.joys = [j for j in range(MAX_JOYSTICKS) if self.poller.joy_buttons(j) is not None]
            top.after(30, self.poll)

    def _mods(self) -> list[str]:
        if self.poller is None:
            return []
        names = []
        for name, vks in (("ctrl", (0x11,)), ("alt", (0x12,)), ("shift", (0x10,)), ("win", (0x5B, 0x5C))):
            if any(self.poller.vk_down(vk) for vk in vks):
                names.append(name)
        return names

    def on_key(self, event) -> str:
        vk = event.keycode
        if vk == 0x1B:
            self.close()
            return "break"
        if vk in MODIFIER_VKS:
            mods = self._mods()
            self.shown.configure(text=pretty_binding("+".join(mods)) + " + …" if mods else "Waiting…")
            return "break"
        try:
            key = _key_name(vk)
            spec = "+".join(self._mods() + [key])
            parse_binding(spec)
        except (ValueError, OverflowError):
            self.note.configure(text="That key can't be used. Try another.", foreground=COLORS["bad"])
            return "break"
        self.propose(spec)
        return "break"

    def poll(self) -> None:
        if self.poller is None or not self.top.winfo_exists():
            return
        for name, vk in (("mouse3", 0x04), ("mouse4", 0x05), ("mouse5", 0x06)):
            down = self.poller.vk_down(vk)
            if down and not self._prev_mouse.get(vk):
                self.propose("+".join(self._mods() + [name]))
            self._prev_mouse[vk] = down
        for j in self.joys:
            mask = self.poller.joy_buttons(j)
            if mask is None:
                continue
            new = mask & ~self._prev_joy.get(j, mask)
            self._prev_joy[j] = mask
            for bit in range(32):
                if new & (1 << bit):
                    self.propose(f"joy{j + 1}:button{bit + 1}")
        self.top.after(30, self.poll)

    def propose(self, spec: str) -> None:
        self.result = spec
        self.shown.configure(text=pretty_binding(spec))
        hk = self.app.cf.config.hotkeys
        clash = next((a for a, s in hk.bindings().items()
                      if a != self.action and s and s.lower() == spec.lower()), None)
        b = parse_binding(spec)
        if clash:
            self.note.configure(text=f"Already used for “{ACTION_LABELS[clash]}”. Pick another.",
                                foreground=COLORS["bad"])
            self.save_btn.configure(state="disabled")
            return
        if b.kind == "key" and not b.mods:
            self.note.configure(text="A single key can clash with your sim's own controls. "
                                     "A combination like Ctrl+Shift+key is safer.",
                                foreground=COLORS["warn"])
        elif b.kind != "key":
            self.note.configure(text="Mouse and joystick buttons also reach the game, so use one "
                                     "your sim doesn't use.", foreground=COLORS["warn"])
        else:
            self.note.configure(text="", foreground=COLORS["ok"])
        self.save_btn.configure(state="normal")

    def save(self) -> None:
        if self.result and self.app.save("hotkeys", {self.action: self.result}):
            log.info("%s is now %s", ACTION_LABELS[self.action], pretty_binding(self.result))
            self.app.refresh_shortcuts()
        self.close()

    def close(self) -> None:
        self.app.send("resume_hotkeys")
        try:
            self.top.grab_release()
            self.top.destroy()
        except tk.TclError:
            pass


# -- entry point --------------------------------------------------------------------------

def _file_log(config_dir: Path) -> logging.Handler | None:
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(config_dir / "openitrack.log", maxBytes=1_000_000,
                                                  backupCount=2, encoding="utf-8")
    except OSError:
        return None
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    return fh


def _add_file_log(config_dir: Path) -> None:
    """Re-open the log file after a reset deleted it (closed handlers are dropped)."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):
            root.removeHandler(h)
    fh = _file_log(config_dir)
    if fh is not None:
        root.addHandler(fh)


def _setup_logging(config_dir: Path) -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=5000)
    handlers: list[logging.Handler] = [_QueueHandler(q)]
    fh = _file_log(config_dir)
    if fh is not None:
        handlers.append(fh)
    if sys.stderr is not None and sys.stderr.isatty():
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    os.environ.setdefault("GLOG_minloglevel", "2")
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    return q


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openitrack-gui")
    parser.add_argument("-c", "--config", type=Path, default=None)
    args = parser.parse_args(argv)
    config_path = args.config or default_config_path()

    _dpi_aware()
    _taskbar_identity()
    if not _single_instance():
        return 0
    log_queue = _setup_logging(config_path.parent)
    root = tk.Tk()
    try:
        sv_ttk.set_theme("dark")
    except Exception:  # theme is cosmetic
        pass
    try:
        App(root, config_path, log_queue)
    except ConfigError as exc:
        root.withdraw()
        messagebox.showerror("OpenITrack", f"The config file has an error:\n\n{config_path}\n\n{exc}")
        return 2
    log.info("OpenITrack %s", __version__)
    root.mainloop()
    # Tracking has stopped and released the camera by now. Exit at once instead of
    # waiting for helper threads (e.g. a camera scan in progress).
    logging.shutdown()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
