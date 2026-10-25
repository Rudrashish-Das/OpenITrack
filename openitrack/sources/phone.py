"""Phone source: the phone opens a page served by the PC, runs MediaPipe in its
own browser and streams head pose to the PC. The PC only filters and outputs,
so it uses almost no CPU.

Security model (web/link.js is the phone side):
- Pairing: the phone shows a 6-character code and the PC user types it in.
  SPAKE2 (RFC 9382) proves both sides know the code without sending it, so
  others on the Wi-Fi can't learn it, brute-force it offline or hijack the
  pairing. Each attempt gives an impostor at most one blind guess.
- After pairing, each phone has its own 256-bit secret (DPAPI-encrypted on the
  PC, a non-extractable key on the phone). Every connection derives fresh keys,
  the PC proves itself first, and every pose is AES-GCM encrypted with a strictly
  increasing counter. Unpaired devices can't send poses, and captured traffic
  can't be read, altered or replayed.
- TLS exists only because phone browsers require HTTPS for the camera; the
  protections above don't depend on the self-signed certificate.
- Privacy: camera frames never leave the phone. The page and all of MediaPipe
  are served by this PC (no third-party requests), a strict Content Security
  Policy blocks any other connection, and only head-pose numbers are sent.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import hmac
import json
import logging
import math
import secrets
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .. import pake
from ..certs import ensure_certificate, local_ipv4_addresses
from ..config import SourceConfig, default_config_path
from ..devices import DeviceStore
from ..features import GAZE_LANDMARKS, Observation, build_observation
from ..mdns import MdnsAdvertiser
from ..webassets import FILES as VENDOR_FILES
from ..webassets import ensure_web_assets
from . import Sample

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
STALE_AFTER = 0.5            # seconds without data before we call the face lost
POSE_AAD = b"OpenITrack pose v1"
FALLBACK_PORT = 8443

# Abuse limits: generous for real use, tight for anyone probing.
MAX_CONNECTIONS = 32
MAX_CONNECTIONS_PER_IP = 4
MAX_PENDING_PAIRINGS = 8
PENDING_TTL = 5 * 60
HELLO_TIMEOUT = 5.0
PAIR_REPLY_TIMEOUT = 15.0
FAILURE_WINDOW = 60.0
MAX_FAILURES = 5             # auth failures per IP per window before it's blocked
MAX_MESSAGE = 16 * 1024

SECURITY_HEADERS = {
    # Only our own files, and the only connection allowed is back to this PC.
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self' 'wasm-unsafe-eval' 'unsafe-eval'; "
        "connect-src 'self'; img-src 'self' data: blob:; media-src 'self' blob: mediastream:; "
        "style-src 'unsafe-inline'; worker-src 'self' blob:; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(self), microphone=(), geolocation=()",
}

_STATIC = {
    "/": ("phone.html", "text/html; charset=utf-8", "no-store"),
    "/pake.js": ("pake.js", "text/javascript; charset=utf-8", "no-store"),
    "/link.js": ("link.js", "text/javascript; charset=utf-8", "no-store"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8", "no-store"),
    "/frame.js": ("frame.js", "text/javascript; charset=utf-8", "no-store"),
    "/overlay.js": ("overlay.js", "text/javascript; charset=utf-8", "no-store"),
    "/icon.png": ("icon.png", "image/png", "max-age=86400"),
}


def parse_message(msg: dict, t: float) -> Observation | None:
    """Validate a decrypted pose message and turn it into an Observation (None = no face)."""
    if not msg.get("face"):
        return None
    m = msg["m"]
    g = msg["g"]
    w, h = float(msg["w"]), float(msg["h"])
    if len(m) != 16 or len(g) != 2 * len(GAZE_LANDMARKS) or w <= 0 or h <= 0:
        raise ValueError("malformed face message")
    values = [float(v) for v in m] + [float(v) for v in g]
    if not all(math.isfinite(v) for v in values):
        raise ValueError("non-finite values")
    pts = [(g[2 * i], g[2 * i + 1]) for i in range(len(GAZE_LANDMARKS))]
    blend = None
    b = msg.get("b")
    if isinstance(b, list) and len(b) == 2:
        blend = {"eyeBlinkLeft": float(b[0]), "eyeBlinkRight": float(b[1])}
    obs = build_observation(t, m, pts, w, h, blend)
    # The phone's video turns with the screen, so rotating the phone rotates the
    # camera. The pipeline recenters when this changes; any fixed camera roll is
    # absorbed by centering. The screen angle (0/90/180/270) also catches 180 deg
    # flips; older phone pages don't send it, so fall back to the frame shape.
    obs.extra["orientation"] = _orientation(msg.get("o"), w, h)
    return obs


def _orientation(angle: object, w: float, h: float) -> int | str:
    if isinstance(angle, (int, float)) and not isinstance(angle, bool) and math.isfinite(angle):
        return round(angle / 90) % 4 * 90
    return "portrait" if h > w else "landscape"


# -- key derivations shared with web/link.js --------------------------------------
def device_secret(ke: bytes) -> bytes:
    return pake.derive(ke, b"OpenITrack device secret v1", 32)


def paired_mac(secret: bytes, device_id: str) -> bytes:
    return hmac.new(secret, b"OpenITrack paired v1|" + device_id.encode(), hashlib.sha256).digest()


def session_keys(secret: bytes, device_id: str, nc: bytes, ns: bytes) -> tuple[bytes, bytes]:
    okm = pake.derive(secret, b"OpenITrack session v1|" + device_id.encode(), 64, salt=nc + ns)
    return okm[:32], okm[32:]


def server_proof(k_down: bytes, nc: bytes, ns: bytes) -> bytes:
    return hmac.new(k_down, b"server" + nc + ns, hashlib.sha256).digest()


@dataclass
class _Pending:
    ws: object
    pa: bytes
    name: str
    ip: str
    created: float
    reply: asyncio.Future | None = None
    keys: pake.Keys | None = None


@dataclass
class _Limits:
    conns: collections.Counter = field(default_factory=collections.Counter)
    failures: dict[str, collections.deque] = field(default_factory=dict)

    def blocked(self, ip: str, now: float) -> bool:
        q = self.failures.get(ip)
        while q and now - q[0] > FAILURE_WINDOW:
            q.popleft()
        if q is not None and not q:
            del self.failures[ip]
        return bool(q) and len(q) >= MAX_FAILURES

    def fail(self, ip: str, now: float) -> None:
        # Forget addresses whose failures have all expired, so probing from many
        # addresses can't grow this table without limit.
        for old in [a for a, q in self.failures.items() if q and now - q[-1] > FAILURE_WINDOW]:
            del self.failures[old]
        self.failures.setdefault(ip, collections.deque()).append(now)


class PhoneSource:
    def __init__(self, cfg: SourceConfig, model_path: Path, config_dir: Path | None = None,
                 web_assets: Path | None = None, advertise: bool = True):
        self.cfg = cfg
        self.model_path = model_path
        self.config_dir = config_dir or default_config_path().parent
        self.devices = DeviceStore(self.config_dir)
        self.web_assets = web_assets
        self.advertise = advertise
        self._cond = threading.Condition()
        self._latest: Observation | None = None
        self._latest_t = 0.0
        self._seq = 0
        self._phone_fps = 0.0
        self._active: tuple[str, object] | None = None    # (device name, ws) streaming now
        self._pending: dict[int, _Pending] = {}            # phones showing a code
        self._attempting: list[_Pending] = []              # phones checking a typed code
        self._limits = _Limits()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._error: BaseException | None = None
        self._mdns: MdnsAdvertiser | None = None
        self._allowed_hosts: set[str] = set()
        self.on_pair_request: Callable[[str, str], None] | None = None
        self.want_frames = False
        self.port = cfg.phone_port
        self.hostname = ""
        self.urls: list[str] = []

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        (self.config_dir / "phone_token.txt").unlink(missing_ok=True)  # old URL-token scheme
        assets = self.web_assets or ensure_web_assets()
        ips = local_ipv4_addresses()
        mdns_name = f"{self.cfg.phone_hostname}.local"
        self._allowed_hosts = {"localhost", "127.0.0.1", *ips, mdns_name.lower(),
                               f"{self.cfg.phone_hostname}-{socket.gethostname()}.local".lower()}
        cert, key = ensure_certificate(self.config_dir / "tls", ips, [mdns_name])
        self._thread = threading.Thread(target=self._serve, args=(assets, cert, key),
                                        daemon=True, name="phone-server")
        self._thread.start()
        self._started.wait(15)
        if self._error:
            raise RuntimeError(f"Phone server failed to start: {self._error}") from self._error

        if self.advertise:  # after binding, so we advertise the port we actually got
            self._mdns = MdnsAdvertiser(self.cfg.phone_hostname, self.port, ips)
            self.hostname = self._mdns.start() or ""
            if self.hostname and self.hostname.lower() not in self._allowed_hosts:
                self._allowed_hosts.add(self.hostname.lower())
        suffix = "" if self.port == 443 else f":{self.port}"
        self.urls = ([f"https://{self.hostname}{suffix}"] if self.hostname else []) + \
                    [f"https://{ip}{suffix}" for ip in ips]

    def close(self) -> None:
        if self._mdns is not None:
            self._mdns.close()
        if self._loop is not None and self._stop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop.set)
            if self._thread is not None:
                self._thread.join(3.0)

    # -- pairing, driven by the PC user typing the phone's code -----------------
    @property
    def pairing_pending(self) -> bool:
        return bool(self._pending)

    def pending_requests(self) -> list[tuple[str, str]]:
        """(name, ip) of phones currently showing a pairing code."""
        return [(p.name, p.ip) for p in list(self._pending.values())]

    @property
    def active_name(self) -> str | None:
        active = self._active
        return active[0] if active else None

    def submit_code(self, code: str, timeout: float = PAIR_REPLY_TIMEOUT + 10) -> str:
        """Called from the console thread with what the user typed. Returns a message."""
        if self._loop is None:
            return "Phone server isn't running."
        fut = asyncio.run_coroutine_threadsafe(self._attempt(code), self._loop)
        try:
            return fut.result(timeout)
        except TimeoutError:
            return "The phone didn't answer in time. Check it's still on the pairing screen."

    async def _attempt(self, code: str) -> str:
        if not pake.is_valid_code(code):
            return "That isn't a pairing code (6 letters/numbers, exactly as shown on the phone)."
        now = time.monotonic()
        for cid in [c for c, p in self._pending.items() if now - p.created > PENDING_TTL]:
            self._pending.pop(cid, None)
        if not self._pending:
            return "No phone is waiting to pair. Open the link on your phone first."

        # Try the code against every waiting phone. Each phone's SPAKE2 message is
        # single-use, so an impostor waiting here gets at most one blind guess per
        # code the user types, and learns nothing about the code.
        tries = list(self._pending.values())
        self._pending.clear()
        self._attempting = tries
        loop = asyncio.get_running_loop()
        winner = None
        try:
            for p in tries:
                p.reply = loop.create_future()
                try:
                    pb, p.keys = await loop.run_in_executor(None, pake.respond_as_b, code, p.pa)
                    await p.ws.send_str(json.dumps({"t": "pair2", "pB": pb.hex(), "cB": p.keys.confirm_b().hex()}))
                except Exception:
                    if not p.reply.done():
                        p.reply.set_result(None)
            # Check confirmations as they arrive and stop at the first phone that
            # proves it knows the code: another waiting phone (an impostor, or an
            # old tab left on the pairing screen) that never answers mustn't make
            # the user wait out the whole timeout.
            waiting = {p.reply: p for p in tries}
            deadline = loop.time() + PAIR_REPLY_TIMEOUT
            while waiting and winner is None:
                done, _ = await asyncio.wait(waiting, timeout=max(0.0, deadline - loop.time()),
                                             return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    break  # timed out
                for fut in done:
                    p = waiting.pop(fut)
                    ca = fut.result()
                    if ca is not None and p.keys is not None and hmac.compare_digest(ca, p.keys.confirm_a()):
                        winner = winner or p
                    elif ca is not None:
                        # Sent a confirmation that doesn't match: not an honest phone with a typo.
                        self._limits.fail(p.ip, time.monotonic())
        finally:
            self._attempting = []  # late replies from the others are ignored

        if winner is None:
            return ("That code didn't match. Check the code on the phone (it changes after a "
                    "failed try) and type it again.")
        assert winner.keys is not None
        secret = device_secret(winner.keys.ke)
        dev = self.devices.add(winner.name, secret)
        await winner.ws.send_str(json.dumps({
            "t": "paired", "id": dev.id, "mac": paired_mac(secret, dev.id).hex(), "pc": socket.gethostname()}))
        return f"Paired '{dev.name}' (id {dev.id[:6]}). It reconnects automatically from now on."

    # -- server ------------------------------------------------------------
    def _host_ok(self, host_header: str) -> bool:
        host = host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header
        return host.strip("[]").lower() in self._allowed_hosts

    def _origin_ok(self, origin: str | None) -> bool:
        if origin is None:
            return True  # non-browser client; it can't be a victim's browser
        if not origin.startswith("https://"):
            return False
        return self._host_ok(origin[len("https://"):])

    def _serve(self, assets: Path, cert: Path, key: Path) -> None:
        from aiohttp import WSMsgType, web  # slow to import; only the server thread needs it

        limits = self._limits

        @web.middleware
        async def guard(request: web.Request, handler):
            # Unknown Host header: blocks DNS-rebinding pages from reaching us.
            if not self._host_ok(request.host or ""):
                return web.Response(status=421, text="Misdirected request")
            try:
                resp = await handler(request)
            except web.HTTPException as exc:
                exc.headers.update(SECURITY_HEADERS)
                raise
            resp.headers.update(SECURITY_HEADERS)
            return resp

        async def static(request: web.Request) -> web.StreamResponse:
            name, ctype, cache = _STATIC[request.path]
            return web.FileResponse(WEB_DIR / name, headers={"Content-Type": ctype, "Cache-Control": cache})

        async def vendor(request: web.Request) -> web.StreamResponse:
            rel = request.match_info["rel"]
            if rel not in VENDOR_FILES:  # fixed allow-list: no path traversal possible
                raise web.HTTPNotFound()
            ctype = "application/wasm" if rel.endswith(".wasm") else "text/javascript; charset=utf-8"
            return web.FileResponse(assets / rel, headers={"Content-Type": ctype,
                                                           "Cache-Control": "max-age=604800"})

        async def model(request: web.Request) -> web.StreamResponse:
            return web.FileResponse(self.model_path, headers={"Cache-Control": "max-age=86400"})

        async def open_ws(request: web.Request):
            ip = request.remote or "?"
            if not self._origin_ok(request.headers.get("Origin")):
                return web.Response(status=403, text="Forbidden origin")
            if limits.blocked(ip, time.monotonic()):
                return web.Response(status=429, text="Too many failed attempts; wait a minute")
            if sum(limits.conns.values()) >= MAX_CONNECTIONS or limits.conns[ip] >= MAX_CONNECTIONS_PER_IP:
                return web.Response(status=429, text="Too many connections")
            ws = web.WebSocketResponse(heartbeat=5.0, max_msg_size=MAX_MESSAGE)
            await ws.prepare(request)
            return ws

        async def pair_handler(request: web.Request) -> web.StreamResponse:
            ws = await open_ws(request)
            if not isinstance(ws, web.WebSocketResponse):
                return ws
            ip = request.remote or "?"
            limits.conns[ip] += 1
            cid = id(ws)
            try:
                async for msg in ws:
                    if msg.type != WSMsgType.TEXT:
                        break
                    try:
                        data = json.loads(msg.data)
                        kind = data.get("t")
                    except (ValueError, AttributeError):
                        break
                    if kind == "pair1":
                        try:
                            pa = bytes.fromhex(data["pA"])
                            pake.decode_point(pa)
                        except (KeyError, ValueError, TypeError):
                            limits.fail(ip, time.monotonic())
                            break
                        if cid not in self._pending and len(self._pending) >= MAX_PENDING_PAIRINGS:
                            await ws.send_str(json.dumps({"t": "error",
                                                          "reason": "Too many phones are pairing at once."}))
                            break
                        name = "".join(ch for ch in str(data.get("name", "")) if ch.isprintable())[:40] or "Phone"
                        is_new = cid not in self._pending
                        self._pending[cid] = _Pending(ws, pa, name, ip, time.monotonic())
                        if is_new and self.on_pair_request:
                            self.on_pair_request(name, ip)
                    elif kind in ("pair3", "pair_fail"):
                        p = next((p for p in self._attempting if p.ws is ws), None)
                        if p is not None and p.reply is not None and not p.reply.done():
                            try:
                                p.reply.set_result(bytes.fromhex(data["cA"]) if kind == "pair3" else None)
                            except (KeyError, ValueError, TypeError):
                                p.reply.set_result(None)
                    else:
                        break
            finally:
                self._pending.pop(cid, None)
                limits.conns[ip] -= 1
                await ws.close()
            return ws

        async def ws_handler(request: web.Request) -> web.StreamResponse:
            ws = await open_ws(request)
            if not isinstance(ws, web.WebSocketResponse):
                return ws
            ip = request.remote or "?"
            limits.conns[ip] += 1
            try:
                await self._session(ws, ip)
            finally:
                limits.conns[ip] -= 1
                if self._active is not None and self._active[1] is ws:
                    log.info("Phone '%s' disconnected", self._active[0])
                    self._active = None
                await ws.close()
            return ws

        app = web.Application(middlewares=[guard], client_max_size=MAX_MESSAGE)
        for path in _STATIC:
            app.router.add_get(path, static)
        app.router.add_get("/vendor/{rel:.+}", vendor)
        app.router.add_get("/face_landmarker.task", model)
        app.router.add_get("/pair", pair_handler)
        app.router.add_get("/ws", ws_handler)

        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_ctx.load_cert_chain(cert, key)

        async def main() -> None:
            self._stop = asyncio.Event()
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=1.0)
            await runner.setup()
            try:
                bound = False
                for port in dict.fromkeys([self.cfg.phone_port, FALLBACK_PORT]):
                    try:
                        await web.TCPSite(runner, "0.0.0.0", port, ssl_context=ssl_ctx).start()
                        self.port, bound = port, True
                        break
                    except OSError as exc:
                        log.info("Port %d unavailable (%s)", port, exc.strerror or exc)
                if not bound:
                    raise OSError(f"no free port for the phone server ({self.cfg.phone_port}, {FALLBACK_PORT})")
                self._started.set()
                await self._stop.wait()
            finally:
                await runner.cleanup()

        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(main())
        except BaseException as exc:  # surfaced to start()
            self._error = exc
            self._started.set()
        finally:
            loop.close()

    async def _session(self, ws, ip: str) -> None:
        from aiohttp import WSMsgType  # already loaded by _serve

        limits = self._limits
        try:
            msg = await asyncio.wait_for(ws.receive(), HELLO_TIMEOUT)
            hello = json.loads(msg.data) if msg.type == WSMsgType.TEXT else {}
            device_id = str(hello.get("id", ""))[:64]
            nc = bytes.fromhex(hello.get("nc", ""))
            if hello.get("t") != "hello" or len(nc) != 16:
                raise ValueError("bad hello")
        except (asyncio.TimeoutError, ValueError, TypeError, AttributeError):
            limits.fail(ip, time.monotonic())
            return

        loop = asyncio.get_running_loop()
        found = await loop.run_in_executor(None, self.devices.secret, device_id)
        if found is None:
            limits.fail(ip, time.monotonic())
            await ws.send_str(json.dumps({"t": "unknown"}))
            return
        dev, secret = found
        ns = secrets.token_bytes(16)
        k_up, k_down = session_keys(secret, dev.id, nc, ns)
        aead = AESGCM(k_up)
        await ws.send_str(json.dumps({"t": "challenge", "ns": ns.hex(),
                                      "proof": server_proof(k_down, nc, ns).hex()}))

        last_ctr = 0
        authenticated = False
        offset: float | None = None
        async for msg in ws:
            if msg.type != WSMsgType.BINARY or len(msg.data) < 8 + 16:
                break
            ctr = int.from_bytes(msg.data[:8], "big")
            if ctr <= last_ctr:
                continue  # replayed or reordered frame: drop it
            try:
                plain = aead.decrypt(b"\x00" * 4 + msg.data[:8], msg.data[8:], POSE_AAD)
            except InvalidTag:
                limits.fail(ip, time.monotonic())
                log.warning("Rejected a forged or tampered pose message from %s", ip)
                break
            last_ctr = ctr
            if not authenticated:
                authenticated = True
                previous, self._active = self._active, (dev.name, ws)
                if previous is not None and previous[1] is not ws:
                    try:
                        await previous[1].send_str(json.dumps({"t": "replaced"}))
                        await previous[1].close()
                    except Exception:
                        pass
                log.info("Phone '%s' connected (encrypted)", dev.name)
                await loop.run_in_executor(None, self.devices.touch, dev.id)
            try:
                data = json.loads(plain)
                now = time.monotonic()
                # Map phone timestamps onto our clock so filter timing uses the
                # real capture spacing, not network jitter.
                pt = float(data["t"]) / 1000.0
                if offset is None or pt + offset > now or now - (pt + offset) > 0.25:
                    offset = now - pt
                self._phone_fps = float(data.get("fps", 0.0))
                obs = parse_message(data, pt + offset)
            except (ValueError, KeyError, TypeError):
                continue
            with self._cond:
                self._latest = obs
                self._latest_t = now
                self._seq += 1
                self._cond.notify_all()

    # -- samples -----------------------------------------------------------
    def samples(self) -> Iterator[Sample]:
        seen = 0
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._seq != seen, timeout=0.05)
                fresh = self._seq != seen
                seen = self._seq
                obs = self._latest
                age = time.monotonic() - self._latest_t
            if fresh:
                status = f"phone {self._phone_fps:.0f} fps" if self._phone_fps else "phone connected"
                yield Sample(obs, status=status)
            elif self._active is None:
                yield Sample(None, status="pairing" if self._pending else "waiting for phone")
            else:
                # Keep the engine ticking so face-loss handling still runs.
                yield Sample(None, status="phone connected", fresh=age > STALE_AFTER)
