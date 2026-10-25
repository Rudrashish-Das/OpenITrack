// SPAKE2 (RFC 9382), P256-SHA256-HKDF-HMAC. Browser/Node port of openitrack/pake.py.
// The phone is party A. Checked against the RFC test vectors (tests/js/pake.test.mjs).

const P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffffn;
const A_COEF = P - 3n;
const B_COEF = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604bn;
export const ORDER = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551n;
const G = [0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296n,
           0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5n];

export const ID_A = "openitrack-phone";
export const ID_B = "openitrack-pc";
export const AAD = "OpenITrack pairing v1";
export const CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789";
export const CODE_LENGTH = 6;

const enc = new TextEncoder();
const subtle = globalThis.crypto.subtle;

const mod = (a, m = P) => ((a % m) + m) % m;
function inv(a, m = P) {
  // Fermat: a^(m-2) mod m (m prime).
  return modPow(mod(a, m), m - 2n, m);
}
function modPow(b, e, m) {
  let r = 1n;
  b = mod(b, m);
  while (e > 0n) {
    if (e & 1n) r = (r * b) % m;
    b = (b * b) % m;
    e >>= 1n;
  }
  return r;
}

function onCurve(pt) {
  if (!pt) return false;
  const [x, y] = pt;
  return x >= 0n && x < P && y >= 0n && y < P && mod(y * y - (x * x * x + A_COEF * x + B_COEF)) === 0n;
}

function add(p1, p2) {
  if (!p1) return p2;
  if (!p2) return p1;
  const [x1, y1] = p1, [x2, y2] = p2;
  let lam;
  if (x1 === x2) {
    if (mod(y1 + y2) === 0n) return null;
    lam = mod((3n * x1 * x1 + A_COEF) * inv(2n * y1));
  } else {
    lam = mod((y2 - y1) * inv(x2 - x1));
  }
  const x3 = mod(lam * lam - x1 - x2);
  return [x3, mod(lam * (x1 - x3) - y1)];
}
const neg = (pt) => (pt ? [pt[0], mod(-pt[1])] : null);

function mult(k, pt) {
  let r0 = null, r1 = pt;
  for (let i = 255n; i >= 0n; i--) {
    if ((k >> i) & 1n) { r0 = add(r0, r1); r1 = add(r1, r1); }
    else { r1 = add(r0, r1); r0 = add(r0, r0); }
  }
  return r0;
}

