"""Built-in TrackIR / FreeTrack game output, so games get head tracking without opentrack.

Games with TrackIR or FreeTrack support load NPClient(64).dll or
FreeTrackClient(64).dll from the folder the registry names. Those libraries
(opentrack's, bundled in clients/) read the pose from the "FT_SharedMem" shared
memory. This module writes that memory the way opentrack's "freetrack 2.0
Enhanced" output does (proto-ft/ftnoir_protocol_ft.cpp) and points the registry
at the bundled folder while it runs, putting the previous values back afterwards.
For the few games that also want a running TrackIR.exe, it starts OpenITrack's
stand-in (packaging/trackir_dummy) from the same folder.
"""

from __future__ import annotations

import atexit
import ctypes
import json
import logging
import math
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path

from .config import default_config_path
from .mapping import Pose

if sys.platform == "win32":
    import winreg

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileMappingW.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                        wintypes.DWORD, wintypes.LPCWSTR)
    _k32.CreateFileMappingW.restype = wintypes.HANDLE
    _k32.MapViewOfFile.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t)
    _k32.MapViewOfFile.restype = ctypes.c_void_p
    _k32.UnmapViewOfFile.argtypes = (ctypes.c_void_p,)
    _k32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    _k32.CreateMutexW.restype = wintypes.HANDLE
    _k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.Process32FirstW.argtypes = _k32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.c_void_p)

log = logging.getLogger(__name__)

HEAP_NAME = "FT_SharedMem"
MUTEX_NAME = "FT_Mutext"  # sic: the name every FreeTrack client uses
CLIENTS_DIR = Path(__file__).resolve().parent / "clients"
GAMES_CSV = CLIENTS_DIR / "supported-games.csv"
# OpenITrack's stand-in TrackIR.exe, built from packaging/trackir_dummy by
# packaging/build.py (`make trackir` when running from source).
TRACKIR_EXE = CLIENTS_DIR / "TrackIR.exe"

# HKCU keys whose "Path" value tells games where the client DLLs are.
NPCLIENT_KEY = r"Software\NaturalPoint\NATURALPOINT\NPClient Location"
FREETRACK_KEY = r"Software\Freetrack\FreeTrackClient"
REGISTRY_KEYS = (NPCLIENT_KEY, FREETRACK_KEY)
REGISTRY_VALUE = "Path"

_D2R = math.pi / 180
_PAGE_READWRITE = 0x04
_FILE_MAP_WRITE = 0x0002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_WAIT_OBJECT_0, _WAIT_ABANDONED = 0x0, 0x80
_TH32CS_SNAPPROCESS = 0x2
_CREATE_NO_WINDOW = 0x08000000


class FTData(ctypes.Structure):
    """FTData from opentrack's freetrackclient/fttypes.h."""
    _fields_ = [("DataID", ctypes.c_uint32), ("CamWidth", ctypes.c_int32), ("CamHeight", ctypes.c_int32)] + [
        (name, ctypes.c_float) for name in (
            "Yaw", "Pitch", "Roll", "X", "Y", "Z",
            "RawYaw", "RawPitch", "RawRoll", "RawX", "RawY", "RawZ",
            "X1", "Y1", "X2", "Y2", "X3", "Y3", "X4", "Y4")]


class FTHeap(ctypes.Structure):
    """FTHeap: FTData, then the NPClient fields (the game's ID and its key)."""
    _fields_ = [("data", FTData), ("GameID", ctypes.c_int32), ("table", ctypes.c_ubyte * 8),
                ("GameID2", ctypes.c_int32)]


POSE_FIELDS = ("Yaw", "Pitch", "Roll", "X", "Y", "Z", "RawYaw", "RawPitch", "RawRoll", "RawX", "RawY", "RawZ")


def freetrack_values(pose: Pose) -> tuple[float, ...]:
    """The POSE_FIELDS values opentrack's freetrack output writes for this pose
    (as received from our UDP output): radians and millimetres.

    opentrack gets yaw and z flipped (see outputs.pack_opentrack), then negates
    yaw and pitch, so: Yaw + = left, Pitch + = down, Roll + = tilt left,
    X + = left, Y + = up, Z + = away from the screen. It fills the Raw fields
    with the same pose but pitch not negated; we have no separate raw pose.
    """
    # opentrack: Falcon BMS makes a "bump" if pitch crosses 90 degrees.
    pitch = 89.86 if abs(pose.pitch - 90) < .15 else pose.pitch
    return (pose.yaw * _D2R, -pitch * _D2R, pose.roll * _D2R, pose.x * 10, pose.y * 10, -pose.z * 10,
            pose.yaw * _D2R, pose.pitch * _D2R, pose.roll * _D2R, pose.x * 10, pose.y * 10, -pose.z * 10)


def _key_table(since: str, code: str) -> bytes:
    """The 8-byte key hidden in a game's 22-hex-digit FTN_ID (opentrack csv.cpp).
    Games supported since V160 don't use one."""
    if since == "V160" or len(code) != 22:
        return bytes(8)
    try:
        b = bytes.fromhex(code)
    except ValueError:
        return bytes(8)
    return bytes((b[5], b[4], b[3], b[2], b[9], b[8], b[7], b[6]))


