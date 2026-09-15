"""派生格式导出。

优先级：RAW HTML > content.local.html > Markdown > TXT。
Markdown/TXT 转换失败绝不阻塞归档 —— 原始数据已经安全落盘。
"""

from __future__ import annotations

import html as html_module
import re
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .parsers import make_soup
from .state import ArchiveState
from .storage import read_json, read_text, write_text_atomic
from .utils import readable_dirname, sanitize_filename, utcnow_iso

Logger = Callable[[str, str], None]


def _noop(tag: str, message: str) -> None:  # pragma: no cover
    print(f"[{tag}] {message}")


def html_to_markdown(content_html: str) -> str:
    """HTML → Markdown。markdownify 不可用时退化为纯文本，绝不抛错。"""
    if not content_html:
        return ""
    try:
        from markdownify import markdownify

        text = markdownify(content_html, heading_style="ATX", bullets="-")
    except Exception:  # noqa: BLE001 —— 派生格式失败不应影响归档
        text = html_to_text(content_html)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def html_to_text(content_html: str) -> str:
    """HTML → 纯文本，用于全文搜索。保留段落换行。"""
    if not content_html:
        return ""
    soup = make_soup(content_html)
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "tr"]):
        block.append("\n")
    text = soup.get_text()
    text = html_module.unescape(text)
    text = text.replace("\xa0", " ")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def run_export(
    config: Config,
    state: ArchiveState,
    *,
    formats: list[str],
    log: Logger = _noop,
    site_dir: Path | None = None,
) -> dict[str, Any]:
    config.ensure_dirs()
    counters = {"md": 0, "txt": 0, "wxr": 0, "site": 0}
    records = state.iter_records("page")

    if "md" in formats or "txt" in formats:
        for record in records:
            key = record["key"]
            item_dir = config.item_dir(key)
            source = item_dir / "content.local.html"
            if not source.exists():
                source = item_dir / "content.original.html"
            if not source.exists():
                continue
            content_html = read_text(source) or ""
            if "md" in formats:
                write_text_atomic(item_dir / "content.md", html_to_markdown(content_html) + "\n")
                counters["md"] += 1
            if "txt" in formats:
                write_text_atomic(item_dir / "content.txt", html_to_text(content_html) + "\n")
                counters["txt"] += 1
        log("EXPORT", f"Markdown {counters['md']} 篇，TXT {counters['txt']} 篇")

    if "wxr" in formats:
        path = export_wxr(config, state)
        counters["wxr"] = 1
        log("EXPORT", f"WordPress WXR → {path}")

    if "site" in formats:
        target = site_dir or Path("site")
        count = export_site(config, state, target)
        counters["site"] = count
        log("EXPORT", f"静态站点 {count} 篇 → {target}/index.html")

    return counters


# --------------------------------------------------------------------------
# WordPress WXR
# --------------------------------------------------------------------------

def _cdata(text: str) -> str:
    """CDATA 内不能出现 ``]]>``，标准做法是拆成两段。"""
    return (text or "").replace("]]>", "]]]]><![CDATA[>")


