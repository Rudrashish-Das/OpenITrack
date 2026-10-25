// Phone page: secure link to the PC + MediaPipe face tracking, all served by the PC.
import { FaceLandmarker, FilesetResolver } from "/vendor/vision_bundle.mjs";
import { PhoneLink, indexedDbStore } from "/link.js";
import { poseMessage } from "/frame.js";
import { drawOverlay } from "/overlay.js";

const MAX_FPS = 30;

const $ = (id) => document.getElementById(id);
const video = $("video");
const overlay = $("overlay");
const overlayCtx = overlay.getContext("2d");
const overlayToggle = $("overlayToggle");

// Remember the overlay choice on this phone (a convenience; works without it).
try { overlayToggle.checked = localStorage.getItem("overlay") !== "off"; } catch (_) {}
overlayToggle.onchange = () => {
  try { localStorage.setItem("overlay", overlayToggle.checked ? "on" : "off"); } catch (_) {}
  if (!overlayToggle.checked) overlayCtx.clearRect(0, 0, overlay.width, overlay.height);
};

function showOverlay(result) {
  const w = video.videoWidth, h = video.videoHeight;
  if (overlay.width !== w || overlay.height !== h) { overlay.width = w; overlay.height = h; }
  // Skip the drawing when it can't be seen (switched off, or the black screen is on).
  if (!overlayToggle.checked || $("blackout").style.display === "flex") return;
  drawOverlay(overlayCtx, result, w, h);
}

// Called for every frame, so skip the DOM work when nothing changed.
const shown = new Map();
function status(dot, text, cls, msg) {
  const key = (cls || "") + "|" + msg;
  if (shown.get(dot) === key) return;
  shown.set(dot, key);
  $(dot).className = "dot " + (cls || "");
  $(text).textContent = msg;
}

// ---- secure link -------------------------------------------------------------
const link = new PhoneLink({
  base: `wss://${location.host}`,
  store: indexedDbStore(),
  onState(state, d = {}) {
    $("pairCard").hidden = state !== "pairing";
    $("repair").hidden = state !== "unknown-device";
    $("forget").hidden = !["connected", "connecting"].includes(state);
    $("start").hidden = !(state === "connected" && !running);
    switch (state) {
      case "pairing":
        $("code").textContent = d.code;
        $("pairNote").textContent = d.reason === "mismatch"
          ? "That code didn't match on the PC, so here's a new one. Type this one."
          : d.reason === "expired" ? "The old code expired; use this one."
          : "The code never leaves this phone. Only someone who can read it can pair.";
        $("pairNote").className = d.reason === "mismatch" ? "notice" : "muted";
        return status("pcDot", "pcText", "warn", "waiting for code on PC");
      case "pairing-connecting":
        return status("pcDot", "pcText", "bad", "looking for the PC…");
      case "paired":
        return status("pcDot", "pcText", "ok", "paired with " + d.pc);
      case "connecting":
        return status("pcDot", "pcText", "warn", "connecting to " + (d.pc || "PC") + "…");
      case "connected":
        return status("pcDot", "pcText", "ok", "connected securely to " + d.pc);
      case "unknown-device":
        return status("pcDot", "pcText", "bad", "this PC no longer recognises this phone");
      case "replaced":
        return status("pcDot", "pcText", "bad", "another phone took over");
      case "error":
        return status("pcDot", "pcText", "bad", d.reason || "error");
    }
  },
});

$("repair").onclick = async () => { await link.forget(); link.start(); };
$("forget").onclick = async () => {
  if (confirm("Forget this PC? You'll need a new pairing code to connect again.")) {
    await link.forget();
    link.start();
  }
};

// ---- wake lock / visibility ----------------------------------------------------------
let wakeLock = null;
async function keepAwake() {
  try { wakeLock = await navigator.wakeLock?.request("screen"); } catch (_) {}
}
document.addEventListener("visibilitychange", () => {
  if (!running) return;
  if (document.visibilityState === "hidden") stopCamera();      // release the camera when not shown
  else startCamera().then(keepAwake).catch(() => {});
});

