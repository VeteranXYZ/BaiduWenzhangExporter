# 交接提示词：百度空间归档工具（baidu_archive）

你接手一个**已完成主体实现、但从未联网跑通**的项目。你相比前一位协作者的关键优势是
**能直接驱动浏览器**——这正好是当前唯一的卡点。

---

## 一、项目是什么

把用户本人账号里的百度空间/百度收藏历史内容（2008–2011 年为主，共 **504 条**）
抢救到本地。这不是爬虫练习，是数字档案抢救：旧服务随时可能关闭，数据没有第二份。

**决策优先级（冲突时按此排序）**：
1. 不丢数据 2. 保存原始数据 3. 完整性 4. 可恢复 5. 可验证
6. 资源本地化 7. 可读性 8. 性能 9. UI

---

## 二、仓库现状

```
仓库：~/GitHub/BaiduWenzhangExporter
分支：feat/baidu-archive（未推送，main 仍是旧的 Selenium 脚本）
提交：cb43d85 → 54fe594 → b2f5c9a → 0bfc540（4 个）
规模：baidu_archive/ 3900 行，tests/ 1082 行
测试：140 个全通过，ruff（F,E9）零告警
环境：.venv 已建好，Python 3.14，依赖 httpx/bs4/lxml/markdownify/pytest
```

### 架构（RAW FIRST）

```
/fav/list (JSON)  →  raw/lists/*.json
      ↓ 解析 data.html
  master index（504 条全部入库，page/article/other 都要）
      ↓ 仅 type=page
/page/view?key=…  →  raw/detail_pages/{key}.html
      ↓ 解析 iframe src（带 sign，短时效）
bdstatic.com/page/content/?… → raw/content_pages/{key}.html
      ↓ parse（纯离线，只读 raw/）
items/{key}/metadata.json + content.original.html  ← 永不修改
      ↓ assets
items/{key}/assets/** + content.local.html         ← src 重写为本地
      ↓ export / verify
```

**FETCH 与 PARSE 完全解耦**。解析器改 bug 后跑 `parse --force` 即可从 `raw/`
重建全部派生数据，**不需要再访问百度**。这是整个设计的核心，请务必维持。

### 模块

| 文件 | 职责 |
|---|---|
| `parsers.py` | 纯函数解析，无网络无磁盘。**改这里最安全** |
| `pipeline.py` | index / fetch / parse 三阶段 |
| `assets.py` | 资源下载 + content.local.html 重写 |
| `export.py` | md / txt / WXR / 静态站点 |
| `verify.py` | 完整性校验与报告 |
| `state.py` | SQLite 状态机（单调推进）+ 幂等补列迁移 |
| `client.py` | httpx：限速/退避/认证失效检测 |
| `auth.py` | 凭证加载 + `import-curl` |
| `cli.py` | 12 个子命令 |

### CLI

```bash
.venv/bin/python -m baidu_archive <cmd>

import-curl <file>   从 DevTools cURL 导入端点与凭证
doctor [--probe]     体检：配置/凭证/端点连通性
dry-run [--key K]    最小链路验证，只抓 1 篇
index                建立 504 条 master index
fetch                抓 detail + content 原始 HTML
parse                raw → metadata/正文（离线）
assets               下载图片，生成 content.local.html
export --format all  md/txt/wxr/site
verify               完整性报告
retry-failed         只重试失败项
status               进度速览
all                  全流水线
```

全局参数：`--root --concurrency --delay --force --limit --max-pages`

---

## 三、⚠️ 已验证 vs 未验证（最重要的一节）

### ✅ 已用真实响应验证

**1. 列表端点**（用户从 DevTools 捕获，工具本身未联网验证过）
```
https://wenzhang.baidu.com/fav/list?start={start}&limit={limit}&wd=&type=&getListHtml=1&_={ts}
```
`wd`/`type` 是搜索与类型筛选，留空即全部。`_` 是缓存穿透参数，每次请求现取毫秒时间戳
（已参数化为 `{ts}`，**不要把它冻结成固定值**）。

响应形如：
```json
{"request_id":…, "data":{"html":"<li…>", "start":100, "limit":30,
 "totalCount":504, "currentCount":30, "currentTag":null},
 "status":0, "msg":"您的操作进展顺利^_^"}
```

