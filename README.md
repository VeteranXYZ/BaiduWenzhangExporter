# BaiduWenzhangExporter

百度文章迁移工具。用于将百度文章（原百度空间/百度文章）批量抓取、清洗、排序，并导出为 WordPress 可直接导入的 WXR 文件。

这个脚本来自实际迁移需求：百度文章列表需要登录后访问，正文存在 iframe、展开全文、懒加载图片、冗余 HTML、时间排序和中途失败等问题。`BaiduExporter.py` 把浏览器抓取、断点续传、内容清洗、时间排序和 WordPress 打包整合在一个脚本里。

## 功能

- 自动打开 Chrome 并等待用户登录百度账号
- 使用 `webdriver-manager` 自动安装/匹配 ChromeDriver
- 深度滚动文章列表，收集所有文章链接
- 逐篇进入文章 iframe 抓取标题、正文、发布时间和原始 URL
- 自动点击“阅读全文 / 展开全文”
- 三段式滚动触发懒加载图片
- 清洗百度冗余 HTML，保留正文结构
- 实时写入 `baidu_articles_RAW.csv`，支持断点续传
- 生成按发布时间倒序排列的 `baidu_articles_SORTED.csv`
- 生成 WordPress WXR 导入文件 `baidu_wordpress_import_FINAL.xml`
- 为文章添加 WordPress 分类 `百度文章`
- 保留原始百度文章 URL 到 `_original_url` 自定义字段

## 文件结构

```text
BaiduWenzhangExporter/
  BaiduExporter.py
  baidu_articles_RAW.csv
  baidu_articles_SORTED.csv
  baidu_wordpress_import_FINAL.xml
```

说明：

- `BaiduExporter.py`：主程序，负责抓取、清洗、排序和导出。
- `baidu_articles_RAW.csv`：实时抓取备份。每成功抓取一篇就写入一次，是断点续传依据。
- `baidu_articles_SORTED.csv`：最终清洗并按发布时间倒序排列的 CSV 备份。
- `baidu_wordpress_import_FINAL.xml`：最终 WordPress WXR 导入文件。

后三个文件由脚本运行后自动生成。

## 环境要求

- Python 3.8+
- Google Chrome
- 可访问百度文章页面的百度账号
- 稳定网络环境

安装依赖：

```bash
pip install selenium beautifulsoup4 webdriver-manager
```

## 使用方法

运行脚本：

```bash
python BaiduExporter.py
```

执行流程：

1. 脚本会自动启动 Chrome，并打开百度文章首页。
2. 在浏览器中手动登录百度账号。
3. 登录后进入「我的文章」列表页面。
4. 回到终端，按下回车。
5. 脚本开始自动扫描列表、抓取文章、清洗内容并实时写入 CSV。
6. 抓取结束后，脚本会自动生成排序后的 CSV 和 WordPress WXR 文件。

脚本启动 Chrome 时使用 `eager` 页面加载策略：HTML 主体加载完成后就开始执行，不等待所有图片资源完全加载。单篇文章内部会再通过滚动触发懒加载图片。

## 导入 WordPress

1. 登录 WordPress 后台。
2. 进入 `工具` -> `导入`。
3. 找到 `WordPress` 导入器。
4. 上传 `baidu_wordpress_import_FINAL.xml`。
5. 分配作者并开始导入。

导入后，文章会以 `post` 类型发布，并自动归入 `百度文章` 分类。

WXR 中的默认作者为 `admin`。如果你的 WordPress 站点没有这个作者，导入器会让你重新分配作者。

## 输出文件

### `baidu_articles_RAW.csv`

实时写入的原始抓取结果。字段包括：

- 标题
- 文章内容
- 发布时间
- 原始URL

如果脚本中途停止，再次运行时会读取这个文件，跳过已抓取过的文章。

### `baidu_articles_SORTED.csv`

最终排序后的 CSV。脚本会按发布时间倒序排列文章，方便检查和备份。

### `baidu_wordpress_import_FINAL.xml`

WordPress WXR 文件，可直接通过 WordPress 官方导入器导入。

生成规则：

- `wp:post_type` 为 `post`
- `wp:status` 为 `publish`
- 分类为 `百度文章`
- 原始 URL 写入 `_original_url`
- 正文写入 `content:encoded`
- 标题和正文使用 CDATA 包裹
- slug 从标题生成，最长保留 50 个字符；如果标题无法生成 slug，则使用 `post-{序号}`

## 断点续传

脚本采用实时追加写入方式。每成功抓取一篇文章，就会立即写入 `baidu_articles_RAW.csv` 并 flush 到磁盘。

