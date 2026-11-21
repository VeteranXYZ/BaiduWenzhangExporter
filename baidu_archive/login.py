"""浏览器内登录：打开百度登录页，用户自行扫码或输入账密，程序只取结果 Cookie。

安全边界（重要）：
* 程序**从不**代填账号、密码或验证码。登录全程在窗口里由用户本人完成。
* 只在检测到登录成功后读取 Cookie，随即关闭登录窗口。
* Cookie 只写入 ``secrets/session.json``（权限 0600），不打印、不进日志。
* 不触碰百度的登录协议 —— 用的就是百度自己的登录页，它改版也不影响这里。
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlsplit

if TYPE_CHECKING:
    import webview

from .auth import AuthError, Credentials, save_cookie_session
from .config import LIST_URL_CANDIDATES, Config

# 直接打开收藏列表页：未登录会被重定向到百度登录页（自带扫码入口），
# 登录成功后又会跳回来 —— 跳回来这一刻拿到的正是 wenzhang 域下可用的 Cookie。
ENTRY_URL = "https://wenzhang.baidu.com/"
SUCCESS_HOST = "wenzhang.baidu.com"

WINDOW_SIZE = (560, 720)


def cookie_header_from(cookies: Any) -> str:
    """把 pywebview 返回的 Cookie 列表拼成 Cookie 请求头。

    macOS 后端返回 SimpleCookie 列表（含 httponly 的 BDUSS）；
    其他后端可能返回 dict，两种都兼容。
    """
    parts: list[str] = []
    seen: set[str] = set()
    for item in cookies or []:
        if hasattr(item, "keys"):          # SimpleCookie / dict
            for name in item.keys():
                value = item[name]
                value = getattr(value, "value", value)
                if name and name not in seen:
                    seen.add(name)
                    parts.append(f"{name}={value}")
        else:                               # 退化形态：(name, value)
            try:
                name, value = item
            except (TypeError, ValueError):
                continue
            if name not in seen:
                seen.add(name)
                parts.append(f"{name}={value}")
    return "; ".join(parts)


def default_list_url_template(config: Config) -> str | None:
    """扫码拿不到列表 URL，用已配置的；没有就退回已知端点。"""
    return config.list_url_template or (LIST_URL_CANDIDATES[0] if LIST_URL_CANDIDATES else None)


def open_login_window(
    config: Config,
    *,
    on_result: Callable[[dict], None] | None = None,
    user_agent: str | None = None,
) -> webview.Window:
    """打开登录窗口并在成功后保存凭证。立即返回，结果通过 ``on_result`` 回调。"""
    import webview

    window = webview.create_window(
        "登录百度",
        url=ENTRY_URL,
        width=WINDOW_SIZE[0],
        height=WINDOW_SIZE[1],
        min_size=(420, 560),
        resizable=True,
    )
    finished = threading.Event()

    def report(payload: dict) -> None:
        if finished.is_set():
            return
        finished.set()
        if on_result:
            try:
                on_result(payload)
            except Exception:  # noqa: BLE001 —— 回调出错不该让登录窗口卡住
                pass

    def check(*_args: Any) -> None:
        """每次页面加载后检查是否已登录。未登录就什么都不做，等用户继续操作。"""
        if finished.is_set():
            return
        try:
            current = urlsplit(window.get_current_url() or "")
            if (current.scheme != "https" or current.hostname != SUCCESS_HOST
                    or current.port not in (None, 443) or current.username is not None):
                return
        except Exception:  # noqa: BLE001
            return
        try:
            header = cookie_header_from(window.get_cookies())
        except Exception:  # noqa: BLE001
            return
        if not Credentials(cookie_header=header).looks_logged_in():
            return                        # 页面到了但还没拿到 BDUSS
        try:
            summary = save_cookie_session(
                header,
                list_url_template=default_list_url_template(config),
                user_agent=user_agent,
                referer=ENTRY_URL,
            )
        except AuthError as exc:
            report({"ok": False, "error": str(exc)})
            return
        report({"ok": True, **summary})
        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass

    def closed(*_args: Any) -> None:
        report({"ok": False, "cancelled": True})

    window.events.loaded += check
    window.events.closed += closed
    return window
