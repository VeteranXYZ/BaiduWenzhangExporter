/* 界面逻辑。数据一律通过 window.pywebview.api 向 Python 要，前端不碰网络、不碰磁盘。 */

const shell = document.getElementById("shell");
const content = document.getElementById("content");
const pageTitle = document.getElementById("pageTitle");

const TITLES = {
  dashboard: "概览", archives: "归档内容", media: "媒体",
  tasks: "归档任务", skips: "异常跳过", export: "导出", settings: "设置",
};

/* ---------- 状态 ---------- */
let stats = { has_db: false, verified: false, complete: 0, warning: 0, failed: 0, pending: 0,
              page: 0, article: 0, other: 0, indexed: 0, total_api: 0,
              assets_downloaded: 0, assets_failed: 0, db_size: "—" };
let library = null;              // Application.library() 的结果
let libraryLoading = false;
let verifying = false;
let current = "dashboard";

const selected = new Set();
const filters = { query: "", type: "page" };
let reading = null;              // 正在阅读的文章

/* ---------- 与 Python 通信 ---------- */
async function api(method, ...args) {
  if (!window.pywebview?.api?.[method]) return null;
  try { return await window.pywebview.api[method](...args); }
  catch (err) { console.error(method, err); return null; }
}

/* 两级加载：先用数据库记账瞬时出数（约 1ms），再在后台逐字节校验（约 1s / 300 篇）。
   校验期间界面照常可用 —— pywebview 的 js_api 跑在独立线程上，不会冻结窗口。 */
async function refresh() {
  const quick = await api("get_stats");
  if (quick) { stats = quick; render(); }
  if (!quick?.has_db) return;
  verifying = true; render();
  const verified = await api("get_verified_stats");
  verifying = false;
  if (verified) stats = verified;
  render();
}

async function loadLibrary(force = false) {
  if (library && !force) return library;
  libraryLoading = true; render();
  library = await api("get_library");
  libraryLoading = false; render();
  return library;
}

/* ---------- 小部件 ---------- */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const statCard = (tone, icon, label, value, hint) => `
  <article class="stat-card">
    <span class="stat-icon tone-${tone}"><svg width="18" height="18"><use href="#i-${icon}"/></svg></span>
    <div><p>${label}</p><strong>${value}</strong><small>${esc(hint)}</small></div>
  </article>`;

const empty = (icon, title, desc) => `
  <div class="empty">
    <span><svg width="23" height="23"><use href="#i-${icon}"/></svg></span>
    <h4>${esc(title)}</h4><p>${esc(desc)}</p>
  </div>`;

const loading = (text) => `<div class="empty"><span class="spin"><svg width="23" height="23"><use href="#i-sync"/></svg></span><h4>${esc(text)}</h4><p>首次读取需要逐字节核对本地文件，请稍候。</p></div>`;

const STATUS_TONE = { complete: "green", warning: "orange", assets_partial: "orange",
                      legacy_unverified: "orange", failed: "danger", auth_error: "danger",
                      parsed: "blue", pending: "blue", skipped_non_page: "blue" };
const STATUS_TEXT = { complete: "已完成", warning: "有警告", assets_partial: "资源缺失",
                      legacy_unverified: "待补证", failed: "失败", auth_error: "认证失败",
                      parsed: "已解析", pending: "待抓取", skipped_non_page: "非文章" };
const badge = (status) =>
  `<span class="badge tone-${STATUS_TONE[status] || "blue"}">${STATUS_TEXT[status] || esc(status)}</span>`;

function records() {
  if (!library?.records) return [];
  const q = filters.query.trim().toLowerCase();
  return library.records.filter((r) => {
    if (filters.type !== "all" && r.type !== filters.type) return false;
    if (!q) return true;
    return (r.title || "").toLowerCase().includes(q)
        || (r.excerpt || "").toLowerCase().includes(q)
        || (r.key || "").toLowerCase().includes(q);
  });
}

