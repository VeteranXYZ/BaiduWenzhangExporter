"""派生格式导出。

优先级：RAW HTML > content.local.html > Markdown > TXT。
Markdown/TXT 转换失败绝不阻塞归档 —— 原始数据已经安全落盘。
"""

from __future__ import annotations

import html as html_module
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape, quoteattr

from .config import Config
from .parsers import make_soup
from .state import ArchiveState
from .storage import IntegrityError, current_content_path, read_json, read_text, write_text_atomic
from .utils import sanitize_filename

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
            source = current_content_path(item_dir)
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
        source = current_content_path(item_dir, prefer_local=False)
        if not source.exists():
            continue
        from .truth import article_truth
        if 'resource_generation_mismatch' in article_truth(config, state, key)['issues']:
            raise IntegrityError('WXR current resource generation mismatch: ' + key)
        date = metadata.get("content_date") or metadata.get("list_date") or record.get("list_date")
        entries.append(
            {
                "key": key,
                "title": metadata.get("title") or record.get("title") or "无标题",
                "content": wxr_content(read_text(source) or '',
                    {**metadata, 'detail_url': metadata.get('detail_url') or record.get('detail_url') or config.detail_url(key)},
                    state.assets_for(key)),
                "date": date,
                "url": record.get("detail_url") or "",
                "tags": metadata.get("tags") or [],
            }
        )

    # Official importer process_post calls post_exists(title, '', date, post_type).
    # Slug, WXR post_id and _archive_key do NOT bypass that check:
    # https://github.com/WordPress/wordpress-importer/blob/master/src/class-wp-import.php
    # Refuse collisions; stable _archive_key metadata remains a traceable identity,
    # not a promise of importer deduplication behavior.
    identities = {}
    for entry in entries:
        try:
            date = datetime.strptime(entry['date'] or '1970-01-01', '%Y-%m-%d').strftime('%Y-%m-%d')
        except ValueError:
            date = '1970-01-01'
        entry['date'] = date
        # Conservative entity/case/accent normalization also catches common SQL
        # collation equivalences. Actual target filters/collation require live import.
        import unicodedata
        title_identity = ''.join(c for c in unicodedata.normalize('NFKD', html_module.unescape(entry['title']).casefold())
                                 if not unicodedata.combining(c))
        identity = (title_identity, date + ' 00:00:00', 'post')
        if identity in identities:
            raise IntegrityError('WordPress importer identity collision: ' +
                                 identities[identity] + ' / ' + entry['key'])
        identities[identity] = entry['key']

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
        f"    <link>{escape(config.site_root)}</link>\n"
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
            f'\n        <category domain="post_tag" nicename={quoteattr(sanitize_filename(t, max_length=30))}>'
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
            f"<wp:meta_value><![CDATA[{_cdata(entry['key'])}]]></wp:meta_value></wp:postmeta>\n"
            "    </item>\n"
        )
    parts.append("</channel>\n</rss>\n")

    from xml.etree import ElementTree

    xml = ''.join(parts)
    try:
        ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        raise IntegrityError('WXR XML 包含无效字符，未发布导出文件') from None
    path = config.root / filename
    write_text_atomic(path, xml)
    return path


def wxr_content(content: str, metadata: dict, assets: list[dict]) -> str:
    """WXR 使用当前未本地化正文，将相对资源引用还原为远程地址。"""
    from .parsers import parse_srcset

    base = metadata.get('iframe_url_original') or metadata.get('detail_url')
    mapping = {}
    for asset in assets:
        remote = asset.get('resolved_url') or urljoin(base or '', asset['original_url'])
        if urlparse(remote).scheme not in {'http', 'https'}:
            continue
        mapping[asset['original_url']] = remote
        if asset.get('local_path'):
            mapping[asset['local_path']] = remote

    def normalize(value):
        value = html_module.unescape(str(value).strip())
        if not value or value.startswith('#'):
            return value
        if value in mapping:
            return mapping[value]
        parsed = urlparse(value)
        if parsed.scheme:
            return value  # Preserve mailto/tel/data and intentional special schemes.
        if value.startswith('assets/') or value.startswith('./assets/') or '/assets/' in value and value.startswith('../'):
            raise IntegrityError('WXR archive-local URL lacks original remote address: ' + value)
        remote = urljoin(base or '', value)
        if value.startswith('//') and not urlparse(remote).scheme:
            remote = 'https:' + value
        if urlparse(remote).scheme not in {'http', 'https'} or not urlparse(remote).netloc:
            raise IntegrityError('WXR relative URL lacks a remote base: ' + value)
        return remote

    soup = make_soup(content)
    for node in soup.find_all(True):
        for attribute in ('src', 'href', 'poster', 'data', 'data-src', 'data-original', 'data-lazy-src'):
            if node.has_attr(attribute):
                node[attribute] = normalize(node[attribute])
        if node.has_attr('srcset'):
            node['srcset'] = ', '.join(normalize(url) + (' ' + descriptor if descriptor else '')
                                     for url, descriptor in parse_srcset(str(node['srcset'])))
        if node.name == 'img':
            for attribute in ('data-src', 'data-original', 'data-lazy-src'):
                if node.get(attribute):
                    node['src'] = node[attribute]
                    break
        for attribute in list(node.attrs):
            if attribute.startswith('data-archive-'):
                del node[attribute]
    # Validate final HTML separately from normalization; do not publish unsafe WXR.
    for node in soup.find_all(True):
        urls = [str(node[a]) for a in ('src', 'href', 'poster', 'data', 'data-src', 'data-original', 'data-lazy-src') if node.has_attr(a)]
        urls += [url for url, _ in parse_srcset(str(node.get('srcset') or ''))]
        for url in urls:
            if url and not url.startswith('#') and not urlparse(url).scheme:
                raise IntegrityError('WXR migration-unsafe URL remains: ' + url)
    return soup.body.decode_contents() if soup.body else str(soup)


# --------------------------------------------------------------------------
# 静态站点（百度关闭后仍可本地阅读）
# --------------------------------------------------------------------------

def export_site(config: Config, state: ArchiveState, site_dir: Path) -> int:
    """导出可搜索、筛选、勾选 TXT 导出的独立离线阅读站点。"""
    from .site import export_reading_site

    return export_reading_site(config, state, site_dir)
