"""Direct game output: the FreeTrack shared memory and the registry redirect."""

import ctypes
import json
import math
import struct
import sys

import pytest

from openitrack import freetrack, outputs
from openitrack.config import ConfigError, OutputConfig, parse_config
from openitrack.freetrack import FreeTrackWriter, FTHeap, RegistryRedirect, load_game_keys
from openitrack.mapping import Pose
from openitrack.outputs import pack_opentrack

if sys.platform == "win32":
    import winreg

windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
D2R = math.pi / 180
# opentrack's FTHeap: DataID, CamWidth, CamHeight, 6 pose + 6 raw + 8 point floats,
# then GameID, the 8-byte key and GameID2.
HEAP_FORMAT = "<I2i20f i 8s i"


def heap_bytes(heap: FTHeap) -> tuple:
    return struct.unpack(HEAP_FORMAT, ctypes.string_at(ctypes.addressof(heap), ctypes.sizeof(heap)))


def test_heap_layout_matches_opentrack():
    assert ctypes.sizeof(FTHeap) == struct.calcsize(HEAP_FORMAT) == 108
    assert FTHeap.data.offset == 0 and FTHeap.GameID.offset == 92
    assert FTHeap.table.offset == 96 and FTHeap.GameID2.offset == 104
    d = freetrack.FTData
    assert (d.Yaw.offset, d.X.offset, d.RawYaw.offset, d.X1.offset) == (12, 24, 36, 60)


def test_pose_is_written_like_opentracks_freetrack_output():
    pose = Pose(yaw=10, pitch=5, roll=-3, x=1, y=2, z=4)
    heap = FTHeap()
    FreeTrackWriter(heap, {}).write(pose)
    vals = heap_bytes(heap)
    yaw, pitch, roll, x, y, z = vals[3:9]
    # What opentrack's freetrack proto does with the pose we'd send it over UDP:
    # yaw = -yaw, pitch = -pitch, roll as is, in radians; x, y, z cm -> mm.
    ux, uy, uz, uyaw, upitch, uroll = struct.unpack("<6d", pack_opentrack(pose))
    assert (yaw, pitch, roll) == pytest.approx((-uyaw * D2R, -upitch * D2R, uroll * D2R), rel=1e-6)
    assert (x, y, z) == pytest.approx((ux * 10, uy * 10, uz * 10), rel=1e-6)
    # In words: yaw + = left, pitch + = down, roll + = left, z + = away from the screen.
    assert yaw > 0 and pitch < 0 and roll < 0 and (x, y, z) == pytest.approx((10, 20, -40))
    raw_yaw, raw_pitch = vals[9:11]
    assert (raw_yaw, raw_pitch) == pytest.approx((10 * D2R, 5 * D2R), rel=1e-6)   # opentrack: raw pitch not negated
    assert vals[1:3] == (100, 250)                                                # CamWidth, CamHeight


def test_pitch_near_90_degrees_avoids_the_falcon_bms_bump():
    heap = FTHeap()
    FreeTrackWriter(heap, {}).write(Pose(pitch=90.05))
    assert heap.data.Pitch == pytest.approx(-89.86 * D2R, rel=1e-6)


def test_data_id_counts_frames_and_a_registered_game_gets_its_key():
    heap = FTHeap()
    writer = FreeTrackWriter(heap, load_game_keys())
    assert heap.data.DataID == 1
    for _ in range(3):
        writer.write(Pose(yaw=1))
    assert heap.data.DataID == 2 and writer.game == ""        # reset to 0 on the first frame
    heap.GameID = 1006                                        # what NP_RegisterProgramProfileID does
    writer.write(Pose(yaw=1))
    assert writer.game == "DCS" and heap.GameID2 == 1006 and heap.data.DataID == 0
    # DCS's FTN_ID 0058F688FC9B0556868F00, unscrambled as opentrack's csv.cpp does.
    assert bytes(heap.table) == bytes.fromhex("9BFC88F68F865605")
    heap.GameID = 8151                                        # MSFS 2024: a V160 game, no key
    writer.write(Pose(yaw=1))
    assert writer.game == "MS Flight Simulator 2024" and bytes(heap.table) == bytes(8)
    heap.GameID = 4242424
    writer.write(Pose())
    assert writer.game == "Unknown game (4242424)" and bytes(heap.table) == bytes(8)