**2. 列表 HTML 结构** —— 曾据此修掉 4 个静默丢数据的 bug：
- 标题在列表项里出现两次：`.mask h4.article-h4 a` 是**完整标题**，
  `.unit-c h4 a._photoLink` 是**缩略版**（约 20 个全角字符处截断）。
  现在取所有候选中最长者。
- 标签在 `<div class="tags _tags"><span>空间</span></div>` 的 **span** 里，不是 a。
- 百度空间条目的外层 h4 带 `href="http://1"` **占位符**，不是真实外链。
- `.page-url` 在列表层就给出来源（「来自于百度空间」）。

**3. 正文页结构**（`bdstatic.com/page/content/?…`）
- 容器带构建哈希 `_ptkaiapt4bxy_baiduscarticle`，**已验证前缀选择器能命中**：
  `[id^="detailArticleTitle_"]` / `[id^="detailArticleContent_"]` / `[id^="detailArticleFooter_"]`
- `.time-cang` → 「收藏于 2010-11-09」，`.link-src` → 「来自于百度空间」
- `.tags-list li` 也带标签，`.note` 是收藏备注
- **页面声明 `<base href="http://1">`，是占位符。已记录进 metadata 但绝不采信**，
  相对 URL 一律以 iframe URL 为基准解析（有测试锁死，别改回去）
- 页脚投诉区有 `qingting.baidu.com` 链接和指向 `/add/ask` 的 POST 表单，
  因为资源扫描只作用于正文容器 inner HTML，天然被排除

### ❌ 尚未验证（你的工作从这里开始）

| # | 未知项 | 影响 |
|---|---|---|
| **1** | **`/page/view?key=…` 详情页的真实 HTML** | `parse_detail_html()` 完全按文字描述写的，**从未见过真实响应**。iframe 选择器可能全错 |
| **2** | Cookie 是否足以调用 `/fav/list` | 可能还需要额外的 header 或先访问一次列表页 |
| **3** | 整条流水线端到端 | 一次都没联网跑过 |
| **4** | `article` 类型条目的行为 | 504 条里混着百度知道收藏等，第一阶段只建索引不抓正文 |
| **5** | 限流阈值 | 默认并发 2 / 间隔 0.8s，是保守猜测 |

`parse_detail_html` 现在的 iframe 选择器（按优先级回退）：
```python
soup.select_one("iframe.pcs-article-iframe")
or soup.select_one('iframe[class*="pcsArticleIframe"]')
or soup.select_one('iframe[src*="/page/content/"]')
or soup.select_one('iframe[src*="bdstatic.com"]')
or soup.find("iframe", src=True)
```
**注意**：`src` 里的 `&amp;` 必须解码成 `&`，否则签名 URL 会带字面 `&amp;` 而失效。

---

## 四、你的任务（按顺序）

### 任务 1：拿到登录态 ★最高优先级

用浏览器登录百度，打开用户的收藏列表页，捕获登录态。三种落地方式任选：

**A. 复刻 cURL 导入**（最省事，现有代码直接支持）
把 DevTools 的 Copy as cURL 内容存成文件，然后：
```bash
.venv/bin/python -m baidu_archive import-curl list.curl.txt
```
会写入 `secrets/session.json`（0600，已被 gitignore）。

**B. 环境变量**
```bash
export BAIDU_ARCHIVE_COOKIE='BDUSS=…; STOKEN=…'
```

**C. 实现扫码登录**（用户明确想要，但**优先级低于先跑通归档**）
建议方案：`pywebview`（6.2.1，已实测有 `Window.get_cookies()`，
macOS 走系统 WKWebView，无需下载 Chromium，仅 2.4MB）。
打开 `passport.baidu.com` 登录页让用户扫码，成功后取 cookie 交给 httpx。
**不要逆向 passport 的扫码接口**——用百度自己的登录页最稳。

验证：
```bash
.venv/bin/python -m baidu_archive doctor
```
应输出 `✅ totalCount = 504`。

### 任务 2：验证并修正详情页解析 ★关键

这是唯一没见过真实响应的环节。

