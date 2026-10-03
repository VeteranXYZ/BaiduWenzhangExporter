/* 界面逻辑。数据一律通过 window.pywebview.api 向 Python 要，前端不碰网络、不碰磁盘。
 *
 * 用词约定（界面上不出现技术术语）：
 *   归档/抓取 → 保存、下载      解析 → （不展示）      补证/legacy → 校验旧数据
 *   资源 → 图片                 索引 → 收藏列表        assets_partial → 部分图片缺失
 */

const shell = document.getElementById("shell");
const content = document.getElementById("content");
const pageTitle = document.getElementById("pageTitle");

const TITLES = {
  dashboard: "概览", archives: "文章", media: "图片",
  tasks: "下载", skips: "问题", export: "导出", settings: "设置",
};

/* ---------- 状态 ---------- */
let stats = { has_db: false, verified: false, complete: 0, warning: 0, failed: 0, pending: 0,
              page: 0, article: 0, other: 0, indexed: 0, total_api: 0,
              assets_downloaded: 0, assets_failed: 0, db_size: "—" };
let health = null;              // 有没有需要处理的事，以及怎么处理
let library = null;             // Application.library()
let problems = null;            // 按原因分组的问题
let images = null;              // 图片页数据
let libraryLoading = false;
let verifying = false;
let repairing = false;
let current = "dashboard";

const selected = new Set();
const filters = { query: "", type: "page" };
let reading = null;
let credential = null;
let exporting = false;
let exportMessage = "";
let taskJob = null;

/* ---------- 与 Python 通信 ---------- */
async function api(method, ...args) {
  if (!window.pywebview?.api?.[method]) return null;
  try { return await window.pywebview.api[method](...args); }
  catch (err) { console.error(method, err); toast(`操作失败：${err?.message || err}`, "error"); return null; }
}

/* 结果与错误一律在界面里提示 —— 系统通知可能被 macOS 静默，不能只靠它 */
function toast(text, kind = "info", ms = 5200) {
  const box = document.getElementById("toasts");
  if (!box || !text) return;
  const el = document.createElement("div");
  el.className = `toast toast-${kind}`;
  el.textContent = text;
  box.appendChild(el);
  setTimeout(() => { el.classList.add("leaving"); setTimeout(() => el.remove(), 240); }, ms);
}

/* 两级加载：先用数据库记账瞬时出数，再在后台逐个核对本地文件。 */
async function refresh() {
  const quick = await api("get_stats");
  if (quick) { stats = quick; render(); }
  health = await api("health");
  updateProblemBadge();
  if (!quick?.has_db) return render();
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

async function loadProblems(force = false) {
  if (problems && !force) return problems;
  problems = await api("get_problems");
  updateProblemBadge();
  render();
  return problems;
}

/* 无法找回的图片：百度删除的 + 第三方网站失效的 */
function lostImages() {
  if (!problems) return { articles: 0, images: 0 };
  const groups = [...(problems.baidu_deleted || []), ...(problems.dead_images || [])];
  return { articles: new Set(groups.map((g) => g.key)).size,
           images: groups.reduce((n, g) => n + g.images.length, 0) };
}

function problemTotal() {
  if (!problems) return 0;
  return Object.values(problems).reduce((n, list) => n + (list?.length || 0), 0);
}

function updateProblemBadge() {
  const el = document.getElementById("problemCount");
  if (!el) return;
  const n = problems ? problemTotal() : 0;
  el.hidden = !n;
  el.textContent = n > 99 ? "99+" : String(n);
}

/* ---------- 小部件 ---------- */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const statCard = (tone, icon, label, value, hint, page) => `
  <article class="stat-card ${page ? "is-link" : ""}" ${page ? `data-goto="${page}"` : ""}>
    <span class="stat-icon tone-${tone}"><svg width="18" height="18"><use href="#i-${icon}"/></svg></span>
    <div><p>${label}</p><strong>${value}</strong><small>${esc(hint)}</small></div>
  </article>`;

const empty = (icon, title, desc) => `
  <div class="empty">
    <span><svg width="23" height="23"><use href="#i-${icon}"/></svg></span>
    <h4>${esc(title)}</h4><p>${esc(desc)}</p>
  </div>`;

const loading = (text, desc = "正在逐个核对本地文件，请稍候。") =>
  `<div class="empty"><span class="spin"><svg width="23" height="23"><use href="#i-sync"/></svg></span><h4>${esc(text)}</h4><p>${esc(desc)}</p></div>`;

/* 页面顶部的一句话说明：这一页是干什么的 */
const intro = (text) => `<p class="page-intro">${text}</p>`;

/* 用户能看懂的状态 */
const STATUS = {
  complete:          ["green",  "已完整保存"],
  parsed:            ["blue",   "已保存"],
  assets_partial:    ["orange", "部分图片缺失"],
  legacy_unverified: ["orange", "旧数据待校验"],
  warning:           ["orange", "内容可能不完整"],
  failed:            ["danger", "下载失败"],
  auth_error:        ["danger", "登录失效"],
  pending:           ["blue",   "未下载"],
  skipped_non_page:  ["blue",   "外部收藏"],
};
const badge = (status) => {
  const [tone, text] = STATUS[status] || ["blue", status];
  return `<span class="badge tone-${tone}">${esc(text)}</span>`;
};

/* 任务名、结果、时间都翻成人话 */
const TASK_NAME = {
  index: "更新收藏列表", archive: "下载文章", fetch: "下载文章", assets: "下载图片",
  parse: "整理文章", verify: "检查完整性", export: "导出文章", export_images: "导出图片",
  "export-selection": "导出所选文章", connection: "测试连接", "retry-failed": "重试失败项",
  all: "完整下载", repair: "修复旧数据",
};
const OUTCOME = {
  completed: ["green", "完成"], stopped: ["orange", "已停止"], partial: ["orange", "部分完成"],
  failed: ["danger", "失败"], interrupted: ["orange", "中途中断"], running: ["blue", "进行中"],
  starting: ["blue", "准备中"], stopping: ["orange", "正在停止"],
};
const taskName = (cmd) => TASK_NAME[String(cmd || "").replace(/^ui:/, "")] || String(cmd || "任务").replace(/^ui:/, "");
const outcomeBadge = (o) => { const [t, x] = OUTCOME[o] || ["blue", o || "—"]; return `<span class="badge tone-${t}">${esc(x)}</span>`; };

function localTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const now = new Date();
  const hm = d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return `今天 ${hm}`;
  const sameYear = d.getFullYear() === now.getFullYear();
  return `${sameYear ? "" : d.getFullYear() + "年"}${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
}
function duration(a, b) {
  const s = Math.max(0, Math.round((new Date(b) - new Date(a)) / 1000));
  if (!a || !b || isNaN(s)) return "";
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.round(s / 60)} 分钟`;
  return `${(s / 3600).toFixed(1)} 小时`;
}

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

