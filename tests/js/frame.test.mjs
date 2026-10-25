// Run: node tests/js/frame.test.mjs   (also run by tests/test_web_js.py)
// Checks web/frame.js, then prints the full-precision and the sent message for
// one fake face so the Python side can check parse_message gives the same pose.
import assert from "node:assert/strict";
import { GAZE, poseMessage, round5, round6 } from "../../openitrack/web/frame.js";

// A head turned ~20 deg with some pitch, 45 cm away, as a column-major 4x4.
const yaw = 0.35, pitch = 0.12;
const cy = Math.cos(yaw), sy = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
const rows = [
  [cy, sy * sp, sy * cp, 1.234567891],
  [0, cp, -sp, 2.345678912],
  [-sy, cy * sp, cy * cp, -45.678912345],
  [0, 0, 0, 1],
];
const data = new Float32Array(16);
for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) data[c * 4 + r] = rows[r][c];

const lms = Array.from({ length: 478 }, (_, i) => ({ x: 0.3 + i * 0.000731234567, y: 0.6 - i * 0.000412345678 }));
const blend = [{ categoryName: "eyeBlinkLeft", score: 0.123456789 },
               { categoryName: "browDownLeft", score: 0.5 },
               { categoryName: "eyeBlinkRight", score: 0.234567891 }];
const result = { faceLandmarks: [lms], facialTransformationMatrixes: [{ data }],
                 faceBlendshapes: [{ categories: blend }] };
const opts = { t: 1234.5, w: 640, h: 480, fps: 29.97, angle: 90 };

// -- rounding -----------------------------------------------------------------------------
assert.equal(round5(0.123456789), 0.12346);
assert.equal(round5(-45.678912345), -45.67891);
assert.equal(round5(1), 1);
assert.equal(round6(0.123456789), 0.123457);

// -- face message ---------------------------------------------------------------------------
const msg = poseMessage(result, opts);
assert.equal(msg.face, 1);
assert.equal(msg.t, 1234.5);
assert.equal(msg.w, 640);
assert.equal(msg.h, 480);
assert.equal(msg.o, 90);
assert.equal(msg.fps, 30);
assert.equal(msg.m.length, 16);
assert.equal(msg.g.length, 2 * GAZE.length);
assert.deepEqual(msg.b, [0.12346, 0.23457]);
for (const v of [...msg.m, ...msg.b]) assert.equal(v, round5(v));
for (const v of msg.g) assert.equal(v, round6(v));
for (let i = 0; i < 16; i++) assert.ok(Math.abs(msg.m[i] - data[i]) <= 5e-6);
GAZE.forEach((idx, k) => {
  assert.ok(Math.abs(msg.g[2 * k] - lms[idx].x) <= 5e-7);
  assert.ok(Math.abs(msg.g[2 * k + 1] - lms[idx].y) <= 5e-7);
});

// The point of rounding: a much smaller message.
const full = { ...msg, m: Array.from(data), g: GAZE.flatMap((i) => [lms[i].x, lms[i].y]),
               b: [blend[0].score, blend[2].score] };
const sentBytes = JSON.stringify(msg).length, fullBytes = JSON.stringify(full).length;
assert.ok(sentBytes < fullBytes * 0.6, `rounded ${sentBytes} B vs full ${fullBytes} B`);

// -- no blink scores, no face ---------------------------------------------------------------------
assert.equal(poseMessage({ ...result, faceBlendshapes: [] }, opts).b, null);
assert.equal(poseMessage({ ...result, faceBlendshapes: [{ categories: [blend[0]] }] }, opts).b, null);
for (const r of [{}, { faceLandmarks: [lms] }, { facialTransformationMatrixes: [{ data }] },
                 { faceLandmarks: [lms.slice(0, 468)], facialTransformationMatrixes: [{ data }] }]) {
  assert.deepEqual(poseMessage(r, opts), { t: 1234.5, face: 0, fps: 29.97 });
}

process.stdout.write(JSON.stringify({ sent: msg, full, sentBytes, fullBytes }) + "\n");
