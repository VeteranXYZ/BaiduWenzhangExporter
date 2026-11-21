from pathlib import Path
import json
import zipfile

import pytest

from baidu_archive.config import Config
from baidu_archive.selection import SelectionError, export_selection
from baidu_archive.state import ArchiveState
from baidu_archive.storage import IntegrityError
from baidu_archive.utils import sha256_bytes


@pytest.fixture
def archive(tmp_path):
    config = Config(root=tmp_path / "archive")
    config.ensure_dirs()
    with ArchiveState(config.db_path) as state:
        for index, key in enumerate(("first", "second", "unfetched")):
            state.upsert_record({"key": key, "type": "page", "global_position": index})
            if key == "unfetched":
                continue
            folder = config.item_dir(key)
            folder.mkdir(parents=True)
            (folder / "content.original.html").write_text(f"<p>{key} 的正文</p>")
            (folder / "metadata.json").write_text(json.dumps({"key": key, "title": key,
                "list_date": "2010-01-01", "content_date": "2010-01-02",
                "iframe_url_original": "https://example.com/?sign=SHOULD_NOT_EXPORT"}))
        state.upsert_record({"key": "other", "type": "article", "global_position": 3})
        yield config, state


def test_selection_includes_only_chosen_records_in_original_order(archive):
    config, state = archive
    result = export_selection(config, state, keys=["second", "first", "second"], formats=["txt"])
    with zipfile.ZipFile(result["path"]) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert [entry["key"] for entry in manifest["entries"]] == ["first", "second"]
        assert len(archive.namelist()) == 5
        for file in manifest["files"]:
            assert sha256_bytes(archive.read(file["path"])) == file["sha256"]
        assert b"SHOULD_NOT_EXPORT" not in archive.read("articles/first/metadata.json")


@pytest.mark.parametrize("keys", [[], ["unknown"], ["other"], ["first", "unfetched"]])
def test_invalid_selection_does_not_publish_partial_bundle(archive, keys):
    config, state = archive
    with pytest.raises(SelectionError):
        export_selection(config, state, keys=keys, formats=["md"])
    assert not list(config.root.rglob("*.zip"))


def test_selection_bundle_keeps_markdown_image_paths_valid(archive):
    config, state = archive
    folder = config.item_dir("first")
    (folder / "assets").mkdir()
    body = (Path(__file__).parent / 'fixtures' / 'tiny.png').read_bytes()
    (folder / "assets" / "local.png").write_bytes(body)
    (folder / "content.original.html").write_text('<p>正文</p><img src="assets/local.png">')
    state.upsert_asset("first", {"kind": "image", "original_url": "https://image.example/a.png", "attribute": "src"})
    state.record_asset_result("first", "https://image.example/a.png", "src", download_status="ok",
                              local_path="assets/local.png", sha256=sha256_bytes(body))
    result = export_selection(config, state, keys=["first"], formats=["md", "txt"])
    with zipfile.ZipFile(result["path"]) as archive:
        assert b"assets/local.png" in archive.read("articles/first/content.md")
        assert archive.read("articles/first/assets/local.png") == body
    (folder / "assets" / "local.png").write_bytes(b"corrupt")
    result = export_selection(config, state, keys=["first", "second"], formats=["md", "txt"])
    with zipfile.ZipFile(result["path"]) as bundle:
        assert "正文" in bundle.read("articles/first/content.txt").decode()
        markdown = bundle.read("articles/first/content.md").decode()
        assert "图片未保存" in markdown and "assets/local.png" not in markdown
        assert "articles/first/assets/local.png" not in bundle.namelist()
        assert "articles/second/content.md" in bundle.namelist()
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest['count'] == 2 and manifest['missing_resources'] == 1
        assert manifest['entries'][0]['missing_resources'][0]['status'] == 'failed_integrity'
    assert (folder / 'assets' / 'local.png').read_bytes() == b'corrupt'


def test_selection_never_overwrites_existing_export(archive):
    config, state = archive
    target = config.root / "existing.zip"
    target.write_bytes(b"old export")
    with pytest.raises(SelectionError):
        export_selection(config, state, keys=["first"], formats=["txt"], output=target)
    assert target.read_bytes() == b"old export"


def add_image(config, state, key='first', url='https://example.test/image?sign=PRIVATE', kind='image', local='assets/same.png'):
    folder = config.item_dir(key)
    path = folder / local
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (Path(__file__).parent / 'fixtures' / 'tiny.png').read_bytes()
    path.write_bytes(body)
    state.upsert_asset(key, {'kind': kind, 'original_url': url, 'attribute': 'src'})
    state.record_asset_result(key, url, 'src', download_status='ok', local_path=local, sha256=sha256_bytes(body))
    return path


