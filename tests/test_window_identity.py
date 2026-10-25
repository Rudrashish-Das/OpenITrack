"""How the window presents itself: title with the version, its own taskbar entry."""

import subprocess
import sys

import pytest

from openitrack import __version__
from openitrack.gui import APP_USER_MODEL_ID, WINDOW_TITLE

# Prints the process's taskbar identity after gui._taskbar_identity() has run.
READ_ID = """
import ctypes
from openitrack import gui
gui._taskbar_identity()
p = ctypes.c_wchar_p()
ctypes.windll.shell32.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(p))
print(p.value)
"""


def test_title_shows_the_version():
    assert WINDOW_TITLE == f"OpenITrack {__version__}"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows taskbar")
def test_process_gets_its_own_taskbar_identity():
    # A fresh process: the identity is per process and must be set before any window.
    out = subprocess.run([sys.executable, "-c", READ_ID], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == APP_USER_MODEL_ID
