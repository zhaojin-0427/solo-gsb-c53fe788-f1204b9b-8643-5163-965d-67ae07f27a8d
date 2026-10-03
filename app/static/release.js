// 印刷配置版本发布与回归门禁 前端（原生 JavaScript，无框架）
"use strict";

const rel = {
  families: [],
  selected: null,   // family detail
  jobs: [],
  gate: null,       // currently displayed gate report
};

const r$ = (id) => document.getElementById(id);

async function relApi(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j && j.detail) msg = j.detail;
    } catch (_) {}
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  if (res.status === 204) return null;
  return res.json();
}

function relError(msg) {
  const box = r$("relError");
  if (!msg) {
    box.hidden = true;
    box.textContent = "";
  } else {
    box.hidden = false;
    box.textContent = msg;
  }
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function shaShort(s) {
  return s ? String(s).slice(0, 12) + "…" : "";
}

function jobOptionsHtml(jobs) {
  if (!jobs || !jobs.length) return '<option value="">（暂无任务）</option>';
  return jobs.map((j) => `
    <option value="${j.job_id}">
      ${j.job_id.slice(0, 10)}… ${j.width}×${j.height} ${esc(j.image_mode)}
      ｜印刷 ${esc(String(j.press_profile).slice(0, 24))}
      ｜ΔE均值 ${(j.de_mean ?? 0).toFixed(2)}
    </option>`).join("");
}

function statusPill(status) {
  const map = {
    current: ["current", "当前版本"],
    draft: ["draft", "草稿"],
    superseded: ["superseded", "已取代"],
    withdrawn: ["withdrawn", "已撤回"],
  };
  const [cls, text] = map[status] || ["superseded", status];
  return `<span class="pill ${cls}">${text}</span>`;
}

// --------------------------------------------------------------------------
// 加载与族列表
// --------------------------------------------------------------------------

async function loadFamilies(keepSelection) {
  // 基线创建需要最近任务列表，与族列表一并刷新。
  const [families, jobs] = await Promise.all([
    relApi("/api/release/families"),
    relApi("/api/jobs"),
  ]);
  rel.families = families;
  rel.jobs = jobs;
  const list = r$("familyList");
  if (!rel.families.length) {
    list.innerHTML = `<div class="small">还没有配置族。</div>`;
  } else {
    list.innerHTML = rel.families.map((f) => {
      const cur = f.current
        ? `<span class="pill current">v${f.current.version_no}</span>`
        : `<span class="pill withdrawn">无当前版本</span>`;
      return `
        <div class="family-item ${keepSelection === f.family_id ? "active" : ""}"
             data-id="${f.family_id}">
          <div class="fname">${esc(f.name)} ${cur}</div>
          <div class="fmeta">
            版本 ${f.version_count} · 基线 ${f.baseline_count} ·
            族版本号 r${f.revision}
            ${f.open_draft ? " · 有开放草稿" : ""}
          </div>
        </div>`;
    }).join("");
    list.querySelectorAll(".family-item").forEach((el) => {
      el.addEventListener("click", () => selectFamily(el.dataset.id));
    });
  }
  if (keepSelection) await selectFamily(keepSelection, true);
}

async function selectFamily(familyId, preserve) {
  relError("");
  rel.selected = await relApi(`/api/release/families/${familyId}`);
  rel.gate = null;
  if (!preserve) {
    // 同步左侧高亮
    document.querySelectorAll(".family-item").forEach((el) =>
      el.classList.toggle("active", el.dataset.id === familyId)
    );
  }
  renderDetail();
  renderAudit();
}

function refreshReleaseView() {
  relError("");
  Promise.all([
    relApi("/api/release/families"),
    relApi("/api/jobs"),
  ]).then(([families, jobs]) => {
    rel.families = families;
    rel.jobs = jobs;
    const keep = rel.selected ? rel.selected.family_id : null;
    return loadFamilies(keep);
  }).catch((e) => relError("加载失败: " + e.message));
}

r$("relRefresh").addEventListener("click", () => refreshReleaseView());

r$("createFamilyBtn").addEventListener("click", async () => {
  relError("");
  const name = r$("newFamilyName").value.trim();
  if (!name) return relError("请输入配置族名称");
  try {
    const f = await relApi("/api/release/families", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    r$("newFamilyName").value = "";
    await loadFamilies(f.family_id);
  } catch (e) {
    relError("新建失败: " + e.message);
  }
});

// --------------------------------------------------------------------------
// 族详情：基线 / 草稿 / 门禁 / 版本
// --------------------------------------------------------------------------

function renderDetail() {
  const f = rel.selected;
  r$("relWelcome").hidden = true;
  const box = r$("relDetail");
  box.hidden = false;

  // 确保最近任务已加载（基线下拉依赖）。
  if (!rel.jobs || !rel.jobs.length) {
    relApi("/api/jobs").then((jobs) => {
      rel.jobs = jobs;
      const sel = r$("baselineJob");
      if (sel && !sel.value) {
        sel.innerHTML = jobOptionsHtml(jobs);
      }
    }).catch(() => {});
  }

  const jobOptions = jobOptionsHtml(rel.jobs);

  box.innerHTML = `
    <h2 style="margin-top:0">${esc(f.name)}
      <span class="small mono">${f.family_id}</span>
      <span class="small">（族版本号 r${f.revision}）</span>
    </h2>

    <div class="rel-section">
      <h3>① 门禁基线（固定图像 / 源·显示配置 / 参数与上限）</h3>
      <div id="baselineArea"></div>
      <div class="rel-grid" style="margin-top:8px">
        <label>代表性任务（从最近已保存任务选择）
          <select id="baselineJob">${jobOptions || '<option value="">（暂无任务）</option>'}</select>
        </label>
        <label>基线名称
          <input type="text" id="baselineName" placeholder="例如：客户标准样张 A">
        </label>
        <label>允许 ΔE 均值上限 <input type="number" id="limMean" value="2.0" min="0" step="0.1"></label>
        <label>允许 ΔE P95 上限 <input type="number" id="limP95" value="4.0" min="0" step="0.1"></label>
        <label>允许超阈面积比例上限（0..1） <input type="number" id="limOver" value="0.02" min="0" max="1" step="0.001"></label>
        <label>允许遮罩变化像素上限（个） <input type="number" id="limChanged" value="500" min="0" step="1"></label>
      </div>
      <div class="row" style="margin-top:8px">
        <button type="button" id="addBaselineBtn">从所选任务固化基线</button>
      </div>
    </div>

    <div class="rel-section">
      <h3>② 上传候选印刷 ICC 草稿（同配置族，按 SHA-256 固定快照）</h3>
      <div class="row">
        <input type="file" id="draftFile" accept=".icc,.icm" style="flex:1">
        <input type="text" id="draftLabel" placeholder="草稿标签（可选）" style="max-width:220px">
        <button type="button" id="uploadDraftBtn">上传草稿</button>
      </div>
      <div id="draftArea" style="margin-top:8px"></div>
    </div>

    <div class="rel-section">
      <h3>③ 回归门禁结果</h3>
      <div id="gateArea" class="small">先上传草稿，然后运行门禁。</div>
    </div>

    <div class="rel-section">
      <h3>④ 版本与回滚</h3>
      <div id="versionsArea"></div>
    </div>`;

  renderBaselines();
  renderDraft();
  renderVersions();

  r$("addBaselineBtn").addEventListener("click", addBaseline);
  r$("uploadDraftBtn").addEventListener("click", uploadDraft);
}

function renderBaselines() {
  const f = rel.selected;
  const area = r$("baselineArea");
  if (!f.baselines.length) {
    area.innerHTML = `<div class="small">尚无基线。基线会固定任务的图像字节、源/显示 ICC、渲染意图、阈值以及遮罩参考。</div>`;
    return;
  }
  area.innerHTML = `
    <table class="rel-table">
      <thead><tr>
        <th>名称</th><th>来源任务</th><th>图像</th><th>意图 / 阈值</th>
        <th>ΔE均值</th><th>P95</th><th>超阈比例</th><th>变化像素</th>
      </tr></thead>
      <tbody>${f.baselines.map((b) => `
        <tr>
          <td>${esc(b.name)}<br><span class="small mono">${b.baseline_id.slice(0, 14)}…</span></td>
          <td class="mono small">${shaShort(b.source_job_id)}</td>
          <td class="small">${b.width}×${b.height} ${esc(b.image_mode)}<br>${shaShort(b.image_sha256)}</td>
          <td class="small">${b.source_intent}<br>${b.proof_intent}<br>阈值 ${b.threshold}</td>
          <td>≤ ${b.limits.de_mean}</td>
          <td>≤ ${b.limits.de_p95}</td>
          <td>≤ ${b.limits.over_ratio}</td>
          <td>≤ ${b.limits.changed_pixels}</td>
        </tr>`).join("")}
      </tbody>
    </table>`;
}

async function addBaseline() {
  relError("");
  const job_id = r$("baselineJob").value;
  const name = r$("baselineName").value.trim();
  if (!job_id) return relError("请先在“软打样”页生成一个任务，再选择它作为基线");
  if (!name) return relError("请填写基线名称");
  try {
    await relApi(`/api/release/families/${rel.selected.family_id}/baselines`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        job_id,
        limit_de_mean: Number(r$("limMean").value),
        limit_de_p95: Number(r$("limP95").value),
        limit_over_ratio: Number(r$("limOver").value),
        limit_changed_pixels: Number(r$("limChanged").value),
      }),
    });
    await selectFamily(rel.selected.family_id, true);
  } catch (e) {
    relError("固化基线失败: " + e.message);
  }
}