/* ---------- 页面 ---------- */
const PAGES = {
  dashboard() {
    const pct = stats.page ? Math.round((stats.complete / stats.page) * 100) : 0;
    return `
    <section class="hero">
      <div>
        <p class="eyebrow">${stats.has_db ? `已索引 ${stats.indexed} 条` : "开始使用"}${
          verifying ? ' <span class="verifying">· 正在逐字节校验…</span>'
                    : (stats.verified ? ' <span class="verified-mark">· 已核实</span>' : "")}</p>
        <h2>${stats.has_db ? "继续归档你的百度空间" : "把十几年前的记忆，留在本地"}</h2>
        <p>${stats.has_db
          ? `本地已保存 ${stats.complete} 篇正文、${stats.assets_downloaded} 个资源。`
          : "导入 Cookie 后即可开始抓取文章、图片与外链记录。"}</p>
      </div>
      <button class="btn-light" data-task="${stats.has_db ? "archive" : ""}" id="primaryAction">
        <svg width="15" height="15"><use href="#i-${stats.has_db ? "sync" : "user"}"/></svg>
        ${stats.has_db ? "继续归档" : "导入 Cookie"}
      </button>
    </section>

    ${stats.has_db && stats.index_complete === false ? `
    <section class="notice">
      <span class="notice-icon"><svg width="18" height="18"><use href="#i-alert"/></svg></span>
      <div>
        <strong>索引尚未完整，因此没有文章会被标记为「已完成」</strong>
        <p>接口声明 ${stats.total_api || "—"} 条，当前已核实 ${stats.indexed} 条。
        在确认一条不漏之前，归档不会对单篇下「完整」的结论 —— 这是刻意的保守设计，
        不代表已抓到的内容有问题。补齐索引后，这些文章会自动转为已完成。</p>
      </div>
      <button class="btn-text" data-task="index">补齐索引</button>
    </section>` : ""}

    <section class="stats">
      ${statCard("blue", "doc", "总记录", stats.indexed || "—", stats.total_api ? `接口声明 ${stats.total_api} 条` : "等待首次索引")}
      ${statCard("green", "inbox", "已完成", stats.complete, stats.page ? `共 ${stats.page} 篇正文` : "暂无")}
      ${statCard("orange", "alert", "需关注", stats.warning + stats.failed, `${stats.warning} 警告 · ${stats.failed} 失败`)}
      ${statCard("purple", "db", "本地占用", stats.db_size, "SQLite + 原始响应")}
    </section>

    <section class="card-grid">
      <article class="surface-card">
        <div class="section-heading">
          <div><span class="eyebrow">PROGRESS</span><h3>归档进度</h3></div>
          <button class="btn-text" data-goto="tasks">查看任务</button>
        </div>
        <div class="row" style="border:0;padding-top:0">
          <div class="row-main"><strong>${stats.complete} / ${stats.page || "—"} 篇正文</strong><small>${pct}% 完成</small></div>
        </div>
        <div class="progress"><i style="width:${pct}%"></i></div>
        <div class="row"><div class="row-main"><strong>${stats.assets_downloaded}</strong><small>资源已校验可用</small></div>
          <span class="badge tone-green">正常</span></div>
        <div class="row"><div class="row-main"><strong>${stats.assets_failed}</strong><small>资源不可用（原始 URL 已保留）</small></div>
          <span class="badge ${stats.assets_failed ? "tone-orange" : "tone-green"}">${stats.assets_failed ? "部分失败" : "正常"}</span></div>
        ${stats.assets_legacy_unverified ? `<div class="row"><div class="row-main"><strong>${stats.assets_legacy_unverified}</strong><small>旧版本资源待补证，文件可能仍在</small></div><span class="badge tone-orange">待补证</span></div>` : ""}
      </article>

      <article class="surface-card">
        <div class="section-heading"><div><span class="eyebrow">TYPES</span><h3>类型分布</h3></div></div>
        ${stats.indexed ? `
          <div class="row"><div class="row-main"><strong>百度空间文章</strong><small>page</small></div><strong>${stats.page}</strong></div>
          <div class="row"><div class="row-main"><strong>外部收藏</strong><small>article</small></div><strong>${stats.article}</strong></div>
          <div class="row"><div class="row-main"><strong>其他</strong><small>other</small></div><strong>${stats.other}</strong></div>
        ` : empty("doc", "尚未索引", "运行索引后，全部收藏会登记在这里。")}
      </article>
    </section>`;
  },

  /* ---- 归档内容 ---- */
  archives() {
    if (reading) return READER();
    if (libraryLoading && !library) return `<article class="surface-card">${loading("正在读取归档内容")}</article>`;
    const list = records();
    const all = library?.records || [];
    return `
    <article class="surface-card toolbar-card">
      <div class="section-heading">
        <div><span class="eyebrow">LOCAL ARCHIVE</span><h3>归档内容</h3></div>
        <div class="toolbar-actions">
          <button class="btn-text" data-act="reload">刷新</button>
          <button class="btn-text" data-export="selection">导出所选</button>
          <button class="btn-text" data-export="images">导出图片</button>
        </div>
      </div>
      <div class="segmented">
        ${["page", "article", "all"].map((t) => `
          <button class="seg ${filters.type === t ? "is-active" : ""}" data-filter="${t}">
            ${{ page: "空间文章", article: "外部收藏", all: "全部" }[t]}
            <small>${t === "all" ? all.length : all.filter((r) => r.type === t).length}</small>
          </button>`).join("")}
      </div>
      <div class="search-row">
        <input type="search" id="searchBox" placeholder="搜索标题、摘要或 key" value="${esc(filters.query)}">
        <button class="btn-text" data-act="select-page">全选当前</button>
        <button class="btn-text" data-act="clear-sel">清空</button>
        <span class="sel-count">${selected.size ? `已选 ${selected.size}` : ""}</span>
      </div>
    </article>

    <article class="surface-card">
      ${list.length ? list.slice(0, 200).map((r) => `
        <div class="record" data-key="${esc(r.key)}">
          <label class="check"><input type="checkbox" data-check="${esc(r.key)}" ${selected.has(r.key) ? "checked" : ""} ${r.type !== "page" ? "disabled" : ""}></label>
          <div class="row-main" data-open="${esc(r.key)}">
            <strong>${esc(r.title || "无标题")}</strong>
            <small>${esc(r.date || "无日期")} · ${esc(r.key)}${r.images ? ` · 图片 ${r.downloaded}/${r.images}` : ""}</small>
            ${r.excerpt ? `<p class="excerpt">${esc(r.excerpt)}</p>` : ""}
          </div>
          ${badge(r.status)}
        </div>`).join("")
      : empty("inbox", library ? "没有匹配的记录" : "尚未抓取", library ? "换个关键词或筛选条件试试。" : "先在任务页运行索引与抓取。")}
      ${list.length > 200 ? `<p class="list-note">仅显示前 200 条（共 ${list.length} 条），用搜索缩小范围。</p>` : ""}
    </article>`;
  },

  /* ---- 媒体 ---- */
  media() {
    if (libraryLoading && !library) return `<article class="surface-card">${loading("正在统计媒体")}</article>`;
    const withImages = (library?.records || []).filter((r) => r.images > 0);
    const s = library?.summary || {};
    return `
    <section class="stats">
      ${statCard("purple", "image", "发现图片", s.images ?? 0, "正文中引用的图片总数")}
      ${statCard("green", "inbox", "本地可用", s.downloaded ?? 0, "已校验字节无误")}
      ${statCard("orange", "alert", "不可用", s.failed_images ?? 0, "原始 URL 已保留")}
      ${statCard("blue", "doc", "含图文章", withImages.length, `共 ${s.pages ?? 0} 篇`)}
    </section>
    <article class="surface-card">
      <div class="section-heading"><div><span class="eyebrow">MEDIA</span><h3>按文章浏览</h3></div>
        <button class="btn-text" data-export="images">导出全部图片</button></div>
      ${withImages.length ? withImages.map((r) => `
        <div class="record">
          <div class="row-main" data-open="${esc(r.key)}">
            <strong>${esc(r.title || "无标题")}</strong>
            <small>${esc(r.date || "无日期")} · 图片 ${r.downloaded}/${r.images}${r.failed ? ` · ${r.failed} 个不可用` : ""}</small>
          </div>
          <span class="badge tone-${r.failed ? "orange" : "green"}">${r.downloaded}/${r.images}</span>
        </div>`).join("")
      : empty("image", "暂无图片", "抓取并补齐图片后，含图文章会列在这里。")}
    </article>`;
  },

  /* ---- 任务 ---- */
  tasks() {
    const job = library?.job ?? taskJob;
    const hist = library?.history || [];
    const active = job && ["running", "starting", "stopping"].includes(job.status);
    return `
    <article class="surface-card">
      <div class="section-heading">
        <div><span class="eyebrow">TASKS</span><h3>${job ? esc(job.label || job.kind || "任务") : "没有进行中的任务"}</h3></div>
        ${active ? `<button class="btn-text danger" data-act="stop">停止</button>` : ""}
      </div>
      ${job ? `
        <div class="row" style="border:0;padding-top:0">
          <div class="row-main"><strong>${esc(job.message || job.status || "")}</strong>
          <small>${esc(job.status || "")}${job.started_at ? ` · 开始于 ${esc(job.started_at)}` : ""}</small></div>
          ${badge(job.status === "completed" ? "complete" : (job.status === "failed" ? "failed" : "parsed"))}
        </div>
        <div class="progress"><i style="width:${Math.min(100, Math.round((job.progress ?? 0) * 100))}%"></i></div>
      ` : `<p class="hint">选择下面任一操作开始。全部任务都可随时停止，已完成的部分不会丢失。</p>`}
      <div class="task-buttons">
        ${[["index", "更新收藏列表"], ["archive", "抓取文章"], ["assets", "补齐图片"], ["verify", "校验归档"]]
          .map(([k, label]) => `<button class="btn-task" data-task="${k}" ${active ? "disabled" : ""}>${label}</button>`).join("")}
      </div>
    </article>

    <article class="surface-card">
      <div class="section-heading"><div><span class="eyebrow">HISTORY</span><h3>最近运行</h3></div></div>
      ${hist.length ? hist.map((h) => `
        <div class="row"><div class="row-main"><strong>${esc(h.command)}</strong>
          <small>${esc(h.started_at || "")}${h.finished_at ? ` → ${esc(h.finished_at)}` : " · 未结束"}</small></div>
          <span class="badge tone-${h.outcome === "completed" ? "green" : "orange"}">${esc(h.outcome)}</span></div>`).join("")
      : empty("sync", "还没有运行记录", "运行过的任务会按时间列在这里。")}
    </article>`;
  },

  /* ---- 异常跳过 ---- */
  skips() {
    if (libraryLoading && !library) return `<article class="surface-card">${loading("正在核对异常条目")}</article>`;
    const bad = (library?.records || []).filter((r) =>
      ["failed", "auth_error", "warning", "assets_partial", "legacy_unverified"].includes(r.status));
    return `
    <article class="surface-card">
      <div class="section-heading">
        <div><span class="eyebrow">SKIPPED</span><h3>异常跳过列表</h3></div>
        ${bad.length ? `<button class="btn-text" data-task="archive" data-keys="all-bad">批量重试</button>` : ""}
      </div>
      <p class="hint">异常位置不会阻塞后续归档。失败的原始 URL 一律保留，不会从正文中删除。</p>
      ${bad.length ? bad.map((r) => `
        <div class="record">
          <div class="row-main" data-open="${esc(r.key)}">
            <strong>${esc(r.title || "无标题")}</strong>
            <small>${esc(r.key)}${r.images ? ` · 图片 ${r.downloaded}/${r.images}` : ""}${r.failed ? ` · ${r.failed} 个资源不可用` : ""}</small>
          </div>
          ${badge(r.status)}
          <button class="btn-text" data-retry="${esc(r.key)}">重试</button>
        </div>`).join("")
      : empty("inbox", "没有需要关注的条目", "所有已抓取的内容都通过了逐字节校验。")}
    </article>`;
  },

  /* ---- 导出 ---- */
  export() {
    const n = selected.size;
    return `
    <article class="surface-card">
      <div class="section-heading"><div><span class="eyebrow">EXPORT</span><h3>导出</h3></div>
        <span class="sel-count">${n ? `已选 ${n} 篇` : "未选择 = 全部"}</span></div>
      <p class="hint">导出只读取本地已校验的内容，不会发起任何网络请求。点击后会弹出系统目录选择器。</p>
      <div class="row"><div class="row-main"><strong>所选文章（ZIP）</strong><small>正文 HTML + 本地图片 + 元数据</small></div>
        <button class="btn-text" data-export="selection">选择目录</button></div>
      <div class="row"><div class="row-main"><strong>文章图片（ZIP）</strong><small>仅打包已下载的图片，未下载的写入清单</small></div>
        <button class="btn-text" data-export="images">选择目录</button></div>
      <div class="row"><div class="row-main"><strong>Markdown / TXT / WXR / 静态站点</strong><small>这些格式目前由命令行提供</small></div>
        <span class="hint-inline">baidu-archive export --format all</span></div>
    </article>`;
  },

  settings() {
    const s = library?.settings || {};
    return `
    <article class="surface-card">
      <div class="section-heading"><div><span class="eyebrow">SETTINGS</span><h3>设置</h3></div></div>
      <div class="row"><div class="row-main"><strong>凭证</strong><small>${
        library?.connection?.state === "saved" ? "已保存，存于 secrets/（权限 0600）" : "尚未提供"}</small></div>
        <button class="btn-text" data-act="import">导入 cURL</button></div>
      <div class="row"><div class="row-main"><strong>请求间隔</strong><small>面对遗留系统，可靠性优先于速度</small></div><strong>${esc(s.delay ?? "0.8")}s</strong></div>
      <div class="row"><div class="row-main"><strong>并发数</strong><small>建议不超过 3</small></div><strong>${esc(s.concurrency ?? "2")}</strong></div>
      <div class="row"><div class="row-main"><strong>归档目录</strong><small>${esc(stats.root || "archive/")}</small></div>
        <button class="btn-text" data-act="reveal">在访达中显示</button></div>
    </article>`;
  },
};

