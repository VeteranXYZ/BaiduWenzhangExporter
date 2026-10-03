"""RC attacks exercise real pipelines and bytes; only HTTP transport is substituted."""
from __future__ import annotations

import io
import json
import wave
from pathlib import Path
from urllib.parse import parse_qs
from xml.etree import ElementTree as ET

import httpx
import pytest

from baidu_archive import server, cli
from baidu_archive.assets import run_assets
from baidu_archive.auth import Credentials, parse_netscape_cookies, load_credentials
from baidu_archive.client import ArchiveClient
from baidu_archive.config import Config
from baidu_archive.export import export_site, export_wxr, wxr_content
from baidu_archive.integrity import audit_stored_files
from baidu_archive.media import validate_media_response
from baidu_archive.parsers import make_soup, parse_srcset
from baidu_archive.pipeline import run_index, run_parse
from baidu_archive.state import ArchiveState
from baidu_archive.storage import IntegrityError, current_content_path, read_json, save_raw
from baidu_archive.tasks import TaskControl, TaskStopped
from baidu_archive.truth import article_truth, index_truth, validate_downloaded_asset
from baidu_archive.verify import run_verify

PNG = (Path(__file__).parent / 'fixtures' / 'tiny.png').read_bytes()
FIXTURES = Path(__file__).parent / 'fixtures'


def list_body(keys, total=None, start=0, limit=30, drift=False):
    html = ''.join(f'<li class="{"new-unit" if drift and i else "unit"} page" id="key_{k}"><h4><a href="/page/view?key={k}">{k}</a></h4></li>' for i, k in enumerate(keys))
    return json.dumps({'status': 0, 'data': {'html': html, 'totalCount': len(keys) if total is None else total,
                                          'currentCount': len(keys), 'start': start, 'limit': limit}}).encode()


def transport(cfg, handler, control=None):
    client = ArchiveClient(cfg, Credentials(), control=control)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler), event_hooks={'request': [client._scope_credentials]})
    return client


def raw_article(cfg, key, body, base='https://media.test/old/body.html', title=None):
    detail = f'<iframe class="pcs-article-iframe" src="{base}"></iframe>'
    content = f'<h1 id="detailArticleTitle_x">{title or key}</h1><div id="detailArticleContent_x">{body}</div><div id="detailArticleFooter_x"></div>'
    for directory, text in ((cfg.detail_dir, detail), (cfg.content_dir, content)):
        save_raw(directory / (key + '.html'), text.encode(), url=base, status_code=200)


@pytest.fixture
def archive(tmp_path):
    cfg = Config(root=tmp_path / 'archive', delay=0, jitter=0, max_retries=1, list_url_template='https://wenzhang.baidu.com/fav/list?start={start}&limit={limit}')
    cfg.ensure_dirs()
    with ArchiveState(cfg.db_path) as state:
        with transport(cfg, lambda r: httpx.Response(200, content=list_body(['one']))) as client:
            assert run_index(cfg, state, client, force=True)['index_complete']
        raw_article(cfg, 'one', '<p>Body</p><img src="photo.png">')
        run_parse(cfg, state)
        yield cfg, state


def download(cfg, state, body=PNG, mime='image/png', **kwargs):
    requests = []
    with transport(cfg, lambda r: requests.append(str(r.url)) or httpx.Response(200, content=body, headers={'content-type': mime})) as client:
        stats = run_assets(cfg, state, client, **kwargs)
    return stats, requests


@pytest.mark.parametrize('damage', ['missing', 'changed', 'wrong_digest'])
@pytest.mark.parametrize('recover', [True, False])
def test_p1_1_bytes_are_truth_and_recovery_preserves_evidence(archive, damage, recover):
    cfg, state = archive
    download(cfg, state)
    asset = state.assets_for('one')[0]
    old = cfg.item_dir('one') / asset['local_path']
    if damage == 'missing': old.unlink()
    elif damage == 'changed': old.write_bytes(PNG[:-1] + b'X')
    else: state.record_asset_result('one', 'photo.png', 'src', sha256='0' * 64)
    assert not validate_downloaded_asset(cfg, state.assets_for('one')[0])['ok']
    assert not article_truth(cfg, state, 'one')['complete']
    assert server.Application(cfg).library()['records'][0]['status'] == 'assets_partial'
    assert not audit_stored_files(cfg, state)['ok']
    before = old.read_bytes() if old.exists() else None
    stats, requests = download(cfg, state, body=PNG if recover else b'<html>login</html>', mime='image/png')
    assert len(requests) == stats['attempted'] == 1
    assert stats['downloaded'] == int(recover) and stats['failed'] == int(not recover)
    assert state.get_item('one')['status'] == ('complete' if recover else 'assets_partial')
    assert article_truth(cfg, state, 'one')['complete'] == recover
    assert run_verify(cfg, state)['validation']['ok'] == recover
    assert cli.main(['--root', str(cfg.root), 'verify']) == (0 if recover else 1)
    node = make_soup((cfg.item_dir('one') / 'content.local.html').read_text()).img
    if recover:
        assert validate_downloaded_asset(cfg, state.assets_for('one')[0])['ok']
        assert (cfg.item_dir('one') / node['src']).is_file()
    else:
        assert node['src'] == 'photo.png'
    if damage == 'changed':
        assert old.read_bytes() == before  # corrupt bytes remain as evidence
        assert state.asset_history('one')


@pytest.mark.parametrize('recover', [True, False])
def test_p1_1_server_repair_task_reports_actual_result(archive, monkeypatch, recover):
    cfg, state = archive
    download(cfg, state)
    (cfg.item_dir('one') / state.assets_for('one')[0]['local_path']).unlink()
    monkeypatch.setattr(server, 'load_credentials', lambda: Credentials(cookie_header='BDUSS=fake'))
    monkeypatch.setattr(server, 'ArchiveClient', lambda *a, **kw: transport(cfg, lambda r: httpx.Response(200, content=PNG if recover else b'<html>login</html>'), control=kw['control']))
    app = server.Application(cfg)
    app.start({'kind': 'assets', 'keys': ['one']})
    app.thread.join(5)
    assert not app.thread.is_alive()
    assert app.snapshot()['status'] == ('completed' if recover else 'partial')
    assert app.library()['records'][0]['status'] == ('complete' if recover else 'assets_partial')


