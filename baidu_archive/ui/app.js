/* 界面逻辑。数据一律通过 window.pywebview.api 向 Python 要，前端不碰网络、不碰磁盘。
 *
 * 设计原则：用户只走一条路 —— 登录 → 下载全部 → 浏览 → 导出。
 * 首页随状态变化，任何时候只给一个主操作。迁移、校验、索引证明等内部工作
 * 由软件自己完成，界面上不出现「修复」「补证」「解析」这类词。
 */

const shell = document.getElementById("shell");
const content = document.getElementById("content");
const pageTitle = document.getElementById("pageTitle");

const TITLES = {
  dashboard: "首页", archives: "文章", media: "图片",
  export: "导出", settings: "设置", missing: "缺失内容",
};
const ACTIVE = ["running", "starting", "stopping"];

/* ---------- 状态 ---------- */
let stats = { has_db: false };
let library = null;
let problems = null;
let images = null;
let credential = null;
let taskJob = null;
let preparing = false;
let current = "dashboard";

const selected = new Set();
const filters = { query: "", type: "page" };
let reading = null;
let exporting = false;
let exportMessage = "";

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

async function reload() {
  const quick = await api("get_stats");
  if (quick) stats = quick;
  render();
  [library, problems, taskJob] = await Promise.all([api("get_library"), api("get_problems"), api("get_task")]);
  images = null;
  render();
  renderAccount();
  if (taskJob && ACTIVE.includes(taskJob.status)) pollTask();
}

/* ---------- 汇总：首页、导出页共用的一组数字 ---------- */
function summary() {
  const s = library?.summary || {};
  const pages = s.pages ?? stats.page ?? 0;
  const saved = s.ready ?? 0;
  const lost = lostImages();
  return {
    total: library?.records?.length ?? stats.historical_indexed ?? 0,
    pages, saved, notSaved: Math.max(0, pages - saved),
    images: s.downloaded ?? 0, lostImages: lost.images, lostArticles: lost.articles,
    size: stats.db_size || "—",
  };
}