/* ---- 文章阅读视图 ---- */
function READER() {
  if (reading === "loading") return `<article class="surface-card">${loading("正在载入正文")}</article>`;
  if (reading?.error) return `<article class="surface-card">${empty("alert", "无法显示", reading.error)}
    <div style="text-align:center"><button class="btn-text" data-act="back">返回列表</button></div></article>`;
  const a = reading;
  return `
  <article class="surface-card reader">
    <button class="btn-text back-btn" data-act="back">← 返回列表</button>
    <h2>${esc(a.title)}</h2>
    <p class="reader-meta">${[a.date && `收藏于 ${a.date}`, a.source, a.tags?.length && `标签：${a.tags.join("、")}`,
      `图片 ${a.images_inlined} 张${a.images_missing ? `，${a.images_missing} 张不可用` : ""}`]
      .filter(Boolean).map(esc).join(" · ")}</p>
    ${a.note ? `<p class="reader-note">备注：${esc(a.note)}</p>` : ""}
    ${a.warnings?.length ? `<div class="notice"><span class="notice-icon"><svg width="18" height="18"><use href="#i-alert"/></svg></span>
      <div><strong>归档提示</strong><p>${esc(a.warnings.join("；"))}</p></div></div>` : ""}
    <div class="reader-body">${a.html}</div>
  </article>`;
}

