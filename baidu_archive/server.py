"""仅监听回环地址的本地操作台，复用归档核心，不暴露 raw 或会话文件。"""
from __future__ import annotations

import copy
import html
import json
import mimetypes
import re
import secrets
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from uuid import uuid4

from .assets import run_assets
from .auth import AuthError, import_curl, load_credentials, parse_curl
from .client import ArchiveClient, AuthExpired
from .config import Config
from .locking import ArchiveBusy, ArchiveLock
from .parsers import parse_list_json, make_soup, parse_srcset
from .pipeline import export_master_index, run_fetch, run_index, run_parse
from .selection import SelectionError, export_images, export_selection
from .site import reading_html
from .state import ArchiveState
from .storage import IntegrityError, current_content_path, read_json, save_raw, write_json_atomic
from .tasks import TaskControl, TaskStopped
from .utils import utcnow_iso
from .verify import run_verify, validation_result
from .truth import article_truth, index_truth, validate_downloaded_asset, validated_reading_content, resource_state

WEB = Path(__file__).parent / 'dashboard'
ACTIVE = {'running', 'stopping'}
# sync 是面向用户的唯一主任务：读列表 → 下载正文 → 整理 → 下载图片，一次走完。
# 其余种类保留给命令行、测试和「重新下载某几篇」这类局部操作。
LABELS = {'sync': '下载全部', 'index': '更新收藏列表', 'archive': '抓取文章', 'assets': '补齐图片',
          'export': '导出所选文章', 'export_images': '导出文章图片',
          'verify': '校验归档', 'connection': '验证百度连接'}


class InputError(ValueError):
    pass


