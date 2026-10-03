// 局部 CMYK 修色视图（原生 JavaScript）
"use strict";

const rtState = {
  jobs: [],
  job: null,      // /api/jobs/{id} 报告
  jobSummary: null,
  rect: null,
  result: null,
};

const rt$ = (id) => document.getElementById(id);

async function rtApi(path, options) {
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

function rtShowError(msg) {
  const box = rt$("rtError");
  if (!msg) { box.hidden = true; box.textContent = ""; return; }
  box.hidden = false;
  box.textContent = msg;
}

async function rtLoadJobs(selectId) {
  rtState.jobs = await rtApi("/api/jobs");
  const sel = rt$("rtJobSelect");
  sel.innerHTML = "";
  if (!rtState.jobs.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "（尚无已保存任务，请先在“软打样”页运行一次）";
    sel.appendChild(opt);
    return;
  }
  for (const j of rtState.jobs) {
    const opt = document.createElement("option");
    opt.value = j.job_id;
    opt.textContent =
      `${j.job_id.slice(0, 12)}… ${j.width}×${j.height} ${j.image_mode} · ` +
      `${new Date(j.created_at + "Z").toLocaleString()} · ΔE均值 ${j.de_mean?.toFixed(2) ?? "-"}`;
    sel.appendChild(opt);
  }
  if (selectId) sel.value = selectId;
}

async function rtSelectJob() {
  const id = rt$("rtJobSelect").value;
  rtState.jobSummary = rtState.jobs.find((j) => j.job_id === id) || null;
  if (!id) { rtState.job = null; return; }
  rtState.job = await rtApi(`/api/jobs/${id}`);
  rt$("rtJobMeta").innerHTML =
    `任务 <code>${id.slice(0, 12)}…</code> · ${rtState.job.image.width}×` +
    `${rtState.job.image.height} · 印刷配置 <b>${rtState.job.profiles.press.name}</b> ` +
    `(${rtState.job.profiles.press.color_space}) · 意图 ${rtState.job.parameters.source_intent_label}`;
  rt$("rtViewImage").src = `/api/jobs/${id}/images/softproof.png?cache=${id}`;
  rtState.rect = null;
  rt$("rtResultPanel").hidden = true;
}

// ---- 框选 ----------------------------------------------------------------
const rtImg = rt$("rtViewImage"), rtCanvas = rt$("rtRoiCanvas");
let rtDrag = null;

function rtRoiRect() {
  const wrap = rt$("rtCanvasWrap");
  const wRect = wrap.getBoundingClientRect();
  const r = rtImg.getBoundingClientRect();
  rtCanvas.style.position = "absolute";
  rtCanvas.style.left = (r.left - wRect.left + wrap.scrollLeft) + "px";
  rtCanvas.style.top = (r.top - wRect.top + wrap.scrollTop) + "px";
  rtCanvas.style.width = r.width + "px";
  rtCanvas.style.height = r.height + "px";
  rtCanvas.width = Math.round(r.width);
  rtCanvas.height = Math.round(r.height);
}
rtImg.addEventListener("load", rtRoiRect);
window.addEventListener("resize", rtRoiRect);

function rtImgCoords(ev) {
  const r = rtImg.getBoundingClientRect();
  return {
    x: Math.round((ev.clientX - r.left) * (rtImg.naturalWidth / r.width)),
    y: Math.round((ev.clientY - r.top) * (rtImg.naturalHeight / r.height)),
    r,
  };
}

rtCanvas.addEventListener("mousedown", (ev) => {
  if (!rtState.job) return;
  rtDrag = { c: rtImgCoords(ev) };
});
window.addEventListener("mousemove", (ev) => {
  if (!rtDrag) return;
  const c = rtImgCoords(ev);
  rtDrawRect(rtDrag.c.x, rtDrag.c.y, c.x, c.y);
});
window.addEventListener("mouseup", (ev) => {
  if (!rtDrag) return;
  const c = rtImgCoords(ev);
  const x0 = rtDrag.c.x, y0 = rtDrag.c.y;
  rtDrag = null;
  const x = Math.max(0, Math.min(x0, c.x));
  const y = Math.max(0, Math.min(y0, c.y));
  const w = Math.abs(c.x - x0), h = Math.abs(c.y - y0);
  if (w < 2 || h < 2) return;
  if (w > 64 || h > 64) {
    rtShowError(`选区 ${w}×${h} 超过 64×64 像素上限，请重新框选更小的矩形`);
    return;
  }
  rtShowError("");
  rtState.rect = { x, y, width: w, height: h };
  rt$("rtX").value = x; rt$("rtY").value = y;
  rt$("rtW").value = w; rt$("rtH").value = h;
});

function rtDrawRect(x0d, y0d, x1d, y1d) {
  const ctx = rtCanvas.getContext("2d");
  ctx.clearRect(0, 0, rtCanvas.width, rtCanvas.height);
  const r = rtImg.getBoundingClientRect();
  const sx = r.width / rtImg.naturalWidth;
  const sy = r.height / rtImg.naturalHeight;
  const rect = {
    x: Math.min(x0d, x1d) * sx,
    y: Math.min(y0d, y1d) * sy,
    w: Math.abs(x1d - x0d) * sx,
    h: Math.abs(y1d - y0d) * sy,
  };
  ctx.fillStyle = "rgba(255,170,0,.16)";
  ctx.strokeStyle = "#ff9900";
  ctx.lineWidth = 1.5;
  ctx.fillRect(rect.x, rect.y, rect.w, rect.h);
  ctx.strokeRect(rect.x, rect.y, rect.w, rect.h);
}

// ---- 运行搜索 -------------------------------------------------------------
rt$("rtRefreshJobs").addEventListener("click", () =>
  rtLoadJobs(rt$("rtJobSelect").value).catch((e) => rtShowError(e.message))
);
rt$("rtJobSelect").addEventListener("change", () =>
  rtSelectJob().catch((e) => rtShowError(e.message))
);

rt$("rtRunBtn").addEventListener("click", async () => {
  rtShowError("");
  if (!rtState.job) return rtShowError("请先选择一个已保存任务");
  let rect;
  try {
    rect = {
      x: parseInt(rt$("rtX").value, 10),
      y: parseInt(rt$("rtY").value, 10),
      width: parseInt(rt$("rtW").value, 10),
      height: parseInt(rt$("rtH").value, 10),
    };
  } catch (_) {
    return rtShowError("选区坐标必须是整数");
  }
  if ([rect.x, rect.y, rect.width, rect.height].some((v) => !Number.isInteger(v))) {
    return rtShowError("选区坐标必须是整数");
  }
  if (rect.width < 1 || rect.height < 1 || rect.width > 64 || rect.height > 64) {
    return rtShowError("选区宽高必须在 1..64 像素之间");
  }

  let L, a, b, tol, maxShift, tac;
  try {
    L = parseFloat(rt$("rtLabL").value);
    a = parseFloat(rt$("rtLabA").value);
    b = parseFloat(rt$("rtLabB").value);
    tol = parseFloat(rt$("rtTol").value);
    maxShift = parseInt(rt$("rtMaxShift").value, 10);
    tac = parseInt(rt$("rtTac").value, 10);
  } catch (_) {
    return rtShowError("约束参数无法解析为数值");
  }
  if (!(L >= 0 && L <= 100)) return rtShowError("目标 L* 必须在 0..100");
  if (!(a >= -128 && a <= 127)) return rtShowError("目标 a* 必须在 -128..127");
  if (!(b >= -128 && b <= 127)) return rtShowError("目标 b* 必须在 -128..127");
  if (!(tol >= 0) || !Number.isFinite(tol)) return rtShowError("ΔE00 容差必须是非负数");
  if (!Number.isInteger(maxShift) || maxShift < 0 || maxShift > 8)
    return rtShowError("单通道偏移上限必须是 0..8 的整数");
  if (!Number.isInteger(tac) || tac < 0 || tac > 1020)
    return rtShowError("总墨量上限必须是 0..1020 的整数");

  const body = {
    job_id: rtState.job.job_id,
    ...rect,
    target_lab: [L, a, b],
    tolerance_de: tol,
    max_shift: maxShift,
    tac_limit: tac,
  };

  rt$("rtRunBtn").disabled = true;
  rt$("rtBusy").hidden = false;
  try {
    rtState.result = await rtApi("/api/retouch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    rtRenderResult();
  } catch (err) {
    rtShowError(err.message);
  } finally {
    rt$("rtRunBtn").disabled = false;
    rt$("rtBusy").hidden = true;
  }
});

function rtLabText(v) {
  return Array.isArray(v) ? `(${v.map((x) => x.toFixed(2)).join(", ")})` : "—";
}

function rtRenderResult() {
  const r = rtState.result;
  rt$("rtResultPanel").hidden = false;
  const status = rt$("rtStatus");
  if (r.feasible_offset_count === 0) {
    status.className = "rt-status unreachable";
    status.textContent = "不可达：在给定通道/总墨量约束下没有任何可行的统一偏移，未生成成品。";
  } else if (!r.reachable) {
    status.className = "rt-status unreachable";
    status.textContent =
      `不可达：存在 ${r.feasible_offset_count} 组可行偏移，但最优 ΔE00=` +
      `${r.residual.de00_to_target} 仍大于容差 ${r.constraints.tolerance_de00}，未生成成品。`;
  } else {
    status.className = "rt-status reachable";
    status.textContent =
      `可达：统一偏移 (dC,dM,dY,dK)=(${r.best_offset_cmyk.join(", ")})，` +
      `平均 Lab 残余 ΔE00=${r.residual.de00_to_target} ≤ 容差 ${r.constraints.tolerance_de00}。`;
  }

  const res = r.residual;
  rt$("rtStats").innerHTML =
    `<span>可行偏移数 <b>${r.feasible_offset_count}</b></span>` +
    `<span>最优偏移绝对值和 <b>${r.best_abs_shift_sum ?? "—"}</b></span>` +
    `<span>修色前均 Lab <b>${rtLabText(res.mean_lab_before)}</b></span>` +
    `<span>修色后均 Lab <b>${rtLabText(res.mean_lab_after)}</b></span>` +
    `<span>目标 Lab <b>${rtLabText(res.target_lab)}</b></span>` +
    `<span>对目标 ΔE00 <b>${res.de00_to_target ?? "—"}</b></span>` +
    (r.tiff
      ? `<span><a class="btn primary" href="${r.tiff}" download>下载修色 TIFF</a></span>`
      : "");

  rt$("rtBeforeImg").src = r.images.before;
  rt$("rtAfterImg").src = r.images.after;
  rt$("rtResidual").innerHTML =
    `<h3>残余色差</h3>` +
    `<div class="kv">修色前平均 Lab → 目标 ΔE00：<b>${res.de00_before_to_target}</b></div>` +
    `<div class="kv">修色后平均 Lab → 目标 ΔE00：<b>${res.de00_to_target ?? "—"}</b></div>` +
    (r.reachable
      ? `<div class="kv">选区修色前/后逐像素 ΔE00 均值：<b>${res.mean_de00_before_after}</b>` +
        `；最大：<b>${res.max_de00_before_after}</b></div>`
      : `<div class="kv">未达容差，不生成修色后逐像素色差与成品 TIFF。</div>`);

  if (r.tiff) {
    rt$("rtExpTiff").hidden = false;
    rt$("rtExpTiff").href = r.tiff;
  } else {
    rt$("rtExpTiff").hidden = true;
  }
  rt$("rtExpReport").href = r.report_url;
  rt$("rtReportJson").textContent = JSON.stringify(r.report, null, 2);
}

// ---- 视图切换钩子（app.js 的导航处理调用） -------------------------------
async function refreshRetouchView() {
  try {
    if (!rtState.jobs.length) await rtLoadJobs();
    if (rtState.job === null && rtState.jobs.length) await rtSelectJob();
    // 视图从 hidden 切回可见后需重新定位覆盖层。
    setTimeout(rtRoiRect, 0);
  } catch (e) {
    rtShowError("加载已保存任务失败: " + e.message);
  }
}
