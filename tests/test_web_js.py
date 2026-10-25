"""Run the browser-side checks under Node: SPAKE2 (RFC 9382 vectors), the pose message
and the tracking overlay."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from openitrack import pake
from openitrack.sources.phone import parse_message

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_pake_js_matches_rfc_and_python():
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "pake.test.mjs")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert f'"w_AB23CD":"{pake.password_scalar("ab2-3cd"):064x}"' in out.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_rounded_phone_message_gives_the_same_pose():
    """web/frame.js sends rounded values; the PC must see the same pose as with full precision."""
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "frame.test.mjs")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    sent, full = parse_message(res["sent"], 1.0), parse_message(res["full"], 1.0)
    for name in ("yaw", "pitch", "roll"):
        assert getattr(sent, name) == pytest.approx(getattr(full, name), abs=1e-3)  # degrees
    for name in ("x", "y", "z"):
        assert getattr(sent, name) == pytest.approx(getattr(full, name), abs=1e-4)  # cm
    for name in ("gaze_x", "gaze_y", "blink"):
        assert getattr(sent, name) == pytest.approx(getattr(full, name), abs=1e-4)
    assert abs(sent.yaw) > 5  # the fake head really is turned, so this compares something
    assert res["sentBytes"] < 0.6 * res["fullBytes"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_phone_overlay_draws_the_face_and_clears_without_one():
    """web/overlay.js: face mesh, eye points, iris rings and head direction; nothing stale."""
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "overlay.test.mjs")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
