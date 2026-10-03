from pathlib import Path
from conftest import seed_generation
from baidu_archive.assets import _asset_filename, image_extension, localize_html


def test_localize_rewrites_downloaded_only():
    html = ('<p><img src="http://ok.com/a.jpg">'
            '<img src="http://dead.com/b.jpg"></p>')
    out = localize_html(html, {"http://ok.com/a.jpg": "assets/0001_aa.jpg"})
    assert 'src="assets/0001_aa.jpg"' in out
    assert "http://dead.com/b.jpg" in out          # 失败的保持原样
    assert 'data-archive-original-src="http://ok.com/a.jpg"' in out


def test_localize_never_removes_failed_elements():
    """下载失败不得删除元素 —— 失效的 URL 本身就是档案信息。"""
    html = '<p><img src="http://dead1.com/a.jpg"><img src="http://dead2.com/b.jpg"></p>'
    out = localize_html(html, {})
    assert out.count("<img") == 2
    assert "dead1.com" in out and "dead2.com" in out


def test_localize_keeps_external_links_untouched():
    html = '<p><a href="http://www.renren.com/x">人人</a></p>'
    assert "renren.com" in localize_html(html, {"http://www.renren.com/x": "assets/x"})


def test_localize_handles_lazy_attributes():
    html = '<img data-src="http://ok.com/lazy.jpg">'
    out = localize_html(html, {"http://ok.com/lazy.jpg": "assets/0002_bb.jpg"})
    assert "assets/0002_bb.jpg" in out
    assert "http://ok.com/lazy.jpg" in out          # 原值仍可追溯


def test_localize_lazy_image_replaces_data_placeholder():
    html = '<img src="data:image/gif;base64,FAKE" data-src="https://images.example/photo.jpg">'
    out = localize_html(html, {"https://images.example/photo.jpg": "assets/local.jpg"})
    assert 'src="assets/local.jpg"' in out
    assert 'data-src="https://images.example/photo.jpg"' in out


def test_localize_returns_fragment_not_document():
    out = localize_html("<p>hi</p>", {})
    assert "<html>" not in out and "<body>" not in out


def test_localize_empty_input():
    assert localize_html("", {}) == ""


def test_localize_drops_srcset_only_when_localized():
    html = '<img src="http://ok.com/a.jpg" srcset="http://ok.com/a2.jpg 2x">'
    out = localize_html(html, {"http://ok.com/a.jpg": "assets/0001_aa.jpg"})
    assert "srcset=" not in out or "data-archive-original-srcset" in out
    assert "data-archive-original-srcset" in out    # 原值留档


def test_asset_filename_contains_order_and_hash():
    name = _asset_filename(7, "abcdef1234567890", ".jpg")
    assert name == "0007_abcdef123456.jpg"


def test_asset_filename_differs_for_different_content():
    """不同内容绝不共用文件名，避免互相覆盖。"""
    assert _asset_filename(1, "a" * 64, ".jpg") != _asset_filename(1, "b" * 64, ".jpg")


def test_image_signature_rejects_html_even_when_url_looks_like_image():
    assert image_extension(b"<!doctype html><html>login</html>") is None
    assert image_extension(b"{\"error\":\"gone\"}") is None
    assert image_extension(b"\xff\xd8\xff\xe0example") == ".jpg"
    assert image_extension(b"\x89PNG\r\n\x1a\nexample") == ".png"
    assert image_extension(b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"></svg>') == ".svg"


def test_image_download_rejects_http_200_html_and_preserves_response(tmp_path):
    import httpx
    from baidu_archive.assets import _download_one
    from baidu_archive.auth import Credentials
    from baidu_archive.client import ArchiveClient
    from baidu_archive.config import Config
    from baidu_archive.storage import read_raw

    config = Config(root=tmp_path, delay=0, jitter=0)
    body = b"<html>this resource no longer exists</html>"
    with ArchiveClient(config, Credentials()) as client:
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=body, headers={"content-type": "image/jpeg"})))
        result = _download_one(client, config, {"key": "sample", "kind": "image",
                               "resolved_url": "https://images.example/old.jpg"}, tmp_path / "assets", 1)
    assert result["download_status"] == "failed"
    assert "local_path" not in result
    raw = next((config.raw_dir / "asset_responses" / "sample").glob("*.bin"))
    assert read_raw(raw).content == body


