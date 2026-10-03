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

## 接管验证进度（2026-09-15）

界面方向：按用户指定的 QzoneArchive 桌面布局推进，本地操作台已接通选择抓取、任务进度与导出。
核心多版本归档与接口规划见 [CORE_DESIGN.md](CORE_DESIGN.md)。

已使用本人账号的真实登录态跑通 `doctor → dry-run → index → fetch → parse → assets → export → verify`：

- **504 条全部建立索引**：497 篇空间文章、7 条其他收藏。
- **497 篇正文全部保存并解析成功**；3 篇首次返回“页面不存在”，刷新详情签名后恢复。
- 233 个图片引用已尝试：**219 个成功、14 个失败**，失败涉及 7 篇文章，正文不受影响。
  7 个嵌入媒体和 175 个外链仅登记；明细见本地 `archive/reports/`。
- 原始响应和首次正文保持不可变；升级前 17 份基线文件摘要全部一致。
- 2,745 个归档文件校验通过；497 篇 Markdown/TXT、WXR 与离线 ZIP 已生成。
- 211 项测试通过。`verify` 返回 1，准确反映尚有 14 个图片引用未能保存。
- 浏览器与 CLI 登录链路已验证；软件内置扫码登录窗口尚未实现。

详情和下一阶段设计见 [接管记录](TAKEOVER_PROGRESS.md)。

完整正文便携包：`archive/exports/baidu-space-497-2026-09-15.zip`（约 20 MB），
包含 Markdown/TXT、已下载图片、文章元数据和逐文件 SHA-256 清单。
原始网页与历史快照仍保留在 `archive/raw/`，便携包不替代完整档案目录。
`site/` 是静态阅读副本；抓取、任务控制与图片 ZIP 导出请使用下面的本地操作台。

### 打开本地操作台

```bash
.venv/bin/python -m baidu_archive serve --port 8765
# 浏览器打开 http://127.0.0.1:8765/
# 用完按 Ctrl+C 关闭服务
```

在「文章归档」中搜索或按年份筛选，勾选文章后可抓取、补齐图片、导出文章。
页面采用桌面侧栏布局，支持折叠侧栏。

- **导出全部图片**：打包全部空间文章已下载的图片，不受当前搜索、年份、分页或勾选影响。
- **导出所选图片**：仅打包已勾选文章的图片；跨页和筛选外的选择仍有效，界面会显示数量。
- 点击后直接开始本地任务，完成后触发 ZIP 下载；也可从当前任务或最近运行记录再次下载。
- ZIP 按文章分文件夹，仅包含图片和 `manifest.json` 清单；清单记录每张图片的 SHA-256、文章范围和未下载引用。
- 导出不请求百度，不自动补抓缺图。没有已下载图片时无法导出；文件缺失或校验失败时不会发布残缺包。
- 输出保存在 `archive/exports/images-<随机标识>.zip`；停止或失败时清除中间文件，已有导出不会被覆盖。

2026-10-01 已实测：所选单篇图片导出、全库 497 篇的 219 张图片导出（约 20.7 MB），
14 个尚未下载的图片引用已在清单中记录；242 项测试通过。
原生 `gui` 外壳仍为界面原型；上述已接通功能当前使用 `serve` 入口。

### 打开离线阅读界面

```bash
.venv/bin/python -m baidu_archive export --format site
open site/index.html  # macOS；其他系统直接用浏览器打开该文件
```

界面支持标题/正文搜索、年份筛选、归档状态分类，以及勾选可读文章合并导出 TXT。
未抓取正文的收藏仍显示在目录中，不能误选为已归档文章。
站点包含自己的资源文件，可复制整个 `site/` 文件夹离线使用，无需服务器。
这是离线阅读界面；扫码登录和启动抓取目前仍由浏览器与 CLI 完成。

阅读副本禁用旧网页脚本、表单和外部嵌入；原始 HTML 仍完整保留在 `archive/` 中。

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

