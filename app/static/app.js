// 印前色彩软打样台前端（原生 JavaScript，无框架）
"use strict";

const state = {
  profiles: { source: [], press: [], display: [] },
  job: null,
  view: "preview",
};

const $ = (id) => document.getElementById(id);

// ---- 顶部导航：软打样 / 发布门禁 -----------------------------------------
document.querySelectorAll(".navbtn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".navbtn").forEach((b) =>
      b.classList.toggle("active", b === btn)
    );
    const v = btn.dataset.view;
    $("view-softproof").hidden = v !== "softproof";
    $("view-retouch").hidden = v !== "retouch";
    $("view-release").hidden = v !== "release";
    if (v === "release" && typeof refreshReleaseView === "function") {
      refreshReleaseView();
    }
    if (v === "retouch" && typeof refreshRetouchView === "function") {
      refreshRetouchView();
    }
  });
});

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j && j.detail) msg = j.detail;
    } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}

function showError(msg) {
  const box = $("errorBox");
  if (!msg) {
    box.hidden = true;
    box.textContent = "";
  } else {
    box.hidden = false;
    box.textContent = msg;
  }
}

function profileLabel(p) {
  const tag = p.builtin ? "内置" : "自定义";
  return `[${tag}] ${p.name} (${p.color_space})`;
}

function fillSelect(select, items, keepId) {
  select.innerHTML = "";
  for (const p of items) {
    const opt = document.createElement("option");
    opt.value = p.profile_id;
    opt.textContent = profileLabel(p);
    select.appendChild(opt);
  }
  if (items.length && !keepId) {
    // Defaults: built-in first
  }
  if (keepId) select.value = keepId;
}

async function loadProfiles() {
  state.profiles = await api("/api/profiles");
  fillSelect($("sourceProfile"), state.profiles.source);
  fillSelect($("pressProfile"), state.profiles.press);
  fillSelect($("displayProfile"), state.profiles.display);
}

$("useEmbedded").addEventListener("change", (e) => {
  $("sourceProfile").disabled = e.target.checked;
});

$("uploadProfileBtn").addEventListener("click", async () => {
  const role = $("uploadRole").value;
  const file = $("profileFile").files[0];
  const msg = $("profileUploadMsg");
  msg.textContent = "";
  if (!file) {
    msg.textContent = "请先选择 .icc/.icm 文件";
    return;
  }
  const fd = new FormData();
  fd.append("file", file);
  try {
    const rec = await api(`/api/profiles/${role}`, { method: "POST", body: fd });
    msg.textContent = `已上传：${rec.name}`;
    await loadProfiles();
    const select = $(`${role}Profile`);
    if (select) select.value = rec.profile_id;
  } catch (err) {
    msg.textContent = "上传被拒绝：" + err.message;
  }
});

$("imageFile").addEventListener("change", () => {
  const f = $("imageFile").files[0];
  $("imageInfo").textContent = f ? `${f.name} (${(f.size / 1024 / 1024).toFixed(2)} MB)` : "";
});

$("runBtn").addEventListener("click", async () => {
  showError("");
  const file = $("imageFile").files[0];
  if (!file) return showError("请先选择图像文件");
  const fd = new FormData();
  fd.append("image", file);
  fd.append("source_profile", $("sourceProfile").value);
  fd.append("press_profile", $("pressProfile").value);
  fd.append("display_profile", $("displayProfile").value);
  fd.append("source_intent", $("sourceIntent").value);
  fd.append("proof_intent", $("proofIntent").value);
  fd.append("threshold", $("threshold").value);
  fd.append("use_embedded", $("useEmbedded").checked ? "true" : "false");

  const btn = $("runBtn");
  btn.disabled = true;
  $("busy").hidden = false;
  try {
    const job = await api("/api/jobs", { method: "POST", body: fd });
    state.job = job;
    renderJob();
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
    $("busy").hidden = true;
  }
});

