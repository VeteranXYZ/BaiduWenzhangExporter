# IMPLEMENTATION_PLAN

> 2026-09-15：登录、全量 504 条索引与 497 篇正文归档已跑通。
> 最新实测状态、剩余限制与下一阶段设计见 [TAKEOVER_PROGRESS.md](TAKEOVER_PROGRESS.md)。
> 后续指示：暂停 UI，以指定 QzoneArchive 版本为未来参考；当前核心版本保护方案见 [CORE_DESIGN.md](CORE_DESIGN.md)。

百度空间历史内容抢救 / 归档工具（`baidu_archive`）实施计划。

目标不是"爬虫"，而是一个 **REPRODUCIBLE / RESUMABLE / VERIFIABLE / LOSS-MINIMIZING** 的本地数字档案。

---

## 1. 与既有代码的关系

仓库原有 `BaiduExporter.py`（Selenium + CSV + WXR）**保留不动**，作为历史参考与既有 WordPress 迁移路径。

可复用的既有认知（已吸收进新实现）：

| 既有代码 | 复用方式 |
|---|---|
| `clean_html_content()` 用 `re.compile(r"detailArticleContent_")` 前缀匹配 | 新解析器全面采用 `[id^=...]` 前缀策略，不再硬编码构建哈希 |
| 正文容器 / 标题 / `.time-cang` / `.link-src` 选择器认知 | 直接沿用 |
| WXR 模板 | 移植为 `export.py` 的一种输出格式 |
| 断点续传思路 | 升级为 SQLite 状态机 |

**不复用**：Selenium 逐篇导航（改为 httpx 直连）、CSV 存储（改为 raw 文件 + SQLite）。

---

## 2. 数据流

```
                 ┌──────────────── RAW FIRST ────────────────┐
                 │                                            │
  list API  ──►  raw/lists/list_start####.json               │
     │           (原始 JSON，含 data.html)                     │
     ▼                                                        │
  parse list HTML ──► master index (SQLite + JSON/CSV 导出)   │
     │                                                        │
     │  仅 type=page                                          │
     ▼                                                        │
  /page/view?key=… ──► raw/detail_pages/{key}.html           │
     │                                                        │
     ▼  解析 iframe src（HTML entity 解码）                    │
  wenzhang.bdstatic.com/page/content/?…sign=…                │
     │                                                        │
     └──► raw/content_pages/{key}.html ──────────────────────┘
                 │
                 ▼  parse（纯函数，可离线重跑）
          items/{key}/metadata.json
          items/{key}/content.original.html     ← 永不修改
                 │
                 ▼  assets
          items/{key}/assets/**  + asset_manifest.json
          items/{key}/content.local.html        ← src 重写为本地相对路径
                 │
                 ▼  export
          content.md / content.txt / WXR / site/
                 │
                 ▼  verify
          reports/summary.{json,md} / failed_items.csv / external_resources.csv
```

**FETCH 与 PARSE 完全解耦**：`parse` 阶段只读 `raw/`，不发任何网络请求。解析器改 bug 后可直接 `parse --force` 重建全部派生数据，无需再访问百度。

---

## 3. 模块划分

| 模块 | 职责 |
|---|---|
| `config.py` | 配置加载（env > config.json > 默认值）、端点模板、限速参数 |
| `auth.py` | Cookie 加载（Header 串 / JSON 导出 / Netscape）、cURL 导入、会话自检 |
| `client.py` | httpx Session 封装：限速+jitter、指数退避、429/5xx/403 分级、认证失效检测、raw 落盘 |
| `storage.py` | 原子写、SHA-256、raw 归档布局、item 目录布局 |
| `state.py` | SQLite 状态机（records / items / assets / failures / runs / meta） |
| `parsers.py` | **纯函数**：list HTML / detail HTML / content HTML / 资源 URL 提取 |
| `pipeline.py` | index / fetch / parse 三阶段，抓取与离线解析解耦 |
| `assets.py` | 资源发现、下载、去重、content.local.html 重写 |
| `export.py` | Markdown / TXT / WXR / 静态站点 |
| `selection.py` | 选择文章的离线 ZIP 导出与逐文件校验清单 |
| `tasks.py` / `locking.py` | 有限并发、停止控制、归档目录进程锁 |
| `integrity.py` | 原件、版本引用、派生正文和资源摘要核验 |
| `verify.py` | 完整性校验与报告生成 |
| `utils.py` | `redact_url()` / `sanitize_filename()` / URL 解析 / Content-Type→扩展名 |
| `cli.py` | argparse 子命令 |

---

## 4. 状态模型

每条 index 记录（504 条全部入库）有 `type`：`page` / `article` / `other`。

`page` 类型进入抓取流水线，逐阶段推进：

```
pending
  → detail_fetched     /page/view 原始 HTML 已落盘
  → content_fetched    iframe 正文页原始 HTML 已落盘
  → parsed             metadata + content.original.html 已生成
  → assets_partial     部分资源下载失败（可重试）
  → complete           全部资源就绪
```

旁路状态：

| 状态 | 含义 | 是否可重试 |
|---|---|---|
| `failed` | 该阶段多次重试后失败 | 是（`retry-failed`） |
| `auth_error` | 认证失效导致，**不计入真实失败** | 是（换 Cookie 后） |
| `warning` | 抓到了但内容可疑（正文为空/极短） | 是 |
| `skipped_non_page` | article/other，第一阶段不抓正文 | — |

同一正文版本内状态单调推进；新 raw 版本使派生阶段重新开始，历史文件保留。
已经 `complete` 且原始摘要未变的条目可跳过。

