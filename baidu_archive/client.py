"""HTTP 客户端。只发 GET，保守限速，分级重试，主动识别认证失效。"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .auth import Credentials
from .config import Config
from .parsers import looks_like_login_page
from .utils import redact_url


class AuthExpired(RuntimeError):
    """连续多次出现认证特征 —— 立即停止任务，避免把几百条污染成假失败。"""


class FetchError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, attempts: int = 0):
        super().__init__(message)
        self.status = status
        self.attempts = attempts


@dataclass
class Response:
    url: str
    status_code: int
    content: bytes
    headers: dict[str, str]
    elapsed: float
    attempts: int

    @property
    def text(self) -> str:
        """老页面编码混乱，一律按 UTF-8 宽松解码，原始字节仍存于 content。"""
        encoding = None
        content_type = self.headers.get("content-type", "")
        if "charset=" in content_type:
            encoding = content_type.split("charset=")[-1].split(";")[0].strip().strip('"')
        for candidate in (encoding, "utf-8", "gb18030"):
            if not candidate:
                continue
            try:
                return self.content.decode(candidate)
            except (UnicodeDecodeError, LookupError):
                continue
        return self.content.decode("utf-8", "replace")


class RateLimiter:
    """全局串行化的最小请求间隔，带抖动。低并发下比令牌桶更可预测。"""

    def __init__(self, delay: float, jitter: float):
        self.delay = max(0.0, delay)
        self.jitter = max(0.0, jitter)
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_at:
                time.sleep(self._next_at - now)
                now = time.monotonic()
            self._next_at = now + self.delay + random.uniform(0, self.jitter)


class ArchiveClient:
    """只读 HTTP 客户端。所有对外日志中的 URL 均已打码。"""

    def __init__(self, config: Config, credentials: Credentials, *, logger=None):
        self.config = config
        self.credentials = credentials
        self.logger = logger
        self.limiter = RateLimiter(config.delay, config.jitter)
        self._auth_streak = 0
        self._streak_lock = threading.Lock()

        headers = {
            "User-Agent": credentials.user_agent or config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": credentials.referer or config.referer,
            "Cache-Control": "no-cache",
        }
        headers.update({k.title(): v for k, v in (credentials.extra_headers or {}).items()})
        headers.update(config.extra_headers or {})
        if credentials.cookie_header:
            headers["Cookie"] = credentials.cookie_header

        self._client = httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(
                connect=config.connect_timeout,
                read=config.read_timeout,
                write=config.read_timeout,
                pool=config.connect_timeout,
            ),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=max(2, config.concurrency * 2)),
        )

    # ------------------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ArchiveClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _log(self, tag: str, message: str) -> None:
        if self.logger:
            self.logger(tag, message)

    def _note_auth(self, suspicious: bool) -> None:
        """连续计数：一次可疑不下结论，连续 N 次才判定 session 失效。"""
        with self._streak_lock:
            if suspicious:
                self._auth_streak += 1
                streak = self._auth_streak
            else:
                self._auth_streak = 0
                return
        if streak >= self.config.auth_failure_threshold:
            raise AuthExpired(
                f"连续 {streak} 次请求出现认证失效特征，已停止任务。"
                "请重新导出 Cookie 后再运行（已成功归档的内容不受影响）。"
            )

    def reset_auth_streak(self) -> None:
        with self._streak_lock:
            self._auth_streak = 0

    # ------------------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        expect: str = "html",
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> Response:
        """带重试的 GET。

        ``expect`` 为 ``json`` 时额外校验 JSON 结构；为 ``html`` 时检查登录页特征。
        """
        retries = self.config.max_retries if max_retries is None else max_retries
        safe_url = redact_url(url)
        last_error: str = "未知错误"
        last_status: int | None = None

        for attempt in range(1, retries + 1):
            self.limiter.wait()
            started = time.monotonic()
            try:
                request_timeout = (
                    httpx.Timeout(
                        connect=self.config.connect_timeout,
                        read=timeout,
                        write=timeout,
                        pool=self.config.connect_timeout,
                    )
                    if timeout
                    else None
                )
                raw = self._client.get(url, timeout=request_timeout) if request_timeout else self._client.get(url)
            except httpx.TimeoutException as exc:
                last_error = f"超时: {type(exc).__name__}"
                last_status = None
            except httpx.HTTPError as exc:
                last_error = f"传输失败: {type(exc).__name__}: {exc}"
                last_status = None
            else:
                elapsed = time.monotonic() - started
                status = raw.status_code
                last_status = status
                headers = {k.lower(): v for k, v in raw.headers.items()}

                if status in (401, 403):
                    # 403 不疯狂重试：这更可能是权限/认证问题而非瞬时故障
                    self._note_auth(True)
                    if attempt >= 2:
                        raise FetchError(f"HTTP {status}（疑似认证/权限问题）", status=status, attempts=attempt)
                    last_error = f"HTTP {status}"
                elif status == 429 or 500 <= status < 600:
                    last_error = f"HTTP {status}"
                    self._note_auth(False)
                elif status >= 400:
                    # 4xx（404 等）是确定性的，重试无意义
                    self._note_auth(False)
                    raise FetchError(f"HTTP {status}", status=status, attempts=attempt)
                else:
                    body = raw.content
                    text_sample = None
                    suspicious = False
                    if expect == "html":
                        text_sample = body[:20000].decode("utf-8", "replace")
                        suspicious = looks_like_login_page(text_sample)
                    elif expect == "json":
                        text_sample = body[:2000].decode("utf-8", "replace").lstrip()
                        suspicious = looks_like_login_page(text_sample) or not text_sample.startswith(("{", "["))
                    self._note_auth(suspicious)
                    if suspicious:
                        last_error = "响应疑似登录页/非预期结构（HTTP 200）"
                    else:
                        return Response(
                            url=url,
                            status_code=status,
                            content=body,
                            headers=headers,
                            elapsed=elapsed,
                            attempts=attempt,
                        )

            if attempt < retries:
                backoff = self.config.backoff_base * (2 ** (attempt - 1))
                backoff += random.uniform(0, self.config.jitter)
                self._log("RETRY", f"{safe_url} — {last_error}（第 {attempt}/{retries} 次），{backoff:.1f}s 后重试")
                time.sleep(backoff)

        raise FetchError(f"{last_error}（已重试 {retries} 次）", status=last_status, attempts=retries)

    def probe(self, url: str) -> dict[str, Any]:
        """轻量探测，不重试、不抛错。用于 doctor 自动探测列表端点。"""
        try:
            self.limiter.wait()
            raw = self._client.get(url)
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "url_redacted": redact_url(url)}
        sample = raw.content[:2000].decode("utf-8", "replace").lstrip()
        return {
            "ok": raw.status_code == 200 and sample.startswith("{"),
            "status": raw.status_code,
            "content_type": raw.headers.get("content-type", ""),
            "looks_json": sample.startswith("{"),
            "looks_login": looks_like_login_page(sample),
            "url_redacted": redact_url(url),
            "sample": sample[:200],
        }
