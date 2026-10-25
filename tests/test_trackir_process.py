"""The stand-in TrackIR.exe the direct output keeps running (packaging/trackir_dummy).

Each test runs its own copy under a unique exe name, so a real TrackIR.exe on
this machine neither gets in the way nor is touched.
"""

import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from openitrack import freetrack, outputs
from openitrack.config import OutputConfig, parse_config
from openitrack.freetrack import TrackIRProcess, process_ids

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="TrackIR.exe is Windows only")


@pytest.fixture(scope="session")
def built_exe(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("trackir")
    subprocess.run([sys.executable, str(ROOT / "packaging" / "build.py"), "trackir", str(out)], check=True,
                   capture_output=True)
    return out / "TrackIR.exe"


@pytest.fixture
def exe(built_exe, tmp_path) -> Path:
    path = tmp_path / f"TrackIRTest{uuid.uuid4().hex[:12]}.exe"
    shutil.copy2(built_exe, path)
    yield path
    for pid in process_ids(path.name):  # only our uniquely named copies
        subprocess.run(["taskkill", "/f", "/pid", str(pid)], capture_output=True)


def wait_for(condition, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def test_starts_and_stops(exe):
    proc = TrackIRProcess(exe)
    assert proc.start()
    assert proc.start()  # already running: no second one
    assert process_ids(exe.name) == [proc.pid]
    proc.stop()
    assert proc.pid is None
    assert wait_for(lambda: not process_ids(exe.name))
    proc.stop()  # stopping twice is fine


def test_not_started_when_another_one_runs_and_that_one_is_left_alone(exe, tmp_path):
    # The other program waits for this test process, like it would for OpenITrack.
    other = subprocess.Popen([str(exe), str(os.getpid())])
    try:
        assert wait_for(lambda: process_ids(exe.name) == [other.pid])
        ours = tmp_path / "ours"
        ours.mkdir()
        shutil.copy2(exe, ours / exe.name)
        proc = TrackIRProcess(ours / exe.name)
        assert not proc.start()
        assert proc.pid is None
        proc.stop()
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_quits_when_openitrack_dies(exe):
    script = ("import sys, time\nfrom pathlib import Path\nfrom openitrack.freetrack import TrackIRProcess\n"
              "p = TrackIRProcess(Path(sys.argv[1]))\nassert p.start()\nprint(p.pid, flush=True)\ntime.sleep(60)\n")
    parent = subprocess.Popen([sys.executable, "-c", script, str(exe)], stdout=subprocess.PIPE, text=True,
                              cwd=ROOT)
    try:
        pid = int(parent.stdout.readline())
        assert process_ids(exe.name) == [pid]
    finally:
        parent.kill()  # like a crash or Task Manager's End task: no clean-up runs
        parent.wait()
    assert wait_for(lambda: not process_ids(exe.name))


def test_quits_at_once_without_a_parent(exe):
    assert subprocess.run([str(exe)], timeout=10).returncode == 2
    assert subprocess.run([str(exe), "2147483647"], timeout=10).returncode == 1  # never a process ID


def test_direct_output_runs_it_while_on(exe, monkeypatch):
    monkeypatch.setattr(freetrack, "TRACKIR_EXE", exe)
    out = outputs.build_outputs(OutputConfig(game="direct"))
    assert wait_for(lambda: len(process_ids(exe.name)) == 1)
    for o in out:
        o.close()
    assert wait_for(lambda: not process_ids(exe.name))


def test_direct_output_without_it(exe, monkeypatch):
    monkeypatch.setattr(freetrack, "TRACKIR_EXE", exe)
    out = outputs.build_outputs(OutputConfig(game="direct", trackir_process=False))
    try:
        time.sleep(0.3)
        assert not process_ids(exe.name)
    finally:
        for o in out:
            o.close()


def test_trackir_process_setting():
    assert parse_config("").output.trackir_process is True
    assert parse_config("[output]\ntrackir_process = false\n").output.trackir_process is False