**原子性**：SQLite 本身事务保证；所有文件写入走 `write_atomic()`（临时文件 + `fsync` + `os.replace`）。

> 设计取舍：状态存 SQLite 而非 `crawl_state.json`。理由是并发写入下的原子性与可查询性。`master_index.json` / `master_index.csv` / `crawl_state.json` 仍会作为**派生产物**导出，保证归档可离线阅读、可人工检视。

---

## 5. 目录结构

```
archive/
  state.db                        SQLite 状态（工作态，可从 raw 重建）
  master_index.json               派生：全部 504 条，保留原始顺序
  master_index.csv                派生：同上，便于表格查看
  crawl_state.json                派生：快照
  raw/
    lists/list_start0000.json     原始列表响应 + .meta.json
    detail_pages/{key}.html       原始详情页 + .meta.json
    content_pages/{key}.html      原始 iframe 正文页 + .meta.json
  items/{key}/
    metadata.json
    content.original.html         ← 永不修改
    content.local.html            ← 图片 src 重写为本地
    content.md
    content.txt
    assets/
    asset_manifest.json
  reports/
    summary.json / summary.md
    failed_items.csv
    external_resources.csv
  logs/archive-YYYYMMDD.log
site/                             可选：静态站点产物
```

`key` 作为唯一主键与目录名（已做文件名净化）；可读性由 `master_index.csv` 与站点索引提供。

---

## 6. 认证方案

**不在源码中出现任何真实 Cookie / bdstoken / sign。**

三种加载方式（优先级从高到低）：

1. 环境变量 `BAIDU_ARCHIVE_COOKIE`（完整 Cookie header 串）
2. `secrets/cookies.txt`（Cookie header 串）、`secrets/cookies.json`（浏览器扩展导出的 JSON 数组）、`secrets/cookies.netscape.txt`
3. `secrets/session.json` —— 由 `baidu-archive import-curl` 从 DevTools「Copy as cURL」生成，**同时捕获真实的列表接口 URL 与请求头**

方式 3 是推荐路径：它把"列表端点到底是什么 URL"这个唯一未知量，从猜测变成**用实际请求确定**。

`secrets/` 整目录进 `.gitignore`。

`bdstoken` 从详情页 `__CONF__` 解析后**只存在于内存与本地 state**，不写日志、不进 README、不进 fixture。

---

## 7. 错误恢复与请求策略

| 项 | 默认 |
|---|---|
| 并发 | 2 |
| 请求间隔 | 0.8s + jitter(0~0.6s) |
| 超时 | connect 10s / read 30s |
| 重试 | 4 次，指数退避 1.5s → 3s → 6s → 12s，带 jitter |
| 429 / 5xx | 自动退避重试 |
| 403 | 最多 1 次重试，随后判定为疑似认证问题 |
| 认证失效 | 连续 5 个 item 出现认证特征 → **立即暂停整个任务**并报告 |

认证失效判定不只看 HTTP 200：还检查 JSON `status != 0`、页面 title 含登录特征、期望 DOM 容器缺失。

**签名 URL 过期处理**：正文页 `sign`/`expire` 为临时参数。若 content 请求失败且判定为签名过期，自动**回到 `/page/view` 重新取当前 iframe src** 后再试一次，绝不自行计算 sign。

---

## 8. 安全边界

只发 `GET`。不调用任何 delete / edit / add / ask 接口，不发业务 POST，不枚举 key，不扫描其他用户，不递归抓取外站。外链只登记进 `external_resources.csv`，默认不下载。

---

## 9. CLI

```
baidu-archive import-curl <file>   从 DevTools cURL 导入端点与凭证
baidu-archive doctor               体检：配置/凭证/端点连通性
baidu-archive dry-run              最小验证链路，不全量抓
baidu-archive index                建立 504 条 master index
baidu-archive fetch                抓 detail + content（RAW）
baidu-archive parse                raw → metadata/content（离线可重跑）
baidu-archive assets               下载资源并生成 content.local.html
baidu-archive export               md / txt / wxr / site
baidu-archive verify               完整性校验与报告
baidu-archive retry-failed         只重试失败项
baidu-archive all                  index → fetch → parse → assets → export → verify
baidu-archive status               当前进度速览
```

全局参数：`--dry-run` `--limit N` `--concurrency N` `--delay S` `--force` `--verbose`

---

## 10. 验收标准

| # | 标准 | 对应命令 |
|---|---|---|
| A | 能获得 API `totalCount` | `dry-run` |
| B | 建立全部记录 master index | `index` |
| C | unique 数量与 totalCount 可核对 | `verify` |
| D | 区分 page / article / other | `verify` |
| E | key → detail → iframe → content 全链路 | `dry-run --key <KEY>` |
| F | 保存 raw list/detail/content | `archive/raw/**` |
| G | 解析标题/日期/tags/source/正文 | `parse` |
| H | 正文原始 HTML 不丢失 | `content.original.html` 只写不改 |
| I | 图片本地归档 | `assets` |
| J | 中断后可继续 | 任意阶段重跑 |
| K | 失败可单独 retry | `retry-failed` |
| L | 认证失效不产生几百条假失败 | 连续检测 → 暂停 |
| M | verify 明确报告有无漏数据 | `reports/summary.md` |
| N | 百度关闭后本地仍可阅读 | `content.local.html` + `site/` |

---

## 11. 测试

解析逻辑与网络完全解耦，全部可单测。fixture **均为脱敏构造数据**，不含真实 Cookie / bdstoken / sign / uid。

覆盖：list HTML 解析、iframe src 提取（含 entity 解码）、正文/标题/footer 解析、资源 URL 提取、`redact_url()`、文件名净化、Content-Type→扩展名、状态机推进、原子写。