function lostImages() {
  if (!problems) return { articles: 0, images: 0 };
  const groups = [...(problems.baidu_deleted || []), ...(problems.dead_images || [])];
  return { articles: new Set(groups.map((g) => g.key)).size,
           images: groups.reduce((n, g) => n + g.images.length, 0) };
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

const loading = (text, desc = "") =>
  `<div class="empty"><span class="spin"><svg width="23" height="23"><use href="#i-sync"/></svg></span><h4>${esc(text)}</h4>${desc ? `<p>${esc(desc)}</p>` : ""}</div>`;

const intro = (text) => `<p class="page-intro">${text}</p>`;

const STATUS = {
  complete: ["green", "已保存"], parsed: ["green", "已保存"],
  assets_partial: ["orange", "部分图片缺失"], legacy_unverified: ["blue", "整理中"],
  warning: ["orange", "内容可能不完整"], failed: ["danger", "未能下载"],
  auth_error: ["danger", "未能下载"], pending: ["blue", "未下载"], skipped_non_page: ["blue", "外部收藏"],
};
const badge = (status) => {
  const [tone, text] = STATUS[status] || ["blue", status];
  return `<span class="badge tone-${tone}">${esc(text)}</span>`;
};

const TASK_NAME = {
  sync: "下载全部", index: "读取收藏列表", archive: "下载文章", fetch: "下载文章", assets: "下载图片",
  parse: "整理文章", verify: "检查完整性", export: "导出文章", export_images: "导出图片",
  "export-selection": "导出所选文章", connection: "测试连接", "retry-failed": "重试",
  all: "下载全部", repair: "整理旧数据",
};
const OUTCOME = {
  completed: ["green", "完成"], stopped: ["orange", "已停止"], partial: ["green", "完成"],
  failed: ["danger", "未完成"], interrupted: ["orange", "中途中断"],
};
const taskName = (cmd) => TASK_NAME[String(cmd || "").replace(/^ui:/, "")] || "操作";
const outcomeBadge = (o) => { const [t, x] = OUTCOME[o] || ["blue", "—"]; return `<span class="badge tone-${t}">${esc(x)}</span>`; };

function localTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const now = new Date();
  const hm = d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return `今天 ${hm}`;
  return `${d.getFullYear() === now.getFullYear() ? "" : d.getFullYear() + "年"}${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
}
function duration(a, b) {
  const s = Math.round((new Date(b) - new Date(a)) / 1000);
  if (!a || !b || isNaN(s) || s < 0) return "";
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
    return (r.title || "").toLowerCase().includes(q) || (r.excerpt || "").toLowerCase().includes(q);
  });
}

/* ---------- 首页的各个状态 ---------- */
const SYNC_STEPS = ["读取收藏列表", "下载文章", "整理文章", "下载图片"];

function progressCard(job) {
  const step = Math.max(0, SYNC_STEPS.indexOf(job.phase));
  const counted = job.phase === "下载文章" && job.total;
  const pct = counted ? Math.min(100, Math.round((job.done / job.total) * 100)) : null;
  const isSync = job.kind === "sync";
  return `
  <section class="progress-card">
    <div class="section-heading">
      <div><span class="eyebrow">正在进行</span><h3>${esc(isSync ? "正在下载你的百度空间" : taskName(job.kind))}</h3></div>
      ${job.status === "stopping" ? `<span class="badge tone-orange">正在停止…</span>`
        : `<button class="btn-text danger" data-act="stop">停止</button>`}
    </div>
    ${isSync ? `<ol class="sync-steps">${SYNC_STEPS.map((label, i) => `
      <li class="${i < step ? "done" : i === step ? "now" : ""}"><span>${i < step ? "✓" : i + 1}</span>${label}</li>`).join("")}</ol>` : ""}
    <div class="progress"><i class="${pct === null ? "indeterminate" : ""}" style="width:${pct ?? 35}%"></i></div>
    <p class="hint">${counted ? `第 ${job.done} / ${job.total} 篇` : esc(job.phase || "准备中")} ·
      可以随时停止，已下载的内容会保留，下次接着下。</p>
  </section>`;
}

const PAGES = {
  dashboard() {
    if (preparing) return `<article class="surface-card">${loading("正在整理你的归档",
      "检测到用旧版本下载的内容，正在核对本地文件、补齐记录。只需一次，全程不联网，大约半分钟。")}</article>`;

    const job = taskJob;
    const running = job && ACTIVE.includes(job.status);
    const sum = summary();
    const loggedIn = credential?.logged_in;
    const hasData = sum.pages > 0;
    const authFailed = job && job.status === "failed" && library?.connection?.state === "expired";
    const stoppedSync = job && job.kind === "sync" && job.status === "stopped";

    let notice = "";
    if (!running && authFailed) notice = `
      <section class="notice">
        <span class="notice-icon"><svg width="18" height="18"><use href="#i-alert"/></svg></span>
        <div><strong>百度登录已失效，下载中断了</strong>
        <p>已下载的内容都还在。重新登录后点「继续下载」，会从中断的地方接着下。</p></div>
        <button class="btn-text" data-act="login">重新登录</button>
      </section>`;
    else if (!running && stoppedSync && sum.notSaved) notice = `
      <section class="notice">
        <span class="notice-icon"><svg width="18" height="18"><use href="#i-sync"/></svg></span>
        <div><strong>上次下载中途停止了</strong><p>已下载的内容都在。点「继续下载」会接着下，已有的不会重复下载。</p></div>
      </section>`;

    /* 还没有任何数据 */
    if (!hasData && !running) return `
      <section class="hero">
        <div>
          <p class="eyebrow">${loggedIn ? "准备好了" : "开始使用"}</p>
          <h2>${loggedIn ? "开始下载你的百度空间" : "把百度空间里的文章，完整保存到你的电脑上"}</h2>
          <p>${loggedIn
            ? "点右边的按钮，软件会读取你的全部收藏，把文章和图片下载到这台电脑。中途可以随时停止，下次接着下。"
            : "先登录百度账号。登录在百度官方页面完成，本软件不会接触你的密码。"}</p>
        </div>
        <div class="hero-actions">
          ${loggedIn
            ? `<button class="btn-light" data-act="sync"><svg width="15" height="15"><use href="#i-sync"/></svg>开始下载</button>`
            : `<button class="btn-light" data-act="login"><svg width="15" height="15"><use href="#i-user"/></svg>登录百度账号</button>`}
        </div>
      </section>
      ${notice}${howItWorks(loggedIn ? 1 : 0)}`;

    const done = !sum.notSaved;
    return `
    ${running ? progressCard(job) : notice}
    <section class="hero">
      <div>
        <p class="eyebrow">你的百度空间</p>
        <h2>${done ? `${sum.pages} 篇文章已全部保存到这台电脑` : `已保存 ${sum.saved} / ${sum.pages} 篇文章`}</h2>
        <p>${sum.images} 张图片在本地${sum.lostImages ? `，${sum.lostImages} 张图片的原图已不存在` : ""}。
        ${done ? "随时可以浏览，也可以一键导出一份永久备份。" : `还有 ${sum.notSaved} 篇没下载完${running ? "，正在下载。" : "，点「继续下载」接着下。"}`}</p>
      </div>
      <div class="hero-actions">
        ${running ? "" : done
          ? `<button class="btn-ghost" data-act="sync" title="读取百度上的最新收藏，下载新增的文章"><svg width="15" height="15"><use href="#i-sync"/></svg>检查更新</button>
             <button class="btn-light" data-goto="export"><svg width="15" height="15"><use href="#i-export"/></svg>一键导出全部</button>`
          : `${sum.saved ? `<button class="btn-ghost" data-goto="export"><svg width="15" height="15"><use href="#i-export"/></svg>导出已保存的</button>` : ""}
             <button class="btn-light" data-act="sync" ${loggedIn ? "" : "disabled title='请先登录'"}><svg width="15" height="15"><use href="#i-sync"/></svg>继续下载</button>`}
      </div>
    </section>
    ${!loggedIn && !done && !running ? `<p class="hint warn-line">要继续下载，请先在右上角登录百度账号。</p>` : ""}

    <section class="stats">
      ${statCard("green", "inbox", "文章", `${sum.saved} / ${sum.pages}`, "已保存 / 百度空间文章总数", "archives")}
      ${statCard("purple", "image", "图片", sum.images, "已保存在本地", "media")}
      ${statCard(sum.lostImages || sum.notSaved ? "orange" : "green", "alert", "缺失内容",
                 sum.lostImages + (done ? 0 : sum.notSaved),
                 sum.lostImages || sum.notSaved ? "查看哪些内容没能保存、为什么" : "没有缺失", "missing")}
      ${statCard("blue", "db", "占用空间", sum.size, `共 ${sum.total} 条收藏`)}
    </section>`;
  },

  /* ---- 文章 ---- */
  archives() {
    if (reading) return READER();
    if (!library) return `<article class="surface-card">${loading("正在读取文章")}</article>`;
    const list = records();
    const all = library.records || [];
    if (!all.length) return `<article class="surface-card">${empty("inbox", "还没有文章", "回到首页点「开始下载」。")}</article>`;
    return `
    ${intro("已经保存到这台电脑上的文章，点标题即可阅读，不需要联网。勾选后可以只导出其中几篇。")}
    <article class="surface-card toolbar-card">
      <div class="segmented">
        ${[["page", "空间文章"], ["article", "外部收藏"], ["all", "全部"]].map(([t, label]) => `
          <button class="seg ${filters.type === t ? "is-active" : ""}" data-filter="${t}">
            ${label}<small>${t === "all" ? all.length : all.filter((r) => r.type === t).length} 条</small>
          </button>`).join("")}
      </div>
      ${filters.type === "article" ? `<p class="hint">外部收藏是你在百度收藏的别处链接（如百度知道），只保存标题和链接，没有正文。</p>` : ""}
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
          <div class="row-main" ${r.type === "page" && r.available ? `data-open="${esc(r.key)}"` : ""}>
            <strong>${esc(r.title || "无标题")}</strong>
            <small>${esc(r.date || "无日期")}${r.images ? ` · 图片 ${r.downloaded}/${r.images}` : ""}</small>
            ${r.excerpt ? `<p class="excerpt">${esc(r.excerpt)}</p>` : ""}
          </div>
          ${badge(r.type === "page" ? (r.available ? r.status : "pending") : "skipped_non_page")}
        </div>`).join("")
      : empty("inbox", "没有匹配的文章", "换个关键词或筛选条件试试。")}
      ${list.length > 200 ? `<p class="list-note">只显示前 200 条（共 ${list.length} 条），可以用搜索缩小范围。</p>` : ""}
    </article>`;
  },

  /* ---- 图片 ---- */
  media() {
    if (!images) return `<article class="surface-card">${loading("正在读取图片")}</article>`;
    if (!images.length) return `<article class="surface-card">${empty("image", "还没有图片", "下载文章时会一并下载其中的图片。")}</article>`;
    const lost = lostImages().images;
    const groups = {};
    for (const img of images) (groups[(img.date || "").slice(0, 4) || "未知年份"] ||= []).push(img);
    return `
    ${intro(`共 <b>${images.length}</b> 张图片保存在本地${lost ? `；另有 ${lost} 张原图已不存在，<a href="#" data-goto="missing">看看是哪些</a>` : ""}。点任意图片打开所在文章。`)}
    <div class="media-toolbar"><button class="btn-text" data-act="export-images-all">导出全部图片</button></div>
    ${Object.keys(groups).sort().reverse().map((year) => `
      <section class="media-year">
        <h3>${esc(year)} <small>${groups[year].length} 张</small></h3>
        <div class="thumb-grid">${groups[year].map((img) => `
          <button class="thumb" data-open="${esc(img.key)}" title="${esc(img.title)}"><img data-thumb="${img.id}" alt=""></button>`).join("")}
        </div>
      </section>`).join("")}`;
  },

  /* ---- 缺失内容 ---- */
  missing() {
    if (!problems) return `<article class="surface-card">${loading("正在检查")}</article>`;
    const p = problems;
    const sum = summary();
    const deleted = p.baidu_deleted || [];
    const dead = p.dead_images || [];
    const back = `<button class="btn-text back-btn" data-goto="dashboard">← 返回首页</button>`;
    if (!deleted.length && !dead.length && !p.not_saved.length && !p.auth.length) return `
      ${back}<article class="surface-card">${empty("inbox", "没有缺失内容", "所有已下载的文章和图片都完整保存了。")}</article>`;
    const imageList = (r) => `<ul class="dead-list">${r.images.map((img) => `
      <li><span class="dead-domain">${esc(img.domain)}</span>${esc(img.reason)}</li>`).join("")}</ul>`;
    return `
    ${back}
    ${intro("这里列出没能保存下来的内容和原因。<b>这些都不影响其他文章的浏览和导出。</b>")}

    ${p.not_saved.length || p.auth.length ? `
    <article class="surface-card">
      <div class="section-heading"><div><span class="eyebrow">可以补救</span><h3>${p.not_saved.length + p.auth.length} 篇文章还没下载下来</h3></div>
        <button class="btn-text" data-act="sync" ${credential?.logged_in ? "" : "disabled"}>继续下载</button></div>
      <p class="hint">${p.auth.length ? "其中有些是因为百度登录过期。" : ""}可能是当时网络或百度临时出错，点「继续下载」再试一次${credential?.logged_in ? "" : "（需要先登录）"}。</p>
      ${[...p.auth, ...p.not_saved].slice(0, 50).map((r) => `
        <div class="record"><div class="row-main"><strong>${esc(r.title)}</strong><small>${esc(r.date || "")}</small></div></div>`).join("")}
    </article>` : ""}

    ${deleted.length ? `
    <article class="surface-card">
      <div class="section-heading"><div><span class="eyebrow">无法找回</span>
        <h3>${deleted.reduce((n, d) => n + d.images.length, 0)} 张图片已被百度删除</h3></div></div>
      <p class="hint">这些图片原本存在百度自己的图床上，百度已经删除了原图，现在只能拿到一张「你查看的图片不存在」的占位图，所以无法再下载。
      文章正文都已完整保存；原始图片地址作为记录保留在正文里。</p>
      ${deleted.map((r) => `
        <div class="record"><div class="row-main" data-open="${esc(r.key)}"><strong>${esc(r.title)}</strong>
          <small>${esc(r.date)} · ${r.images.length} 张</small></div></div>`).join("")}
    </article>` : ""}

    ${dead.length ? `
    <article class="surface-card">
      <div class="section-heading"><div><span class="eyebrow">无法找回</span>
        <h3>${dead.reduce((n, d) => n + d.images.length, 0)} 张图片所在的网站已失效</h3></div></div>
      <p class="hint">这些图片当年存在别的网站上（不是百度），那些网站如今已关闭或不再提供原图。原始图片地址作为记录保留在正文里。</p>
      ${dead.map((r) => `
        <div class="record"><div class="row-main" data-open="${esc(r.key)}"><strong>${esc(r.title)}</strong>
          <small>${esc(r.date)}</small>${imageList(r)}</div></div>`).join("")}
    </article>` : ""}`;
  },

  /* ---- 导出 ---- */
  export() {
    const n = selected.size;
    const sum = summary();
    return `
    <section class="hero export-hero">
      <div>
        <p class="eyebrow">一键导出全部</p>
        <h2>生成一份可以永久保存的完整备份</h2>
        <p>得到一个文件夹：双击里面的「打开归档.html」就能浏览全部文章，图片都在本地。
        另附 Markdown / TXT 文本版、WordPress 导入包和总目录。不依赖网络，百度关闭后依然能打开。</p>
      </div>
      <button class="btn-light" data-act="export-all" ${exporting || !sum.saved ? "disabled" : ""}>
        <svg width="15" height="15"><use href="#i-export"/></svg>${exporting ? "正在导出…" : "选择位置并导出"}
      </button>
    </section>
    ${sum.notSaved && sum.saved ? `<p class="hint warn-line">还有 ${sum.notSaved} 篇没下载完，现在导出只包含已保存的 ${sum.saved} 篇。</p>` : ""}
    ${!sum.saved ? `<p class="hint warn-line">还没有可导出的文章，先回首页下载。</p>` : ""}
    <p class="login-status" id="exportStatus">${esc(exportMessage)}</p>

    <article class="surface-card">
      <div class="section-heading"><div><span class="eyebrow">按需导出</span><h3>只导出一部分</h3></div>
        <span class="sel-count">${n ? `已选 ${n} 篇` : "未选择时导出全部"}</span></div>
      <p class="hint">想只导出几篇？先到「文章」页勾选，再回来点这里。</p>
      <div class="row"><div class="row-main"><strong>文章文本（ZIP）</strong><small>Markdown 与 TXT 两种格式</small></div>
        <button class="btn-text" data-export="selection" ${sum.saved ? "" : "disabled"}>选择位置</button></div>
      <div class="row"><div class="row-main"><strong>文章图片（ZIP）</strong><small>只含已保存在本地的图片</small></div>
        <button class="btn-text" data-export="images" ${sum.images ? "" : "disabled"}>选择位置</button></div>
    </article>`;
  },

  /* ---- 设置 ---- */
  settings() {
    const c = credential;
    const state = c?.logged_in ? "ok" : (c?.present ? "weak" : "none");
    const s = library?.settings || {};
    const hist = library?.history || [];
    const ignored = Object.values(stats.ignored_settings || {});
    return `
    <article class="surface-card">
      <div class="section-heading"><div><span class="eyebrow">账号</span><h3>百度账号</h3></div>
        ${state === "ok" ? `<span class="badge tone-green">已登录</span>` : `<span class="badge tone-orange">未登录</span>`}</div>
      ${state === "ok" ? `
        <p class="hint">登录状态只保存在这台电脑上（<code>secrets/session.json</code>），其他用户无法读取。</p>
        <div class="row"><div class="row-main"><strong>重新登录</strong><small>登录过期时使用</small></div>
          <button class="btn-text" data-act="login">打开登录窗口</button></div>
        <div class="row"><div class="row-main"><strong>退出登录</strong><small>删除保存的登录状态，已下载的内容不受影响</small></div>
          <button class="btn-text danger" data-act="logout">退出</button></div>
      ` : `
        <p class="hint">点下面的按钮会打开百度官方登录页，<strong>扫码或输入账号密码都在百度页面完成</strong>，本软件不会接触你的密码，只在登录成功后保存登录状态。</p>
        <div class="login-actions">
          <button class="btn-primary" data-act="login"><svg width="15" height="15"><use href="#i-user"/></svg> 登录百度账号</button>
          <button class="btn-text" data-act="toggle-curl">登录窗口打不开？</button>
        </div>
        <div class="curl-box" id="curlBox" hidden>
          <p class="hint">备用方式：在浏览器登录百度后按 F12 → Network → 打开收藏列表 →
          找到 <code>/fav/list</code> 请求 → 右键 Copy as cURL → 粘贴到这里。</p>
          <textarea id="curlInput" placeholder="curl 'https://wenzhang.baidu.com/fav/list?...' -H 'Cookie: ...'"></textarea>
          <button class="btn-primary" data-act="save-curl">导入</button>
        </div>
      `}
      <p class="login-status" id="loginStatus"></p>
    </article>

    <article class="surface-card">
      <div class="section-heading"><div><span class="eyebrow">下载</span><h3>下载速度</h3></div>
        <span class="badge tone-green">稳妥</span></div>
      <p class="hint">每次请求间隔 ${esc(s.delay ?? 0.8)} 秒，同时下载 ${esc(s.concurrency ?? 2)} 篇。
      百度空间是很老的服务，下载太快可能被限制访问；这个速度下 500 篇大约需要 15–20 分钟。已下载的不会重复下载。</p>
      ${ignored.map((t) => `<p class="hint warn-line">${esc(t)}</p>`).join("")}
    </article>

    <article class="surface-card">
      <div class="section-heading"><div><span class="eyebrow">存储</span><h3>本地数据</h3></div></div>
      <div class="row"><div class="row-main"><strong>保存位置</strong><small>${esc(stats.root || "archive/")}</small></div>
        <button class="btn-text" data-act="reveal">在访达中显示</button></div>
    </article>

    <article class="surface-card">
      <div class="section-heading"><div><span class="eyebrow">记录</span><h3>最近的操作</h3></div></div>
      ${hist.length ? hist.map((h) => `
        <div class="row"><div class="row-main"><strong>${esc(taskName(h.command))}</strong>
          <small>${esc(localTime(h.started_at))}${h.finished_at ? ` · 用时 ${esc(duration(h.started_at, h.finished_at))}` : " · 未正常结束"}</small></div>
          ${outcomeBadge(h.outcome)}</div>`).join("")
      : `<p class="hint">还没有记录。</p>`}
    </article>`;
  },
};

