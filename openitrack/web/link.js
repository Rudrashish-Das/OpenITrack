// Phone <-> PC secure link. Mirrors openitrack/sources/phone.py.
//
// Pairing (/pair): the phone makes a random 6-character code, shows it, and runs
// SPAKE2 with it. The code itself is never sent. When the PC user types the
// same code, both sides end up with the same key; a wrong code (or a
// man-in-the-middle) makes the confirmation MACs fail. The resulting device
// secret is kept as a non-extractable WebCrypto key.
//
// Sessions (/ws): every connection derives fresh keys from the device secret and
// two random nonces. The PC proves it knows the secret before the phone sends
// anything, and every pose is AES-GCM encrypted with a strictly increasing
// counter, so nothing can be read, altered or replayed on the network.

import * as pake from "./pake.js";

const enc = new TextEncoder();
const subtle = globalThis.crypto.subtle;
const POSE_AAD = enc.encode("OpenITrack pose v1");
const CODE_LIFETIME_MS = 5 * 60 * 1000;

function random(n) {
  return globalThis.crypto.getRandomValues(new Uint8Array(n));
}

export function deviceLabel(ua = globalThis.navigator?.userAgent ?? "") {
  if (/iPad/.test(ua)) return "iPad";
  if (/iPhone/.test(ua)) return "iPhone";
  if (/Android/.test(ua)) return /Mobile/.test(ua) ? "Android phone" : "Android tablet";
  return "Phone";
}

export class PhoneLink {
  /**
   * @param {object} o
   * @param {string} o.base       e.g. "wss://openitrack.local"
   * @param {object} o.store      { get(): creds|null, set(creds), clear() }
   * @param {(state: string, detail?: object) => void} o.onState
   * @param {string} [o.name]     label shown on the PC
   */
  constructor({ base, store, onState, name }) {
    this.base = base;
    this.store = store;
    this.onState = onState;
    this.name = name ?? deviceLabel();
    this.ws = null;
    this.ready = false;
    this.counter = 0n;
    this.stopped = false;
    this._retry = 1000;
    this._timer = null;
  }

  async start() {
    this.stopped = false;
    const creds = await this.store.get();
    if (creds) this._connect(creds);
    else this._pair();
  }

  stop() {
    this.stopped = true;
    clearTimeout(this._timer);
    this.ready = false;
    if (this.ws) this.ws.close();
  }

  async forget() {
    this.stop();
    await this.store.clear();
  }

  _schedule(fn, ms) {
    clearTimeout(this._timer);
    if (!this.stopped) this._timer = setTimeout(fn, ms);
  }