@pytest.mark.parametrize('attack', ['drift', 'increase', 'decrease', 'duplicate', 'interrupted', 'max_pages'])
def test_p1_2_historical_rows_never_mask_current_generation(archive, attack):
    cfg, state = archive
    cfg.page_limit = 1 if attack in {'increase', 'decrease', 'interrupted', 'max_pages'} else 30
    with transport(cfg, lambda r: httpx.Response(200, content=list_body(['one', 'two']))) as client:
        cfg.page_limit = 30
        run_index(cfg, state, client, force=True)
    previous = state.get_meta('index_last_complete')
    if attack in {'increase', 'decrease', 'interrupted', 'max_pages'}: cfg.page_limit = 1
    control = TaskControl()
    def response(r):
        start = int(parse_qs(r.url.query.decode())['start'][0])
        if attack == 'interrupted' and start:
            control.stop(); control.check()
        if attack == 'drift': body = list_body(['one', 'three'], drift=True)
        elif attack == 'duplicate': body = list_body(['one', 'one'])
        else: body = list_body(['one' if start == 0 else 'three' if start == 1 else 'four'],
                              total=2 if start == 0 else 3 if attack == 'increase' else 1 if attack == 'decrease' else 2,
                              start=start, limit=1)
        return httpx.Response(200, content=body)
    with transport(cfg, response, control) as client:
        if attack == 'interrupted':
            with pytest.raises(TaskStopped): run_index(cfg, state, client, force=True)
        else:
            stats = run_index(cfg, state, client, force=True, max_pages=1 if attack == 'max_pages' else None)
            assert stats['failed'] == 1
    truth = index_truth(state)
    assert not truth['ok']
    assert state.get_record('two')  # historical data preserved
    assert state.get_meta('index_last_complete') == previous
    assert not run_verify(cfg, state)['validation']['checks']['index_complete']
    assert not server.Application(cfg).library()['index_generation']['ok']
    assert list(cfg.lists_dir.glob('*.json'))


def test_p1_2_full_new_generation_keeps_history_without_counting_it(archive):
    cfg, state = archive
    state.upsert_record({'key': 'old', 'type': 'page'})
    with transport(cfg, lambda r: httpx.Response(200, content=list_body(['one', 'three']))) as client:
        result = run_index(cfg, state, client, force=True)
    assert result['index_complete'] and result['indexed'] == 2 and result['historical_indexed'] == 3
    assert index_truth(state)['seen_keys'] == ['one', 'three']


def test_p1_3_relative_reference_new_base_gets_new_download(archive):
    cfg, state = archive
    download(cfg, state)
    previous = state.assets_for('one')[0]
    old = cfg.item_dir('one') / previous['local_path']
    original = (cfg.item_dir('one') / 'content.original.html').read_bytes()
    raw_article(cfg, 'one', '<p>Body</p><img src="photo.png">', base='https://media.test/new/body.html')
    run_parse(cfg, state)
    assert state.get_item('one')['status'] == 'parsed'
    current = state.assets_for('one')[0]
    assert current['resolved_url'] == 'https://media.test/new/photo.png'
    assert current['download_status'] is None and current['local_path'] is None
    assert old.read_bytes() == PNG and state.asset_history('one')[0]['record']['download_identity'] == previous['download_identity']
    assert current_content_path(cfg.item_dir('one')).parent.name == 'parsed'
    assert not article_truth(cfg, state, 'one')['complete']
    stats, requests = download(cfg, state, body=PNG + b'new')
    assert requests == ['https://media.test/new/photo.png'] and stats['downloaded'] == 1
    assert (cfg.item_dir('one') / 'content.original.html').read_bytes() == original
    assert run_verify(cfg, state)['validation']['ok']


@pytest.mark.parametrize('kind', ['audio', 'video'])
@pytest.mark.parametrize('response', ['valid', 'html', 'empty', 'captcha', 'wrong_mime', 'unknown'])
def test_p1_4_media_requires_recognizable_bytes(archive, kind, response):
    cfg, state = archive
    raw_article(cfg, 'one', f'<{kind} controls src="clip"></{kind}>')
    run_parse(cfg, state)
    body = (FIXTURES / ('tiny.wav' if kind == 'audio' else 'tiny.mp4')).read_bytes()
    mime = 'audio/wav' if kind == 'audio' else 'video/mp4'
    if response == 'html': body, mime = b'<html>login</html>', mime
    elif response == 'empty': body = b''
    elif response == 'captcha': body, mime = b'<!doctype html><form>captcha</form>', 'application/octet-stream'
    elif response == 'wrong_mime': mime = 'application/octet-stream'
    elif response == 'unknown': body = b'unknown format'
    accepted = response in {'valid', 'wrong_mime'}
    stats, _ = download(cfg, state, body, mime, include_media=True)
    assert stats['downloaded'] == int(accepted) and stats['failed'] == int(not accepted)
    assert state.get_item('one')['status'] == ('complete' if accepted else 'assets_partial')
    assert article_truth(cfg, state, 'one')['complete'] == accepted
    assert run_verify(cfg, state)['validation']['ok'] == accepted
    if not accepted:
        assert list((cfg.raw_dir / 'asset_responses' / 'one').glob('*.bin'))
        assert state.assets_for('one')[0]['local_path'] is None


@pytest.mark.parametrize('attribute', ['src', 'data-src', 'srcset'])
def test_p2_5_alias_references_localize_after_one_download(archive, attribute):
    cfg, state = archive
    body = '<img srcset="photo.png 1x, ./photo.png 2x">' if attribute == 'srcset' else f'<img {attribute}="photo.png"><img {attribute}="./photo.png">'
    raw_article(cfg, 'one', body)
    run_parse(cfg, state)
    assert len(state.assets_for('one')) == 2
    stats, requests = download(cfg, state)
    assert stats['attempted'] == stats['downloaded'] == len(requests) == 1
    references = state.assets_for('one')
    assert all(validate_downloaded_asset(cfg, a)['ok'] for a in references)
    assert len({a['local_path'] for a in references}) == 1
    soup = make_soup((cfg.item_dir('one') / 'content.local.html').read_text())
    urls = [u for u, _ in parse_srcset(soup.img['srcset'])] if attribute == 'srcset' else [n['src'] for n in soup.find_all('img')]
    assert len(urls) == 2 and len(set(urls)) == 1
    assert (cfg.item_dir('one') / urls[0]).is_file()