// ---- tracking -------------------------------------------------------------------------
let landmarker = null;
let running = false;
let stream = null;
let lastSent = 0, frames = 0, fpsT0 = performance.now(), fps = 0;

async function createLandmarker() {
  const files = await FilesetResolver.forVisionTasks("/vendor/wasm");
  const opts = (delegate) => ({
    baseOptions: { modelAssetPath: "/face_landmarker.task", delegate },
    runningMode: "VIDEO", numFaces: 1,
    outputFaceBlendshapes: true, outputFacialTransformationMatrixes: true,
  });
  try { return await FaceLandmarker.createFromOptions(files, opts("GPU")); }
  catch (e) { console.warn("GPU delegate failed, using CPU", e); return FaceLandmarker.createFromOptions(files, opts("CPU")); }
}

async function startCamera() {
  if (stream) return;
  // 720p gives the iris (and so the gaze) about twice the pixels of 640x480.
  // "ideal" lets phones that can't do it fall back to their closest mode.
  stream = await navigator.mediaDevices.getUserMedia({
    audio: false,
    video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30, max: 30 } },
  });
  video.srcObject = stream;
  await video.play();
  schedule();
}

function stopCamera() {
  if (!stream) return;
  for (const t of stream.getTracks()) t.stop();
  stream = null;
  video.srcObject = null;
  overlayCtx.clearRect(0, 0, overlay.width, overlay.height);  // no stale marks over a paused camera
  status("faceDot", "faceText", "", "camera paused");
}

// Screen rotation in degrees (0/90/180/270). The camera frames turn with it,
// so the PC recenters when it changes, also for 180 deg flips that keep w/h.
function screenAngle() {
  const a = screen.orientation?.angle ?? window.orientation ?? 0;
  return Number.isFinite(a) ? a : 0;
}

function onFrame() {
  if (!running || !stream) return;
  const now = performance.now();
  if (now - lastSent >= 1000 / MAX_FPS - 2 && video.readyState >= 2) {
    lastSent = now;
    const r = landmarker.detectForVideo(video, now);
    const msg = poseMessage(r, { t: now, w: video.videoWidth, h: video.videoHeight, fps, angle: screenAngle() });
    link.send(msg);
    showOverlay(r);
    if (msg.face) status("faceDot", "faceText", "ok", "tracking");
    else status("faceDot", "faceText", "warn", "no face in view");
    frames++;
    if (now - fpsT0 >= 1000) {
      fps = (frames * 1000) / (now - fpsT0);
      frames = 0; fpsT0 = now;
      $("fpsText").textContent = fps.toFixed(0) + " fps";
    }
  }
  schedule();
}
function schedule() {
  if ("requestVideoFrameCallback" in video) video.requestVideoFrameCallback(onFrame);
  else requestAnimationFrame(onFrame);
}

$("start").onclick = async () => {
  const btn = $("start");
  btn.disabled = true;
  try {
    btn.textContent = "Loading face model…";
    landmarker = landmarker || await createLandmarker();
    btn.textContent = "Starting camera…";
    running = true;
    $("stage").hidden = false;
    $("overlayRow").hidden = false;
    await startCamera();
    btn.hidden = true;
    $("dim").hidden = false;
    keepAwake();
  } catch (e) {
    running = false;
    btn.disabled = false;
    btn.textContent = "Start tracking";
    status("faceDot", "faceText", "bad", "error: " + (e.message || e.name || e));
  }
};

$("dim").onclick = () => { $("blackout").style.display = "flex"; };
$("blackout").onclick = () => { $("blackout").style.display = "none"; };

if (!window.isSecureContext) status("pcDot", "pcText", "bad", "open the https:// link shown on the PC");
else link.start();