/* 首页上的使用说明，标出用户当前在第几步 */
function howItWorks(at = 0) {
  const steps = [["登录", "用百度官方页面登录你的账号"], ["下载", "一个按钮，把文章和图片下载到这台电脑"],
                 ["浏览", "在「文章」「图片」里随时查看，不需要联网"], ["导出", "生成一个文件夹，双击就能看，可长期保存"]];
  return `
  <article class="surface-card how">
    <div class="section-heading"><div><span class="eyebrow">怎么用</span><h3>四步把百度空间留在本地</h3></div></div>
    <div class="steps">${steps.map(([t, d], i) => `
      <div class="step ${i < at ? "is-done" : i === at ? "is-now" : ""}"><span>${i < at ? "✓" : i + 1}</span>
        <div><strong>${t}</strong><small>${d}</small></div></div>`).join("")}</div>
  </article>`;
}

function READER() {
  if (reading === "loading") return `<article class="surface-card">${loading("正在打开文章")}</article>`;
  if (reading?.error) return `<article class="surface-card">${empty("alert", "无法打开这篇文章", reading.error)}
    <div style="text-align:center"><button class="btn-text" data-act="back">返回</button></div></article>`;
  const a = reading;
  return `
  <article class="surface-card reader">
    <button class="btn-text back-btn" data-act="back">← 返回</button>
    <h2>${esc(a.title)}</h2>
    <p class="reader-meta">${[a.date && `收藏于 ${a.date}`, a.source, a.tags?.length && `标签：${[...new Set(a.tags)].join("、")}`,
      a.images_missing ? `${a.images_missing} 张图片原图已不存在` : ""].filter(Boolean).map(esc).join(" · ")}</p>
    ${a.note ? `<p class="reader-note">备注：${esc(a.note)}</p>` : ""}
    <div class="reader-body">${a.html}</div>
  </article>`;
}