def test_p2_6_offline_csp_allows_actual_local_wav_and_mp4(archive):
    cfg, state = archive
    raw_article(cfg, 'one', '<audio controls src="clip.wav"></audio><video controls src="clip.mp4"></video>')
    run_parse(cfg, state)
    with transport(cfg, lambda r: httpx.Response(200, content=(FIXTURES / ('tiny.wav' if r.url.path.endswith('.wav') else 'tiny.mp4')).read_bytes())) as client:
        run_assets(cfg, state, client, include_media=True)
    site = cfg.root.parent / 'site'
    export_site(cfg, state, site)
    soup = make_soup((site / 'articles' / 'one' / 'index.html').read_text())
    directives = dict((parts[0], parts[1:]) for d in soup.find('meta', attrs={'http-equiv': 'Content-Security-Policy'})['content'].split(';') if (parts := d.split()))
    assert directives['default-src'] == ["'none'"] and directives['media-src'] == ["'self'"]
    for node in soup.find_all(['audio', 'video']):
        path = site / 'articles' / 'one' / node['src']
        assert path.is_file()
        assert validate_media_response(node.name, path.read_bytes(), None)['status'] == 'accepted'


def test_p2_7_wxr_normalizes_all_url_attributes_and_preserves_special_schemes():
    body = '<img src="photo.png" data-src="./lazy.png" srcset="./one.png 1x, //cdn.test/two.png 2x"><picture><source srcset="../wide.png 400w"></picture><video src="clip.mp4" poster="poster.png"><source src="./alt.mp4"></video><audio src="sound.wav"></audio><iframe src="assets/player.html"></iframe><a href="../relative?a=1&amp;b=2">relative</a>'
    special = ['#anchor', 'mailto:a@b.test', 'tel:123', 'data:text/plain,hello', 'magnet:?xt=urn:btih:123']
    body += ''.join(f'<a href="{url}">x</a>' for url in special)
    out = wxr_content(body, {'iframe_url_original': 'https://remote.test/old/body.html'}, [{'original_url': 'player.html', 'resolved_url': 'https://player.test/watch', 'local_path': 'assets/player.html'}])
    soup = make_soup(out)
    assert soup.iframe['src'] == 'https://player.test/watch'
    assert soup.img['src'] == 'https://remote.test/old/lazy.png'
    assert soup.video['src'] == 'https://remote.test/old/clip.mp4'
    assert soup.audio['src'] == 'https://remote.test/old/sound.wav'
    assert soup.a['href'] == 'https://remote.test/relative?a=1&b=2'
    assert [a['href'] for a in soup.find_all('a')][1:] == special
    assert 'assets/player.html' not in out
    assert parse_srcset(soup.img['srcset'])[-1][0] == 'https://cdn.test/two.png'
    with pytest.raises(IntegrityError): wxr_content('<iframe src="assets/unknown.html"></iframe>', {}, [])
    with pytest.raises(IntegrityError): wxr_content('<a href="../unknown"></a>', {}, [])


def test_p2_8_official_importer_collision_is_blocked_before_publish(archive):
    cfg, state = archive
    with transport(cfg, lambda r: httpx.Response(200, content=list_body(['one', 'two']))) as client:
        run_index(cfg, state, client, force=True)
    for key in ['one', 'two']:
        raw_article(cfg, key, f'<p>{key}</p>', title='same &amp; title')
    run_parse(cfg, state)
    target = cfg.root / 'wordpress_import.xml'
    target.write_bytes(b'previous export evidence')
    with pytest.raises(IntegrityError, match='identity collision.*one.*two'):
        export_wxr(cfg, state)
    assert target.read_bytes() == b'previous export evidence'
    metadata = read_json(cfg.item_dir('two') / 'metadata.json')
    metadata['title'] = 'different title'
    (cfg.item_dir('two') / 'metadata.json').write_text(json.dumps(metadata))
    tree = ET.parse(export_wxr(cfg, state))
    ns = {'wp': 'http://wordpress.org/export/1.2/'}
    identities = [item.find("wp:postmeta[wp:meta_key='_archive_key']/wp:meta_value", ns).text for item in tree.findall('channel/item')]
    assert identities == ['one', 'two']


def test_p2_9_cancel_between_articles_preserves_atomic_first_save(archive, monkeypatch):
    cfg, state = archive
    with transport(cfg, lambda r: httpx.Response(200, content=list_body(['one', 'two', 'three']))) as client:
        run_index(cfg, state, client, force=True)
    for key in ['one', 'two', 'three']: raw_article(cfg, key, '<p>new body</p>')
    from baidu_archive import pipeline
    control = TaskControl()
    real_parse = pipeline.parse_content_html
    calls = []
    def stop_during_first(body):
        calls.append(body)
        control.stop()
        return real_parse(body)
    monkeypatch.setattr(pipeline, 'parse_content_html', stop_during_first)
    with pytest.raises(TaskStopped): run_parse(cfg, state, control=control)
    assert len(calls) == 1
    assert (cfg.item_dir('one') / 'metadata.json').is_file()
    assert not (cfg.item_dir('two') / 'metadata.json').exists()
    assert not (cfg.item_dir('three') / 'metadata.json').exists()
    monkeypatch.setattr(server, 'load_credentials', lambda: Credentials(cookie_header='BDUSS=fake'))
    app = server.Application(cfg)
    monkeypatch.setattr(server, 'run_fetch', lambda *a, **k: {'failed': 0})
    def stop_server_first(body):
        app.stop()
        return real_parse(body)
    monkeypatch.setattr(pipeline, 'parse_content_html', stop_server_first)
    # Force a new raw body so the first article is not skipped.
    raw_article(cfg, 'one', '<p>another body</p>')
    app.start({'kind': 'archive', 'keys': ['one', 'two', 'three'], 'images': False})
    app.thread.join(5)
    assert not app.thread.is_alive() and app.snapshot()['status'] == 'stopped'
    assert not (cfg.item_dir('two') / 'metadata.json').exists()