function renderJob() {
  const job = state.job;
  $("resultsPanel").hidden = false;
  const s = job.stats;
  const pct = (s.over_ratio * 100).toFixed(3);
  $("statsBar").innerHTML =
    `<span>均值 ΔE00 <b>${s.mean.toFixed(2)}</b></span>` +
    `<span>P95 <b>${s.p95.toFixed(2)}</b></span>` +
    `<span>最大 <b>${s.max.toFixed(2)}</b></span>` +
    `<span>阈值 ${s.threshold} 超阈面积 <b>${pct}%</b></span>` +
    `<span>超阈像素 ${s.over_pixels.toLocaleString()} / ${s.total_pixels.toLocaleString()}</span>` +
    `<span>任务 <code>${job.job_id.slice(0, 12)}…</code></span>`;

  $("reportJson").textContent = JSON.stringify(job.report, null, 2);
  setView("preview");

  const base = `/api/jobs/${job.job_id}`;
  $("expPreview").href = `${base}/images/preview.png`;
  $("expProof").href = `${base}/images/softproof.png`;
  $("expHeat").href = `${base}/images/heatmap.png`;
  $("expMask").href = `${base}/images/mask.png`;
  $("expReport").href = `${base}/report`;
  $("expZip").href = `${base}/export.zip`;
  for (const a of ["expPreview", "expProof", "expHeat", "expMask"]) $(a).download = $(a).href.split("/").pop();
  $("expReport").download = `${job.job_id.slice(0, 12)}-report.json`;
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => setView(tab.dataset.view));
});

function setView(view) {
  state.view = view;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  const job = state.job;
  if (!job) return;
  const map = {
    preview: job.images.preview,
    softproof: job.images.softproof,
    heatmap: job.images.heatmap,
    overlay: job.images.overlay,
    mask: job.images.mask,
  };
  $("viewImage").src = map[view];
}

// ---- ROI selection -------------------------------------------------------
const img = $("viewImage"), canvas = $("roiCanvas");
let drag = null;

function roiRect() {
  const wrap = $("canvasWrap");
  const wRect = wrap.getBoundingClientRect();
  const r = img.getBoundingClientRect();
  canvas.style.position = "absolute";
  canvas.style.left = (r.left - wRect.left + wrap.scrollLeft) + "px";
  canvas.style.top = (r.top - wRect.top + wrap.scrollTop) + "px";
  canvas.style.width = r.width + "px";
  canvas.style.height = r.height + "px";
  canvas.width = Math.round(r.width);
  canvas.height = Math.round(r.height);
}
img.addEventListener("load", roiRect);
window.addEventListener("resize", roiRect);

function imgCoords(ev) {
  const r = img.getBoundingClientRect();
  const sx = img.naturalWidth / r.width;
  const sy = img.naturalHeight / r.height;
  return {
    x: Math.round((ev.clientX - r.left) * sx),
    y: Math.round((ev.clientY - r.top) * sy),
    sx, sy, r,
  };
}

canvas.addEventListener("mousedown", (ev) => {
  if (!state.job) return;
  const c = imgCoords(ev);
  drag = { x0: c.x, y0: c.y, c };
});
window.addEventListener("mousemove", (ev) => {
  if (!drag) return;
  const c = imgCoords(ev);
  drawRect(drag.x0, drag.y0, c.x, c.y);
});
window.addEventListener("mouseup", async (ev) => {
  if (!drag) return;
  const c = imgCoords(ev);
  const x = Math.max(0, Math.min(drag.x0, c.x));
  const y = Math.max(0, Math.min(drag.y0, c.y));
  const w = Math.abs(c.x - drag.x0);
  const h = Math.abs(c.y - drag.y0);
  drag = null;
  if (w < 3 || h < 3) return;
  await queryRegion(x, y, w, h);
});

function drawRect(x0d, y0d, x1d, y1d) {
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const { sx, sy } = drag.c;
  const r = {
    x: Math.min(x0d, x1d) / sx,
    y: Math.min(y0d, y1d) / sy,
    w: Math.abs(x1d - x0d) / sx,
    h: Math.abs(y1d - y0d) / sy,
  };
  ctx.fillStyle = "rgba(58,110,165,.18)";
  ctx.strokeStyle = "#ffdd33";
  ctx.lineWidth = 1.5;
  ctx.fillRect(r.x, r.y, r.w, r.h);
  ctx.strokeRect(r.x, r.y, r.w, r.h);
}

async function queryRegion(x, y, w, h) {
  try {
    const data = await api(`/api/jobs/${state.job.job_id}/region`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ x, y, width: w, height: h }),
    });
    $("regionPanel").hidden = false;
    $("regionData").textContent = JSON.stringify(data, null, 2);
  } catch (err) {
    $("regionPanel").hidden = false;
    $("regionData").textContent = "区域查询失败：" + err.message;
  }
}

loadProfiles().catch((e) => showError("加载配置列表失败: " + e.message));
