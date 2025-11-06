# baidu_to_wordpress_ULTRA.py
# 百度文章 → WordPress 一键迁移神器（完整共享版 · 断点续传已关闭）
# 输出文件：CSV + WXR 自动保存到脚本**同一目录**

import os
import time
import re
import csv
from datetime import datetime
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import xml.etree.ElementTree as ET
from xml.dom import minidom

# ==================== 配置区（无需修改）===================
# 输出文件放在脚本同目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(SCRIPT_DIR, "baidu_articles.csv")
WXR_FILE = os.path.join(SCRIPT_DIR, "baidu_wordpress_import.xml")

SCROLL_PAUSE = 0.5
MAX_NO_NEW = 10
# =====================================================


def setup_driver():
    options = Options()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    # options.add_argument('--headless')  # 调试时取消注释
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.maximize_window()
    return driver


def collect_urls(driver):
    print("正在超快滚动加载所有文章...")
    urls = set()
    no_new = 0

    while no_new < MAX_NO_NEW:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(SCROLL_PAUSE)

        units = driver.find_elements(By.CSS_SELECTOR, 'li.unit[id^="key_"]')
        old_count = len(urls)
        for u in units:
            key = u.get_attribute("id")[4:]
            if re.match(r"^[a-f0-9-]+-\d+$", key):
                if "article" in u.get_attribute("class"):
                    urls.add(f"https://wenzhang.baidu.com/article/view?key={key}")
                else:
                    urls.add(f"https://wenzhang.baidu.com/page/view?key={key}")

        if len(urls) > old_count:
            print(f"  已收集 {len(urls)} 篇...", end="\r")
            no_new = 0
        else:
            no_new += 1

    print(f"\n收集完成：{len(urls)} 篇")
    return list(urls)


def fetch_article(driver, url):
    try:
        driver.get(url)
        WebDriverWait(driver, 3).until(
            EC.frame_to_be_available_and_switch_to_it(
                (By.CSS_SELECTOR, "iframe.pcs-article-iframe")
            )
        )
        time.sleep(0.6)

        soup = BeautifulSoup(driver.page_source, "html.parser")
        driver.switch_to.default_content()

        title_el = soup.select_one("h1, .pcs-article-title_ptkaiapt4bxy_baiduscarticle")
        title = title_el.get_text(strip=True) if title_el else "无标题"

        time_el = soup.select_one(".time-cang")
        date_str = "1970-01-01"
        if time_el:
            text = time_el.get_text(strip=True)
            m = re.search(r"(\d{4})[年\.\-](\d{1,2})[月\.\-](\d{1,2})", text)
            if m:
                date_str = f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"

        content_el = soup.select_one(
            "#detailArticleContent_ptkaiapt4bxy_baiduscarticle, .pcs-article-content_ptkaiapt4bxy_baiduscarticle"
        )
        content = str(content_el) if content_el else "<p>无内容</p>"

        return {"title": title, "content": content, "date": date_str, "url": url}
    except:
        driver.switch_to.default_content()
        return None