def test_p2_10_netscape_jar_retains_semantics_and_client_filters_requests(tmp_path, monkeypatch):
    import time
    future = int(time.time()) + 86400
    lines = [f'#HttpOnly_.baidu.com\tTRUE\t/\tTRUE\t{future}\tBDUSS\tfake',
             f'foreign.test\tFALSE\t/\tFALSE\t{future}\tFOREIGN\tbad',
             '.baidu.com\tTRUE\t/\tFALSE\t1\tEXPIRED\tbad',
             f'wenzhang.baidu.com\tFALSE\t/fav\tFALSE\t{future}\tPATH\tgood',
             f'wenzhang.baidu.com\tFALSE\t/\tFALSE\t{future}\tHOST\tgood',
             f'#ordinary comment\tTRUE\t/\tFALSE\t{future}\tCOMMENT\tbad',
             '', 'malformed', '.baidu.com\tBAD\t/\tFALSE\t0\tBAD\tbad',
             '.baidu.com\tTRUE\t/\tFALSE\t0\tUNICODE\t中文']
    text = '\n'.join(lines)
    jar = parse_netscape_cookies(text)
    credentials = Credentials(cookie_jar=jar)
    bduss = next(c for c in jar if c.name == 'BDUSS')
    assert bduss.domain == '.baidu.com' and bduss.domain_specified and bduss.secure
    assert bduss.path == '/' and bduss.expires == future and bduss.has_nonstandard_attr('HttpOnly')
    assert credentials.header_for('https://wenzhang.baidu.com/fav/list') == 'PATH=good; BDUSS=fake; HOST=good'
    assert credentials.header_for('https://wenzhang.baidu.com/other') == 'BDUSS=fake; HOST=good'
    assert 'BDUSS' not in credentials.header_for('http://wenzhang.baidu.com/fav/list')
    assert 'HOST' not in credentials.header_for('https://sub.wenzhang.baidu.com/')
    assert 'BDUSS' not in credentials.header_for('https://foreign.test/')
    monkeypatch.chdir(tmp_path)
    secret = tmp_path / 'secrets'; secret.mkdir(); (secret / 'cookies.netscape.txt').write_text(text)
    monkeypatch.delenv('BAIDU_ARCHIVE_COOKIE', raising=False)
    assert load_credentials().cookie_jar is not None
    cfg = Config(root=tmp_path / 'archive', delay=0, jitter=0)
    captured = []
    with ArchiveClient(cfg, credentials) as client:
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(lambda r: captured.append(dict(r.headers)) or httpx.Response(200)), event_hooks={'request': [client._scope_credentials]})
        for url in ['https://wenzhang.baidu.com/fav/list', 'https://wenzhang.baidu.com/other', 'http://wenzhang.baidu.com/fav/list', 'https://foreign.test/']:
            client._client.get(url)
    assert captured[0]['cookie'] == 'PATH=good; BDUSS=fake; HOST=good'
    assert captured[1]['cookie'] == 'BDUSS=fake; HOST=good'
    assert 'cookie' not in captured[2] and 'cookie' not in captured[3]


@pytest.mark.parametrize('damage', ['absolute', 'escape', 'outside_assets', 'directory', 'symlink', 'wrong_size', 'wrong_identity'])
def test_shared_resource_validator_refuses_unsafe_or_inconsistent_records(archive, damage):
    cfg, state = archive
    download(cfg, state)
    asset = state.assets_for('one')[0]
    if damage == 'absolute': asset['local_path'] = str(cfg.item_dir('one') / asset['local_path'])
    elif damage == 'escape': asset['local_path'] = '../outside.png'
    elif damage == 'outside_assets': asset['local_path'] = 'content.original.html'
    elif damage == 'directory':
        path = cfg.item_dir('one') / 'assets' / 'dir'; path.mkdir(); asset['local_path'] = 'assets/dir'
    elif damage == 'symlink':
        link = cfg.item_dir('one') / 'assets' / 'link.png'; link.symlink_to(cfg.item_dir('one') / asset['local_path']); asset['local_path'] = 'assets/link.png'
    elif damage == 'wrong_size': asset['size'] += 1
    elif damage == 'wrong_identity': asset['resolved_url'] = 'https://other.test/photo.png'
    assert not validate_downloaded_asset(cfg, asset)['ok']


def test_gui_data_api_uses_shared_truth_without_a_native_window(archive, monkeypatch):
    # Stub only the optional UI import surface; get_stats itself executes unchanged.
    import importlib, sys, types
    view = types.ModuleType('webview'); menu = types.ModuleType('webview.menu')
    menu.Menu = menu.MenuAction = menu.MenuSeparator = object
    monkeypatch.setitem(sys.modules, 'webview', view)
    monkeypatch.setitem(sys.modules, 'webview.menu', menu)
    gui = importlib.import_module('baidu_archive.gui')
    cfg, state = archive
    download(cfg, state)
    api = gui.Api(cfg)

    # 界面取数分两级：get_stats 是首屏用的数据库快照（毫秒级，可能偏乐观），
    # get_verified_stats 才逐字节核对、才允许下结论。真相断言落在后者。
    assert api.get_verified_stats()['complete'] == 1
    (cfg.item_dir('one') / state.assets_for('one')[0]['local_path']).unlink()
    stats = api.get_verified_stats()
    assert stats['complete'] == 0 and stats['assets_downloaded'] == 0 and stats['assets_failed'] == 1
    assert stats['verified'] is True

    # 快路径绝不能冒充已核实 —— 它可以乐观，但必须自曝身份，
    # 否则界面会把数据库的记账当成事实展示给用户。
    quick = api.get_stats()
    assert quick['verified'] is False

    generation = state.get_meta('index_generation'); generation['complete'] = False
    state.set_meta('index_generation', generation)
    assert not api.get_verified_stats()['index_complete']
    assert not api.get_stats()['index_complete']   # 索引完整性两级都如实反映
    sys.modules.pop('baidu_archive.gui', None)


def test_corrupt_media_is_not_served_even_with_database_success(archive):
    import threading
    cfg, state = archive
    download(cfg, state)
    asset = state.assets_for('one')[0]
    (cfg.item_dir('one') / asset['local_path']).write_bytes(PNG[:-1] + b'X')
    app = server.Application(cfg)
    service = server.LocalServer(('127.0.0.1', 0), app)
    thread = threading.Thread(target=service.serve_forever); thread.start()
    try:
        response = httpx.get(f'http://127.0.0.1:{service.server_port}/media/one/' + Path(asset['local_path']).name, trust_env=False)
        assert response.status_code == 404
    finally:
        service.shutdown(); service.server_close(); thread.join(5)