/* ---------- 渲染与导航 ---------- */
function render() {
  content.innerHTML = `<div class="page">${PAGES[current]()}</div>`;
  pageTitle.textContent = reading && current === "archives" ? "阅读" : TITLES[current];
  if (current === "media") observeThumbs();
}

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
  const navPage = page === "missing" ? "dashboard" : page;
  document.querySelectorAll("[data-page]").forEach((el) => el.classList.toggle("active", el.dataset.page === navPage));
  render();
  content.scrollTop = 0;
  if (page === "media" && !images) api("get_images").then((list) => { images = list || []; render(); });
  if (page === "settings") api("credential_state").then((c) => { credential = c; renderAccount(); render(); });
}

async function openArticle(key) {
  current = "archives"; reading = "loading";
  document.querySelectorAll("[data-page]").forEach((el) => el.classList.toggle("active", el.dataset.page === "archives"));
  render();
  reading = await api("get_article", key);
  render();
  content.scrollTop = 0;
}

/* ---------- 下载 ---------- */
async function startSync() {
  if (!credential?.logged_in) { toast("请先登录百度账号", "warn"); return; }
  const result = await api("start_task", "sync", []);
  if (!result) return;
  if (!result.ok) { toast(`无法开始下载：${result.error || "未知错误"}`, "error", 8000); return; }
  taskJob = result.job;
  goto("dashboard");
  pollTask();
}

