"""命令行入口。

日志中绝不完整打印 Cookie / bdstoken / sign —— 所有 URL 经 redact_url() 处理。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .auth import AuthError, import_curl, load_credentials
from .client import ArchiveClient, AuthExpired, FetchError
from .config import LIST_URL_CANDIDATES, Config, load_config
from .parsers import parse_content_html, parse_detail_html, parse_list_html, parse_list_json
from .pipeline import export_master_index, run_fetch, run_index, run_parse
from .assets import run_assets
from .export import run_export
from .state import ArchiveState
from .verify import run_verify
from .utils import redact_url, truncate, utcnow_iso


class Log:
    """终端 + 文件双写。文件日志同样是脱敏的。"""

    def __init__(self, logs_dir: Path, verbose: bool = False):
        self.verbose = verbose
        logs_dir.mkdir(parents=True, exist_ok=True)
        self.path = logs_dir / f"archive-{datetime.now().strftime('%Y%m%d')}.log"
        self._handle = open(self.path, "a", encoding="utf-8")

    def __call__(self, tag: str, message: str) -> None:
        line = f"[{tag}] {message}"
        print(line, flush=True)
        self._handle.write(f"{utcnow_iso()} {line}\n")
        self._handle.flush()

    def close(self) -> None:
        try:
            self._handle.close()
        except OSError:
            pass


def _open(args) -> tuple[Config, ArchiveState, Log]:
    config = load_config(root=args.root, config_file=args.config)
    if getattr(args, "concurrency", None):
        config.concurrency = args.concurrency
    if getattr(args, "delay", None) is not None:
        config.delay = args.delay
    config.ensure_dirs()
    return config, ArchiveState(config.db_path), Log(config.logs_dir, getattr(args, "verbose", False))


def _client(config: Config, log: Log, *, require_auth: bool = True) -> ArchiveClient:
    credentials = load_credentials()
    if require_auth and not credentials.present:
        raise AuthError(
            "未找到凭证。请任选一种方式提供：\n"
            "  1) 在浏览器 DevTools → Network 中右键列表请求 → Copy as cURL，存成文件后运行：\n"
            "     baidu-archive import-curl <文件路径>\n"
            "  2) export BAIDU_ARCHIVE_COOKIE='完整的 Cookie header 串'\n"
            "  3) 把 Cookie header 串写入 secrets/cookies.txt"
        )
    if credentials.present and not credentials.looks_logged_in():
        log("AUTH", "警告：Cookie 中未见 BDUSS，可能不是有效登录态")
    log("AUTH", f"凭证来源：{credentials.source}，cookie 名：{', '.join(credentials.cookie_names()) or '无'}")
    return ArchiveClient(config, credentials, logger=log)


# ==========================================================================
# 子命令
# ==========================================================================

def cmd_import_curl(args) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"文件不存在：{path}", file=sys.stderr)
        return 2
    try:
        info = import_curl(path.read_text(encoding="utf-8"))
    except AuthError as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 2
    print("✅ 已写入 secrets/session.json（权限 0600，已被 .gitignore 覆盖）")
    print(f"   列表 URL 模板：{info['list_url_template_redacted']}")
    print(f"   Cookie 名：{', '.join(info['cookie_names']) or '无'}")
    print(f"   登录态判定：{'看起来有效' if info['looks_logged_in'] else '⚠️ 未见 BDUSS，可能无效'}")
    print("\n下一步：baidu-archive doctor")
    return 0


def cmd_doctor(args) -> int:
    """体检：配置、凭证、端点连通性。可选自动探测列表端点。"""
    config, state, log = _open(args)
    try:
        credentials = load_credentials()
        print(f"版本：baidu_archive {__version__}")
        print(f"归档目录：{config.root.resolve()}")
        print(f"凭证来源：{credentials.source}")
        print(f"Cookie：{'已提供 (' + str(len(credentials.cookie_names())) + ' 项)' if credentials.present else '❌ 缺失'}")
        if credentials.present:
            print(f"登录态：{'看起来有效' if credentials.looks_logged_in() else '⚠️ 未见 BDUSS'}")
        print(f"列表 URL 模板：{redact_url(config.list_url_template) if config.list_url_template else '❌ 未配置'}")

        if not credentials.present:
            print("\n请先提供凭证，见 README「提供 Cookie」一节。")
            return 1

        client = _client(config, log)
        try:
            if config.list_url_template:
                url = config.list_url(0, config.page_limit)
                result = client.probe(url)
                print(f"\n探测已配置端点：HTTP {result.get('status')} json={result.get('looks_json')}")
                if result.get("ok"):
                    parsed = parse_list_json(client.get(url, expect="json").content)
                    print(f"✅ totalCount = {parsed['total_count']}，本页 {parsed['current_count']} 条")
                    state.set_meta("total_count", parsed["total_count"])
                    return 0
                print(f"   样本：{truncate(result.get('sample', ''), 120)}")

            if args.probe or not config.list_url_template:
                print("\n自动探测候选端点：")
                for candidate in LIST_URL_CANDIDATES:
                    url = candidate.format(start=0, limit=config.page_limit)
                    result = client.probe(url)
                    mark = "✅" if result.get("ok") else "  "
                    print(f" {mark} HTTP {result.get('status','—')} {redact_url(candidate)}")
                    if result.get("ok"):
                        config.list_url_template = candidate
                        state.set_meta("discovered_list_url", candidate)
                        print("\n找到可用端点。请把它写进 config.json：")
                        print(f'  {{"list_url_template": "{candidate}"}}')
                        return 0
                print(
                    "\n❌ 候选端点均不可用。这是预期情况 —— 请用 import-curl 从真实请求捕获：\n"
                    "   DevTools → Network → 滚动列表 → 右键那条返回 JSON 的请求 → Copy as cURL\n"
                    "   存成文件后运行：baidu-archive import-curl <文件>"
                )
                return 1
            return 0
        finally:
            client.close()
    finally:
        state.close()
        log.close()


def cmd_dry_run(args) -> int:
    """最小链路验证：list → key → detail → iframe → content。不全量抓。"""
    config, state, log = _open(args)
    try:
        client = _client(config, log)
        try:
            log("DRY", "第 1 步：列表接口")
            url = config.list_url(0, config.page_limit)
            response = client.get(url, expect="json")
            parsed = parse_list_json(response.content)
            log("DRY", f"✅ totalCount={parsed['total_count']} limit={parsed['limit']} 本页={parsed['current_count']}")

            log("DRY", "第 2 步：解析列表 HTML")
            records = parse_list_html(parsed["html"], offset=parsed["start"], site_root=config.site_root)
            types: dict[str, int] = {}
            for record in records:
                types[record["type"]] = types.get(record["type"], 0) + 1
            with_key = sum(1 for r in records if r.get("key"))
            log("DRY", f"✅ 解析 {len(records)} 条，含 key {with_key} 条，类型 {types}")
            if with_key == 0:
                log("DRY", "❌ 一条 key 都没解析出来，列表 HTML 结构可能已变更")
                return 1

            target = None
            if args.key:
                target = {"key": args.key, "type": "page",
                          "detail_url": config.detail_url(args.key, "page"), "title": "(指定 key)"}
            else:
                for record in records:
                    if record["type"] == "page" and record.get("key"):
                        target = record
                        break
            if not target:
                log("DRY", "⚠️ 本页没有 page 类型记录，无法验证正文链路（可用 --key 指定）")
                return 1

            key = target["key"]
            log("DRY", f"第 3 步：详情页 key={key} {truncate(target.get('title') or '', 30)}")
            detail_url = target.get("detail_url") or config.detail_url(key, "page")
            detail = client.get(detail_url, expect="html")
            info = parse_detail_html(detail.text, base_url=detail_url)
            if not info.get("iframe_url"):
                log("DRY", "❌ 详情页未找到 iframe")
                return 1
            log("DRY", f"✅ iframe: {redact_url(info['iframe_url'])}")

            log("DRY", "第 4 步：正文页")
            content = client.get(info["iframe_url"], expect="html")
            parsed_content = parse_content_html(content.text)
            log("DRY", f"✅ 标题={parsed_content['title']!r}")
            log("DRY", f"   正文长度={parsed_content['content_length']} 字符，"
                       f"HTML={parsed_content['content_html_length']} 字符")
            log("DRY", f"   收藏于={parsed_content['content_date']} 来源={parsed_content['source']!r}")

            from .parsers import extract_assets

            assets = extract_assets(parsed_content["content_html"] or "", base_url=info["iframe_url"])
            images = [a for a in assets if a["kind"] == "image"]
            log("DRY", f"   资源 {len(assets)} 个（图片 {len(images)}）")

            ok = bool(parsed_content["has_container"] and parsed_content["content_length"] > 0)
            log("DRY", "✅ 全链路验证通过，可以运行 `baidu-archive all`" if ok
                       else "⚠️ 链路通但正文为空 —— 请人工确认这篇文章本身是否有内容")
            return 0 if ok else 1
        finally:
            client.close()
    finally:
        state.close()
        log.close()


def cmd_index(args) -> int:
    config, state, log = _open(args)
    run_id = state.start_run("index")
    try:
        client = _client(config, log)
        try:
            stats = run_index(config, state, client, log=log, force=args.force, max_pages=args.max_pages)
            state.finish_run(run_id, stats)
            return 0
        finally:
            client.close()
    except AuthExpired as exc:
        log("AUTH", str(exc))
        return 3
    finally:
        state.close()
        log.close()


def cmd_fetch(args) -> int:
    config, state, log = _open(args)
    run_id = state.start_run("fetch")
    try:
        client = _client(config, log)
        try:
            stats = run_fetch(config, state, client, log=log, force=args.force, limit=args.limit,
                              only_keys=args.key or None)
            state.finish_run(run_id, stats)
            return 3 if stats.get("aborted") else 0
        finally:
            client.close()
    finally:
        export_master_index(config, state)
        state.close()
        log.close()


def cmd_parse(args) -> int:
    config, state, log = _open(args)
    run_id = state.start_run("parse")
    try:
        stats = run_parse(config, state, log=log, force=args.force, limit=args.limit)
        state.finish_run(run_id, stats)
        return 0
    finally:
        state.close()
        log.close()


def cmd_assets(args) -> int:
    config, state, log = _open(args)
    run_id = state.start_run("assets")
    try:
        client = _client(config, log)
        try:
            stats = run_assets(config, state, client, log=log, force=args.force, limit=args.limit,
                               include_media=args.include_media)
            state.finish_run(run_id, stats)
            return 3 if stats.get("aborted") else 0
        finally:
            client.close()
    finally:
        state.close()
        log.close()


def cmd_export(args) -> int:
    config, state, log = _open(args)
    try:
        formats = args.format or ["md", "txt"]
        if "all" in formats:
            formats = ["md", "txt", "wxr", "site"]
        run_export(config, state, formats=formats, log=log, site_dir=Path(args.site_dir) if args.site_dir else None)
        return 0
    finally:
        state.close()
        log.close()


def cmd_verify(args) -> int:
    config, state, log = _open(args)
    try:
        summary = run_verify(config, state, log=log)
        print()
        print((config.reports_dir / "summary.md").read_text(encoding="utf-8"))
        complete = summary.get("index_complete")
        pages = summary["baidu_space_pages"]
        return 0 if (complete is not False and pages["failed"] == 0) else 1
    finally:
        state.close()
        log.close()


def cmd_retry_failed(args) -> int:
    config, state, log = _open(args)
    try:
        targets = state.items_by_status(["failed", "auth_error", "pending", "warning"])
        if not targets:
            log("RETRY", "没有需要重试的条目")
            return 0
        keys = [t["key"] for t in targets]
        log("RETRY", f"待重试 {len(keys)} 条")
        client = _client(config, log)
        try:
            client.reset_auth_streak()
            stats = run_fetch(config, state, client, log=log, force=True, only_keys=keys)
            run_parse(config, state, log=log, force=True)
            run_assets(config, state, client, log=log, force=False)
            return 3 if stats.get("aborted") else 0
        finally:
            client.close()
    finally:
        state.close()
        log.close()


def cmd_status(args) -> int:
    config, state, log = _open(args)
    try:
        total = state.get_meta("total_count")
        print(f"归档目录：{config.root.resolve()}")
        print(f"API totalCount：{total}")
        print(f"已索引：{state.count_records()}")
        print(f"类型：{state.type_counts()}")
        print(f"状态：{state.status_counts()}")
        print(f"资源：{state.asset_counts()}")
        failures = state.all_failures()
        print(f"失败记录：{len(failures)}")
        for failure in failures[:10]:
            print(f"  - key={failure['key']} {failure['stage']}: {truncate(failure['error'] or '', 80)}")
        return 0
    finally:
        state.close()
        log.close()


def cmd_all(args) -> int:
    for step, func in (
        ("index", cmd_index), ("fetch", cmd_fetch), ("parse", cmd_parse),
        ("assets", cmd_assets), ("export", cmd_export), ("verify", cmd_verify),
    ):
        print(f"\n{'='*64}\n  {step.upper()}\n{'='*64}")
        code = func(args)
        if code == 3:
            print(f"\n❌ 在 {step} 阶段因认证失效中止。换新 Cookie 后重新运行即可续传。")
            return 3
        if code != 0 and step in {"index", "fetch"}:
            print(f"\n⚠️ {step} 阶段返回 {code}，继续后续阶段（已抓到的数据不受影响）。")
    return 0


# ==========================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="baidu-archive",
        description="百度空间历史内容抢救 / 归档工具（只读，不修改远端任何内容）",
    )
    parser.add_argument("--version", action="version", version=f"baidu_archive {__version__}")
    parser.add_argument("--root", default=None, help="归档目录（默认 archive/）")
    parser.add_argument("--config", default=None, help="配置文件（默认 config.json）")
    parser.add_argument("--concurrency", type=int, default=None, help="并发数（默认 2）")
    parser.add_argument("--delay", type=float, default=None, help="请求最小间隔秒（默认 0.8）")
    parser.add_argument("--verbose", action="store_true")

    subparsers = parser.add_subparsers(dest="command", required=True)

    sub = subparsers.add_parser("import-curl", help="从 DevTools cURL 导入端点与凭证")
    sub.add_argument("file", help="保存了 cURL 命令的文件")
    sub.set_defaults(func=cmd_import_curl)

    sub = subparsers.add_parser("doctor", help="体检：配置/凭证/端点连通性")
    sub.add_argument("--probe", action="store_true", help="自动探测候选列表端点")
    sub.set_defaults(func=cmd_doctor)

    sub = subparsers.add_parser("dry-run", help="最小链路验证，不全量抓取")
    sub.add_argument("--key", default=None, help="指定要验证的 key")
    sub.set_defaults(func=cmd_dry_run)

    sub = subparsers.add_parser("index", help="建立全部记录的 master index")
    sub.add_argument("--force", action="store_true", help="忽略已有 raw，强制重取")
    sub.add_argument("--max-pages", type=int, default=None)
    sub.set_defaults(func=cmd_index)

    sub = subparsers.add_parser("fetch", help="抓取 detail + content 原始 HTML")
    sub.add_argument("--force", action="store_true")
    sub.add_argument("--limit", type=int, default=None)
    sub.add_argument("--key", action="append", default=None, help="只抓指定 key（可重复）")
    sub.set_defaults(func=cmd_fetch)

    sub = subparsers.add_parser("parse", help="从 raw/ 重建 metadata 与正文（离线，不发请求）")
    sub.add_argument("--force", action="store_true")
    sub.add_argument("--limit", type=int, default=None)
    sub.set_defaults(func=cmd_parse)

    sub = subparsers.add_parser("assets", help="下载资源并生成 content.local.html")
    sub.add_argument("--force", action="store_true")
    sub.add_argument("--limit", type=int, default=None)
    sub.add_argument("--include-media", action="store_true", default=None, help="同时下载视频/音频等")
    sub.set_defaults(func=cmd_assets)

    sub = subparsers.add_parser("export", help="导出 md / txt / wxr / site")
    sub.add_argument("--format", action="append", choices=["md", "txt", "wxr", "site", "all"])
    sub.add_argument("--site-dir", default=None)
    sub.set_defaults(func=cmd_export)

    sub = subparsers.add_parser("verify", help="完整性校验与报告")
    sub.set_defaults(func=cmd_verify)

    sub = subparsers.add_parser("retry-failed", help="只重试失败/警告/未完成的条目")
    sub.set_defaults(func=cmd_retry_failed)

    sub = subparsers.add_parser("status", help="当前进度速览")
    sub.set_defaults(func=cmd_status)

    sub = subparsers.add_parser("all", help="index → fetch → parse → assets → export → verify")
    sub.add_argument("--force", action="store_true")
    sub.add_argument("--limit", type=int, default=None)
    sub.add_argument("--max-pages", type=int, default=None)
    sub.add_argument("--key", action="append", default=None)
    sub.add_argument("--include-media", action="store_true", default=None)
    sub.add_argument("--format", action="append", choices=["md", "txt", "wxr", "site", "all"])
    sub.add_argument("--site-dir", default=None)
    sub.set_defaults(func=cmd_all)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except AuthError as exc:
        print(f"\n❌ 凭证问题：\n{exc}", file=sys.stderr)
        return 2
    except AuthExpired as exc:
        print(f"\n❌ {exc}", file=sys.stderr)
        return 3
    except FetchError as exc:
        print(f"\n❌ 请求失败：{exc}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("\n已中断。已成功归档的内容不受影响，重新运行同一命令即可续传。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