def test_reading_views_do_not_reuse_stale_local_references(archive):
    cfg, state = archive
    download(cfg, state)
    asset = state.assets_for('one')[0]
    saved_local = (cfg.item_dir('one') / 'content.local.html').read_bytes()
    (cfg.item_dir('one') / asset['local_path']).unlink()
    site = cfg.root.parent / 'reading'
    export_site(cfg, state, site)
    node = make_soup((site / 'articles' / 'one' / 'index.html').read_text()).img
    assert not node['src'].startswith('assets/')
    node = make_soup(server.Application(cfg).article('one').decode()).img
    assert not node.has_attr('src')
    assert (cfg.item_dir('one') / 'content.local.html').read_bytes() == saved_local


def test_missing_parsed_version_is_rebuilt_from_verified_raw(archive):
    cfg, state = archive
    download(cfg, state)
    parsed = current_content_path(cfg.item_dir('one'), prefer_local=False)
    expected = parsed.read_bytes(); parsed.unlink()
    assert not article_truth(cfg, state, 'one')['complete']
    result = run_parse(cfg, state)
    assert result['parsed'] == 1 and result['skip'] == 0
    assert parsed.read_bytes() == expected
    assert article_truth(cfg, state, 'one')['complete']


def test_current_resource_set_mismatch_blocks_localization_publication(archive):
    cfg, state = archive
    download(cfg, state)
    before = (cfg.item_dir('one') / 'content.local.html').read_bytes()
    state.upsert_asset('one', {'kind': 'image', 'original_url': 'photo.png', 'attribute': 'src',
                              'resolved_url': 'https://wrong.test/photo.png', 'downloadable': True})
    with transport(cfg, lambda r: (_ for _ in ()).throw(AssertionError('must not download stale collection'))) as client:
        with pytest.raises(IntegrityError, match='资源集合'):
            run_assets(cfg, state, client)
    assert (cfg.item_dir('one') / 'content.local.html').read_bytes() == before
    assert state.get_item('one')['status'] == 'parsed'
    assert not audit_stored_files(cfg, state)['ok']


def test_incomplete_generation_is_partial_in_cli_run_history(archive):
    cfg, state = archive
    generation = state.get_meta('index_generation'); generation['complete'] = False
    state.set_meta('index_generation', generation)
    with state.tracked_run('assets') as stats:
        stats.update(download(cfg, state)[0])
    assert stats['failed'] == 0 and stats['incomplete_items'] == 1
    assert state.recent_runs(1)[0]['stats']['outcome'] == 'partial'
    assert state.get_item('one')['status'] == 'parsed'


def test_new_raw_generation_cannot_inherit_old_article_completeness(archive):
    cfg, state = archive
    download(cfg, state)
    assert article_truth(cfg, state, 'one')['complete']
    raw_article(cfg, 'one', '<p>changed body</p><img src="photo.png">')
    facts = article_truth(cfg, state, 'one')
    assert not facts['complete'] and 'stale_content_generation' in facts['issues']
    assert server.Application(cfg).library()['records'][0]['status'] == 'assets_partial'
    assert not run_verify(cfg, state)['validation']['ok']
    run_parse(cfg, state)
    download(cfg, state)
    assert article_truth(cfg, state, 'one')['complete']


def test_assets_refuses_unparsed_raw_generation_without_republishing(archive):
    cfg, state = archive
    download(cfg, state)
    before = (cfg.item_dir('one') / 'content.local.html').read_bytes()
    raw_article(cfg, 'one', '<p>new raw body</p>')
    with transport(cfg, lambda r: (_ for _ in ()).throw(AssertionError('stale parse must not fetch assets'))) as client:
        with pytest.raises(IntegrityError, match='raw 世代'):
            run_assets(cfg, state, client)
    assert (cfg.item_dir('one') / 'content.local.html').read_bytes() == before
    assert state.get_item('one')['status'] == 'parsed'


def _gui(monkeypatch):
    """导入 gui 时打桩掉可选的 webview 依赖，数据方法本身原样执行。"""
    import importlib, sys, types
    view = types.ModuleType('webview'); menu = types.ModuleType('webview.menu')
    menu.Menu = menu.MenuAction = menu.MenuSeparator = object
    monkeypatch.setitem(sys.modules, 'webview', view)
    monkeypatch.setitem(sys.modules, 'webview.menu', menu)
    module = importlib.import_module('baidu_archive.gui')
    sys.modules.pop('baidu_archive.gui', None)
    return module


def test_gui_library_reuses_server_application(archive, monkeypatch):
    """归档/媒体/异常三页共用 Application.library()，不另起一套统计口径。"""
    gui = _gui(monkeypatch)
    cfg, state = archive
    download(cfg, state)
    data = gui.Api(cfg).get_library()
    assert [r['key'] for r in data['records']] == ['one']
    assert data['records'][0]['status'] == 'complete'
    assert data['summary']['downloaded'] == 1


def test_gui_article_inlines_only_validated_bytes(archive, monkeypatch):
    """预览把图片内联成 data URI；字节一旦损坏就必须降级为占位，不能照样显示。"""
    gui = _gui(monkeypatch)
    cfg, state = archive
    download(cfg, state)
    api = gui.Api(cfg)

    good = api.get_article('one')
    assert good['images_inlined'] == 1 and good['images_missing'] == 0
    assert 'data:image/png;base64,' in good['html']

    (cfg.item_dir('one') / state.assets_for('one')[0]['local_path']).unlink()
    broken = api.get_article('one')
    assert broken['images_inlined'] == 0 and broken['images_missing'] == 1
    assert 'data:image/png;base64,' not in broken['html']
    assert 'data-archive-missing' in broken['html']


def test_gui_article_rejects_unknown_key(archive, monkeypatch):
    gui = _gui(monkeypatch)
    cfg, state = archive
    assert 'error' in gui.Api(cfg).get_article('没有这个 key')


def test_gui_export_cancelled_when_no_directory_chosen(archive, monkeypatch):
    """用户在原生目录选择器里按取消时，不得写出任何文件。"""
    gui = _gui(monkeypatch)
    cfg, state = archive
    api = gui.Api(cfg)
    monkeypatch.setattr(api, 'choose_directory', lambda: None)
    assert api.export_to('selection', ['one']) == {'ok': False, 'cancelled': True}
    assert not list((cfg.root / 'exports').glob('*.zip')) if (cfg.root / 'exports').exists() else True


def test_gui_export_writes_zip_into_chosen_directory(archive, monkeypatch, tmp_path):
    gui = _gui(monkeypatch)
    cfg, state = archive
    download(cfg, state)
    api = gui.Api(cfg)
    target = tmp_path / 'out'; target.mkdir()
    monkeypatch.setattr(api, 'choose_directory', lambda: str(target))
    monkeypatch.setattr(gui, '_notify', lambda *a: None)
    result = api.export_to('images', ['one'])
    assert result['ok'] and Path(result['path']).parent == target
    assert Path(result['path']).suffix == '.zip' and Path(result['path']).is_file()