/* 「需要修复旧数据」横幅 —— 概览、图片、问题页共用 */
function repairBanner() {
  if (!health?.needs_offline_repair) return "";
  return `
  <section class="notice notice-strong">
    <span class="notice-icon"><svg width="18" height="18"><use href="#i-alert"/></svg></span>
    <div>
      <strong>有一批旧数据需要校验${health.legacy_assets ? `（${health.legacy_assets} 张图片）` : ""}</strong>
      <p>这些内容是用旧版本下载的，文件都还在你电脑上，只是缺少新版要求的校验记录，
      所以暂时被当作「未确认」——这就是图片显示 0 张、无法导出的原因。
      点右边的按钮会逐个核对本地文件并补上记录，<b>全程不联网</b>，大约需要半分钟。</p>
    </div>
    <button class="btn-primary" data-act="repair" ${repairing ? "disabled" : ""}>${repairing ? "正在修复…" : "一键修复"}</button>
  </section>`;
}

/* ---------- 页面 ---------- */
const PAGES = {
  /* ---- 概览 ---- */
  dashboard() {
    const pages = stats.page || 0;
    const lost = lostImages();
    const loggedIn = credential?.logged_in;

    if (!stats.has_db) return `
      <section class="hero">
        <div>
          <p class="eyebrow">开始使用</p>
          <h2>把百度空间里的文章，完整保存到你的电脑上</h2>
          <p>先登录百度账号，然后到「下载」页把收藏的文章和图片下载到本地。之后即使百度关闭，也能随时打开阅读、导出备份。</p>
        </div>
        <div class="hero-actions">
          <button class="btn-light" data-act="login"><svg width="15" height="15"><use href="#i-user"/></svg>${loggedIn ? "重新登录" : "登录百度账号"}</button>
        </div>
      </section>
      ${howItWorks()}`;

    return `
    ${repairBanner()}
    <section class="hero">
      <div>
        <p class="eyebrow">你的百度空间${verifying ? ' <span class="verifying">· 正在核对本地文件…</span>' : ""}</p>
        <h2>已完整保存 ${stats.complete} / ${pages} 篇文章</h2>
        <p>${stats.assets_downloaded} 张图片在本地${lost.images ? ` · ${lost.images} 张图片已无法找回（原图已被删除或原网站失效）` : ""}。
        ${stats.complete ? "这些内容已经在你电脑上了，随时可以浏览和导出。" : "保存完成后就可以浏览和导出了。"}</p>
      </div>
      <div class="hero-actions">
        ${stats.complete ? `<button class="btn-light" data-goto="export"><svg width="15" height="15"><use href="#i-export"/></svg>一键导出全部</button>` : ""}
        <button class="btn-ghost" data-goto="tasks"><svg width="15" height="15"><use href="#i-sync"/></svg>从百度更新</button>
      </div>
    </section>

    <section class="stats">
      ${statCard("green", "inbox", "文章", `${stats.complete} / ${pages}`, "已完整保存 / 空间文章总数", "archives")}
      ${statCard("purple", "image", "图片", stats.assets_downloaded, "已保存在本地，可浏览、可导出", "media")}
      ${statCard(problemTotal() ? "orange" : "green", "alert", "需要留意", problemTotal(),
                 problemTotal() ? "点击查看原因与处理方式" : "没有需要处理的问题", "skips")}
      ${statCard("blue", "db", "占用空间", stats.db_size, `共 ${stats.indexed || stats.historical_indexed || 0} 条收藏`)}
    </section>

    ${howItWorks()}`;
  },

  /* ---- 文章 ---- */
  archives() {
    if (reading) return READER();
    if (libraryLoading && !library) return `<article class="surface-card">${loading("正在读取文章")}</article>`;
    const list = records();
    const all = library?.records || [];
    return `
    ${intro("这里是<b>已经保存到你电脑上</b>的文章。点标题即可阅读（不联网）；勾选后可以只导出其中几篇。")}
    <article class="surface-card toolbar-card">
      <div class="segmented">
        ${[["page", "空间文章", "你在百度空间写的文章"], ["article", "外部收藏", "收藏的别处链接，只保存目录"], ["all", "全部", "所有收藏记录"]]
          .map(([t, label, tip]) => `
          <button class="seg ${filters.type === t ? "is-active" : ""}" data-filter="${t}" title="${tip}">
            ${label}<small>${t === "all" ? all.length : all.filter((r) => r.type === t).length} 条</small>
          </button>`).join("")}
      </div>
      <div class="search-row">
        <input type="search" id="searchBox" placeholder="搜索标题或正文摘要" value="${esc(filters.query)}">
        <button class="btn-text" data-act="select-page">全选当前</button>
        <button class="btn-text" data-act="clear-sel">清空</button>
        <span class="sel-count">${selected.size ? `已选 ${selected.size} 篇` : ""}</span>
        ${selected.size ? `<button class="btn-text" data-goto="export">导出所选 →</button>` : ""}
      </div>
    </article>

    <article class="surface-card">
      ${list.length ? list.slice(0, 200).map((r) => `
        <div class="record">
          <label class="check"><input type="checkbox" data-check="${esc(r.key)}" ${selected.has(r.key) ? "checked" : ""} ${r.type !== "page" ? "disabled" : ""}></label>
          <div class="row-main" ${r.type === "page" ? `data-open="${esc(r.key)}"` : ""}>
            <strong>${esc(r.title || "无标题")}</strong>
            <small>${esc(r.date || "无日期")}${r.images ? ` · 图片 ${r.downloaded}/${r.images}` : ""}</small>
            ${r.excerpt ? `<p class="excerpt">${esc(r.excerpt)}</p>` : ""}
          </div>
          ${badge(r.type === "page" ? r.status : "skipped_non_page")}
        </div>`).join("")
      : empty("inbox", library ? "没有匹配的文章" : "还没有保存任何文章",
              library ? "换个关键词或筛选条件试试。" : "先到「下载」页把文章从百度下载下来。")}
      ${list.length > 200 ? `<p class="list-note">只显示前 200 条（共 ${list.length} 条），可以用搜索缩小范围。</p>` : ""}
    </article>`;
  },

  /* ---- 图片 ---- */
  media() {
    if (!images) return `<article class="surface-card">${loading("正在读取图片")}</article>`;
    const deadCount = lostImages().images;
    if (!images.length) return `
      ${repairBanner()}
      <article class="surface-card">${empty("image", "还没有可浏览的图片",
        health?.needs_offline_repair ? "上面的「一键修复」完成后，已下载的图片就会出现在这里。"
                                     : "下载文章时会一并下载图片，完成后会显示在这里。")}</article>`;

    const groups = {};
    for (const img of images) (groups[(img.date || "").slice(0, 4) || "未知年份"] ||= []).push(img);
    return `
    ${intro(`共 <b>${images.length}</b> 张图片已保存在本地${deadCount ? `，另有 ${deadCount} 张已无法找回（见「问题」页）` : ""}。点任意图片可打开所在文章。`)}
    <div class="media-toolbar"><button class="btn-text" data-act="export-images-all">导出全部图片</button></div>
    ${Object.keys(groups).sort().reverse().map((year) => `
      <section class="media-year">
        <h3>${esc(year)} <small>${groups[year].length} 张</small></h3>
        <div class="thumb-grid">
          ${groups[year].map((img) => `
            <button class="thumb" data-open="${esc(img.key)}" title="${esc(img.title)}">
              <img data-thumb="${img.id}" alt="">
            </button>`).join("")}
        </div>
      </section>`).join("")}`;
  },

  /* ---- 下载 ---- */
  tasks() {
    const job = taskJob;
    const hist = library?.history || [];
    const active = job && ["running", "starting", "stopping"].includes(job.status);
    return `
    ${intro("从百度把内容<b>下载到你的电脑</b>。已经下载过的不会重复下载，随时可以停止，停下后再点会从断点继续。")}
    ${active ? `
    <article class="surface-card">
      <div class="section-heading">
        <div><span class="eyebrow">正在进行</span><h3>${esc(taskName(job.kind || job.label))}</h3></div>
        <button class="btn-text danger" data-act="stop">停止</button>
      </div>
      <p class="hint">${esc(job.message || "正在处理…")}</p>
      <div class="progress"><i class="${job.progress ? "" : "indeterminate"}" style="width:${Math.min(100, Math.round((job.progress ?? 0) * 100)) || 35}%"></i></div>
    </article>` : ""}

    <article class="surface-card">
      <div class="section-heading"><div><span class="eyebrow">操作</span><h3>从百度下载</h3></div></div>
      ${[
        ["index", "更新收藏列表", "检查百度上有没有新收藏或删除的条目。约 20 秒，会访问百度。"],
        ["archive", "下载文章", "把还没保存的文章正文下载下来，已保存的会跳过。会访问百度。"],
        ["assets", "下载图片", "补齐文章里还缺的图片。已失效的第三方图片无法找回。"],
        ["verify", "检查完整性", "只核对你电脑上的文件有没有损坏或缺失，不联网。"],
      ].map(([k, label, desc]) => `
        <div class="row">
          <div class="row-main"><strong>${label}</strong><small>${desc}</small></div>
          <button class="btn-task" data-task="${k}" ${active ? "disabled" : ""}>开始</button>
        </div>`).join("")}
    </article>

    <article class="surface-card">
      <div class="section-heading"><div><span class="eyebrow">记录</span><h3>最近运行</h3></div></div>
      ${hist.length ? hist.map((h) => `
        <div class="row">
          <div class="row-main"><strong>${esc(taskName(h.command))}</strong>
            <small>${esc(localTime(h.started_at))}${h.finished_at ? ` · 用时 ${esc(duration(h.started_at, h.finished_at))}` : " · 未正常结束"}</small></div>
          ${outcomeBadge(h.outcome)}
        </div>`).join("")
      : empty("sync", "还没有运行记录", "运行过的操作会按时间列在这里。")}
    </article>`;
  },

  /* ---- 问题 ---- */
  skips() {
    if (!problems) return `<article class="surface-card">${loading("正在检查问题")}</article>`;
    const p = problems;
    const deadImgs = p.dead_images.reduce((n, d) => n + d.images.length, 0);
    if (!problemTotal() && !health?.needs_offline_repair) return `
      <article class="surface-card">${empty("inbox", "没有需要处理的问题", "所有已保存的文章和图片都通过了检查。")}</article>`;
    return `
    ${intro("按<b>原因</b>列出需要留意的条目，每类都附上能解决它的办法。这些问题都不会影响其他文章的保存与导出。")}
    ${repairBanner()}

    ${p.unverified.length ? section("待校验", `${p.unverified.length} 篇文章的旧数据待校验`,
        "文件都在本地，只是缺少校验记录。用上方的「一键修复」即可，不联网。",
        p.unverified, () => "") : ""}

    ${p.auth.length ? section("登录", `${p.auth.length} 篇因登录失效没有下载成功`,
        "百度的登录状态过期了。重新登录后再下载即可。",
        p.auth, () => "", `<button class="btn-text" data-act="login">重新登录</button>`) : ""}

    ${p.not_saved.length ? section("未下载", `${p.not_saved.length} 篇正文没有下载下来`,
        "可能是当时网络或百度临时出错。可以重新下载试试。",
        p.not_saved, (r) => r.error ? `<small class="err">${esc(r.error)}</small>` : "",
        `<button class="btn-text" data-retry-keys="${esc(p.not_saved.map((r) => r.key).join(","))}">全部重新下载</button>`,
        (r) => `<button class="btn-text" data-retry-keys="${esc(r.key)}">重新下载</button>`) : ""}

    ${p.baidu_deleted?.length ? `
    <article class="surface-card">
      <div class="section-heading">
        <div><span class="eyebrow">百度已删除</span><h3>${p.baidu_deleted.length} 篇文章里有 ${p.baidu_deleted.reduce((n, d) => n + d.images.length, 0)} 张图片已被百度删除</h3></div>
      </div>
      <p class="hint">这些图片原本存在<b>百度自己的图床</b>上，百度已经删除了原图——现在去下载，只会得到一张写着「你查看的图片不存在」的占位图。
      重试没有用，所以这里不提供重试。文章正文都已完整保存；<b>原始图片地址和那张占位图都原样保留</b>，作为「这里曾有一张图」的记录。</p>
      ${p.baidu_deleted.map((r) => `
        <div class="record">
          <div class="row-main" data-open="${esc(r.key)}">
            <strong>${esc(r.title)}</strong>
            <small>${esc(r.date)} · ${r.images.length} 张</small>
          </div>
        </div>`).join("")}
    </article>` : ""}

    ${p.dead_images.length ? `
    <article class="surface-card">
      <div class="section-heading">
        <div><span class="eyebrow">无法找回</span><h3>${p.dead_images.length} 篇文章里有 ${deadImgs} 张图片已无法找回</h3></div>
        <button class="btn-text" data-retry-images="${esc(p.dead_images.map((r) => r.key).join(","))}" title="这些网站多半已关闭，重试大概率仍会失败">仍要再试一次</button>
      </div>
      <p class="hint">这些图片当年存在<b>别的网站</b>上（不是百度），那些网站如今已关闭或不再提供原图。
      文章正文都已完整保存，缺的只是这几张图——它们在正文里会显示为占位框，<b>原始图片地址一律保留</b>，作为档案记录。</p>
      ${p.dead_images.map((r) => `
        <div class="record">
          <div class="row-main" data-open="${esc(r.key)}">
            <strong>${esc(r.title)}</strong>
            <small>${esc(r.date)}</small>
            <ul class="dead-list">${r.images.map((img) => `
              <li><span class="dead-domain">${esc(img.domain)}</span>${esc(img.reason)}</li>`).join("")}</ul>
          </div>
        </div>`).join("")}
    </article>` : ""}`;
  },

  /* ---- 导出 ---- */
  export() {
    const n = selected.size;
    return `
    <section class="hero export-hero">
      <div>
        <p class="eyebrow">一键导出全部</p>
        <h2>生成一份可以永久保存的完整备份</h2>
        <p>得到一个文件夹：双击里面的「打开归档.html」就能浏览全部文章，图片都在本地。
        另附 Markdown / TXT 文本版、WordPress 导入包和总目录。不依赖网络，百度关闭后依然能打开。</p>
      </div>
      <button class="btn-light" data-act="export-all" ${exporting ? "disabled" : ""}>
        <svg width="15" height="15"><use href="#i-export"/></svg>${exporting ? "正在导出…" : "选择位置并导出"}
      </button>
    </section>
    ${health?.needs_offline_repair ? `<p class="hint warn-line">提示：还有旧数据未校验，现在导出的话这部分图片不会包含在内。建议先到「问题」页一键修复。</p>` : ""}
    <p class="login-status" id="exportStatus">${esc(exportMessage)}</p>

    <article class="surface-card">
      <div class="section-heading"><div><span class="eyebrow">按需导出</span><h3>只导出一部分</h3></div>
        <span class="sel-count">${n ? `已选 ${n} 篇` : "未选择时导出全部"}</span></div>
      <p class="hint">想只导出几篇？先到「文章」页勾选，再回来点这里。</p>
      <div class="row"><div class="row-main"><strong>文章文本（ZIP）</strong><small>Markdown 与 TXT 两种格式</small></div>
        <button class="btn-text" data-export="selection">选择位置</button></div>
      <div class="row"><div class="row-main"><strong>文章图片（ZIP）</strong><small>只含已保存在本地的图片</small></div>
        <button class="btn-text" data-export="images">选择位置</button></div>
    </article>`;
  },

  /* ---- 设置 ---- */
  settings() {
    const s = library?.settings || {};
    const c = credential;
    const state = c?.logged_in ? "ok" : (c?.present ? "weak" : "none");
    const conn = library?.connection?.state;
    const fastDelay = Number(s.delay ?? 0.8) < 0.5;
    const highConc = Number(s.concurrency ?? 2) > 3;
    const ignored = Object.values(health?.ignored_settings || {});
    return `
    <article class="surface-card">
      <div class="section-heading"><div><span class="eyebrow">账号</span><h3>百度账号</h3></div>
        ${state === "ok" ? `<span class="badge tone-green">已登录</span>`
          : state === "weak" ? `<span class="badge tone-orange">登录信息可疑</span>`
          : `<span class="badge tone-orange">未登录</span>`}</div>

      ${state === "ok" ? `
        <p class="hint">登录信息只保存在你电脑上的 <code>secrets/session.json</code>，其他用户无法读取。${conn === "connected" ? " 连接已测试通过。" : ""}</p>
        <div class="row"><div class="row-main"><strong>重新登录</strong><small>登录过期时使用</small></div>
          <button class="btn-text" data-act="login">打开登录窗口</button></div>
        <div class="row"><div class="row-main"><strong>测试连接</strong><small>实际访问一次百度，确认能正常下载</small></div>
          <button class="btn-text" data-act="check-conn">测试</button></div>
        <div class="row"><div class="row-main"><strong>退出登录</strong><small>删除保存的登录信息，已下载的内容不受影响</small></div>
          <button class="btn-text danger" data-act="logout">退出</button></div>
      ` : `
        <p class="hint">下载的是你本人账号里的内容，需要先登录。点下面的按钮会打开百度官方登录页，
        <strong>扫码或输入账号密码都由你自己完成</strong>——本软件不会代填任何信息，只在你登录成功后保存登录状态。</p>
        <div class="login-actions">
          <button class="btn-primary" data-act="login"><svg width="15" height="15"><use href="#i-user"/></svg> 登录百度账号</button>
          <button class="btn-text" data-act="toggle-curl">改用 cURL 导入</button>
        </div>
        <div class="curl-box" id="curlBox" hidden>
          <p class="hint">浏览器按 F12 → Network → 筛选 Fetch/XHR → 滚动收藏列表 →
          找到 <code>/fav/list</code> 请求 → 右键 Copy as cURL → 粘贴到这里。</p>
          <textarea id="curlInput" placeholder="curl 'https://wenzhang.baidu.com/fav/list?...' -H 'Cookie: ...'"></textarea>
          <button class="btn-primary" data-act="save-curl">导入</button>
        </div>
      `}
      <p class="login-status" id="loginStatus"></p>
    </article>

    <article class="surface-card">
      <div class="section-heading"><div><span class="eyebrow">下载</span><h3>下载速度</h3></div>
        ${fastDelay || highConc ? `<button class="btn-text" data-act="reset-settings">恢复推荐值</button>` : ""}</div>
      <p class="hint">百度空间是个很老的服务，下载太快可能被限制访问，甚至影响账号。慢一点更稳妥，反正已下载的不会重复下载。</p>
      <div class="row"><div class="row-main"><strong>每次请求的间隔</strong>
        <small>${fastDelay ? "当前偏快，建议不低于 0.8 秒" : "推荐 0.8 秒"}</small></div>
        <strong class="${fastDelay ? "value-warn" : ""}">${esc(s.delay ?? "0.8")} 秒</strong></div>
      <div class="row"><div class="row-main"><strong>同时下载数</strong>
        <small>${highConc ? "当前高于推荐值，可能给百度带来压力" : "推荐 2，不超过 3"}</small></div>
        <strong class="${highConc ? "value-warn" : ""}">${esc(s.concurrency ?? "2")}</strong></div>
      ${ignored.map((t) => `<p class="hint warn-line">${esc(t)}</p>`).join("")}
    </article>

    <article class="surface-card">
      <div class="section-heading"><div><span class="eyebrow">存储</span><h3>本地数据</h3></div></div>
      <div class="row"><div class="row-main"><strong>保存位置</strong><small>${esc(stats.root || "archive/")}</small></div>
        <button class="btn-text" data-act="reveal">在访达中显示</button></div>
    </article>`;
  },
};

