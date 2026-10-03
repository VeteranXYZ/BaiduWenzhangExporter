"""独立可搬运的离线阅读界面；仅生成派生文件，不改动归档原件。"""

from __future__ import annotations

import html
import json
from pathlib import Path
from urllib.parse import quote, urlparse

from .config import Config
from .parsers import make_soup
from .state import ArchiveState
from .truth import validated_reading_content, validate_downloaded_asset
from .storage import current_content_path, read_json, read_text, write_atomic, write_text_atomic
from .utils import safe_key_dirname, utcnow_iso


def reading_html(content: str) -> str:
    """阅读副本禁用旧网页脚本和表单。raw / content.original.html 保持原样。"""
    soup = make_soup(content)
    for node in soup.find_all(["script", "iframe", "object", "embed", "base", "link", "meta"]):
        node.decompose()
    for node in soup.find_all("form"):
        node.unwrap()
    for node in soup.find_all(True):
        for attr in list(node.attrs):
            if attr.lower().startswith("on") or attr.lower() in {"srcdoc", "formaction", "action"}:
                del node[attr]
        if node.has_attr("href"):
            scheme = urlparse(str(node["href"])).scheme.lower()
            if scheme not in {"", "http", "https", "mailto"}:
                del node["href"]
            else:
                node["rel"] = "noopener noreferrer"
    return str(soup.body.decode_contents() if soup.body else soup)


def export_reading_site(config: Config, state: ArchiveState, site_dir: Path) -> int:
    from .export import html_to_text

    site_dir = Path(site_dir)
    resources = Path(__file__).parent / "web"
    for filename in ("style.css", "app.js"):
        write_atomic(site_dir / filename, (resources / filename).read_bytes())

    entries = []
    ready = 0
    for record in state.iter_records():
        key = record["key"]
        item_dir = config.item_dir(key)
        source = current_content_path(item_dir)
        available = record["type"] == "page" and source.exists()
        metadata = read_json(item_dir / "metadata.json", {}) or {}
        title = metadata.get("title") or record.get("title") or "无标题"
        date = metadata.get("content_date") or record.get("list_date") or ""
        content = validated_reading_content(config, state, key) if available else ""
        text = html_to_text(content) if available else ""
        entry = {
            "key": key, "title": title, "date": date, "year": date[:4] or "未知年份",
            "type": record["type"], "available": available,
            "source": metadata.get("source") or record.get("list_source") or "来源未记录",
            "excerpt": (text or record.get("list_desc") or "")[:120],
            "text": text, "href": None,
        }
        if available:
            ready += 1
            # 完整 key 保证相同日期/标题、相同 key 前缀也不会互相覆盖。
            dirname = safe_key_dirname(key)
            target = site_dir / "articles" / dirname
            entry["href"] = f"articles/{quote(dirname)}/index.html"
            for asset in state.assets_for(key):
                result = validate_downloaded_asset(config, asset)
                if result['ok']:
                    write_atomic(target / asset['local_path'], result['body'])
            warnings = metadata.get("warnings") or []
            notice = f'<p class="notice">{html.escape("；".join(warnings))}</p>' if warnings else ""
            article = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; media-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>{html.escape(title)} · 百度空间归档</title><link rel="stylesheet" href="../../style.css"></head>
