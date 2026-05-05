import os
import time
import random
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

# ==================== 1. 全局配置 (Configuration) ===================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 原始数据（抓一篇存一篇，绝对安全）
RAW_CSV = os.path.join(SCRIPT_DIR, "baidu_articles_RAW.csv")
# 最终排序后的数据（用于备份）
SORTED_CSV = os.path.join(SCRIPT_DIR, "baidu_articles_SORTED.csv")
# 最终 WordPress 导入包 (WXR 格式)
WXR_FILE = os.path.join(SCRIPT_DIR, "baidu_wordpress_import_FINAL.xml")

# ==================== 2. 工具函数 (Helpers) ===================

def setup_driver():
    """配置浏览器参数"""
    options = Options()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    # eager 模式：只要 HTML 骨架加载完就行动，不干等图片加载，极大提速
    options.page_load_strategy = 'eager' 
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(30)
    driver.maximize_window()
    return driver

def clean_html_content(raw_html):
    """HTML 净化器：剥离百度冗余标签，保留纯净排版"""
    if not raw_html or "<div" not in raw_html: 
        return "<p>无内容</p>"
    soup = BeautifulSoup(raw_html, "html.parser")
    # 查找百度文章的主体容器
    content_div = soup.find("div", id=re.compile(r"detailArticleContent_")) or \
                  soup.find("div", class_=re.compile(r"pcs-article-content_"))
    
    if not content_div: return "<p>无内容</p>"
    
    parts = []
    for child in content_div.children:
        if child.name:
            # 过滤掉百度自带的空 div 和空 p
            if child.name in ["p", "div"] and not child.get_text(strip=True) and not child.find("img"):
                continue
            parts.append(str(child))
        elif isinstance(child, str) and child.strip():
            # 将纯文本中的换行符转换为 HTML 换行
            parts.append(child.replace("\n", "<br>"))
            
    content = "".join(parts)
    # 压缩冗余的连续换行
    content = re.sub(r"(<br>\s*){3,}", "<br><br>", content)
    return content.strip() or "<p>无内容</p>"