// -- 草稿 -------------------------------------------------------------------

function renderDraft() {
  const f = rel.selected;
  const area = r$("draftArea");
  const drafts = f.versions.filter((v) => v.status === "draft");
  if (!drafts.length) {
    area.innerHTML = `<div class="small">没有开放草稿。上传新草稿会自动取代之前未发布的草稿（历史记录保留）。</div>`;
    bindGateButtons();
    return;
  }
  area.innerHTML = drafts.map((d) => `
    <div class="kv" style="border:1px dashed #c7ccd6;border-radius:6px;padding:8px 10px">
      <b>${esc(d.profile_name)}</b> ${statusPill("draft")}
      ${d.label ? `<br>标签：${esc(d.label)}` : ""}
      <br>SHA-256：<code>${d.profile_sha256}</code>（${d.profile_size} 字节）
      <div class="row" style="margin-top:6px">
        <button type="button" class="btn gate-run-btn" data-id="${d.version_id}">③ 运行回归门禁</button>
        <a class="btn" href="/api/release/versions/${d.version_id}/icc"
           download="${d.version_id}.icc">下载草稿 ICC</a>
      </div>
    </div>`).join("");
  bindGateButtons();
}

function bindGateButtons() {
  document.querySelectorAll(".gate-run-btn").forEach((btn) => {
    btn.addEventListener("click", () => runGate(btn.dataset.id, btn));
  });
}

