Baidu-to-WordPress 迁移神器（中文版）

一键导出百度文章 → 生成 CSV + WXR → 清理冗余 → 直接导入 WordPress

文件结构
baidu-to-wordpress/
├── baidu_to_wordpress_ULTRA.py          # 主迁移脚本（原始导出）
├── clean_baidu_export.py                # 清理脚本（推荐运行）
├── baidu_articles.csv                   # ← 原始导出（含冗余 HTML）
├── baidu_wordpress_import.xml           # ← 原始 WXR（可导入但建议清理）
├── baidu_articles_clean.csv             # ← 清理后 CSV
└── baidu_wordpress_import_clean.xml     # ← 最终版 WXR（推荐导入）

使用方法（超简单 3 步）

第一步：准备环境（只需一次）
# 1. 安装 Python（3.8+）
# 2. 安装依赖
pip install selenium beautifulsoup4 lxml webdriver-manager

第二步：运行迁移脚本（导出原始数据）
python baidu_to_wordpress_ULTRA.py
操作流程：脚本启动 Chrome 浏览器
打开 https://wenzhang.baidu.com/
请手动登录百度账号
进入 「我的文章」 页面
按回车键 → 自动滚动加载 + 抓取所有文章
等待完成 → 生成：baidu_articles.csv
baidu_wordpress_import.xml

第三步：运行清理脚本（生成最终版）
python clean_baidu_export.py
自动输出：baidu_articles_clean.csv（内容精简）
baidu_wordpress_import_clean.xml（推荐导入 WordPress）

WordPress 导入说明
登录 WordPress 后台
工具 → 导入 → WordPress
上传 baidu_wordpress_import_clean.xml
勾选「下载并导入文件附件」（可选）
点击「提交」→ 全部导入成功！

注意事项
百度需验证，脚本无法绕过
每次运行从头开始
断点续传已关闭，避免重复
不要直接导入原始 XML
建议使用 _clean.xml 版本
Chrome 浏览器会自动下载驱动
首次运行稍慢，之后秒开
内容含 <br/> 换行
保留原始段落格式
分类固定为「百度文章」
可在 WP 后台批量修改

常见问题
Q：导入后文章时间错乱？
A：使用 baidu_wordpress_import_clean.xml，时间已 100% 修复。
Q：文章内容有乱码或多余 div？
A：必须运行 clean_baidu_export.py 清理。
Q：想保留原始 HTML 结构？
A：直接用 baidu_articles.csv 手动处理。