/* ---------- 渲染 ---------- */
let taskJob = null;
function render() {
  content.innerHTML = `<div class="page">${PAGES[current]()}</div>`;
  pageTitle.textContent = reading && current === "archives" ? "阅读" : TITLES[current];
  const box = document.getElementById("searchBox");
  if (box) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
}

function goto(page) {
  if (!PAGES[page]) return;
  current = page; reading = null;
  document.querySelectorAll("[data-page]").forEach((el) =>
    el.classList.toggle("active", el.dataset.page === page));
  render();
  if (["archives", "media", "skips", "tasks", "settings"].includes(page)) loadLibrary();
}

async function openArticle(key) {
  current = "archives"; reading = "loading";
  document.querySelectorAll("[data-page]").forEach((el) =>
    el.classList.toggle("active", el.dataset.page === "archives"));
  render();
  reading = await api("get_article", key);
  render();
}

async function runTask(kind, keys) {
  const result = await api("start_task", kind, keys || []);
  if (result && !result.ok) { await api("notify", "无法开始", result.error || "未知错误"); return; }
  taskJob = result?.job || null;
  goto("tasks");
  pollTask();
}

let polling = null;
async function pollTask() {
  clearInterval(polling);
  polling = setInterval(async () => {
    taskJob = await api("get_task");
    if (current === "tasks") render();
    if (!taskJob || !["running", "starting", "stopping"].includes(taskJob.status)) {
      clearInterval(polling); polling = null;
      await loadLibrary(true); await refresh();
    }
  }, 1200);
}