def load_game_keys(path: Path = GAMES_CSV) -> dict[str, tuple[str, bytes]]:
    """Game ID (as text) -> (name, key) from opentrack's supported games list."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("Can't read the game list %s (%s); games that need a key won't track", path, exc)
        return {}
    keys: dict[str, tuple[str, bytes]] = {}
    # No;Game Name;Game protocol;Supported since;Verified;By;INTERNATIONAL_ID;FTN_ID
    for line in text.splitlines():
        cols = line.split(";")
        if len(cols) == 8:
            keys.setdefault(cols[6], (cols[1], _key_table(cols[3], cols[7])))
    return keys


class FreeTrackWriter:
    """Writes poses into an FTHeap the way opentrack does, and gives each TrackIR
    game that registers (NPClient writes its ID into GameID) its key."""

    def __init__(self, heap: FTHeap, keys: dict[str, tuple[str, bytes]]):
        self.heap = heap
        self.keys = keys
        self.game_id = -1
        self.game = ""  # name of the TrackIR game reading the pose, if one registered
        heap.data.DataID = 1
        heap.data.CamWidth = 100
        heap.data.CamHeight = 250
        heap.GameID2 = 0
        heap.table[:] = bytes(8)

    def write(self, pose: Pose) -> None:
        data = self.heap.data
        for name, value in zip(POSE_FIELDS, freetrack_values(pose)):
            setattr(data, name, value)
        game_id = self.heap.GameID
        if game_id != self.game_id:
            name, table = self.keys.get(str(game_id), ("", bytes(8)))
            self.heap.table[:] = table
            self.heap.GameID2 = game_id
            data.DataID = 0
            self.game_id = game_id
            self.game = name or (f"Unknown game ({game_id})" if game_id else "")
            if game_id:
                log.info("TrackIR game connected: %s", self.game)
        else:
            data.DataID = (data.DataID + 1) & 0xFFFFFFFF

    def clear(self) -> None:
        """Center the view (all pose fields 0) so a game doesn't keep the last pose."""
        for name in POSE_FIELDS:
            setattr(self.heap.data, name, 0.0)


class SharedMemory:
    """The named FreeTrack shared memory and its mutex (Windows only)."""

    def __init__(self, name: str | None = None, mutex_name: str | None = None):
        if sys.platform != "win32":
            raise RuntimeError("The direct game output is only supported on Windows")
        size = ctypes.sizeof(FTHeap)
        self._map = _k32.CreateFileMappingW(_INVALID_HANDLE_VALUE, None, _PAGE_READWRITE, 0, size,
                                            name or HEAP_NAME)
        if not self._map:
            raise ctypes.WinError(ctypes.get_last_error())
        self._view = _k32.MapViewOfFile(self._map, _FILE_MAP_WRITE, 0, 0, size)
        if not self._view:
            err = ctypes.get_last_error()
            _k32.CloseHandle(self._map)
            raise ctypes.WinError(err)
        self.heap = FTHeap.from_address(self._view)
        self._mutex = _k32.CreateMutexW(None, False, mutex_name or MUTEX_NAME)

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Hold the mutex FreeTrack clients take while copying the pose, so they
        never read a half-written one. Writes anyway if a client holds it too long."""
        got = bool(self._mutex) and _k32.WaitForSingleObject(self._mutex, 16) in (_WAIT_OBJECT_0, _WAIT_ABANDONED)
        try:
            yield
        finally:
            if got:
                _k32.ReleaseMutex(self._mutex)

    def close(self) -> None:
        if self._view:
            self.heap = None
            _k32.UnmapViewOfFile(self._view)
            self._view = None
        for handle in (self._map, self._mutex):
            if handle:
                _k32.CloseHandle(handle)
        self._map = self._mutex = None


def registry_backup_path() -> Path:
    return default_config_path().parent / "registry-backup.json"


def _read_value(root, key: str) -> tuple[bool, str | None]:
    """(key exists, its Path value or None)."""
    try:
        with winreg.OpenKey(root, key) as k:
            try:
                value, _ = winreg.QueryValueEx(k, REGISTRY_VALUE)
            except FileNotFoundError:
                return True, None
    except FileNotFoundError:
        return False, None
    return True, value if isinstance(value, str) else None


def _put_back(root, key: str, original: dict, ours: str) -> None:
    """Restore one key's Path, unless something else changed it after we did."""
    existed, current = _read_value(root, key)
    if current != ours:
        return
    with winreg.OpenKey(root, key, 0, winreg.KEY_ALL_ACCESS) as k:
        if original.get("value") is not None:
            winreg.SetValueEx(k, REGISTRY_VALUE, 0, winreg.REG_SZ, original["value"])
            return
        winreg.DeleteValue(k, REGISTRY_VALUE)
        empty = winreg.QueryInfoKey(k)[:2] == (0, 0)
    if not original.get("existed", True) and empty:
        winreg.DeleteKey(root, key)


