# BaiduWenzhangExporter

百度空间 / 百度文章历史内容的**本地数字档案工具**。

目标不是"做一个爬虫"，而是在旧服务彻底关闭之前，把仍然能访问的个人历史内容
**尽快、完整、可验证、尽可能原样**地抢救到本地。

核心特性：

| | |
|---|---|
| **RAW FIRST** | 每个网络响应先原样落盘，再解析。解析器有 bug 也能离线重建，无需再访问百度 |
| **可恢复** | 任意阶段中断后重跑即续传，已完成的内容永不重复下载 |
| **可验证** | 自动核对 API `totalCount` 与实际归档数，明确告诉你"到底有没有漏" |
| **保留原始** | 正文原始 HTML 永不修改；失效的图片/外链 URL 一律保留，不删元素 |
| **只读** | 只发 GET。不删除、不编辑、不修改百度服务器上的任何内容 |
| **保守** | 默认并发 2、间隔 0.8s，面对遗留系统可靠性优先于速度 |

> 仓库中另有 [`BaiduExporter.py`](BaiduExporter.py)（Selenium + CSV + WordPress WXR），
> 是本工具的前身，文档见 [README.legacy.md](README.legacy.md)。新工具已内置 WXR 导出。

---

## 安装

需要 Python 3.9+（开发环境为 3.14）。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

依赖只有四个：`httpx`、`beautifulsoup4`、`lxml`、`markdownify`。

运行方式二选一：

```bash
.venv/bin/python -m baidu_archive <命令>
```

或装成命令：

```bash
.venv/bin/python -m pip install -e .
baidu-archive <命令>
```

---

## 提供 Cookie

归档的是**你本人账号**的历史数据，因此需要登录态。
凭证只保存在本地 `secrets/`（已被 `.gitignore` 覆盖），**永不写入日志、报告或源码**。

### 方式 A：从 DevTools 导入（推荐）

这一步同时解决凭证和"列表接口 URL 到底是什么"两个问题。

1. 浏览器登录百度，打开你的文章/收藏列表页
2. F12 → **Network** → 筛选 **Fetch/XHR**
3. 向下滚动列表，触发翻页请求
4. 找到返回 JSON（含 `totalCount`）的那条请求，**右键 → Copy → Copy as cURL**
5. 粘贴到一个文件里，例如 `list.curl.txt`
6. 导入：

```bash
.venv/bin/python -m baidu_archive import-curl list.curl.txt
```

导入结果会显示 Cookie 名称与打码后的 URL 模板，不会回显任何真实值。

### 方式 B：环境变量

```bash
export BAIDU_ARCHIVE_COOKIE='BDUSS=...; STOKEN=...'
```

### 方式 C：Cookie 文件

把完整 Cookie header 串写入 `secrets/cookies.txt`；
也支持浏览器扩展导出的 `secrets/cookies.json` 与 `secrets/cookies.netscape.txt`。

> 用方式 B / C 时，列表接口 URL 需要单独写进 `config.json`：
> ```json
> { "list_url_template": "https://.../getlist?start={start}&limit={limit}" }
> ```
> 也可以让 `doctor --probe` 尝试自动探测。

---

## 快速开始

```bash
# 1. 体检：确认凭证有效、端点可达
.venv/bin/python -m baidu_archive doctor

# 2. 最小链路验证（只抓 1 篇，不碰全量）
.venv/bin/python -m baidu_archive dry-run

# 3. 确认无误后，全量归档
.venv/bin/python -m baidu_archive all

# 4. 看报告
.venv/bin/python -m baidu_archive verify
```

**务必先 `dry-run` 再 `all`**。dry-run 会依次验证：
列表接口 → `totalCount` → 解析记录 → 取一条 key → 详情页 → iframe → 正文 → 资源发现。

指定某一篇验证：

```bash
.venv/bin/python -m baidu_archive dry-run --key 0000000000000002-1000000002
```

---

## 命令

| 命令 | 作用 |
|---|---|
| `import-curl <文件>` | 从 DevTools cURL 导入端点与凭证 |
| `doctor [--probe]` | 体检：配置 / 凭证 / 端点连通性 |
| `dry-run [--key K]` | 最小链路验证，不全量抓 |
| `index` | 翻完所有分页，建立**全部记录**的 master index |
| `fetch` | 抓取详情页 + 正文页原始 HTML |
| `parse` | 从 `raw/` 重建 metadata 与正文（**离线，不发请求**） |
| `assets` | 下载图片等资源，生成 `content.local.html` |
| `export` | 导出 `md` / `txt` / `wxr` / `site` |
| `verify` | 完整性校验，生成报告 |
| `retry-failed` | 只重试失败 / 警告 / 未完成的条目 |
| `status` | 当前进度速览 |
| `all` | 上述流水线一次跑完 |

常用参数：`--root` `--concurrency` `--delay` `--force` `--limit` `--max-pages`