async function uploadDraft() {
  relError("");
  const file = r$("draftFile").files[0];
  if (!file) return relError("请先选择 .icc/.icm 候选印刷配置");
  const fd = new FormData();
  fd.append("file", file);
  fd.append("label", r$("draftLabel").value);
  const btn = r$("uploadDraftBtn");
  btn.disabled = true;
  try {
    const d = await relApi(
      `/api/release/families/${rel.selected.family_id}/drafts`,
      { method: "POST", body: fd }
    );
    r$("draftFile").value = "";
    r$("draftLabel").value = "";
    await selectFamily(rel.selected.family_id, true);
    if (d && d.version_id) {
      // 自动选中新草稿提示
      relError("");
    }
  } catch (e) {
    relError("草稿上传被拒绝: " + e.message);
  } finally {
    btn.disabled = false;
  }
}

// -- 门禁运行与结果 ----------------------------------------------------------

async function runGate(draftId, btn) {
  relError("");
  if (btn) btn.disabled = true;
  r$("gateArea").innerHTML = "正在按草稿快照固定重跑全部基线，请稍候…";
  try {
    const gate = await relApi(
      `/api/release/families/${rel.selected.family_id}/gates`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ draft_version_id: draftId }),
      }
    );
    rel.gate = gate;
    renderGate(draftId);
  } catch (e) {
    r$("gateArea").innerHTML =
      `<span class="error" style="display:block">门禁运行失败: ${esc(e.message)}</span>`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderGate(draftId) {
  const g = rel.gate;
  if (!g) {
    r$("gateArea").innerHTML = `<span class="small">先上传草稿，然后运行门禁。</span>`;
    return;
  }
  const ok = g.status === "passed";
  let html = `
    <div class="kv">
      运行：<code>${g.run_id}</code><br>
      草稿快照：<code>${g.draft_profile_sha256}</code><br>
      结果：
        <span class="pill ${ok ? "passed" : "failed"}">
          ${ok ? "全部通过，可以发布" : "存在失败项，拒绝发布、保留草稿"}
        </span>
      （${g.n_passed}/${g.n_baselines} 个基线通过）
    </div>`;

  for (const r0 of g.results) {
    const a = r0.actual, lim = r0.limits;
    html += `
    <div class="rel-section" style="background:#fff">
      <div><b>基线：${esc(r0.baseline_name)}</b>
        <span class="pill ${r0.passed ? "passed" : "failed"}">
          ${r0.passed ? "通过" : "失败"}
        </span>
        <span class="small mono">${r0.baseline_id.slice(0, 14)}…</span>
      </div>
      <table class="rel-table">
        <thead><tr><th>检查项</th><th>基线参考</th><th>草稿实测</th><th>允许上限</th><th>判定 / 原因</th></tr></thead>
        <tbody>
          ${r0.checks.map((c) => `
            <tr class="${c.passed ? "check-pass" : "check-fail"}">
              <td>${esc(c.name)}</td>
              <td>${c.reference}</td>
              <td><b>${c.actual}</b></td>
              <td>${c.limit}</td>
              <td>${c.passed ? "✅ 通过" : "❌ 失败"}：${esc(c.reason)}</td>
            </tr>`).join("")}
        </tbody>
      </table>
      <div class="small">
        最大 ΔE00 ${a.de_max.toFixed(3)} ｜ 超阈像素
        ${a.over_pixels}/${a.total_pixels} ｜ 遮罩变化像素
        <b>${a.changed_pixels}</b>
      </div>
      <div class="gate-thumbs">
        <figure>
          <img src="/api/release/gates/${g.run_id}/baselines/${r0.baseline_id}/softproof.png"
               alt="候选软打样">
          <figcaption>候选软打样（快照）</figcaption>
        </figure>
        <figure>
          <img src="/api/release/gates/${g.run_id}/baselines/${r0.baseline_id}/mask.png"
               alt="候选超阈遮罩">
          <figcaption>候选超阈遮罩</figcaption>
        </figure>
        <figure>
          <img src="/api/release/gates/${g.run_id}/baselines/${r0.baseline_id}/diff.png"
               alt="变化像素定位">
          <figcaption>
            变化像素定位
            （<span style="color:#3c78e6">蓝=消失超阈</span>，
             <span style="color:#e63c3c">红=新增超阈</span>，灰=一致）
          </figcaption>
        </figure>
      </div>
      <div class="row">
        <a class="btn"
           href="/api/release/gates/${g.run_id}/baselines/${r0.baseline_id}/changed_pixels.csv"
           download>下载变化像素坐标 CSV</a>
      </div>
    </div>`;
  }

  // 发布按钮（仅对本次门禁的草稿）
  if (g.draft_version_id === draftId) {
    html += `
      <div class="row" style="margin-top:8px">
        <input type="text" id="publishLabel" placeholder="发布标签（可选，如 v1 正式版）"
               style="max-width:260px">
        <button type="button" id="publishBtn" class="primary" ${ok ? "" : "disabled"}>
          ④ 用期望版本号 r${rel.selected.revision} 在事务中发布
        </button>
        ${ok ? "" : `<span class="small">门禁未全部通过时不能发布。</span>`}
      </div>`;
  }
  r$("gateArea").innerHTML = html;

  const pub = r$("publishBtn");
  if (pub) {
    pub.addEventListener("click", () =>
      publishDraft(g.draft_version_id, pub)
    );
  }
}

async function publishDraft(draftId, btn) {
  relError("");
  const expected = rel.selected.revision;
  const label = r$("publishLabel").value.trim();
  if (!confirm(`确认把该草稿发布为新版本？\n期望版本号（乐观锁）：r${expected}`)) return;
  btn.disabled = true;
  try {
    const f = await relApi(
      `/api/release/families/${rel.selected.family_id}/publish`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          draft_version_id: draftId,
          expected_revision: expected,
          label: label || null,
        }),
      }
    );
    rel.selected = f;
    rel.gate = f.gate_run || rel.gate;
    await loadFamilies(f.family_id);
  } catch (e) {
    relError(`发布失败${e.status === 409 ? "（并发冲突）" : ""}: ${e.message}`);
    await selectFamily(rel.selected.family_id, true);
  }
}

