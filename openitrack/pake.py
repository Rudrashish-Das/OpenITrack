"""SPAKE2 (RFC 9382), ciphersuite P256-SHA256-HKDF-HMAC, for phone pairing.

The phone shows a short random code and the PC user types it in. SPAKE2 turns
that low-entropy code into a strong shared key without ever sending the code:
an eavesdropper learns nothing, and an active attacker gets exactly one online
guess per attempt (no offline brute force). The phone is party A, the PC is B.

The browser side (web/pake.js) implements the same thing; both are checked
against the RFC's test vectors.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# NIST P-256
P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A_COEF = P - 3
B_COEF = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
N_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
     0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)

# RFC 9382 section 6 seeds for P-256 (compressed SEC1).
M_HEX = "02886e2f97ace46e55ba9dd7242579f2993b64e16ef3dcab95afd497333d8fa12f"
N_HEX = "03d8bbd6c639c62937b04d997f38c3770719c629d7014d49a24b4f98baa1292b49"

ID_A = b"openitrack-phone"
ID_B = b"openitrack-pc"
AAD = b"OpenITrack pairing v1"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L look-alikes
CODE_LENGTH = 6

Point = tuple[int, int] | None  # None = point at infinity


def _on_curve(pt: Point) -> bool:
    if pt is None:
        return False
    x, y = pt
    return 0 <= x < P and 0 <= y < P and (y * y - (x * x * x + A_COEF * x + B_COEF)) % P == 0


def point_add(p1: Point, p2: Point) -> Point:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % P == 0:
            return None
        lam = (3 * x1 * x1 + A_COEF) * pow(2 * y1, -1, P) % P
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (lam * lam - x1 - x2) % P
    return x3, (lam * (x1 - x3) - y1) % P


def point_neg(pt: Point) -> Point:
    return None if pt is None else (pt[0], (-pt[1]) % P)


def scalar_mult(k: int, pt: Point) -> Point:
    # Montgomery ladder (same add/double pattern for every bit). Python ints are
    # not constant-time; acceptable here because every pairing uses a fresh,
    # short-lived code and a fresh ephemeral scalar.
    r0, r1 = None, pt
    for i in reversed(range(256)):
        if (k >> i) & 1:
            r0, r1 = point_add(r0, r1), point_add(r1, r1)
        else:
            r1, r0 = point_add(r0, r1), point_add(r0, r0)
    return r0


def encode_point(pt: Point) -> bytes:
    assert pt is not None
    return b"\x04" + pt[0].to_bytes(32, "big") + pt[1].to_bytes(32, "big")


def decode_point(data: bytes) -> Point:
    """Uncompressed or compressed SEC1; raises ValueError for anything invalid."""
    if len(data) == 65 and data[0] == 4:
        pt = (int.from_bytes(data[1:33], "big"), int.from_bytes(data[33:], "big"))
    elif len(data) == 33 and data[0] in (2, 3):
        x = int.from_bytes(data[1:], "big")
        y2 = (x * x * x + A_COEF * x + B_COEF) % P
        y = pow(y2, (P + 1) // 4, P)
        if (y & 1) != (data[0] & 1):
            y = P - y
        pt = (x, y)
    else:
        raise ValueError("bad point encoding")
    if not _on_curve(pt):
        raise ValueError("point not on curve")
    return pt


M = decode_point(bytes.fromhex(M_HEX))
N = decode_point(bytes.fromhex(N_HEX))


def normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def is_valid_code(code: str) -> bool:
    c = normalize_code(code)
    return len(c) == CODE_LENGTH and all(ch in CODE_ALPHABET for ch in c)


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def password_scalar(code: str) -> int:
    """w from the pairing code. No slow hash needed: SPAKE2 prevents offline guessing."""
    digest = hashlib.sha256(b"OpenITrack SPAKE2 w v1|" + normalize_code(code).encode()).digest()
    return int.from_bytes(digest, "big") % N_ORDER


def _len_prefixed(*parts: bytes) -> bytes:
    return b"".join(struct.pack("<Q", len(p)) + p for p in parts)


@dataclass
class Keys:
    ke: bytes      # shared secret output
    kc_a: bytes    # A's confirmation key
    kc_b: bytes
    tt: bytes

    def confirm_a(self) -> bytes:
        return hmac.new(self.kc_a, self.tt, hashlib.sha256).digest()

    def confirm_b(self) -> bytes:
        return hmac.new(self.kc_b, self.tt, hashlib.sha256).digest()


def key_schedule(id_a: bytes, id_b: bytes, pa: Point, pb: Point, k: Point, w: int,
                 aad: bytes = AAD) -> Keys:
    tt = _len_prefixed(id_a, id_b, encode_point(pa), encode_point(pb), encode_point(k),
                       w.to_bytes(32, "big"))
    digest = hashlib.sha256(tt).digest()
    ke, ka = digest[:16], digest[16:]
    kc = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
              info=b"ConfirmationKeys" + aad).derive(ka)
    return Keys(ke=ke, kc_a=kc[:16], kc_b=kc[16:], tt=tt)


def _random_scalar() -> int:
    return secrets.randbelow(N_ORDER - 1) + 1


def respond_as_b(code: str, pa_bytes: bytes, y: int | None = None,
                 id_a: bytes = ID_A, id_b: bytes = ID_B, aad: bytes = AAD) -> tuple[bytes, Keys]:
    """PC side: given the phone's pA and the typed code, return (pB, keys)."""
    return _respond(password_scalar(code), decode_point(pa_bytes), y, id_a, id_b, aad)


def _respond(w: int, pa: Point, y: int | None, id_a: bytes, id_b: bytes, aad: bytes) -> tuple[bytes, Keys]:
    y = y if y is not None else _random_scalar()
    pb = point_add(scalar_mult(y, G), scalar_mult(w, N))
    k = scalar_mult(y, point_add(pa, point_neg(scalar_mult(w, M))))
    if k is None:
        raise ValueError("degenerate key")
    return encode_point(pb), key_schedule(id_a, id_b, pa, pb, k, w, aad)


class PartyA:
    """Phone side (used by tests and tools; the real phone runs web/pake.js)."""

    def __init__(self, code: str, x: int | None = None, w: int | None = None,
                 id_a: bytes = ID_A, id_b: bytes = ID_B, aad: bytes = AAD):
        self.w = w if w is not None else password_scalar(code)
        self.x = x if x is not None else _random_scalar()
        self.pa = point_add(scalar_mult(self.x, G), scalar_mult(self.w, M))
        self.ids = (id_a, id_b, aad)

    @property
    def message(self) -> bytes:
        return encode_point(self.pa)

    def finish(self, pb_bytes: bytes) -> Keys:
        pb = decode_point(pb_bytes)
        k = scalar_mult(self.x, point_add(pb, point_neg(scalar_mult(self.w, N))))
        if k is None:
            raise ValueError("degenerate key")
        id_a, id_b, aad = self.ids
        return key_schedule(id_a, id_b, self.pa, pb, k, self.w, aad)


def respond_with_scalar(w: int, pa_bytes: bytes, y: int | None = None,
                        id_a: bytes = ID_A, id_b: bytes = ID_B, aad: bytes = AAD) -> tuple[bytes, Keys]:
    return _respond(w, decode_point(pa_bytes), y, id_a, id_b, aad)


def derive(secret: bytes, info: bytes, length: int, salt: bytes | None = None) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(secret)