```bash
# 只跑前 2 页，先看看效果
.venv/bin/python -m baidu_archive index --max-pages 2

# 放慢速度
.venv/bin/python -m baidu_archive --delay 2.0 --concurrency 1 fetch

# 同时下载视频/音频（默认只下图片）
.venv/bin/python -m baidu_archive assets --include-media

# 导出全部格式
.venv/bin/python -m baidu_archive export --format all
```

---

## 中断与恢复

**直接 Ctrl+C 即可**，无需任何清理。

所有文件写入都是原子的（临时文件 + `fsync` + `os.replace`），状态存在 SQLite 事务里，
任何时刻断电都不会留下半个 JSON。

恢复就是**重新运行同一条命令**：

```bash
.venv/bin/python -m baidu_archive all
```

已完成的条目会显示 `[SKIP] already complete` 并被跳过。

### 失败重试

```bash
.venv/bin/python -m baidu_archive retry-failed
```

只处理 `failed` / `warning` / `pending` 状态的条目。

### Cookie 失效

工具会主动识别登录态失效 —— 不只看 HTTP 200，还检查响应是否为登录页、
JSON 结构是否异常。**连续 5 条出现认证特征就立即停止整个任务**，
避免把后面几百条污染成假失败。

看到 `[AUTH] 连续 N 次请求出现认证失效特征` 时：重新导入 Cookie，再跑一次即可续传。

---

## 输出目录

```
archive/
├── state.db                  SQLite 状态（可从 raw 完全重建）
├── master_index.json         全部记录，保留列表原始顺序
├── master_index.csv          同上，Excel 可直接打开
├── crawl_state.json          进度快照
│
├── raw/                      ← 原始响应，只写一次，永不修改
│   ├── lists/                列表接口 JSON（含 .meta.json 旁路元数据）
│   ├── detail_pages/         /page/view 原始 HTML
│   └── content_pages/        iframe 正文页原始 HTML
│
├── items/<key>/              ← 解析产物
│   ├── metadata.json         标题、双份日期、标签、来源、资源清单、hash
│   ├── content.original.html 正文原始 HTML（永不修改）
│   ├── content.local.html    图片 src 重写为本地路径
│   ├── content.md            Markdown
│   ├── content.txt           纯文本（全文搜索用）
│   ├── assets/               下载到本地的图片等
│   └── asset_manifest.json   每个资源的 URL / 状态 / sha256 / 大小
│
├── reports/
│   ├── summary.json / summary.md    完整性报告
│   ├── failed_items.csv             需要关注的条目
│   └── external_resources.csv       全部外链（含已失效的）
│
└── logs/archive-YYYYMMDD.log

site/                         静态站点，百度关闭后仍可离线浏览
├── index.html                按年份分组 + 标题搜索
└── articles/<日期_标题_key>/
```

### 几个设计细节

- **目录名用 `key`**，不用标题 —— 标题可能重复、超长、含非法字符。可读性由
  `master_index.csv` 和静态站点承担。
- **日期保存两份**：列表里的 `list_date` 和正文 footer 里的 `content_date`。
  两者不同时**都保留**，不擅自判断谁正确。
- **`totalCount` 是 504 条全部收藏**，不全是百度空间文章。`article`（如百度知道收藏）
  等类型同样进 master index，只是第一阶段不抓正文。
- **重复只报告不删除**：两个不同 key 即使正文相同，也可能代表不同的历史记录。

---

## 安全注意事项

- **凭证**：只存 `secrets/`（`0600` 权限），已被 `.gitignore` 覆盖。
  日志、报告、异常信息中的 URL 一律经 `redact_url()` 打码，`sign` / `expire` / `uid` / `token` 全部替换为 `<redacted>`。
  完整签名 URL 只出现在本地 `metadata.json` 中，用于将来复原。
- **不破解签名**：每次需要正文时，都从当前 `/page/view` 页面解析最新的 iframe `src`。
  签名过期时自动回详情页重取，绝不自行计算 `sign`。
- **不枚举、不越界**：只访问从你自己账号列表中实际取得的记录，不枚举 key、
  不扫描其他用户、不递归抓取外站。外链只登记进 `external_resources.csv`。
- **只读**：全程只发 GET，不调用任何 delete / edit / add 接口。
- **测试 fixture 全部是脱敏构造数据**，不含任何真实 Cookie / bdstoken / sign / uid。

---

## 开发

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

解析逻辑与网络完全解耦，全部可离线单测（126 个测试，含端到端离线流水线）。

架构与设计取舍见 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)。

### 页面结构变了怎么办？

所有选择器都用**前缀匹配**（`[id^="detailArticleContent_"]`），
不硬编码百度的构建哈希（如 `_ptkaiapt4bxy_baiduscarticle`）—— 那串每次发版都会变。

若百度仍然改版：改 `parsers.py` 后跑 `parse --force` 即可从已有 `raw/` 重建全部归档，
**不需要重新访问百度**。这正是 RAW FIRST 的意义。