/* 问题页的一个分组 */
function section(tag, title, desc, items, extra, headAction = "", rowAction = null) {
  return `
  <article class="surface-card">
    <div class="section-heading"><div><span class="eyebrow">${esc(tag)}</span><h3>${esc(title)}</h3></div>${headAction}</div>
    <p class="hint">${esc(desc)}</p>
    ${items.slice(0, 50).map((r) => `
      <div class="record">
        <div class="row-main" data-open="${esc(r.key)}"><strong>${esc(r.title)}</strong>
          <small>${esc(r.date || "")}</small>${extra(r)}</div>
        ${rowAction ? rowAction(r) : ""}
      </div>`).join("")}
    ${items.length > 50 ? `<p class="list-note">还有 ${items.length - 50} 条未列出</p>` : ""}
  </article>`;
}

/* 「这个软件是做什么的」——直接回答「归档到底是什么意思」 */
function howItWorks() {
  return `
  <article class="surface-card how">
    <div class="section-heading"><div><span class="eyebrow">怎么用</span><h3>三步把百度空间留在本地</h3></div></div>
    <div class="steps">
      <div class="step"><span>1</span><div><strong>下载</strong><small>从百度把你收藏的文章和图片下载到这台电脑</small></div></div>
      <div class="step"><span>2</span><div><strong>检查</strong><small>在「文章」「图片」里浏览，「问题」里看有没有缺失</small></div></div>
      <div class="step"><span>3</span><div><strong>导出</strong><small>生成一个文件夹，双击就能看，可以拷到任何地方长期保存</small></div></div>
    </div>
  </article>`;
}

