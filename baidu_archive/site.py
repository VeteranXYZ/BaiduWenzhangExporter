"""独立可搬运的离线阅读界面；仅生成派生文件，不改动归档原件。"""

from __future__ import annotations

import html
import json
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from .config import Config
from .parsers import make_soup, parse_srcset
from .state import ArchiveState
from .truth import missing_resource, validated_reading_content, validate_downloaded_asset
from .storage import current_content_path, read_json, write_atomic, write_text_atomic
from .utils import bookmark_url, redact_url, safe_key_dirname, utcnow_iso


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


def offline_reading_html(content: str, available_paths, *, base_url: str | None = None,
                         origin_by_path: dict | None = None) -> str:
    """Build an offline view with working local media and readable source pointers."""
    soup = make_soup(content)
    origins = origin_by_path or {}

    def local(value):
        return bool(value and (value in available_paths or value.startswith('data:image/')))

    def source_url(value):
        value = origins.get(value, value)
        if not value or value.startswith(('assets/', './assets/')):
            return None
        try:
            url = urljoin(base_url or '', value)
            parsed = urlparse(url)
            if parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username:
                return url
        except ValueError:
            pass
        return None

    def replace_with_pointer(node, kind, label, candidates):
        marker = soup.new_tag('span')
        marker['class'] = 'archive-media-unavailable'
        marker['data-archive-unavailable'] = kind
        marker.string = f'［{label}］'
        url = next((url for value in candidates if (url := source_url(value))), None)
        if url:
            marker['data-archive-source'] = redact_url(url)
            if url == redact_url(url) and not node.find_parent('a'):
                link = soup.new_tag('a', href=url)
                link.string = '原始地址'
                marker.append(' ')
                marker.append(link)
        node.replace_with(marker)

    for node in soup.find_all(['iframe', 'object', 'embed']):
        if node.parent is not None:
            replace_with_pointer(node, 'embed', '旧嵌入媒体无法离线播放',
                                 [node.get('src'), node.get('data')])

    for img in soup.find_all('img'):
        src = img.get('src')
        image_set = parse_srcset(str(img.get('srcset') or ''))
        selected = [(url, desc) for url, desc in image_set if local(url)]
        if not local(src):
            candidate = next((url for url, _ in selected), None)
            if not candidate and (picture := img.find_parent('picture')):
                candidate = next((url for node in picture.find_all('source')
                                  for url, _ in parse_srcset(str(node.get('srcset') or '')) if local(url)), None)
            if not candidate:
                hint = str(img.get('alt') or '').strip()
                replace_with_pointer(img, 'image', '图片未保存' + (f'：{hint}' if hint else ''),
                                     [img.get('data-archive-original-src'), img.get('data-src'), src,
                                      *(url for url, _ in image_set)])
                continue
            img['src'] = candidate
        if selected:
            img['srcset'] = ', '.join(url + (' ' + desc if desc else '') for url, desc in selected)
        else:
            img.attrs.pop('srcset', None)
        for attr in ('data-src', 'data-original', 'data-lazy-src'):
            img.attrs.pop(attr, None)

    for node in soup.find_all(['video', 'audio']):
        src = node.get('src')
        children = node.find_all('source')
        valid = [child for child in children if local(child.get('src'))]
        if not local(src) and not valid:
            replace_with_pointer(node, 'media', '媒体文件未保存',
                                 [src, *(child.get('src') for child in children)])
            continue
        if not local(src):
            node.attrs.pop('src', None)
        for child in children:
            if child not in valid:
                child.decompose()
        if not local(node.get('poster')):
            node.attrs.pop('poster', None)

    for source in soup.select('picture source[srcset]'):
        selected = [(url, desc) for url, desc in parse_srcset(str(source['srcset'])) if local(url)]
        if selected:
            source['srcset'] = ', '.join(url + (' ' + desc if desc else '') for url, desc in selected)
        else:
            source.decompose()
    for node in soup.find_all('a', href=True):
        href = node['href']
        if not href.startswith('#') and href not in available_paths:
            url = source_url(href)
            if url and url == redact_url(url):
                node['href'] = url
            elif not href.startswith(('mailto:', 'tel:')):
                del node['href']
    return reading_html(str(soup))


