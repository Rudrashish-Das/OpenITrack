// Run: node tests/js/pake.test.mjs   (also run by tests/test_web_js.py)
import assert from "node:assert/strict";
import * as pake from "../../openitrack/web/pake.js";

const H = pake.unhex;
const vectors = [
  { idA: "server", idB: "client",
    w: 0x2ee57912099d31560b3a44b1184b9b4866e904c49d12ac5042c97dca461b1a5fn,
    x: 0x43dd0fd7215bdcb482879fca3220c6a968e66d70b1356cac18bb26c84a78d729n,
    y: 0xdcb60106f276b02606d8ef0a328c02e4b629f84f89786af5befb0bc75b6e66ben,
    pA: "04a56fa807caaa53a4d28dbb9853b9815c61a411118a6fe516a8798434751470f9010153ac33d0d5f2047ffdb1a3e42c9b4e6be662766e1eeb4116988ede5f912c",
    pB: "0406557e482bd03097ad0cbaa5df82115460d951e3451962f1eaf4367a420676d09857ccbc522686c83d1852abfa8ed6e4a1155cf8f1543ceca528afb591a1e0b7",
    ke: "0e0672dc86f8e45565d338b0540abe69",
    cA: "58ad4aa88e0b60d5061eb6b5dd93e80d9c4f00d127c65b3b35b1b5281fee38f0",
    cB: "d3e2e547f1ae04f2dbdbf0fc4b79f8ecff2dff314b5d32fe9fcef2fb26dc459b" },
  { idA: "", idB: "client",
    w: 0x0548d8729f730589e579b0475a582c1608138ddf7054b73b5381c7e883e2efaen,
    x: 0x403abbe3b1b4b9ba17e3032849759d723939a27a27b9d921c500edde18ed654bn,
    y: 0x903023b6598908936ea7c929bd761af6039577a9c3f9581064187c3049d87065n,
    pA: "04a897b769e681c62ac1c2357319a3d363f610839c4477720d24cbe32f5fd85f44fb92ba966578c1b712be6962498834078262caa5b441ecfa9d4a9485720e918a",
    pB: "04e0f816fd1c35e22065d5556215c097e799390d16661c386e0ecc84593974a61b881a8c82327687d0501862970c64565560cb5671f696048050ca66ca5f8cc7fc",
    ke: "642f05c473c2cd79909f9a841e2f30a7",
    cA: "47d29e6666af1b7dd450d571233085d7a9866e4d49d2645e2df975489521232b",
    cB: "3313c5cefc361d27fb16847a91c2a73b766ffa90a4839122a9b70a2f6bd1d6df" },
];

for (const v of vectors) {
  const opts = { idA: v.idA, idB: v.idB, aad: "" };
  const a = await pake.startA("", { ...opts, w: v.w, x: v.x });
  assert.equal(pake.hex(a.message), v.pA);
  const b = await pake.respondB(v.w, a.message, { ...opts, y: v.y });
  assert.equal(pake.hex(b.message), v.pB);
  const ka = await a.finish(b.message);
  assert.equal(pake.hex(ka.ke), v.ke);
  assert.equal(pake.hex(await ka.confirmA()), v.cA);
  assert.equal(pake.hex(await b.keys.confirmB()), v.cB);
}

// Code generation and password derivation must match Python byte-for-byte.
for (let i = 0; i < 200; i++) {
  const c = pake.newCode();
  assert.equal(c.length, 6);
  for (const ch of c) assert.ok(pake.CODE_ALPHABET.includes(ch));
}
const w = await pake.passwordScalar("ab2-3cd");
console.log(JSON.stringify({ ok: true, w_AB23CD: w.toString(16) }));