/* ---- 文章阅读视图 ---- */
function READER() {
  if (reading === "loading") return `<article class="surface-card">${loading("正在打开文章", "正在读取本地保存的正文与图片。")}</article>`;
  if (reading?.error) return `<article class="surface-card">${empty("alert", "无法打开这篇文章", reading.error)}
    <div style="text-align:center"><button class="btn-text" data-act="back">返回</button></div></article>`;
  const a = reading;
  return `
  <article class="surface-card reader">
    <button class="btn-text back-btn" data-act="back">← 返回</button>
    <h2>${esc(a.title)}</h2>
    <p class="reader-meta">${[a.date && `收藏于 ${a.date}`, a.source, a.tags?.length && `标签：${[...new Set(a.tags)].join("、")}`,
      a.images_inlined || a.images_missing ? `图片 ${a.images_inlined} 张${a.images_missing ? `，${a.images_missing} 张无法显示` : ""}` : ""]
      .filter(Boolean).map(esc).join(" · ")}</p>
    ${a.note ? `<p class="reader-note">备注：${esc(a.note)}</p>` : ""}
    <div class="reader-body">${a.html}</div>
  </article>`;
}

/* ---------- 渲染 ---------- */
function render() {
  content.innerHTML = `<div class="page">${PAGES[current]()}</div>`;
  pageTitle.textContent = reading && current === "archives" ? "阅读" : TITLES[current];
  if (current === "media") observeThumbs();
}

