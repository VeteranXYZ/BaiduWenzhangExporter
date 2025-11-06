# clean_baidu_export.py
# 终极完美版：完全符合 WordPress 官方 WXR 规范

import csv
import re
import os
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from xml.dom import minidom
from datetime import datetime

# ==================== 文件路径（当前目录）===================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

CSV_IN = os.path.join(SCRIPT_DIR, "baidu_articles.csv")
CSV_OUT = os.path.join(SCRIPT_DIR, "baidu_articles_clean.csv")

WXR_IN = os.path.join(SCRIPT_DIR, "baidu_wordpress_import.xml")
WXR_OUT = os.path.join(SCRIPT_DIR, "baidu_wordpress_import_clean.xml")
# =======================================================


def clean_html_content(raw_html):
    if not raw_html or "<div" not in raw_html:
        return "<p>无内容</p>"

    soup = BeautifulSoup(raw_html, "html.parser")
    content_div = soup.find("div", id=re.compile(r"detailArticleContent_"))
    if not content_div:
        outer = soup.find("div", class_=re.compile(r"pcs-article-content_"))
        return str(outer.contents[0]) if outer and outer.contents else "<p>无内容</p>"

    parts = []
    for child in content_div.children:
        if child.name:
            if child.name in ["p", "div"] and not child.get_text(strip=True):
                continue
            parts.append(str(child))
        elif isinstance(child, str) and child.strip():
            parts.append(child.replace("\n", "<br>"))
        elif child == "\n":
            parts.append("<br>")

    content = "".join(parts)
    content = re.sub(r"(<br>\s*){3,}", "<br><br>", content)
    content = re.sub(r"<p>\s*</p>|<div>\s*</div>", "", content)
    return content.strip() or "<p>无内容</p>"


def clean_csv():
    if not os.path.exists(CSV_IN):
        print(f"未找到 {CSV_IN}，跳过 CSV 清理")
        return False

    data = []
    with open(CSV_IN, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return False

        for row in reader:
            if len(row) < 4:
                continue
            title, html, date_str, url = row[:4]
            clean_content = clean_html_content(html)
            data.append((title.strip(), clean_content, date_str.strip(), url.strip()))

    data.sort(key=lambda x: x[2], reverse=True)

    with open(CSV_OUT, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["标题", "文章内容", "发布时间", "原始URL"])
        for title, content, date_str, url in data:
            writer.writerow([title, content, date_str, url])

    print(f"CSV 清理完成 → {CSV_OUT}（{len(data)} 篇）")
    return True


def clean_wxr():
    if not os.path.exists(WXR_IN):
        print(f"未找到 {WXR_IN}，跳过 WXR 清理")
        return False

    tree = ET.parse(WXR_IN)
    root = tree.getroot()

    # 注册命名空间
    nsmap = {
        "content": "http://purl.org/rss/1.0/modules/content/",
        "excerpt": "http://wordpress.org/export/1.2/excerpt/",
        "wfw": "http://wellformedweb.org/CommentAPI/",
        "dc": "http://purl.org/dc/elements/1.1/",
        "wp": "http://wordpress.org/export/1.2/",
    }
    for prefix, uri in nsmap.items():
        ET.register_namespace(prefix, uri)

    items = root.findall(".//item")
    cleaned_count = 0

    for item in items:
        # === 1. 提取日期（从 wp:post_date）===
        post_date_el = item.find("{http://wordpress.org/export/1.2/}post_date")
        date_str = "1970-01-01 00:00:00"
        if post_date_el is not None and post_date_el.text:
            try:
                dt = datetime.strptime(post_date_el.text.strip(), "%Y-%m-%d %H:%M:%S")
                date_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            except:
                pass
        else:
            post_date_el = ET.SubElement(item, "wp:post_date")
            post_date_el.text = date_str

        # === 2. 同步 wp:post_date_gmt ===
        post_date_gmt = item.find("{http://wordpress.org/export/1.2/}post_date_gmt")
        if post_date_gmt is None:
            post_date_gmt = ET.SubElement(item, "wp:post_date_gmt")
        post_date_gmt.text = date_str

        # === 3. 设置 pubDate 为 RFC 2822（WordPress 标准）===
        pubdate_el = item.find("pubDate")
        if pubdate_el is None:
            pubdate_el = ET.SubElement(item, "pubDate")
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
            pubdate_el.text = dt.strftime("%a, %d %b %Y %H:%M:%S +0800")
        except:
            pubdate_el.text = "Thu, 01 Jan 1970 00:00:00 +0800"

        # === 4. 清理内容 ===
        encoded = item.find("{http://purl.org/rss/1.0/modules/content/}encoded")
        if encoded is not None and encoded.text and "<![CDATA[" in encoded.text:
            raw = encoded.text[9:-3]
            cleaned = clean_html_content(raw)
            encoded.text = f"<![CDATA[{cleaned}]]>"

        # === 5. 清理摘要 ===
        excerpt = item.find("{http://wordpress.org/export/1.2/excerpt/}encoded")
        if excerpt is not None:
            excerpt.text = "<![CDATA[]]>"

        # === 6. 修复 post_name（英文 slug）===
        post_name = item.find("{http://wordpress.org/export/1.2/}post_name")
        title_el = item.find("title")
        if post_name is not None and title_el is not None and title_el.text:
            title = re.sub(r"[^\w\s-]", "", title_el.text).strip()
            slug = re.sub(r"\s+", "-", title.lower())[:50]
            post_name.text = slug or "post"

        cleaned_count += 1

    # === 输出：UTF-8 + 正确声明 ===
    rough = ET.tostring(root, "utf-8")
    pretty = minidom.parseString(rough).toprettyxml(indent="  ", encoding="utf-8")
    final = b"\n".join([line for line in pretty.splitlines() if line.strip()])

    with open(WXR_OUT, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write(final)

    print(f"WXR 清理完成 → {WXR_OUT}（{cleaned_count} 篇已优化）")
    return True


# ===================== 主程序 =====================
if __name__ == "__main__":
    print("=" * 60)
    print("     百度文章导出文件清理工具")
    print("     自动处理当前目录下的：")
    print("     • baidu_articles.csv")
    print("     • baidu_wordpress_import.xml")
    print("=" * 60)

    csv_done = clean_csv()
    wxr_done = clean_wxr()

    if not csv_done and not wxr_done:
        print(
            "未找到任何文件，请确保 baidu_articles.csv 或 baidu_wordpress_import.xml 存在于当前目录"
        )
    else:
        print("\n" + "=" * 60)
        print("  清理完成！")
        if csv_done:
            print(f"  → 清理后 CSV：{CSV_OUT}")
        if wxr_done:
            print(f"  → 清理后 WXR：{WXR_OUT}")
            print("  → 可直接导入 WordPress")
        print("=" * 60)
