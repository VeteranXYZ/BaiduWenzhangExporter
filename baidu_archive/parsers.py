"""纯解析函数：输入字符串，输出结构化数据。不发网络请求，不碰磁盘。

所有选择器都使用 **前缀匹配**，绝不硬编码百度的构建哈希
（如 ``_ptkaiapt4bxy_baiduscarticle``）—— 百度前端一发版那串就会变。
"""

from __future__ import annotations

import html as html_module
import json
import re
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .utils import domain_of, url_scheme_kind

try:  # lxml 容错性更好，处理 <p><p>…</p></p> 这类十几年前的畸形嵌套更稳
    import lxml  # noqa: F401

    _PARSER = "lxml"
except ImportError:  # pragma: no cover
    _PARSER = "html.parser"


def make_soup(markup: str) -> BeautifulSoup:
    return BeautifulSoup(markup or "", _PARSER)


# --------------------------------------------------------------------------
# 列表 JSON / HTML
# --------------------------------------------------------------------------

class ListResponseError(ValueError):
    """列表响应不是预期结构（可能是登录页、错误页或接口变更）。"""


def parse_list_json(payload: str | bytes | dict) -> dict[str, Any]:
    """解析列表接口 JSON，返回归一化字典。

    预期结构::

        {"request_id":…, "data":{"html":…,"start":…,"limit":…,
         "totalCount":…,"currentCount":…,"currentTag":…},
         "status":0, "msg":"…"}
    """
    if isinstance(payload, (str, bytes)):
        text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ListResponseError(f"列表响应不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ListResponseError("列表响应顶层不是对象")

    status = payload.get("status")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ListResponseError(f"列表响应缺少 data 对象（status={status}, msg={payload.get('msg')!r}）")

    def _int(name: str, default: int = 0) -> int:
        try:
            return int(data.get(name, default))
        except (TypeError, ValueError):
            return default

    return {
        "status": status,
        "msg": payload.get("msg"),
        "request_id": payload.get("request_id"),
        "html": data.get("html") or "",
        "start": _int("start"),
        "limit": _int("limit", 30),
        "total_count": _int("totalCount"),
        "current_count": _int("currentCount"),
        "current_tag": data.get("currentTag"),
    }


_KEY_IN_HREF = re.compile(r"[?&]key=([^&#\s\"']+)")
_TYPE_TOKENS = ("page", "article")


def _classify(li) -> str:
    """从 li 的 class 列表判定类型。未知类型一律归为 other，绝不丢弃。"""
    classes = [c.lower() for c in (li.get("class") or [])]
    for token in _TYPE_TOKENS:
        if token in classes:
            return token
    # 少数情况下类型只体现在 href 上
    for anchor in li.find_all("a", href=True):
        path = urlparse(anchor["href"]).path
        if "/page/view" in path:
            return "page"
        if "/article/view" in path:
            return "article"
    return "other"


def _extract_key(li) -> str | None:
    """key 的三条获取路径，按可靠性排序。"""
    # 1) li 自身的 id="key_<KEY>"
    raw_id = li.get("id") or ""
    if raw_id.startswith("key_") and len(raw_id) > 4:
        return raw_id[4:]
    # 2) 任意带 key= 的链接
    for anchor in li.find_all("a", href=True):
        match = _KEY_IN_HREF.search(html_module.unescape(anchor["href"]))
        if match:
            return match.group(1)
    # 3) data-* 属性兜底
    for attr in ("data-key", "data-id", "data-fsid"):
        if li.get(attr):
            return str(li.get(attr))
    return None


def _first_text(li, selectors: Iterable[str]) -> str | None:
    for selector in selectors:
        node = li.select_one(selector)
        if node:
            text = node.get_text(" ", strip=True)
            if text:
                return text
    return None


_DATE_PATTERN = re.compile(r"(\d{4})\s*[年./\-]\s*(\d{1,2})\s*[月./\-]\s*(\d{1,2})")


def normalize_date(text: str | None) -> str | None:
    """从任意中文/数字日期文本里抽出 ``YYYY-MM-DD``。抽不到返回 None（绝不编造）。"""
    if not text:
        return None
    match = _DATE_PATTERN.search(text)
    if not match:
        return None
    year, month, day = match.groups()
    try:
        if not (1990 <= int(year) <= 2100 and 1 <= int(month) <= 12 and 1 <= int(day) <= 31):
            return None
    except ValueError:
        return None
    return f"{year}-{month.zfill(2)}-{day.zfill(2)}"


def parse_list_html(html: str, *, offset: int = 0, site_root: str = "https://wenzhang.baidu.com") -> list[dict[str, Any]]:
    """解析 ``data.html``，返回该页全部记录。

    保留原始顺序：``list_offset`` + ``position_in_page`` + ``global_position``。
    每条都附带 ``raw_html``，未来解析器改进后可离线重建。
    """
    soup = make_soup(html)
    units = soup.select("li.unit")
    if not units:
        units = soup.find_all("li", id=re.compile(r"^key_"))

    records: list[dict[str, Any]] = []
    for position, li in enumerate(units):
        key = _extract_key(li)
        record_type = _classify(li)

        detail_href = None
        external_url = None
        for anchor in li.find_all("a", href=True):
            href = html_module.unescape(anchor["href"]).strip()
            if not href or href.startswith("#"):
                continue
            absolute = urljoin(site_root + "/", href)
            path = urlparse(absolute).path
            if "/page/view" in path or "/article/view" in path:
                detail_href = detail_href or absolute
            elif url_scheme_kind(absolute) == "http" and domain_of(absolute) not in {
                "wenzhang.baidu.com",
                "",
            }:
                external_url = external_url or absolute

        tags = [
            t.get_text(" ", strip=True)
            for t in li.select(".tag, .tags a, .tag-item, a.tag")
            if t.get_text(strip=True)
        ]

        title = _first_text(
            li, (".title", "h3", "h2", ".unit-title", "a.title", ".name")
        )
        if not title and detail_href:
            anchor = li.find("a", href=re.compile(r"/(page|article)/view"))
            if anchor:
                title = anchor.get_text(" ", strip=True) or anchor.get("title")
        date_text = _first_text(li, (".date", ".time", ".time-cang", ".create-time", ".unit-date"))
        desc = _first_text(li, (".desc", ".summary", ".abstract", ".content"))

        records.append(
            {
                "key": key,
                "type": record_type,
                "title": title,
                "list_date_text": date_text,
                "list_date": normalize_date(date_text),
                "tags": tags,
                "list_desc": desc,
                "detail_url": detail_href,
                "external_url": external_url,
                "list_offset": offset,
                "position_in_page": position,
                "global_position": offset + position,
                "raw_html": str(li),
                "li_attrs": {k: (" ".join(v) if isinstance(v, list) else v) for k, v in li.attrs.items()},
            }
        )
    return records


# --------------------------------------------------------------------------
# 详情页 -> iframe
# --------------------------------------------------------------------------

_BDSTOKEN_PATTERN = re.compile(r"bdstoken\s*[:=]\s*['\"]([0-9a-zA-Z]+)['\"]")


def parse_detail_html(html: str, *, base_url: str | None = None) -> dict[str, Any]:
    """从 ``/page/view?key=…`` 页面提取 iframe 正文地址。

    HTML 属性里的 ``&amp;`` 必须解码，否则请求会带着字面 ``&amp;`` 打过去而签名失效。
    BeautifulSoup 读取属性时已自动解码，这里再兜一次底以防上游换解析器。
    """
    soup = make_soup(html)
    iframe = (
        soup.select_one("iframe.pcs-article-iframe")
        or soup.select_one('iframe[class*="pcsArticleIframe"]')
        or soup.select_one('iframe[src*="/page/content/"]')
        or soup.select_one('iframe[src*="bdstatic.com"]')
        or soup.find("iframe", src=True)
    )
    iframe_url = None
    if iframe and iframe.get("src"):
        iframe_url = html_module.unescape(iframe["src"].strip())
        if iframe_url.startswith("//"):
            iframe_url = "https:" + iframe_url
        elif base_url and url_scheme_kind(iframe_url) == "relative":
            iframe_url = urljoin(base_url, iframe_url)

    bdstoken = None
    match = _BDSTOKEN_PATTERN.search(html or "")
    if match:
        bdstoken = match.group(1)

    title_tag = soup.find("title")
    return {
        "iframe_url": iframe_url,
        "has_iframe": iframe is not None,
        "bdstoken": bdstoken,
        "page_title": title_tag.get_text(strip=True) if title_tag else None,
    }


# --------------------------------------------------------------------------
# 正文页
# --------------------------------------------------------------------------

def _select_prefixed(soup: BeautifulSoup, id_prefix: str, class_prefix: str):
    """按 id 前缀选，退化到 class 前缀。两者都不写死哈希段。"""
    node = soup.select_one(f'[id^="{id_prefix}"]')
    if node is None:
        node = soup.select_one(f'[class^="{class_prefix}"]')
    if node is None:
        node = soup.find(attrs={"class": re.compile(rf"\b{re.escape(class_prefix)}")})
    return node


def parse_content_html(html: str) -> dict[str, Any]:
    """解析 iframe 正文页，返回标题、正文原始 HTML、收藏日期、来源。

    ``content_html`` 是容器的 **inner HTML 原样**，不做任何清洗 —— 清洗是派生步骤。
    """
    soup = make_soup(html)

    title_node = _select_prefixed(soup, "detailArticleTitle_", "pcs-article-title")
    if title_node is None:
        container = _select_prefixed(soup, "pcsArticle_", "pcs-article")
        title_node = container.find("h1") if container else soup.find("h1")
    title = title_node.get_text(" ", strip=True) if title_node else None

    content_node = _select_prefixed(soup, "detailArticleContent_", "pcs-article-content")
    content_html = content_node.decode_contents() if content_node else None

    footer_node = _select_prefixed(soup, "detailArticleFooter_", "footer")
    footer_text = footer_node.get_text(" ", strip=True) if footer_node else None

    scope = footer_node or soup
    time_node = scope.select_one(".time-cang") or soup.select_one(".time-cang")
    source_node = scope.select_one(".link-src") or soup.select_one(".link-src")
    time_text = time_node.get_text(" ", strip=True) if time_node else None
    source = source_node.get_text(" ", strip=True) if source_node else None

    text = ""
    if content_node:
        text = content_node.get_text("\n", strip=True)

    page_title_tag = soup.find("title")
    return {
        "title": title,
        "content_html": content_html,
        "content_text": text,
        "content_length": len(text),
        "content_html_length": len(content_html or ""),
        "footer_text": footer_text,
        "content_date_text": time_text,
        "content_date": normalize_date(time_text),
        "source": source,
        "page_title": page_title_tag.get_text(strip=True) if page_title_tag else None,
        "has_container": content_node is not None,
        "has_title": title_node is not None,
        "has_footer": footer_node is not None,
    }


# --------------------------------------------------------------------------
# 资源提取
# --------------------------------------------------------------------------

# (CSS 选择器, 取哪个属性, 归类)
ASSET_SELECTORS: tuple[tuple[str, str, str], ...] = (
    ("img", "src", "image"),
    ("img", "data-src", "image"),          # 老页面懒加载：真实地址常在 data-src
    ("img", "data-original", "image"),
    ("img", "data-lazy-src", "image"),
    ("video", "src", "video"),
    ("audio", "src", "audio"),
    ("source", "src", "media-source"),
    ("embed", "src", "embed"),
    ("object", "data", "object"),
    ("iframe", "src", "iframe"),
    ("a", "href", "link"),
)

IMAGE_LIKE = frozenset({"image"})
MEDIA_LIKE = frozenset({"video", "audio", "media-source", "embed", "object"})


def extract_assets(content_html: str, *, base_url: str | None = None) -> list[dict[str, Any]]:
    """扫描正文中所有远程资源引用。

    返回顺序稳定、按 (element_type, attr, url) 去重。
    ``data:`` / ``javascript:`` / ``about:`` 会被正确分类而不是当成 HTTP 下载。
    """
    if not content_html:
        return []
    soup = make_soup(content_html)
    seen: set[tuple[str, str, str]] = set()
    assets: list[dict[str, Any]] = []

    for selector, attr, kind in ASSET_SELECTORS:
        for node in soup.select(selector):
            raw = node.get(attr)
            if not raw or not str(raw).strip():
                continue
            raw = html_module.unescape(str(raw).strip())
            scheme_kind = url_scheme_kind(raw)
            resolved = raw
            if scheme_kind == "protocol-relative":
                resolved = "https:" + raw
                scheme_kind = "http"
            elif scheme_kind == "relative" and base_url:
                resolved = urljoin(base_url, raw)
                scheme_kind = url_scheme_kind(resolved)

            dedupe = (kind, attr, resolved)
            if dedupe in seen:
                continue
            seen.add(dedupe)

            assets.append(
                {
                    "element_type": node.name,
                    "attribute": attr,
                    "kind": kind,
                    "original_url": raw,
                    "resolved_url": resolved if scheme_kind == "http" else None,
                    "scheme_kind": scheme_kind,
                    "domain": domain_of(resolved) if scheme_kind == "http" else "",
                    "downloadable": scheme_kind == "http",
                    "alt": node.get("alt") if node.name == "img" else None,
                    "text": node.get_text(" ", strip=True)[:200] if node.name == "a" else None,
                }
            )

        # srcset 单独处理：逗号分隔的候选列表
        if selector == "img" and attr == "src":
            for node in soup.select("img[srcset]"):
                for candidate in str(node.get("srcset") or "").split(","):
                    url = candidate.strip().split(" ")[0]
                    if not url:
                        continue
                    url = html_module.unescape(url)
                    scheme_kind = url_scheme_kind(url)
                    resolved = url
                    if scheme_kind == "protocol-relative":
                        resolved = "https:" + url
                        scheme_kind = "http"
                    elif scheme_kind == "relative" and base_url:
                        resolved = urljoin(base_url, url)
                        scheme_kind = url_scheme_kind(resolved)
                    dedupe = ("image", "srcset", resolved)
                    if dedupe in seen:
                        continue
                    seen.add(dedupe)
                    assets.append(
                        {
                            "element_type": "img",
                            "attribute": "srcset",
                            "kind": "image",
                            "original_url": url,
                            "resolved_url": resolved if scheme_kind == "http" else None,
                            "scheme_kind": scheme_kind,
                            "domain": domain_of(resolved) if scheme_kind == "http" else "",
                            "downloadable": scheme_kind == "http",
                            "alt": node.get("alt"),
                            "text": None,
                        }
                    )
    return assets


# --------------------------------------------------------------------------
# 认证 / 错误页识别
# --------------------------------------------------------------------------

_LOGIN_MARKERS = (
    "passport.baidu.com",
    "请先登录",
    "登录百度账号",
    "用户未登录",
    "登录后查看",
    "百度账号登录",
    "wappass.baidu.com",
)


def looks_like_login_page(html: str | None) -> bool:
    """HTTP 200 不代表成功 —— 旧站点常把登录页当 200 返回。"""
    if not html:
        return False
    sample = html[:20000].lower()
    return any(marker.lower() in sample for marker in _LOGIN_MARKERS)