/* 缩略图按需加载：滚到可见区才向 Python 要，几百张也不卡 */
let thumbObserver = null;
function observeThumbs() {
  thumbObserver?.disconnect();
  thumbObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const img = entry.target;
      thumbObserver.unobserve(img);
      api("get_thumbnail", Number(img.dataset.thumb)).then((src) => {
        if (src) img.src = src; else img.closest(".thumb")?.classList.add("is-broken");
      });
    }
  }, { root: content, rootMargin: "300px" });
  document.querySelectorAll("img[data-thumb]").forEach((img) => thumbObserver.observe(img));
}

function goto(page) {
  if (!PAGES[page]) return;
  current = page; reading = null;
  document.querySelectorAll("[data-page]").forEach((el) =>
    el.classList.toggle("active", el.dataset.page === page));
  render();
  content.scrollTop = 0;
  if (["archives", "tasks", "settings"].includes(page)) loadLibrary();
  if (["skips", "media", "dashboard", "export"].includes(page)) loadProblems();
  if (page === "media" && !images) api("get_images").then((list) => { images = list || []; render(); });
  if (page === "settings") api("credential_state").then((c) => { credential = c; renderAccount(); render(); });
  if (page === "tasks") api("get_task").then((j) => {
    taskJob = j; render();
    if (j && ["running", "starting", "stopping"].includes(j.status)) pollTask();
  });
}