def test_gui_export_selection_with_real_archive(archive, monkeypatch, tmp_path):
    """回归：export_to('selection') 曾漏传 formats，按钮每次点击都抛 TypeError。
    之前的测试只覆盖了 images 分支和取消分支，没走到这里。"""
    gui = _gui(monkeypatch)
    cfg, state = archive
    download(cfg, state)
    api = gui.Api(cfg)
    monkeypatch.setattr(api, 'choose_directory', lambda: str(tmp_path))
    monkeypatch.setattr(gui, '_notify', lambda *a: None)
    result = api.export_to('selection', [])
    assert result['ok'], result.get('error')
    import zipfile
    with zipfile.ZipFile(result['path']) as bundle:
        names = bundle.namelist()
    assert any(n.endswith('.md') for n in names) and any(n.endswith('.txt') for n in names)


def _export_all(monkeypatch, cfg, target):
    gui = _gui(monkeypatch)
    api = gui.Api(cfg)
    monkeypatch.setattr(api, 'choose_directory', lambda: str(target))
    monkeypatch.setattr(gui, '_notify', lambda *a: None)
    monkeypatch.setattr(gui, '_reveal', lambda *a: None)
    return api


def test_gui_export_all_builds_complete_offline_package(archive, monkeypatch, tmp_path):
    """一键导出全部：一个文件夹，双击即可浏览，所有格式齐备。"""
    cfg, state = archive
    download(cfg, state)
    result = _export_all(monkeypatch, cfg, tmp_path).export_all()
    assert result['ok'], result.get('error')
    package = Path(result['path'])
    for name in ('打开归档.html', 'index.html', 'articles', '文本版-Markdown与TXT.zip',
                 'WordPress导入.xml', '总目录.csv', '总目录.json', '说明.txt'):
        assert (package / name).exists(), f'导出包缺少 {name}'
    assert result['articles'] == 1 and not result['warnings']
    # 图片随附且走相对路径，不依赖网络
    article = next((package / 'articles').iterdir())
    assert list(article.rglob('*.png'))
    assert 'assets/' in (article / 'index.html').read_text(encoding='utf-8')


def test_gui_export_all_never_overwrites_and_leaves_no_partial(archive, monkeypatch, tmp_path):
    cfg, state = archive
    download(cfg, state)
    api = _export_all(monkeypatch, cfg, tmp_path)
    first, second = api.export_all(), api.export_all()
    assert first['ok'] and second['ok'] and first['path'] != second['path']
    assert not [p for p in tmp_path.iterdir() if p.name.startswith('.')]   # 无 .partial 残留


def test_gui_export_all_skips_corrupted_images(archive, monkeypatch, tmp_path):
    """损坏的图片不得打进导出包 —— 包里只放校验通过的字节。"""
    cfg, state = archive
    download(cfg, state)
    (cfg.item_dir('one') / state.assets_for('one')[0]['local_path']).write_bytes(b'not a png')
    result = _export_all(monkeypatch, cfg, tmp_path).export_all()
    assert result['ok']
    assert not list(Path(result['path']).rglob('*.png'))


def test_gui_export_all_refuses_while_task_running(archive, monkeypatch, tmp_path):
    cfg, state = archive
    api = _export_all(monkeypatch, cfg, tmp_path)
    monkeypatch.setattr(api, 'get_task', lambda: {'status': 'running'})
    result = api.export_all()
    assert not result['ok'] and '任务' in result['error']
    # tmp_path 下本来就有夹具建的 archive/，只需确认没有生成任何导出包或半成品
    assert not [p for p in tmp_path.iterdir() if '百度空间归档' in p.name]


def test_gui_logout_keeps_endpoint_config(tmp_path, monkeypatch):
    """退出登录只删 Cookie；列表端点、UA 必须保留，否则重新登录后接口地址就丢了。"""
    import json
    from baidu_archive.auth import clear_credentials, save_cookie_session, load_credentials
    monkeypatch.delenv('BAIDU_ARCHIVE_COOKIE', raising=False)
    save_cookie_session('BDUSS=FAKE', list_url_template='https://wenzhang.baidu.com/fav/list?start={start}&limit={limit}',
                        user_agent='UA-KEEP', secrets_dir=tmp_path)
    result = clear_credentials(secrets_dir=tmp_path)
    saved = json.loads((tmp_path / 'session.json').read_text(encoding='utf-8'))
    assert saved['cookie_header'] == '' and 'fav/list' in saved['list_url_template']
    assert saved['user_agent'] == 'UA-KEEP'
    assert not load_credentials(secrets_dir=tmp_path).present
    assert result['env_cookie_still_set'] is False


def test_gui_logout_reports_env_cookie_it_cannot_clear(tmp_path, monkeypatch):
    from baidu_archive.auth import clear_credentials
    monkeypatch.setenv('BAIDU_ARCHIVE_COOKIE', 'BDUSS=FROMENV')
    assert clear_credentials(secrets_dir=tmp_path)['env_cookie_still_set'] is True


def test_config_file_page_limit_is_ignored(tmp_path, monkeypatch):
    """回归：config.json 里的 page_limit=100 让索引永远无法离线核实
    （已存原始页按 0/30/60… 存放，limit 对不上就报 unexpected_limit）。"""
    import json
    from baidu_archive.config import BAIDU_PAGE_LIMIT, load_config
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'config.json').write_text(json.dumps({'page_limit': 100, 'delay': 1.5}))
    cfg = load_config()
    assert cfg.page_limit == BAIDU_PAGE_LIMIT == 30
    assert cfg.delay == 1.5                              # 其他设置照常生效
    assert 'page_limit' in cfg.ignored_settings


# ---------------- 梳理：离线修复 / 问题分组 / 图片墙 ----------------

def _legacy_archive(cfg, state):
    """模拟旧版本留下的归档：图片字节都在，但缺少新版的身份校验记录，索引也没有 generation。"""
    download(cfg, state)
    for asset in state.assets_for('one'):
        state.record_asset_result('one', asset['original_url'], asset['attribute'], download_identity=None)
    state.set_meta('index_generation', None)


