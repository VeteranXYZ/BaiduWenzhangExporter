/* 界面逻辑。数据一律通过 window.pywebview.api 向 Python 要，前端不碰网络。 */

const shell = document.getElementById("shell");
const content = document.getElementById("content");
const pageTitle = document.getElementById("pageTitle");

const TITLES = {
  dashboard: "概览", archives: "归档内容", media: "媒体",
  tasks: "归档任务", skips: "异常跳过", export: "导出", settings: "设置",
};

let stats = {
  total_api: 0, indexed: 0, page: 0, article: 0, other: 0,
  complete: 0, warning: 0, failed: 0, pending: 0,
  assets_downloaded: 0, assets_failed: 0, db_size: "—", has_db: false,
};

/* ---------- 与 Python 通信 ---------- */
async function api(method, ...args) {
  if (!window.pywebview?.api?.[method]) return null;
  try { return await window.pywebview.api[method](...args); }
  catch (err) { console.error(method, err); return null; }
}

/* 两级加载：先用数据库记账瞬时出数（约 1ms），再在后台逐字节校验（约 1s / 300 篇）。
   校验期间界面照常可用 —— pywebview 的 js_api 跑在独立线程上，不会冻结窗口。 */
let verifying = false;

async function refresh() {
  const quick = await api("get_stats");
  if (quick) { stats = quick; render(); }
  if (!quick?.has_db) return;

  verifying = true;
  render();
  const verified = await api("get_verified_stats");
  verifying = false;
  if (verified) stats = verified;
  render();
}

/* ---------- 渲染 ---------- */
const statCard = (tone, icon, label, value, hint) => `
  <article class="stat-card">
    <span class="stat-icon tone-${tone}"><svg width="18" height="18"><use href="#i-${icon}"/></svg></span>
    <div><p>${label}</p><strong>${value}</strong><small>${hint}</small></div>
  </article>`;