import pytest
from baidu_archive.parsers import extract_assets, make_soup, parse_srcset


@pytest.mark.parametrize('attribute', ['data-src', 'data-original', 'data-lazy-src'])
def test_lazy_real_image_wins_over_downloaded_http_placeholder(attribute):
    html = f'<img src="https://example.test/spacer.gif" {attribute}="https://example.test/photo.jpg">'
    node = make_soup(localize_html(html, {'https://example.test/spacer.gif': 'assets/spacer.gif',
                                       'https://example.test/photo.jpg': 'assets/photo.jpg'})).img
    assert node['src'] == 'assets/photo.jpg'
    assert node[attribute] == 'https://example.test/photo.jpg'
    assert node['data-archive-original-src'] == 'https://example.test/photo.jpg'


def test_lazy_priority_is_explicit_and_unavailable_candidates_fall_back():
    html = '<img src="https://x/fallback" data-src="https://x/first" data-original="https://x/second" data-lazy-src="https://x/third">'
    mapping = {f'https://x/{x}': f'assets/{x}.jpg' for x in ('first', 'second', 'third', 'fallback')}
    assert make_soup(localize_html(html, mapping)).img['src'] == 'assets/first.jpg'
    del mapping['https://x/first']
    assert make_soup(localize_html(html, mapping)).img['src'] == 'assets/second.jpg'


@pytest.mark.parametrize('descriptor,sizes', [('1x', ''), ('400w', ' sizes="50vw"')])
def test_srcset_only_retains_descriptors_and_gets_local_fallback(descriptor, sizes):
    html = f'<img srcset="https://x/a.jpg {descriptor}, https://x/missing.jpg 2x"{sizes}>'
    node = make_soup(localize_html(html, {'https://x/a.jpg': 'assets/a.jpg'})).img
    assert node['src'] == 'assets/a.jpg'
    assert node['srcset'] == f'assets/a.jpg {descriptor}'
    assert node.get('sizes') == ('50vw' if sizes else None)
    assert 'https://x/missing.jpg' in node['data-archive-original-srcset']


def test_src_and_responsive_srcset_keep_width_and_sizes_semantics():
    html = '<img src="https://x/a" srcset="https://x/b 400w, https://x/c 800w" sizes="(max-width: 500px) 100vw, 50vw">'
    node = make_soup(localize_html(html, {f'https://x/{c}':f'assets/{c}.jpg' for c in 'abc'})).img
    assert node['src'] == 'assets/a.jpg'
    assert node['srcset'] == 'assets/b.jpg 400w, assets/c.jpg 800w'
    assert node['sizes'] == '(max-width: 500px) 100vw, 50vw'


def test_picture_sources_are_discovered_and_keep_art_direction():
    html = '<picture><source media="(min-width: 600px)" type="image/webp" srcset="https://x/wide 1x, https://x/wide2 2x"><source media="(max-width: 599px)" srcset="https://x/missing 1x"><img src="https://x/fallback"></picture>'
    assets = extract_assets(html)
    assert {a['original_url'] for a in assets if a['kind'] == 'image'} == {'https://x/wide','https://x/wide2','https://x/missing','https://x/fallback'}
    soup = make_soup(localize_html(html, {f'https://x/{c}':f'assets/{c}.jpg' for c in ('wide', 'wide2', 'fallback')}))
    assert soup.source['srcset'] == 'assets/wide.jpg 1x, assets/wide2.jpg 2x'
    assert soup.source['media'] == '(min-width: 600px)' and soup.source['type'] == 'image/webp'
    assert not soup.find_all('source')[1].has_attr('srcset')
    assert soup.img['src'] == 'assets/fallback.jpg'


def test_srcset_parser_keeps_data_uri_commas_and_does_not_download_fragments():
    value = 'data:image/png;base64,ABC 1x, https://x/a.jpg 2x'
    assert parse_srcset(value) == [('data:image/png;base64,ABC','1x'), ('https://x/a.jpg','2x')]
    assets = extract_assets(f'<img srcset="{value}">')
    assert len(assets) == 2 and assets[0]['scheme_kind'] == 'data'
    node = make_soup(localize_html(f'<img srcset="{value}">', {'https://x/a.jpg':'assets/a.jpg'})).img
    assert node['srcset'] == 'data:image/png;base64,ABC 1x, assets/a.jpg 2x'


