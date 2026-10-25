# PyInstaller spec for OpenITrack. Build with:  python packaging/build.py
# One-folder build: starts instantly and is far less likely to trip antivirus
# heuristics than a self-extracting one-file exe.

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent
PKG = ROOT / "openitrack"

datas = [
    (str(PKG / "web" / "phone.html"), "openitrack/web"),
    (str(PKG / "web" / "app.js"), "openitrack/web"),
    (str(PKG / "web" / "frame.js"), "openitrack/web"),
    (str(PKG / "web" / "overlay.js"), "openitrack/web"),
    (str(PKG / "web" / "link.js"), "openitrack/web"),
    (str(PKG / "web" / "pake.js"), "openitrack/web"),
    (str(PKG / "web" / "icon.png"), "openitrack/web"),
    # MediaPipe for the phone page, pinned by hash (packaging/build.py fetches it).
    (str(PKG / "web" / "vendor"), "openitrack/web/vendor"),
    (str(PKG / "models" / "face_landmarker.task"), "openitrack/models"),
    (str(PKG / "assets" / "icon.ico"), "openitrack/assets"),
    # opentrack's TrackIR/FreeTrack client DLLs for the direct game output. Kept as
    # plain data (not binaries) so PyInstaller doesn't scan or move them: games load
    # them from this folder, which the registry points at while tracking.
    (str(PKG / "clients"), "openitrack/clients"),
]
datas += collect_data_files("sv_ttk")  # Windows 11 style theme for the window
datas += collect_data_files("mediapipe", excludes=["**/*.pyc", "**/testdata/**"])

# libmediapipe.dll is loaded with ctypes via importlib.resources, so it must keep
# its package-relative location.
binaries = collect_dynamic_libs("mediapipe")

hiddenimports = collect_submodules("mediapipe.tasks.python") + collect_submodules("zeroconf") + [
    "segno",
    "sv_ttk",
    "aiohttp",
    "cryptography.hazmat.primitives.asymmetric.ec",
]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=[
        # mediapipe.tasks eagerly imports its audio/text modules and matplotlib
        # (drawing utils), so those must stay; only truly unused packages go.
        "IPython", "jupyter", "pytest",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

# Two executables share one _internal folder (packaging/entry.py picks the mode
# from the exe name): the window, and a console build for the command line.
common = dict(
    exclude_binaries=True,
    icon=str(ROOT / "assets" / "logo" / "hud-eye.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
    upx=False,
)
gui_exe = EXE(pyz, a.scripts, [], name="OpenITrack", console=False, **common)
cli_exe = EXE(pyz, a.scripts, [], name="OpenITrack-cli", console=True, **common)

coll = COLLECT(gui_exe, cli_exe, a.binaries, a.datas, strip=False, upx=False, name="OpenITrack")