// ---- encoding ---------------------------------------------------------------
export const hex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
export function unhex(s) {
  if (typeof s !== "string" || s.length % 2 || /[^0-9a-f]/i.test(s)) throw new Error("bad hex");
  const out = new Uint8Array(s.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(s.substr(i * 2, 2), 16);
  return out;
}
const toBytes = (n, len = 32) => unhex(n.toString(16).padStart(len * 2, "0"));
const toInt = (b) => BigInt("0x" + (hex(b) || "0"));
export function concat(...parts) {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}

export function encodePoint(pt) {
  return concat(Uint8Array.of(4), toBytes(pt[0]), toBytes(pt[1]));
}
export function decodePoint(b) {
  let pt;
  if (b.length === 65 && b[0] === 4) {
    pt = [toInt(b.slice(1, 33)), toInt(b.slice(33))];
  } else if (b.length === 33 && (b[0] === 2 || b[0] === 3)) {
    const x = toInt(b.slice(1));
    let y = modPow(mod(x * x * x + A_COEF * x + B_COEF), (P + 1n) / 4n, P);
    if (Number(y & 1n) !== (b[0] & 1)) y = P - y;
    pt = [x, y];
  } else {
    throw new Error("bad point encoding");
  }
  if (!onCurve(pt)) throw new Error("point not on curve");
  return pt;
}

const M = decodePoint(unhex("02886e2f97ace46e55ba9dd7242579f2993b64e16ef3dcab95afd497333d8fa12f"));
const N = decodePoint(unhex("03d8bbd6c639c62937b04d997f38c3770719c629d7014d49a24b4f98baa1292b49"));

// ---- primitives -----------------------------------------------------------------
export async function sha256(data) {
  return new Uint8Array(await subtle.digest("SHA-256", data));
}
export async function hkdf(ikm, info, length, salt = new Uint8Array(0)) {
  const key = await subtle.importKey("raw", ikm, "HKDF", false, ["deriveBits"]);
  return new Uint8Array(await subtle.deriveBits({ name: "HKDF", hash: "SHA-256", salt, info }, key, length * 8));
}
export async function hmac(key, data) {
  const k = await subtle.importKey("raw", key, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return new Uint8Array(await subtle.sign("HMAC", k, data));
}
export function equalBytes(a, b) {
  if (a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a[i] ^ b[i];
  return d === 0;
}
function randomScalar() {
  // Rejection sampling for a uniform scalar in [1, ORDER).
  for (;;) {
    const k = toInt(globalThis.crypto.getRandomValues(new Uint8Array(32)));
    if (k > 0n && k < ORDER) return k;
  }
}

export function normalizeCode(code) {
  return code.toUpperCase().replace(/[^A-Z0-9]/g, "");
}
export function newCode() {
  const out = [];
  while (out.length < CODE_LENGTH) {
    const [b] = globalThis.crypto.getRandomValues(new Uint8Array(1));
    if (b < 248) out.push(CODE_ALPHABET[b % CODE_ALPHABET.length]); // 248 = 8*31, unbiased
  }
  return out.join("");
}
export async function passwordScalar(code) {
  const d = await sha256(enc.encode("OpenITrack SPAKE2 w v1|" + normalizeCode(code)));
  return toInt(d) % ORDER;
}

function lenPrefixed(...parts) {
  const chunks = [];
  for (const p of parts) {
    const len = new Uint8Array(8);
    new DataView(len.buffer).setBigUint64(0, BigInt(p.length), true);
    chunks.push(len, p);
  }
  return concat(...chunks);
}

async function keySchedule(idA, idB, pA, pB, K, w, aad) {
  const tt = lenPrefixed(enc.encode(idA), enc.encode(idB), encodePoint(pA), encodePoint(pB),
                         encodePoint(K), toBytes(w));
  const digest = await sha256(tt);
  const ke = digest.slice(0, 16), ka = digest.slice(16);
  const kc = await hkdf(ka, concat(enc.encode("ConfirmationKeys"), enc.encode(aad)), 32);
  const kcA = kc.slice(0, 16), kcB = kc.slice(16);
  return {
    ke, kcA, kcB, tt,
    confirmA: () => hmac(kcA, tt),
    confirmB: () => hmac(kcB, tt),
  };
}

/** Phone side. `opts` lets tests inject the RFC vectors' x and w. */
export async function startA(code, opts = {}) {
  const w = opts.w ?? await passwordScalar(code);
  const x = opts.x ?? randomScalar();
  const idA = opts.idA ?? ID_A, idB = opts.idB ?? ID_B, aad = opts.aad ?? AAD;
  const pA = add(mult(x, G), mult(w, M));
  return {
    message: encodePoint(pA),
    async finish(pBBytes) {
      const pB = decodePoint(pBBytes);
      const K = mult(x, add(pB, neg(mult(w, N))));
      if (!K) throw new Error("degenerate key");
      return keySchedule(idA, idB, pA, pB, K, w, aad);
    },
  };
}

/** PC side, only used to cross-check against the RFC vectors. */
export async function respondB(w, pABytes, opts = {}) {
  const y = opts.y ?? randomScalar();
  const idA = opts.idA ?? ID_A, idB = opts.idB ?? ID_B, aad = opts.aad ?? AAD;
  const pA = decodePoint(pABytes);
  const pB = add(mult(y, G), mult(w, N));
  const K = mult(y, add(pA, neg(mult(w, M))));
  return { message: encodePoint(pB), keys: await keySchedule(idA, idB, pA, pB, K, w, aad) };
}