def export_reading_site(config: Config, state: ArchiveState, site_dir: Path, *,
                        resource_report: dict | None = None) -> int:
    from .export import html_to_text

    site_dir = Path(site_dir)
    resources = Path(__file__).parent / "web"
    for filename in ("style.css", "app.js"):
        write_atomic(site_dir / filename, (resources / filename).read_bytes())
    for filename in ("brand.css", "icon.png"):
        write_atomic(site_dir / filename, (resources.parent / "ui" / filename).read_bytes())

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
            "bookmark_url": bookmark_url(record),
        }
        if available:
            ready += 1
            # 完整 key 保证相同日期/标题、相同 key 前缀也不会互相覆盖。
            dirname = safe_key_dirname(key)
            target = site_dir / "articles" / dirname
            entry["href"] = f"articles/{quote(dirname)}/index.html"
            available_paths, origins = set(), {}
            for asset in state.assets_for(key):
                if asset['kind'] == 'link' or asset.get('scheme_kind') == 'data':
                    continue
                if asset.get('local_path'):
                    origins[asset['local_path']] = asset.get('resolved_url') or asset['original_url']
                result = validate_downloaded_asset(config, asset)
                if result['ok']:
                    relative = asset['local_path']
                    if relative not in available_paths:
                        write_atomic(target / relative, result['body'])
                        available_paths.add(relative)
                    if resource_report is not None:
                        resource_report['included'].append({
                            'key': key, 'id': asset['id'], 'kind': asset['kind'],
                            'url': redact_url(asset.get('resolved_url') or asset['original_url']),
                            'path': (target / relative).relative_to(site_dir).as_posix(),
                            'sha256': asset['sha256'],
                        })
                elif resource_report is not None:
                    resource_report['missing'].append({'key': key, **missing_resource(asset, result)})
            body = offline_reading_html(content, available_paths,
                                        base_url=metadata.get('iframe_url_original') or metadata.get('detail_url'),
                                        origin_by_path=origins)
            warnings = metadata.get("warnings") or []
            notice = f'<p class="notice">{html.escape("；".join(warnings))}</p>' if warnings else ""
            article = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; media-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>{html.escape(title)} · 百度空间归档</title><link rel="icon" href="../../icon.png" type="image/png">
<link rel="stylesheet" href="../../brand.css"><link rel="stylesheet" href="../../style.css"></head>
<body class="reading"><main class="reading-sheet"><a class="back" href="../../index.html">← 返回目录</a>
<p class="eyebrow">BAIDU SPACE / 我的旧时光</p><h1>{html.escape(title)}</h1>
<p class="reading-meta">收藏于 {html.escape(date or "未知日期")} · {html.escape(entry['source'])}</p>
{notice}<article>{body}</article><footer>已保存在本地 · 百度空间归档</footer></main></body></html>'''
            write_text_atomic(target / "index.html", article)
        elif record['type'] == 'page' and resource_report is not None:
            resource_report['missing_articles'].append({'key': key, 'title': title, 'status': 'body_unavailable'})
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
<title>我的百度空间 · 本地档案</title><link rel="icon" href="icon.png" type="image/png">
<link rel="stylesheet" href="brand.css"><link rel="stylesheet" href="style.css">
<script defer src="archive-data.js"></script><script defer src="app.js"></script></head>
<body><aside class="sidebar"><a class="brand" href="index.html"><img class="brand-mark" src="icon.png" width="43" height="43" alt=""><span>旧时光<small>个人数字档案</small></span></a>
<p class="nav-label">我的收藏</p><nav aria-label="归档筛选">
<button class="nav-item active" data-filter="all">全部收藏 <span>{len(entries)}</span></button>
<button class="nav-item" data-filter="ready">可离线阅读 <span>{ready}</span></button>
<button class="nav-item" data-filter="pending">待归档文章 <span>{data['pending']}</span></button>
<button class="nav-item" data-filter="other">其他收藏 <span>{data['other']}</span></button></nav>
<div class="sidebar-note"><span class="status-dot"></span> 本地档案<br><small>把旧日文字，留在自己手里。</small></div></aside>
<div class="workspace"><header class="topbar"><span>个人数字档案 <span class="slash">/</span> 百度空间</span><span class="local-badge">● 离线可读</span></header>
<main><section class="hero"><div><p class="eyebrow">THE WORDS WE KEEP</p><h1>我的百度空间<span>。</span></h1><p class="hero-description">那些写下的日子，值得好好收藏。</p></div><span class="archive-stamp">私人藏品<br><b>ARCHIVE</b></span></section>
<section class="stats" aria-label="归档进度"><div><span>账号全部收藏</span><strong>{total_label}<small> 条</small></strong></div>
<div><span>已建立索引</span><strong>{len(entries)}<small> 条</small></strong></div><div><span>可离线阅读</span><strong class="brand-accent">{ready}<small> 篇</small></strong></div>
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