/* ---------- 事件 ---------- */
document.addEventListener("click", async (e) => {
  const nav = e.target.closest("[data-page]");
  if (nav) return goto(nav.dataset.page);

  const jump = e.target.closest("[data-goto]");
  if (jump) return goto(jump.dataset.goto);

  const open = e.target.closest("[data-open]");
  if (open) return openArticle(open.dataset.open);

  const filter = e.target.closest("[data-filter]");
  if (filter) { filters.type = filter.dataset.filter; return render(); }

  const task = e.target.closest("[data-task]");
  if (task && task.dataset.task) {
    let keys = [];
    if (task.dataset.keys === "all-bad") {
      keys = (library?.records || []).filter((r) =>
        ["failed", "auth_error", "warning", "assets_partial"].includes(r.status)).map((r) => r.key);
    } else if (task.dataset.task === "archive" && selected.size) {
      keys = [...selected];
    }
    return runTask(task.dataset.task, keys);
  }

  const retry = e.target.closest("[data-retry]");
  if (retry) return runTask("archive", [retry.dataset.retry]);

  const exp = e.target.closest("[data-export]");
  if (exp) {
    const result = await api("export_to", exp.dataset.export, [...selected]);
    if (result?.ok) await api("notify", "导出完成", result.path);
    else if (result && !result.cancelled) await api("notify", "导出失败", result.error || "未知错误");
    return;
  }

  const act = e.target.closest("[data-act]");
  if (act) {
    const what = act.dataset.act;
    if (what === "back") { reading = null; return render(); }
    if (what === "reload") { selected.clear(); await loadLibrary(true); return; }
    if (what === "clear-sel") { selected.clear(); return render(); }
    if (what === "select-page") { records().slice(0, 200).filter((r) => r.type === "page").forEach((r) => selected.add(r.key)); return render(); }
    if (what === "stop") { taskJob = await api("stop_task"); return render(); }
    if (what === "reveal") return api("reveal_archive");
    if (what === "import") {
      const file = await api("choose_file");
      if (file) await api("notify", "已选择", "请在命令行运行 import-curl 完成导入");
      return;
    }
  }

  if (e.target.closest("#primaryAction") && !stats.has_db) {
    await api("notify", "需要凭证", "请先在设置页导入 Cookie");
  }
});

