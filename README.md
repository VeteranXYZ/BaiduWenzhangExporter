# 百度空间归档 · BaiduWenzhangExporter

<img src="baidu_archive/ui/icon.png" width="72" height="72" alt="百度空间归档图标">

将本人百度账号中仍可访问的空间文章、图片和收藏目录保存到本机。下载时先保留原始响应，整理、阅读、导出和校验可以在离线状态下完成。

| 功能 | 说明 |
| --- | --- |
| 桌面应用 | 登录、同步、搜索文章、查看图片、处理缺失项和导出 |
| 离线阅读 | 双击导出的 HTML 目录即可阅读正文和已保存图片 |
| 定向补救 | 按文章、图片分组或单个图片引用重试，保留尝试结果 |
| 可校验备份 | 保留原始响应、正文版本、数据库和缺失清单，逐文件记录 SHA-256 |

## 安装与启动

需要 Python 3.9 或更高版本。桌面端已在 macOS 验证，所用窗口由 pywebview 调用系统 WebKit。CLI 和本地操作台另有 Ubuntu 发布检查。Windows 桌面端的资源校验和全量备份仍待验证。

进入仓库目录后执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[gui]'
baidu-archive gui
```

仅用命令行或本地操作台时，安装 `python -m pip install -e .` 即可。虚拟环境内也可用 `python -m baidu_archive` 运行相同命令。

默认归档位置是工作目录下的 `archive/`。打开已有归档时，可把 `--root` 放在子命令前。

```bash
baidu-archive --root /path/to/archive gui
```

## 用桌面应用归档

1. 在「归档概览」点「登录百度」，在百度页面完成验证。登录窗口打不开时，可以从「设置」导入浏览器复制的收藏列表 cURL。
2. 点「开始下载」。应用会读取收藏列表、下载空间文章、解析正文，再下载图片。停止后可从概览继续。
3. 在「文章」里按标题、摘要、年份或保存状态筛选。文章可跨页勾选，「导出所选」会把范围带到导出页。
4. 在「图片」里按年份浏览缩略图，点开即可阅读对应文章。在「其他收藏」中可通过「打开原地址」访问外部收藏。

外部收藏会保留原始目录记录。访问旧链接时，源站可能返回原内容、跳转页、访问验证或失效提示。已保存的正文和图片可以离线阅读。

### 补救缺失内容

| 界面分类 | 可用操作 |
| --- | --- |
| 正文还未保存 | 登录后继续下载这些文章 |
| 图片未保存 | 重试本组或指定图片，已验证的图片会跳过 |
| 原图不可用 | 已确认返回占位图。存在符合条件的同站替代地址时，可尝试替代图片 |
| 旧图片待核对 | 用已有原始响应和本地文件离线补证 |
| 外部嵌入 | 保留原址线索，离线阅读版会标明无法播放 |

补救失败时，原始文件和已有失败证据会继续保留。新返回的错误页或占位图不会计作成功图片。

## 导出与恢复

在桌面「导出」页可以选择保存位置，也可以直接写到归档目录下的 `exports/`。导出使用本地内容。

| 导出方式 | 内容与范围 |
| --- | --- |
| 离线备份 | 全部已保存正文、离线图片、收藏总目录、文本 ZIP、WordPress 导入文件、缺失清单及原始文件 ZIP |
| 文章文本 ZIP | 全部已保存或当前勾选范围的 Markdown、TXT、有效媒体及清单 |
| 图片 ZIP | 全部已保存或当前勾选范围内通过校验的图片，附文章范围、缺失原因和文件摘要 |

有资源缺失或校验失败时，应用显示「部分导出」。通过校验的正文和图片继续导出，缺失项单独列入清单。文本 ZIP 或 WXR 生成失败时，备份会列出未生成的格式。选择范围内没有有效图片时，图片 ZIP 无法生成。

| 离线备份内的文件 | 用途 |
| --- | --- |
| `打开归档.html` | 双击进入目录，支持正文搜索、年份筛选和查看原地址 |
| `articles/` | 每篇一页，随附可离线显示的已校验媒体 |
| `总目录.csv` / `总目录.json` | 所有已索引收藏，含外部收藏及其原地址 |
| `缺失与资源清单.json` | 已纳入的资源、缺失原因与未纳入的正文 |
| `导出校验清单.json` | 备份中每个产物的字节数和 SHA-256 |
| `原始文件与版本.zip` | 私有恢复包，包含 `state.db`、原始响应、正文及历史文件 |
| `WordPress导入.xml` | WXR 迁移文件，媒体链接仍引用原站 |

移动备份时请复制整个文件夹。WordPress 导入后的远程媒体仍依赖源站，离线副本中的图片可直接从 `articles/` 或图片 ZIP 取出。

要从自己的「原始文件与版本.zip」恢复，请在仓库之外新建空目录并解压，使 `state.db`、`raw/`、`items/` 位于同一层。然后核对归档，或用桌面端打开：

```bash
baidu-archive --root ../restored-archive verify
baidu-archive --root ../restored-archive gui
```

## 命令行与本地操作台

### 通过浏览器导入登录会话

1. 在浏览器登录百度，打开收藏列表页。
2. 在开发者工具的 **Network → Fetch/XHR** 面板中找到收藏列表请求。向下翻页可以触发请求。
3. 确认响应含 `totalCount`，然后使用 **Copy as cURL**。可以先创建 `secrets/` 目录并保存为 `secrets/list.curl.txt`，或直接粘贴到桌面「设置」的导入框。
4. 用命令行时执行以下命令：

```bash
baidu-archive import-curl secrets/list.curl.txt
baidu-archive doctor
baidu-archive dry-run
```

`dry-run` 验证列表、单篇详情、正文和资源发现。诊断响应单独保存在 `archive/raw/diagnostics/`。也可以通过 `BAIDU_ARCHIVE_COOKIE` 环境变量或 `secrets/cookies.txt`、`secrets/cookies.json`、`secrets/cookies.netscape.txt` 提供会话。

### 日常命令

下面的 `YOUR_KEY` 替换为自己归档的 `master_index.json` 中的文章标识，`--key` 可以重复提供。

```bash
# 下载、整理、导出和校验
baidu-archive all