def test_repair_offline_recovers_legacy_archive_without_network(archive, monkeypatch):
    """旧版本归档：文件都在却被判为不可信。离线修复必须救回来，且一次网络请求都不发。"""
    gui = _gui(monkeypatch)
    cfg, state = archive
    _legacy_archive(cfg, state)
    api = gui.Api(cfg)
    monkeypatch.setattr(api, 'get_task', lambda: None)

    sent = []
    real = gui._offline_client
    def spying(config):
        client = real(config)
        original = client._client.send
        client._client.send = lambda request, **kw: sent.append(str(request.url)) or original(request, **kw)
        return client
    monkeypatch.setattr(gui, '_offline_client', spying)

    assert api.health()['needs_offline_repair'] is True
    result = api.repair_offline()
    assert result['ok'], result
    assert result['after']['complete'] == 1 and result['after']['index_complete'] is True
    assert result['after']['assets_downloaded'] == 1
    assert sent == [], f'离线修复不得联网，实际请求了 {sent}'
    assert api.health()['needs_offline_repair'] is False


def test_repair_offline_keeps_original_failure_reason_of_dead_images(archive, monkeypatch):
    """含失败图片的文章不能被交给离线重试 —— 否则 HTTP 504 之类的原始失败原因会被覆盖。"""
    gui = _gui(monkeypatch)
    cfg, state = archive
    with transport(cfg, lambda r: httpx.Response(504)) as client:
        run_assets(cfg, state, client)
    before = state.assets_for('one')[0]
    assert before['download_status'] == 'failed' and before['http_status'] == 504
    api = gui.Api(cfg); monkeypatch.setattr(api, 'get_task', lambda: None)
    assert api.repair_offline()['ok']
    after = state.assets_for('one')[0]
    assert after['http_status'] == 504 and after['error'] == before['error']


def test_repair_refuses_while_task_running(archive, monkeypatch):
    gui = _gui(monkeypatch)
    cfg, state = archive
    api = gui.Api(cfg); monkeypatch.setattr(api, 'get_task', lambda: {'status': 'running'})
    assert not api.repair_offline()['ok']


def test_problems_group_dead_images_with_plain_reason(archive, monkeypatch):
    """死链图片归到「无法找回」，并给出人话原因，而不是只给一个重试按钮。"""
    gui = _gui(monkeypatch)
    cfg, state = archive
    with transport(cfg, lambda r: httpx.Response(504)) as client:
        run_assets(cfg, state, client)
    groups = gui.Api(cfg).get_problems()
    assert [g['key'] for g in groups['dead_images']] == ['one']
    image = groups['dead_images'][0]['images'][0]
    assert image['url'] == 'photo.png' and '停止运营' in image['reason']
    assert groups['not_saved'] == [] and groups['unverified'] == []


@pytest.mark.parametrize('asset,expected', [
    ({'http_status': 404}, '已被删除'),
    ({'http_status': 403}, '拒绝访问'),
    ({'http_status': 504}, '停止运营'),
    ({'http_status': 200, 'error': '响应不是可识别的图片；原始响应已保存'}, '网页而不是图片'),
    ({'http_status': None, 'error': 'ConnectError: ...'}, '无法连接'),
])
def test_plain_failure_reason(monkeypatch, asset, expected):
    assert expected in _gui(monkeypatch)._plain_failure(asset)


def test_images_page_lists_only_verified_and_thumbnails_are_bytes_checked(archive, monkeypatch):
    gui = _gui(monkeypatch)
    cfg, state = archive
    download(cfg, state)
    api = gui.Api(cfg)
    listed = api.get_images()
    assert [i['key'] for i in listed] == ['one']
    thumb = api.get_thumbnail(listed[0]['id'])
    assert thumb and thumb.startswith('data:image/')
    # 字节损坏后：不再列出，也不再生成缩略图（新建 Api，避免内存缓存）
    (cfg.item_dir('one') / state.assets_for('one')[0]['local_path']).write_bytes(b'broken')
    fresh = gui.Api(cfg)
    assert fresh.get_images() == []
    assert fresh.get_thumbnail(listed[0]['id']) is None


def test_health_flags_risky_download_settings(archive, monkeypatch):
    gui = _gui(monkeypatch)
    cfg, state = archive
    cfg.concurrency, cfg.delay = 4, 0.25
    warnings = gui.Api(cfg).health()['settings_warnings']
    assert any('并发' in w for w in warnings) and any('间隔' in w for w in warnings)


# ---------------- 图床占位图 ----------------

def _as_placeholder(monkeypatch, body):
    """把一张合成图登记为占位图。真实的百度占位图属于第三方资源，不放进仓库。"""
    import hashlib
    from baidu_archive import media
    monkeypatch.setitem(media.KNOWN_PLACEHOLDERS, hashlib.sha256(body).hexdigest(), '测试占位图')


def test_known_placeholder_is_not_an_accepted_image(monkeypatch):
    from baidu_archive.media import validate_media_response
    assert validate_media_response('image', PNG, 'image/png')['status'] == 'accepted'
    _as_placeholder(monkeypatch, PNG)
    result = validate_media_response('image', PNG, 'image/png')
    assert result['status'] == 'placeholder' and result['reason'] == 'source_deleted_placeholder'


def test_downloaded_placeholder_is_not_counted_as_saved(archive, monkeypatch):
    """回归：图床把删除的原图换成「图片不存在」占位图并返回 200。只看结构会把它当成
    下载成功 —— 真实归档里 219 张「已核实」图片中有 89 张其实是同一张占位图。"""
    gui = _gui(monkeypatch)
    cfg, state = archive
    download(cfg, state)                      # 先按旧逻辑「成功」下载
    _as_placeholder(monkeypatch, PNG)         # 之后识别出它是占位图
    asset = state.assets_for('one')[0]
    assert not validate_downloaded_asset(cfg, asset)['ok']
    assert not article_truth(cfg, state, 'one')['complete']
    api = gui.Api(cfg)
    assert api.get_images() == []             # 不出现在图片墙
    groups = api.get_problems()
    assert [g['key'] for g in groups['baidu_deleted']] == ['one']
    assert '占位图' in groups['baidu_deleted'][0]['images'][0]['reason']
    assert groups['dead_images'] == []
    # 占位图文件仍原样保留在磁盘上，作为「原图已被删除」的证据
    assert (cfg.item_dir('one') / asset['local_path']).read_bytes() == PNG


