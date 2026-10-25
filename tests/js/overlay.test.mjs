// Checks web/overlay.js with a recording stand-in for the canvas (Node has none).
// Run: node tests/js/overlay.test.mjs   (also run by tests/test_web_js.py)
import assert from "node:assert/strict";
import { drawOverlay } from "../../openitrack/web/overlay.js";
import { GAZE } from "../../openitrack/web/frame.js";

function recorder() {
  const calls = [];
  const ctx = new Proxy({ calls }, {
    get(target, prop) {
      if (prop in target) return target[prop];
      return (...args) => calls.push([prop, ...args]);
    },
    set(target, prop, value) { calls.push(["set", prop, value]); return true; },
  });
  return { ctx, calls };
}

// A face: 478 points around the middle of a 640x480 frame.
const lms = Array.from({ length: 478 }, (_, i) => ({ x: 0.4 + (i % 20) / 100, y: 0.3 + Math.floor(i / 20) / 100 }));
lms[1] = { x: 0.5, y: 0.5 };                       // nose tip
// Head turned toward image-right and up: forward axis (column 3) = (0.5, 0.3, 0.8).
const m = [1, 0, 0, 0, 0, 1, 0, 0, 0.5, 0.3, 0.8, 0, 0, 0, -50, 1];
const result = { faceLandmarks: [lms], facialTransformationMatrixes: [{ data: m }] };

{
  const { ctx, calls } = recorder();
  assert.equal(drawOverlay(ctx, result, 640, 480), true);
  assert.deepEqual(calls[0], ["clearRect", 0, 0, 640, 480]);          // old marks always cleared first
  const arcs = calls.filter((c) => c[0] === "arc");
  // mesh (every 2nd point) + eye points + 2 iris rings + the direction dot
  assert.equal(arcs.length, 239 + GAZE.length + 2 + 1);
  // Head-direction line starts at the nose and goes right (+x) and up (-y in the image).
  const lineTo = calls.find((c) => c[0] === "lineTo");
  assert.ok(lineTo[1] > 320 && lineTo[2] < 240, `direction line ends at ${lineTo[1]},${lineTo[2]}`);
}

{
  const { ctx, calls } = recorder();
  assert.equal(drawOverlay(ctx, { faceLandmarks: [] }, 640, 480), false);   // no face: just cleared
  assert.deepEqual(calls, [["clearRect", 0, 0, 640, 480]]);
  assert.equal(drawOverlay(recorder().ctx, null, 640, 480), false);
}

{
  // Marks keep the same size relative to the picture at higher resolutions.
  const { ctx, calls } = recorder();
  drawOverlay(ctx, result, 1280, 960);
  const meshRadius = calls.find((c) => c[0] === "arc")[3];
  assert.equal(meshRadius, 1.3 * 2);
}
console.log(JSON.stringify({ ok: true }));