# 仅处理指定文章
baidu-archive fetch --key YOUR_KEY
baidu-archive parse --key YOUR_KEY
baidu-archive assets --key YOUR_KEY

# 生成独立的离线阅读目录
baidu-archive export --format site --site-dir site

# 离线导出所选文章
baidu-archive export-selection --key YOUR_KEY --output archive/exports/selected.zip

# 查看进度与完整性报告
baidu-archive status
baidu-archive verify
```

| 命令 | 说明 |
| --- | --- |
| `index` | 读取收藏列表并核对分页。可用 `--max-pages` 限定检查页数 |
| `fetch` | 保存详情和正文页原始响应。可用 `--key` 或 `--limit` 缩小范围 |
| `parse` | 离线解析已保存的原始响应 |
| `assets` | 下载图片。可用 `--include-media` 处理支持的音视频 |
| `export` | 默认生成 Markdown 和 TXT。可加 `--format wxr`、`--format site` 或 `--format all` |
| `retry-failed` | 重试失败、待处理、告警或资源未齐的文章。仅资源失败时跳过正文重抓 |
| `verify` | 核对索引、正文、资源和版本记录，生成校验报告 |
| `serve` | 启动仅监听本机的浏览器操作台 |

```bash
baidu-archive serve --port 8765
```

在本机浏览器访问 `http://127.0.0.1:8765/`。操作台支持勾选文章执行任务、导出文本或图片 ZIP、暂停任务并从历史记录下载产物。用完可用 Ctrl+C 停止服务。

## 校验、继续与旧档迁移

- **校验结果**。查看 `archive/reports/summary.md`。`verify` 通过时返回 `0`，归档未达完整条件时返回 `1`。`stored_file_integrity.json` 单独列出已保存文件的校验结果。索引完整、正文齐全和资源完整是不同检查项。
- **继续任务**。用界面的停止按钮或 Ctrl+C 停止后，可以重跑同一命令。写任务按归档目录加锁。相同归档的写任务冲突时，CLI 返回 `6`。
- **登录失效**。重新登录或更新导入会话，再继续原任务。遇到连续认证失败时，下载会停止并保留已完成进度。
- **旧档补证**。旧数据库升级前会在归档的 `backups/` 中生成并校验备份。桌面自动整理旧数据也会先备份，失败时停止。命令行可用 `verify --rebuild-legacy-evidence` 离线补证。只有核实过的列表和资源才能达到完整状态。
- **版本保留**。强制重抓会另存原始响应版本，并更新 `.latest.json`。用 `parse --force` 重新解析时，首次 `content.original.html` 仍保留，新正文保存到 `parsed/<sha256>.html`。

默认同时下载 2 篇，请求最小间隔 0.8 秒。命令行、环境变量和配置文件都遵守最多同时下载 3 篇、请求间隔至少 0.5 秒的限制。列表固定每页 30 条。

### 本地文件

```text
archive/
├── state.db                       SQLite 状态与运行记录
├── master_index.json / .csv       收藏索引和原始地址
├── raw/                           原始响应、版本及失败证据
│   ├── lists/
│   ├── detail_pages/ / content_pages/
│   └── asset_responses/ / image_recovery/
├── items/<key>/                   正文、元数据和资源
│   ├── content.original.html
│   ├── parsed/ / content.local.html
│   └── assets/ / asset_manifest.json
├── backups/                       升级前数据库快照
├── reports/                       完整性、失败项与外部资源报告
└── exports/                       本地导出结果
```

## 隐私与访问范围

仅归档自己有权访问的内容。归档请求以只读方式访问收藏列表及对应资源。第三方收藏会保留目录和链接，打开原地址由用户发起。

`secrets/`、真实归档、诊断响应和导出文件只留在本机。原始响应和私有恢复包可能包含私人内容、登录相关响应或签名链接。提交前核对暂存区，避免将这些内容或真实归档标识加入仓库。

## 开发与发布前检查

在虚拟环境内运行：

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
node --test tests/test_dashboard.cjs
python scripts/check_release_package.py
```

发布检查从 Git 跟踪清单构建 wheel，检查桌面、操作台和阅读资源，独立安装后运行测试、CLI 和 HTTP 检查。在 macOS 桌面会话中可进一步验证已安装的原生窗口：

```bash
python scripts/check_release_package.py --native-gui
```

CI 使用 Python 3.9 和 3.14 检查 Ubuntu 上的命令行和 wheel，并在 macOS 上启动桌面版。发布前的真实环境验收还包括百度登录、原生保存位置选择和测试 WordPress 站点导入。

仓库保留旧 Selenium 脚本，使用说明见 [旧版说明](README.legacy.md)。

项目采用 [MIT 许可证](LICENSE)。