def test_clear_centers_the_view():
    heap = FTHeap()
    writer = FreeTrackWriter(heap, {})
    writer.write(Pose(yaw=20, z=-3))
    writer.clear()
    assert heap_bytes(heap)[3:15] == (0.0,) * 12


def test_game_list_is_bundled():
    keys = load_game_keys()
    assert len(keys) > 700 and keys["1006"][0] == "DCS"
    for name in ("NPClient.dll", "NPClient64.dll", "freetrackclient.dll", "freetrackclient64.dll", "NOTICE.txt"):
        assert (freetrack.CLIENTS_DIR / name).is_file()


@windows
def test_shared_memory_is_seen_by_another_mapping_of_the_same_name():
    writer_side = freetrack.SharedMemory()
    reader_side = freetrack.SharedMemory()
    try:
        w = FreeTrackWriter(writer_side.heap, {})
        with writer_side.lock():
            w.write(Pose(yaw=15, pitch=-4, x=2))
        assert reader_side.heap.data.Yaw == pytest.approx(15 * D2R, rel=1e-6)
        assert reader_side.heap.data.Pitch == pytest.approx(4 * D2R, rel=1e-6)
        assert reader_side.heap.data.X == pytest.approx(20)
    finally:
        writer_side.close()
        reader_side.close()


def _real_mapping_in_use() -> bool:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenFileMappingW.restype = ctypes.c_void_p
    handle = k32.OpenFileMappingW(0x0004, False, "FT_SharedMem")   # FILE_MAP_READ
    if handle:
        k32.CloseHandle(ctypes.c_void_p(handle))
    return bool(handle)


class TirData(ctypes.Structure):
    """tir_data_t from opentrack's contrib/npclient/npclient.c."""
    _fields_ = [("status", ctypes.c_short), ("frame", ctypes.c_short), ("cksum", ctypes.c_uint),
                ("roll", ctypes.c_float), ("pitch", ctypes.c_float), ("yaw", ctypes.c_float),
                ("tx", ctypes.c_float), ("ty", ctypes.c_float), ("tz", ctypes.c_float),
                ("padding", ctypes.c_float * 9)]


@windows
@pytest.mark.skipif(struct.calcsize("P") != 8, reason="needs 64-bit Python for the 64-bit client DLLs")
def test_opentracks_client_dlls_read_the_moving_pose():
    """End to end with the DLLs games load: the real shared memory name, read the
    way FreeTrack (FTGetData) and TrackIR (NP_GetData) games read it."""
    if _real_mapping_in_use():
        pytest.skip("a game, opentrack or OpenITrack is using the real FreeTrack shared memory")
    shm = freetrack.SharedMemory("FT_SharedMem", "FT_Mutext")
    try:
        writer = FreeTrackWriter(shm.heap, load_game_keys())
        ft = ctypes.WinDLL(str(freetrack.CLIENTS_DIR / "freetrackclient64.dll"))
        np_ = ctypes.WinDLL(str(freetrack.CLIENTS_DIR / "NPClient64.dll"))
        np_.NP_RegisterProgramProfileID(ctypes.c_ushort(8151))     # MSFS 2024, unencrypted
        for yaw in (5.0, 10.0, 20.0):
            pose = Pose(yaw=yaw, pitch=yaw / 2, roll=-yaw / 4, x=1, y=-2, z=3)
            with shm.lock():
                writer.write(pose)
            got = freetrack.FTData()
            assert ft.FTGetData(ctypes.byref(got))
            assert got.Yaw == pytest.approx(yaw * D2R, rel=1e-5)
            assert got.Pitch == pytest.approx(-yaw / 2 * D2R, rel=1e-5)
            assert (got.X, got.Y, got.Z) == pytest.approx((10, -20, -30))

            tir = TirData()
            assert np_.NP_GetData(ctypes.byref(tir)) == 0              # NPCLIENT_STATUS_OK
            # TrackIR units: +-16383 for +-180 degrees, +-500 mm.
            assert tir.yaw == pytest.approx(yaw / 180 * 16383, rel=1e-4)
            assert tir.pitch == pytest.approx(-yaw / 2 / 180 * 16383, rel=1e-4)
            assert tir.roll == pytest.approx(-yaw / 4 / 180 * 16383, rel=1e-4)
            assert tir.tz == pytest.approx(-30 / 500 * 16383, rel=1e-4)
        assert writer.game == "MS Flight Simulator 2024"
    finally:
        shm.close()