`doctor` 和 `dry-run` 每次收到的成功响应先保存到独立的
`archive/raw/diagnostics/<命令>-<唯一编号>/`，再解析；这些联调记录不计入正式归档进度。
首次建议先 `index --max-pages 2`、`fetch --limit 5` 做小批量检查。

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
| `export-selection` | 将明确选择的文章、图片与校验清单打包成 ZIP |
| `verify` | 完整性校验，生成报告 |
| `retry-failed` | 只重试失败 / 警告 / 未完成的条目 |
| `status` | 当前进度速览 |
| `all` | 上述流水线一次跑完 |

常用参数：`--root` `--concurrency` `--delay` `--page-limit` `--force` `--limit` `--max-pages`

```bash
# 只跑前 2 页，先看看效果
.venv/bin/python -m baidu_archive index --max-pages 2

# 放慢速度
.venv/bin/python -m baidu_archive --delay 2.0 --concurrency 1 fetch

# 列表每次请求 100 条；正文接口仍为每篇一次，靠并发提速
.venv/bin/python -m baidu_archive --page-limit 100 index
.venv/bin/python -m baidu_archive --delay 0.25 --concurrency 4 fetch

# 同时下载视频/音频（默认只下图片）
.venv/bin/python -m baidu_archive assets --include-media

# 导出全部格式
.venv/bin/python -m baidu_archive export --format all

# 仅导出所选文章，ZIP 内含 Markdown、TXT 和已下载图片
.venv/bin/python -m baidu_archive export-selection --key YOUR_KEY --output selected.zip
```

按本次用户要求，本地 `config.json` 已设为：列表每页 100 条、并发 4、
请求间隔 0.25 秒、随机附加间隔 0–0.15 秒。库默认值保留保守配置；
本地配置与环境变量可以覆盖，正文请求的 429/5xx 仍自动退避。
列表 100 条已用真实接口验证；本次首次完整索引使用原来的 30 条分页完成。

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

写任务按归档目录加进程锁，重复启动会返回退出码 6；运行期间仍可用 `status` 查看进度。
停止会结束后续调度，正在保存的文件完成原子写入后退出。索引、抓取、解析和资源任务
会记录完成、部分失败或中断结果；旧版中断任务可能显示 `running_or_interrupted`。

### 失败重试

```bash
.venv/bin/python -m baidu_archive retry-failed
```

只处理 `failed` / `warning` / `pending` 状态的条目。

也包括 `assets_partial`（资源部分失败）；仅资源失败时不重复抓取文章正文。
`parse` 和 `assets` 支持重复 `--key KEY` 参数，只处理所选文章。

### 强制重抓与重新解析

`fetch --force`、`index --force` 和签名刷新均另存 raw 版本，首次响应不覆盖。
当前版本由相邻的 `.latest.json` 引用；历史响应位于 `<原文件名>.versions/`。
`parse --force` 保留首次 `content.original.html`，新的解析结果保存在 `parsed/<sha256>.html`。
导出自动选择当前正文版本，仍可完全离线重建。

`verify` 现在同时检查文件 SHA-256、版本引用和派生数据是否过期，
明细保存在 `reports/stored_file_integrity.json`。本地校验失败不会自动覆盖或删除原件。

报告区分“源站正文不存在”与“图片下载失败”。纯图片文章仍属于有效正文；
图片地址返回的 HTML 错误页不会计入图片成功数，原始响应另存以便排查。

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

解析逻辑与网络完全解耦，测试包含端到端离线流水线、HTTP 凭证隔离、诊断响应保存和站点导出。

架构与设计取舍见 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)。

### 页面结构变了怎么办？

所有选择器都用**前缀匹配**（`[id^="detailArticleContent_"]`），
不硬编码百度的构建哈希（如 `_ptkaiapt4bxy_baiduscarticle`）—— 那串每次发版都会变。

若百度仍然改版：改 `parsers.py` 后跑 `parse --force` 即可从已有 `raw/` 重建全部归档，
**不需要重新访问百度**。这正是 RAW FIRST 的意义。