class Application:
    def __init__(self, config: Config, config_file: Path | None = None):
        self.config = config
        self.config_file = config_file or Path('config.json')
        self.config.ensure_dirs()
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.control = TaskControl()
        self.thread = None
        self.job_path = config.root / 'ui-task.json'
        self.job = read_json(self.job_path, None)
        if self.job and self.job.get('status') in ACTIVE:
            self.job.update(status='stopped', message='上次服务已停止，可以继续任务。')
        self.connection = {'state': 'saved' if load_credentials().present else 'missing', 'checked_at': None}

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.job)

    def update(self, **changes):
        with self.lock:
            self.job.update(changes)
            write_json_atomic(self.job_path, self.job)

    def log(self, tag, message):
        # 前端只看简短、已脱敏的阶段信息；不返回网络响应、会话信息或任意异常文本。
        if tag == 'AUTH':
            message = '登录状态需要重新验证'
        elif tag == 'FAIL':
            message = '当前条目未完成，已保留可恢复进度'
        with self.lock:
            self.job['events'] = (self.job['events'] + [{'at': utcnow_iso(), 'tag': tag, 'message': message}])[-60:]
            match = re.match(r'(\d+)/(\d+)', message)
            if match:
                self.job['done'], self.job['total'] = map(int, match.groups())
            self.job['message'] = message

    def library(self):
        records = []
        with ArchiveState(self.config.db_path) as state:
            all_assets = state.all_assets()
            asset_counts = {}
            for asset in all_assets:
                if asset['kind'] != 'image':
                    continue
                counts = asset_counts.setdefault(asset['key'], {'images': 0, 'downloaded': 0, 'failed': 0, 'legacy_unverified': 0, 'legacy_saved': 0})
                counts['images'] += 1
                resource = resource_state(self.config, asset)
                valid = resource['state'] == 'verified'
                counts['legacy_unverified'] += resource['state'] == 'legacy_unverified'
                counts['legacy_saved'] += resource['state'] == 'legacy_unverified' and resource['saved']
                counts['downloaded'] += valid
                counts['failed'] += bool(asset['download_attempted'] and not valid and resource['state'] != 'legacy_unverified')
            for record in state.iter_records():
                key = record['key']
                meta = read_json(self.config.item_dir(key) / 'metadata.json', {}) or {}
                facts = article_truth(self.config, state, key)
                available = facts['available']
                records.append({'key': key, 'title': meta.get('title') or record.get('title') or '无标题',
                                'date': meta.get('content_date') or record.get('list_date') or '',
                                'type': record['type'], 'status': facts['status'],
                                'available': available, 'position': record.get('global_position'),
                                'excerpt': (record.get('list_desc') or '')[:140],
                                **asset_counts.get(key, {'images': 0, 'downloaded': 0, 'failed': 0, 'legacy_unverified': 0, 'legacy_saved': 0})})
            history = [{'command': r['command'], 'started_at': r['started_at'],
                        'finished_at': r['finished_at'], 'outcome': r['stats'].get('outcome') or
                        ('completed' if r['finished_at'] else 'interrupted'),
                        'download': r['stats'].get('download') if re.fullmatch(
                            r'/download/(?:selection|images)-[a-f0-9]{32}\.zip', r['stats'].get('download', '')) else None}
                       for r in state.recent_runs(8)]
            return {'records': records, 'total': index_truth(state)['expected'],
                    'index_generation': index_truth(state),
                    'indexed_at': state.get_meta('indexed_at'), 'history': history,
                    'connection': self.connection.copy(), 'settings': self.settings(),
                    'summary': {'indexed': index_truth(state)['indexed'], 'historical_indexed': len(records), 'pages': sum(r['type'] == 'page' for r in records),
                                'ready': sum(r['available'] for r in records),
                                'images': sum(r['images'] for r in records),
                                'downloaded': sum(r['downloaded'] for r in records),
                                'legacy_images': sum(r['legacy_unverified'] for r in records),
                                'legacy_saved_images': sum(r['legacy_saved'] for r in records),
                                'failed_images': sum(r['failed'] for r in records)}}

    def settings(self):
        return {**{k: getattr(self.config, k) for k in ('concurrency', 'delay', 'page_limit')},
                'directory': str(self.config.root.resolve())}

    def save_settings(self, data):
        values = {}
        # page_limit 不再可调：百度接口固定每页 30 条，改了只会让索引永远无法核实。
        from .config import MAX_CONCURRENCY, MIN_DELAY
        for name, low, high, kind in [('concurrency', 1, MAX_CONCURRENCY, int), ('delay', MIN_DELAY, 10, float)]:
            value = data.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise InputError(f'{name} 需在 {low}–{high} 范围内')
            if kind is int and int(value) != value:
                raise InputError('并发数必须为整数')
            values[name] = kind(value)
        with self.lock, ArchiveLock(self.config.root):
            if self.job and self.job['status'] in ACTIVE:
                raise ArchiveBusy('任务结束后再修改设置')
            stored = read_json(self.config_file, {})
            stored.update(values)
            write_json_atomic(self.config_file, stored)
            self.config = replace(self.config, **values)
        return self.settings()

    def save_session(self, data):
        text = data.get('curl', '')
        if not isinstance(text, str):
            raise InputError('请输入收藏列表请求的 cURL')
        parsed = parse_curl(text)
        url = urlparse(parsed['url'])
        if url.scheme != 'https' or url.hostname != 'wenzhang.baidu.com' or url.path != '/fav/list':
            raise InputError('请选择百度收藏 /fav/list 请求')
        if 'BDUSS=' not in parsed['cookie_header']:
            raise InputError('请求没有登录 Cookie，请登录百度后重新复制')
        with self.lock, ArchiveLock(self.config.root):
            if self.job and self.job['status'] in ACTIVE:
                raise ArchiveBusy('请先停止当前任务')
            import_curl(text)
            from .auth import build_list_url_template
            self.config.list_url_template = build_list_url_template(parsed['url'])
            self.connection = {'state': 'saved', 'checked_at': None}
        return {'saved': True}

    def start(self, data):
        kind = data.get('kind')
        if kind not in LABELS:
            raise InputError('未知任务')
        args = {'rebuild_legacy': data.get('rebuild_legacy', False), 'kind': kind, 'keys': [], 'images': data.get('images', True), 'force': data.get('force', False)}
        if not isinstance(args['images'], bool) or not isinstance(args['force'], bool) or not isinstance(args['rebuild_legacy'], bool):
            raise InputError('抓取选项无效')
        with self.lock:
            if self.job and self.job['status'] in ACTIVE:
                raise ArchiveBusy('已有任务进行中，请等待完成或先停止')
            guard = ArchiveLock(self.config.root)
            guard.__enter__()
            try:
                if kind in {'archive', 'assets', 'export', 'export_images'}:
                    keys = data.get('keys')
                    if kind == 'export_images':
                        scope = data.get('scope', 'selected')
                        if scope not in {'all', 'selected'}:
                            raise InputError('请选择全部文章或已选文章')
                        if scope == 'all':
                            if keys:
                                raise InputError('全部图片导出不能同时指定文章')
                            with ArchiveState(self.config.db_path) as state:
                                keys = [r['key'] for r in state.iter_records() if r['type'] == 'page']
                        # 保存实际文章范围；继续任务时沿用当时的选择。
                        args['scope'] = 'selected'
                    if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
                        raise InputError('请先勾选至少一篇空间文章')
                    with ArchiveState(self.config.db_path) as state:
                        known = {r['key']: r for r in state.iter_records()}
                        if any(k not in known or known[k]['type'] != 'page' for k in keys):
                            raise InputError('所选内容必须是本人收藏中的空间文章')
                        if kind == 'export' and any(not current_content_path(self.config.item_dir(k)).is_file() for k in keys):
                            raise InputError('选择中有未保存的正文，请先抓取或仅选择已保存文章')
                        if kind == 'export_images' and not any(a['kind'] == 'image' and a['download_status'] == 'ok'
                                                               for k in keys for a in state.assets_for(k)):
                            raise InputError('所选文章没有已下载的图片，请先抓取或补齐图片')
                    args['keys'] = list(dict.fromkeys(keys))
                keys = args["keys"]
                endpoint = (self.config.list_url(0) if self.config.list_url_template else self.config.site_root + '/fav/list') if kind in {'connection', 'index'} else self.config.detail_url(keys[0]) if keys else self.config.site_root + '/fav/list'
                if kind == 'sync':
                    endpoint = self.config.list_url(0)
                if kind in {'connection', 'index', 'archive', 'sync'} and not load_credentials().available_for(endpoint):
                    raise InputError('请先连接百度账号')
                self.control = TaskControl()
                self.job = {'id': uuid4().hex, 'kind': kind, 'label': LABELS[kind], 'status': 'running',
                            'phase': '准备', 'message': '任务已开始', 'started_at': utcnow_iso(),
                            'done': 0, 'total': len(args['keys']), 'args': args, 'events': [], 'download': None}
                write_json_atomic(self.job_path, self.job)
                self.thread = threading.Thread(target=self.work, args=(args, guard), daemon=True)
                self.thread.start()
            except BaseException:
                guard.__exit__()
                raise
            return self.snapshot()

    def work(self, args, guard):
        config = replace(self.config)
        kind, keys = args['kind'], args['keys']
        published = None
        try:
            with ArchiveState(config.db_path) as state, state.tracked_run('ui:' + kind) as stats:
                def stage(label, action):
                    self.control.check()
                    self.update(phase=label, done=0, message=label)
                    result = action()
                    if isinstance(result, dict) and result.get('aborted'):
                        raise AuthExpired('登录已失效')
                    return result
                if kind in {'export', 'export_images'}:
                    result = stage('生成图片 ZIP' if kind == 'export_images' else '生成导出包',
                                   lambda: export_images(config, state, keys=keys, control=self.control, log=self.log)
                                   if kind == 'export_images' else export_selection(config, state, keys=keys, formats=['md', 'txt'], control=self.control))
                    published = Path(result['path'])
                    self.control.check()
                    stats['download'] = '/download/' + Path(result['path']).name
                    self.update(download=stats['download'], result={k: v for k, v in result.items() if k != 'path'})
                elif kind == 'verify':
                    if args.get('rebuild_legacy'):
                        from .legacy import rebuild_legacy_evidence
                        stats['legacy_evidence'] = stage('从 raw 离线补证', lambda: rebuild_legacy_evidence(config, state, control=self.control))
                        self.update(legacy_evidence=stats['legacy_evidence'])
                    result = stage('核对文件', lambda: run_verify(config, state, log=self.log))
                    validation = validation_result(result)
                    stats['failed'] = not validation['ok']
                    self.update(result=validation)
                else:
                    with ArchiveClient(config, load_credentials(), logger=self.log, control=self.control) as client:
                        if kind == 'connection':
                            self.update(phase='验证连接')
                            response = client.get(config.list_url(0), expect='json', max_retries=1)
                            save_raw(config.raw_dir / 'diagnostics' / f'ui-{self.job["id"]}' / 'list.json',
                                     response.content, url=response.url, status_code=response.status_code, headers=response.headers)
                            parsed = parse_list_json(response.content)
                            if parsed['status'] not in (0, '0') or (parsed['total_count'] and not parsed['html']):
                                raise AuthExpired('登录无效')
                            self.connection = {'state': 'connected', 'checked_at': utcnow_iso()}
                        elif kind == 'index':
                            stats.update(stage('读取收藏列表', lambda: run_index(config, state, client, force=True, log=self.log)))
                            self.connection = {'state': 'connected', 'checked_at': utcnow_iso()}
                        elif kind in {'archive', 'sync'}:
                            if kind == 'sync':
                                # 先读一遍最新收藏列表，再对全部空间文章续传。已完成的会被跳过。
                                stats.update(stage('读取收藏列表', lambda: run_index(config, state, client, force=True, log=self.log)))
                                self.connection = {'state': 'connected', 'checked_at': utcnow_iso()}
                                keys = [r['key'] for r in state.iter_records('page')]
                                args['keys'] = keys
                                self.update(total=len(keys))

                            def fetch_selected():
                                if args['force']:
                                    return run_fetch(config, state, client, only_keys=keys, force=True, log=self.log)
                                # 无效或失败的旧响应需刷新签名；其余内容仍按已有进度续传。
                                retry = [key for key in keys if (state.get_item(key) or {}).get('status')
                                         in {'warning', 'failed', 'auth_error'}]
                                result = {'failed': 0}
                                for subset, force in ((retry, True), ([key for key in keys if key not in retry], False)):
                                    if not subset:
                                        continue
                                    part = run_fetch(config, state, client, only_keys=subset, force=force, log=self.log)
                                    if part.get('aborted'):
                                        return part
                                    result['failed'] += part.get('failed', 0)
                                return result
                            fetch = stage('下载文章', fetch_selected)
                            parsed = stage('整理文章', lambda: run_parse(config, state, only_keys=keys, log=self.log, control=self.control))
                            stats['failed'] = fetch.get('failed', 0) + parsed.get('warning', 0) + parsed.get('missing_raw', 0)
                            if args['images'] or kind == 'sync':
                                assets = stage('下载图片', lambda: run_assets(config, state, client, only_keys=keys, log=self.log))
                                stats['failed'] += assets.get('failed', 0)
                        elif kind == 'assets':
                            assets = stage('补齐图片', lambda: run_assets(config, state, client, only_keys=keys, log=self.log))
                            stats['failed'] = assets.get('failed', 0)
                if kind in {'archive', 'assets', 'sync'}:
                    stats['failed'] = max(stats.get('failed', 0), sum(
                        not article_truth(config, state, key)['complete'] for key in keys))
                export_master_index(config, state)
                status = 'partial' if stats.get('failed') else 'completed'
                message = ('任务完成，部分内容仍待补齐。已保存的数据可继续使用。' if status == 'partial' else
                           '百度连接正常，可以更新列表和抓取文章。' if kind == 'connection' else '任务完成，结果已保存。')
                if kind == 'export_images':
                    message = f"已打包 {result['images']} 张图片，范围 {result['count']} 篇文章。"
                    if result['not_downloaded']:
                        message += f"另有 {result['not_downloaded']} 个图片引用尚未下载，已列入 ZIP 清单。"
                if kind == 'verify':
                    message = '校验通过，归档完整。' if validation['ok'] else '校验未通过，请查看缺失数量与完整性检查。'
                # 停止与完成共用锁：已接收停止信号的任务不能再次标记 completed。
                with self.lock:
                    self.control.check()
                    self.update(status=status, phase='已结束', finished_at=utcnow_iso(), done=len(keys), message=message)
        except (TaskStopped, KeyboardInterrupt):
            self.update(status='stopped', message='任务已停止，已保存的内容保留。点击继续任务即可续传。')
        except AuthExpired:
            self.connection = {'state': 'expired', 'checked_at': utcnow_iso()}
            self.update(status='failed', message='百度登录已失效，请在连接账号中更新登录凭证。')
        except (SelectionError, IntegrityError):
            self.update(status='failed', message='导出未完成：没有可导出的图片，或本地文件缺失、校验不一致。请校验归档后重试。')
        except Exception as exc:
            if kind == 'connection':
                self.connection = {'state': 'error', 'checked_at': utcnow_iso()}
            self.update(status='failed', message=f'任务未完成（{type(exc).__name__}），可检查连接后重试。')
        finally:
            try:
                if published and self.snapshot()['status'] != 'completed':
                    published.unlink(missing_ok=True)
                    self.update(download=None, result=None)
            finally:
                guard.__exit__()

    def stop(self):
        with self.lock:
            if self.job and self.job['status'] in ACTIVE:
                self.control.stop()
                self.update(status='stopping', message='正在停止，等待当前请求和文件保存结束…')
        return self.snapshot()

    def article(self, key):
        with ArchiveState(self.config.db_path) as state:
            record = state.get_record(key)
            if not record or record['type'] != 'page':
                raise InputError('未找到文章')
            folder = self.config.item_dir(key)
            source = current_content_path(folder)
            if not source.is_file():
                raise InputError('正文尚未抓取')
            meta = read_json(folder / 'metadata.json', {}) or {}
            soup = make_soup(reading_html(validated_reading_content(self.config, state, key)))
            for node in soup.find_all(['img', 'video', 'audio', 'source']):
                src = str(node.get('src', ''))
                if src.startswith('assets/') and len(Path(src).parts) == 2:
                    node['src'] = '/media/' + quote(key, safe='') + '/' + quote(Path(src).name, safe='')
                elif not src.startswith('data:image/'):
                    node.attrs.pop('src', None)
                    node['alt'] = '图片未能保存'
                candidates = []
                for candidate, descriptor in parse_srcset(str(node.get('srcset') or '')):
                    if candidate.startswith('assets/') and len(Path(candidate).parts) == 2:
                        candidate = '/media/' + quote(key, safe='') + '/' + quote(Path(candidate).name, safe='')
                    elif not candidate.startswith('data:image/'):
                        continue
                    candidates.append(candidate + (' ' + descriptor if descriptor else ''))
                if candidates:
                    node['srcset'] = ', '.join(candidates)
                else:
                    node.attrs.pop('srcset', None)
            title = html.escape(meta.get('title') or record.get('title') or '无标题')
            return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><link rel="stylesheet" href="/dashboard.css">