// -- 版本列表 / 撤回 / 恢复 ---------------------------------------------------

function renderVersions() {
  const f = rel.selected;
  const released = f.versions.filter((v) => v.version_no != null);
  if (!released.length) {
    r$("versionsArea").innerHTML =
      `<div class="small">尚无已发布版本。新版本发布后，旧当前版本会在同一事务中转为“已取代”，其 ICC 文件永不覆盖，历史任务继续绑定原文件。</div>`;
    return;
  }
  r$("versionsArea").innerHTML = `
    <table class="rel-table">
      <thead><tr>
        <th>版本</th><th>状态</th><th>配置</th><th>SHA-256</th>
        <th>发布时间</th><th>门禁</th><th>操作</th>
      </tr></thead>
      <tbody>${released.map((v) => `
        <tr>
          <td><b>v${v.version_no}</b>${v.label ? `<br><span class="small">${esc(v.label)}</span>` : ""}</td>
          <td>${statusPill(v.status)}</td>
          <td class="small">${esc(v.profile_name)}</td>
          <td class="small mono">${shaShort(v.profile_sha256)}</td>
          <td class="small">${esc(v.published_at || "")}</td>
          <td>${v.gate_run_id
            ? `<a href="#" class="gate-link small" data-run="${v.gate_run_id}">${v.gate_run_id.slice(0, 12)}…</a>`
            : '<span class="small">—</span>'}</td>
          <td>
            ${v.status === "current"
              ? `<button class="btn withdraw-btn" data-id="${v.version_id}">撤回当前版本</button>`
              : `<button class="btn restore-btn" data-id="${v.version_id}">恢复（回滚）到此版本</button>`}
            <a class="btn" href="/api/release/versions/${v.version_id}/icc"
               download="${v.version_id}.icc">ICC</a>
          </td>
        </tr>`).join("")}
      </tbody>
    </table>`;

  document.querySelectorAll(".gate-link").forEach((a) => {
    a.addEventListener("click", async (ev) => {
      ev.preventDefault();
      try {
        rel.gate = await relApi(`/api/release/gates/${a.dataset.run}`);
        renderGate(null);
      } catch (e) {
        relError("加载历史门禁报告失败: " + e.message);
      }
    });
  });
  document.querySelectorAll(".withdraw-btn").forEach((b) => {
    b.addEventListener("click", () => versionAction(b.dataset.id, "withdraw"));
  });
  document.querySelectorAll(".restore-btn").forEach((b) => {
    b.addEventListener("click", () => versionAction(b.dataset.id, "restore"));
  });
}

