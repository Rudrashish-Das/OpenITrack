"""Command line entry point."""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, ConfigFile, default_config_path
from .devices import DeviceStore
from .hotkeys import watch_buttons
from .monitors import list_monitors
from .reset import delete_user_data, saved_items


class _StatusAwareHandler(logging.StreamHandler):
    """Clears the live status line before printing a log message."""

    def emit(self, record: logging.LogRecord) -> None:
        if self.stream.isatty():
            self.stream.write("\r" + " " * 118 + "\r")
        super().emit(record)


def _setup_logging(verbose: bool) -> None:
    handler = _StatusAwareHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=[handler])
    # MediaPipe / TFLite are chatty on stderr at startup.
    os.environ.setdefault("GLOG_minloglevel", "2")
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="openitrack",
        description="Webcam / phone head and eye tracking for simulators (opentrack-compatible).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", type=Path, default=None,
                        help=f"config file (default: {default_config_path()})")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command")

    def tracking_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--preview", action="store_true", help="show the tuning window")
        p.add_argument("--phone", action="store_true", help="use phone mode for this run")
        p.add_argument("--webcam", action="store_true", help="use webcam mode for this run")
        p.add_argument("--camera", help="camera index or URL for this run")
        p.add_argument("--profile", help="profile to start with (\"\" = Default), see `openitrack profiles`")

    run = sub.add_parser("run", help="start tracking (default)")
    tracking_options(run)
    run.add_argument("--calibrate", action="store_true", help="start with the calibration overlay")

    cal = sub.add_parser("calibrate", help="start tracking and run the calibration overlay")
    tracking_options(cal)
    cal.add_argument("--reset", action="store_true", help="delete the saved calibration and exit")

    devices = sub.add_parser("devices", help="list paired phones, or revoke them")
    devices.add_argument("--revoke", metavar="ID", help="device id (or its first characters), or 'all'")

    sub.add_parser("gui", help="open the OpenITrack window")
    reset = sub.add_parser("reset", help="delete everything OpenITrack saved (setup runs again next time)")
    reset.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    reset.add_argument("--downloads", action="store_true", help="also delete downloaded tracker files")
    profiles = sub.add_parser("profiles", help="list per-game profiles, or make one")
    profiles.add_argument("--new", metavar="NAME", help="make a profile (a file you can then edit)")
    profiles.add_argument("--app", action="append", default=[], metavar="EXE",
                          help="with --new: program that switches to it, e.g. DCS.exe (repeatable)")

    sub.add_parser("cameras", help="list webcams")
    sub.add_parser("monitors", help="list monitors (for [calibration] monitor)")
    buttons = sub.add_parser("buttons", help="press mouse/joystick buttons to see their shortcut names")
    buttons.add_argument("--seconds", type=float, default=60.0)
    sub.add_parser("config", help="print the config file path (creating it if missing)")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    log = logging.getLogger("openitrack")
    path = args.config or default_config_path()

    if args.command == "reset":
        data_dir = path.parent
        items = saved_items(data_dir, args.downloads)
        if not items:
            print("Nothing saved; already clean.")
            return 0
        print(f"This deletes from {data_dir}:")
        for p in items:
            print(f"  {p.name}")
        if not args.yes:
            try:
                answer = input("Delete these? Paired phones will need to pair again. [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                print("Nothing deleted.")
                return 1
        report = delete_user_data(data_dir, args.downloads)
        for p, why in report.failed:
            print(f"  could not delete {p.name}: {why}")
        print(f"Deleted {len(report.removed)} item(s). Setup runs again the next time you open OpenITrack.")
        return 0 if not report.failed else 1

    if args.command == "gui":
        from .gui import main as gui_main  # pulls in tkinter

        return gui_main(["-c", str(args.config)] if args.config else [])

    if args.command == "monitors":
        for i, m in enumerate(list_monitors(), 1):
            primary = ", primary: also monitor = 0" if m.primary else ""
            print(f"  monitor = {i}    ({m.width}x{m.height} at {m.left},{m.top}{primary})")
        print('  monitor = "all"  (span every monitor)')
        return 0

    if args.command == "cameras":
        from .sources.webcam import list_cameras  # pulls in OpenCV

        cams = list_cameras()
        if not cams:
            print("No webcams found.")
        for idx, w, h in cams:
            print(f"  camera = {idx}    ({w}x{h})")
        return 0

    if args.command == "buttons":
        try:
            watch_buttons(args.seconds)
        except KeyboardInterrupt:
            pass
        return 0

    try:
        cf = ConfigFile(path, getattr(args, "profile", None))
    except ConfigError as exc:
        log.error("Config error in %s: %s", path, exc)
        return 2

    if args.command == "config":
        print(path)
        return 0

    if args.command == "profiles":
        store = cf.profiles
        if args.new:
            try:
                name = store.create(args.new, cf.base, apps=args.app)
            except ConfigError as exc:
                print(exc)
                return 2
            print(f"Made profile '{name}': {store.path(name)}")
            print("Edit that file to change settings for this profile.")
            return 0
        listed = store.list()
        auto = "on" if cf.base.profiles.auto_switch else "off"
        mark = "*" if not cf.profile else " "
        print(f"{mark} Default          config.toml   (automatic switching {auto})")
        for prof in listed:
            mark = "*" if prof.name == cf.profile else " "
            apps = ", ".join(prof.apps) or "(no programs: pick it by hand)"
            print(f"{mark} {prof.name:<16} {apps}")
        print(f"Profiles folder: {store.dir}")
        return 0

    if args.command == "devices":
        store = DeviceStore(path.parent)
        if args.revoke:
            if args.revoke != "all" and len(args.revoke) < 4:
                print("Give at least 4 characters of the device id, or 'all'.")
                return 2
            gone = store.revoke(args.revoke)
            for d in gone:
                print(f"Revoked {d.name} [{d.id}]. It must pair again to connect.")
            if not gone:
                print("No paired phone matches that id.")
            return 0 if gone else 1
        listed = store.list()
        if not listed:
            print("No paired phones.")
        for d in listed:
            print(f"  {d.id}  {d.name:<16} paired {d.paired}  last seen {d.last_seen or 'never'}")
        return 0

    if args.command == "calibrate" and args.reset:
        from .engine import CALIBRATION_FILE  # pulls in numpy and the tracking stack

        cal_path = path.parent / CALIBRATION_FILE
        if cal_path.exists():
            cal_path.unlink()
            print(f"Removed {cal_path}")
        else:
            print("No saved calibration.")
        return 0

    preview = getattr(args, "preview", False)
    calibrate = args.command == "calibrate" or getattr(args, "calibrate", False)
    if getattr(args, "phone", False):
        cf.override({"source": {"type": "phone"}})
    if getattr(args, "webcam", False):
        cf.override({"source": {"type": "webcam"}})
    if getattr(args, "camera", None) is not None:
        cam = args.camera
        cf.override({"source": {"camera": int(cam) if cam.isdigit() else cam}})

    from .engine import Engine  # pulls in numpy and the tracking stack

    log.info("OpenITrack %s, config: %s", __version__, path)
    try:
        Engine(cf, preview=preview, calibrate=calibrate).run()
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    return 0


def _launched_by_double_click() -> bool:
    """True when this process owns its console window (Explorer double-click),
    in which case the window would vanish before anyone could read it."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    procs = (ctypes.c_uint * 2)()
    return ctypes.windll.kernel32.GetConsoleProcessList(procs, 2) <= 1


def frozen_main() -> int:
    code = main()
    if _launched_by_double_click():
        try:
            input("\nPress Enter to close...")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