<title>{title} · 百度空间归档</title><body class="article-body"><h1>{title}</h1>
<p class="muted">收藏于 {html.escape(meta.get('content_date') or record.get('list_date') or '未知日期')}</p>
<article>{str(soup)}</article></body></html>'''.encode()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: LocalServer

    def log_message(self, *args):
        pass  # 不记录带文章标识或用户输入的 HTTP 请求。

    def send(self, body, mime='application/json; charset=utf-8', status=200, filename=None):
        if isinstance(body, (dict, list)) or body is None:
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def allowed(self, mutation=False):
        port = self.server.server_port
        hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        origin = self.headers.get('Origin')
        if self.headers.get('Host') not in hosts or (origin and origin not in {'http://' + h for h in hosts}):
            return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            return False
        return not mutation or secrets.compare_digest(self.headers.get('X-Archive-Token', ''), self.server.app.token)

    def do_GET(self):
        if not self.allowed():
            return self.send({'error': '仅允许本地操作台访问'}, status=403)
        app = self.server.app
        url = urlparse(self.path)
        path = unquote(url.path)
        try:
            if path == '/api/bootstrap':
                return self.send({'token': app.token})
            if path == '/api/library':
                return self.send(app.library())
            if path == '/api/job':
                return self.send(app.snapshot())
            if path in {'/', '/index.html', '/dashboard.css', '/dashboard.js', '/logo.svg'}:
                filename = 'index.html' if path in {'/', '/index.html'} else path[1:]
                return self.send((WEB / filename).read_bytes(), mimetypes.guess_type(filename)[0] or 'text/plain')
            if path == '/article':
                return self.send(app.article(parse_qs(url.query).get('key', [''])[0]), 'text/html; charset=utf-8')
            parts = path.strip('/').split('/')
            if len(parts) == 3 and parts[0] == 'media':
                key, filename = parts[1:]
                with ArchiveState(app.config.db_path) as state:
                    asset = next((a for a in state.assets_for(key) if a['download_status'] == 'ok' and a['local_path'] == 'assets/' + filename), None)
                if asset and filename not in {'.', '..'}:
                    file = app.config.item_dir(key) / 'assets' / filename
                    if not file.resolve().is_relative_to(app.config.items_dir.resolve()):
                        raise InputError('无效资源')
                    validation = validate_downloaded_asset(app.config, asset)
                    if not validation['ok']:
                        raise InputError('资源缺失或校验不一致')
                    return self.send(validation['body'], mimetypes.guess_type(filename)[0] or 'application/octet-stream')
            if len(parts) == 2 and parts[0] == 'download' and re.fullmatch(r'(?:selection|images)-[a-f0-9]{32}\.zip', parts[1]):
                file = app.config.root / 'exports' / parts[1]
                if file.is_file() and file.resolve().is_relative_to((app.config.root / 'exports').resolve()):
                    return self.send(file.read_bytes(), 'application/zip', filename=parts[1])
            return self.send({'error': '页面不存在'}, status=404)
        except (InputError, FileNotFoundError):
            return self.send({'error': '请求的文章或文件不可用'}, status=404)
        except (BrokenPipeError, ConnectionResetError):
            # 浏览器取消下载或关闭页面；完整导出仍在本地，不再向已断开的连接写入错误页。
            self.close_connection = True
        except Exception:
            return self.send({'error': '无法读取归档，请检查本地文件'}, status=500)

    def do_POST(self):
        if not self.allowed(mutation=True):
            return self.send({'error': '请求校验失败，请刷新本地页面'}, status=403)
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 256_000:
                raise InputError('请求大小无效')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise InputError('请求格式无效')
            app = self.server.app
            actions = {'/api/jobs': app.start, '/api/settings': app.save_settings,
                       '/api/session': app.save_session, '/api/stop': lambda _: app.stop()}
            if self.path not in actions:
                return self.send({'error': '接口不存在'}, status=404)
            return self.send(actions[self.path](data))
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except ArchiveBusy:
            return self.send({'error': '当前有归档任务进行中，请等待完成或先停止'}, status=409)
        except (InputError, AuthError, ValueError) as exc:
            message = str(exc) if isinstance(exc, InputError) else '输入无效，请检查后重试'
            return self.send({'error': message}, status=400)
        except Exception:
            return self.send({'error': '操作未完成，请检查本地归档目录'}, status=500)


def serve(config, *, port=8765, config_file=None):
    app = Application(config, config_file)
    server = LocalServer(('127.0.0.1', port), app)
    print(f'百度空间归档操作台：http://127.0.0.1:{server.server_port}/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        if app.thread:
            app.thread.join(timeout=50)
        server.server_close()