<body class="reading"><main class="reading-sheet"><a class="back" href="../../index.html">← 返回目录</a>
<p class="eyebrow">BAIDU SPACE / 我的旧时光</p><h1>{html.escape(title)}</h1>
<p class="reading-meta">收藏于 {html.escape(date or "未知日期")} · {html.escape(entry['source'])}</p>
{notice}<article>{reading_html(content)}</article><footer>已保存在本地 · 百度空间归档</footer></main></body></html>'''
            write_text_atomic(target / "index.html", article)
        entries.append(entry)

    total = state.get_meta("total_count")
    data = {
        "entries": entries, "total": total, "indexed": len(entries), "ready": ready,
        "pending": sum(e["type"] == "page" and not e["available"] for e in entries),
        "other": sum(e["type"] != "page" for e in entries), "generated": utcnow_iso(),
    }
    # 单独的本地脚本支持 file://，无需服务器或 fetch，正文不能关闭 script 标签。
    serialized = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    write_text_atomic(site_dir / "archive-data.js", "window.ARCHIVE_DATA = " + serialized + ";\n")
    total_label = str(total) if isinstance(total, int) else "待确认"
    page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'self'; style-src 'self'; media-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'">
<title>我的百度空间 · 本地档案</title><link rel="stylesheet" href="style.css">
<script defer src="archive-data.js"></script><script defer src="app.js"></script></head>
<body><aside class="sidebar"><a class="brand" href="index.html"><span class="brand-mark">藏</span><span>旧时光<small>个人数字档案</small></span></a>
<p class="nav-label">我的收藏</p><nav aria-label="归档筛选">
<button class="nav-item active" data-filter="all">全部收藏 <span>{len(entries)}</span></button>
<button class="nav-item" data-filter="ready">可离线阅读 <span>{ready}</span></button>
<button class="nav-item" data-filter="pending">待归档文章 <span>{data['pending']}</span></button>
<button class="nav-item" data-filter="other">其他收藏 <span>{data['other']}</span></button></nav>
<div class="sidebar-note"><span class="status-dot"></span> 本地档案<br><small>把旧日文字，留在自己手里。</small></div></aside>
<div class="workspace"><header class="topbar"><span>个人数字档案 <span class="slash">/</span> 百度空间</span><span class="local-badge">● 离线可读</span></header>
<main><section class="hero"><div><p class="eyebrow">THE WORDS WE KEEP</p><h1>我的百度空间<span>。</span></h1><p class="hero-description">那些写下的日子，值得好好收藏。</p></div><span class="archive-stamp">私人藏品<br><b>ARCHIVE</b></span></section>
<section class="stats" aria-label="归档进度"><div><span>账号全部收藏</span><strong>{total_label}<small> 条</small></strong></div>
<div><span>已建立索引</span><strong>{len(entries)}<small> 条</small></strong></div><div><span>可离线阅读</span><strong class="green">{ready}<small> 篇</small></strong></div>
<div><span>索引内待归档</span><strong>{data['pending']}<small> 篇</small></strong></div></section>
<div class="progress-note"><span class="status-dot"></span><p id="progress-text"></p><span>只读归档</span></div>
<section class="collection"><div class="collection-heading"><div><p class="eyebrow">COLLECTION</p><h2 id="section-title">收藏目录</h2></div><span id="result-count" aria-live="polite"></span></div>
<div class="toolbar"><label class="search"><span aria-hidden="true">⌕</span><input id="q" type="search" aria-label="搜索标题或正文" placeholder="搜索标题或正文…" autocomplete="off"></label>
<label class="year-filter"><span class="sr-only">收藏年份</span><select id="year"><option value="">全部年份</option></select></label></div>
<div class="selection-bar"><label><input type="checkbox" id="select-all"> 选择当前可读文章</label><div><span id="selection-count">已选 0 篇</span><button id="export" disabled>导出所选 TXT <span aria-hidden="true">↓</span></button></div></div>
<div id="list"></div><div id="empty" class="empty" hidden><h3>没有找到相关收藏</h3><p>试试其他关键词，或切换年份。</p><button id="reset">重置筛选</button></div>
<noscript><p>请启用 JavaScript 查看收藏目录。文章仍可在 articles 文件夹中独立阅读。</p></noscript></section>
<footer class="page-footer"><span>百度空间 · 本地档案</span><span>生成于 {html.escape(data['generated'])}</span></footer></main></div></body></html>'''
    write_text_atomic(site_dir / "index.html", page)
    return ready