async function openArticle(key) {
  current = "archives"; reading = "loading";
  document.querySelectorAll("[data-page]").forEach((el) =>
    el.classList.toggle("active", el.dataset.page === "archives"));
  render();
  reading = await api("get_article", key);
  render();
  content.scrollTop = 0;
}

async function runTask(kind, keys) {
  const result = await api("start_task", kind, keys || []);
  if (!result) return;
  if (!result.ok) { toast(`无法开始：${result.error || "未知错误"}`, "error", 8000); return; }
  toast(`已开始：${taskName(kind)}`, "info");
  taskJob = result.job || null;
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
      const ok = taskJob?.status === "completed";
      toast(ok ? `完成：${taskName(taskJob?.kind)}` : `已结束：${taskJob?.message || taskName(taskJob?.kind)}`,
            ok ? "success" : "warn", 7000);
      images = null; problems = null;
      await loadLibrary(true); await refresh(); await loadProblems(true);
    }
  }, 1200);
}

async function repair() {
  repairing = true; render();
  toast("开始校验旧数据，全程不联网，大约半分钟…", "info", 6000);
  const r = await api("repair_offline");
  repairing = false;
  if (r?.ok) {
    const a = r.after;
    toast(`修复完成：${a.complete}/${a.page} 篇已完整保存，${a.assets_downloaded} 张图片可用`, "success", 9000);
    if (r.steps?.index && r.steps.index !== "ok") toast(r.steps.index, "warn", 10000);
  } else if (r) {
    toast(`修复失败：${r.error || "未知错误"}`, "error", 9000);
  }
  images = null; problems = null; library = null;
  await refresh(); await loadProblems(true);
  if (current === "media") api("get_images").then((list) => { images = list || []; render(); });
  if (["archives", "tasks", "settings"].includes(current)) loadLibrary(true);
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
  if (task?.dataset.task) {
    const keys = task.dataset.task === "archive" && selected.size ? [...selected] : [];
    return runTask(task.dataset.task, keys);
  }

  const retryKeys = e.target.closest("[data-retry-keys]");
  if (retryKeys) return runTask("archive", retryKeys.dataset.retryKeys.split(",").filter(Boolean));

  const retryImages = e.target.closest("[data-retry-images]");
  if (retryImages) return runTask("assets", retryImages.dataset.retryImages.split(",").filter(Boolean));

  const exp = e.target.closest("[data-export]");
  if (exp) {
    const result = await api("export_to", exp.dataset.export, [...selected]);
    if (result?.ok) toast(`已导出：${result.path}`, "success", 9000);
    else if (result && !result.cancelled) toast(`导出失败：${result.error || "未知错误"}`, "error", 9000);
    return;
  }

  const act = e.target.closest("[data-act]");
  if (!act) return;
  const what = act.dataset.act;
  if (accountMenu?.contains(act)) closeAccountMenu();

  if (what === "back") { reading = null; return render(); }
  if (what === "repair") return repair();
  if (what === "clear-sel") { selected.clear(); return render(); }
  if (what === "select-page") { records().slice(0, 200).filter((r) => r.type === "page").forEach((r) => selected.add(r.key)); return render(); }
  if (what === "stop") { taskJob = await api("stop_task"); toast("正在停止，当前这一条处理完就会停下", "info"); return render(); }
  if (what === "reveal") return api("reveal_archive");
  if (what === "export-images-all") {
    const r = await api("export_to", "images", []);
    if (r?.ok) toast(`已导出：${r.path}`, "success", 9000);
    else if (r && !r.cancelled) toast(`导出失败：${r.error || "未知错误"}`, "error", 9000);
    return;
  }
  if (what === "export-all") {
    exporting = true; exportMessage = "正在生成备份，文章多时需要一会儿…"; render();
    const r = await api("export_all");
    exporting = false;
    if (r?.ok) {
      exportMessage = `已导出 ${r.articles} 篇文章 → ${r.path}`;
      toast(`导出完成：${r.articles} 篇文章，已在访达中打开`, "success", 9000);
      (r.warnings || []).forEach((w) => toast(w, "warn", 9000));
    } else {
      exportMessage = r?.cancelled ? "" : `导出失败：${r?.error || "未知错误"}`;
      if (r && !r.cancelled) toast(exportMessage, "error", 9000);
    }
    return render();
  }
  if (what === "reset-settings") {
    const r = await api("reset_recommended_settings");
    if (r?.ok) { toast("已恢复为推荐的下载速度", "success"); library = null; await loadLibrary(true); health = await api("health"); render(); }
    else if (r) toast(r.error || "修改失败", "error");
    return;
  }
  if (what === "logout") {
    const ok = await api("confirm", "退出百度登录？", "将删除保存的登录信息。已下载的文章、图片都不会受影响，随时可以重新登录。");
    if (!ok) return;
    const r = await api("logout");
    if (!r?.ok) return toast(r?.error || "退出失败", "error");
    await afterLogin();
    toast(r.env_cookie_still_set
      ? "已删除保存的登录信息，但环境变量 BAIDU_ARCHIVE_COOKIE 仍在生效，需要在终端里取消。"
      : "已退出登录", r.env_cookie_still_set ? "warn" : "success", 8000);
    return;
  }
  if (what === "account-settings") return goto("settings");
  if (what === "login") {
    setLoginStatus("已打开百度登录窗口，请在那个窗口里扫码或输入账号密码…");
    toast("已打开百度登录窗口，请在新窗口里完成登录", "info", 6000);
    const r = await api("start_login");
    if (r && !r.ok) { setLoginStatus("打不开登录窗口：" + (r.error || "未知错误"), true); toast("打不开登录窗口", "error"); }
    return;
  }
  if (what === "toggle-curl") {
    const box = document.getElementById("curlBox");
    if (box) box.hidden = !box.hidden;
    return;
  }
  if (what === "save-curl") {
    const text = document.getElementById("curlInput")?.value || "";
    if (!text.trim()) return setLoginStatus("请先粘贴 cURL 内容", true);
    setLoginStatus("正在导入…");
    const r = await api("import_curl_text", text);
    if (r?.ok) { setLoginStatus("导入成功"); toast("登录信息已导入", "success"); await afterLogin(); }
    else setLoginStatus(r?.error || "导入失败", true);
    return;
  }
  if (what === "check-conn") {
    toast("正在测试连接…", "info");
    const r = await api("check_connection");
    if (r && !r.ok) toast(`无法测试：${r.error}`, "error", 8000);
    else { taskJob = r?.job || null; pollTask(); }
    return;
  }
});

