BaiduWenzhangExporter 百度空间迁移工具

功能:一键导出百度文章 → 生成 CSV + WXR → 清理冗余 → 可直接导入 WordPress

文件结构
BaiduWenzhangExporter/
-   baidu_to_wordpress_ULTRA.py          # 主迁移脚本（原始导出）
-   clean_baidu_export.py                # 清理脚本（推荐运行）
-   baidu_articles.csv                   # ← 原始导出（含冗余 HTML）
-   baidu_wordpress_import.xml           # ← 原始 WXR（可导入但建议清理）
-   baidu_articles_clean.csv             # ← 清理后 CSV
-   baidu_wordpress_import_clean.xml     # ← 最终版 WXR（推荐导入）

使用方法

第一步：准备环境
安装 Python（3.8+）
安装依赖: pip install selenium beautifulsoup4 lxml webdriver-manager

第二步：运行迁移脚本（导出原始数据）
python baidu_to_wordpress_ULTRA.py
操作流程：脚本会自动启动 Chrome 浏览器打开 https://wenzhang.baidu.com/
请手动登录百度账号
脚本自动进入 「我的文章」 页面后
在命令行按回车键 → 自动滚动加载 + 抓取所有文章
等待完成 → 生成：baidu_articles.csv
baidu_wordpress_import.xml

第三步：运行清理脚本
python clean_baidu_export.py
自动输出：baidu_articles_clean.csv（内容精简后的数据文件）
baidu_wordpress_import_clean.xml（可直接导入 WordPress）

常见问题
Q：导入后文章时间错乱？
A：使用 baidu_wordpress_import_clean.xml，时间已 100% 修复。
Q：文章内容有乱码或多余 div？
A：必须运行 clean_baidu_export.py 清理。
Q：想保留原始 HTML 结构？
A：直接用 baidu_articles.csv 手动处理。

2025/11/06


