// Phone page: turn one MediaPipe face-landmarker result into the pose message
// sent to the PC (parsed by parse_message in openitrack/sources/phone.py).

// Must match GAZE_LANDMARKS in openitrack/features.py.
export const GAZE = [33, 133, 159, 145, 468, 362, 263, 386, 374, 473];

// Values are sent rounded instead of with ~17 digits: about half the bytes to
// encrypt, send and parse 30x a second, and still far finer than the tracking
// noise. The matrix gets 5 decimals (0.0006 deg, 0.0001 mm). Gaze landmarks get
// 6: gaze is an offset divided by the eye width (~0.05 of the frame), which
// magnifies their rounding about 20x.
export const round5 = (v) => Math.round(v * 1e5) / 1e5;
export const round6 = (v) => Math.round(v * 1e6) / 1e6;

/**
 * @param {object} result   FaceLandmarker.detectForVideo() output
 * @param {object} o        { t, w, h, fps, angle }: timestamp (ms), video size,
 *                          measured fps, screen rotation in degrees
 * @returns {object}        the message; face: 0 when no usable face was found
 */
export function poseMessage(result, { t, w, h, fps, angle }) {
  const lms = result.faceLandmarks?.[0];
  const mat = result.facialTransformationMatrixes?.[0];
  if (!lms || !mat || lms.length <= Math.max(...GAZE)) return { t, face: 0, fps };
  const g = [];
  for (const i of GAZE) g.push(round6(lms[i].x), round6(lms[i].y));
  let b = null;
  const cats = result.faceBlendshapes?.[0]?.categories;
  if (cats) {
    let l = null, r = null;
    for (const c of cats) {
      if (c.categoryName === "eyeBlinkLeft") l = c;
      else if (c.categoryName === "eyeBlinkRight") r = c;
    }
    if (l && r) b = [round5(l.score), round5(r.score)];
  }
  return { t, face: 1, w, h, m: Array.from(mat.data, round5), g, b, fps: Math.round(fps * 10) / 10, o: angle };
}
