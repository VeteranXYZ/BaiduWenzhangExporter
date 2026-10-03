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