1. 用浏览器打开任意一条 `https://wenzhang.baidu.com/page/view?key=<KEY>`
   （key 从列表 JSON 里取，例如 `0000000000000001-1000000001`）
2. 拿到真实 HTML，核对 `parse_detail_html()` 是否能提取出 iframe src
3. 不对就改 `parsers.py`（**只改解析器，不动流水线**）
4. 把真实结构**脱敏后**加进 `tests/fixtures/detail_page.html`，补回归测试

然后：
```bash
.venv/bin/python -m baidu_archive dry-run
```
它会走完 list → key → detail → iframe → content → 资源发现全链路。

### 任务 3：小批量试跑

**不要一上来就抓 504 条。**

```bash
.venv/bin/python -m baidu_archive index --max-pages 2      # 先 60 条索引
.venv/bin/python -m baidu_archive fetch --limit 5          # 先抓 5 篇
.venv/bin/python -m baidu_archive parse
.venv/bin/python -m baidu_archive assets --limit 5
.venv/bin/python -m baidu_archive verify
```

人工检查 `archive/items/<key>/content.original.html` 内容对不对，
`reports/summary.md` 数字合不合理。

### 任务 4：全量归档

```bash
.venv/bin/python -m baidu_archive all
```

17 个列表请求 + 约 N×2 个页面请求。中断随时 Ctrl+C，重跑即续传。

盯着这几件事：
- 出现 429/5xx → 调大 `--delay`，调小 `--concurrency`
- `[AUTH]` 连续认证失败 → 换 Cookie 后重跑（已归档的不受影响）
- `verify` 报告里 `index_complete` 必须是 ✅，`failed` 必须是 0

### 任务 5（可选）：扫码登录 + GUI

用户最终想要「打开软件、扫码登录、可视化、可勾选导出」。
建议 `pywebview` 一个依赖同时解决登录窗口和 GUI。
**但务必先让归档跑通**——数据抢救优先于 UI。

---

## 五、铁律（继承自原始需求，不可违反）

**绝对禁止**
- 调用 delete / edit / add / `add/ask` 等任何写接口，只发 **GET**
- 自行计算或破解 `sign`——每次都从当前 `/page/view` 页面取最新 iframe src
- 暴力枚举 key、扫描其他用户、递归抓取外站（人人/土豆等只登记不下载）
- 把 Cookie / bdstoken / sign / uid 写进源码、日志、README、测试 fixture
- 为了"清洁"覆盖 `raw/` 或 `content.original.html`
- 因为图片 404 就删除 HTML 里的元素——**失效的 URL 本身就是档案信息**
- 因为正文重复就自动删除文章——只报告，不删
- 高并发轰炸这个遗留系统

**必须做到**
- 每个有价值的响应先落盘 `raw/` 再解析
- 所有文件写入走 `write_atomic()`（临时文件 + fsync + os.replace）
- 日志/报告里的 URL 一律经 `redact_url()` 打码
- 504 条全部进 master index，`article` 类型不许丢弃
- 保留列表原始顺序（`global_position` / `list_offset` / `position_in_page`）
- 双份日期（`list_date` / `content_date`）、双份来源、双份标签都各存一份，
  不同时**都保留**，不擅自判断谁正确
- 字段为空也不删除字段
- 新增/修改解析逻辑必须配单测，fixture 必须脱敏

---

## 六、常用命令

```bash
cd ~/GitHub/BaiduWenzhangExporter

.venv/bin/python -m pytest tests/ -q                      # 测试
.venv/bin/python -m ruff check baidu_archive/ tests/ --select F,E9   # lint
.venv/bin/python -m baidu_archive status                  # 进度
.venv/bin/python -m baidu_archive verify                  # 报告

# 解析器改动后从 raw 重建全部归档（不联网）
.venv/bin/python -m baidu_archive parse --force
```

提交前务必确认：
```bash
git status --short          # secrets/ archive/ site/ config.json 不该出现
grep -rn "BDUSS\|sign=[a-f0-9]\{16,\}\|uid=[0-9]\{9,\}" --exclude-dir=.git --exclude-dir=.venv .
```

更多设计取舍见 `IMPLEMENTATION_PLAN.md`，使用说明见 `README.md`。