def export_wxr(config: Config, state: ArchiveState, filename: str = "wordpress_import.xml") -> Path:
    """生成 WordPress WXR。移植自仓库原有 BaiduExporter.py 的模板并修正 CDATA 转义。"""
    from datetime import datetime

    records = state.iter_records("page")
    entries = []
    for record in records:
        key = record["key"]
        item_dir = config.item_dir(key)
        metadata = read_json(item_dir / "metadata.json", {}) or {}
        source = item_dir / "content.local.html"
        if not source.exists():
            source = item_dir / "content.original.html"
        if not source.exists():
            continue
        date = metadata.get("content_date") or metadata.get("list_date") or record.get("list_date")
        entries.append(
            {
                "key": key,
                "title": metadata.get("title") or record.get("title") or "无标题",
                "content": read_text(source) or "",
                "date": date,
                "url": record.get("detail_url") or "",
                "tags": metadata.get("tags") or [],
            }
        )

    def sort_key(entry: dict) -> str:
        return entry["date"] or "0000-00-00"

    entries.sort(key=sort_key, reverse=True)
    now = datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0800")

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"\n'
        '    xmlns:excerpt="http://wordpress.org/export/1.2/excerpt/"\n'
        '    xmlns:content="http://purl.org/rss/1.0/modules/content/"\n'
        '    xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
        '    xmlns:wp="http://wordpress.org/export/1.2/">\n'
        "<channel>\n"
        "    <title>百度空间归档</title>\n"
        f"    <link>{config.site_root}</link>\n"
        "    <description>Baidu Space local archive</description>\n"
        f"    <pubDate>{now}</pubDate>\n"
        "    <language>zh-CN</language>\n"
        "    <wp:wxr_version>1.2</wp:wxr_version>\n"
    ]
    for index, entry in enumerate(entries):
        date = entry["date"] or "1970-01-01"
        try:
            parsed_date = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            parsed_date = datetime(1970, 1, 1)
            date = "1970-01-01"
        slug_raw = re.sub(r"[^\w\s-]", "", entry["title"]).strip()
        slug = re.sub(r"\s+", "-", slug_raw.lower())[:50] or f"post-{index}"
        tag_xml = "".join(
            f'\n        <category domain="post_tag" nicename="{sanitize_filename(t, max_length=30)}">'
            f"<![CDATA[{_cdata(t)}]]></category>"
            for t in entry["tags"]
        )
        parts.append(
            "    <item>\n"
            f"        <title><![CDATA[{_cdata(entry['title'])}]]></title>\n"
            f"        <pubDate>{parsed_date.strftime('%a, %d %b %Y 00:00:00 +0800')}</pubDate>\n"
            "        <dc:creator><![CDATA[admin]]></dc:creator>\n"
            f"        <content:encoded><![CDATA[{_cdata(entry['content'])}]]></content:encoded>\n"
            f"        <wp:post_date><![CDATA[{date} 00:00:00]]></wp:post_date>\n"
            f"        <wp:post_name><![CDATA[{slug}]]></wp:post_name>\n"
            "        <wp:status><![CDATA[publish]]></wp:status>\n"
            "        <wp:post_type><![CDATA[post]]></wp:post_type>\n"
            '        <category domain="category" nicename="baidu-space">'
            "<![CDATA[百度空间]]></category>"
            f"{tag_xml}\n"
            "        <wp:postmeta><wp:meta_key><![CDATA[_original_url]]></wp:meta_key>"
            f"<wp:meta_value><![CDATA[{_cdata(entry['url'])}]]></wp:meta_value></wp:postmeta>\n"
            "        <wp:postmeta><wp:meta_key><![CDATA[_archive_key]]></wp:meta_key>"
            f"<wp:meta_value><![CDATA[{entry['key']}]]></wp:meta_value></wp:postmeta>\n"
            "    </item>\n"
        )
    parts.append("</channel>\n</rss>\n")

    path = config.root / filename
    write_text_atomic(path, "".join(parts))
    return path


# --------------------------------------------------------------------------
# 静态站点（百度关闭后仍可本地阅读）
# --------------------------------------------------------------------------

