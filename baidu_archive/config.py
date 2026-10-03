"""配置加载。优先级：命令行 > 环境变量 > config.json > 内置默认值。

本模块不持有任何凭证，凭证一律由 auth.py 处理。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SITE_ROOT = "https://wenzhang.baidu.com"

# 首选来源是 ``import-curl``（从 DevTools 实际请求捕获）；这里的候选仅用于 doctor 自动探测。
# 第一条由用户从 DevTools 实际请求中捕获（wd/type 为搜索与类型筛选，留空即全部；
# _ 是缓存穿透参数，每次请求现取），已通过真实归档验证。
# 后两条是早期在拿到真实 URL 之前的猜测，仅作兜底探测。
LIST_URL_CANDIDATES = (
    f"{SITE_ROOT}/fav/list?start={{start}}&limit={{limit}}&wd=&type=&getListHtml=1&_={{ts}}",
    f"{SITE_ROOT}/api/getlist?start={{start}}&limit={{limit}}",
    f"{SITE_ROOT}/page/getlist?start={{start}}&limit={{limit}}",
)

DETAIL_URL_TEMPLATE = f"{SITE_ROOT}/page/view?key={{key}}"
ARTICLE_URL_TEMPLATE = f"{SITE_ROOT}/article/view?key={{key}}"


@dataclass
class Config:
    """运行期配置。所有路径都在 ``root`` 之下。"""

    root: Path = field(default_factory=lambda: Path("archive"))
    site_root: str = SITE_ROOT
    list_url_template: str | None = None
    detail_url_template: str = DETAIL_URL_TEMPLATE
    article_url_template: str = ARTICLE_URL_TEMPLATE
    user_agent: str = DEFAULT_USER_AGENT
    referer: str = f"{SITE_ROOT}/"
    page_limit: int = 30

    # 请求策略 —— 面对遗留系统，可靠性优先于速度。
    concurrency: int = 2
    delay: float = 0.8
    jitter: float = 0.6
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    max_retries: int = 4
    backoff_base: float = 1.5

    # 连续多少个 item 出现认证特征就中止整个任务，避免几百条被污染成假失败。
    auth_failure_threshold: int = 5

    # 资源下载
    download_images: bool = True
    download_other_media: bool = False
    max_asset_bytes: int = 64 * 1024 * 1024
    asset_timeout: float = 45.0

    extra_headers: dict[str, str] = field(default_factory=dict)

    # ---- 派生路径 ----
    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def lists_dir(self) -> Path:
        return self.raw_dir / "lists"

    @property
    def detail_dir(self) -> Path:
        return self.raw_dir / "detail_pages"

    @property
    def content_dir(self) -> Path:
        return self.raw_dir / "content_pages"

    @property
    def items_dir(self) -> Path:
        return self.root / "items"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def db_path(self) -> Path:
        return self.root / "state.db"

    def ensure_dirs(self) -> None:
        for path in (
            self.root,
            self.lists_dir,
            self.detail_dir,
            self.content_dir,
            self.items_dir,
            self.reports_dir,
            self.logs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def item_dir(self, key: str) -> Path:
        from .utils import safe_key_dirname

        return self.items_dir / safe_key_dirname(key)

    def detail_url(self, key: str, record_type: str = "page") -> str:
        template = self.article_url_template if record_type == "article" else self.detail_url_template
        return template.format(key=key)

    def list_url(self, start: int, limit: int | None = None) -> str:
        if not self.list_url_template:
            raise RuntimeError(
                "列表接口 URL 未配置。请先运行 `baidu-archive import-curl <file>` 导入，"
                "或用 `baidu-archive doctor --probe` 自动探测。"
            )
        # ts 始终提供：模板里没有 {ts} 时 format 会忽略它
        return self.list_url_template.format(
            start=start, limit=limit or self.page_limit, ts=int(time.time() * 1000)
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        data["root"] = str(self.root)
        return data


def _coerce(value: str, current):
    if isinstance(current, bool):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    return value


def load_config(root: str | os.PathLike[str] | None = None, config_file: str | os.PathLike[str] | None = None) -> Config:
    """加载配置。``config.json`` 与环境变量 ``BAIDU_ARCHIVE_*`` 均为可选。"""
    cfg = Config()
    if root:
        cfg.root = Path(root)

    candidate = Path(config_file) if config_file else Path("config.json")
    if candidate.exists():
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"配置文件解析失败 {candidate}: {exc}") from exc
        for key, value in raw.items():
            if key == "root":
                if not root:
                    cfg.root = Path(value)
            elif hasattr(cfg, key):
                setattr(cfg, key, value)

    # 由 import-curl 写出的会话文件同时携带真实的列表 URL 与请求头。
    session_path = Path("secrets/session.json")
    if session_path.exists():
        try:
            session = json.loads(session_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            session = {}
        if session.get("list_url_template") and not cfg.list_url_template:
            cfg.list_url_template = session["list_url_template"]
        if session.get("user_agent"):
            cfg.user_agent = session["user_agent"]
        if session.get("referer"):
            cfg.referer = session["referer"]
        for name, value in (session.get("extra_headers") or {}).items():
            cfg.extra_headers.setdefault(name, value)

    for name in list(vars(cfg)):
        env_name = f"BAIDU_ARCHIVE_{name.upper()}"
        if env_name in os.environ and os.environ[env_name] != "":
            current = getattr(cfg, name)
            if name == "root":
                if not root:
                    cfg.root = Path(os.environ[env_name])
            elif isinstance(current, (dict, list)):
                continue
            else:
                setattr(cfg, name, _coerce(os.environ[env_name], current))

    return cfg
