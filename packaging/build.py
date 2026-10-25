"""Build dist/OpenITrack/OpenITrack.exe and a shareable zip.

    .venv\\Scripts\\pip install -e .[build]
    .venv\\Scripts\\python packaging\\build.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
sys.path.insert(0, str(ROOT))

from openitrack import __version__  # noqa: E402
from openitrack.model import MODEL_NAME, MODEL_URL  # noqa: E402
from openitrack.webassets import ensure_web_assets  # noqa: E402


def write_version_info(path: Path) -> None:
    nums = [int(p) for p in __version__.split(".")] + [0] * 4
    v = tuple(nums[:4])
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', 'OpenITrack head and eye tracker'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', 'OpenITrack'),
      StringStruct('OriginalFilename', 'OpenITrack.exe'),
      StringStruct('ProductName', 'OpenITrack'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")


def ensure_bundled_model() -> None:
    target = ROOT / "openitrack" / "models" / MODEL_NAME
    if target.is_file() and target.stat().st_size > 1_000_000:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {MODEL_URL}")
    urllib.request.urlretrieve(MODEL_URL, target)


def main() -> int:
    ensure_bundled_model()
    ensure_web_assets(ROOT / "openitrack" / "web" / "vendor")  # hash-checked
    write_version_info(PACKAGING / "version_info.txt")

    dist, work = ROOT / "dist", ROOT / "build"
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(dist), "--workpath", str(work), str(PACKAGING / "OpenITrack.spec")],
        check=True, cwd=ROOT,
    )
    # build/ holds PyInstaller's intermediate files, including an OpenITrack.exe
    # that can't run on its own. Remove it so nobody launches the wrong exe;
    # builds always start clean (--clean), so nothing is lost.
    shutil.rmtree(work, ignore_errors=True)
    app_dir = dist / "OpenITrack"
    shutil.copy2(ROOT / "README.md", app_dir / "README.md")
    zip_base = dist / f"OpenITrack-{__version__}-win64"
    archive = shutil.make_archive(str(zip_base), "zip", root_dir=dist, base_dir="OpenITrack")
    print(f"\nBuilt {app_dir / 'OpenITrack.exe'}\nZip   {archive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