def get_done_urls():
    """读取已抓取的 URL 集合，支持断点续传"""
    done_urls = set()
    if os.path.exists(RAW_CSV):
        try:
            with open(RAW_CSV, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("原始URL"):
                        done_urls.add(row["原始URL"].strip())
        except: pass
    return done_urls

# ==================== 3. 核心抓取模块 (Crawler) ===================

def collect_all_urls(driver):
    """深度滚动列表页，确保文章全部入库"""
    print("🚀 正在扫描文章列表，执行深度滚动机制...")
    last_height = driver.execute_script("return document.body.scrollHeight")
    no_change_count = 0
    
    while no_change_count < 10: 
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        # 给予充足的加载时间
        time.sleep(1.8 + random.uniform(0.1, 0.4))
        
        new_height = driver.execute_script("return document.body.scrollHeight")
        units = driver.find_elements(By.CSS_SELECTOR, 'li.unit[id^="key_"]')
        print(f"  > 已发现文章: {len(units)} 篇...", end="\r")
        
        if new_height == last_height:
            no_change_count += 1
        else:
            no_change_count = 0
            last_height = new_height
            
    urls = []
    for u in units:
        key = u.get_attribute("id")[4:]
        prefix = "article" if "article" in u.get_attribute("class") else "page"
        urls.append(f"https://wenzhang.baidu.com/{prefix}/view?key={key}")
        
    print(f"\n✅ 列表扫描完成！共获取到 {len(urls)} 条链接。")
    return urls

def fetch_article(driver, url):
    """稳健抓取单篇文章：处理 Iframe、展开全文、懒加载图片"""
    try:
        driver.get(url)
        wait = WebDriverWait(driver, 15)
        # 1. 进入文章嵌套的 Iframe
        wait.until(EC.frame_to_be_available_and_switch_to_it((By.CSS_SELECTOR, "iframe.pcs-article-iframe")))
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        
        # 2. 点击“展开全文”按钮
        driver.execute_script("""
            let btns = document.querySelectorAll('*');
            for(let b of btns) { 
                let text = (b.innerText || '').trim();
                if(text === '阅读全文' || text === '展开全文' || text === '展开') { b.click(); break; } 
            }
        """)
        time.sleep(0.5) 
        
        # 3. 三段式模拟滚动，触发所有懒加载图片
        for p in [0.33, 0.66, 1.0]:
            driver.execute_script(f"window.scrollTo({{top: document.body.scrollHeight * {p}, behavior: 'smooth'}});")
            time.sleep(0.5)

        soup = BeautifulSoup(driver.page_source, "html.parser")
        driver.switch_to.default_content()

        # 解析标题
        title_el = soup.select_one("h1, .pcs-article-title_ptkaiapt4bxy_baiduscarticle")
        title = title_el.get_text(strip=True) if title_el else "无标题"
        
        # 解析发布日期
        date_str = "1970-01-01"
        time_el = soup.select_one(".time-cang")
        if time_el:
            m = re.search(r"(\d{4})[年\.\-](\d{1,2})[月\.\-](\d{1,2})", time_el.get_text())
            if m: date_str = f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"

        # 解析并清洗正文
        content_el = soup.select_one("#detailArticleContent_ptkaiapt4bxy_baiduscarticle, .pcs-article-content_ptkaiapt4bxy_baiduscarticle")
        clean_content = clean_html_content(str(content_el)) if content_el else "<p>无内容</p>"
        
        return {"title": title, "content": clean_content, "date": date_str, "url": url}
    except Exception as e:
        print(f"  [!] 抓取单篇出错: {str(e)[:50]}")
        try: driver.switch_to.default_content()
        except: pass
        return None

# ==================== 4. 数据后期处理 (Post-Processing) ===================

def build_final_package():
    """读取 RAW 数据，排序并生成最终的 CSV 和完美 WXR"""
    print("\n📦 正在进行数据组装（排序与 WXR 生成）...")
    if not os.path.exists(RAW_CSV): return

    articles = []
    with open(RAW_CSV, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("发布时间"): articles.append(row)

    if not articles:
        print("暂无抓取到的数据，退出封装。")
        return

    # 容错排序：最新发布的在前
    def safe_date(art):
        try: return datetime.strptime(art["发布时间"], "%Y-%m-%d")
        except: return datetime(1970, 1, 1)
    articles.sort(key=safe_date, reverse=True)

    # 1. 导出 SORTED CSV
    with open(SORTED_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["标题", "文章内容", "发布时间", "原始URL"])
        writer.writeheader()
        writer.writerows(articles)

    # 2. 导出完美 WXR (标准的 WordPress 导入协议)
    channel_pubDate = datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0800")
    xml_header = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
    xmlns:excerpt="http://wordpress.org/export/1.2/excerpt/"
    xmlns:content="http://purl.org/rss/1.0/modules/content/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:wp="http://wordpress.org/export/1.2/">
<channel>
    <title>百度文章迁移</title>
    <link>https://wenzhang.baidu.com</link>
    <description>BaiduWenzhang to WordPress Exporter</description>
    <pubDate>{channel_pubDate}</pubDate>
    <language>zh-CN</language>
    <wp:wxr_version>1.2</wp:wxr_version>
"""
    item_tpl = """    <item>
        <title><![CDATA[{title}]]></title>
        <pubDate>{pub_date}</pubDate>
        <dc:creator><![CDATA[admin]]></dc:creator>
        <content:encoded><![CDATA[{content}]]></content:encoded>
        <wp:post_date><![CDATA[{raw_date} 00:00:00]]></wp:post_date>
        <wp:post_name><![CDATA[{slug}]]></wp:post_name>
        <wp:status><![CDATA[publish]]></wp:status>
        <wp:post_type><![CDATA[post]]></wp:post_type>
        <category domain="category" nicename="baidu-wenzhang"><![CDATA[百度文章]]></category>
        <wp:postmeta><wp:meta_key><![CDATA[_original_url]]></wp:meta_key><wp:meta_value><![CDATA[{url}]]></wp:meta_value></wp:postmeta>
    </item>
"""
    
    with open(WXR_FILE, "w", encoding="utf-8") as f:
        f.write(xml_header)
        for i, art in enumerate(articles):
            dt = safe_date(art)
            slug_raw = re.sub(r"[^\w\s-]", "", art["标题"]).strip()
            slug = re.sub(r"\s+", "-", slug_raw.lower())[:50] or f"post-{i}"
            
            f.write(item_tpl.format(
                title=art["标题"].replace("]]>", "]]&gt;"),
                pub_date=dt.strftime("%a, %d %b %Y 00:00:00 +0800"),
                content=art["文章内容"].replace("]]>", "]]&gt;"),
                raw_date=art["发布时间"],
                slug=slug,
                url=art["原始URL"]
            ))
        f.write("</channel>\n</rss>")

    print(f"🎉 任务圆满成功！\n 1. 干净数据: {SORTED_CSV}\n 2. 完美导入包: {WXR_FILE}")

# ==================== 5. 主程序入口 (Main) ===================

if __name__ == "__main__":
    # 初始化文件头
    if not os.path.exists(RAW_CSV):
        with open(RAW_CSV, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerow(["标题", "文章内容", "发布时间", "原始URL"])

    driver = setup_driver()
    try:
        driver.get("https://wenzhang.baidu.com/")
        print("="*60 + "\n  BaiduWenzhang to WordPress Exporter - Ultimate Release\n" + "="*60)
        input("👉 请先在浏览器中登录百度，进入「我的文章」列表后，回到这里按回车...")
        
        all_urls = collect_all_urls(driver)
        done_urls = get_done_urls()
        todo_urls = [u for u in all_urls if u not in done_urls]
        
        print(f"📊 状态：总计 {len(all_urls)} 篇，已抓取 {len(done_urls)} 篇，待抓取 {len(todo_urls)} 篇。")

        if todo_urls:
            with open(RAW_CSV, "a", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                for i, url in enumerate(todo_urls):
                    art = fetch_article(driver, url)
                    if art:
                        writer.writerow([art["title"], art["content"], art["date"], art["url"]])
                        f.flush() # 实时落盘，绝对安全
                        print(f"[{len(done_urls)+i+1}/{len(all_urls)}] 成功: {art['title'][:15]}")
                    else:
                        print(f"[{len(done_urls)+i+1}/{len(all_urls)}] ⏭️ 跳过失败链接")
                    
                    # 随机小停顿，模拟人类操作
                    time.sleep(random.uniform(0.5, 1.2))
        else:
            print("✨ 所有文章已抓取完毕，无需重复抓取。")
            
    finally:
        driver.quit()
        
    # 无论抓取是否完整，最后都运行一次打包程序
    build_final_package()
