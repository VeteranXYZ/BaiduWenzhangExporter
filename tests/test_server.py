from pathlib import Path
import json
import threading

import httpx
import pytest

from conftest import seed_generation
from baidu_archive import server
from baidu_archive.auth import Credentials
from baidu_archive.config import Config
from baidu_archive.locking import ArchiveBusy
from baidu_archive.pipeline import run_parse
from baidu_archive.state import ArchiveState
from baidu_archive.storage import save_raw


@pytest.fixture
def application(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server, 'load_credentials', lambda: Credentials(cookie_header='BDUSS=FAKE_PRIVATE_COOKIE'))
    config = Config(root=tmp_path / 'archive', delay=0, jitter=0)
    config.ensure_dirs()
    with ArchiveState(config.db_path) as state:
        for position, key in enumerate(['one', 'two', 'other']):
            state.upsert_record({'key': key, 'type': 'article' if key == 'other' else 'page',
                                 'global_position': position, 'title': key,
                                 'raw_html': 'DO_NOT_EXPOSE_RAW'})
            state.ensure_item(key, 'pending')
        seed_generation(state, ['one', 'two', 'other'])
        body = '<h1 id="detailArticleTitle_x">One</h1><div id="detailArticleContent_x"><p>Saved article</p></div><div id="detailArticleFooter_x"></div>'
        save_raw(config.content_dir / 'one.html', body.encode(), url='https://example.test/?sign=PRIVATE_SIGNATURE', status_code=200)
        run_parse(config, state, only_keys=['one'])
    return server.Application(config, tmp_path / 'config.json')


@pytest.fixture
def http(application):
    service = server.LocalServer(('127.0.0.1', 0), application)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    with httpx.Client(base_url=f'http://127.0.0.1:{service.server_port}', trust_env=False) as client:
        yield client, application
    application.stop()
    if application.thread:
        application.thread.join(timeout=3)
    service.shutdown()
    service.server_close()
    thread.join(timeout=3)


def completed(app):
    app.thread.join(timeout=5)
    assert not app.thread.is_alive()
    return app.snapshot()


def test_library_returns_safe_fields_and_actual_state(application):
    data = application.library()
    assert data['summary']['indexed'] == 3
    assert data['summary']['pages'] == 2
    assert data['summary']['ready'] == 1
    encoded = json.dumps(data)
    assert 'PRIVATE_SIGNATURE' not in encoded
    assert 'FAKE_PRIVATE_COOKIE' not in encoded
    assert 'DO_NOT_EXPOSE_RAW' not in encoded
    assert 'iframe_url' not in encoded


@pytest.mark.parametrize('keys', [[], ['unknown'], ['other'], [7]])
def test_start_rejects_invalid_or_empty_scope(application, keys):
    with pytest.raises(server.InputError):
        application.start({'kind': 'archive', 'keys': keys})
    assert application.snapshot() is None


def test_cannot_export_unfetched_articles(application):
    with pytest.raises(server.InputError):
        application.start({'kind': 'export', 'keys': ['two']})


def test_export_job_download_and_no_raw_file_serving(http):
    client, app = http
    token = client.get('/api/bootstrap').json()['token']
    response = client.post('/api/jobs', json={'kind': 'export', 'keys': ['one']}, headers={'X-Archive-Token': token})
    assert response.status_code == 200
    job = completed(app)
    assert job['status'] == 'completed'
    assert app.library()['history'][0]['download'] == job['download']
    bundle = client.get(job['download'])
    assert bundle.status_code == 200 and bundle.content.startswith(b'PK')
    assert client.get('/secrets/session.json').status_code == 404
    assert client.get('/archive/state.db').status_code == 404
    assert client.get('/download/../../state.db').status_code == 404
    assert client.get('/article?key=unknown').status_code == 404