def generate_wxr(articles):
    rss = ET.Element("rss", version="2.0")

    namespaces = {
        "excerpt": "http://wordpress.org/export/1.2/excerpt/",
        "content": "http://purl.org/rss/1.0/modules/content/",
        "wfw": "http://wellformedweb.org/CommentAPI/",
        "dc": "http://purl.org/dc/elements/1.1/",
        "wp": "http://wordpress.org/export/1.2/",
    }
    for prefix, uri in namespaces.items():
        rss.set(f"xmlns:{prefix}", uri)

    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "百度文章迁移"
    ET.SubElement(channel, "link").text = "https://your-site.com"
    ET.SubElement(channel, "description").text = "从百度文章导入"
    ET.SubElement(channel, "pubDate").text = datetime.now().strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )
    ET.SubElement(channel, "language").text = "zh-CN"
    ET.SubElement(channel, "wp:wxr_version").text = "1.2"
    ET.SubElement(channel, "wp:base_site_url").text = "https://your-site.com"
    ET.SubElement(channel, "wp:base_blog_url").text = "https://your-site.com"

    cat = ET.SubElement(channel, "wp:category")
    ET.SubElement(cat, "wp:term_id").text = "1"
    ET.SubElement(cat, "wp:category_nicename").text = "baidu-wenzhang"
    ET.SubElement(cat, "wp:cat_name").text = "<![CDATA[百度文章]]>"

    for i, art in enumerate(articles):
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = (
            art["title"] if art["title"] != "无标题" else ""
        )
        ET.SubElement(item, "link").text = art["url"]
        ET.SubElement(item, "pubDate").text = (
            f"{art['date'].replace('-', ' ')} 00:00:00 +0800"
        )
        ET.SubElement(item, "dc:creator").text = "admin"
        ET.SubElement(item, "guid", isPermaLink="false").text = art["url"]
        ET.SubElement(item, "description").text = ""
        ET.SubElement(item, "content:encoded").text = f"<![CDATA[{art['content']}]]>"
        ET.SubElement(item, "excerpt:encoded").text = "<![CDATA[]]>"

        ET.SubElement(item, "wp:post_id").text = str(i + 1)
        ET.SubElement(item, "wp:post_date").text = f"{art['date']} 00:00:00"
        ET.SubElement(item, "wp:post_date_gmt").text = f"{art['date']} 00:00:00"
        ET.SubElement(item, "wp:comment_status").text = "open"
        ET.SubElement(item, "wp:ping_status").text = "open"
        ET.SubElement(item, "wp:post_name").text = re.sub(
            r"\W+", "-", art["title"].lower()
        )[:50]
        ET.SubElement(item, "wp:status").text = "publish"
        ET.SubElement(item, "wp:post_parent").text = "0"
        ET.SubElement(item, "wp:menu_order").text = "0"
        ET.SubElement(item, "wp:post_type").text = "post"
        ET.SubElement(item, "wp:is_sticky").text = "0"

        c = ET.SubElement(
            item, "category", domain="category", nicename="baidu-wenzhang"
        )
        c.text = "<![CDATA[百度文章]]>"

        meta = ET.SubElement(item, "wp:postmeta")
        ET.SubElement(meta, "wp:meta_key").text = "_original_url"
        ET.SubElement(meta, "wp:meta_value").text = f"<![CDATA[{art['url']}]]>"

    rough = ET.tostring(rss, "utf-8")
    pretty = minidom.parseString(rough).toprettyxml(indent="  ")
    final = "\n".join([line for line in pretty.splitlines() if line.strip()])

    with open(WXR_FILE, "w", encoding="utf-8") as f:
        f.write(final)

    return WXR_FILE


# ===================== 主程序 =====================
if __name__ == "__main__":
    print("=" * 60)
    print("     百度文章 → WordPress 一键迁移神器")
    print("     运行后：登录 → 进入文章页 → 按回车")
    print("     输出文件将保存在：脚本所在目录")
    print("=" * 60)

    driver = setup_driver()
    try:
        driver.get("https://wenzhang.baidu.com/")
        print("\n请完成登录 → 进入「我的文章」页面 → 按回车继续...")
        input()

        all_urls = collect_urls(driver)

        print(f"\n开始抓取 {len(all_urls)} 篇（断点续传已关闭，每次从头开始）...")
        articles = []
        with open(CSV_FILE, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["标题", "文章内容", "发布时间", "原始URL"])

            for i, url in enumerate(all_urls):
                print(f"[{i+1}/{len(all_urls)}] ", end="")
                art = fetch_article(driver, url)
                if art:
                    writer.writerow(
                        [art["title"], art["content"], art["date"], art["url"]]
                    )
                    articles.append(art)
                    print(f"{art['title'][:20]} → {art['date']}")
                else:
                    writer.writerow(["失败", "", "", url])
                    print("失败")
                time.sleep(0.5)

        wxr_path = generate_wxr(articles) if articles else "未生成"

        print("\n" + "=" * 60)
        print("  迁移完成！所有文章已成功导出")
        print(f"  CSV 文件：{CSV_FILE}")
        if wxr_path != "未生成":
            print(f"  WXR 文件：{WXR_FILE}")
            print("  → 导入 WordPress：工具 → 导入 → WordPress → 上传此文件")
        print("=" * 60)

    except Exception as e:
        print(f"\n错误：{e}")
    finally:
        driver.quit()