  // ---- pairing --------------------------------------------------------------
  _pair() {
    this.onState("pairing-connecting");
    const ws = (this.ws = new WebSocket(this.base + "/pair"));
    let party = null, secret = null, done = false, codeTimer = null;

    const newCode = async (reason) => {
      clearTimeout(codeTimer);
      const code = pake.newCode();
      party = await pake.startA(code);
      if (ws.readyState !== 1) return;
      ws.send(JSON.stringify({ t: "pair1", pA: pake.hex(party.message), name: this.name }));
      this.onState("pairing", { code, reason });
      codeTimer = setTimeout(() => newCode("expired"), CODE_LIFETIME_MS);
    };

    ws.onopen = () => { this._retry = 1000; newCode(); };
    ws.onmessage = async (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.t === "pair2" && party) {
        const p = party;
        party = null; // one attempt per SPAKE2 message
        let keys;
        try { keys = await p.finish(pake.unhex(msg.pB)); } catch { return newCode("error"); }
        if (!pake.equalBytes(await keys.confirmB(), pake.unhex(msg.cB))) {
          ws.send(JSON.stringify({ t: "pair_fail" }));
          return newCode("mismatch");
        }
        secret = await pake.hkdf(keys.ke, enc.encode("OpenITrack device secret v1"), 32);
        ws.send(JSON.stringify({ t: "pair3", cA: pake.hex(await keys.confirmA()) }));
      } else if (msg.t === "paired" && secret) {
        const expect = await pake.hmac(secret, enc.encode("OpenITrack paired v1|" + msg.id));
        if (!pake.equalBytes(expect, pake.unhex(msg.mac))) return newCode("error");
        const key = await subtle.importKey("raw", secret, "HKDF", false, ["deriveBits"]);
        secret.fill(0);
        const creds = { id: msg.id, key, pc: String(msg.pc || "PC") };
        await this.store.set(creds);
        done = true;
        clearTimeout(codeTimer);
        ws.close();
        this.onState("paired", { pc: creds.pc });
        this._connect(creds);
      } else if (msg.t === "error") {
        this.onState("error", { reason: msg.reason });
      }
    };
    ws.onclose = () => {
      clearTimeout(codeTimer);
      if (!done) {
        this.onState("pairing-connecting");
        this._schedule(() => this._pair(), this._backoff());
      }
    };
  }

  // ---- session ----------------------------------------------------------------
  _connect(creds) {
    this.onState("connecting", { pc: creds.pc });
    this.ready = false;
    const ws = (this.ws = new WebSocket(this.base + "/ws"));
    ws.binaryType = "arraybuffer";
    const nc = random(16);
    let unknown = false;

    ws.onopen = () => ws.send(JSON.stringify({ t: "hello", id: creds.id, nc: pake.hex(nc) }));
    ws.onmessage = async (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.t === "challenge") {
        const ns = pake.unhex(msg.ns);
        const okm = new Uint8Array(await subtle.deriveBits(
          { name: "HKDF", hash: "SHA-256", salt: pake.concat(nc, ns),
            info: enc.encode("OpenITrack session v1|" + creds.id) }, creds.key, 512));
        const kDown = okm.slice(32);
        const proof = await pake.hmac(kDown, pake.concat(enc.encode("server"), nc, ns));
        if (!pake.equalBytes(proof, pake.unhex(msg.proof))) {
          this.onState("error", { reason: "The PC could not prove it is the paired PC." });
          return ws.close();
        }
        this.upKey = await subtle.importKey("raw", okm.slice(0, 32), "AES-GCM", false, ["encrypt"]);
        okm.fill(0);
        this.counter = 0n;
        this.ready = true;
        this._retry = 1000;
        this.onState("connected", { pc: creds.pc });
      } else if (msg.t === "unknown") {
        unknown = true;
        this.onState("unknown-device");
      } else if (msg.t === "replaced") {
        this.onState("replaced");
      } else if (msg.t === "error") {
        this.onState("error", { reason: msg.reason });
      }
    };
    ws.onclose = () => {
      this.ready = false;
      if (unknown) return; // the UI offers to pair again
      this.onState("connecting", { pc: creds.pc });
      this._schedule(() => this._connect(creds), this._backoff());
    };
  }

  _backoff() {
    const ms = this._retry;
    this._retry = Math.min(10000, this._retry * 2);
    return ms;
  }

  /** Encrypt and send one pose message. Drops it if the link isn't ready or is backed up. */
  async send(obj) {
    const ws = this.ws;
    if (!this.ready || !ws || ws.readyState !== 1 || ws.bufferedAmount > 4096) return false;
    this.counter += 1n;
    const ctr = new Uint8Array(8);
    new DataView(ctr.buffer).setBigUint64(0, this.counter, false);
    const iv = pake.concat(new Uint8Array(4), ctr);
    const ct = new Uint8Array(await subtle.encrypt(
      { name: "AES-GCM", iv, additionalData: POSE_AAD }, this.upKey, enc.encode(JSON.stringify(obj))));
    if (ws.readyState === 1) ws.send(pake.concat(ctr, ct));
    return true;
  }
}

/** IndexedDB-backed credential store (CryptoKeys stay non-extractable). */
export function indexedDbStore(dbName = "openitrack") {
  const open = () => new Promise((resolve, reject) => {
    const req = indexedDB.open(dbName, 1);
    req.onupgradeneeded = () => req.result.createObjectStore("kv");
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  const tx = async (mode, fn) => {
    const db = await open();
    return new Promise((resolve, reject) => {
      const t = db.transaction("kv", mode);
      const req = fn(t.objectStore("kv"));
      t.oncomplete = () => resolve(req?.result);
      t.onerror = () => reject(t.error);
    });
  };
  return {
    get: () => tx("readonly", (s) => s.get("creds")).then((v) => v ?? null),
    set: (creds) => tx("readwrite", (s) => s.put(creds, "creds")),
    clear: () => tx("readwrite", (s) => s.delete("creds")),
  };
}