_SITE_CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#e5e5e5;--accent:#2b6cb0}
@media(prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#e8e8e8;--muted:#9aa0a6;--line:#2c2f34;--accent:#7cb3ec}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.75 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:32px 16px 80px}
h1{font-size:1.6rem;margin:0 0 4px}
.sub{color:var(--muted);font-size:.9rem;margin-bottom:24px}
input[type=search]{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--fg);font-size:1rem;margin-bottom:20px}
.year{margin:28px 0 8px;font-size:1.05rem;color:var(--muted);border-bottom:1px solid var(--line);padding-bottom:6px}
ul{list-style:none;padding:0;margin:0}
li.post{padding:8px 0;border-bottom:1px solid var(--line)}
li.post a{color:var(--accent);text-decoration:none}
li.post a:hover{text-decoration:underline}
.date{color:var(--muted);font-size:.85rem;margin-right:10px;font-variant-numeric:tabular-nums}
article img{max-width:100%;height:auto}
.meta{color:var(--muted);font-size:.85rem;margin-bottom:24px}
.back{display:inline-block;margin-bottom:20px;color:var(--accent);text-decoration:none}
.warn{background:#fff4e5;border-left:3px solid #f0a020;padding:8px 12px;margin:16px 0;font-size:.9rem;color:#7a4a00}
@media(prefers-color-scheme:dark){.warn{background:#2a2113;color:#e0b070}}
"""

_ARTICLE_TPL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><link rel="stylesheet" href="../style.css"></head>
<body><div class="wrap">
<a class="back" href="../index.html">← 返回目录</a>
<h1>{title}</h1>
<div class="meta">{meta}</div>
{warning}
<article>{content}</article>
</div></body></html>
"""


def export_site(config: Config, state: ArchiveState, site_dir: Path) -> int:
    """生成可离线浏览的静态站点：按年份分组、标题搜索、本地图片。"""
    site_dir = Path(site_dir)
    articles_dir = site_dir / "articles"
    articles_dir.mkdir(parents=True, exist_ok=True)
    write_text_atomic(site_dir / "style.css", _SITE_CSS)

    records = state.iter_records("page")
    entries = []
    for record in records:
        key = record["key"]
        item_dir = config.item_dir(key)
        source = item_dir / "content.local.html"
        if not source.exists():
            source = item_dir / "content.original.html"
        if not source.exists():
            continue
        metadata = read_json(item_dir / "metadata.json", {}) or {}
        title = metadata.get("title") or record.get("title") or "无标题"
        date = metadata.get("content_date") or metadata.get("list_date") or ""
        slug = readable_dirname(date, title, key)
        target_dir = articles_dir / slug
        target_dir.mkdir(parents=True, exist_ok=True)

        content_html = read_text(source) or ""
        # 站点里的图片指向 items/<key>/assets/，用相对路径穿回去
        depth_prefix = "../../../"
        rel_assets = f"{depth_prefix}{config.items_dir.name}/{item_dir.name}/assets/"
        content_html = content_html.replace('src="assets/', f'src="{rel_assets}')
        content_html = content_html.replace("src='assets/", f"src='{rel_assets}")

        warnings = metadata.get("warnings") or []
        warning_html = ""
        if warnings:
            warning_html = '<div class="warn">归档提示：' + html_module.escape("；".join(warnings)) + "</div>"

        meta_bits = []
        if date:
            meta_bits.append(f"收藏于 {html_module.escape(date)}")
        if metadata.get("source"):
            meta_bits.append(html_module.escape(str(metadata["source"])))
        if metadata.get("tags"):
            meta_bits.append("标签：" + html_module.escape("、".join(metadata["tags"])))
        meta_bits.append(f"key {html_module.escape(key)}")

        write_text_atomic(
            target_dir / "index.html",
            _ARTICLE_TPL.format(
                title=html_module.escape(title),
                meta=" · ".join(meta_bits),
                warning=warning_html,
                content=content_html,
            ),
        )
        entries.append({"title": title, "date": date, "href": f"articles/{slug}/index.html"})

    entries.sort(key=lambda e: e["date"] or "0000-00-00", reverse=True)
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        year = (entry["date"] or "")[:4] or "未知年份"
        groups.setdefault(year, []).append(entry)

    body = []
    for year in sorted(groups, reverse=True):
        body.append(f'<div class="year">{html_module.escape(year)}（{len(groups[year])}）</div><ul>')
        for entry in groups[year]:
            body.append(
                f'<li class="post"><span class="date">{html_module.escape(entry["date"] or "—")}</span>'
                f'<a href="{html_module.escape(entry["href"])}">{html_module.escape(entry["title"])}</a></li>'
            )
        body.append("</ul>")

    index_html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>百度空间归档</title><link rel="stylesheet" href="style.css"></head>
<body><div class="wrap">
<h1>百度空间归档</h1>
<div class="sub">共 {len(entries)} 篇 · 生成于 {utcnow_iso()}</div>
<input type="search" id="q" placeholder="搜索标题…" autocomplete="off">
<div id="list">{''.join(body)}</div>
<script>
const q=document.getElementById('q');
q.addEventListener('input',()=>{{
  const term=q.value.trim().toLowerCase();
  document.querySelectorAll('li.post').forEach(li=>{{
    li.style.display = !term || li.textContent.toLowerCase().includes(term) ? '' : 'none';
  }});
  document.querySelectorAll('.year').forEach(h=>{{
    let n=h.nextElementSibling, any=false;
    if(n) n.querySelectorAll('li.post').forEach(li=>{{ if(li.style.display!=='none') any=true; }});
    h.style.display = any ? '' : 'none';
    if(n) n.style.display = any ? '' : 'none';
  }});
}});
</script>
</div></body></html>
"""
    write_text_atomic(site_dir / "index.html", index_html)
    return len(entries)