def test_verified_placeholder_does_not_block_either_zip(archive, monkeypatch):
    from PIL import Image

    from baidu_archive.media import KNOWN_PLACEHOLDERS
    from baidu_archive.selection import export_images

    config, state = archive
    good = add_image(config, state, url='https://example.test/good.png')
    placeholder_url = 'https://example.test/gone.png?sign=PRIVATE'
    placeholder = add_image(config, state, url=placeholder_url, local='assets/placeholder.png')
    Image.new('RGB', (4, 4), 'white').save(placeholder)
    original = placeholder.read_bytes()
    digest = sha256_bytes(original)
    monkeypatch.setitem(KNOWN_PLACEHOLDERS, digest, 'test placeholder')
    state.record_asset_result('first', placeholder_url, 'src', sha256=digest)
    (config.item_dir('first') / 'content.original.html').write_text(
        '<p>保留的正文</p><img src="assets/same.png"><img src="assets/placeholder.png">', encoding='utf-8')

    text = export_selection(config, state, keys=['first', 'second'], formats=['md', 'txt'])
    with zipfile.ZipFile(text['path']) as bundle:
        manifest = json.loads(bundle.read('manifest.json'))
        assert manifest['count'] == 2 and manifest['missing_resources'] == 1
        assert manifest['entries'][0]['missing_resources'][0]['status'] == 'source_placeholder'
        assert b'PRIVATE' not in bundle.read('manifest.json')
        md = bundle.read('articles/first/content.md').decode()
        assert '保留的正文' in md and 'assets/same.png' in md and '图片未保存' in md
        assert 'assets/placeholder.png' not in md
        assert bundle.read('articles/first/assets/same.png') == good.read_bytes()

    pictures = export_images(config, state, keys=['first', 'second'])
    with zipfile.ZipFile(pictures['path']) as bundle:
        manifest = json.loads(bundle.read('manifest.json'))
        assert manifest['images'] == 1 and manifest['not_downloaded'] == 1
        assert manifest['entries'][0]['not_downloaded'][0]['status'] == 'source_placeholder'
        assert bundle.read(manifest['files'][0]['path']) == good.read_bytes()
    assert placeholder.read_bytes() == original
    assert state.assets_for('first')[1]['download_status'] == 'ok'


def test_images_only_chosen_current_images_and_safe_manifest(archive):
    from baidu_archive.selection import export_images
    config, state = archive
    add_image(config, state)
    add_image(config, state, 'second')
    add_image(config, state, url='https://example.test/video', kind='video', local='assets/movie.mp4')
    state.upsert_asset('first', {'kind': 'image', 'original_url': 'https://bad.test/?sign=PRIVATE', 'attribute': 'src'})
    result = export_images(config, state, keys=['first'])
    with zipfile.ZipFile(result['path']) as bundle:
        assert bundle.testzip() is None
        manifest = json.loads(bundle.read('manifest.json'))
        assert [e['key'] for e in manifest['entries']] == ['first']
        assert result['images'] == 1 and result['not_downloaded'] == 1
        assert len(bundle.namelist()) == 2
        assert all(n.endswith(('.png', 'manifest.json')) for n in bundle.namelist())
        assert b'PRIVATE' not in bundle.read('manifest.json')
        for file in manifest['files']:
            assert sha256_bytes(bundle.read(file['path'])) == file['sha256']


def test_images_duplicate_references_deduplicate_files_and_separate_articles(archive):
    from baidu_archive.selection import export_images
    config, state = archive
    add_image(config, state)
    add_image(config, state, url='https://example.test/duplicate')
    add_image(config, state, 'second')
    result = export_images(config, state, keys=['second', 'first', 'first', 'unfetched'])
    with zipfile.ZipFile(result['path']) as bundle:
        manifest = json.loads(bundle.read('manifest.json'))
        assert [e['key'] for e in manifest['entries']] == ['first', 'second', 'unfetched']
        assert result['images'] == 2
        assert manifest['entries'][0]['image_references'] == 2
        assert len(set(bundle.namelist())) == 3