const empty = (icon, title, desc) => `
  <div class="empty">
    <span><svg width="23" height="23"><use href="#i-${icon}"/></svg></span>
    <h4>${title}</h4><p>${desc}</p>
  </div>`;

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
      <button class="btn-light" id="primaryAction">
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
      <button class="btn-text" data-goto="tasks">去补齐</button>
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
        <div class="row"><div class="row-main"><strong>${stats.assets_downloaded}</strong><small>资源已下载并验证</small></div>
          <span class="badge tone-green">正常</span></div>
        ${stats.assets_legacy_unverified?`<div class="row"><div class="row-main"><strong>${stats.assets_legacy_unverified}</strong><small>旧版本资源待补证；文件可能仍存在。使用操作台的「离线验证旧归档」或 CLI verify --rebuild-legacy-evidence。</small></div><span class="badge tone-orange">待补证</span></div>`:""}
        <div class="row"><div class="row-main"><strong>${stats.assets_failed}</strong><small>资源下载失败（原始 URL 已保留）</small></div>
          <span class="badge ${stats.assets_failed ? "tone-orange" : "tone-green"}">${stats.assets_failed ? "部分失败" : "正常"}</span></div>
      </article>

      <article class="surface-card">
        <div class="section-heading"><div><span class="eyebrow">TYPES</span><h3>类型分布</h3></div></div>
        ${stats.indexed ? `
          <div class="row"><div class="row-main"><strong>百度空间文章</strong><small>page</small></div><strong>${stats.page}</strong></div>
          <div class="row"><div class="row-main"><strong>外部收藏</strong><small>article</small></div><strong>${stats.article}</strong></div>
          <div class="row"><div class="row-main"><strong>其他</strong><small>other</small></div><strong>${stats.other}</strong></div>
        ` : empty("doc", "尚未索引", "运行索引后，504 条收藏会全部登记在这里。")}
      </article>
    </section>`;
  },

  archives: () => `<article class="surface-card">${empty("inbox", "归档内容", "抓取完成后，文章会在这里按时间列出，可搜索、勾选、批量导出。")}</article>`,
  media:    () => `<article class="surface-card">${empty("image", "媒体时光轴", "本地化后的图片会按年份在这里展示。")}</article>`,
  tasks:    () => `<article class="surface-card">${empty("sync", "归档任务", "index / fetch / parse / assets 各阶段的实时进度。")}</article>`,
  skips:    () => `<article class="surface-card">${empty("alert", "异常跳过列表", "失败的条目会列在这里，可逐条或批量重试。异常位置不会阻塞后续归档。")}</article>`,
  export:   () => `
    <article class="surface-card">
      <div class="section-heading"><div><span class="eyebrow">EXPORT</span><h3>导出</h3></div></div>
      <div class="row"><div class="row-main"><strong>整库 SQLite</strong><small>完整备份，可再加工</small></div><button class="btn-text" data-export="db">选择目录</button></div>
      <div class="row"><div class="row-main"><strong>单篇 HTML</strong><small>自包含，图片已本地化</small></div><button class="btn-text" data-export="html">选择目录</button></div>
      <div class="row"><div class="row-main"><strong>Markdown / TXT</strong><small>迁移到 Hugo、Obsidian</small></div><button class="btn-text" data-export="md">选择目录</button></div>
      <div class="row"><div class="row-main"><strong>WordPress WXR</strong><small>站点迁移导入包</small></div><button class="btn-text" data-export="wxr">选择目录</button></div>
      <div class="row"><div class="row-main"><strong>静态站点</strong><small>百度关闭后仍可离线浏览</small></div><button class="btn-text" data-export="site">选择目录</button></div>
    </article>`,
  settings: () => `
    <article class="surface-card">
      <div class="section-heading"><div><span class="eyebrow">SETTINGS</span><h3>设置</h3></div></div>
      <div class="row"><div class="row-main"><strong>凭证</strong><small>从 DevTools cURL 导入，存于 secrets/（0600）</small></div><button class="btn-text" data-act="import">导入 cURL</button></div>
      <div class="row"><div class="row-main"><strong>请求间隔</strong><small>面对遗留系统，可靠性优先于速度</small></div><strong>0.8s</strong></div>
      <div class="row"><div class="row-main"><strong>并发数</strong><small>建议不超过 3</small></div><strong>2</strong></div>
      <div class="row"><div class="row-main"><strong>归档目录</strong><small id="rootPath">archive/</small></div><button class="btn-text" data-act="reveal">在访达中显示</button></div>
      <div class="row"><div class="row-main"><strong>删除全部本地数据</strong><small>不可撤销，请先备份</small></div><button class="btn-text danger" data-act="wipe">删除</button></div>
    </article>`,
};

let current = "dashboard";
function render() {
  content.innerHTML = `<div class="page">${PAGES[current]()}</div>`;
  pageTitle.textContent = TITLES[current];
}

function goto(page) {
  if (!PAGES[page]) return;
  current = page;
  document.querySelectorAll("[data-page]").forEach((el) =>
    el.classList.toggle("active", el.dataset.page === page));
  render();
}

/* ---------- 事件 ---------- */
document.addEventListener("click", async (e) => {
  const nav = e.target.closest("[data-page]");
  if (nav) return goto(nav.dataset.page);

  const jump = e.target.closest("[data-goto]");
  if (jump) return goto(jump.dataset.goto);

  const exp = e.target.closest("[data-export]");
  if (exp) {
    const dir = await api("choose_directory");       // 原生文件选择器
    if (dir) await api("notify", "已选择导出目录", dir);
    return;
  }

  const act = e.target.closest("[data-act]");
  if (act) {
    if (act.dataset.act === "reveal") await api("reveal_archive");
    if (act.dataset.act === "wipe") {
      const ok = await api("confirm", "删除全部本地数据？", "此操作不可撤销。已归档的原始响应、正文与资源都会被删除。");
      if (ok) await api("notify", "已取消", "演示版未执行删除");
    }
    if (act.dataset.act === "import") await api("choose_file");
    return;
  }

  if (e.target.closest("#primaryAction")) {
    await api("notify", "演示版", "此处将触发 index → fetch → parse 流水线");
  }
});

document.getElementById("toggleSidebar").onclick = () => shell.classList.toggle("collapsed");

const themeBtn = document.getElementById("themeBtn");
themeBtn.onclick = () => {
  const dark = document.documentElement.classList.toggle("app-dark");
  document.getElementById("themeIcon").setAttribute("href", dark ? "#i-sun" : "#i-moon");
  api("set_theme", dark ? "dark" : "light");
};

/* 跟随系统深浅色 */
const mq = window.matchMedia("(prefers-color-scheme: dark)");
function applySystemTheme(isDark) {
  document.documentElement.classList.toggle("app-dark", isDark);
  document.getElementById("themeIcon").setAttribute("href", isDark ? "#i-sun" : "#i-moon");
}
applySystemTheme(mq.matches);
mq.addEventListener("change", (ev) => applySystemTheme(ev.matches));

/* 让它不像网页：禁右键菜单、禁拖拽、禁文本框外的选中 */
document.addEventListener("contextmenu", (e) => e.preventDefault());
document.addEventListener("dragstart", (e) => e.preventDefault());

render();
window.addEventListener("pywebviewready", refresh);
