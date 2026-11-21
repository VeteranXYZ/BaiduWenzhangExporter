/* The desktop UI reads the archive through the pywebview bridge. */
const shell = document.getElementById("shell");
const content = document.getElementById("content");
const pageTitle = document.getElementById("pageTitle");
const accountBtn = document.getElementById("accountBtn");
const taskIndicator = document.getElementById("taskIndicator");
const themeIcon = document.getElementById("themeIcon");
const themePreference = window.matchMedia("(prefers-color-scheme: dark)");

const TITLES = { dashboard: "归档概览", archives: "文章", media: "图片", missing: "缺失内容", export: "导出", settings: "设置" };
const ACTIVE = ["running", "starting", "stopping"];
const STAGES = ["读取收藏列表", "下载文章", "整理文章", "下载图片"];
const PAGE_SIZE = 24;

let current = "dashboard";
let stats = {};
let library = null;
let libraryError = false;
let credential = null;
let taskJob = null;
let preparing = false;
let preparationError = null;
let reading = null;
let images = null;
let problems = null;
let activeLoad = null;
let reloadVersion = 0;
let pollTimer = null;
let polling = false;
let starting = false;
let stopping = false;
let signingIn = false;
let signingOut = false;
let importing = false;
let exporting = false;
let exportNotice = null;
let loginNotice = null;
let themeMode = "system";