如果出现以下情况：

- 网络中断
- 电脑卡死
- Chrome 崩溃
- 手动 `Ctrl+C` 停止脚本

可以直接重新运行：

```bash
python BaiduExporter.py
```

脚本会读取已有的 `baidu_articles_RAW.csv`，自动跳过已经抓取过的 URL，只继续处理剩余文章。

断点判断依据是 `原始URL` 字段。只要某篇文章已经写入 `baidu_articles_RAW.csv`，再次运行时就不会重复抓取。

## 内容处理

脚本会对百度文章正文做基础清洗：

- 定位百度文章正文容器
- 移除空 `div` / `p`
- 保留正文段落、图片和基础 HTML 结构
- 将纯文本换行转换为 `<br>`
- 压缩过多连续换行
- 使用 CDATA 包裹正文写入 WXR，减少 WordPress 导入时的转义问题

如果脚本找不到正文容器，会写入 `<p>无内容</p>` 作为占位内容。

## 抓取逻辑

脚本主要分为三个阶段。

### 1. 扫描列表

在「我的文章」列表页中，脚本会持续滚动页面，直到页面高度连续多次不再变化。随后读取 `li.unit[id^="key_"]` 元素，并根据元素 class 判断文章链接类型：

- class 中包含 `article` -> `https://wenzhang.baidu.com/article/view?key=...`
- 否则 -> `https://wenzhang.baidu.com/page/view?key=...`

### 2. 抓取单篇

每篇文章会按以下顺序处理：

1. 打开文章 URL。
2. 进入 `iframe.pcs-article-iframe`。
3. 等待 body 出现。
4. 尝试点击 `阅读全文`、`展开全文` 或 `展开`。
5. 滚动到 33%、66%、100% 位置，触发懒加载内容。
6. 解析标题、发布时间、正文。
7. 清洗正文 HTML。
8. 返回主页面上下文。

如果单篇抓取失败，脚本会打印错误并跳过该链接，继续处理下一篇。

### 3. 打包输出

抓取结束后，脚本会读取 `baidu_articles_RAW.csv`：

1. 过滤有发布时间的记录。
2. 按发布时间倒序排序。
3. 写入 `baidu_articles_SORTED.csv`。
4. 生成 WordPress WXR 文件。

如果某篇文章的日期无法解析，会按 `1970-01-01` 处理，避免排序和导出中断。

## 注意事项

- 抓取前必须手动登录百度账号。
- 请确保已经进入「我的文章」列表页后再回到终端按回车。
- 抓取过程中不要手动关闭 Chrome。
- 如果百度页面结构变化，选择器可能需要调整。
- 单篇文章抓取失败时会跳过，不会终止整个任务。
- 日期解析失败时会使用 `1970-01-01` 作为兜底日期。
- 图片仍然引用百度原始地址，导入 WordPress 后可能遇到防盗链问题。

图片防盗链的常见处理方式：

1. 在 WordPress 安装 `Auto Upload Images` 等图片搬运插件。
2. 导入或保存文章时，让插件自动把远程图片下载到自己的服务器。
3. 导入完成后检查图片是否已本地化。

## 常见问题

### 跑到一半中断怎么办？

重新运行脚本即可。脚本会读取 `baidu_articles_RAW.csv`，自动跳过已抓取的文章。

### 导入 WordPress 后文章时间会乱吗？

不会。生成最终文件前，脚本会按发布时间倒序排序，并将发布时间写入 WXR。

### 为什么有些图片导入后不显示？

通常是百度图片防盗链导致。建议使用 WordPress 图片搬运插件把远程图片保存到自己的服务器。

### 能不能重复运行？

可以。重复运行时会跳过 `baidu_articles_RAW.csv` 中已经存在的原始 URL。

### 生成的 XML 是什么格式？

`baidu_wordpress_import_FINAL.xml` 是 WordPress WXR 格式，可通过 WordPress 官方导入器导入。

### 为什么有些文章显示“无内容”？

通常是因为百度页面结构变化、正文容器未加载成功，或该文章页面本身异常。可以重新运行脚本，或针对新的页面结构调整正文选择器。

## 开发说明

这个脚本最早由 Grok 根据迁移需求生成初版，后来又用 Codex 根据实际运行问题进行了多轮更新，逐步合并抓取、清洗、排序、断点续传和 WXR 导出流程。

当前版本是单文件脚本，方便直接运行和备份。后续如果继续扩展，可以考虑拆分为：

- crawler
- cleaner
- exporter
- checkpoint
- WordPress WXR builder

## 更新时间

2026/05
