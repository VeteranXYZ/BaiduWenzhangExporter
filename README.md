BaiduWenzhangExporter 百度文章迁移工具 (终极版)
功能: 一键将百度文章（原百度空间）完整迁移至 WordPress。本版本已实现“合二为一”，支持断点续传、内存级净化、自动时间排序，可直接输出 100% 兼容 WordPress 的完美 WXR 文件。

📁 文件结构
Plaintext
BaiduWenzhangExporter/
-   BaiduExporter.py                      # 终极主程序（抓取/清洗/排序/打包 一体化）
-   baidu_articles_RAW.csv                # ← 实时抓取备份（断点续传的依据，防断电丢失）
-   baidu_articles_SORTED.csv             # ← 最终清洗且按发布时间排好序的 CSV 备份
-   baidu_wordpress_import_FINAL.xml      # ← 最终完美版 WXR（直接拿去导入 WordPress）
-   
🚀 使用方法
第一步：准备环境
确保已安装 Python（3.8+）和 Google Chrome 浏览器。
安装必备依赖库:
Bash
pip install selenium beautifulsoup4 webdriver-manager
第二步：运行迁移程序
在终端（Terminal）或命令行中运行脚本：
Bash
python BaiduExporter.py
第三步：操作流程
脚本会自动启动 Chrome 浏览器，并打开百度文章首页。
请手动扫码或输入密码登录百度账号。
登录成功并进入「我的文章」列表页面后。
回到运行代码的黑色控制台（终端），按下回车键 (Enter)。
全自动执行：脚本将自动进行深度滚动、破解隐藏折叠、破解懒加载图片、清洗冗余 HTML，并实时安全落盘。
等待完成，自动输出排好序的 .csv 和 .xml 导入包。
第四步：导入 WordPress
进入你的 WordPress 后台。
点击左侧菜单的 工具 (Tools) → 导入 (Import)。
找到 WordPress 选项，点击“运行导入器”。
上传刚刚生成的 baidu_wordpress_import_FINAL.xml，分配作者并提交即可！

💡 核心升级亮点
断点续传机制：采用实时追加写入技术，哪怕中途断网、死机、或者手动 Ctrl+C 停止，再次运行程序也会自动跳过已抓取的文章，从断点继续！
杜绝乱码与丢失排版：放弃了容易产生 Bug 的传统 XML 库，底层使用自研字符串模板强力保护 <![CDATA[...]]>，确保 WordPress 100% 还原原始段落排版。
SEO 友好封装：自动分配“百度文章”分类，保留原百度文章 URL 作为自定义字段（Meta），并自动生成合法的英文缩写链接 (Slug)。

❓ 常见问题
Q：跑到一半电脑卡死，或者不小心关掉了程序怎么办？
A：完全不用慌。直接重新运行 python BaiduExporter.py，程序会自动识别目录下的 RAW.csv 记录，直接从你断掉的那一篇接着往下抓。
Q：导入 WordPress 后，文章的时间会错乱吗？
A：绝对不会。程序在生成最终文件前，会自动在内存中对所有文章进行严格的发布时间倒序排列。
Q：为什么导入后，部分图片显示不出来或者带有“防盗链”图标？
A：这是因为百度服务器对图片开启了防盗链拦截。解决办法很简单：在 WordPress 后台安装一款名为 "Auto Upload Images" 的免费插件，再导入或保存文章时，它会自动帮你把百度的图片下载存到你自己的服务器上。

Updated: 2026/05