## 发布前验证

项目继续支持 Python 3.9+。CI 在 Python 3.9 与 3.14 上运行完整测试、CLI smoke test 和实际 wheel 安装检查。

```bash
.venv/bin/python -m pytest
node --test tests/test_dashboard.cjs
.venv/bin/python scripts/check_release_package.py
```

最后一条命令在临时目录中构建 wheel、检查 GUI/操作台/阅读站点资源，并在独立虚拟环境中安装依赖、运行入口和资源检查；结束后自动清理环境。

CLI 与操作台采用同一完整性判定；操作台校验结果显示接口声明、已索引和缺失数量。
WXR 使用当前未本地化正文及原始远程资源地址，导入后的图片仍依赖原始地址可访问；XML 解析检查不等同于真实 WordPress 导入测试。
文章 ZIP 和图片 ZIP 均支持停止，取消任务清除临时文件及尚未完成发布的结果。


### Round 3：媒体结构、旧归档与发布门禁

资源收到 bytes（downloaded）与结构验证通过（structurally_valid）是不同事实。
只有 accepted 媒体才能贡献文章 complete：PNG 校验 chunk/CRC/IHDR/IDAT/IEND，
其他位图由 Pillow 验证并解码每帧；SVG 必须是完整 SVG XML。
新增轻量运行依赖 Pillow>=10，避免将图片头识别当作解码成功。
WAV 验证 RIFF 长度和全部 PCM 帧；MP4 内部验证普通 H.264 视频的 AVC 配置、
轨道、样本表、chunk 偏移、样本边界与 NAL framing。
这不保证浏览器支持，也不等同完整视频解码。未知音视频编码、MP4 分片或不支持的轨道
返回 unverifiable，不能升级 complete；无需 ffmpeg/ffprobe。

旧 schema 首次打开通过 SQLite BEGIN IMMEDIATE 串行迁移到 user_version=1；
中断可回滚、重开幂等，未知更高版本拒绝打开。迁移不自动认证旧下载。
legacy_unverified 表示旧文件可能仍在，但当前文章/资源关系尚未补证，不等于未保存。

```bash
baidu-archive --root /path/to/archive status
baidu-archive --root /path/to/archive verify --rebuild-legacy-evidence
```

离线补证仅使用已保存 raw 快照与正文、base URL、资源关系、大小、SHA-256 和媒体结构；
不得发网络请求。证据不足保留未验证状态并报告原因。
缺少可信索引 generation 时，即便资源补证成功，也需要重新读取列表才能认证整体完整。
操作台提供「离线验证旧归档」动作（verify API: rebuild_legacy=true）。

stored_file_integrity（audit_stored_files / reports/stored_file_integrity.json）只描述
已保存文件与派生证据的校验，不代表归档完整；总结果以 summary.validation.ok 为准。
报告分别给出 current_resource_integrity 和 historical_retained_resource_integrity。
成功下载因 URL、内容或后续下载尝试被替代时，先保留路径、identity、大小、SHA 和时间；
这些历史文件承诺保留，删除/损坏会让校验失败。已经确认损坏的记录只作为诊断证据保留，
不承诺损坏文件的字节正确。

Server 媒体读取从归档 root 的目录 fd 开始，逐层 O_NOFOLLOW 打开，fstat、读取与摘要
均针对同一 fd。平台缺少目录 fd / O_NOFOLLOW 时保守拒绝读取；不回退到不安全的路径读取。
该平台限制会使资源无法认证 complete，需在目标平台实际验证。

发布检查先审计 git ls-files，再仅复制该清单中的文件构建 wheel，执行隔离安装、
clean tracked tree 全量 pytest、Node、pip check、项目目录外 CLI 正/负校验、
Server 启动与 HTTP、全部 UI/操作台/阅读资源 lookup。
导入的 runtime 模块或必需测试/fixture 未跟踪时立即失败，不能从 untracked 文件借用实现。