@pytest.mark.parametrize('damage', ['corrupt', 'missing', 'escape'])
def test_image_zip_keeps_verified_images_beside_unusable_items(archive, damage):
    from baidu_archive.selection import export_images

    config, state = archive
    url = 'https://example.test/image?sign=PRIVATE'
    rejected = add_image(config, state, url=url)
    good = add_image(config, state, 'second')
    if damage == 'corrupt':
        rejected.write_bytes(b'broken')
    elif damage == 'missing':
        rejected.unlink()
    else:
        state.record_asset_result('first', url, 'src', local_path='../outside.png')
    result = export_images(config, state, keys=['first', 'second'])
    with zipfile.ZipFile(result['path']) as bundle:
        manifest = json.loads(bundle.read('manifest.json'))
        assert result['images'] == 1 and result['not_downloaded'] == 1
        assert manifest['entries'][0]['not_downloaded'][0]['status'] == 'failed_integrity'
        assert bundle.read(manifest['files'][0]['path']) == good.read_bytes()
        assert b'PRIVATE' not in bundle.read('manifest.json')


@pytest.mark.parametrize('failure', ['empty', 'corrupt', 'missing', 'escape', 'stopped', 'inactive'])
def test_images_failure_never_publishes_partial_or_leaves_temporary_files(archive, failure):
    from baidu_archive.selection import export_images
    from baidu_archive.tasks import TaskControl, TaskStopped
    config, state = archive
    control = TaskControl()
    if failure != 'empty':
        path = add_image(config, state)
        if failure == 'corrupt':
            path.write_bytes(b'bad')
        elif failure == 'missing':
            path.unlink()
        elif failure == 'escape':
            state.record_asset_result('first', 'https://example.test/image?sign=PRIVATE', 'src', local_path='../outside.png')
        elif failure == 'stopped':
            control.stop()
        elif failure == 'inactive':
            state.set_current_assets('first', [])
    with pytest.raises((SelectionError, IntegrityError, TaskStopped)):
        export_images(config, state, keys=['first'], control=control)
    assert not list((config.root / 'exports').iterdir())


def test_images_stop_during_export_cleans_temporary_and_never_overwrites(archive):
    from baidu_archive.selection import export_images
    from baidu_archive.tasks import TaskControl, TaskStopped
    config, state = archive
    add_image(config, state)
    control = TaskControl()
    with pytest.raises(TaskStopped):
        export_images(config, state, keys=['first'], control=control, log=lambda *_: control.stop())
    assert not list((config.root / 'exports').iterdir())
    target = config.root / 'exports' / 'existing.zip'
    target.write_bytes(b'existing')
    with pytest.raises(SelectionError):
        export_images(config, state, keys=['first'], output=target)
    assert target.read_bytes() == b'existing'


@pytest.mark.parametrize('phase', ['before','conversion','zip','hash','publish'])
def test_selection_cancel_never_publishes_final_or_keeps_temporary(archive, monkeypatch, phase):
    from baidu_archive import selection
    from baidu_archive.tasks import TaskControl, TaskStopped
    config,state=archive
    control=TaskControl()
    if phase=='before':
        control.stop()
    elif phase=='conversion':
        original=selection.html_to_markdown
        def convert(body):
            control.stop()
            return original(body)
        monkeypatch.setattr(selection,'html_to_markdown',convert)
    elif phase=='zip':
        original=zipfile.ZipFile.writestr
        def write(self,*args,**kwargs):
            result=original(self,*args,**kwargs)
            control.stop()
            return result
        monkeypatch.setattr(zipfile.ZipFile,'writestr',write)
    elif phase=='hash':
        original=selection.sha256_file
        def digest(path):
            result=original(path)
            control.stop()
            return result
        monkeypatch.setattr(selection,'sha256_file',digest)
    elif phase=='publish':
        original=selection.os.link
        def publish(*args):
            original(*args)
            control.stop()
        monkeypatch.setattr(selection.os,'link',publish)
    with pytest.raises(TaskStopped):
        export_selection(config,state,keys=['first','second'],formats=['md','txt'],control=control)
    assert not list(config.root.rglob('*.zip'))
    assert not list(config.root.rglob('*.tmp'))


@pytest.mark.parametrize('phase', ['hash', 'publish'])
def test_image_export_cancel_during_publication_cleans_final(archive, monkeypatch, phase):
    from baidu_archive import selection
    from baidu_archive.tasks import TaskControl, TaskStopped
    config,state=archive
    add_image(config,state)
    control=TaskControl()
    if phase=='hash':
        original=selection.sha256_file
        def digest(path):
            result=original(path)
            control.stop()
            return result
        monkeypatch.setattr(selection,'sha256_file',digest)
    else:
        original=selection.os.link
        def publish(*args):
            original(*args)
            control.stop()
        monkeypatch.setattr(selection.os,'link',publish)
    with pytest.raises(TaskStopped):
        selection.export_images(config,state,keys=['first'],control=control)
    assert not list(config.root.rglob('*.zip'))
    assert not list(config.root.rglob('*.tmp'))
