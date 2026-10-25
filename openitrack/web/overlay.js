// Phone page: draw what the tracker sees over the camera picture, so you can
// check the face is found and the eyes are tracked. Drawn on the phone only;
// nothing extra is sent to the PC.

import { GAZE } from "./frame.js";

const MESH_STEP = 2;                 // every 2nd of the 478 points is plenty
const IRISES = [468, 473];
const NOSE_TIP = 1;

/**
 * Draw one landmarker result. The canvas uses the video's own pixel size and
 * is mirrored with CSS exactly like the video, so points are drawn unmirrored.
 * @param {CanvasRenderingContext2D} ctx
 * @param {object|null} result  FaceLandmarker.detectForVideo() output, or null to clear
 * @param {number} w  video width in pixels
 * @param {number} h  video height in pixels
 * @returns {boolean} whether a face was drawn
 */
export function drawOverlay(ctx, result, w, h) {
  ctx.clearRect(0, 0, w, h);
  const lms = result?.faceLandmarks?.[0];
  if (!lms || lms.length <= Math.max(...IRISES)) return false;
  const s = Math.max(1, w / 640);    // keep marks the same size at any resolution

  ctx.fillStyle = "rgba(76, 141, 255, 0.65)";
  ctx.beginPath();
  for (let i = 0; i < lms.length; i += MESH_STEP) {
    const p = lms[i];
    ctx.moveTo(p.x * w + 1.3 * s, p.y * h);
    ctx.arc(p.x * w, p.y * h, 1.3 * s, 0, Math.PI * 2);
  }
  ctx.fill();

  // The eye points eye-assist uses, and rings around the irises.
  ctx.fillStyle = "#3ecf8e";
  ctx.beginPath();
  for (const i of GAZE) {
    const p = lms[i];
    ctx.moveTo(p.x * w + 2.6 * s, p.y * h);
    ctx.arc(p.x * w, p.y * h, 2.6 * s, 0, Math.PI * 2);
  }
  ctx.fill();
  ctx.strokeStyle = "#3ecf8e";
  ctx.lineWidth = 1.5 * s;
  for (const i of IRISES) {
    const p = lms[i];
    ctx.beginPath();
    ctx.arc(p.x * w, p.y * h, 6 * s, 0, Math.PI * 2);
    ctx.stroke();
  }

  // Which way the head points: the face's forward axis from the transform
  // matrix (column-major; camera space has +y up, the image has +y down).
  const m = result.facialTransformationMatrixes?.[0]?.data;
  if (m && m.length === 16) {
    const nose = lms[NOSE_TIP];
    const x0 = nose.x * w, y0 = nose.y * h;
    const len = 0.22 * w;
    const x1 = x0 + m[8] * len, y1 = y0 - m[9] * len;
    ctx.strokeStyle = "#f2b84b";
    ctx.lineWidth = 3 * s;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(x0, y0);
    ctx.lineTo(x1, y1);
    ctx.stroke();
    ctx.fillStyle = "#f2b84b";
    ctx.beginPath();
    ctx.arc(x1, y1, 4.5 * s, 0, Math.PI * 2);
    ctx.fill();
  }
  return true;
}