const selected = new Set();
const filters = { query: "", type: "page", year: "", status: "" };
let articlePage = 1;
let galleryYear = "";
let exportScope = "all";
let exportDestination = "choose";

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (name, size = 18) => `<svg width="${size}" height="${size}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const running = () => Boolean(taskJob && ACTIVE.includes(taskJob.status));
const expired = () => library?.connection?.state === "expired";
const loggedIn = () => Boolean(credential?.logged_in && !expired());
const canExport = () => !exporting && !running() && !starting && !preparing && !preparationError;
const canLogin = () => !signingIn && !signingOut && !importing && !running() && !starting && !preparing;
const canStartSync = () => !signingIn && !signingOut && !importing && !running() && !starting && !exporting && !preparing && !preparationError;
const isSavedPage = (r) => r.type === "page" && r.available;
const yearOf = (date) => /^\d{4}/.exec(date || "")?.[0] || "未知年份";
const yearsOf = (items) => [...new Set(items.map((r) => yearOf(r.date)))].sort(
  (a, b) => a === "未知年份" ? 1 : b === "未知年份" ? -1 : b.localeCompare(a));
const disabled = (condition) => condition ? "disabled" : "";
const pressed = (condition) => `aria-pressed="${Boolean(condition)}"`;

const STATUS = {
  complete: ["blue", "已保存"], parsed: ["blue", "已保存"],
  assets_partial: ["orange", "资源待补齐"], legacy_unverified: ["orange", "待核对"],
  warning: ["orange", "内容可能不完整"], failed: ["danger", "下载失败"],
  auth_error: ["danger", "登录已失效"], pending: ["neutral", "未下载"],
};
const TASKS = {
  sync: "下载归档", index: "读取收藏列表", archive: "下载文章", fetch: "下载文章",
  assets: "下载图片", parse: "整理文章", verify: "检查完整性", export: "导出文章",
  export_images: "导出图片", "export-selection": "导出所选文章", connection: "测试连接",
  "retry-failed": "重试", retry_images: "补救缺失图片", all: "下载归档", repair: "整理旧数据",
};
const OUTCOME = {
  completed: ["blue", "完成"], stopped: ["neutral", "已停止"], partial: ["orange", "部分完成"],
  failed: ["danger", "失败"], interrupted: ["orange", "中断"],
};

const badge = ([tone, label]) => `<span class="badge tone-${tone}">${esc(label)}</span>`;
const taskName = (kind) => TASKS[String(kind || "").replace(/^ui:/, "")] || "归档任务";
const allRecords = () => library?.records || [];
const savedSelection = () => allRecords().filter((r) => selected.has(r.key) && isSavedPage(r));
const archiveCounts = () => {
  const s = library?.summary || {};
  const pages = s.pages ?? stats.page ?? 0;
  const saved = s.ready ?? 0;
  return {
    pages, saved, remaining: Math.max(0, pages - saved),
    images: s.downloaded ?? 0, missingImages: s.failed_images ?? 0, uncheckedImages: s.legacy_images ?? 0,
    externalEmbeds: s.external_embeds ?? 0,
    total: library?.records?.length ?? stats.historical_indexed ?? 0, size: stats.db_size || "—",
  };
};

function call(method, ...args) {
  const fn = window.pywebview?.api?.[method];
  if (!fn) throw new Error("桌面连接尚未就绪");
  return fn(...args);
}

async function api(method, ...args) {
  try { return await call(method, ...args); }
  catch (error) {
    console.error(method, error);
    toast("无法完成这项操作，请重试。", "error");
    return null;
  }
}

function toast(message, kind = "info") {
  const box = document.getElementById("toasts");
  while (box.childElementCount >= 3) box.firstElementChild.remove();
  const el = document.createElement("div");
  el.className = `toast toast-${kind}`;
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  el.textContent = message;
  box.appendChild(el);
  setTimeout(() => el.remove(), kind === "info" ? 5000 : 8500);
}

const panel = (body, extra = "") => `<section class="panel ${extra}">${body}</section>`;
const sectionHeader = (title, detail = "", action = "") =>
  `<div class="section-head"><div><h2>${title}</h2>${detail ? `<p>${detail}</p>` : ""}</div>${action}</div>`;
const empty = (symbol, title, detail, action = "") => panel(`
  <div class="empty"><span class="empty-icon">${icon(symbol, 23)}</span>
    <h2>${esc(title)}</h2>${detail ? `<p>${esc(detail)}</p>` : ""}${action}</div>`);
const loading = (title) => panel(`<div class="empty" role="status">
  <span class="empty-icon spin">${icon("sync", 23)}</span><h2>${title}</h2></div>`);
const action = (label, name, options = {}) => `<button type="button" class="btn ${options.style || ""}"
  data-act="${name}" ${disabled(options.disabled)}>${options.icon ? icon(options.icon, 16) : ""}${label}</button>`;
const jump = (label, page, style = "") => `<button type="button" class="btn ${style}" data-goto="${page}">${label}</button>`;
const openButton = (record) => `<button type="button" class="record-title" data-open="${esc(record.key)}">${esc(record.title || "无标题")}</button>`;

function browserRows() {
  const q = filters.query.trim().toLocaleLowerCase();
  return allRecords().filter((r) => (
    (filters.type === "all" || (filters.type === "external" ? r.type !== "page" : r.type === filters.type)) &&
    (!filters.year || yearOf(r.date) === filters.year) &&
    (!filters.status || (r.type === "page" && (filters.status === "saved" ? r.available : !r.available))) &&
    (!q || [r.title, r.excerpt].some((value) => String(value || "").toLocaleLowerCase().includes(q)))
  ));
}

function articleSlice() {
  const matches = browserRows();
  const total = matches.length;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  articlePage = Math.max(1, Math.min(articlePage, pages));
  const start = (articlePage - 1) * PAGE_SIZE;
  return { rows: matches.slice(start, start + PAGE_SIZE), total, pages, start };
}

function progressCard(job) {
  const step = STAGES.indexOf(job.phase);
  const total = Number(job.total);
  const done = Math.min(Math.max(0, Number(job.done) || 0), total);
  const counted = (job.phase === "下载文章" || job.kind === "retry_images") && total > 0;
  const pct = counted ? Math.round(done / total * 100) : null;
  return panel(`
    ${sectionHeader(job.status === "stopping" ? "正在停止" : taskName(job.kind),
      job.status === "stopping" ? "正在等待本次请求和文件保存结束。" : "你可以继续浏览已经保存的文章。",
      action("停止任务", "stop", { disabled: stopping || job.status === "stopping" }))}
    ${job.kind === "sync" ? `<ol class="stage-list">${STAGES.map((label, i) => `
      <li class="${i < step ? "done" : i === step ? "now" : ""}" ${i === step ? 'aria-current="step"' : ""}>
        <span>${i < step ? "✓" : i + 1}</span>${label}</li>`).join("")}</ol>` : ""}
    <div class="progress" role="progressbar" aria-label="${esc(job.phase || "任务进度")}"
      ${counted ? `aria-valuenow="${pct}"` : ""}><i class="${counted ? "" : "indeterminate"}" style="width:${pct ?? 30}%"></i></div>
    <div class="progress-caption"><span>${esc(job.phase || "准备中")}</span>${counted ? `<strong>${done} / ${total} ${job.kind === "retry_images" ? "项" : "篇"}</strong>` : ""}</div>`, "progress-panel");
}

function metric(symbol, tone, title, value, note, page) {
  const tag = page ? "button" : "div";
  return `<${tag} class="metric" ${page ? `type="button" data-goto="${page}"` : ""}>
    <span class="metric-icon tone-${tone}">${icon(symbol)}</span>
    <span class="metric-label">${title}</span>
    <strong>${esc(value)}</strong><small>${esc(note)}</small>
  </${tag}>`;
}

function dashboard() {
  if (preparing) return loading("正在整理旧归档");
  if (!library && !libraryError) return loading("正在读取本地归档");
  if (!library) return empty("alert", "无法读取归档", "请重试，或在设置中查看归档位置。", action("重新读取", "refresh"));
  const s = archiveCounts();
  const online = loggedIn();
  const verifiedIndex = library.index_generation?.ok === true;
  const hasData = s.total > 0;
  const mainAction = !online
    ? action(expired() ? "重新登录百度" : "登录百度", "login", { style: "on-hero", icon: "user", disabled: !canLogin() })
    : action(s.remaining ? "继续下载" : hasData || verifiedIndex ? "检查更新" : "开始下载",
      "sync", { style: "on-hero", icon: "sync", disabled: !canStartSync() });
  const missingCount = s.remaining + s.missingImages + s.uncheckedImages + s.externalEmbeds;
  const title = hasData ? `已保存 ${s.saved} 篇空间文章` :
    verifiedIndex ? "当前账号没有可归档的收藏" : "把百度空间带回本地";
  const description = hasData
    ? [
      s.remaining ? `${s.remaining} 篇已索引文章还未保存。` : `本地有 ${s.images} 个图片引用已保存。`,
      !verifiedIndex ? "收藏列表尚未确认完整。" : "",
      expired() ? "百度登录已失效。" : "",
    ].filter(Boolean).join(" ")
    : verifiedIndex ? "你可以随时检查百度收藏是否有更新。"
      : online ? "下载空间文章和图片，之后可离线阅读和导出。" : "通过百度官方页面登录后，即可下载文章和图片。";
  const outcome = OUTCOME[taskJob?.status];
  return `
    ${running() ? progressCard(taskJob) : ""}
    <section class="hero">
      <div><p class="hero-label">${icon("db", 15)}${hasData ? "本地归档" : "百度空间"}</p>
        <h2>${title}</h2><p>${description}</p></div>
      ${!running() ? `<div class="hero-actions">${mainAction}${s.saved ? jump("导出备份", "export", "hero-secondary") : ""}</div>` : ""}
    </section>
    ${hasData ? `<section class="metrics" aria-label="归档统计">
      ${metric("inbox", "blue", "空间文章", `${s.saved} / ${s.pages}`, "已保存 / 已索引", "archives")}
      ${metric("image", "blue", "本地图片", s.images, "已保存的引用", "media")}
      ${metric("alert", missingCount ? "orange" : "neutral", "缺失或待核对", missingCount, "查看原因与状态", "missing")}
      ${metric("db", "neutral", "归档占用", s.size, `共 ${s.total} 条收藏`)}
    </section>` : ""}
    ${!running() && outcome ? panel(`<div class="activity"><div>
      <h2>上次任务 · ${esc(taskName(taskJob.kind))}</h2><p>${esc(taskJob.message || "")}</p></div>${badge(outcome)}</div>`) : ""}
    ${!hasData && !running() && !verifiedIndex ? panel(`<div class="setup-steps">
      <div>${icon("user")}<strong>连接百度</strong></div>
      <div>${icon("sync")}<strong>下载内容</strong></div>
      <div>${icon("inbox")}<strong>离线浏览</strong></div>
    </div>`) : ""}`;
}

function archives() {
  if (!library) return catalogUnavailable("文章");
  const all = allRecords();
  if (!all.length) return empty("inbox", "还没有文章", "完成首次下载后，文章会显示在这里。", jump("返回首页", "dashboard"));
  const types = [["page", "空间文章", all.filter((r) => r.type === "page").length],
    ["external", "其他收藏", all.filter((r) => r.type !== "page").length], ["all", "全部", all.length]];
  const years = yearsOf(all);
  if (filters.year && !years.includes(filters.year)) filters.year = "";
  return `
    ${panel(`
      <div class="segments" aria-label="收藏类别">${types.map(([type, label, count]) =>
        `<button type="button" data-filter="${type}" ${pressed(filters.type === type)}>${label}<span>${count}</span></button>`).join("")}</div>
      <div class="filters">
        <label class="search-field"><span class="sr-only">搜索标题和摘要</span>${icon("search")}
          <input id="searchBox" type="search" placeholder="搜索标题和摘要"></label>
        <label><span class="sr-only">按年份筛选</span><select id="yearFilter">
          <option value="">所有年份</option>${years.map((y) => `<option ${y === filters.year ? "selected" : ""}>${y}</option>`).join("")}</select></label>
        <label><span class="sr-only">按保存状态筛选</span><select id="statusFilter" ${disabled(filters.type === "external")}>
          <option value="">所有状态</option>
          <option value="saved" ${filters.status === "saved" ? "selected" : ""}>已保存</option>
          <option value="pending" ${filters.status === "pending" ? "selected" : ""}>未保存</option>
        </select></label>
      </div>
      <div class="selection-bar">
        <div class="inline-actions">
          ${action("全选本页", "select-page")}
          ${action("清空勾选", "clear-selection")}
          <span id="selectionCount" class="muted" role="status"></span>
        </div>
        <button type="button" class="btn primary" data-act="export-selected">导出所选</button>
      </div>
    `, "article-toolbar")}
    ${panel('<div id="articleResults"></div>', "article-results")}`;
}

function updateArticleResults({ preserveFocus = false } = {}) {
  const target = document.getElementById("articleResults");
  if (!target) return;
  const { rows, total, pages, start } = articleSlice();
  const active = preserveFocus ? document.activeElement : null;
  const checkedKey = active?.dataset?.check;
  const direction = active?.dataset?.act;
  target.innerHTML = `
    <div class="list-head" id="resultHeading" tabindex="-1"><strong>${total} 条结果</strong>
      <span>${total ? `第 ${start + 1}–${start + rows.length} 条` : "可调整搜索和筛选条件"}</span></div>
    ${rows.length ? rows.map((r) => `
      <div class="record">
        ${r.type === "page" ? `<input type="checkbox" aria-label="选择 ${esc(r.title || "无标题")}"
          data-check="${esc(r.key)}" ${selected.has(r.key) ? "checked" : ""} ${disabled(!isSavedPage(r))}>` : ""}
        <div class="record-body">
          ${isSavedPage(r) ? openButton(r) : `<strong>${esc(r.title || "无标题")}</strong>`}
          <p class="record-meta">${esc(r.date || "日期未知")}${r.images ? ` · 图片 ${r.downloaded}/${r.images}` : ""}</p>
          ${r.excerpt ? `<p class="excerpt">${esc(r.excerpt)}</p>` : ""}
          ${r.bookmark_available ? `<button type="button" class="record-title record-source" data-bookmark="${esc(r.key)}"
            aria-label="打开原地址 ${esc(r.title)}">打开原地址</button>` : ""}
        </div>
        ${badge(r.type === "page" ? STATUS[r.status] || STATUS.pending : ["neutral", "外部收藏"])}
      </div>`).join("") : '<p class="no-results">没有符合条件的收藏。</p>'}
    ${pages > 1 ? `<div class="pager" aria-label="文章分页">
      ${action("上一页", "prev-page", { disabled: articlePage === 1 })}
      <span role="status">第 ${articlePage} / ${pages} 页</span>
      ${action("下一页", "next-page", { disabled: articlePage === pages })}
    </div>` : ""}`;
  updateSelection();
  if (checkedKey) [...target.querySelectorAll("[data-check]")].find((el) => el.dataset.check === checkedKey)?.focus();
  else if (direction) target.querySelector("#resultHeading").focus({ preventScroll: true });
}

function updateSelection() {
  const counter = document.getElementById("selectionCount");
  if (!counter) return;
  const n = selected.size;
  counter.textContent = n ? `已选 ${n} 篇（含其他页）` : "未勾选";
  content.querySelector('[data-act="clear-selection"]').disabled = !n;
  content.querySelector('[data-act="export-selected"]').disabled = !n;
  const boxes = [...content.querySelectorAll("[data-check]:not(:disabled)")];
  for (const box of boxes) box.checked = selected.has(box.dataset.check);
  content.querySelector('[data-act="select-page"]').disabled = boxes.every((box) => box.checked);
}

function catalogUnavailable(noun) {
  return libraryError ? empty("alert", `无法读取${noun}`, "请重新读取本地归档。", action("重新读取", "refresh")) :
    loading(`正在读取${noun}`);
}

function media() {
  if (images === null) return loading("正在读取本地图片");
  if (images.error) return empty("alert", "无法读取图片", "请稍后重试。", action("重新读取", "refresh-view"));
  if (!images.length) return empty("image", "还没有保存的图片", "已通过校验的图片会显示在这里。", jump("查看文章", "archives"));
  const years = yearsOf(images);
  if (galleryYear && !years.includes(galleryYear)) galleryYear = "";
  const shown = galleryYear ? images.filter((img) => yearOf(img.date) === galleryYear) : images;
  const groups = new Map();
  for (const img of shown) {
    const year = yearOf(img.date);
    if (!groups.has(year)) groups.set(year, []);
    groups.get(year).push(img);
  }
  return `
    ${panel(`<div class="section-head gallery-toolbar">
      <div><h2>${shown.length} 张本地图片</h2><p>点击图片可阅读它所在的文章。</p></div>
      <div class="inline-actions"><label><span class="sr-only">图片年份</span><select id="galleryYear">
        <option value="">所有年份</option>${years.map((y) => `<option ${y === galleryYear ? "selected" : ""}>${y}</option>`).join("")}
      </select></label>${jump("导出图片", "export")}</div>
    </div>`)}
    ${years.filter((year) => groups.has(year)).map((year) => `
      <section class="gallery-year"><h2>${year}<small>${groups.get(year).length} 张</small></h2>
      <div class="gallery-grid">${groups.get(year).map((img) => `
        <button type="button" class="photo" data-open="${esc(img.key)}" aria-label="打开文章 ${esc(img.title)}">
          <span class="photo-image"><img data-thumb="${Number(img.id)}" alt=""></span>
          <span class="photo-caption"><strong>${esc(img.title)}</strong><small>${esc(img.date || "日期未知")}</small></span>
        </button>`).join("")}</div></section>`).join("")}`;
}

function missing() {
  if (problems === null) return loading("正在检查归档内容");
  if (problems.error) return empty("alert", "无法读取缺失记录", "请重新检查本地归档。", action("重新读取", "refresh-view"));
  const { auth = [], not_saved: unsaved = [], baidu_deleted: removed = [], dead_images: failed = [], unverified = [],
    external_embeds: embeds = [] } = problems;
  const pending = [...auth, ...unsaved];
  if (![pending, removed, failed, unverified, embeds].some((group) => group.length)) {
    return empty("inbox", library?.index_generation?.ok ? "未发现缺失内容" : "暂时没有缺失记录",
      library?.index_generation?.ok ? "已索引的归档没有报告缺失项。" : "读取完整收藏列表后才能确认归档范围。");
  }
  function issueGroup(title, detail, group, open, extra) {
    const count = group.reduce((n, r) => n + (r.images?.length || 1), 0);
    return group.length ? panel(`
      ${sectionHeader(`${title} · ${count}`, detail, extra)}
      <details class="issue-details" data-issue="${title}"><summary>查看 ${group.length} 篇文章</summary>
        ${group.map((r) => `<div class="record"><div class="record-body">
          ${open ? openButton(r) : `<strong>${esc(r.title)}</strong>`}
          <p class="record-meta">${esc(r.date || "日期未知")}${r.images ? ` · ${r.images.length} 项` : ""}</p>
          ${r.images?.length ? `<ul class="issue-reasons">${r.images.map((img) =>
            `<li>${img.domain ? `<strong>${esc(img.domain)}</strong> · ` : ""}${esc(img.reason)}
              ${img.retryable ? ` <button type="button" class="btn quiet" data-retry-image="${Number(img.id)}" ${disabled(!canStartSync())}>补救这项</button>` : ""}
              ${img.url ? `<div class="issue-url">${esc(img.url)}</div>` : ""}</li>`).join("")}</ul>` : ""}
        </div></div>`).join("")}
      </details>`) : "";
  }
  const retry = (label) => loggedIn() ? action(label, "retry-articles", { style: "primary", disabled: !canStartSync() })
    : action("登录百度", "login", { style: "primary", disabled: !canLogin() });
  const retryGroup = (group, label, name) => {
    const n = imageRetryIds(group).length;
    return n ? action(`${label}（${n} 项）`, name, { style: "primary", disabled: !canStartSync() }) : "";
  };
  return [
    issueGroup("正文还未保存", auth.length ? "部分记录需要重新登录后重试。" : "可继续下载未保存的文章。", pending, false, retry("继续下载")),
    issueGroup("原图不可用", "百度返回了占位图。只尝试原文明确链接的替代地址。", removed, true,
      retryGroup(removed, "尝试替代图片", "retry-alternates")),
    issueGroup("图片未保存", "可重试本组未保存的图片。已验证的图片会跳过。", failed, true,
      retryGroup(failed, "重试缺失图片", "retry-images")),
    issueGroup("旧图片待核对", "这些图片需要补齐校验记录。", unverified, true),
    issueGroup("外部嵌入", "归档保留原始地址。离线阅读页会标明该媒体无法播放。", embeds, true),
  ].join("");
}

function imageRetryIds(group) {
  return [...new Set((group || []).flatMap((r) => (r.images || []).filter((img) => img.retryable).map((img) => img.id)))];
}

function exportPage() {
  if (!library) return catalogUnavailable("导出数据");
  const s = archiveCounts();
  const chosen = savedSelection();
  const available = exportScope === "selected" ? chosen.length : s.saved;
  const pictureCount = exportScope === "selected" ? chosen.reduce((n, r) => n + (r.downloaded || 0), 0) : s.images;
  const busyHint = running() ? '<p class="inline-note">下载结束后即可导出。</p>' : "";
  return `
    ${panel(`<label class="setting-field export-location">保存位置
      <select id="exportDestination" ${disabled(exporting)}>
        <option value="choose" ${exportDestination === "choose" ? "selected" : ""}>选择文件夹</option>
        <option value="archive" ${exportDestination === "archive" ? "selected" : ""}>归档内的导出文件夹</option>
      </select></label>`)}
    ${panel(sectionHeader("离线备份",
      s.saved ? `把 ${s.saved} 篇已保存文章导出为可离线阅读的文件夹，附带文本、目录和 WordPress 导入文件。`
        : "下载文章后可生成离线备份。",
      action(exportDestination === "archive" ? "导出到归档目录" : "选择位置并导出", "export-all",
        { style: "primary", icon: "export", disabled: !canExport() || !s.saved })), "export-main")}
    ${panel(`
      ${sectionHeader("按格式导出", "选择范围，再导出文本或图片。")}
      <div class="segments export-scope" aria-label="导出范围">
        <button type="button" data-scope="all" ${pressed(exportScope === "all")}>全部已保存</button>
        <button type="button" data-scope="selected" ${pressed(exportScope === "selected")}>已勾选 <span>${chosen.length}</span></button>
      </div>
      ${exportScope === "selected" && !chosen.length ? `<p class="inline-note">还没有勾选文章。 ${jump("去文章列表", "archives", "quiet")}</p>` : ""}
      <div class="option-row"><div>${icon("doc")}<div><strong>文章文本</strong><small>Markdown 和 TXT · ZIP</small></div></div>
        ${action("导出文本", "export-text", { disabled: !canExport() || !available })}</div>
      <div class="option-row"><div>${icon("image")}<div><strong>本地图片</strong><small>原图和校验清单 · ZIP</small></div></div>
        ${action("导出图片", "export-images", { disabled: !canExport() || !pictureCount })}</div>
    `)}
    ${busyHint}
    ${exportNotice ? `<div class="feedback tone-${exportNotice.tone}" role="status">${esc(exportNotice.text)}${exportNotice.warnings?.length ?
      `<ul>${exportNotice.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}</div>` : ""}`;
}

