"""First-run setup: a step-by-step guide that covers the main window.

Each step saves what it decides straight to config.toml (so quitting half-way
keeps what was done), and live steps use the real tracker the window runs.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from .monitors import list_monitors
from .scrollarea import ScrollArea

log = logging.getLogger("openitrack.setup")

OK, WARN, BAD, INFO, IDLE = "#3ecf8e", "#f2b84b", "#ef5d5d", "#4c8dff", "#8b95a3"
OPENTRACK_URL = "https://github.com/opentrack/opentrack/releases"
# [output] game choices, as shown here and in the window's Settings tab.
GAME_OUTPUT_CHOICES = (("direct", "Direct (built-in, recommended)"),
                       ("opentrack", "Through opentrack (UDP over network)"),
                       ("off", "Off"))
# Camera picker: this many snapshots at full size in one row; more are shown half size.
CAMERAS_FULL_SIZE = 2
CAMERAS_PER_ROW_SMALL = 3

FEEL_PRESETS = {
    "precise": ("Smooth & precise", "Small head movements are gentle; best for reading instruments.",
                {"exponent": 1.9}, {"head_min_cutoff": 0.5, "head_beta": 0.05}),
    "balanced": ("Balanced", "A good start for most sims (recommended).",
                 {"exponent": 1.6}, {"head_min_cutoff": 0.7, "head_beta": 0.06}),
    "fast": ("Fast", "Quick, direct response; best for racing and combat.",
             {"exponent": 1.25}, {"head_min_cutoff": 1.0, "head_beta": 0.1}),
}


def camera_position_text(yaw: float, pitch: float, z: float) -> tuple[str, list[str]]:
    """Describe where the camera is from the face pose measured while the user
    looks at the screen center. Returns (description, warnings)."""
    # Looking at the screen, the face is turned *away* from the camera: a face
    # turned to the user's left (+yaw) means the camera sits to their right.
    side = "right" if yaw > 0 else "left"
    vert = "below" if pitch > 0 else "above"
    dist = abs(z)
    parts = []
    parts.append("straight ahead" if abs(yaw) < 5 else f"about {abs(yaw):.0f}° to your {side}")
    parts.append("at eye level" if abs(pitch) < 5 else f"about {abs(pitch):.0f}° {vert} your eyes")
    text = f"Your camera is {parts[0]} and {parts[1]}, about {dist:.0f} cm from you."
    warnings = []
    if abs(yaw) > 35 or abs(pitch) > 35:
        warnings.append("That's a steep angle: turns away from the camera may track less reliably. "
                        "If you can, move the camera closer to the middle of your monitor.")
    if dist < 35:
        warnings.append("You're quite close to the camera; leaning in may move your face out of view.")
    elif dist > 120:
        warnings.append("You're far from the camera; tracking gets noisier with distance. "
                        "Around 50–90 cm works best.")
    return text, warnings


def opentrack_running() -> bool:
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq opentrack.exe", "/NH"],
                             capture_output=True, text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "opentrack.exe" in out.lower()


class SetupWizard:
    POLL_MS = 200

    def __init__(self, app, on_done):
        self.app = app
        self.on_done = on_done
        self.root = app.root
        self.cfg = app.cf.config
        self.source = self.cfg.source.type
        self.step = 0
        self.steps = [
            ("Welcome", self.s_welcome),
            ("Camera", self.s_source),
            ("Connect", self.s_connect),
            ("Position", self.s_position),
            ("Monitor", self.s_monitor),
            ("Calibrate", self.s_calibrate),
            ("Feel", self.s_feel),
            ("Your game", self.s_game),
            ("Shortcuts", self.s_shortcuts),
            ("Done", self.s_done),
        ]
        self._poll_job = None
        self._step_state: dict = {}

        self.frame = ttk.Frame(self.root)
        self.frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.frame.lift()
        side = ttk.Frame(self.frame, padding=(18, 20))
        side.pack(side="left", fill="y")
        ttk.Label(side, text="Setup", font=app.f_title).pack(anchor="w", pady=(0, 14))
        self.side_labels = []
        for i, (name, _) in enumerate(self.steps):
            lbl = ttk.Label(side, text=f"  {name}", font=app.f_body)
            lbl.pack(anchor="w", pady=3)
            self.side_labels.append(lbl)
        ttk.Separator(self.frame, orient="vertical").pack(side="left", fill="y")

        main = ttk.Frame(self.frame, padding=(26, 22, 26, 16))
        main.pack(side="left", fill="both", expand=True)
        self.title = ttk.Label(main, text="", font=app.f_title)
        self.title.pack(anchor="w")
        # The page scrolls if it's ever taller than the window; Back/Next stay put.
        self.body_area = ScrollArea(main)
        self.body_area.pack(fill="both", expand=True, pady=(14, 10))
        self.body = self.body_area.body
        nav = ttk.Frame(main)
        nav.pack(fill="x", side="bottom")
        self.skip_all = ttk.Button(nav, text="Skip setup", command=self.skip_setup)
        self.skip_all.pack(side="left")
        self.next_btn = ttk.Button(nav, text="Next", style="Accent.TButton", command=self.next, width=12)
        self.next_btn.pack(side="right")
        self.skip_step = ttk.Button(nav, text="Skip this step", command=self.next)
        self.back_btn = ttk.Button(nav, text="Back", command=self.back)
        self.back_btn.pack(side="right", padx=8)
        self.show(0)

    # ---- navigation ------------------------------------------------------------
    def show(self, index: int) -> None:
        if self._poll_job:
            self.root.after_cancel(self._poll_job)
            self._poll_job = None
        self.step = index
        for i, lbl in enumerate(self.side_labels):
            name = self.steps[i][0]
            if i < index:
                lbl.configure(text=f"✓ {name}", foreground=OK)
            elif i == index:
                lbl.configure(text=f"›  {name}", foreground="#ffffff", font=self.app.f_h)
            else:
                lbl.configure(text=f"   {name}", foreground=IDLE, font=self.app.f_body)
            if i != index:
                lbl.configure(font=self.app.f_body)
        for w in self.body.winfo_children():
            w.destroy()
        self.body_area.scroll_to_top()
        self._step_state = {}
        self.back_btn.configure(state="normal" if index > 0 else "disabled")
        self.skip_step.pack_forget()
        self.next_btn.configure(text="Next", state="normal")
        self.steps[index][1]()

    def next(self) -> None:
        if self.step < len(self.steps) - 1:
            self.show(self.step + 1)

    def back(self) -> None:
        target = self.step - 1
        if target == 4 and not self._multi_monitor():
            target = 3
        if target >= 0:
            self.show(target)

    def allow_next(self, ok: bool, text: str = "Next") -> None:
        self.next_btn.configure(state="normal" if ok else "disabled", text=text)

    def offer_skip(self) -> None:
        self.skip_step.pack(side="right", padx=(0, 8), before=self.back_btn)

    def poll(self, fn) -> None:
        def run():
            if not self.frame.winfo_exists():
                return
            fn()
            self._poll_job = self.root.after(self.POLL_MS, run)
        self._poll_job = self.root.after(self.POLL_MS, run)

    def skip_setup(self) -> None:
        if messagebox.askyesno("OpenITrack", "Skip setup? You can run it again any time from Settings."):
            self.finish()

    def finish(self) -> None:
        if self._poll_job:
            self.root.after_cancel(self._poll_job)
        self.app.save("ui", {"setup_done": True})
        self.frame.destroy()
        self.on_done()

    # ---- helpers ---------------------------------------------------------------
    def snap(self) -> dict:
        e = self.app.engine
        return e.snapshot if e is not None and self.app.running else {}

    def text(self, parent, text, muted=False, **pack) -> ttk.Label:
        lbl = ttk.Label(parent, text=text, justify="left", style="Muted.TLabel" if muted else "TLabel")
        lbl.bind("<Configure>", lambda e: lbl.configure(wraplength=max(200, e.width - 6)))
        lbl.pack(anchor="w", fill="x", **({"pady": (0, 8)} | pack))
        return lbl

    def check_row(self, parent, label: str) -> tuple[tk.Canvas, ttk.Label]:
        row = ttk.Frame(parent)
        row.pack(anchor="w", fill="x", pady=3)
        dot = tk.Canvas(row, width=14, height=14, highlightthickness=0, bd=0,
                        background=ttk.Style().lookup("TFrame", "background"))
        dot.create_oval(2, 2, 12, 12, fill=IDLE, outline="", tags="d")
        dot.pack(side="left", padx=(0, 8))
        ttk.Label(row, text=label, width=18).pack(side="left")
        value = ttk.Label(row, text="…", style="Muted.TLabel")
        value.pack(side="left", fill="x", expand=True)
        return dot, value

    @staticmethod
    def set_check(item, color: str, text: str) -> None:
        dot, value = item
        dot.itemconfigure("d", fill=color)
        value.configure(text=text)

    def ensure_tracking(self) -> None:
        """Run the tracker with the current config (restarting if the source changed)."""
        e = self.app.engine
        if self.app.running and e is not None and e.cfg.source.type == self.source \
                and (self.source == "phone" or e.cfg.source.camera == self.app.cf.config.source.camera):
            return
        if self.app.running:
            self.app.stop()
            self.app._restart_when_stopped()
        else:
            self.app.start()

    def _multi_monitor(self) -> bool:
        try:
            return len(list_monitors()) > 1
        except Exception:
            return False

    # ---- steps -----------------------------------------------------------------
    def s_welcome(self) -> None:
        self.title.configure(text="Welcome to OpenITrack")
        b = self.body
        self.text(b, "OpenITrack turns your webcam, or your phone, into a head and eye tracker for "
                     "flight, racing and space sims. Look around the cockpit by turning your head; "
                     "glance toward the screen edge and the view follows your eyes.")
        self.text(b, "This takes about 3 minutes:")
        for line in ("1.  Pick your camera and check it can see you",
                     "2.  Find where the camera is, and calibrate to your head and eyes",
                     "3.  Choose how it should feel, and connect your game"):
            self.text(b, line, pady=(0, 2))
        self.text(b, "Privacy: video is processed on this PC (or on your phone) and never stored or "
                     "uploaded. Only head angles are sent to your game.", muted=True, pady=(14, 0))
        self.next_btn.configure(text="Let's start")

    def s_source(self) -> None:
        self.title.configure(text="Which camera will you use?")
        b = self.body
        var = tk.StringVar(value=self.source)
        for value, name, desc in (
            ("webcam", "A webcam on this PC",
             "Any USB or laptop webcam. The PC does the tracking (about a quarter of one CPU core)."),
            ("phone", "My phone",
             "Your phone's front camera, over Wi-Fi. The phone does the tracking, so the PC does almost "
             "nothing. Video never leaves the phone; the connection is encrypted and paired with a code."),
        ):
            card = ttk.Frame(b, padding=(4, 8))
            card.pack(fill="x", pady=4)
            ttk.Radiobutton(card, text=name, value=value, variable=var).pack(anchor="w")
            self.text(card, desc, muted=True, padx=(28, 0), pady=(2, 0))

        def save():
            self.source = var.get()
            self.app.save("source", {"type": self.source})
            self.app.source_var.set(self.source)
        var.trace_add("write", lambda *a: save())
        save()

    def s_connect(self) -> None:
        if self.source == "phone":
            self._connect_phone()
        else:
            self._connect_webcam()

    def _connect_webcam(self) -> None:
        self.title.configure(text="Pick your webcam")
        b = self.body
        status = self.text(b, "Looking for cameras… (this takes a few seconds)")
        grid = ttk.Frame(b)
        grid.pack(fill="both", expand=True)
        url_row = ttk.Frame(b)
        url_row.pack(fill="x", pady=(8, 0))
        ttk.Label(url_row, text="Or an IP-camera address:", style="Muted.TLabel").pack(side="left")
        url_var = tk.StringVar(value=self.cfg.source.camera if isinstance(self.cfg.source.camera, str) else "")
        ttk.Entry(url_row, textvariable=url_var, width=36).pack(side="left", padx=8)
        choice = tk.StringVar()
        self.allow_next(False)
        self._images = []

        def chosen(*_):
            v = choice.get()
            if v == "url":
                if url_var.get().strip():
                    self.app.save("source", {"camera": url_var.get().strip()})
                    self.allow_next(True)
            elif v:
                self.app.save("source", {"camera": int(v)})
                self.allow_next(True)
        choice.trace_add("write", chosen)
        url_var.trace_add("write", lambda *a: (choice.set("url") if url_var.get().strip() else None, chosen()))

        def show(cams):
            if not self.frame.winfo_exists() or self.step != 2:
                return
            for w in grid.winfo_children():
                w.destroy()
            if not cams:
                status.configure(text="No webcam found. Check it's plugged in, its privacy shutter is open and "
                                      "Windows allows desktop apps to use the camera (Settings › Privacy › "
                                      "Camera). Then press Search again.", foreground=WARN)
            else:
                status.configure(text="Pick the camera that faces you:", foreground="")
            # Up to two cameras side by side at full size; more get half-size
            # snapshots in rows, so even six fit the window.
            small = len(cams) > CAMERAS_FULL_SIZE
            per_row = CAMERAS_PER_ROW_SMALL if small else CAMERAS_FULL_SIZE
            for n, (i, w, h, png) in enumerate(cams):
                card = ttk.Frame(grid, padding=6)
                card.grid(row=n // per_row, column=n % per_row, padx=(0, 12), sticky="n")
                if png:
                    img = tk.PhotoImage(data=base64.b64encode(png))
                    if small:
                        img = img.subsample(2, 2)
                    self._images.append(img)
                    ttk.Label(card, image=img).pack()
                ttk.Radiobutton(card, text=f"Camera {i}  ({w}×{h})", value=str(i), variable=choice).pack(pady=4)
            current = self.app.cf.config.source.camera
            if cams and isinstance(current, int) and any(c[0] == current for c in cams):
                choice.set(str(current))
            elif cams:
                choice.set(str(cams[0][0]))
            ttk.Button(grid, text="Search again", command=scan).grid(
                row=(len(cams) - 1) // per_row + 1 if cams else 1, column=0, sticky="w", pady=8)

        def scan():
            status.configure(text="Looking for cameras… (this takes a few seconds)", foreground="")

            def work():
                try:
                    from .sources.webcam import probe_cameras  # pulls in OpenCV

                    cams = probe_cameras()
                except Exception as exc:
                    log.warning("Camera scan failed: %s", exc)
                    cams = []
                self.app.ui_queue.put(lambda: show(cams))
            threading.Thread(target=work, daemon=True, name="camera-probe").start()

        # The scan needs the camera, so tracking can't be holding it.
        if self.app.running:
            self.app.stop()

            def wait_then_scan():
                if self.app.running:
                    self.root.after(150, wait_then_scan)
                else:
                    scan()
            wait_then_scan()
        else:
            scan()

    def _connect_phone(self) -> None:
        self.title.configure(text="Connect your phone")
        b = self.body
        self.ensure_tracking()
        top = ttk.Frame(b)
        top.pack(fill="x")
        qr = ttk.Label(top)
        qr.pack(side="left", padx=(0, 18))
        right = ttk.Frame(top)
        right.pack(side="left", fill="both", expand=True)
        self.text(right, "1.  Connect the phone to the same Wi-Fi as this PC.")
        self.text(right, "2.  Scan the code with the phone's camera, or type the address below.")
        url = ttk.Label(right, text="Starting…", font=self.app.f_big)
        url.pack(anchor="w", pady=(0, 6))
        self.text(right, "3.  The first time, the browser warns about the certificate: tap Advanced › "
                         "Proceed. That's expected for a private address on your own network.")
        self.text(right, "4.  The phone shows a 6-character code. Type it here:")
        row = ttk.Frame(right)
        row.pack(anchor="w")
        code = tk.StringVar()
        entry = ttk.Entry(row, textvariable=code, width=8, font=self.app.f_code, justify="center")
        entry.pack(side="left")
        pair_btn = ttk.Button(row, text="Pair", style="Accent.TButton")
        pair_btn.pack(side="left", padx=10)
        result = self.text(right, "", pady=(8, 0))
        alt = self.text(b, "", muted=True, pady=(10, 0))
        self.allow_next(False)
        st = self._step_state

        def norm(*_):
            v = code.get()
            clean = "".join(c for c in v.upper() if c.isalnum())[:6]
            if clean != v:
                code.set(clean)
        code.trace_add("write", norm)

        def pair(*_):
            src = getattr(self.app.engine, "source", None)
            if len(code.get()) != 6 or not hasattr(src, "submit_code"):
                result.configure(text="Type all 6 characters shown on the phone.", foreground=WARN)
                return
            pair_btn.configure(state="disabled")
            result.configure(text="Checking with the phone…", foreground=IDLE)
            value = code.get()

            def work():
                msg = src.submit_code(value)
                self.app.ui_queue.put(lambda: done(msg))
            threading.Thread(target=work, daemon=True).start()

        def done(msg):
            if not self.frame.winfo_exists():
                return
            pair_btn.configure(state="normal")
            ok = msg.startswith("Paired")
            result.configure(text=msg if not ok else "Paired! Now tap “Start tracking” on the phone.",
                             foreground=OK if ok else BAD)
            if ok:
                code.set("")
                self.app.refresh_devices()
        pair_btn.configure(command=pair)
        entry.bind("<Return>", pair)

        def tick():
            phone = self.snap().get("phone") or {}
            urls = phone.get("urls") or []
            if urls and st.get("url") != urls[0]:
                st["url"] = urls[0]
                url.configure(text=urls[0])
                try:
                    import segno  # only needed once a phone link is shown

                    buf = io.BytesIO()
                    segno.make(urls[0], error="m").save(buf, kind="png", scale=5, border=2)
                    st["qr"] = tk.PhotoImage(data=base64.b64encode(buf.getvalue()))
                    qr.configure(image=st["qr"])
                except Exception:
                    pass
                if len(urls) > 1:
                    alt.configure(text="If the address doesn't open on your phone (some older Android phones "
                                       "can't open .local names), use " + " or ".join(urls[1:]))
            if phone.get("pending"):
                entry.focus_set()
            if phone.get("active") and self.snap().get("face"):
                result.configure(text=f"Connected to {phone['active']} and tracking your face.", foreground=OK)
                self.allow_next(True)
            elif phone.get("active"):
                result.configure(text=f"Connected to {phone['active']}. Tap “Start tracking” on the phone "
                                      "and allow the camera.", foreground=OK)
        self.poll(tick)

    def s_position(self) -> None:
        self.title.configure(text="Can the camera see you?")
        b = self.body
        self.ensure_tracking()
        self.text(b, "Sit the way you normally play. You should be lit from the front; a bright window or "
                     "lamp behind you makes tracking worse.")
        checks = ttk.Frame(b)
        checks.pack(fill="x", pady=(4, 10))
        c_face = self.check_row(checks, "Face")
        c_rate = self.check_row(checks, "Tracking rate")
        c_pos = self.check_row(checks, "Camera position")
        self.text(b, "Now look at the middle of the screen you play on, keep still, and press Measure.")
        measure = ttk.Button(b, text="Measure", style="Accent.TButton")
        measure.pack(anchor="w")
        out = self.text(b, "", pady=(10, 0))
        warn = self.text(b, "", pady=(4, 0))
        warn.configure(foreground=WARN)
        self.allow_next(False)
        st = self._step_state
        st["face_since"] = None

        def do_measure():
            st["measured_at"] = time.monotonic()
            self.app.send("recenter")
            out.configure(text="Measuring… keep looking at the middle of the screen.", foreground=IDLE)
        measure.configure(command=do_measure)

        def tick():
            s = self.snap()
            if not s.get("running"):
                self.set_check(c_face, IDLE, "starting the camera…")
                return
            now = time.monotonic()
            if s.get("face"):
                st["face_since"] = st["face_since"] or now
                held = now - st["face_since"]
                self.set_check(c_face, OK if held > 1.0 else INFO, "found" if held > 1.0 else "found…")
            else:
                st["face_since"] = None
                self.set_check(c_face, WARN, "not found — face the camera and check the lighting")
            fps = float(s.get("fps", 0))
            self.set_check(c_rate, OK if fps >= 20 else WARN if fps >= 10 else BAD,
                           f"{fps:.0f} frames/s" + ("" if fps >= 20 else " — more light usually helps"))
            center = s.get("center")
            if st.get("measured_at") and center and now - st["measured_at"] > 0.6 and s.get("centered"):
                text, warnings = camera_position_text(center["yaw"], center["pitch"], center["z"])
                out.configure(text=text, foreground="")
                warn.configure(text="\n".join(warnings))
                self.set_check(c_pos, WARN if warnings else OK, "measured")
                st["done"] = True
            elif not st.get("done"):
                self.set_check(c_pos, IDLE, "press Measure")
            measure.configure(state="normal" if s.get("face") else "disabled")
            self.allow_next(bool(st.get("done")))
        self.poll(tick)

    def s_monitor(self) -> None:
        try:
            monitors = list_monitors()
        except Exception:
            monitors = []
        if len(monitors) <= 1:
            self.app.save("calibration", {"monitor": 0})
            self.root.after(1, self.next)
            return
        self.title.configure(text="Which screen do you play on?")
        b = self.body
        self.text(b, "Calibration shows dots on this screen, and eye-assist is measured against it.")
        current = self.app.cf.config.calibration.monitor
        var = tk.StringVar(value=str(current))
        for i, m in enumerate(monitors, start=1):
            label = f"Monitor {i}  —  {m.width}×{m.height}" + ("  (main)" if getattr(m, "primary", False) else "")
            ttk.Radiobutton(b, text=label, value=str(i), variable=var).pack(anchor="w", pady=2)
        ttk.Radiobutton(b, text="All of them (triple screens / spanned)", value="all",
                        variable=var).pack(anchor="w", pady=(8, 2))
        if str(current) not in [str(i) for i in range(1, len(monitors) + 1)] + ["all"]:
            primary = next((i for i, m in enumerate(monitors, start=1) if getattr(m, "primary", False)), 1)
            var.set(str(primary))

        def save(*_):
            v = var.get()
            self.app.save("calibration", {"monitor": v if v == "all" else int(v)})
        var.trace_add("write", save)
        save()

    def s_calibrate(self) -> None:
        self.title.configure(text="Calibrate to you")
        b = self.body
        self.ensure_tracking()
        self.text(b, "This sets the sensitivity automatically from how far you turn and look. A full-screen "
                     "guide shows dots and arrows; it takes about 30 seconds:")
        for line in ("•  Look at the dot in the center.",
                     "•  Look at dots at the screen edges, moving only your eyes.",
                     "•  Turn your head left, right, up and down as far as is comfortable while you can "
                     "still see the screen."):
            self.text(b, line, pady=(0, 2))
        start = ttk.Button(b, text="Start calibration", style="Accent.TButton",
                           command=lambda: self.app.send("calibrate"))
        start.pack(anchor="w", pady=(12, 0))
        out = self.text(b, "", pady=(12, 0))
        self.allow_next(False)
        self.offer_skip()
        st = self._step_state
        st["before"] = self.snap().get("calibrated")

        def tick():
            s = self.snap()
            start.configure(state="normal" if s.get("running") and not s.get("calibrating") else "disabled")
            if s.get("calibrating"):
                out.configure(text="Calibrating… follow the guide on screen.", foreground=INFO)
            elif s.get("calibrated") and s.get("calibrated") != st["before"]:
                out.configure(text="Done! " + self._calibration_summary(), foreground=OK)
                self.allow_next(True)
        self.poll(tick)

    def _calibration_summary(self) -> str:
        try:
            data = json.loads((self.app.config_dir / "calibration.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        parts = []
        if data.get("head_yaw_range"):
            parts.append(f"a comfortable head turn of ±{data['head_yaw_range']:.0f}° gives a full look around")
        eyes = [d for d in ("left", "right", "up", "down") if data.get(f"gaze_{d}")]
        if eyes:
            parts.append(f"eye range measured in {len(eyes)} of 4 directions")
        return ("Measured: " + "; ".join(parts) + ".") if parts else ""

    def s_feel(self) -> None:
        self.title.configure(text="How should it feel?")
        b = self.body
        self.ensure_tracking()
        self.text(b, "Pick a style, then turn your head and watch the bar. You can change this later.")
        var = tk.StringVar(value="balanced")
        for key, (name, desc, _, _) in FEEL_PRESETS.items():
            row = ttk.Frame(b)
            row.pack(fill="x", pady=2)
            ttk.Radiobutton(row, text=name, value=key, variable=var).pack(side="left")
            ttk.Label(row, text="  " + desc, style="Muted.TLabel").pack(side="left")
        bar = tk.Canvas(b, height=36, highlightthickness=0, bd=0,
                        background=ttk.Style().lookup("TFrame", "background"))
        bar.pack(fill="x", pady=(16, 0))
        lbl = ttk.Label(b, text="Turn your head left and right", style="Muted.TLabel")
        lbl.pack(anchor="w")

        def apply(*_):
            _, _, axis, flt = FEEL_PRESETS[var.get()]
            for section in ("axes.yaw", "axes.pitch"):
                self.app.save(section, axis)
            self.app.save("filter", flt)
        var.trace_add("write", apply)
        apply()

        def tick():
            w = max(bar.winfo_width(), 200)
            yaw = (self.snap().get("pose") or (0,))[0]
            bar.delete("all")
            bar.create_rectangle(0, 12, w, 24, fill="#2b2f36", outline="")
            mid = w / 2
            end = mid - max(-1, min(1, yaw / 180)) * mid
            bar.create_rectangle(min(mid, end), 12, max(mid, end), 24, fill=INFO, outline="")
            bar.create_line(mid, 6, mid, 30, fill="#57606a")
            lbl.configure(text=f"In-game view: {abs(yaw):.0f}° " + ("left" if yaw > 0 else "right"))
        self.poll(tick)

    def s_game(self) -> None:
        self.title.configure(text="Connect your game")
        b = self.body
        self.ensure_tracking()
        self.text(b, "Most sims (MSFS, DCS, X-Plane, IL-2, Elite Dangerous, iRacing, ETS2 and more) support "
                     "TrackIR/FreeTrack head tracking. OpenITrack talks to them directly, no other app needed.")
        choice = tk.StringVar(value=self.app.cf.config.output.game)
        for value, text in GAME_OUTPUT_CHOICES:
            ttk.Radiobutton(b, text=text, value=value, variable=choice).pack(anchor="w", pady=1)

        direct = ttk.Frame(b)
        for line in ("1.  Keep OpenITrack tracking, then start your sim (games look for head tracking "
                     "when they start).",
                     "2.  In your sim, turn on TrackIR / head tracking."):
            self.text(direct, line, pady=(0, 2))
        game = self.check_row(direct, "Game")
        clash = self.check_row(direct, "opentrack")

        via_ot = ttk.Frame(b)
        status = self.check_row(via_ot, "opentrack")
        for line in ("1.  Install opentrack and open it.",
                     "2.  Input: “UDP over network” (port 4242).   Output: “freetrack 2.0 Enhanced”.",
                     "3.  Filter: “Accela” with low smoothing, or none.  Then press Start.",
                     "4.  In your sim, turn on TrackIR / head tracking."):
            self.text(via_ot, line, pady=(0, 2))
        row = ttk.Frame(via_ot)
        row.pack(anchor="w", pady=(10, 0))
        ttk.Button(row, text="Get opentrack", command=lambda: webbrowser.open(OPENTRACK_URL)).pack(side="left")

        mouse = tk.BooleanVar(value=self.app.cf.config.output.mouse)
        mouse_box = ttk.Checkbutton(b, text="My game has no head tracking: move the mouse instead (mouse-look)",
                                    variable=mouse, style="Switch.TCheckbutton",
                                    command=lambda: self.app.save("output", {"mouse": mouse.get()}))
        mouse_box.pack(anchor="w", pady=(14, 0))

        def show(*_):
            value = choice.get()
            if value != self.app.cf.config.output.game:
                self.app.save("output", {"game": value})
            for frame, on in ((direct, value == "direct"), (via_ot, value == "opentrack")):
                if on:
                    frame.pack(anchor="w", fill="x", pady=(10, 0), before=mouse_box)
                else:
                    frame.pack_forget()
        choice.trace_add("write", show)
        show()

        st = self._step_state
        st["next_check"] = 0.0
        st["opentrack"] = False

        def tick():
            name = self.snap().get("game")
            self.set_check(game, OK if name else IDLE, f"{name} is reading head tracking" if name
                           else "start your sim to check (not every game reports in)")
            ot = st["opentrack"]
            if choice.get() == "direct":
                self.set_check(clash, WARN if ot else OK,
                               "running: close it, both would steer the view" if ot else "not needed")
            else:
                self.set_check(status, OK if ot else WARN,
                               "running" if ot else "not running yet (start it before your game)")
            if time.monotonic() < st["next_check"]:
                return
            st["next_check"] = time.monotonic() + 2.0

            def work():
                running = opentrack_running()
                self.app.ui_queue.put(lambda: st.__setitem__("opentrack", running))
            threading.Thread(target=work, daemon=True).start()
        self.poll(tick)

    def s_shortcuts(self) -> None:
        self.title.configure(text="Shortcuts you'll use in game")
        b = self.body
        self.ensure_tracking()
        from .gui import pretty_binding  # gui.py imports this module

        hk = self.app.cf.config.hotkeys
        self.text(b, "These work while your game has focus. Change them any time in the Shortcuts tab.")
        table = ttk.Frame(b)
        table.pack(anchor="w", pady=(4, 12))
        for r, (action, name) in enumerate((("recenter", "Recenter (look at the screen center first)"),
                                            ("pause", "Pause / resume"),
                                            ("toggle_gaze", "Eye-assist on/off"),
                                            ("calibrate", "Calibrate again"))):
            ttk.Label(table, text=pretty_binding(getattr(hk, action)), font=self.app.f_h).grid(
                row=r, column=0, sticky="w", padx=(0, 18), pady=2)
            ttk.Label(table, text=name).grid(row=r, column=1, sticky="w")
        self.text(b, f"Try it now: look at the middle of the screen and press {pretty_binding(hk.recenter)}.")
        tried = self.check_row(b, "Recenter shortcut")
        st = self._step_state
        st["since"] = time.monotonic()
        self.offer_skip()
        self.allow_next(False)

        def tick():
            last = self.snap().get("last_action")
            if last and last[0] == "recenter" and last[1] >= st["since"]:
                self.set_check(tried, OK, "works!")
                self.allow_next(True)
            else:
                self.set_check(tried, IDLE, "waiting for you to press it…")
        self.poll(tick)

    def s_done(self) -> None:
        self.title.configure(text="You're all set")
        b = self.body
        self.text(b, "OpenITrack is tracking now. Start opentrack, then your sim."
                  if self.app.cf.config.output.game == "opentrack" else
                  "OpenITrack is tracking now. Start your sim and turn on its TrackIR / head tracking.")
        self.text(b, "Tips:", pady=(8, 2))
        for line in ("•  Recenter whenever you shift in your seat.",
                     "•  Keep OpenITrack open while you play; it uses very little of your PC.",
                     "•  Settings › Start over runs this setup again or deletes everything OpenITrack saved."):
            self.text(b, line, pady=(0, 2))
        auto = tk.BooleanVar(value=self.app.cf.config.ui.autostart)
        ttk.Checkbutton(b, text="Start tracking automatically when OpenITrack opens", variable=auto,
                        style="Switch.TCheckbutton",
                        command=lambda: self.app.save("ui", {"autostart": auto.get()})).pack(anchor="w", pady=(14, 0))
        self.skip_all.configure(state="disabled")
        self.next_btn.configure(text="Finish", command=self.finish_and_reset)

    def finish_and_reset(self) -> None:
        self.next_btn.configure(command=self.next)
        self.skip_all.configure(state="normal")
        self.finish()