def test_failed_srcset_is_preserved_when_nothing_was_downloaded():
    html = '<img srcset="https://x/a 1x, https://x/b 2x">'
    assert make_soup(localize_html(html, {})).img['srcset'] == 'https://x/a 1x, https://x/b 2x'


def test_include_media_after_image_completion_is_incremental(tmp_path):
    import httpx
    from baidu_archive.assets import run_assets
    from baidu_archive.auth import Credentials
    from baidu_archive.client import ArchiveClient
    from baidu_archive.config import Config
    from baidu_archive.state import ArchiveState
    from baidu_archive.storage import write_json_atomic
    config = Config(root=tmp_path / 'archive', delay=0, jitter=0)
    config.ensure_dirs()
    folder = config.item_dir('one')
    folder.mkdir()
    body = '<img src="https://x/image.png"><video src="https://x/movie.mp4"></video><audio src="https://x/music.mp3"></audio>'
    (folder / 'content.original.html').write_text(body)
    write_json_atomic(folder / 'metadata.json', {'key':'one'})
    requests = []
    def respond(request):
        requests.append(request.url.path)
        body = (Path(__file__).parent / 'fixtures' / 'tiny.png').read_bytes() if request.url.path.endswith('png') else (Path(__file__).parent / 'fixtures' / ('tiny.mp4' if request.url.path.endswith('mp4') else 'tiny.wav')).read_bytes()
        return httpx.Response(200, content=body, headers={'content-type':'image/png' if request.url.path.endswith('png') else 'application/octet-stream'})
    with ArchiveState(config.db_path) as state, ArchiveClient(config, Credentials()) as client:
        state.upsert_record({'key':'one','type':'page','global_position':0})
        state.update_item('one', status='parsed')
        seed_generation(state, ['one'])
        from baidu_archive.pipeline import run_parse
        from baidu_archive.storage import save_raw
        page = '<h1 id="detailArticleTitle_x">One</h1><div id="detailArticleContent_x">' + body + '</div><div id="detailArticleFooter_x"></div>'
        save_raw(config.content_dir / 'one.html', page.encode(), url='https://x/body', status_code=200)
        run_parse(config, state)
        for asset in extract_assets(body):
            state.upsert_asset('one', asset)
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(respond))
        assert run_assets(config, state, client)['downloaded'] == 1
        assert state.get_item('one')['status'] == 'complete'
        assert run_assets(config, state, client, include_media=True)['downloaded'] == 2
        assert sorted(requests) == ['/image.png', '/movie.mp4', '/music.mp3']
        assert run_assets(config, state, client, include_media=True)['attempted'] == 0
        assert len(requests) == 3


def test_complete_legacy_localization_is_rebuilt_without_redownloading(tmp_path):
    from types import SimpleNamespace
    from baidu_archive.assets import run_assets
    from baidu_archive.config import Config
    from baidu_archive.state import ArchiveState
    from baidu_archive.storage import write_json_atomic
    from baidu_archive.tasks import TaskControl
    from baidu_archive.utils import sha256_bytes
    config = Config(root=tmp_path / 'archive')
    config.ensure_dirs()
    folder = config.item_dir('one')
    (folder/'assets').mkdir(parents=True)
    original = '<img src="https://x/spacer" data-src="https://x/photo">'
    (folder/'content.original.html').write_text(original)
    (folder/'content.local.html').write_text('<img src="https://x/spacer">')
    write_json_atomic(folder/'metadata.json', {})
    with ArchiveState(config.db_path) as state:
        state.upsert_record({'key':'one','type':'page'})
        state.update_item('one', status='complete')
        for asset in extract_assets(original):
            state.upsert_asset('one',asset)
            body=(Path(__file__).parent / 'fixtures' / 'tiny.png').read_bytes()
            name='assets/'+asset['original_url'].rsplit('/',1)[-1]+'.png'
            (folder/name).write_bytes(body)
            state.record_asset_result('one',asset['original_url'],asset['attribute'],download_status='ok',local_path=name,sha256=sha256_bytes(body))
        result=run_assets(config,state,SimpleNamespace(control=TaskControl()))
        assert result['attempted'] == 0
        assert make_soup((folder/'content.local.html').read_text()).img['src'] == 'assets/photo.png'