document.addEventListener("change", (e) => {
  const box = e.target.closest("[data-check]");
  if (!box) return;
  box.checked ? selected.add(box.dataset.check) : selected.delete(box.dataset.check);
  const counter = document.querySelector(".sel-count");
  if (counter) counter.textContent = selected.size ? `已选 ${selected.size}` : "";
});

document.addEventListener("input", (e) => {
  if (e.target.id !== "searchBox") return;
  filters.query = e.target.value;
  render();
});

document.getElementById("toggleSidebar").onclick = () => shell.classList.toggle("collapsed");

const themeBtn = document.getElementById("themeBtn");
themeBtn.onclick = () => {
  const dark = document.documentElement.classList.toggle("app-dark");
  document.getElementById("themeIcon").setAttribute("href", dark ? "#i-sun" : "#i-moon");
  api("set_theme", dark ? "dark" : "light");
};

const mq = window.matchMedia("(prefers-color-scheme: dark)");
function applySystemTheme(isDark) {
  document.documentElement.classList.toggle("app-dark", isDark);
  document.getElementById("themeIcon").setAttribute("href", isDark ? "#i-sun" : "#i-moon");
}
applySystemTheme(mq.matches);
mq.addEventListener("change", (ev) => applySystemTheme(ev.matches));

/* 让它不像网页：禁右键菜单、禁拖拽 */
document.addEventListener("contextmenu", (e) => e.preventDefault());
document.addEventListener("dragstart", (e) => e.preventDefault());

render();
window.addEventListener("pywebviewready", refresh);
