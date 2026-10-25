"""MediaPipe-for-web files served to the phone by the PC itself.

Serving them locally means the phone page makes no third-party requests (no CDN
learns who uses OpenITrack or when), works without internet, and runs only code
whose SHA-256 is pinned here.
"""

from __future__ import annotations

import hashlib
import logging
import os
import urllib.request
from pathlib import Path

from .config import default_config_path

log = logging.getLogger(__name__)

MEDIAPIPE_VERSION = "1.1.1"
_BASE = f"https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@{MEDIAPIPE_VERSION}/"

# relative path -> sha256
FILES = {
    "vision_bundle.mjs": "a84921e02321e582484c1f8126d344b5f33b791fa09c640a52e20ee8632068f1",
    "wasm/vision_wasm_internal.js": "6522286aa32dd93eff17c3da002b0649516f87abfec7a80b5bfe5c9625caa5f8",
    "wasm/vision_wasm_internal.wasm": "0e101a010e262ef8e78f22e3c95b60a9705fd06962753c9acd6356d9fb56b7bd",
    "wasm/vision_wasm_nosimd_internal.js": "942654dbdcebce08ea039af23d18e58bebf23c8607d74023d2df7071c8a7b638",
    "wasm/vision_wasm_nosimd_internal.wasm": "c9b1b69bb87baf9e529254d8559dcbcf4c5a9c0eda84f56a12f17d675335ec9c",
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _complete(directory: Path) -> bool:
    return all((directory / rel).is_file() and _sha256(directory / rel) == digest
               for rel, digest in FILES.items())


def candidates() -> list[Path]:
    return [
        Path(__file__).resolve().parent / "web" / "vendor",         # bundled with the package / exe
        default_config_path().parent / "web-vendor" / MEDIAPIPE_VERSION,
    ]


def ensure_web_assets(target: Path | None = None) -> Path:
    """Return a directory holding every pinned file, downloading once if needed."""
    if target is None:
        for d in candidates():
            if _complete(d):
                return d
        target = candidates()[-1]
    elif _complete(target):
        return target
    log.info("Downloading the phone tracker files once (~27 MB, MediaPipe %s)", MEDIAPIPE_VERSION)
    for rel, digest in FILES.items():
        dest = target / rel
        if dest.is_file() and _sha256(dest) == digest:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with urllib.request.urlopen(_BASE + rel, timeout=60) as resp, open(tmp, "wb") as fh:
                while chunk := resp.read(1 << 16):
                    fh.write(chunk)
            got = _sha256(tmp)
            if got != digest:
                raise RuntimeError(f"{rel}: checksum mismatch (expected {digest[:12]}…, got {got[:12]}…)")
            os.replace(tmp, dest)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"Could not fetch phone tracker file {rel}: {exc}") from exc
    return target