# -- registry ---------------------------------------------------------------------------------

def _get(key):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            return winreg.QueryValueEx(k, "Path")[0]
    except FileNotFoundError:
        return None


def _key_exists(key):
    try:
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, key).Close()
        return True
    except FileNotFoundError:
        return False


def _set(key, value):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
        winreg.SetValueEx(k, "Path", 0, winreg.REG_SZ, value)


@windows
def test_registry_points_at_our_dlls_and_restores_the_previous_values(tmp_path):
    npclient, ft = freetrack.REGISTRY_KEYS       # throwaway keys (see conftest)
    assert "OpenITrackTests" in npclient
    _set(npclient, "C:\\Program Files (x86)\\NaturalPoint\\TrackIR5\\")
    redirect = RegistryRedirect(tmp_path / "clients", tmp_path / "backup.json")
    redirect.apply()
    ours = str(tmp_path / "clients") + "\\"
    assert _get(npclient) == _get(ft) == ours
    assert (tmp_path / "backup.json").is_file()
    redirect.restore()
    assert _get(npclient) == "C:\\Program Files (x86)\\NaturalPoint\\TrackIR5\\"
    assert not _key_exists(ft)                   # we created it, so it goes again
    assert not (tmp_path / "backup.json").exists()
    redirect.restore()                           # twice is harmless


@windows
def test_registry_redirect_survives_a_crash(tmp_path):
    npclient, ft = freetrack.REGISTRY_KEYS
    _set(npclient, "C:/Program Files (x86)/opentrack/modules/")
    backup = tmp_path / "backup.json"
    RegistryRedirect(tmp_path, backup).apply()   # never restored: the process "died"
    assert json.loads(backup.read_text())["keys"][npclient]["value"] == "C:/Program Files (x86)/opentrack/modules/"
    # The next run starts the direct output again: it must keep the real originals.
    again = RegistryRedirect(tmp_path, backup)
    again.apply()
    again.restore()
    assert _get(npclient) == "C:/Program Files (x86)/opentrack/modules/" and not _key_exists(ft)

    RegistryRedirect(tmp_path, backup).apply()   # crash again, next run uses opentrack instead
    freetrack.restore_stale(backup)
    assert _get(npclient) == "C:/Program Files (x86)/opentrack/modules/" and not _key_exists(ft)
    assert not backup.exists()


@windows
def test_registry_restore_leaves_a_newer_value_alone(tmp_path):
    npclient, _ = freetrack.REGISTRY_KEYS
    redirect = RegistryRedirect(tmp_path, tmp_path / "backup.json")
    redirect.apply()
    _set(npclient, "D:\\opentrack\\")            # opentrack started meanwhile and took over
    redirect.restore()
    assert _get(npclient) == "D:\\opentrack\\"


@windows
def test_direct_output_end_to_end(no_real_game_output):
    out = outputs.build_outputs(OutputConfig(game="direct"))
    try:
        assert len(out) == 1 and isinstance(out[0], outputs.DirectOutput)
        npclient, ft = freetrack.REGISTRY_KEYS
        assert _get(npclient) == _get(ft) == str(freetrack.CLIENTS_DIR) + "\\"
        reader = freetrack.SharedMemory()
        out[0].send(Pose(yaw=30))
        assert reader.heap.data.Yaw == pytest.approx(30 * D2R, rel=1e-6)
    finally:
        for o in out:
            o.close()
    assert reader.heap.data.Yaw == 0.0           # view centered on close
    reader.close()
    assert not _key_exists(freetrack.REGISTRY_KEYS[0])


# -- config -------------------------------------------------------------------------------------

def test_game_output_config_and_old_opentrack_setting():
    assert parse_config("").output.game == "direct"
    assert parse_config("[output]\ngame = 'opentrack'\n").output.game == "opentrack"
    assert parse_config("[output]\nopentrack = true\n").output.game == "opentrack"    # older config.toml
    assert parse_config("[output]\nopentrack = false\n").output.game == "off"
    assert parse_config("[output]\nopentrack = true\ngame = 'direct'\n").output.game == "direct"
    with pytest.raises(ConfigError):
        parse_config("[output]\ngame = 'trackir'\n")