document.addEventListener("change", (e) => {
  const box = e.target.closest("[data-check]");
  if (!box) return;
  box.checked ? selected.add(box.dataset.check) : selected.delete(box.dataset.check);
  render();
});

document.addEventListener("input", (e) => {
  if (e.target.id !== "searchBox") return;
  filters.query = e.target.value;
  const pos = e.target.selectionStart;
  render();
  const box = document.getElementById("searchBox");
  if (box) { box.focus(); box.setSelectionRange(pos, pos); }
});

document.getElementById("toggleSidebar").onclick = () => shell.classList.toggle("collapsed");

document.getElementById("themeBtn").onclick = () => {
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

/* ---------- 账号区（右上角） ---------- */
const accountBtn = document.getElementById("accountBtn");
let accountMenu = null;

function renderAccount() {
  const name = document.getElementById("acctName");
  const sub = document.getElementById("acctSub");
  if (!name || !sub) return;
  if (credential?.logged_in) {
    name.textContent = "已登录百度";
    sub.textContent = library?.connection?.state === "connected" ? "连接正常" : "点击管理账号";
    accountBtn.classList.add("is-logged-in");
  } else {
    name.textContent = credential?.present ? "登录信息可疑" : "尚未登录";
    sub.textContent = "点击登录百度账号";
    accountBtn.classList.remove("is-logged-in");
  }
}

function closeAccountMenu() { accountMenu?.remove(); accountMenu = null; }

accountBtn.addEventListener("click", async (e) => {
  e.stopPropagation();
  if (accountMenu) return closeAccountMenu();
  if (!credential) credential = await api("credential_state");
  if (!credential?.logged_in) {
    goto("settings");
    setTimeout(() => document.querySelector('[data-act="login"]')?.click(), 50);
    return;
  }
  accountMenu = document.createElement("div");
  accountMenu.className = "account-menu";
  accountMenu.innerHTML = `
    <div class="account-menu-head"><strong>已登录百度</strong>
      <small>登录信息只保存在这台电脑上</small></div>
    <button data-act="login">重新登录</button>
    <button data-act="check-conn">测试连接</button>
    <button data-act="account-settings">账号设置</button>
    <button data-act="logout" class="danger">退出登录</button>`;
  document.body.appendChild(accountMenu);
  const rect = accountBtn.getBoundingClientRect();
  accountMenu.style.top = `${rect.bottom + 8}px`;
  accountMenu.style.right = `${window.innerWidth - rect.right}px`;
});
document.addEventListener("click", (e) => {
  if (accountMenu && !accountMenu.contains(e.target)) closeAccountMenu();
});

/* ---------- 登录 ---------- */
function setLoginStatus(text, isError = false) {
  const el = document.getElementById("loginStatus");
  if (!el) return;
  el.textContent = text || "";
  el.className = "login-status" + (isError ? " is-error" : "");
}

async function afterLogin() {
  credential = await api("credential_state");
  renderAccount();
  library = null;
  await loadLibrary(true);
  await refresh();
  render();
  renderAccount();
}

/* 由 Python 在登录窗口拿到 Cookie 后调用 */
window.onLoginResult = async (payload) => {
  if (payload?.ok) {
    setLoginStatus("登录成功");
    toast("登录成功，登录信息已保存", "success", 7000);
    await afterLogin();
  } else if (payload?.cancelled) {
    setLoginStatus("已取消登录");
  } else {
    setLoginStatus(payload?.error || "登录未完成", true);
    toast(payload?.error || "登录未完成", "error", 8000);
  }
};

/* 让它不像网页：禁右键菜单、禁拖拽 */
document.addEventListener("contextmenu", (e) => e.preventDefault());
document.addEventListener("dragstart", (e) => e.preventDefault());

render();
window.addEventListener("pywebviewready", async () => {
  credential = await api("credential_state");
  renderAccount();
  await refresh();
  await loadProblems(true);
});
