"use strict";

const $ = (id) => document.getElementById(id);
const DEV = { rot: 270, lw: 416, lh: 240, connected: false };

let busy = false;
let fileB64 = null;      // chosen image, dataURL
let pendingFrame = null; // converted frame (base64), ready to send
let previewTimer = null;

function log(msg, bad = false) {
  const el = $("log");
  const line = document.createElement("div");
  line.className = "logline" + (bad ? " bad" : "");
  line.textContent = new Date().toLocaleTimeString() + "  " + msg;
  el.prepend(line);
  while (el.children.length > 9) el.lastChild.remove();
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  return r.json();
}

function setBusy(b) {
  busy = b;
  document.querySelectorAll("button").forEach((btn) => (btn.disabled = b));
  document.body.classList.toggle("busy", b);
}

function note(ok, reply, err) {
  log(ok ? reply : "ERR " + err, !ok);
}

/* ---------------------------------------------------------------- status */

async function refreshStatus() {
  const dot = $("dot");
  try {
    const j = await api("/api/status");
    if (j.ok) {
      DEV.rot = j.rot; DEV.lw = j.lw; DEV.lh = j.lh; DEV.connected = true;
      $("rot").value = String(j.rot);
      $("status-text").textContent =
        `${j.port} · logical ${j.lw}×${j.lh} · rot ${j.rot}° · ` +
        `mem ${Math.round(j.mem / 1024)}k · last: ${j.last}`;
      dot.className = "dot on";
    } else {
      DEV.connected = false;
      $("status-text").textContent = "disconnected — " + j.error;
      dot.className = "dot off";
    }
  } catch (e) {
    DEV.connected = false;
    $("status-text").textContent = "server unreachable";
    dot.className = "dot off";
  }
}

/* ----------------------------------------------------------------- text */

function textParams() {
  return {
    text: $("text").value,
    size: +$("tsize").value || 0,
    x: +$("tx").value || 0,
    y: +$("ty").value || 0,
  };
}

async function previewText() {
  try {
    const j = await api("/api/text/preview", textParams());
    if (j.ok) $("preview").src = j.preview;
  } catch (e) { /* server gone; status bar will say so */ }
}

function scheduleTextPreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(previewText, 300);
}

async function sendText() {
  setBusy(true);
  try {
    const j = await api("/api/text", textParams());
    note(j.ok, j.reply, j.error);
    if (j.ok && j.preview) $("preview").src = j.preview;
    await refreshStatus();
  } finally { setBusy(false); }
}

/* ---------------------------------------------------------------- image */

function loadFile(f) {
  if (!f) return;
  const rd = new FileReader();
  rd.onload = () => { fileB64 = rd.result; convertImage(); };
  rd.readAsDataURL(f);
}

async function convertImage() {
  if (!fileB64) { log("choose an image first", true); return; }
  setBusy(true);
  try {
    const j = await api("/api/image/convert", {
      data: fileB64,
      scale: $("iscale").value,
      rotate: +$("irotate").value,
      dither: $("idither").checked,
      invert: $("iinvert").checked,
    });
    if (j.ok) {
      pendingFrame = j.frame;
      $("preview").src = j.preview;
      $("send-image").disabled = false;
      log(`converted to ${j.w}×${j.h} frame`);
      if ($("iauto").checked) await sendImage();
    } else {
      log("convert failed: " + j.error, true);
    }
  } finally { setBusy(false); }
}

async function sendImage() {
  if (!pendingFrame) return;
  setBusy(true);
  try {
    const j = await api("/api/image/send", { frame: pendingFrame });
    note(j.ok, j.reply, j.error);
    if (j.ok) pendingFrame = null;
    await refreshStatus();
  } finally { setBusy(false); }
}

/* -------------------------------------------------------------- commands */

async function command(name, seconds) {
  setBusy(true);
  try {
    const j = await api("/api/command", { cmd: name, seconds: seconds || 0 });
    note(j.ok, j.reply, j.error);
    if (name === "reset") await new Promise((r) => setTimeout(r, 5000));
    if (name === "repl") DEV.connected = false;
    await refreshStatus();
  } finally { setBusy(false); }
}

async function changeRotation() {
  setBusy(true);
  try {
    const j = await api("/api/rotate", { deg: +$("rot").value });
    note(j.ok, j.reply, j.error);
    if (j.ok) {
      DEV.rot = j.rot; DEV.lw = j.lw; DEV.lh = j.lh;
      pendingFrame = null;               // old frame is the wrong shape now
      $("send-image").disabled = true;
      log(`rotation ${DEV.rot}° — logical size now ${DEV.lw}×${DEV.lh}`);
      previewText();
    }
    await refreshStatus();
  } finally { setBusy(false); }
}

/* ------------------------------------------------------------------ wire */

document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => {
  document.querySelectorAll(".tab").forEach((x) =>
    x.classList.toggle("active", x === t));
  const which = t.dataset.tab;
  $("tab-text").classList.toggle("hidden", which !== "text");
  $("tab-image").classList.toggle("hidden", which !== "image");
  if (which === "text") previewText();
}));

const drop = $("drop");
["dragover", "dragenter"].forEach((ev) =>
  drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) =>
  drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => loadFile(e.dataTransfer.files[0]));
$("imgfile").addEventListener("change", (e) => loadFile(e.target.files[0]));

$("text").addEventListener("input", scheduleTextPreview);
["tsize", "tx", "ty"].forEach((id) => $(id).addEventListener("input", scheduleTextPreview));

$("send-text").onclick = sendText;
$("convert").onclick = convertImage;
$("send-image").onclick = sendImage;
$("b-clear").onclick = () => command("clear");
$("b-demo").onclick = () => command("demo");
$("b-save").onclick = () => command("save");
$("b-reset").onclick = () => command("reset");
$("b-repl").onclick = () => command("repl");
$("b-refresh").onclick = refreshStatus;
$("rot").onchange = changeRotation;

refreshStatus().then(previewText);
