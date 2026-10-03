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
from pathlib import Path
from typing import Any

import webview
from webview.menu import Menu, MenuAction, MenuSeparator

from .config import Config, load_config
from .state import ArchiveState
from .truth import article_truth, index_truth, validate_downloaded_asset, resource_state

UI_DIR = Path(__file__).parent / "ui"

WINDOW_MIN = (900, 620)
WINDOW_DEFAULT = (1180, 780)


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
        return json.loads(_prefs_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_prefs(data: dict[str, Any]) -> None:
    try:
        _prefs_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
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

    # ---------- 数据 ----------
    EMPTY_STATS = {
        "total_api": 0, "indexed": 0, "page": 0, "article": 0, "other": 0,
        "complete": 0, "warning": 0, "failed": 0, "pending": 0,
        "legacy_unverified": 0, "assets_legacy_unverified": 0,
        "assets_downloaded": 0, "assets_failed": 0, "historical_indexed": 0,
        "index_complete": None, "db_size": "—", "has_db": False, "verified": False,
    }

    def _empty(self) -> dict[str, Any]:
        return {**self.EMPTY_STATS, "root": str(self.config.root.resolve())}

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
                try:
                    db_bytes = self.config.db_path.stat().st_size
                except OSError:
                    db_bytes = 0
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

                budget = 24 * 1024 * 1024          # 单篇内联上限，避免超长图文撑爆 WebView
                used = 0
                inlined = skipped = 0
                for node in soup.find_all(["img", "video", "audio", "source"]):
                    src = str(node.get("src") or "")
                    node.attrs.pop("srcset", None)   # 多分辨率候选无法内联，直接去掉
                    if src.startswith("data:"):
                        continue
                    target = folder / src
                    if not (src.startswith("assets/") and len(Path(src).parts) == 2
                            and target.is_file()):
                        node.attrs.pop("src", None)
                        node["alt"] = node.get("alt") or "图片未能保存"
                        node["data-archive-missing"] = "1"
                        skipped += 1
                        continue
                    try:
                        body = target.read_bytes()
                    except OSError:
                        node.attrs.pop("src", None)
                        node["data-archive-missing"] = "1"
                        skipped += 1
                        continue
                    if used + len(body) > budget:
                        node.attrs.pop("src", None)
                        node["alt"] = "图片过大，未在预览中载入"
                        node["data-archive-skipped"] = "1"
                        skipped += 1
                        continue
                    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
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
    def export_to(self, what: str, keys: list[str] | None = None) -> dict[str, Any]:
        """原生目录选择器 + 实际导出。``what`` 为 selection / images。"""
        directory = self.choose_directory()
        if not directory:
            return {"ok": False, "cancelled": True}
        from uuid import uuid4

        from .selection import export_images, export_selection

        target = Path(directory) / f"{what}-{uuid4().hex[:8]}.zip"
        try:
            with ArchiveState(self.config.db_path) as state:
                chosen = list(keys or [])
                if not chosen:
                    chosen = [r["key"] for r in state.iter_records("page")]
                if what == "images":
                    result = export_images(self.config, state, keys=chosen, output=target)
                else:
                    # export_selection 只接受 md/txt，与操作台（server.py）的调用保持一致。
                    # 早先这里漏传 formats，导致「导出所选文章」每次都抛 TypeError。
                    result = export_selection(self.config, state, keys=chosen,
                                              formats=["md", "txt"], output=target)
            _notify("导出完成", f"{target.name} 已保存")
            return {"ok": True, "path": str(target), **(result or {})}
        except Exception as exc:  # noqa: BLE001
            _notify("导出失败", str(exc)[:120])
            return {"ok": False, "error": str(exc)[:200]}

    def export_all(self) -> dict[str, Any]:
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
        directory = self.choose_directory()
        if not directory:
            return {"ok": False, "cancelled": True}

        job = self.get_task()
        if job and job.get("status") in {"running", "starting", "stopping"}:
            return {"ok": False, "error": "有任务正在进行，请等它结束或先停止后再导出"}

        import shutil
        from datetime import datetime

        from .export import export_wxr
        from .pipeline import export_master_index
        from .selection import export_selection
        from .site import export_reading_site

        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        package = Path(directory) / f"百度空间归档-{stamp}"
        suffix = 1
        while package.exists():                  # 绝不覆盖已有导出
            suffix += 1
            package = Path(directory) / f"百度空间归档-{stamp}-{suffix}"
        building = package.with_name("." + package.name + ".partial")
        shutil.rmtree(building, ignore_errors=True)
        building.mkdir(parents=True)

        report: dict[str, Any] = {"articles": 0, "warnings": []}
        try:
            with ArchiveState(self.config.db_path) as state:
                # 1) 可离线浏览的阅读站 —— 包的核心
                report["articles"] = export_reading_site(self.config, state, building)
                entry = building / "index.html"
                if entry.exists():
                    shutil.copyfile(entry, building / "打开归档.html")

                # 2) Markdown / TXT
                pages = [r["key"] for r in state.iter_records("page")]
                if pages:
                    try:
                        export_selection(self.config, state, keys=pages, formats=["md", "txt"],
                                         output=building / "文本版-Markdown与TXT.zip")
                    except Exception as exc:  # noqa: BLE001 —— 派生格式失败不应毁掉整个包
                        report["warnings"].append(f"Markdown/TXT 未生成：{exc}"[:160])

                # 3) WordPress 迁移包
                try:
                    wxr = export_wxr(self.config, state)
                    shutil.copyfile(wxr, building / "WordPress导入.xml")
                except Exception as exc:  # noqa: BLE001
                    report["warnings"].append(f"WordPress 导入包未生成：{exc}"[:160])

                # 4) 总目录：全部记录（含外部收藏），保留原始顺序
                export_master_index(self.config, state)
                for src, dst in (("master_index.csv", "总目录.csv"), ("master_index.json", "总目录.json")):
                    if (self.config.root / src).exists():
                        shutil.copyfile(self.config.root / src, building / dst)

                total = state.count_records()

            (building / "说明.txt").write_text(
                "百度空间归档 · 导出包\n"
                f"生成时间：{datetime.now():%Y-%m-%d %H:%M}\n\n"
                "打开归档.html          双击即可浏览全部文章（按年份分组、可搜索、图片在本地）\n"
                "articles/              每篇文章一页，图片随附\n"
                "文本版-Markdown与TXT.zip  纯文本格式，便于搜索或迁移到 Obsidian、Hugo 等\n"
                "WordPress导入.xml      在 WordPress 后台「工具 → 导入 → WordPress」使用\n"
                "总目录.csv / .json     全部收藏记录（含外部收藏），保留百度列表的原始顺序\n\n"
                f"共 {total} 条记录，其中 {report['articles']} 篇文章可阅读。\n"
                "本包只包含校验通过的内容；未能保存的图片在正文中显示为占位，原始地址仍保留。\n"
                "整个文件夹不依赖网络，百度服务关闭后依然可以打开。\n",
                encoding="utf-8",
            )
            building.rename(package)             # 全部写完才发布，中途失败不会留下半成品
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(building, ignore_errors=True)
            _notify("导出失败", str(exc)[:120])
            return {"ok": False, "error": str(exc)[:200]}

        _notify("导出完成", f"{report['articles']} 篇文章已导出到 {package.name}")
        _reveal(package / "打开归档.html" if (package / "打开归档.html").exists() else package)
        return {"ok": True, "path": str(package), **report}

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

    def set_theme(self, mode: str) -> None:
        prefs = _load_prefs()
        prefs["theme"] = mode
        _save_prefs(prefs)


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
            saved = _load_prefs()
            saved.update({"width": window.width, "height": window.height})
            _save_prefs(saved)
        except Exception:  # noqa: BLE001
            pass

    window.events.resized += remember_geometry
    window.events.closing += remember_geometry

    webview.start(menu=_build_menu(api), debug=debug)


if __name__ == "__main__":  # pragma: no cover
    launch(debug="--debug" in sys.argv)
