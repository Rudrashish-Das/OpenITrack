"""Main loop: source -> pose pipeline -> outputs, plus shortcuts, calibration,
live config reload, profile switching, preview and a one-line console status.

The window (gui.py) runs the engine on a background thread: it sends commands
through `Engine.command()` and reads `Engine.snapshot`, a plain dict replaced
about ten times a second."""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from pathlib import Path

from .calibration import Calibration, CalibrationSession
from .config import ConfigError, ConfigFile
from .foreground import foreground_app
from .gaze_overlay import GazeDot
from .hotkeys import HotkeyListener
from .mapping import PosePipeline
from .model import ensure_model
from .monitors import list_monitors, pick, use_physical_pixels
from .outputs import Output, build_outputs
from .sounds import Sounds

log = logging.getLogger(__name__)

CALIBRATION_FILE = "calibration.json"
RESULT_SCREEN_SECONDS = 2.5
SNAPSHOT_INTERVAL = 0.1
SENSITIVITY_LIMITS = (0.3, 3.0)


class Engine:
    def __init__(self, config_file: ConfigFile, preview: bool = False, calibrate: bool = False,
                 console_pairing: bool = True):
        self.cf = config_file
        self.cfg = config_file.config
        self.calibration_path = config_file.path.parent / CALIBRATION_FILE
        self.calibration = Calibration.load(self.calibration_path)
        self.pipeline = PosePipeline(self.cfg, self.calibration)
        self.sounds = Sounds(self.cfg.hotkeys.sounds)
        self.outputs: list[Output] = []
        self.hotkeys: HotkeyListener | None = None
        self.source = None
        self.preview = None
        self._want_preview = preview
        self._calibrate_on_start = calibrate
        self.session: CalibrationSession | None = None
        self.overlay = None
        self.dot: GazeDot | None = None
        self._dot_on = self.cfg.overlay.enabled
        self._session_end: float | None = None
        self._console_pairing = console_pairing
        self._commands: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._hotkeys_suspended = False
        self._front_app: str | None = None
        self.snapshot: dict = {"running": False, "status": "starting", "mode": self.cfg.source.type}

    # -- control from another thread (the window) --------------------------------
    def command(self, action: str) -> None:
        """Queue an action: any shortcut action name, 'set_sensitivity:<x>',
        'use_profile:<name>' ("" = Default), 'suspend_hotkeys' or 'resume_hotkeys'.
        Applied on the tracking thread."""
        self._commands.put(action)

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # -- setup -------------------------------------------------------------
    def _make_source(self, model_path: Path):
        sc = self.cfg.source
        if sc.type == "phone":
            from .sources.phone import PhoneSource  # pulls in cryptography

            src = PhoneSource(sc, model_path, config_dir=self.cf.path.parent)
            src.on_pair_request = self._on_pair_request
            src.start()
            log.info("Phone mode. On your phone (same Wi-Fi), open:")
            log.info("    %s", src.urls[0] if src.urls else "(no network connection found)")
            if len(src.urls) > 1:
                log.info("    (if that name doesn't open, use %s)", " or ".join(src.urls[1:]))
            log.info("The browser warns about the certificate the first time; that's expected "
                     "for a private local link. Choose Advanced > Proceed.")
            paired = src.devices.list()
            if paired:
                log.info("Paired phones: %s (manage with `openitrack devices`)",
                         ", ".join(f"{d.name} [{d.id[:6]}]" for d in paired))
            if self._console_pairing:
                self._start_code_prompt(src)
            return src
        from .sources.webcam import WebcamSource  # pulls in OpenCV

        return WebcamSource(sc, self.cfg.tracking, model_path)

    # -- phone pairing -------------------------------------------------------------
    def _on_pair_request(self, name: str, ip: str) -> None:
        log.info("'%s' at %s wants to pair. Type the 6-character code shown on that "
                 "phone and press Enter (ignore it if this isn't your phone):", name, ip)
        self.sounds.play("step")

    def _start_code_prompt(self, src) -> None:
        """Read pairing codes typed into the console, without blocking tracking."""
        if sys.stdin is None:
            log.warning("No console input: phones can't be paired in this session")
            return

        def loop() -> None:
            for line in sys.stdin:
                code = line.strip()
                if not code:
                    continue
                log.info("%s", src.submit_code(code))

        threading.Thread(target=loop, daemon=True, name="pair-input").start()

    def _bind_hotkeys(self) -> None:
        if self.hotkeys:
            self.hotkeys.stop()
        self.hotkeys = HotkeyListener(self.cfg.hotkeys.bindings())
        self.hotkeys.start()

    # -- actions -------------------------------------------------------------
    def _on_off(self, label: str, on: bool) -> None:
        log.info("%s %s", label, "on" if on else "off")
        self.sounds.play("on" if on else "off")

    def _handle_action(self, action: str, now: float) -> None:
        p = self.pipeline
        self._last_action = (action, now)  # lets the setup guide confirm a shortcut worked
        if action.startswith("set_sensitivity:"):
            lo, hi = SENSITIVITY_LIMITS
            try:
                p.sensitivity = round(max(lo, min(hi, float(action.split(":", 1)[1]))), 2)
            except ValueError:
                pass
        elif action.startswith("use_profile:"):
            self._switch_profile(action.split(":", 1)[1])
        elif action == "next_profile":
            names = ["", *self.cf.profiles.names()]
            current = next((i for i, n in enumerate(names) if n.lower() == self.cf.profile.lower()), 0)
            self._switch_profile(names[(current + 1) % len(names)], sound=True)
        elif action == "suspend_hotkeys":
            # While the window records a new shortcut, the old ones must not fire.
            if self.hotkeys and not self._hotkeys_suspended:
                self.hotkeys.stop()
                self._hotkeys_suspended = True
        elif action == "resume_hotkeys":
            if self._hotkeys_suspended:
                self._hotkeys_suspended = False
                self._bind_hotkeys()
        elif action == "recenter":
            p.recenter()
            log.info("Recentered")
            self.sounds.play("recenter")
        elif action == "pause":
            paused = p.toggle_pause()
            log.info("Tracking %s", "paused" if paused else "resumed")
            self.sounds.play("off" if paused else "on")
        elif action == "toggle_gaze":
            self._on_off("Eye-assist", p.toggle_gaze())
        elif action == "toggle_translation":
            self._on_off("Position tracking (x/y/z)", p.toggle_translation())
        elif action == "toggle_roll":
            self._on_off("Roll", p.toggle_roll())
        elif action in ("sensitivity_up", "sensitivity_down"):
            up = action == "sensitivity_up"
            before = p.sensitivity
            value = p.change_sensitivity(up)
            log.info("Sensitivity x%.2f", value)
            self.sounds.play("error" if value == before else "up" if up else "down")
        elif action == "toggle_preview":
            self._set_preview(self.preview is None)
        elif action == "toggle_overlay":
            self._dot_on = not self._dot_on
            self._on_off("Gaze dot", self._dot_on)
        elif action == "calibrate":
            if self.session is not None and not self.session.finished:
                self.session.cancel(now)
                self._session_end = now
                self.sounds.play("off")
            elif self.session is None:
                self._start_calibration(now)

    def _set_preview(self, show: bool) -> None:
        if show and self.preview is None:
            from .preview import Preview  # pulls in OpenCV

            self.preview = Preview()
            self.source.want_frames = True
        elif not show and self.preview is not None:
            self.preview.close()
            self.preview = None
            self.source.want_frames = False

    def _update_dot(self, face: bool) -> None:
        """Move the gaze dot, hiding it without a face and while calibrating."""
        if not (self._dot_on and face and self.session is None):
            if self.dot is not None:
                self.dot.hide()
            return
        if self.dot is None:
            self.dot = GazeDot(pick(list_monitors(), self.cfg.calibration.monitor), self.cfg.overlay.size)
        point = self.pipeline.gaze_point(self.dot.area.width / self.dot.area.height)
        if point is None:
            self.dot.hide()
        else:
            self.dot.show_at(*point)

    def _close_dot(self) -> None:
        if self.dot is not None:
            self.dot.close()
            self.dot = None

    # -- calibration ---------------------------------------------------------
    def _start_calibration(self, now: float) -> None:
        from .calibration_overlay import CalibrationOverlay  # pulls in OpenCV

        log.info("Calibration started")
        self.sounds.play("on")
        area = pick(list_monitors(), self.cfg.calibration.monitor)
        self.session = CalibrationSession(now, self.cfg.overlay.screen_degrees, area.width / area.height)
        self.overlay = CalibrationOverlay(monitor=self.cfg.calibration.monitor)
        self._session_end = None

    def _run_calibration(self, obs, now: float) -> None:
        s = self.session
        assert s is not None and self.overlay is not None
        before = (s.phase, s.index)
        s.update(obs, now)
        if self.overlay.escape_pressed and not s.finished:
            s.cancel(now)
            self.sounds.play("off")
        if (s.phase, s.index) != before:
            if s.phase == "message" and not s.message_ok:
                self.sounds.play("error")
            elif s.index != before[1] and not s.finished:
                self.sounds.play("step")
        if s.finished and self._session_end is None:
            self._session_end = now
            if s.phase == "done":
                self._apply_calibration(s)
            else:
                log.info("Calibration cancelled")
        self.overlay.render(s, now, self.cfg.hotkeys.calibrate or "Esc")
        if self._session_end is not None and now - self._session_end > (
                RESULT_SCREEN_SECONDS if s.phase == "done" else 1.0):
            self.overlay.close()
            self.overlay = None
            self.session = None

    def _apply_calibration(self, s: CalibrationSession) -> None:
        cal = s.result
        try:
            cal.save(self.calibration_path)
        except OSError as exc:
            log.error("Could not save calibration: %s", exc)
        self.calibration = cal
        self.pipeline.set_calibration(cal)
        if s.center is not None:
            self.pipeline.set_center(s.center)
        log.info("Calibration saved: %s", cal.summary())
        self.sounds.play("done")

    # -- profiles -------------------------------------------------------------
    def _switch_profile(self, name: str, reason: str = "", sound: bool = False) -> None:
        if name.lower() == self.cf.profile.lower():
            return
        try:
            self.cf.use_profile(name)
        except ConfigError as exc:
            log.error("%s", exc)
            if sound:
                self.sounds.play("error")
            return
        self._reload()
        log.info("Profile: %s%s", self.cf.profile or "Default", f" ({reason})" if reason else "")
        if sound:
            self.sounds.play("step")

    def _follow_front_app(self) -> None:
        """Use the profile of the program in front. Programs without one keep the
        current profile, so alt-tabbing to the desktop or this window changes nothing."""
        try:
            app = foreground_app()
        except OSError:
            return
        if app is None or app == self._front_app:
            return
        self._front_app = app
        name = self.cf.profiles.find_for_app(app)
        if name:
            self._switch_profile(name, f"{app} is in front", sound=True)

    # -- config reload -------------------------------------------------------
    def _reload(self) -> None:
        old = self.cfg
        self.cfg = self.cf.config
        self.pipeline.set_config(self.cfg)
        self.sounds.enabled = self.cfg.hotkeys.sounds and sys.platform == "win32"
        if old.output != self.cfg.output:
            for o in self.outputs:
                o.close()
            self.outputs = build_outputs(self.cfg.output)
        if old.overlay.enabled != self.cfg.overlay.enabled:
            self._dot_on = self.cfg.overlay.enabled
        if (old.overlay.size, old.calibration.monitor) != (self.cfg.overlay.size, self.cfg.calibration.monitor):
            self._close_dot()  # made again, with the new size or on the new monitor, when next shown
        if old.hotkeys.bindings() != self.cfg.hotkeys.bindings() and not self._hotkeys_suspended:
            self._bind_hotkeys()
        if (old.source != self.cfg.source or old.tracking.min_confidence != self.cfg.tracking.min_confidence
                or old.tracking.blendshapes != self.cfg.tracking.blendshapes):
            log.warning("[source], min_confidence and blendshapes changes take effect after restarting OpenITrack")

    # -- main loop -------------------------------------------------------------
    def run(self) -> None:
        # The overlay and preview windows are created on this thread: real pixels,
        # even when the main window is scaled by Windows.
        use_physical_pixels()
        model_path = ensure_model()
        self.source = self._make_source(model_path)
        self.outputs = build_outputs(self.cfg.output)
        self._bind_hotkeys()
        if self.calibration is not None:
            log.info("Loaded calibration from %s", self.calibration_path)
        else:
            log.info("Not calibrated yet: press %s (or run `openitrack calibrate`) for best results",
                     self.cfg.hotkeys.calibrate or "the calibrate shortcut")
        self._set_preview(self._want_preview)
        if self._calibrate_on_start:
            self._start_calibration(time.monotonic())

        if self.cf.profile:
            log.info("Profile: %s", self.cf.profile)
        log.info("Running. Look at the center of your screen and press %s to recenter. Ctrl+C to quit.",
                 self.cfg.hotkeys.recenter)
        last_poll = last_status = last_snap = time.monotonic()
        frames = 0
        fps = 0.0
        status = ""
        try:
            for sample in self.source.samples():
                now = time.monotonic()
                if self._stop.is_set():
                    break
                assert self.hotkeys is not None
                for q in (self.hotkeys.events, self._commands):
                    while True:
                        try:
                            self._handle_action(q.get_nowait(), now)
                        except queue.Empty:
                            break
                if now - last_poll >= 1.0:
                    last_poll = now
                    if self.cf.poll():
                        self._reload()
                    if self.cfg.profiles.auto_switch:
                        self._follow_front_app()
                if not sample.fresh:
                    continue
                status = sample.status
                was_centered = self.pipeline.centered
                held = self.hotkeys.held
                calibrating = self.session is not None
                pose = self.pipeline.update(
                    sample.obs, now,
                    freeze="freeze_hold" in held,
                    center_hold="center_hold" in held,
                    calibrating=calibrating,
                )
                if not was_centered and self.pipeline.centered:
                    log.info("Face found, centered")
                for out in self.outputs:
                    out.send(pose)
                if sample.obs is not None:
                    frames += 1

                if calibrating:
                    self._run_calibration(sample.obs, now)
                self._update_dot(sample.obs is not None)

                if self.preview is not None:
                    raw = self.pipeline.raw_relative(sample.obs) if sample.obs else None
                    self.preview.show(sample.frame, sample.landmarks, pose, raw, status, self._flags(held))
                    if not self.preview.open:
                        self._set_preview(False)

                if now - last_status >= 0.5:
                    fps = frames / (now - last_status)
                    frames, last_status = 0, now
                    self._print_status(status, fps, pose, held)
                if now - last_snap >= SNAPSHOT_INTERVAL:
                    last_snap = now
                    self._publish(status, fps, pose, held, sample.obs is not None)
        except KeyboardInterrupt:
            pass
        finally:
            if sys.stdout is not None and sys.stdout.isatty():
                sys.stdout.write("\n")
            self.close()
            self.snapshot = {**self.snapshot, "running": False, "status": "stopped"}

    def _publish(self, status: str, fps: float, pose, held, face: bool) -> None:
        """Replace the snapshot the window reads (a new dict, so reads never tear)."""
        p = self.pipeline
        src = self.source
        snap = {
            "running": True,
            "mode": self.cfg.source.type,
            "profile": self.cf.profile,
            "status": status,
            "fps": fps,
            "face": face,
            "pose": pose.as_tuple(),
            "centered": p.centered,
            "paused": p.paused,
            "gaze": self.cfg.gaze.enabled and p.gaze_enabled,
            "translation": p.translation_enabled,
            "roll": p.roll_enabled,
            "sensitivity": p.sensitivity,
            "held": sorted(held),
            "calibrating": self.session is not None,
            "calibrated": self.calibration.created if self.calibration else None,
            "preview": self.preview is not None,
            "overlay": self._dot_on,
            "flags": self._flags(held),
            "last_action": getattr(self, "_last_action", None),
        }
        c = p.center
        if c is not None:
            # Neutral face pose relative to the camera: where the camera sits.
            snap["center"] = {"yaw": c.yaw, "pitch": c.pitch, "roll": c.roll, "x": c.x, "y": c.y, "z": c.z}
        if hasattr(src, "pending_requests"):
            snap["phone"] = {
                "urls": list(src.urls),
                "hostname": src.hostname,
                "pending": src.pending_requests(),
                "active": src.active_name,
            }
        self.snapshot = snap

    def _flags(self, held: frozenset[str] = frozenset()) -> str:
        p = self.pipeline
        parts = []
        if self.session is not None:
            parts.append("CALIBRATING")
        if p.paused:
            parts.append("PAUSED")
        if "center_hold" in held:
            parts.append("CENTER")
        elif "freeze_hold" in held:
            parts.append("FROZEN")
        if not (self.cfg.gaze.enabled and p.gaze_enabled):
            parts.append("eyes off")
        if not p.translation_enabled:
            parts.append("xyz off")
        if not p.roll_enabled:
            parts.append("roll off")
        if p.sensitivity != 1.0:
            parts.append(f"sens x{p.sensitivity:.2f}")
        return " ".join(parts)

    def _print_status(self, status: str, fps: float, pose, held) -> None:
        if sys.stdout is None or not sys.stdout.isatty() or getattr(self.source, "pairing_pending", False):
            return  # don't overwrite what the user is typing
        line = (f"\r[{status:<17}] {fps:4.0f} fps | yaw {pose.yaw:+6.1f} pitch {pose.pitch:+6.1f} "
                f"roll {pose.roll:+5.1f} | x {pose.x:+5.1f} y {pose.y:+5.1f} z {pose.z:+5.1f} "
                f"{self._flags(held):<20}")
        sys.stdout.write(line)
        sys.stdout.flush()

    def close(self) -> None:
        if self.hotkeys:
            self.hotkeys.stop()
        for o in self.outputs:
            o.close()
        if self.overlay is not None:
            self.overlay.close()
        self._close_dot()
        if self.preview is not None:
            self.preview.close()
        if self.source is not None:
            self.source.close()