def _restore(root, backup: dict) -> None:
    for key, original in backup.get("keys", {}).items():
        try:
            _put_back(root, key, original, backup.get("value", ""))
        except OSError as exc:
            log.warning("Couldn't restore HKCU\\%s: %s", key, exc)


def restore_stale(backup_path: Path | None = None, root=None) -> None:
    """Undo a redirect left by a run that didn't exit cleanly (crash, killed)."""
    backup_path = backup_path or registry_backup_path()
    if sys.platform != "win32" or not backup_path.is_file():
        return
    try:
        backup = json.loads(backup_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Ignoring unreadable %s: %s", backup_path, exc)
        backup = {}
    _restore(winreg.HKEY_CURRENT_USER if root is None else root, backup)
    backup_path.unlink(missing_ok=True)
    log.info("Restored the TrackIR/FreeTrack registry entries left by an earlier run")


class RegistryRedirect:
    """Points the TrackIR and FreeTrack client locations (HKCU only) at `folder`
    while active, then puts back what was there, so a real TrackIR or opentrack
    install keeps working. The previous values are also kept in `backup_path`,
    so a crash is undone the next time OpenITrack starts."""

    def __init__(self, folder: Path, backup_path: Path | None = None,
                 keys: tuple[str, ...] | None = None, root=None):
        self.value = str(folder).rstrip("\\/") + "\\"
        self.backup_path = backup_path or registry_backup_path()
        self.keys = keys or REGISTRY_KEYS
        self.root = winreg.HKEY_CURRENT_USER if root is None else root
        self._backup: dict | None = None

    def apply(self) -> None:
        try:
            stale = json.loads(self.backup_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stale = {}
        originals = {}
        for key in self.keys:
            existed, current = _read_value(self.root, key)
            if current == self.value:
                # Ours from an earlier run that didn't exit cleanly: keep what that
                # run saved, or remove the value if nothing was saved.
                originals[key] = stale.get("keys", {}).get(key, {"existed": True, "value": None})
            else:
                originals[key] = {"existed": existed, "value": current}
        self._backup = {"value": self.value, "keys": originals}
        self.backup_path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_path.write_text(json.dumps(self._backup, indent=2), encoding="utf-8")
        atexit.register(self.restore)
        for key in self.keys:
            with winreg.CreateKeyEx(self.root, key, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, REGISTRY_VALUE, 0, winreg.REG_SZ, self.value)
        log.info("TrackIR/FreeTrack games will load the client libraries from %s", self.value)

    def restore(self) -> None:
        if self._backup is None:
            return
        backup, self._backup = self._backup, None
        atexit.unregister(self.restore)
        _restore(self.root, backup)
        self.backup_path.unlink(missing_ok=True)


class _ProcessEntry(ctypes.Structure):
    """PROCESSENTRY32W."""
    _fields_ = [("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32), ("th32ProcessID", ctypes.c_uint32),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", ctypes.c_uint32),
                ("cntThreads", ctypes.c_uint32), ("th32ParentProcessID", ctypes.c_uint32),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_uint32),
                ("szExeFile", ctypes.c_wchar * 260)]


def process_ids(name: str) -> list[int]:
    """IDs of the running processes whose exe is called `name` (any case)."""
    snap = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = _ProcessEntry(dwSize=ctypes.sizeof(_ProcessEntry))
        ids = []
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == name.lower():
                ids.append(entry.th32ProcessID)
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
        return ids
    finally:
        _k32.CloseHandle(snap)


class TrackIRProcess:
    """Keeps OpenITrack's stand-in TrackIR.exe running, for games that only use
    TrackIR while a process with that name exists. It isn't started when another
    one (NaturalPoint's, opentrack's) already runs, and only the one started here
    is ever stopped. It is given our process ID and quits when we exit, so a
    crash doesn't leave it behind."""

    def __init__(self, exe: Path | None = None):
        self.exe = exe or TRACKIR_EXE
        self._proc: subprocess.Popen | None = None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def start(self) -> bool:
        """Start it; False if it wasn't (another one runs, or it isn't built)."""
        if self._proc is not None:
            return True
        others = process_ids(self.exe.name)
        if others:
            log.info("%s is already running (process %s); not starting OpenITrack's", self.exe.name,
                     ", ".join(map(str, others)))
            return False
        if not self.exe.is_file():
            log.warning("%s isn't there (run `make trackir` when running from source); games that need a "
                        "running TrackIR.exe won't see one", self.exe)
            return False
        try:
            self._proc = subprocess.Popen([str(self.exe), str(os.getpid())], stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          cwd=self.exe.parent, creationflags=_CREATE_NO_WINDOW)
        except OSError as exc:
            log.warning("Couldn't start %s: %s", self.exe, exc)
            return False
        atexit.register(self.stop)
        log.info("Started %s (process %d) for games that look for it", self.exe.name, self._proc.pid)
        return True

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        atexit.unregister(self.stop)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                log.warning("%s (process %d) didn't stop", self.exe.name, proc.pid)