function settings() {
  const c = loggedIn();
  const s = library?.settings;
  const history = library?.history || [];
  return `
    ${panel(`
      ${sectionHeader("百度账号", "登录状态保存在本机。退出后仍可浏览已下载的内容。",
        badge(c ? ["blue", "已登录"] : expired() ? ["orange", "已失效"] : ["neutral", "未登录"]))}
      <div class="inline-actions">${action(c || expired() ? "重新登录" : "登录百度", "login",
        { style: "primary", icon: "user", disabled: !canLogin() })}
        ${credential?.present ? action("退出登录", "logout", { disabled: !canLogin() }) : ""}
      </div>
      <details class="advanced-login" id="loginDetails"><summary>登录窗口无法使用？</summary>
        <p>在浏览器登录百度，打开开发者工具 Network，找到收藏列表的 <code>/fav/list</code> 请求并复制为 cURL。</p>
        <label for="curlInput">粘贴 cURL</label><textarea id="curlInput" rows="4" spellcheck="false" autocomplete="off"></textarea>
        ${action("导入登录状态", "save-curl", { disabled: !canLogin() })}
      </details>
      <p class="inline-note" id="loginStatus" role="status">${esc(loginNotice || "")}</p>`)}
    <div class="settings-grid">
      ${panel(sectionHeader("外观") + `<label class="setting-field">主题 <select id="themeMode">
        ${[["system", "跟随系统"], ["light", "浅色"], ["dark", "深色"]].map(([mode, label]) =>
          `<option value="${mode}" ${mode === themeMode ? "selected" : ""}>${label}</option>`).join("")}
      </select></label>`)}
      ${panel(sectionHeader("下载速度") + `<p class="muted">${s ? `同时下载 ${esc(s.concurrency)} 篇，请求间隔 ${esc(s.delay)} 秒。` : "待读取归档设置。"}</p>
        ${Object.values(stats.ignored_settings || {}).map((message) => `<p class="inline-note">${esc(message)}</p>`).join("")}`)}
    </div>
    ${panel(sectionHeader("归档位置", `<span class="selectable">${esc(stats.root || "archive/")}</span>`, action("打开目录", "reveal")))}
    ${panel(sectionHeader("最近任务", "", action("刷新", "refresh")) +
      (history.length ? history.map((h) => `<div class="option-row"><div><div><strong>${esc(taskName(h.command))}</strong>
        <small>${esc(new Date(h.started_at).toLocaleString("zh-CN", { hour12: false }))}</small></div></div>
        ${badge(OUTCOME[h.outcome] || ["neutral", "未知"])} </div>`).join("") : '<p class="muted">还没有任务记录。</p>'))}`;
}

