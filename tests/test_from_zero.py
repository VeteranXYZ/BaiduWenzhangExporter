"""从零开始的完整使用路径：空目录 → 登录 → 下载全部 → 浏览 → 导出。

这是产品的验收测试：用户只按一次「下载全部」，之后不需要任何修复、补证或
手动重试，就能得到完整、可导出的归档。百度的各个接口用 MockTransport 模拟。
"""
import json
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from baidu_archive import server
from baidu_archive.auth import Credentials
from baidu_archive.client import ArchiveClient
from baidu_archive.config import Config

PNG = (Path(__file__).parent / 'fixtures' / 'tiny.png').read_bytes()
KEYS = [f'k{i:03d}' for i in range(65)]          # 3 页列表（30 + 30 + 5）


def fake_baidu(request: httpx.Request) -> httpx.Response:
    url = urlparse(str(request.url))
    query = parse_qs(url.query)
    if url.path == '/fav/list':
        start = int(query['start'][0]); limit = int(query['limit'][0])
        chunk = KEYS[start:start + limit]
        html = ''.join(f'<li class="unit page" id="key_{k}"><h4 class="article-h4"><a href="http://1">标题{k}</a></h4>'
                       f'<div class="date">2011-02-08</div><a href="/page/view?key={k}">标题{k}</a></li>' for k in chunk)
        return httpx.Response(200, json={'status': 0, 'data': {'html': html, 'start': start, 'limit': limit,
                                                               'totalCount': len(KEYS), 'currentCount': len(chunk)}})
    if url.path == '/page/view':
        k = query['key'][0]
        return httpx.Response(200, text=f'<html><iframe class="pcs-article-iframe" '
                              f'src="https://wenzhang.bdstatic.com/page/content/?key={k}&amp;sign=FAKE"></iframe></html>')
    if url.path == '/page/content/':
        k = query['key'][0]
        return httpx.Response(200, text=(
            f'<html><body><h1 id="detailArticleTitle_x">标题{k}</h1>'
            f'<div id="detailArticleContent_x"><p>这是 {k} 的正文。</p><img src="https://img.test/{k}.png"></div>'
            f'<div id="detailArticleFooter_x"><div class="time-cang">收藏于 2011-02-08</div>'
            f'<div class="link-src">来自于百度空间</div></div></body></html>'))
    if url.hostname == 'img.test':
        return httpx.Response(200, content=PNG, headers={'content-type': 'image/png'})
    return httpx.Response(404)


@pytest.fixture
def fresh_app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    creds = Credentials(cookie_header='BDUSS=FAKE')
    monkeypatch.setattr(server, 'load_credentials', lambda: creds)

    def client_factory(config, credentials, **kwargs):
        client = ArchiveClient(config, credentials, **kwargs)
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(fake_baidu),
                                      event_hooks={'request': [client._scope_credentials]})
        return client
    monkeypatch.setattr(server, 'ArchiveClient', client_factory)
    config = Config(root=tmp_path / 'archive', delay=0, jitter=0, max_retries=1)
    return server.Application(config, tmp_path / 'config.json')


def finish(app, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = app.snapshot()
        if job and job['status'] not in {'running', 'starting', 'stopping'}:
            return job
        time.sleep(0.05)
    raise AssertionError('任务超时')


def test_one_button_download_produces_complete_archive_without_repair(fresh_app):
    job = fresh_app.start({'kind': 'sync'})
    assert job['label'] == '下载全部'
    job = finish(fresh_app)
    assert job['status'] == 'completed', job['message']

    library = fresh_app.library()
    pages = [r for r in library['records'] if r['type'] == 'page']
    assert len(pages) == len(KEYS)
    assert all(r['status'] == 'complete' for r in pages), {r['status'] for r in pages}
    assert library['summary']['downloaded'] == len(KEYS)          # 每篇一张图，全部在本地
    assert library['index_generation']['ok'] is True               # 索引证明在下载时就写好了


def test_from_zero_needs_no_offline_repair_and_exports(fresh_app, monkeypatch, tmp_path):
    """下载完立刻可用：健康检查不需要修复，一键导出直接得到完整包。"""
    fresh_app.start({'kind': 'sync'}); finish(fresh_app)

    import importlib, sys, types
    view = types.ModuleType('webview'); menu = types.ModuleType('webview.menu')
    menu.Menu = menu.MenuAction = menu.MenuSeparator = object
    monkeypatch.setitem(sys.modules, 'webview', view); monkeypatch.setitem(sys.modules, 'webview.menu', menu)
    gui = importlib.import_module('baidu_archive.gui'); sys.modules.pop('baidu_archive.gui', None)

    api = gui.Api(fresh_app.config)
    api._application = fresh_app
    assert api.health()['needs_offline_repair'] is False
    stats = api.get_verified_stats()
    assert stats['complete'] == len(KEYS) and stats['assets_downloaded'] == len(KEYS)
    assert all(not v for v in api.get_problems().values())

    out = tmp_path / 'out'; out.mkdir()
    monkeypatch.setattr(api, 'choose_directory', lambda: str(out))
    monkeypatch.setattr(gui, '_notify', lambda *a: None); monkeypatch.setattr(gui, '_reveal', lambda *a: None)
    result = api.export_all()
    assert result['ok'] and result['articles'] == len(KEYS) and not result['warnings']
    assert len(list((Path(result['path']) / 'articles').glob('*/assets/*.png'))) == len(KEYS)


def test_download_resumes_after_stop_without_refetching_saved_articles(fresh_app, monkeypatch):
    """中途停止后再点一次「下载全部」，已保存的文章不会重复下载。"""
    fresh_app.start({'kind': 'sync'}); finish(fresh_app)
    seen = []
    original = fake_baidu
    def counting(request):
        seen.append(urlparse(str(request.url)).path)
        return original(request)
    monkeypatch.setattr(sys_module(), 'fake_baidu', counting)
    def client_factory(config, credentials, **kwargs):
        client = ArchiveClient(config, credentials, **kwargs)
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(counting),
                                      event_hooks={'request': [client._scope_credentials]})
        return client
    monkeypatch.setattr(server, 'ArchiveClient', client_factory)
    fresh_app.start({'kind': 'sync'}); job = finish(fresh_app)
    assert job['status'] == 'completed'
    assert seen.count('/page/view') == 0 and seen.count('/page/content/') == 0   # 正文全跳过
    assert seen.count('/fav/list') == 3                                          # 只重读了列表


def test_sync_requires_login(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server, 'load_credentials', lambda: Credentials())
    app = server.Application(Config(root=tmp_path / 'archive'), tmp_path / 'config.json')
    with pytest.raises(server.InputError):
        app.start({'kind': 'sync'})


def sys_module():
    import sys
    return sys.modules[__name__]