def test_placeholder_at_download_time_is_kept_as_evidence(archive, monkeypatch):
    _as_placeholder(monkeypatch, PNG)
    cfg, state = archive
    download(cfg, state)
    asset = state.assets_for('one')[0]
    assert asset['download_status'] == 'failed' and 'placeholder' in asset['error']
    kept = list((cfg.raw_dir / 'asset_responses').rglob('*.bin'))
    assert kept and kept[0].read_bytes() == PNG


def test_aggressive_speed_in_config_file_is_clamped(tmp_path, monkeypatch):
    """回归：config.json 里被写入过 并发 4 / 间隔 0.25 秒。下载速度由软件负责，
    配置文件里超出安全边界的值收回到边界内，并在界面上说明。"""
    import json
    from baidu_archive.config import MAX_CONCURRENCY, MIN_DELAY, load_config
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'config.json').write_text(json.dumps({'concurrency': 4, 'delay': 0.25}))
    cfg = load_config()
    assert cfg.concurrency == MAX_CONCURRENCY == 3
    assert cfg.delay == MIN_DELAY == 0.5
    assert 'concurrency' in cfg.ignored_settings and 'delay' in cfg.ignored_settings


def test_default_speed_is_safe(tmp_path, monkeypatch):
    from baidu_archive.config import load_config
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert (cfg.concurrency, cfg.delay) == (2, 0.8) and not cfg.ignored_settings


def test_prepare_archive_migrates_legacy_silently_with_backup(archive, monkeypatch):
    """打开软件即自动迁移旧归档，用户不需要点任何「修复」；迁移前留数据库备份。"""
    gui = _gui(monkeypatch)
    cfg, state = archive
    _legacy_archive(cfg, state)
    api = gui.Api(cfg); monkeypatch.setattr(api, 'get_task', lambda: None)
    result = api.prepare_archive()
    assert result['migrated'] is True and result['state'] == 'ready'
    assert result['after']['complete'] == 1
    backup = Path(result['backup'])
    assert backup.is_file() and backup.parent == cfg.root / 'backups'
    import sqlite3
    with sqlite3.connect(backup) as db:                     # 备份是可用的完整数据库
        assert db.execute('select count(*) from records').fetchone()[0] == 1
    # 第二次打开：已迁移，什么都不做
    again = api.prepare_archive()
    assert again == {'state': 'ready', 'migrated': False}


def test_prepare_archive_on_fresh_install(tmp_path, monkeypatch):
    gui = _gui(monkeypatch)
    assert gui.Api(Config(root=tmp_path / 'nothing')).prepare_archive() == {'state': 'empty', 'migrated': False}


def _two_image_legacy_archive(tmp_path, monkeypatch):
    """一篇文章两张图：一张真照片、一张图床占位图；模拟旧版本留下的归档。"""
    import io
    from PIL import Image
    buffer = io.BytesIO(); Image.new('RGB', (3, 3), (200, 30, 30)).save(buffer, 'PNG')
    placeholder = buffer.getvalue()
    cfg = Config(root=tmp_path / 'archive', delay=0, jitter=0, max_retries=1,
                 list_url_template='https://wenzhang.baidu.com/fav/list?start={start}&limit={limit}')
    cfg.ensure_dirs()
    state = ArchiveState(cfg.db_path)
    with transport(cfg, lambda r: httpx.Response(200, content=list_body(['one']))) as client:
        run_index(cfg, state, client, force=True)
    raw_article(cfg, 'one', '<p>Body</p><img src="real.png"><img src="gone.png">')
    run_parse(cfg, state)
    bodies = {'real.png': PNG, 'gone.png': placeholder}
    with transport(cfg, lambda r: httpx.Response(200, content=bodies[str(r.url).rsplit('/', 1)[-1]],
                                                 headers={'content-type': 'image/png'})) as client:
        run_assets(cfg, state, client)
    assert {a['download_status'] for a in state.assets_for('one')} == {'ok'}
    _as_placeholder(monkeypatch, placeholder)              # 之后才认出它是占位图
    for asset in state.assets_for('one'):                   # 旧版本没有身份记录与索引证明
        state.record_asset_result('one', asset['original_url'], asset['attribute'], download_identity=None)
    state.set_meta('index_generation', None)
    return cfg, state


def test_offline_migration_never_rewrites_download_results(tmp_path, monkeypatch):
    """回归：离线整理曾调用 run_assets，后者把校验未通过的图片作废并尝试重下载，
    被离线客户端拒绝后把原本「下载成功」的记录改写成失败（真实归档里 111 条）。"""
    gui = _gui(monkeypatch)
    cfg, state = _two_image_legacy_archive(tmp_path, monkeypatch)
    before = {(a['original_url'], a['download_status'], a['http_status'], a['error'], a['local_path'])
              for a in state.assets_for('one')}
    import baidu_archive.assets as assets_module
    monkeypatch.setattr(assets_module, 'run_assets', lambda *a, **k: pytest.fail('离线整理不得调用 run_assets'))
    api = gui.Api(cfg); monkeypatch.setattr(api, 'get_task', lambda: None)
    assert api.prepare_archive()['migrated'] is True
    after = {(a['original_url'], a['download_status'], a['http_status'], a['error'], a['local_path'])
             for a in state.assets_for('one')}
    assert after == before


def test_placeholder_does_not_block_evidence_for_other_photos(tmp_path, monkeypatch):
    """回归：补证曾按篇「全有或全无」，一张占位图就让同篇真照片全部停在待校验，
    而且因此每次打开软件都会重新整理一遍。"""
    gui = _gui(monkeypatch)
    cfg, state = _two_image_legacy_archive(tmp_path, monkeypatch)
    api = gui.Api(cfg); monkeypatch.setattr(api, 'get_task', lambda: None)
    result = api.prepare_archive()
    by_url = {a['original_url']: a for a in state.assets_for('one')}
    assert validate_downloaded_asset(cfg, by_url['real.png'])['ok']          # 真照片通过校验
    assert not validate_downloaded_asset(cfg, by_url['gone.png'])['ok']      # 占位图不算保存成功
    assert result['after']['assets_downloaded'] == 1 and result['after']['assets_legacy_unverified'] == 0
    assert article_truth(cfg, state, 'one')['status'] == 'assets_partial'
    groups = api.get_problems()
    assert [g['key'] for g in groups['baidu_deleted']] == ['one'] and not groups['unverified']
    assert api.prepare_archive() == {'state': 'ready', 'migrated': False}    # 第二次打开什么都不做