function reader() {
  const back = action("← 返回", "reader-back", { style: "quiet" });
  if (reading.state === "loading") return back + loading("正在打开文章");
  if (reading.state === "error") return back + empty("alert", "无法打开文章", reading.error, action("重试", "reader-retry"));
  const a = reading.article;
  const metadata = [a.date && `收藏于 ${a.date}`, a.source, a.tags?.length && [...new Set(a.tags)].join("、")].filter(Boolean);
  return `
    <div class="reader-nav">${back}</div>
    <article class="panel reader">
      <header><h2>${esc(a.title)}</h2><p class="muted">${metadata.map(esc).join(" · ")}</p></header>
      ${a.note ? `<p class="reader-note">${esc(a.note)}</p>` : ""}
      ${a.images_missing ? `<p class="inline-note">${a.images_missing} 个媒体引用无法在此预览。</p>` : ""}
      <iframe id="readerFrame" sandbox="allow-same-origin" title="文章正文"></iframe>
    </article>`;
}

const PAGES = { dashboard, archives, media, missing, export: exportPage, settings };

let lastMarkup = null;
let lastReaderArticle = null;
let lastCatalog = null;
let readerObserver = null;
let thumbObserver = null;
let thumbnailQueue = [];
let activeThumbnails = 0;

function render() {
  const menuPage = reading ? "archives" : current;
  pageTitle.textContent = reading ? "阅读文章" : TITLES[current];
  for (const link of document.querySelectorAll("[data-page]")) {
    if (link.dataset.page === menuPage) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  taskIndicator.hidden = !running();
  taskIndicator.querySelector("span").textContent = taskJob?.status === "stopping" ? "正在停止" : taskJob?.phase || "任务进行中";
  renderAccount();

  const markup = preparationError && current !== "settings"
    ? empty("alert", "归档暂未打开", preparationError, action("重试打开", "refresh"))
    : reading ? reader() : PAGES[current]();
  const article = reading?.article || null;
  if (markup === lastMarkup && article === lastReaderArticle) {
    if (!reading && current === "archives" && lastCatalog !== library) {
      lastCatalog = library;
      updateArticleResults({ preserveFocus: true });
    }
    return;
  }
  lastMarkup = markup;
  lastReaderArticle = article;
  lastCatalog = library;
  const draft = document.getElementById("curlInput")?.value;
  const detailsOpen = document.getElementById("loginDetails")?.open;
  const activeId = content.contains(document.activeElement) ? document.activeElement.id : "";
  readerObserver?.disconnect();
  thumbObserver?.disconnect();
  thumbnailQueue = [];
  content.innerHTML = markup;
  const search = document.getElementById("searchBox");
  if (search) {
    search.value = filters.query;
    updateArticleResults();
  }
  if (!reading && current === "media") observeThumbnails();
  if (article) mountReader(article);
  const textarea = document.getElementById("curlInput");
  if (textarea && draft) textarea.value = draft;
  if (textarea && detailsOpen) document.getElementById("loginDetails").open = true;
  if (activeId) document.getElementById(activeId)?.focus({ preventScroll: true });
}

function observeThumbnails() {
  thumbObserver = new IntersectionObserver((entries, observer) => {
    for (const entry of entries) {
      if (!entry.isIntersecting || !entry.target.isConnected) continue;
      observer.unobserve(entry.target);
      thumbnailQueue.push(entry.target);
    }
    loadThumbnails();
  }, { root: content, rootMargin: "180px" });
  for (const img of content.querySelectorAll("[data-thumb]")) thumbObserver.observe(img);
}

function loadThumbnails() {
  while (activeThumbnails < 3 && thumbnailQueue.length) {
    const img = thumbnailQueue.shift();
    if (!img.isConnected) continue;
    activeThumbnails++;
    Promise.resolve().then(() => call("get_thumbnail", Number(img.dataset.thumb))).then((src) => {
      if (img.isConnected && src) img.src = src;
      else if (img.isConnected) img.closest(".photo").classList.add("thumbnail-failed");
    }, () => {
      if (img.isConnected) img.closest(".photo").classList.add("thumbnail-failed");
    }).finally(() => { activeThumbnails--; loadThumbnails(); });
  }
}

function mountReader(article) {
  const frame = document.getElementById("readerFrame");
  frame.addEventListener("load", () => {
    if (!frame.isConnected) return;
    const doc = frame.contentDocument;
    if (!doc?.body) return;
    doc.addEventListener("keydown", (event) => { if (event.key === "Escape") backToList(); });
    function resize() {
      if (frame.isConnected) frame.style.height = `${Math.max(96, doc.body.getBoundingClientRect().height + 4)}px`;
    }
    readerObserver = new ResizeObserver(resize);
    readerObserver.observe(doc.body);
    resize();
  }, { once: true });
  frame.srcdoc = `<!doctype html><html lang="zh-CN" class="${darkMode() ? "dark" : ""}"><head><meta charset="utf-8">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; media-src data:; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'">
    <style>
      html { color-scheme: light; color: #223242; }
      html.dark { color-scheme: dark; color: #e4ecef; }
      body { display: flow-root; margin: 0; font: 16px/1.85 -apple-system, BlinkMacSystemFont, "PingFang SC", system-ui, sans-serif; overflow-wrap: anywhere; }
      img,video,audio,table,pre { max-width: 100%; } img { height: auto; } pre,table { display: block; overflow-x: auto; } a { color: inherit; }
      img[data-archive-missing],img[data-archive-skipped] { display: inline-block; min-height: 2.5em; background: #80808018; }
    </style></head><body>${article.html || ""}</body></html>`;
}

function goto(page) {
  if (!PAGES[page]) return;
  reading = null;
  current = page;
  closeAccountMenu();
  render();
  content.scrollTop = 0;
  pageTitle.focus({ preventScroll: true });
  loadPageData();
}

async function loadPageData() {
  if (preparing || preparationError || reading || activeLoad?.page === current) return;
  const page = current;
  if ((page !== "media" || images !== null) && (page !== "missing" || problems !== null)) return;
  const request = { page, version: reloadVersion };
  activeLoad = request;
  const value = await api(page === "media" ? "get_images" : "get_problems");
  if (activeLoad === request) activeLoad = null;
  if (request.version !== reloadVersion) return;
  if (page === "media") images = value || { error: true };
  else problems = value || { error: true };
  if (!reading && current === page) render();
}

async function openArticle(key, trigger) {
  const origin = reading?.origin || {
    page: current, scroll: content.scrollTop, key: trigger?.dataset.open,
    image: trigger?.querySelector("[data-thumb]")?.dataset.thumb,
    issue: trigger?.closest("[data-issue]")?.dataset.issue,
  };
  const request = { key, origin, state: "loading" };
  reading = request;
  render();
  content.scrollTop = 0;
  pageTitle.focus({ preventScroll: true });
  const result = await api("get_article", key);
  if (reading !== request) return;
  if (!result || result.error) Object.assign(request, { state: "error", error: result?.error || "无法读取本地正文。" });
  else Object.assign(request, { state: "ready", article: result });
  render();
}

function backToList() {
  if (!reading) return;
  const origin = reading.origin;
  reading = null;
  current = origin.page;
  render();
  const group = [...content.querySelectorAll("[data-issue]")].find((el) => el.dataset.issue === origin.issue);
  if (group) group.open = true;
  const trigger = [...content.querySelectorAll("[data-open]")].find((el) =>
    el.dataset.open === origin.key && (!origin.image || el.querySelector("[data-thumb]")?.dataset.thumb === origin.image));
  trigger?.focus({ preventScroll: true });
  content.scrollTop = origin.scroll;
  loadPageData();
}

async function reload() {
  const version = ++reloadVersion;
  const [quick, data] = await Promise.all([api("get_stats"), api("get_library")]);
  if (version !== reloadVersion) return;
  stats = quick || stats;
  libraryError = !data || Boolean(data.error);
  if (data && !data.error) {
    library = data;
    const keep = new Set(allRecords().filter(isSavedPage).map((r) => r.key));
    for (const key of selected) if (!keep.has(key)) selected.delete(key);
  } else if (library && data?.error) toast("无法刷新归档，当前显示上次成功读取的数据。", "warn");
  images = null;
  problems = null;
  render();
  loadPageData();
}

function schedulePoll() {
  if (pollTimer !== null || polling || !running()) return;
  pollTimer = setTimeout(pollTask, 1200);
}

async function pollTask() {
  pollTimer = null;
  polling = true;
  const id = taskJob?.id;
  try {
    const job = await call("get_task");
    if (taskJob?.id !== id || !job || job.id !== id) return;
    if (taskJob.status === "stopping" && job.status === "running") return;
    taskJob = job;
    if (!running()) {
      const kind = job.status === "failed" ? "error" : job.status === "partial" ? "warn" : job.status === "completed" ? "success" : "info";
      toast(job.message || OUTCOME[job.status]?.[1] || "任务已结束", kind);
      await reload();
    } else render();
  } catch (error) {
    console.warn("读取任务进度失败，稍后重试", error);
  } finally {
    polling = false;
    schedulePoll();
  }
}

async function startSync() {
  if (!canStartSync()) return;
  if (!loggedIn()) return beginLogin();
  return startDownload("start_task", "sync", []);
}

async function startDownload(method, ...args) {
  if (!canStartSync()) return;
  starting = true;
  render();
  const result = await api(method, ...args);
  starting = false;
  if (!result?.ok) {
    render();
    if (result) toast(result.error || "无法开始下载", "error");
    return;
  }
  taskJob = result.job;
  goto("dashboard");
  schedulePoll();
}

async function stopTask() {
  if (stopping || !running()) return;
  stopping = true;
  render();
  const result = await api("stop_task");
  stopping = false;
  if (result && result.id === taskJob?.id && running() && ACTIVE.includes(result.status)) taskJob = result;
  render();
  schedulePoll();
}

async function runExport(kind) {
  if (!canExport()) return;
  const keys = exportScope === "selected" ? savedSelection().map((r) => r.key) : [];
  if (kind !== "all" && exportScope === "selected" && !keys.length) return;
  exporting = true;
  const intoArchive = exportDestination === "archive";
  exportNotice = { tone: "blue", text: intoArchive ? "正在导出到归档的导出文件夹…" : "正在导出，请在目录选择器中确认保存位置。" };
  render();
  const result = kind === "all" ? await api("export_all", intoArchive) : await api("export_to", kind, keys, intoArchive);
  exporting = false;
  if (result?.ok) {
    const missing = result.missing_resources ?? result.not_downloaded ?? 0;
    const warnings = [...(result.warnings || [])];
    if (result.missing_articles) warnings.unshift(`${result.missing_articles} 篇未纳入的正文已列入清单。`);
    if (missing) warnings.unshift(`${missing} 个未纳入的资源引用已列入清单。`);
    const partial = warnings.length > 0;
    exportNotice = { tone: partial ? "orange" : "blue", text: `${partial ? "部分导出已保存到" : "已导出到"} ${result.path}`, warnings };
    toast(partial ? "部分导出已完成，请查看清单。" : "导出完成", partial ? "warn" : "success");
  } else if (result?.cancelled) {
    exportNotice = { tone: "neutral", text: "已取消导出。" };
  } else {
    exportNotice = { tone: "danger", text: `导出失败：${result?.error || "请重试。"}` };
    if (current !== "export" && result) toast(exportNotice.text, "error");
  }
  render();
}

function darkMode() {
  return themeMode === "dark" || (themeMode === "system" && themePreference.matches);
}

function applyTheme(mode, save = false) {
  themeMode = ["system", "light", "dark"].includes(mode) ? mode : "system";
  const dark = darkMode();
  document.documentElement.classList.toggle("app-dark", dark);
  themeIcon.setAttribute("href", dark ? "#i-sun" : "#i-moon");
  document.getElementById("themeBtn").setAttribute("aria-label", dark ? "切换到浅色主题" : "切换到深色主题");
  document.getElementById("readerFrame")?.contentDocument?.documentElement.classList.toggle("dark", dark);
  const field = document.getElementById("themeMode");
  if (field) field.value = themeMode;
  if (save) api("set_theme", themeMode);
}

function renderAccount() {
  const logged = loggedIn();
  document.getElementById("acctName").textContent = logged ? "百度账号" : expired() ? "登录已失效" : "登录百度";
  accountBtn.classList.toggle("is-logged-in", logged);
  accountBtn.disabled = signingIn || (!logged && !canLogin());
  for (const item of accountMenu.querySelectorAll("[data-act]")) item.disabled = !canLogin();
}

function updateLoginNotice(message) {
  loginNotice = message;
  const el = document.getElementById("loginStatus");
  if (el) el.textContent = message || "";
}

async function beginLogin() {
  if (!canLogin()) return;
  signingIn = true;
  updateLoginNotice("请在打开的百度窗口中完成登录。");
  render();
  const r = await api("start_login");
  if (!r?.ok && signingIn) {
    signingIn = false;
    updateLoginNotice(r?.error || "无法打开百度登录窗口。");
    render();
    if (r) toast(loginNotice, "error");
  }
}

async function refreshAccount() {
  credential = await api("credential_state") || credential;
  await reload();
}

window.onLoginResult = async (payload) => {
  signingIn = false;
  updateLoginNotice(payload?.ok ? "登录成功。" : payload?.cancelled ? "已取消登录。" : payload?.error || "登录未完成。");
  if (!payload?.cancelled) toast(loginNotice, payload?.ok ? "success" : "error");
  if (payload?.ok) await refreshAccount();
  else render();
};

async function importLogin() {
  if (!canLogin()) return;
  const text = document.getElementById("curlInput")?.value.trim();
  if (!text) return updateLoginNotice("请先粘贴 cURL。");
  importing = true;
  render();
  const result = await api("import_curl_text", text);
  importing = false;
  if (!result?.ok) { updateLoginNotice(result?.error || "导入失败。"); render(); return; }
  const input = document.getElementById("curlInput");
  if (input) input.value = "";
  const details = document.getElementById("loginDetails");
  if (details) details.open = false;
  updateLoginNotice("登录状态已导入。");
  await refreshAccount();
}

async function logout() {
  if (!canLogin()) return;
  signingOut = true;
  render();
  try {
    if (!await api("confirm", "退出百度登录？", "已下载的文章和图片仍可在本机浏览。")) return;
    const result = await api("logout");
    if (!result?.ok) return toast(result?.error || "退出登录失败", "error");
    await refreshAccount();
    toast(result.env_cookie_still_set ? "已清除保存的登录状态，终端中的 Cookie 环境变量仍在生效。" : "已退出百度登录。",
      result.env_cookie_still_set ? "warn" : "success");
  } finally {
    signingOut = false;
    render();
  }
}

const accountMenu = document.getElementById("accountMenu");
function closeAccountMenu(restore = false) {
  if (accountMenu.hidden) return;
  accountMenu.hidden = true;
  accountBtn.setAttribute("aria-expanded", "false");
  if (restore) accountBtn.focus();
}

const ACTIONS = {
  sync: startSync,
  "retry-articles": () => startDownload("start_task", "archive", [...new Set([
    ...(problems?.auth || []), ...(problems?.not_saved || []),
  ].map((r) => r.key))]),
  "retry-images": () => startDownload("retry_images", imageRetryIds(problems?.dead_images)),
  "retry-alternates": () => startDownload("retry_images", imageRetryIds(problems?.baidu_deleted)),
  stop: stopTask,
  login: beginLogin,
  logout,
  "save-curl": importLogin,
  reveal: () => api("reveal_archive"),
  refresh: () => preparationError ? prepareAndReload() : reload(),
  "refresh-view": () => { if (current === "media") images = null; else problems = null; render(); loadPageData(); },
  "reader-back": backToList,
  "reader-retry": () => openArticle(reading.key),
  "export-all": () => runExport("all"),
  "export-text": () => runExport("selection"),
  "export-images": () => runExport("images"),
  "export-selected": () => { exportScope = "selected"; goto("export"); },
  "prev-page": () => { articlePage--; updateArticleResults({ preserveFocus: true }); content.scrollTop = 0; },
  "next-page": () => { articlePage++; updateArticleResults({ preserveFocus: true }); content.scrollTop = 0; },
  "select-page": () => { articleSlice().rows.filter(isSavedPage).forEach((r) => selected.add(r.key)); updateSelection(); },
  "clear-selection": () => { selected.clear(); updateSelection(); },
};

document.addEventListener("click", (event) => {
  const el = event.target.closest("button");
  if (!accountMenu.contains(event.target) && el !== accountBtn) closeAccountMenu();
  if (!el || el.disabled) return;
  const { page, goto: jumpTo, open, bookmark, filter, scope, act, retryImage } = el.dataset;
  if (accountMenu.contains(el)) closeAccountMenu(true);
  if (page || jumpTo) return goto(page || jumpTo);
  if (open) return openArticle(open, el);
  if (bookmark) return api("open_bookmark", bookmark).then((ok) => {
    if (ok === false) toast("无法打开原地址。", "error");
  });
  if (retryImage) return startDownload("retry_images", [Number(retryImage)]);
  if (filter) {
    filters.type = filter;
    if (filter === "external") filters.status = "";
    content.querySelector("#statusFilter").value = filters.status;
    content.querySelector("#statusFilter").disabled = filter === "external";
    articlePage = 1;
    for (const option of content.querySelectorAll("[data-filter]")) option.setAttribute("aria-pressed", option === el);
    return updateArticleResults();
  }
  if (scope) { exportScope = scope; render(); content.querySelector(`[data-scope="${scope}"]`)?.focus(); return; }
  if (act) ACTIONS[act]?.();
});

function updateQuery(event) {
  if (event.target.id !== "searchBox" || event.isComposing) return;
  filters.query = event.target.value;
  articlePage = 1;
  updateArticleResults();
}
document.addEventListener("input", updateQuery);
document.addEventListener("compositionend", updateQuery);

document.addEventListener("change", (event) => {
  const el = event.target;
  if (el.dataset.check) {
    if (el.checked) selected.add(el.dataset.check);
    else selected.delete(el.dataset.check);
    updateSelection();
  } else if (el.id === "yearFilter" || el.id === "statusFilter") {
    filters[el.id === "yearFilter" ? "year" : "status"] = el.value;
    articlePage = 1;
    updateArticleResults();
  } else if (el.id === "galleryYear") {
    galleryYear = el.value; render(); content.scrollTop = 0;
  } else if (el.id === "exportDestination") {
    exportDestination = el.value; render();
  } else if (el.id === "themeMode") applyTheme(el.value, true);
});

document.getElementById("toggleSidebar").addEventListener("click", (event) => {
  const collapsed = shell.classList.toggle("collapsed");
  event.currentTarget.setAttribute("aria-expanded", String(!collapsed));
  event.currentTarget.setAttribute("aria-label", collapsed ? "展开侧边栏" : "折叠侧边栏");
});
document.getElementById("themeBtn").addEventListener("click", () => applyTheme(darkMode() ? "light" : "dark", true));
themePreference.addEventListener("change", () => applyTheme(themeMode));
accountBtn.addEventListener("click", () => {
  if (!loggedIn()) return beginLogin();
  if (!accountMenu.hidden) return closeAccountMenu(true);
  accountMenu.hidden = false;
  accountBtn.setAttribute("aria-expanded", "true");
  accountMenu.querySelector("button").focus();
});
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "f") {
    const input = document.getElementById("searchBox");
    if (input) { event.preventDefault(); input.focus(); input.select(); }
  }
  if (event.key === "Escape") {
    if (!accountMenu.hidden) closeAccountMenu(true);
    else if (reading) backToList();
  }
});

async function prepareAndReload(health) {
  if (preparing) return;
  if (!health || health.error || (health.needs_offline_repair && !running())) {
    preparing = true;
    preparationError = null;
    render();
    const prep = await api("prepare_archive");
    preparing = false;
    if (!prep || prep.state === "error" || (prep.state === "busy" && !running())) {
      preparationError = prep?.error || "暂时无法整理归档，请稍后重试。";
      render();
      return;
    }
  }
  await reload();
}

async function initialize() {
  const [creds, mode, job, health] = await Promise.all([
    api("credential_state"), api("get_theme"), api("get_task"), api("health"),
  ]);
  credential = creds;
  applyTheme(mode);
  taskJob = job;
  await prepareAndReload(health);
  schedulePoll();
}

window.refresh = ACTIONS.refresh;
applyTheme(themeMode);
render();
window.addEventListener("pywebviewready", initialize, { once: true });