def test_loopback_host_origin_and_mutation_token_are_required(http):
    client, app = http
    assert client.get('/api/library', headers={'Host': 'evil.test'}).status_code == 403
    assert client.get('/api/bootstrap', headers={'Origin': 'https://evil.test'}).status_code == 403
    assert client.get('/api/library', headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert client.post('/api/jobs', json={'kind': 'verify'}).status_code == 403
    assert client.post('/api/jobs', json={'kind': 'verify'}, headers={'X-Archive-Token': app.token, 'Origin': 'https://evil.test'}).status_code == 403
    assert app.snapshot() is None


def test_settings_persist_and_reject_out_of_bounds(application):
    result = application.save_settings({'concurrency': 4, 'delay': .25, 'page_limit': 100})
    # 每页条数不再可调：百度固定 30，改成别的值会让索引永远无法核实
    assert result['page_limit'] == 30
    assert 'page_limit' not in json.loads(application.config_file.read_text())
    assert json.loads(application.config_file.read_text())['delay'] == .25
    with pytest.raises(server.InputError):
        application.save_settings({'concurrency': 100, 'delay': 0, 'page_limit': -1})
    assert application.config.concurrency == 4


def test_only_selected_keys_reach_pipeline_and_stop_is_resumable(application, monkeypatch):
    entered = threading.Event()
    seen = []

    def fetch(config, state, client, **kwargs):
        seen.extend(kwargs['only_keys'])
        entered.set()
        client.control.wait(10)
        return {}

    monkeypatch.setattr(server, 'run_fetch', fetch)
    application.start({'kind': 'archive', 'keys': ['one'], 'images': False})
    assert entered.wait(timeout=3)
    with pytest.raises(ArchiveBusy):
        application.start({'kind': 'export', 'keys': ['one']})
    application.stop()
    job = completed(application)
    assert job['status'] == 'stopped'
    assert job['args']['keys'] == ['one']
    assert seen == ['one']
    assert (application.config.item_dir('one') / 'content.original.html').exists()


def test_ui_job_recovery_after_server_restart(application):
    application.job_path.write_text(json.dumps({'status': 'running', 'args': {'keys': ['one']}}))
    restored = server.Application(application.config)
    assert restored.snapshot()['status'] == 'stopped'
    assert restored.snapshot()['args']['keys'] == ['one']


def test_article_removes_active_content_and_remote_image_requests(application):
    folder = application.config.item_dir('one')
    # A legacy source without versioned parsed metadata.
    (folder / 'metadata.json').write_text('{}')
    (folder / 'content.original.html').write_text('<script>alert(1)</script><img onerror="x()" src="https://tracker.test/a.png"><a href="javascript:alert(1)">x</a>')
    body = application.article('one').decode()
    assert '<script' not in body and 'onerror' not in body
    assert 'tracker.test' not in body and 'javascript:' not in body
    assert '图片未能保存' in body


def test_failed_article_refreshes_response_but_successful_article_resumes(application, monkeypatch):
    calls = []
    with ArchiveState(application.config.db_path) as state:
        state.update_item('two', status='warning')
    monkeypatch.setattr(server, 'run_fetch', lambda *a, **kw: calls.append((kw['only_keys'], kw['force'])) or {'failed': 0})
    monkeypatch.setattr(server, 'run_parse', lambda *a, **kw: {'warning': 0})
    application.start({'kind': 'archive', 'keys': ['one', 'two'], 'images': False})
    assert completed(application)['status'] == 'partial'  # Mocked stages did not save article two.
    assert calls == [(['two'], True), (['one'], False)]


def test_failed_images_produce_partial_job_result(application, monkeypatch):
    monkeypatch.setattr(server, 'run_assets', lambda *a, **kw: {'failed': 2})
    application.start({'kind': 'assets', 'keys': ['one']})
    assert completed(application)['status'] == 'partial'
    assert application.library()['history'][0]['outcome'] == 'partial'


def image_fixture(app, key):
    from baidu_archive.utils import sha256_bytes
    folder = app.config.item_dir(key) / 'assets'
    folder.mkdir(parents=True, exist_ok=True)
    body = (Path(__file__).parent / 'fixtures' / 'tiny.png').read_bytes()
    (folder / 'image.png').write_bytes(body)
    with ArchiveState(app.config.db_path) as state:
        state.upsert_asset(key, {'kind': 'image', 'original_url': 'https://example.test/image', 'attribute': 'src'})
        state.record_asset_result(key, 'https://example.test/image', 'src', download_status='ok',
                                  local_path='assets/image.png', sha256=sha256_bytes(body))


@pytest.mark.parametrize('scope,keys,expected', [('selected', ['one'], ['one']), ('all', None, ['one', 'two'])])
def test_image_export_scope_download_history_and_no_login(http, monkeypatch, scope, keys, expected):
    import io
    import zipfile
    client, app = http
    image_fixture(app, 'one')
    image_fixture(app, 'two')
    monkeypatch.setattr(server, 'load_credentials', lambda: Credentials())
    def no_network(*a, **kw):
        pytest.fail('Offline image export must not construct a network client')
    monkeypatch.setattr(server, 'ArchiveClient', no_network)
    response = client.post('/api/jobs', json={'kind': 'export_images', 'scope': scope, 'keys': keys},
                           headers={'X-Archive-Token': app.token})
    assert response.status_code == 200
    job = completed(app)
    assert job['status'] == 'completed'
    assert job['args']['keys'] == expected
    assert job['result']['images'] == len(expected)
    response = client.get(job['download'])
    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/zip'
    assert 'attachment;' in response.headers['content-disposition']
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        assert bundle.testzip() is None
        manifest = json.loads(bundle.read('manifest.json'))
        assert [e['key'] for e in manifest['entries']] == expected
    assert app.library()['history'][0]['download'] == job['download']


@pytest.mark.parametrize('data', [
    {'scope': 'selected', 'keys': []}, {'scope': 'selected', 'keys': ['other']},
    {'scope': 'selected', 'keys': ['one']}, {'scope': 'wrong'},
    {'scope': 'all', 'keys': ['one']},
])
def test_images_invalid_or_empty_selection_does_not_start(application, data):
    with pytest.raises(server.InputError):
        application.start({'kind': 'export_images', **data})
    assert application.snapshot() is None


def test_cancelled_browser_download_does_not_attempt_second_response(application):
    from types import SimpleNamespace
    handler = object.__new__(server.Handler)
    handler.server = SimpleNamespace(app=application)
    handler.path = '/api/library'
    handler.allowed = lambda **_: True
    attempted = []
    def disconnected(*a, **kw):
        attempted.append(kw.get('status', 200))
        raise BrokenPipeError('Browser closed the connection')
    handler.send = disconnected
    handler.do_GET()
    assert attempted == [200]
    assert handler.close_connection


def test_selection_stop_during_conversion_is_stopped_without_zip(application, monkeypatch):
    from baidu_archive import selection
    entered,release=threading.Event(),threading.Event()
    original=selection.html_to_markdown
    def convert(body):
        entered.set()
        assert release.wait(3)
        return original(body)
    monkeypatch.setattr(selection,'html_to_markdown',convert)
    application.start({'kind':'export','keys':['one']})
    try:
        assert entered.wait(3)
        assert application.stop()['status']=='stopping'
    finally:
        release.set()
    job=completed(application)
    assert job['status']=='stopped' and job['download'] is None
    assert application.library()['history'][0]['download'] is None
    assert application.library()['history'][0]['outcome']=='stopped'
    assert not list(application.config.root.rglob('*.zip'))
    assert not list(application.config.root.rglob('*.tmp'))


def test_stop_after_zip_return_before_job_completion_rolls_back(application, monkeypatch):
    original=server.export_master_index
    def stopped(config,state):
        application.stop()
        original(config,state)
    monkeypatch.setattr(server,'export_master_index',stopped)
    application.start({'kind':'export','keys':['one']})
    job=completed(application)
    assert job['status']=='stopped' and job['download'] is None
    assert application.library()['history'][0]['download'] is None
    assert not list(application.config.root.rglob('*.zip'))


def test_article_preserves_only_safe_local_responsive_candidates(application):
    folder=application.config.item_dir('one')
    (folder/'metadata.json').write_text('{}')
    (folder/'content.original.html').write_text('<picture><source media="(min-width: 600px)" srcset="assets/wide.webp 1x, https://tracker.test/wide2 2x"><img src="assets/fallback.png" srcset="assets/small.png 400w, assets/large.png 800w" sizes="50vw"></picture>')
    body=application.article('one').decode()
    assert '/media/one/wide.webp 1x' in body
    assert '/media/one/small.png 400w, /media/one/large.png 800w' in body
    assert 'sizes="50vw"' in body and 'media="(min-width: 600px)"' in body
    assert 'tracker.test' not in body
