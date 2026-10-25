"""Paired phones. Each device's secret is encrypted at rest with Windows DPAPI,
so the file is useless to other Windows accounts or if copied off the PC."""

from __future__ import annotations

import base64
import ctypes
import datetime as dt
import json
import logging
import os
import secrets
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEVICES_FILE = "paired_devices.json"
_ENTROPY = b"OpenITrack device secret"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buf_in = ctypes.create_string_buffer(data, len(data))
    ent = ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
    blob_in = _Blob(len(data), ctypes.cast(buf_in, ctypes.POINTER(ctypes.c_char)))
    blob_ent = _Blob(len(_ENTROPY), ctypes.cast(ent, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, ctypes.byref(blob_ent), None, None, 0x1,  # UI_FORBIDDEN
            ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"DPAPI {'protect' if protect else 'unprotect'} failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def seal(secret: bytes) -> str:
    if sys.platform == "win32":
        return "dpapi:" + base64.b64encode(_dpapi(secret, True)).decode()
    return "raw:" + base64.b64encode(secret).decode()


def unseal(value: str) -> bytes:
    kind, _, data = value.partition(":")
    raw = base64.b64decode(data)
    if kind == "dpapi":
        return _dpapi(raw, False)
    if kind == "raw":
        return raw
    raise ValueError("unknown secret format")


@dataclass
class Device:
    id: str
    name: str
    paired: str
    last_seen: str = ""


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class DeviceStore:
    """Thread-safe; re-reads the file when it changes (so `openitrack devices
    --revoke` takes effect on a running tracker)."""

    def __init__(self, directory: Path):
        self.path = directory / DEVICES_FILE
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._data: dict = {"devices": []}

    def _load(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self._data, self._mtime = {"devices": []}, None
            return
        if mtime == self._mtime:
            return
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
            self._mtime = mtime
        except (OSError, ValueError) as exc:
            log.error("Can't read %s (%s); no phones are paired until it's fixed or deleted", self.path, exc)
            self._data = {"devices": []}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        if sys.platform != "win32":
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime

    def add(self, name: str, secret: bytes) -> Device:
        with self._lock:
            self._load()
            dev = Device(id=secrets.token_hex(8), name=name[:40] or "Phone", paired=_now())
            self._data["devices"].append({**dev.__dict__, "secret": seal(secret)})
            self._save()
            return dev

    def secret(self, device_id: str) -> tuple[Device, bytes] | None:
        with self._lock:
            self._load()
            for d in self._data["devices"]:
                if secrets.compare_digest(str(d.get("id", "")), device_id):
                    try:
                        raw = unseal(d["secret"])
                    except (OSError, ValueError, KeyError) as exc:
                        log.error("Paired phone %s has an unreadable secret (%s); pair it again", device_id, exc)
                        return None
                    return Device(d["id"], d.get("name", ""), d.get("paired", ""), d.get("last_seen", "")), raw
        return None

    def touch(self, device_id: str) -> None:
        with self._lock:
            self._load()
            for d in self._data["devices"]:
                if d.get("id") == device_id:
                    d["last_seen"] = _now()
                    self._save()
                    return

    def list(self) -> list[Device]:
        with self._lock:
            self._load()
            return [Device(d["id"], d.get("name", ""), d.get("paired", ""), d.get("last_seen", ""))
                    for d in self._data["devices"]]

    def revoke(self, prefix: str) -> list[Device]:
        """Remove devices whose id starts with prefix ('all' removes every device)."""
        with self._lock:
            self._load()
            keep, gone = [], []
            for d in self._data["devices"]:
                (gone if prefix == "all" or d.get("id", "").startswith(prefix) else keep).append(d)
            if gone:
                self._data["devices"] = keep
                self._save()
            return [Device(d["id"], d.get("name", ""), d.get("paired", ""), d.get("last_seen", "")) for d in gone]
