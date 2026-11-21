"""原生桌面外壳。

这一层才是「软件」与「网页」的分界：界面本身是 HTML/CSS（与 QzoneArchive 同构），
但它跑在一个没有浏览器外壳的原生窗口里，并接上系统菜单栏、文件选择器与通知。

macOS 走系统 WKWebView，不下载任何浏览器运行时。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from typing import Any

import webview
from webview.menu import Menu, MenuAction, MenuSeparator

from .config import Config, load_config
from .locking import ArchiveLock
from .state import SCHEMA_VERSION, ArchiveState, backup_before_migration, database_version
from .storage import write_json_atomic
from .utils import bookmark_url, utcnow_iso
from .truth import article_truth, index_truth, validate_downloaded_asset, resource_state

UI_DIR = Path(__file__).parent / "ui"

WINDOW_MIN = (720, 540)
WINDOW_DEFAULT = (1180, 780)
THUMBNAIL_CACHE_ENTRIES = 128
THUMBNAIL_CACHE_BYTES = 8 * 1024 * 1024
THUMBNAIL_MAX_SIZE = 1024
_PREFS_LOCK = Lock()


def _prefs_path() -> Path:
    """窗口几何等界面偏好，与归档数据分开存放。"""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "BaiduArchive"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home())) / "BaiduArchive"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "baidu-archive"
    base.mkdir(parents=True, exist_ok=True)
    return base / "window.json"


def _load_prefs() -> dict[str, Any]:
    try:
        prefs = json.loads(_prefs_path().read_text(encoding="utf-8"))
        return prefs if isinstance(prefs, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _update_prefs(**changes: Any) -> None:
    with _PREFS_LOCK:
        try:
            write_json_atomic(_prefs_path(), {**_load_prefs(), **changes})
        except OSError:
            pass


def _human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def _notify(title: str, message: str) -> None:
    """系统通知。macOS 用 osascript，避免为此引入 pyobjc。"""
    if sys.platform == "darwin":
        script = f'display notification {json.dumps(message)} with title {json.dumps(title)}'
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
    elif sys.platform.startswith("linux"):
        subprocess.run(["notify-send", title, message], check=False, capture_output=True)


def _disk_usage(root: Path) -> int:
    """归档目录的实际占用（文章、图片、原始响应、数据库）。只做 stat，不读内容，几千个文件也只需几十毫秒。"""
    total = 0
    stack = [str(root)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _reconcile_statuses(config: Config, state: ArchiveState) -> int:
    """让数据库里的文章状态等于真相层的判断。返回状态有变化的篇数。

    真相层只会降级、不会升级：数据库若停在 parsed，即使一切就绪也不会显示为完整。
    做法是先假定完整，再交给 article_truth 逐项核对 —— 它会按实际字节把有问题的
    降回 assets_partial 等状态。只改状态字段，不碰任何资源记录。
    """
    from .truth import article_truth

    changed = 0
    for record in state.iter_records("page"):
        key = record["key"]
        current = (state.get_item(key) or {}).get("status")
        if current not in {"parsed", "complete", "assets_partial", "legacy_unverified"}:
            continue                    # 未下载 / 失败 / 有警告的不在这里处理
        state.update_item(key, status="complete")
        effective = article_truth(config, state, key)["status"]
        state.update_item(key, status=effective)
        changed += effective != current
    return changed


def _offline_client(config: Config):
    """一个拒绝任何网络请求的客户端，用于离线修复。

    离线整理复用 run_index，它需要一个 client 参数；
    这里给的 client 一旦被要求联网就直接报错，保证「离线修复」名副其实。
    """
    import httpx

    from .auth import Credentials
    from .client import ArchiveClient

    def refuse(request):
        raise httpx.ConnectError("离线修复不发网络请求", request=request)

    client = ArchiveClient(config, Credentials())
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(refuse))
    return client


def _plain_failure(asset: dict[str, Any]) -> str:
    """把图片下载失败的技术原因翻译成人话。原始错误仍完整保存在数据库与 raw 里。"""
    error = (asset.get("error") or "").lower()
    code = asset.get("http_status")
    if asset.get("validation_reason") == "invalid_media_placeholder" or "placeholder" in error:
        return "百度已删除原图，只返回了一张「图片不存在」占位图"
    if asset.get("download_status") == "ok":
        return "本地图片文件缺失，或未通过校验"
    if "不是可识别的图片" in (asset.get("error") or ""):
        return "原网站返回的是网页而不是图片"
    if code == 404:
        return "原地址找不到图片（HTTP 404）"
    if code in (401, 403):
        return "原网站拒绝访问"
    if isinstance(code, int) and 500 <= code < 600:
        return f"原网站服务器出错（HTTP {code}）"
    if isinstance(code, int) and 400 <= code < 500:
        return "原网站拒绝了请求"
    if "connect" in error or "name" in error or "resolve" in error:
        return "请求时无法连接原网站"
    if "timeout" in error or "超时" in error:
        return "原网站长时间无响应"
    return asset.get("error") or "下载失败"


def _saved_article_keys(config: Config, state: ArchiveState) -> list[str]:
    return [record["key"] for record in state.iter_records("page")
            if article_truth(config, state, record["key"])["available"]]


def _reveal(path: Path) -> None:
    """在文件管理器中定位。"""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", str(path)], check=False)
        elif os.name == "nt":
            subprocess.run(["explorer", "/select,", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path.parent)], check=False)
    except OSError:
        pass


class Api:
    """暴露给前端的方法。前端不碰网络与磁盘，一切经由这里。"""

    def __init__(self, config: Config):
        self.config = config
        self.window: webview.Window | None = None
        # 懒建：Application 会在构造时创建目录、读任务状态并生成令牌，
        # 没必要在窗口还没打开时就做这些。
        self._application: Any = None
        self._thumbs: OrderedDict[tuple, str] = OrderedDict()
        self._thumb_lock = Lock()

    # ---------- 数据 ----------
    EMPTY_STATS = {
        "total_api": 0, "indexed": 0, "page": 0, "article": 0, "other": 0,
        "complete": 0, "warning": 0, "failed": 0, "pending": 0,
        "legacy_unverified": 0, "assets_legacy_unverified": 0,
        "assets_downloaded": 0, "assets_failed": 0, "historical_indexed": 0,
        "index_complete": None, "db_size": "—", "has_db": False, "verified": False,
    }

    def _empty(self) -> dict[str, Any]:
        return {**self.EMPTY_STATS, "root": str(self.config.root.resolve()),
                "ignored_settings": dict(self.config.ignored_settings)}

    def get_stats(self) -> dict[str, Any]:
        """首屏用的瞬时快照：只读数据库记账，不做任何磁盘校验。

        数字可能偏乐观（数据库说下载成功，字节却可能已损坏），所以 ``verified``
        为 False，界面据此显示「校验中」。真实结论由 get_verified_stats() 给出。
        """
        if not self.config.db_path.exists():
            return self._empty()
        try:
            with ArchiveState(self.config.db_path) as state:
                types = state.type_counts()
                statuses = state.status_counts()
                assets = state.asset_counts()
                index = index_truth(state)
                db_bytes = _disk_usage(self.config.root)
                return {
                    **self._empty(),
                    "has_db": True,
                    "verified": False,
                    "total_api": state.get_meta("total_count") or 0,
                    "indexed": index["indexed"],
                    "historical_indexed": state.count_records(),
                    "index_complete": index["ok"],
                    "page": types.get("page", 0),
                    "article": types.get("article", 0),
                    "other": types.get("other", 0),
                    "complete": statuses.get("complete", 0),
                    "warning": statuses.get("warning", 0),
                    "failed": statuses.get("failed", 0) + statuses.get("auth_error", 0),
                    "pending": statuses.get("pending", 0),
                    "assets_downloaded": assets.get("ok", 0),
                    "assets_failed": assets.get("failed", 0),
                    "db_size": _human_bytes(db_bytes),
                }
        except Exception:  # noqa: BLE001 —— 界面不能因为读库失败而白屏
            return self._empty()

    def get_verified_stats(self) -> dict[str, Any]:
        """完整校验：逐篇核对磁盘字节，这才是能用来下结论的数字。

        耗时随归档规模线性增长（约 300 篇 / 900 资源 ≈ 1s），前端在首屏之后
        异步调用，期间界面照常可用 —— pywebview 的 js_api 本就跑在独立线程上。

        性能要点：article_truth() 内部已经校验过该篇的全部资源并在 usable_assets
        里给出结果，此处直接复用，不再重复校验（早先的实现会把每个资源验 3 遍）。
        """
        if not self.config.db_path.exists():
            return self._empty()
        try:
            with ArchiveState(self.config.db_path) as state:
                types = state.type_counts()
                statuses: dict[str, int] = {}
                usable_ids: set[Any] = set()
                for record in state.iter_records("page"):
                    facts = article_truth(self.config, state, record["key"])
                    statuses[facts["status"]] = statuses.get(facts["status"], 0) + 1
                    usable_ids.update(facts["usable_assets"])

                ok = failed = legacy = 0
                for asset in state.all_assets():
                    valid = asset["id"] in usable_ids
                    if not valid and asset.get("download_status") == "ok":
                        # 不属于任何 page 条目的资源（例如 article 类型）仍需单独校验
                        valid = validate_downloaded_asset(self.config, asset)["ok"]
                    is_legacy = (
                        not valid
                        and asset.get("download_attempted")
                        and resource_state(self.config, asset)["state"] == "legacy_unverified"
                    )
                    ok += valid
                    legacy += bool(is_legacy)
                    failed += bool(asset.get("download_attempted") and not valid and not is_legacy)

                total = 0
                for file in self.config.root.rglob("*"):
                    if file.is_file():
                        try:
                            total += file.stat().st_size
                        except OSError:
                            pass

                index = index_truth(state)
                return {
                    **self._empty(),
                    "has_db": True,
                    "verified": True,
                    "legacy_unverified": statuses.get("legacy_unverified", 0),
                    "assets_legacy_unverified": legacy,
                    "total_api": state.get_meta("total_count") or 0,
                    "indexed": index["indexed"],
                    "historical_indexed": state.count_records(),
                    "index_complete": index["ok"],
                    "page": types.get("page", 0),
                    "article": types.get("article", 0),
                    "other": types.get("other", 0),
                    "complete": statuses.get("complete", 0),
                    "warning": statuses.get("warning", 0),
                    "failed": statuses.get("failed", 0) + statuses.get("auth_error", 0),
                    "pending": statuses.get("pending", 0),
                    "assets_downloaded": ok,
                    "assets_failed": failed,
                    "db_size": _human_bytes(total),
                }
        except Exception:  # noqa: BLE001
            return self._empty()

    # ---------- 归档内容 / 媒体 / 任务 ----------
    def _app(self):
        """复用 server.Application —— 它是普通 Python 类，不需要起 HTTP 服务。"""
        if self._application is None:
            from .server import Application

            self._application = Application(self.config)
        return self._application

    def get_library(self) -> dict[str, Any]:
        """归档内容 / 媒体 / 异常跳过三页共用的数据源。

        内部逐篇走 article_truth，耗时与 get_verified_stats 同量级，
        前端异步调用并在期间显示加载态。
        """
        try:
            return self._app().library()
        except Exception as exc:  # noqa: BLE001
            return {"records": [], "summary": {}, "error": str(exc)[:200]}

    def get_article(self, key: str) -> dict[str, Any]:
        """单篇阅读副本。

        图片内联为 data URI —— 原生窗口没有 HTTP 服务，而 WKWebView 读取
        页面目录之外的 file:// 资源会被拦截，内联是唯一可靠的办法。
        只内联校验通过的本地文件；失败的图片保留占位与原始 URL，绝不删除元素。
        """
        import base64
        import mimetypes

        from .site import reading_html
        from .truth import validated_reading_content
        from .parsers import make_soup
        from .storage import read_json

        try:
            with ArchiveState(self.config.db_path) as state:
                record = state.get_record(key)
                if not record:
                    return {"error": "未找到该条目"}
                folder = self.config.item_dir(key)
                meta = read_json(folder / "metadata.json", {}) or {}
                soup = make_soup(reading_html(validated_reading_content(self.config, state, key)))
                # 桌面阅读器离线显示正文。链接地址保留在提示和导出的原文中。
                for link in soup.find_all("a"):
                    href = link.get("href") or link.get("xlink:href")
                    if href and not href.startswith("#"):
                        link.attrs.pop("href", None)
                        link.attrs.pop("xlink:href", None)
                        if not link.get("title"):
                            link["title"] = href
                local_assets = {a['local_path']: a for a in state.assets_for(key)
                                if a.get('local_path') and a.get('kind') in {'image', 'video', 'audio', 'media-source'}}

                budget = 24 * 1024 * 1024          # 单篇内联上限，避免超长图文撑爆 WebView
                used = 0
                inlined = skipped = 0
                for node in soup.find_all(["img", "video", "audio", "source"]):
                    src = str(node.get("src") or "")
                    node.attrs.pop("srcset", None)   # 多分辨率候选无法内联，直接去掉
                    if src.startswith("data:"):
                        continue
                    asset = local_assets.get(src)
                    validation = validate_downloaded_asset(self.config, asset) if asset else {'ok': False}
                    if not validation['ok']:
                        node.attrs.pop("src", None)
                        node["alt"] = node.get("alt") or "图片未能保存"
                        node["data-archive-missing"] = "1"
                        skipped += 1
                        continue
                    # 使用同一文件描述符读出并校验的字节，不能再按路径重读。
                    body = validation['body']
                    if used + len(body) > budget:
                        node.attrs.pop("src", None)
                        node["alt"] = "图片过大，未在预览中载入"
                        node["data-archive-skipped"] = "1"
                        skipped += 1
                        continue
                    mime = mimetypes.guess_type(validation['path'].name)[0] or "application/octet-stream"
                    node["src"] = f"data:{mime};base64," + base64.b64encode(body).decode("ascii")
                    used += len(body)
                    inlined += 1

                return {
                    "key": key,
                    "title": meta.get("title") or record.get("title") or "无标题",
                    "date": meta.get("content_date") or record.get("list_date") or "",
                    "source": meta.get("source") or record.get("list_source") or "",
                    "tags": (meta.get("tags") or []) + (meta.get("content_tags") or []),
                    "note": meta.get("note") or "",
                    "detail_url": record.get("detail_url") or "",
                    "warnings": meta.get("warnings") or [],
                    "html": str(soup),
                    "images_inlined": inlined,
                    "images_missing": skipped,
                }
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:200]}

    def open_bookmark(self, key: str) -> bool:
        """Open only an address preserved in the current archive catalog."""
        import webbrowser

        if not key or not self.config.db_path.is_file():
            return False
        with ArchiveState(self.config.db_path) as state:
            record = state.get_record(key)
        url = bookmark_url(record) if record else None
        return bool(url and webbrowser.open(url, new=2))

    def get_task(self) -> dict[str, Any] | None:
        try:
            return self._app().snapshot()
        except Exception:  # noqa: BLE001
            return None

    def start_task(self, kind: str, keys: list[str] | None = None, **options: Any) -> dict[str, Any]:
        payload = {"kind": kind, "keys": keys or [], **options}
        try:
            return {"ok": True, "job": self._app().start(payload)}
        except Exception as exc:  # noqa: BLE001 —— 任何失败都要原样回显给用户
            return {"ok": False, "error": str(exc)[:200]}

    def stop_task(self) -> dict[str, Any] | None:
        try:
            return self._app().stop()
        except Exception:  # noqa: BLE001
            return None

    def retry_images(self, asset_ids: list[int]) -> dict[str, Any]:
        return self.start_task("retry_images", asset_ids=asset_ids)

    # ---------- 凭证 ----------
    def start_login(self) -> dict[str, Any]:
        """打开百度登录窗口。登录由用户本人在窗口里完成，程序只取结果 Cookie。

        立即返回；结果通过主窗口的 onLoginResult() 回调推给界面。
        """
        from .login import open_login_window

        def done(payload: dict[str, Any]) -> None:
            if payload.get("ok"):
                self._application = None          # 凭证变了，Application 需重建
                _notify("登录成功", "凭证已保存到本地")
            if self.window:
                import json as _json

                try:
                    self.window.evaluate_js(f"onLoginResult({_json.dumps(payload, ensure_ascii=False)})")
                except Exception:  # noqa: BLE001
                    pass

        try:
            open_login_window(self.config, on_result=done, user_agent=self.config.user_agent)
            return {"ok": True, "opened": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}

    def import_curl_text(self, text: str) -> dict[str, Any]:
        """粘贴 cURL 导入（扫码失败时的兜底）。校验由 Application.save_session 负责。"""
        try:
            self._app().save_session({"curl": text or ""})
            self._application = None
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}

    def check_connection(self) -> dict[str, Any]:
        """实测连通性：真发一次列表请求，而不是只看 Cookie 在不在。"""
        return self.start_task("connection")

    def credential_state(self) -> dict[str, Any]:
        from .auth import load_credentials

        creds = load_credentials()
        return {
            "present": creds.present,
            "logged_in": creds.looks_logged_in(),
            "source": creds.source,
            "cookie_names": creds.cookie_names(),      # 只回名字，不回值
        }

    # ---------- 导出 ----------
    def _export_directory(self, use_archive_directory: bool) -> str | None:
        if use_archive_directory is True:
            directory = self.config.root / "exports"
            directory.mkdir(parents=True, exist_ok=True)
            return str(directory.resolve())
        return self.choose_directory()

    def export_to(self, what: str, keys: list[str] | None = None,
                  use_archive_directory: bool = False) -> dict[str, Any]:
        """原生目录选择器 + 实际导出。``what`` 为 selection / images。"""
        if what not in {"selection", "images"}:
            return {"ok": False, "error": "未知导出格式"}
        directory = self._export_directory(use_archive_directory)
        if not directory:
            return {"ok": False, "cancelled": True}
        from uuid import uuid4

        from .selection import export_images, export_selection

        target = Path(directory) / f"{what}-{uuid4().hex[:8]}.zip"
        try:
            with ArchiveLock(self.config.root), ArchiveState(self.config.db_path) as state:
                chosen = list(keys or [])
                if not chosen:
                    chosen = (_saved_article_keys(self.config, state) if what == "selection" else
                              [r["key"] for r in state.iter_records("page")])
                if what == "images":
                    result = export_images(self.config, state, keys=chosen, output=target)
                else:
                    # export_selection 只接受 md/txt，与操作台（server.py）的调用保持一致。
                    # 早先这里漏传 formats，导致「导出所选文章」每次都抛 TypeError。
                    result = export_selection(self.config, state, keys=chosen,
                                              formats=["md", "txt"], output=target)
            missing = result.get("missing_resources", result.get("not_downloaded", 0))
            _notify("部分导出" if missing else "导出完成", f"{target.name} 已保存")
            return {"ok": True, "path": str(target), **(result or {})}
        except Exception as exc:  # noqa: BLE001
            _notify("导出失败", str(exc)[:120])
            return {"ok": False, "error": str(exc)[:200]}

    def export_all(self, use_archive_directory: bool = False) -> dict[str, Any]:
        """一键导出全部：生成一个可以长期保存、离线打开的完整归档包。

        用户最在意的就是这一件事 —— 把所有东西拿出来，百度关了也能看。
        产物是一个文件夹而不是 ZIP，双击里面的「打开归档.html」就能浏览。

            百度空间归档-20261003-1530/
              打开归档.html          ← 双击即可浏览（按年份、可搜索、图片在本地）
              index.html             同上（站点入口）
              articles/              每篇一页，图片已随附
              文本版-Markdown与TXT.zip
              WordPress导入.xml
              总目录.csv / 总目录.json   全部记录，保留原始顺序
              说明.txt

        只读取本地已校验通过的内容，不发任何网络请求；损坏或缺失的图片不会被
        打进包里，但原始 URL 仍保留在正文与总目录中。
        """
        directory = self._export_directory(use_archive_directory)
        if not directory:
            return {"ok": False, "cancelled": True}

        job = self.get_task()
        if job and job.get("status") in {"running", "starting", "stopping"}:
            return {"ok": False, "error": "有任务正在进行，请等它结束或先停止后再导出"}

        import shutil
        from dataclasses import replace
        from datetime import datetime
        from tempfile import mkdtemp

        from .export import export_wxr
        from .pipeline import export_master_index
        from .selection import export_preservation, export_selection
        from .site import export_reading_site
        from .utils import sha256_file

        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        package = Path(directory) / f"百度空间归档-{stamp}"
        suffix = 1
        while package.exists():                  # 绝不覆盖已有导出
            suffix += 1
            package = Path(directory) / f"百度空间归档-{stamp}-{suffix}"
        building = None
        report: dict[str, Any] = {"articles": 0, "warnings": []}
        resources: dict = {'schema_version': 1, 'included': [], 'missing': [], 'missing_articles': []}
        try:
            with ArchiveLock(self.config.root), ArchiveState(self.config.db_path) as state:
                building = Path(mkdtemp(prefix=f".{package.name}-", suffix=".partial", dir=directory))
                # 1) 可离线浏览的阅读站 —— 包的核心
                report["articles"] = export_reading_site(self.config, state, building, resource_report=resources)
                report["missing_resources"] = len(resources['missing'])
                write_json_atomic(building / "缺失与资源清单.json", resources)
                entry = building / "index.html"
                if entry.exists():
                    shutil.copyfile(entry, building / "打开归档.html")

                # 2) Markdown / TXT
                pages = _saved_article_keys(self.config, state)
                if pages:
                    try:
                        export_selection(self.config, state, keys=pages, formats=["md", "txt"],
                                         output=building / "文本版-Markdown与TXT.zip")
                    except Exception as exc:  # noqa: BLE001 —— 派生格式失败不应毁掉整个包
                        report["warnings"].append(f"Markdown/TXT 未生成：{exc}"[:160])

                # 3) WordPress 迁移包
                try:
                    export_wxr(self.config, state, filename=str(building.resolve() / "WordPress导入.xml"))
                except Exception as exc:  # noqa: BLE001
                    report["warnings"].append(f"WordPress 导入包未生成：{exc}"[:160])

                # 4) 总目录：全部记录（含外部收藏），保留原始顺序
                export_master_index(replace(self.config, root=building), state)
                for src, dst in (("master_index.csv", "总目录.csv"), ("master_index.json", "总目录.json")):
                    (building / src).rename(building / dst)
                (building / 'crawl_state.json').unlink()
                export_preservation(self.config, state, building / "原始文件与版本.zip")

                total = state.count_records()

            optional = {
                '文本版-Markdown与TXT.zip': '已保存文章的 Markdown、TXT、通过校验的媒体及缺失清单',
                'WordPress导入.xml': '文章迁移文件。媒体仍引用原站；可用资源和本地路径见「缺失与资源清单.json」',
            }
            (building / "说明.txt").write_text(
                "百度空间归档 · 导出包\n"
                f"生成时间：{datetime.now():%Y-%m-%d %H:%M}\n\n"
                "打开归档.html          双击即可浏览全部文章（按年份分组、可搜索、图片在本地）\n"
                "articles/              每篇文章一页，图片随附\n"
                "总目录.csv / .json     全部收藏记录（含外部收藏），保留百度列表的原始顺序\n"
                "缺失与资源清单.json     缺失原因、原始地址线索及已归档媒体的本地路径与摘要\n"
                "原始文件与版本.zip      原始响应、正文、历史文件及数据库；仅供私下保存和恢复\n"
                "导出校验清单.json       本包文件的字节数和 SHA-256\n"
                + "".join(f"{name}  {detail}\n" for name, detail in optional.items() if (building / name).is_file())
                + "".join(f"未生成：{warning}\n" for warning in report['warnings'])
                + "\n"
                f"共 {total} 条记录，其中 {report['articles']} 篇文章可阅读。\n"
                f"另有 {len(resources['missing'])} 个媒体引用、{len(resources['missing_articles'])} 篇正文未包含在阅读版。\n"
                "阅读版使用已校验的本地文件，并为缺失媒体添加可读说明。原始证据另外保留。\n",
                encoding="utf-8",
            )
            files = [{'path': path.relative_to(building).as_posix(), 'bytes': path.stat().st_size,
                      'sha256': sha256_file(path)} for path in sorted(building.rglob('*')) if path.is_file()]
            write_json_atomic(building / "导出校验清单.json", {'schema_version': 1, 'files': files})
            building.rename(package)             # 全部写完才发布，中途失败不会留下半成品
        except Exception as exc:  # noqa: BLE001
            if building is not None:
                shutil.rmtree(building, ignore_errors=True)
            _notify("导出失败", str(exc)[:120])
            return {"ok": False, "error": str(exc)[:200]}

        partial = report['missing_resources'] or resources['missing_articles'] or report['warnings']
        _notify("部分导出" if partial else "导出完成", f"{report['articles']} 篇文章已导出到 {package.name}")
        _reveal(package / "打开归档.html" if (package / "打开归档.html").exists() else package)
        return {"ok": True, "path": str(package), **report,
                "missing_articles": len(resources['missing_articles'])}

    # ---------- 健康检查与离线修复 ----------
    def _busy(self) -> bool:
        job = self.get_task()
        return bool(job and job.get("status") in {"running", "starting", "stopping"})

    def health(self) -> dict[str, Any]:
        """告诉界面「归档现在有什么需要处理」，并给出对应的处理方式。

        区分三类：可离线修复的（旧数据待校验、索引待核实）、需要联网的
        （正文没下载、登录失效）、无法挽回的（图片所在网站已失效）。
        """
        from .truth import index_truth, resource_state

        report: dict[str, Any] = {
            "has_db": self.config.db_path.exists(),
            "ignored_settings": dict(self.config.ignored_settings),
            "settings_warnings": [],
            "needs_offline_repair": False,
        }
        if self.config.concurrency > 3:
            report["settings_warnings"].append(f"并发数 {self.config.concurrency} 高于建议值 3")
        if self.config.delay < 0.5:
            report["settings_warnings"].append(f"请求间隔 {self.config.delay}s 低于建议值 0.8s")
        if not report["has_db"]:
            return report
        try:
            if database_version(self.config.db_path) < SCHEMA_VERSION:
                return {**report, "needs_offline_repair": True}
            with ArchiveState(self.config.db_path) as state:
                index = index_truth(state)
                legacy_assets = sum(
                    1 for a in state.all_assets()
                    if a.get("download_status") == "ok" and resource_state(self.config, a)["state"] == "legacy_unverified"
                )
                raw_pages = len([p for p in self.config.lists_dir.glob("list_start*.json")
                                 if not p.name.endswith((".meta.json", ".latest.json"))])
            report.update({
                "index_ok": index["ok"],
                "index_reason": index["reason"],
                "legacy_assets": legacy_assets,
                "raw_list_pages": raw_pages,
                # 有已存的原始列表页，就能离线重建索引证明；有旧资源就能离线补证
                "needs_offline_repair": bool(legacy_assets or (not index["ok"] and raw_pages)),
            })
        except Exception as exc:  # noqa: BLE001
            report["error"] = str(exc)[:200]
        return report

    def prepare_archive(self) -> dict[str, Any]:
        """打开软件时自动调用：旧版本留下的归档在这里悄悄迁移，用户不需要做任何事。

        从零开始下载的归档不会走到迁移分支 —— 新流程在下载时就写好了全部校验记录。
        只有用旧版本代码下载过的归档才需要补齐，补齐全程离线；动手前先备份数据库。
        """
        if not self.config.db_path.exists():
            return {"state": "empty", "migrated": False}
        if self._busy():
            return {"state": "busy", "migrated": False}
        report = self.health()
        if report.get("error"):
            return {"state": "error", "migrated": False, "error": report["error"]}
        if not report.get("needs_offline_repair"):
            return {"state": "ready", "migrated": False}

        result = self.repair_offline()
        return {"state": "ready" if result.get("ok") else "error", "migrated": bool(result.get("ok")),
                "backup": result.get("backup"), "error": result.get("error"),
                "after": result.get("after")}

    def repair_offline(self) -> dict[str, Any]:
        """离线整理旧版本归档。只读写本地记录，从不下载、也从不改写任何下载结果。

        旧版本抓下来的内容都还在磁盘上，只是缺少新版要求的校验记录：
          1. 证明索引完整：复用已保存的原始列表页（不联网）
          2. 补证：逐个核对已下载图片的字节，补写校验记录、重建本地阅读版
          3. 对齐状态：让每篇文章在数据库里的状态等于真相层的判断

        这里刻意不调用 run_assets：它是「下载并自我修复」的函数，会把校验未通过的
        图片（例如图床占位图）先作废再重新下载。早先的实现在离线整理里调了它，
        结果 111 条原本下载成功的记录被离线客户端拒绝后改写成了失败。
        """
        if self._busy():
            return {"ok": False, "error": "有任务正在进行，请等它结束后再整理"}
        from .legacy import rebuild_legacy_evidence
        from .pipeline import run_index

        steps: dict[str, Any] = {}
        backup = None
        client = _offline_client(self.config)
        quiet = lambda *_a, **_k: None  # noqa: E731 —— 整理过程不需要逐条输出
        try:
            with ArchiveLock(self.config.root), ArchiveState(self.config.db_path) as state:
                backup = state.schema_backup or backup_before_migration(self.config.db_path)
                before = self.get_verified_stats()
                try:
                    run_index(self.config, state, client, force=False, log=quiet)
                    steps["index"] = "ok"
                except Exception as exc:  # noqa: BLE001 —— 原始页不全时会想联网，离线客户端会拦下
                    steps["index"] = f"原始列表页不完整，下次「检查更新」时会重新读取（{type(exc).__name__}）"
                steps["legacy"] = rebuild_legacy_evidence(self.config, state)
                steps["reconciled"] = _reconcile_statuses(self.config, state)
                state.set_meta("migrated_at", utcnow_iso())
                state.set_meta("migration_backup", str(backup))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200], "steps": steps,
                    "backup": str(backup) if backup else None}
        finally:
            client.close()

        self._application = None
        after = self.get_verified_stats()
        return {"ok": True, "before": before, "after": after, "steps": steps, "backup": str(backup)}

    def get_problems(self) -> dict[str, Any]:
        """按「原因」而不是按「状态码」分组，每组配上对症的处理方式。"""
        from .assets import image_retry_urls
        from .storage import current_content_path, read_json
        from .truth import article_truth, is_source_placeholder, resource_state
        from .utils import redact_url

        groups: dict[str, list[dict[str, Any]]] = {
            "baidu_deleted": [],   # 百度图床删了原图，只给「图片不存在」占位图
            "dead_images": [],     # 图片请求失败，或本地文件无法通过校验
            "not_saved": [],       # 正文没下载下来：可以重新下载
            "auth": [],            # 登录失效导致的失败：重新登录后再下载
            "unverified": [],      # 旧数据待校验：一键离线修复
            "external_embeds": [],
        }
        if not self.config.db_path.exists():
            return groups
        with ArchiveState(self.config.db_path) as state:
            for record in state.iter_records("page"):
                key = record["key"]
                item = state.get_item(key) or {}
                facts = article_truth(self.config, state, key)
                base = {"key": key, "title": record.get("title") or "无标题",
                        "date": record.get("list_date") or ""}
                if item.get("status") == "auth_error":
                    groups["auth"].append(base)
                    continue
                if not facts["available"]:
                    groups["not_saved"].append({**base, "error": (item.get("last_error") or "")[:120]})
                    continue
                dead, deleted, unverified, embeds = [], [], [], []
                source = None
                base_url = None
                for asset in state.assets_for(key):
                    if asset.get("kind") in {"embed", "object", "iframe"}:
                        embeds.append({"url": redact_url(asset.get("resolved_url") or asset["original_url"]),
                                       "domain": asset.get("domain") or "", "reason": "旧站嵌入无法离线播放"})
                        continue
                    if asset.get("kind") != "image":
                        continue
                    resource = resource_state(self.config, asset)
                    if resource["state"] == "legacy_unverified":
                        unverified.append({"domain": asset.get("domain") or "", "reason": resource["reason"]})
                        continue
                    if resource["state"] == "verified":
                        continue
                    if not (asset.get("download_attempted") or asset.get("download_status") == "ok"
                            or (self.config.download_images and asset.get("downloadable"))):
                        continue
                    if source is None:
                        folder = self.config.item_dir(key)
                        metadata = read_json(folder / 'metadata.json', {}) or {}
                        base_url = metadata.get('iframe_url_original') or metadata.get('detail_url')
                        source = current_content_path(folder, prefer_local=False).read_text(encoding='utf-8')
                    entry = {
                        "id": asset["id"],
                        "url": redact_url(asset.get("original_url") or ""),
                        "domain": asset.get("domain") or "",
                        "reason": _plain_failure({**asset, "validation_reason": resource["reason"]}),
                        "retryable": bool(image_retry_urls(source, base_url, asset, resource["reason"])),
                    }
                    # 字节已下载、结构也合法，却被识别为占位图 = 百度删了原图
                    is_placeholder = is_source_placeholder(asset, resource["reason"])
                    (deleted if is_placeholder else dead).append(entry)
                if deleted:
                    groups["baidu_deleted"].append({**base, "images": deleted})
                if dead:
                    groups["dead_images"].append({**base, "images": dead})
                if unverified:
                    groups["unverified"].append({**base, "images": unverified})
                if embeds:
                    groups["external_embeds"].append({**base, "images": embeds})
        return groups

    def get_images(self) -> list[dict[str, Any]]:
        """图片页数据：只列校验通过的本地图片。缩略图另由 get_thumbnail 按需生成。"""
        from .truth import validate_downloaded_asset

        out: list[dict[str, Any]] = []
        if not self.config.db_path.exists():
            return out
        with ArchiveState(self.config.db_path) as state:
            for record in state.iter_records("page"):
                for asset in state.assets_for(record["key"]):
                    if asset.get("kind") != "image" or asset.get("download_status") != "ok":
                        continue
                    if not validate_downloaded_asset(self.config, asset)["ok"]:
                        continue
                    out.append({
                        "id": asset["id"], "key": record["key"],
                        "title": record.get("title") or "无标题",
                        "date": record.get("list_date") or "",
                    })
        return out

    def get_thumbnail(self, asset_id: int, size: int = 320) -> str | None:
        """从已校验字节生成缩略图，缓存按内容和尺寸区分且限制内存占用。"""
        if (type(asset_id) is not int or asset_id <= 0 or type(size) is not int or size <= 0
                or not self.config.db_path.exists()):
            return None
        size = min(size, THUMBNAIL_MAX_SIZE)
        import base64
        import io

        from PIL import Image

        from .truth import validate_downloaded_asset

        with ArchiveState(self.config.db_path) as state:
            asset = state.get_asset(asset_id)
        validation = (validate_downloaded_asset(self.config, asset)
                      if asset and asset.get('kind') == 'image' else {'ok': False})
        # 即使缓存命中，也必须先核对当前资源关系和磁盘字节。
        if not validation['ok']:
            with self._thumb_lock:
                for key in list(self._thumbs):
                    if key[0] == asset_id:
                        del self._thumbs[key]
            return None
        cache_key = (asset_id, size, asset['sha256'], asset.get('content_type'), validation['path'].suffix)
        with self._thumb_lock:
            if cache_key in self._thumbs:
                self._thumbs.move_to_end(cache_key)
                return self._thumbs[cache_key]
        body = validation['body']
        try:
            with Image.open(io.BytesIO(body)) as image:
                image.thumbnail((size, size))
                if image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")
                buffer = io.BytesIO()
                image.save(buffer, "JPEG", quality=82)
            result = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode('ascii')
        except Exception:  # noqa: BLE001 —— GIF/SVG 等无法缩略时退回原图
            import mimetypes
            if len(body) > 2 * 1024 * 1024:
                return None
            mime = mimetypes.guess_type(validation['path'].name)[0] or "image/png"
            result = f"data:{mime};base64," + base64.b64encode(body).decode('ascii')
        with self._thumb_lock:
            self._thumbs[cache_key] = result
            self._thumbs.move_to_end(cache_key)
            while (len(self._thumbs) > THUMBNAIL_CACHE_ENTRIES
                   or sum(len(value) for value in self._thumbs.values()) > THUMBNAIL_CACHE_BYTES):
                self._thumbs.popitem(last=False)
        return result

    def reset_recommended_settings(self) -> dict[str, Any]:
        """把并发与请求间隔恢复为对旧服务友好的推荐值。"""
        if self._busy():
            return {"ok": False, "error": "有任务正在进行，请等它结束后再修改"}
        try:
            from .config import BAIDU_PAGE_LIMIT

            settings = self._app().save_settings({"concurrency": 2, "delay": 0.8})
            self.config.concurrency, self.config.delay = 2, 0.8
            self.config.page_limit = BAIDU_PAGE_LIMIT
            self.config.ignored_settings.pop("page_limit", None)
            return {"ok": True, "settings": settings}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}

    # ---------- 退出登录 ----------
    def logout(self) -> dict[str, Any]:
        """删除本地保存的百度 Cookie。已归档的内容完全不受影响。"""
        job = self.get_task()
        if job and job.get("status") in {"running", "starting", "stopping"}:
            return {"ok": False, "error": "有任务正在进行，请先停止再退出登录"}
        from .auth import clear_credentials

        try:
            result = clear_credentials()
        except OSError as exc:
            return {"ok": False, "error": str(exc)[:200]}
        self._application = None
        return {"ok": True, **result}

    # ---------- 系统集成 ----------
    def choose_directory(self) -> str | None:
        """原生目录选择器。"""
        if not self.window:
            return None
        result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        return result[0] if result else None

    def choose_file(self) -> str | None:
        if not self.window:
            return None
        result = self.window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=("文本文件 (*.txt;*.curl;*.sh)", "所有文件 (*.*)"),
        )
        return result[0] if result else None

    def confirm(self, title: str, message: str) -> bool:
        """原生确认框，而不是网页 confirm()。"""
        if not self.window:
            return False
        return bool(self.window.create_confirmation_dialog(title, message))

    def notify(self, title: str, message: str) -> None:
        _notify(title, message)

    def reveal_archive(self) -> None:
        self.config.root.mkdir(parents=True, exist_ok=True)
        _reveal(self.config.root.resolve())

    def get_theme(self) -> str:
        mode = _load_prefs().get("theme")
        return mode if mode in ("light", "dark", "system") else "system"

    def set_theme(self, mode: str) -> None:
        if mode not in ("light", "dark", "system"):
            return
        _update_prefs(theme=mode)


def _build_menu(api: Api) -> list:
    """原生菜单栏。macOS 会把它挂到屏幕顶端的系统菜单条上。"""
    def open_archive() -> None:
        api.reveal_archive()

    def show_about() -> None:
        if api.window:
            api.window.create_confirmation_dialog(
                "关于",
                "百度空间归档\n\nRAW-first、可恢复、可验证的本地数字档案工具。\n只读访问，不修改远端任何内容。",
            )

    return [
        Menu("文件", [
            MenuAction("打开归档目录", open_archive),
            MenuSeparator(),
            MenuAction("关于", show_about),
        ]),
        Menu("视图", [
            MenuAction("刷新数据", lambda: api.window and api.window.evaluate_js("refresh()")),
        ]),
    ]


def launch(config: Config | None = None, *, debug: bool = False) -> None:
    """打开原生窗口。"""
    import socket
    from time import monotonic, sleep

    from webview.http import BottleServer

    class DesktopServer(BottleServer):
        @classmethod
        def start_server(cls, *args, **kwargs):
            address, root, server = super().start_server(*args, **kwargs)
            # pywebview starts serving on a background thread. WKWebView can
            # request the first page before that thread has bound the port.
            deadline = monotonic() + 5
            while True:
                try:
                    with socket.create_connection(('127.0.0.1', server.port), timeout=0.25):
                        return address, root, server
                except OSError as exc:
                    if monotonic() >= deadline:
                        raise RuntimeError('桌面界面的本地服务未能启动') from exc
                    sleep(0.025)

    config = config or load_config()
    api = Api(config)
    prefs = _load_prefs()

    width = int(prefs.get("width") or WINDOW_DEFAULT[0])
    height = int(prefs.get("height") or WINDOW_DEFAULT[1])

    window = webview.create_window(
        "百度空间归档",
        url=str(UI_DIR / "index.html"),
        js_api=api,
        width=width,
        height=height,
        min_size=WINDOW_MIN,
        resizable=True,
        # 没有地址栏、没有标签页 —— pywebview 的窗口本身就不带任何浏览器外壳
    )
    api.window = window

    def remember_geometry(*_args: Any) -> None:
        try:
            _update_prefs(width=window.width, height=window.height)
        except Exception:  # noqa: BLE001
            pass

    window.events.resized += remember_geometry
    window.events.closing += remember_geometry

    icon_name = {"darwin": "icon.icns", "win32": "icon.ico"}.get(sys.platform, "icon.png")
    webview.start(menu=_build_menu(api), debug=debug, icon=str(UI_DIR / icon_name), server=DesktopServer)


if __name__ == "__main__":  # pragma: no cover
    launch(debug="--debug" in sys.argv)