let polling = null;
function pollTask() {
  clearInterval(polling);
  polling = setInterval(async () => {
    const before = taskJob?.phase;
    taskJob = await api("get_task");
    if (current === "dashboard") render();
    if (taskJob?.phase !== before && taskJob?.phase === "下载图片") library = await api("get_library");
    if (!taskJob || !ACTIVE.includes(taskJob.status)) {
      clearInterval(polling); polling = null;
      const status = taskJob?.status;
      if (status === "completed" || status === "partial") toast("下载完成", "success", 7000);
      else if (status === "stopped") toast("已停止。已下载的内容都保留了，可以随时继续。", "info", 7000);
      else if (status === "failed") toast(taskJob?.message || "下载没有完成", "error", 9000);
      await reload();
    }
  }, 1200);
}

/* ---------- 事件 ---------- */
document.addEventListener("click", async (e) => {
  const nav = e.target.closest("[data-page]");
  if (nav) return goto(nav.dataset.page);
  const jump = e.target.closest("[data-goto]");
  if (jump) { e.preventDefault(); return goto(jump.dataset.goto); }
  const open = e.target.closest("[data-open]");
  if (open) return openArticle(open.dataset.open);
  const filter = e.target.closest("[data-filter]");
  if (filter) { filters.type = filter.dataset.filter; return render(); }

  const exp = e.target.closest("[data-export]");
  if (exp && !exp.disabled) {
    const result = await api("export_to", exp.dataset.export, [...selected]);
    if (result?.ok) toast(`已导出：${result.path}`, "success", 9000);
    else if (result && !result.cancelled) toast(`导出失败：${result.error || "未知错误"}`, "error", 9000);
    return;
  }

  const act = e.target.closest("[data-act]");
  if (!act || act.disabled) return;
  const what = act.dataset.act;
  if (accountMenu?.contains(act)) closeAccountMenu();

  if (what === "sync") return startSync();
  if (what === "stop") { taskJob = await api("stop_task"); return render(); }
  if (what === "back") { reading = null; return render(); }
  if (what === "clear-sel") { selected.clear(); return render(); }
  if (what === "select-page") { records().slice(0, 200).filter((r) => r.type === "page" && r.available).forEach((r) => selected.add(r.key)); return render(); }
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
  if (what === "logout") {
    const ok = await api("confirm", "退出百度登录？", "将删除保存的登录状态。已下载的文章、图片都不会受影响，随时可以重新登录。");
    if (!ok) return;
    const r = await api("logout");
    if (!r?.ok) return toast(r?.error || "退出失败", "error");
    await afterLogin();
    toast(r.env_cookie_still_set ? "已删除保存的登录状态，但环境变量 BAIDU_ARCHIVE_COOKIE 仍在生效，需要在终端里取消。"
                                 : "已退出登录", r.env_cookie_still_set ? "warn" : "success", 8000);
    return;
  }
  if (what === "account-settings") return goto("settings");
  if (what === "login") {
    setLoginStatus("已打开百度登录窗口，请在那个窗口里登录…");
    toast("已打开百度登录窗口，请在新窗口里完成登录", "info", 6000);
    const r = await api("start_login");
    if (r && !r.ok) { setLoginStatus("打不开登录窗口：" + (r.error || "未知错误"), true); toast("打不开登录窗口", "error"); }
    return;
  }
  if (what === "toggle-curl") { const box = document.getElementById("curlBox"); if (box) box.hidden = !box.hidden; return; }
  if (what === "save-curl") {
    const text = document.getElementById("curlInput")?.value || "";
    if (!text.trim()) return setLoginStatus("请先粘贴 cURL 内容", true);
    const r = await api("import_curl_text", text);
    if (r?.ok) { setLoginStatus("导入成功"); toast("登录状态已导入", "success"); await afterLogin(); }
    else setLoginStatus(r?.error || "导入失败", true);
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
    name.textContent = "已登录百度"; sub.textContent = "点击管理账号";
    accountBtn.classList.add("is-logged-in");
  } else {
    name.textContent = "尚未登录"; sub.textContent = "点击登录百度账号";
    accountBtn.classList.remove("is-logged-in");
  }
}
function closeAccountMenu() { accountMenu?.remove(); accountMenu = null; }

accountBtn.addEventListener("click", async (e) => {
  e.stopPropagation();
  if (accountMenu) return closeAccountMenu();
  if (!credential?.logged_in) {
    setLoginStatus("");
    toast("已打开百度登录窗口，请在新窗口里完成登录", "info", 6000);
    return api("start_login");
  }
  accountMenu = document.createElement("div");
  accountMenu.className = "account-menu";
  accountMenu.innerHTML = `
    <div class="account-menu-head"><strong>已登录百度</strong><small>登录状态只保存在这台电脑上</small></div>
    <button data-act="login">重新登录</button>
    <button data-act="account-settings">账号设置</button>
    <button data-act="logout" class="danger">退出登录</button>`;
  document.body.appendChild(accountMenu);
  const rect = accountBtn.getBoundingClientRect();
  accountMenu.style.top = `${rect.bottom + 8}px`;
  accountMenu.style.right = `${window.innerWidth - rect.right}px`;
});
document.addEventListener("click", (e) => { if (accountMenu && !accountMenu.contains(e.target)) closeAccountMenu(); });

function setLoginStatus(text, isError = false) {
  const el = document.getElementById("loginStatus");
  if (!el) return;
  el.textContent = text || "";
  el.className = "login-status" + (isError ? " is-error" : "");
}

async function afterLogin() {
  credential = await api("credential_state");
  renderAccount();
  await reload();
}

/* 由 Python 在登录窗口拿到登录状态后调用 */
window.onLoginResult = async (payload) => {
  if (payload?.ok) {
    setLoginStatus("登录成功");
    toast("登录成功", "success", 6000);
    await afterLogin();
  } else if (payload?.cancelled) {
    setLoginStatus("已取消登录");
  } else {
    setLoginStatus(payload?.error || "登录未完成", true);
    toast(payload?.error || "登录未完成", "error", 8000);
  }
};

document.addEventListener("contextmenu", (e) => e.preventDefault());
document.addEventListener("dragstart", (e) => e.preventDefault());

/* ---------- 启动 ---------- */
render();
window.addEventListener("pywebviewready", async () => {
  credential = await api("credential_state");
  renderAccount();
  const quick = await api("get_stats");
  if (quick) stats = quick;
  // 旧版本留下的归档在这里自动整理，用户不需要做任何事；从零开始下载的归档不会进这个分支
  const check = await api("health");
  if (check?.needs_offline_repair) {
    preparing = true; render();
    const prep = await api("prepare_archive");
    preparing = false;
    if (prep?.state === "error") toast(`整理旧数据时出错：${prep.error || "未知错误"}。已下载的内容没有受影响。`, "error", 10000);
  }
  await reload();
});
