"""Phone server: pairing, encrypted sessions and the attacks they must stop."""

import asyncio
import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

import aiohttp
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from openitrack import pake
from openitrack.config import SourceConfig
from openitrack.sources import phone as ph
from openitrack.sources.phone import PhoneSource
from openitrack.webassets import FILES
from test_core import eye_points, matrix

ROOT = Path(__file__).resolve().parent.parent
CLOSED = (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def phone(tmp_path):
    model = tmp_path / "model.task"
    model.write_bytes(b"x" * 10)
    vendor = tmp_path / "vendor"
    for rel in FILES:  # stand-in files: serving is tested here, MediaPipe itself isn't
        (vendor / rel).parent.mkdir(parents=True, exist_ok=True)
        (vendor / rel).write_text("// vendor")
    src = PhoneSource(SourceConfig(type="phone", phone_port=free_port()), model,
                      config_dir=tmp_path, web_assets=vendor, advertise=False)
    src.requests = []
    src.on_pair_request = lambda name, ip: src.requests.append(name)
    src.start()
    yield src
    src.close()


def tls():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def base(src):
    return f"https://127.0.0.1:{src.port}"


def wss(src):
    return f"wss://127.0.0.1:{src.port}"


def pose_message(yaw=12.0):
    return {"t": 1000.0, "face": 1, "w": 1, "h": 1, "fps": 30,
            "m": matrix(yaw).T.reshape(-1).tolist(),
            "g": [c for p in eye_points() for c in p], "b": [0.0, 0.0]}


async def type_code_later(src, code, delay=0.3):
    await asyncio.sleep(delay)
    return await asyncio.to_thread(src.submit_code, code)


async def pair(session, src, code_typed=None):
    """A Python 'phone' pairing; returns (device id, secret, PC message)."""
    code = pake.new_code()
    a = pake.PartyA(code)
    async with session.ws_connect(wss(src) + "/pair", ssl=tls()) as ws:
        await ws.send_str(json.dumps({"t": "pair1", "pA": a.message.hex(), "name": "Py phone"}))
        typing = asyncio.create_task(type_code_later(src, code_typed or code))
        msg = json.loads((await ws.receive(timeout=10)).data)
        assert msg["t"] == "pair2"
        keys = a.finish(bytes.fromhex(msg["pB"]))
        if keys.confirm_b() != bytes.fromhex(msg["cB"]):
            await ws.send_str(json.dumps({"t": "pair_fail"}))
            return None, None, await typing
        secret = ph.device_secret(keys.ke)
        await ws.send_str(json.dumps({"t": "pair3", "cA": keys.confirm_a().hex()}))
        done = json.loads((await ws.receive(timeout=10)).data)
        assert done["t"] == "paired"
        assert ph.paired_mac(secret, done["id"]).hex() == done["mac"]
        return done["id"], secret, await typing


async def open_session(session, src, device_id, secret):
    ws = await session.ws_connect(wss(src) + "/ws", ssl=tls())
    nc = os.urandom(16)
    await ws.send_str(json.dumps({"t": "hello", "id": device_id, "nc": nc.hex()}))
    msg = json.loads((await ws.receive(timeout=5)).data)
    if msg["t"] != "challenge":
        return ws, msg, None
    ns = bytes.fromhex(msg["ns"])
    k_up, k_down = ph.session_keys(secret, device_id, nc, ns)
    assert ph.server_proof(k_down, nc, ns).hex() == msg["proof"]  # the PC proved itself
    return ws, msg, AESGCM(k_up)


def frame(aead, ctr, obj):
    c = ctr.to_bytes(8, "big")
    return c + aead.encrypt(b"\x00" * 4 + c, json.dumps(obj).encode(), ph.POSE_AAD)


# -- pairing ----------------------------------------------------------------------------

def test_pair_then_stream_encrypted(phone):
    async def go():
        async with aiohttp.ClientSession() as s:
            dev, secret, msg = await pair(s, phone)
            assert msg.startswith("Paired 'Py phone'")
            ws, _, aead = await open_session(s, phone, dev, secret)
            await ws.send_bytes(frame(aead, 1, pose_message(12.0)))
            await asyncio.sleep(0.2)
            await ws.close()
    asyncio.run(go())
    assert phone.requests == ["Py phone"]
    assert [d.name for d in phone.devices.list()] == ["Py phone"]
    raw = json.loads((phone.config_dir / "paired_devices.json").read_text())
    assert raw["devices"][0]["secret"].startswith("dpapi:")  # encrypted at rest
    assert phone._seq >= 1 and phone._latest.yaw == pytest.approx(12.0)


def test_wrong_code_does_not_pair(phone):
    async def go():
        async with aiohttp.ClientSession() as s:
            return await pair(s, phone, code_typed="ZZZZZZ")
    dev, secret, msg = asyncio.run(go())
    assert dev is None and "didn't match" in msg
    assert phone.devices.list() == []


def test_code_entry_validation(phone):
    assert "isn't a pairing code" in phone.submit_code("hello")
    assert "No phone is waiting" in phone.submit_code("ABCDEF")


def test_impostor_waiting_alongside_never_pairs(phone):
    """The real phone and an impostor wait together; only the real one pairs, and
    the impostor staying silent doesn't hold the pairing up until the timeout."""
    async def go():
        async with aiohttp.ClientSession() as s:
            impostor = pake.PartyA("QQQQQQ")
            async with s.ws_connect(wss(phone) + "/pair", ssl=tls()) as evil:
                await evil.send_str(json.dumps({"t": "pair1", "pA": impostor.message.hex(), "name": "Evil"}))
                await asyncio.sleep(0.1)
                started = time.monotonic()
                dev, secret, msg = await pair(s, phone)
                took = time.monotonic() - started
                assert took < ph.PAIR_REPLY_TIMEOUT / 3, f"pairing waited {took:.1f}s for the silent impostor"
                m = json.loads((await evil.receive(timeout=5)).data)
                # The impostor got the PC's SPAKE2 reply but can't confirm it.
                keys = impostor.finish(bytes.fromhex(m["pB"]))
                assert keys.confirm_b().hex() != m["cB"]
                return dev, msg
    dev, msg = asyncio.run(go())
    assert dev and "Py phone" in msg
    assert [d.name for d in phone.devices.list()] == ["Py phone"]


# -- sessions ---------------------------------------------------------------------------

def test_unknown_and_revoked_devices_are_refused(phone):
    async def go():
        async with aiohttp.ClientSession() as s:
            _, msg, _ = await open_session(s, phone, "deadbeefdeadbeef", b"\x00" * 32)
            assert msg["t"] == "unknown"
            dev, secret, _ = await pair(s, phone)
            phone.devices.revoke(dev[:4])
            _, msg, _ = await open_session(s, phone, dev, secret)
            assert msg["t"] == "unknown"
    asyncio.run(go())


def test_forged_tampered_and_replayed_frames(phone):
    async def go():
        async with aiohttp.ClientSession() as s:
            dev, secret, _ = await pair(s, phone)

            # Knowing the device id but not its secret: the first frame is rejected.
            ws = await s.ws_connect(wss(phone) + "/ws", ssl=tls())
            await ws.send_str(json.dumps({"t": "hello", "id": dev, "nc": os.urandom(16).hex()}))
            await ws.receive(timeout=5)
            await ws.send_bytes(frame(AESGCM(os.urandom(32)), 1, pose_message(45.0)))
            assert (await ws.receive(timeout=5)).type in CLOSED

            ws, _, aead = await open_session(s, phone, dev, secret)
            await ws.send_bytes(frame(aead, 5, pose_message(10.0)))
            await asyncio.sleep(0.1)
            seq = phone._seq
            await ws.send_bytes(frame(aead, 5, pose_message(30.0)))   # replayed counter
            await ws.send_bytes(frame(aead, 3, pose_message(30.0)))   # older counter
            await asyncio.sleep(0.1)
            assert phone._seq == seq and phone._latest.yaw == pytest.approx(10.0)
            bad = bytearray(frame(aead, 6, pose_message(30.0)))
            bad[-1] ^= 1                                               # one flipped bit
            await ws.send_bytes(bytes(bad))
            assert (await ws.receive(timeout=5)).type in CLOSED
            assert phone._latest.yaw == pytest.approx(10.0)
    asyncio.run(go())


def test_repeated_failures_get_blocked(phone):
    async def go():
        async with aiohttp.ClientSession() as s:
            for _ in range(ph.MAX_FAILURES):
                _, msg, _ = await open_session(s, phone, "0" * 16, b"\x00" * 32)
                assert msg["t"] == "unknown"
            with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                await s.ws_connect(wss(phone) + "/ws", ssl=tls())
            assert exc.value.status == 429
    asyncio.run(go())


# -- web hardening ------------------------------------------------------------------------

def test_static_files_headers_and_host_checks(phone):
    async def go():
        async with aiohttp.ClientSession() as s:
            async with s.get(base(phone) + "/", ssl=tls()) as r:
                assert r.status == 200 and "OpenITrack Phone" in await r.text()
                csp = r.headers["Content-Security-Policy"]
                assert "connect-src 'self'" in csp and "default-src 'none'" in csp
                assert r.headers["Referrer-Policy"] == "no-referrer"
            for path in ("/app.js", "/frame.js", "/overlay.js", "/link.js", "/pake.js", "/vendor/vision_bundle.mjs"):
                async with s.get(base(phone) + path, ssl=tls()) as r:
                    assert r.status == 200 and "javascript" in r.headers["Content-Type"]
            async with s.get(base(phone) + "/vendor/wasm/vision_wasm_internal.wasm", ssl=tls()) as r:
                assert r.headers["Content-Type"] == "application/wasm"
            async with s.get(base(phone) + "/icon.png", ssl=tls()) as r:
                assert r.status == 200 and (await r.read())[:4] == b"\x89PNG"
            for path in ("/vendor/..%2F..%2Fconfig.toml", "/vendor/secret.txt", "/paired_devices.json"):
                async with s.get(base(phone) + path, ssl=tls()) as r:
                    assert r.status == 404, path
            # DNS-rebinding style request carrying a foreign Host header.
            async with s.get(base(phone) + "/", ssl=tls(), headers={"Host": "evil.example"}) as r:
                assert r.status == 421
            # A page on another site can't open the socket from a victim's browser.
            with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                await s.ws_connect(wss(phone) + "/pair", ssl=tls(), headers={"Origin": "https://evil.example"})
            assert exc.value.status == 403
    asyncio.run(go())


# -- the real phone code (web/link.js) under Node ---------------------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_real_phone_js_pairs_reconnects_and_streams(phone, tmp_path):
    env = {**os.environ, "NODE_TLS_REJECT_UNAUTHORIZED": "0", "NODE_NO_WARNINGS": "1"}
    script = str(ROOT / "tests" / "js" / "phone_client.mjs")
    store = str(tmp_path / "phone_creds.json")

    proc = subprocess.Popen(["node", script, wss(phone), "5", "--store", store],
                            stdout=subprocess.PIPE, text=True, env=env)
    events, reply = [], {}

    def read():
        for line in proc.stdout:
            ev = json.loads(line)
            events.append(ev)
            if ev.get("state") == "pairing" and "msg" not in reply:
                # The phone reports "pairing" as it sends its SPAKE2 message; on a busy
                # machine the server may not have read it yet. A person can't type that
                # fast, so retry briefly instead of failing on the race.
                deadline = time.monotonic() + 10
                msg = phone.submit_code(ev["code"].lower())  # typing is case-insensitive
                while msg.startswith("No phone is waiting") and time.monotonic() < deadline:
                    time.sleep(0.1)
                    msg = phone.submit_code(ev["code"].lower())
                reply["msg"] = msg

    t = threading.Thread(target=read)
    t.start()
    assert proc.wait(40) == 0, events
    t.join(5)
    states = [e.get("state") for e in events]
    assert "pairing" in states and "paired" in states and "connected" in states, events
    assert reply["msg"].startswith("Paired 'Test phone'")
    assert {"sent": 5} in events
    deadline = time.monotonic() + 2
    while phone._seq < 5 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert phone._latest is not None and phone._latest.yaw == pytest.approx(14.0, abs=0.01)

    # Second run: stored credentials, no pairing, straight to an encrypted session.
    seq = phone._seq
    out = subprocess.run(["node", script, wss(phone), "3", "--store", store],
                         capture_output=True, text=True, env=env, timeout=40)
    states2 = [json.loads(line).get("state") for line in out.stdout.splitlines()]
    assert out.returncode == 0 and "pairing" not in states2 and "connected" in states2, out.stdout
    assert phone._seq >= seq + 3
