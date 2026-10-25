// Headless phone for integration tests: runs the real web/link.js against a
// running PhoneSource. Prints one JSON line per event on stdout.
//
//   node phone_client.mjs <wss://host:port> <poses-to-send> [--store file.json]
//
// NODE_TLS_REJECT_UNAUTHORIZED=0 must be set (self-signed test certificate).
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { PhoneLink } from "../../openitrack/web/link.js";

const [base, posesArg, flag, storePath] = process.argv.slice(2);
const poses = Number(posesArg || 0);
const out = (o) => process.stdout.write(JSON.stringify(o) + "\n");

// Persist credentials between runs the way IndexedDB would. Only for tests:
// the raw secret is exported here, which the real page never does.
const subtle = globalThis.crypto.subtle;
let memory = null;
const store = {
  async get() {
    if (memory) return memory;
    if (flag === "--store" && existsSync(storePath)) {
      const s = JSON.parse(readFileSync(storePath, "utf8"));
      const key = await subtle.importKey("raw", Buffer.from(s.secret, "hex"), "HKDF", false, ["deriveBits"]);
      memory = { id: s.id, pc: s.pc, key };
    }
    return memory;
  },
  async set(c) { memory = c; },
  async clear() { memory = null; },
};

// Capture the raw device secret for --store by wrapping importKey once.
const realImport = subtle.importKey.bind(subtle);
let capturedSecret = null;
subtle.importKey = (fmt, data, alg, ext, uses) => {
  if (alg === "HKDF" && fmt === "raw" && data.length === 32) capturedSecret = Buffer.from(data).toString("hex");
  return realImport(fmt, data, alg, ext, uses);
};

let sent = 0;
const link = new PhoneLink({
  base, store, name: "Test phone",
  onState: async (state, d) => {
    out({ state, ...d });
    if (state === "paired" && flag === "--store" && capturedSecret) {
      const c = await store.get();
      writeFileSync(storePath, JSON.stringify({ id: c.id, pc: c.pc, secret: capturedSecret }));
    }
    if (state === "connected") {
      for (let i = 0; i < poses; i++) {
        const m = [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,-50,1];
        const yaw = (10 + i) * Math.PI / 180;           // rotate about Y by 10+i degrees
        m[0] = Math.cos(yaw); m[8] = Math.sin(yaw); m[2] = -Math.sin(yaw); m[10] = Math.cos(yaw);
        const g = [0.35,0.5, 0.45,0.5, 0.4,0.485, 0.4,0.515, 0.4,0.5,
                   0.55,0.5, 0.65,0.5, 0.6,0.485, 0.6,0.515, 0.6,0.5];
        await link.send({ t: 1000 + i * 33, face: 1, w: 1, h: 1, m, g, b: [0, 0], fps: 30 });
        sent++;
        await new Promise((r) => setTimeout(r, 20));
      }
      out({ sent });
      setTimeout(() => { link.stop(); process.exit(0); }, 300);
    }
    if (state === "unknown-device") { link.stop(); process.exit(3); }
  },
});
link.start();
setTimeout(() => { out({ state: "timeout" }); process.exit(2); }, 30000);