async function versionAction(versionId, action) {
  relError("");
  const expected = rel.selected.revision;
  const verb = action === "withdraw" ? "撤回当前版本" : "恢复（回滚）到此版本";
  const reason = prompt(`${verb}\n请输入原因（可留空）：\n期望版本号（乐观锁）：r${expected}`, "");
  if (reason === null) return;
  try {
    await relApi(`/api/release/versions/${versionId}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: expected, reason: reason || null }),
    });
    await selectFamily(rel.selected.family_id, true);
  } catch (e) {
    relError(`${verb}失败${e.status === 409 ? "（并发冲突）" : ""}: ${e.message}`);
    await selectFamily(rel.selected.family_id, true);
  }
}

// -- 审计 --------------------------------------------------------------------

async function renderAudit() {
  try {
    const rows = await relApi(
      `/api/release/audit?family_id=${encodeURIComponent(rel.selected.family_id)}&limit=50`
    );
    if (!rows.length) {
      r$("auditList").innerHTML = `<div class="small">暂无审计记录。</div>`;
      return;
    }
    const actionText = {
      family_created: "创建配置族",
      draft_uploaded: "上传草稿",
      baseline_created: "固化基线",
      published: "发布版本",
      withdrawn: "撤回版本",
      restored: "恢复版本",
    };
    r$("auditList").innerHTML = rows.map((r) => `
      <div class="audit-item">
        <div class="a-top">
          #${r.seq} ${actionText[r.action] || r.action}
          <span class="small">r${r.revision} · ${esc(r.created_at)}</span>
        </div>
        <pre>${esc(JSON.stringify(r.payload, null, 2))}</pre>
        <div class="a-hash">hash=${r.entry_hash}<br>prev=${r.prev_hash || "（根）"}</div>
      </div>`).join("");
  } catch (e) {
    r$("auditList").innerHTML = `<div class="small">审计加载失败: ${esc(e.message)}</div>`;
  }
}
