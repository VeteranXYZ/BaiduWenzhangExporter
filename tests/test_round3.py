"""Round 3 original reproductions with real parsers, SQLite, file reads and task completion."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest
from PIL import Image

from baidu_archive import cli, server, safe_files
from baidu_archive.auth import Credentials, AuthError, parse_netscape_cookies
from baidu_archive.integrity import audit_stored_files
from baidu_archive.legacy import rebuild_legacy_evidence
from baidu_archive.media import validate_media_response
from baidu_archive.parsers import parse_list_json, ListResponseError
from baidu_archive.state import ArchiveState, SCHEMA_VERSION, SchemaVersionError
from baidu_archive.storage import read_json, write_json_atomic
from baidu_archive.truth import article_truth, index_truth, resource_state
from baidu_archive.verify import run_verify
from test_rc_regressions import archive, download, raw_article, transport, list_body, PNG, FIXTURES
from baidu_archive.pipeline import run_index, run_parse


def empty_mp4():
    return (b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomavc1' +
            b'\x00\x00\x00\x08moov' + b'\x00\x00\x00\x08mdat')


BAD_MEDIA = [(b'\x89PNG\r\n\x1a\n', 'image', 'image/png'),
             (PNG[:-12], 'image', 'image/png'), (b'ID3', 'audio', 'audio/mpeg'),
             (b'fLaC', 'audio', 'audio/flac'), (empty_mp4(), 'video', 'video/mp4'),
             (b'<html>login</html>', 'video', 'video/mp4'), (b'', 'video', 'video/mp4'),
             ((FIXTURES / 'tiny.wav').read_bytes()[:-1], 'audio', 'audio/wav')]


@pytest.mark.parametrize('body,kind,mime', BAD_MEDIA)
def test_original_header_only_attacks_never_complete(archive, body, kind, mime):
    cfg, state = archive
    tag = '<img src="resource">' if kind == 'image' else f'<{kind} src="resource"></{kind}>'
    raw_article(cfg, 'one', '<p>Body</p>' + tag)
    run_parse(cfg, state, force=True)
    stats, requests = download(cfg, state, body, mime, include_media=True)
    assert requests and stats['failed'] == 1 and stats['downloaded'] == 0
    assert not article_truth(cfg, state, 'one')['complete']
    assert not run_verify(cfg, state)['validation']['ok']
    assert cli.main(['--root', str(cfg.root), 'verify']) == 1
    app = server.Application(cfg)
    app.start({'kind': 'verify'})
    app.thread.join(5)
    assert not app.thread.is_alive() and app.snapshot()['status'] == 'partial'
    assert not app.snapshot()['result']['ok'] and '校验未通过' in app.snapshot()['message']
    if body.startswith(b'<html>'):
        summary = run_verify(cfg, state)
        assert summary['stored_file_integrity']['ok']  # Bytes intact; archive incomplete.
        assert summary['stored_file_integrity']['scope'] == 'stored_files_only'
        assert 'integrity' not in summary


@pytest.mark.parametrize('name,kind,mime', [('tiny.png','image','image/png'),
                                         ('tiny.wav','audio','audio/wav'),
                                         ('tiny.mp4','video','video/mp4')])
def test_true_media_structure(name, kind, mime):
    result = validate_media_response(kind, (FIXTURES / name).read_bytes(), mime)
    assert result['status'] == 'accepted' and result['structurally_valid'] and result['downloaded']


@pytest.mark.parametrize('format,mime', [('JPEG','image/jpeg'), ('GIF','image/gif'), ('WEBP','image/webp')])
def test_image_decoder_and_truncation(format, mime):
    stream = io.BytesIO()
    Image.new('RGB', (2, 2), 'red').save(stream, format=format)
    body = stream.getvalue()
    assert validate_media_response('image', body, mime)['status'] == 'accepted'
    assert validate_media_response('image', body[:10], mime)['status'] != 'accepted'
    assert validate_media_response('image', body[:-1], mime)['status'] != 'accepted'


def test_unsupported_media_is_unverifiable():
    result = validate_media_response('audio', b'ID3' + b'\x00' * 30, 'audio/mpeg')
    assert result['status'] == 'unverifiable' and result['downloaded'] and not result['structurally_valid']


@pytest.mark.parametrize('value', [False, True, 0.5, 1.0, -1, '', '1.2', 'invalid', float('nan'), float('inf')])
@pytest.mark.parametrize('field', ['totalCount', 'currentCount', 'start', 'limit'])
def test_count_metadata_rejects_coercion(field, value):
    payload = json.loads(list_body([]))
    payload['data'][field] = value
    with pytest.raises(ListResponseError): parse_list_json(payload)


@pytest.mark.parametrize('value', [False, True, 0.0, 1, 'success', None, [], {}])
def test_status_types_are_strict(value):
    payload = json.loads(list_body([])); payload['status'] = value
    with pytest.raises(ListResponseError): parse_list_json(payload)


@pytest.mark.parametrize('value', [0, '0', 1, '1'])
def test_explicit_integer_representations(value):
    payload = json.loads(list_body(['one'] if int(value) else []))
    payload['data']['totalCount'] = value
    assert parse_list_json(payload)['total_count'] == int(value)


@pytest.mark.parametrize('field,value', [('totalCount',False),('totalCount',0.5),('status',False)])
def test_original_abnormal_empty_response_keeps_history(archive, field, value):
    cfg, state = archive
    download(cfg, state)
    payload = json.loads(list_body([]))
    (payload['data'] if field != 'status' else payload)[field] = value
    with transport(cfg, lambda r: httpx.Response(200, content=json.dumps(payload).encode())) as client:
        with pytest.raises(ListResponseError): run_index(cfg, state, client, force=True)
    assert state.get_meta('index_generation')['status'] == 'failed' and not index_truth(state)['ok']
    assert state.get_record('one') and not run_verify(cfg, state)['validation']['ok']
    from baidu_archive.storage import read_raw
    assert json.loads(read_raw(cfg.lists_dir / 'list_start00000.json').content) == payload


def convert_to_legacy(cfg, state):
    """Use actual old DDL, then copy only old columns; no constructor backfill."""
    old_path = cfg.root / 'old.db'
    with sqlite3.connect(old_path) as old:
        old.executescript((FIXTURES / 'legacy_schema.sql').read_text())
        for table in ('meta','records','items','assets','failures','runs'):
            columns = [r[1] for r in old.execute('PRAGMA table_info(' + table + ')')]
            rows = state._conn.execute('SELECT ' + ','.join(columns) + ' FROM ' + table).fetchall()
            if rows:
                old.executemany('INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES(' +
                                ','.join('?' for _ in columns) + ')', [tuple(r) for r in rows])
    state.close()
    cfg.db_path.unlink()
    old_path.replace(cfg.db_path)


@pytest.mark.parametrize('damage', [None, 'raw', 'base', 'relation', 'body', 'size', 'digest', 'no_index'])
def test_legacy_migration_offline_proof_and_no_network(archive, monkeypatch, damage, capsys):
    cfg, state = archive
    download(cfg, state)
    asset = state.assets_for('one')[0]
    saved_path = cfg.item_dir('one') / asset['local_path']
    before = saved_path.read_bytes()
    convert_to_legacy(cfg, state)
    meta_path = cfg.item_dir('one') / 'metadata.json'
    meta = read_json(meta_path)
    # Old derivatives predate parsed body/fingerprint evidence.
    for field in ('parsed_sha256','parsed_content_path','resource_fingerprint','localized_resource_fingerprint'):
        meta.pop(field, None)
    write_json_atomic(meta_path, meta)
    def forbidden(*a, **kw): pytest.fail('Legacy proof must not construct any HTTP client')
    monkeypatch.setattr(server, 'ArchiveClient', forbidden)
    monkeypatch.setattr(httpx, 'Client', forbidden)
    with ArchiveState(cfg.db_path) as migrated:
        assert migrated._conn.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        row = migrated.assets_for('one')[0]
        assert row['download_identity'] is None
        assert resource_state(cfg, row)['state'] == 'legacy_unverified'
        assert server.Application(cfg).library()['summary']['legacy_saved_images'] == 1
        assert cli.main(['--root',str(cfg.root),'status']) == 0
        assert '文件仍可校验' in capsys.readouterr().out
        if damage == 'raw': (cfg.content_dir / 'one.html').unlink()
        elif damage == 'base': meta['iframe_url_original'] = 'https://wrong.test/'; write_json_atomic(meta_path,meta)
        elif damage == 'relation': migrated._conn.execute("UPDATE assets SET resolved_url='https://wrong.test/p.png'"); migrated._conn.commit()
        elif damage == 'body': (cfg.item_dir('one')/'content.original.html').write_text('changed')
        elif damage == 'size': migrated._conn.execute('UPDATE assets SET size=size+1'); migrated._conn.commit()
        elif damage == 'digest': migrated._conn.execute("UPDATE assets SET sha256=?",('0'*64,)); migrated._conn.commit()
        elif damage == 'no_index': migrated.set_meta('index_generation', None)
        result = rebuild_legacy_evidence(cfg, migrated)
        proved = damage in {None, 'no_index'}
        assert result['verified'] == int(proved)
        assert result['legacy_unverified'] == int(not proved)
        assert (migrated.assets_for('one')[0]['download_identity'] is not None) == proved
        assert article_truth(cfg, migrated, 'one')['complete'] == (damage is None)
        assert not result['reasons'] if proved else result['reasons']
        assert saved_path.read_bytes() == before
        assert rebuild_legacy_evidence(cfg, migrated)['verified'] == 0
        if damage is None:
            assert cli.main(['--root',str(cfg.root),'verify','--rebuild-legacy-evidence']) == 0
            app=server.Application(cfg); app.start({'kind':'verify','rebuild_legacy':True}); app.thread.join(5)
            assert not app.thread.is_alive() and app.snapshot()['status']=='completed'


def test_same_url_new_bytes_retains_and_audits_history(archive):
    cfg, state = archive
    download(cfg, state)
    old = state.assets_for('one')[0]
    old_path = cfg.item_dir('one') / old['local_path']
    stream = io.BytesIO(); Image.new('RGB',(2,2),'blue').save(stream, format='PNG')
    download(cfg, state, stream.getvalue(), force=True)
    version, = state.asset_history('one')
    assert version['reason']=='download_replaced'
    assert version['record']['sha256']==old['sha256'] and version['record']['size']==old['size']
    assert version['record']['download_identity']==old['download_identity']
    assert old_path.read_bytes()==PNG and run_verify(cfg,state)['validation']['ok']
    old_path.unlink()
    report = audit_stored_files(cfg,state)
    assert report['current_resource_integrity']['ok']
    assert not report['historical_retained_resource_integrity']['ok']
    assert not report['ok'] and not run_verify(cfg,state)['validation']['ok']


def test_failed_force_attempt_retains_prior_good_version(archive):
    cfg,state=archive; download(cfg,state)
    old=state.assets_for('one')[0]
    download(cfg,state,b'<html>login</html>',force=True)
    version,=state.asset_history('one')
    assert version['record']['sha256']==old['sha256']
    assert (cfg.item_dir('one')/old['local_path']).is_file()
    assert audit_stored_files(cfg,state)['historical_retained_resource_integrity']['ok']


def test_descriptor_read_cannot_follow_swapped_symlink(archive, monkeypatch):
    cfg,state=archive; download(cfg,state)
    row=state.assets_for('one')[0]; target=cfg.item_dir('one')/row['local_path']
    outside=cfg.root.parent/'outside.png'; outside.write_bytes(b'OUTSIDE SECRET')
    real_open=os.open
    def swap(path, flags, *args, **kwargs):
        if str(path)==target.name:
            target.unlink(); target.symlink_to(outside)
        return real_open(path,flags,*args,**kwargs)
    monkeypatch.setattr(safe_files.os,'open',swap)
    from baidu_archive.truth import validate_downloaded_asset
    result=validate_downloaded_asset(cfg,row)
    assert not result['ok'] and 'body' not in result


def test_descriptor_read_fails_closed_without_platform_support(archive, monkeypatch):
    cfg,state=archive; download(cfg,state)
    monkeypatch.setattr(safe_files,'SAFE_DESCRIPTOR_READ',False)
    with pytest.raises(safe_files.UnsafeRead,match='unavailable'):
        safe_files.read_resource(cfg.root, Path('missing'))
    assert not article_truth(cfg,state,'one')['complete']


def old_empty_db(path):
    with sqlite3.connect(path) as conn: conn.executescript((FIXTURES/'legacy_schema.sql').read_text())


@pytest.mark.parametrize('attempt', range(8))
def test_migration_concurrent_processes(tmp_path, attempt):
    path=tmp_path/'old.db'; old_empty_db(path)
    code='from pathlib import Path; from baidu_archive.state import ArchiveState; import sys; s=ArchiveState(Path(sys.argv[1])); s.close()'
    processes=[subprocess.Popen([sys.executable,'-c',code,str(path)], stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(4)]
    for process in processes:
        out,err=process.communicate(timeout=40)
        assert process.returncode==0,err.decode()
    for _ in range(3):
        with ArchiveState(path) as state:
            assert state._conn.execute('PRAGMA user_version').fetchone()[0]==SCHEMA_VERSION
            assert state._conn.execute('SELECT download_identity FROM assets').fetchall()==[]


def test_migration_process_killed_rolls_back_and_retries(tmp_path):
    path=tmp_path/'old.db'; old_empty_db(path)
    code='''from baidu_archive.state import ArchiveState
import os,sys
def interrupt(self):
 self._conn.execute('ALTER TABLE assets ADD COLUMN active INTEGER NOT NULL DEFAULT 1')
 os._exit(17)
ArchiveState._migrate=interrupt
ArchiveState(sys.argv[1])
'''
    assert subprocess.run([sys.executable,'-c',code,str(path)],timeout=30).returncode==17
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0]==0
        assert 'active' not in {r[1] for r in conn.execute('PRAGMA table_info(assets)')}
    with ArchiveState(path) as state:
        assert state._conn.execute('PRAGMA user_version').fetchone()[0]==SCHEMA_VERSION


def test_future_schema_fails_without_modification(tmp_path):
    path=tmp_path/'future.db'; old_empty_db(path)
    with sqlite3.connect(path) as conn: conn.execute('PRAGMA user_version=999')
    before=path.read_bytes()
    with pytest.raises(SchemaVersionError): ArchiveState(path)
    assert path.read_bytes()==before


@pytest.mark.parametrize('domain,flag,path,secure,expires,url,expected', [
    ('wenzhang.baidu.com','FALSE','/fav','TRUE',0,'https://wenzhang.baidu.com/fav/list',True),
    ('wenzhang.baidu.com','FALSE','/fav','TRUE',0,'https://wenzhang.baidu.com/page/view',False),
    ('wenzhang.baidu.com','FALSE','/wrong','TRUE',0,'https://wenzhang.baidu.com/fav/list',False),
    ('wenzhang.baidu.com','FALSE','/fav','TRUE',1,'https://wenzhang.baidu.com/fav/list',False),
    ('wenzhang.baidu.com','FALSE','/fav','TRUE',0,'http://wenzhang.baidu.com/fav/list',False),
    ('wenzhang.baidu.com','FALSE','/fav','FALSE',0,'http://wenzhang.baidu.com/fav/list',True),
    ('wenzhang.baidu.com','FALSE','/fav','TRUE',0,'https://sub.wenzhang.baidu.com/fav/list',False),
    ('.baidu.com','TRUE','/fav','TRUE',0,'https://wenzhang.baidu.com/fav/list',True),
])
def test_cookie_endpoint_scope(domain,flag,path,secure,expires,url,expected):
    jar=parse_netscape_cookies('\t'.join(map(str,[domain,flag,path,secure,expires,'BDUSS','test'])))
    assert Credentials(cookie_jar=jar).available_for(url)==expected


def test_original_fav_cookie_cli_server_preflight(archive, monkeypatch):
    cfg,state=archive
    credentials=Credentials(cookie_jar=parse_netscape_cookies('wenzhang.baidu.com\tFALSE\t/fav\tTRUE\t0\tBDUSS\ttest'))
    monkeypatch.setattr(cli,'load_credentials',lambda:credentials)
    with cli._client(cfg,lambda *a:None) as client:
        assert client.credentials.header_for(cfg.list_url(0))=='BDUSS=test'
    with pytest.raises(AuthError): cli._client(cfg,lambda *a:None,endpoint=cfg.detail_url('one'))
    monkeypatch.setattr(server,'load_credentials',lambda:credentials)
    monkeypatch.setattr(server,'ArchiveClient',lambda *a,**kw:transport(cfg,lambda r:httpx.Response(200,content=list_body(['one'])),kw['control']))
    app=server.Application(cfg); app.start({'kind':'index'}); app.thread.join(5)
    assert not app.thread.is_alive() and app.snapshot()['status']=='completed'
    with pytest.raises(server.InputError): app.start({'kind':'archive','keys':['one']})


def test_git_required_file_audit_rejects_missing_runtime():
    import importlib.util
    source=Path(__file__).parents[1]/'scripts/check_release_package.py'
    spec=importlib.util.spec_from_file_location('package_gate',source)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    root=source.parents[1]
    # This also works in the already constructed clean release tree, without .git.
    manifest={str(p.relative_to(root)) for folder in ('baidu_archive','tests','scripts') for p in (root/folder).rglob('*') if p.is_file()}
    manifest.update({'pyproject.toml','README.md','LICENSE','requirements.txt','requirements-dev.txt'})
    module.audit_required_files(root,manifest)
    for name in ('baidu_archive/truth.py', 'baidu_archive/media.py', 'LICENSE'):
        with pytest.raises(RuntimeError,match=name): module.audit_required_files(root,manifest-{name})


def test_resolved_url_history_is_not_a_diagnostic_cache(archive):
    cfg,state=archive; download(cfg,state)
    old=state.assets_for('one')[0]; path=cfg.item_dir('one')/old['local_path']
    raw_article(cfg,'one','<p>Body</p><img src="photo.png">',base='https://new.test/body')
    run_parse(cfg,state,force=True); download(cfg,state)
    assert state.asset_history('one')[0]['reason']=='resolved_url_changed'
    path.unlink()
    report=audit_stored_files(cfg,state)
    assert not report['historical_retained_resource_integrity']['ok']
    assert not run_verify(cfg,state)['validation']['ok']


def test_server_media_read_race_never_returns_outside_bytes(archive, monkeypatch):
    from urllib.request import urlopen
    from urllib.error import HTTPError
    cfg,state=archive; download(cfg,state)
    row=state.assets_for('one')[0]; target=cfg.item_dir('one')/row['local_path']
    secret=cfg.root.parent/'secret'; secret.write_bytes(b'OUTSIDE SECRET')
    app=server.Application(cfg); service=server.LocalServer(('127.0.0.1',0),app)
    thread=threading.Thread(target=service.serve_forever); thread.start()
    real_open=os.open
    def swap(path,flags,*args,**kwargs):
        if str(path)==target.name:
            target.unlink(); target.symlink_to(secret)
        return real_open(path,flags,*args,**kwargs)
    monkeypatch.setattr(safe_files.os,'open',swap)
    try:
        with pytest.raises(HTTPError) as exc:
            urlopen('http://127.0.0.1:'+str(service.server_port)+'/media/one/'+target.name)
        assert exc.value.code==404
    finally:
        service.shutdown(); service.server_close(); thread.join(5)
        assert not thread.is_alive()


@pytest.mark.parametrize('damage', ['no_track','no_samples','no_frames','zero_dimensions'])
def test_mp4_requires_track_sample_and_frame_structure(damage):
    body=bytearray((FIXTURES/'tiny.mp4').read_bytes())
    if damage=='no_track':
        p=body.index(b'trak'); body[p:p+4]=b'free'
    elif damage=='no_samples':
        p=body.index(b'stsz')+12; body[p:p+4]=b'\x00'*4
    elif damage=='zero_dimensions':
        p=body.index(b'avc1',40)+4+24; body[p:p+4]=b'\x00'*4
    else:
        p=body.index(b'mdat'); n=int.from_bytes(body[p-4:p],'big'); body[p+4:p+n-4]=b'\x00'*(n-8)
    assert validate_media_response('video',bytes(body),'video/mp4')['status']=='rejected'


from test_release_validation import complete_archive


def test_legacy_body_only_article_can_rebuild_offline(complete_archive, monkeypatch):
    cfg,state=complete_archive
    assert not state.assets_for('one')
    convert_to_legacy(cfg,state)
    path=cfg.item_dir('one')/'metadata.json'; meta=read_json(path)
    for field in ('parsed_sha256','parsed_content_path','resource_fingerprint','localized_resource_fingerprint'):
        meta.pop(field,None)
    write_json_atomic(path,meta)
    monkeypatch.setattr(httpx,'Client',lambda *a,**kw:pytest.fail('offline only'))
    with ArchiveState(cfg.db_path) as migrated:
        assert article_truth(cfg,migrated,'one')['status']=='legacy_unverified'
        result=rebuild_legacy_evidence(cfg,migrated)
        assert result['verified_articles']==1 and not result['reasons']
        assert run_verify(cfg,migrated)['validation']['ok']


def test_public_asset_endpoint_does_not_require_an_unrelated_cookie(archive, monkeypatch):
    cfg,state=archive
    monkeypatch.setattr(server,'load_credentials',lambda:Credentials())
    monkeypatch.setattr(server,'ArchiveClient',lambda *a,**kw:transport(cfg,lambda r:httpx.Response(200,content=PNG),kw['control']))
    app=server.Application(cfg); app.start({'kind':'assets','keys':['one']}); app.thread.join(5)
    assert not app.thread.is_alive() and app.snapshot()['status']=='completed'
    assert article_truth(cfg,state,'one')['complete']


def test_retained_history_is_audited_even_without_article_metadata(archive):
    cfg,state=archive; download(cfg,state)
    old=state.assets_for('one')[0]; path=cfg.item_dir('one')/old['local_path']
    stream=io.BytesIO(); Image.new('RGB',(2,2),'green').save(stream,format='PNG')
    download(cfg,state,stream.getvalue(),force=True)
    path.unlink(); (cfg.item_dir('one')/'metadata.json').unlink()
    report=audit_stored_files(cfg,state)
    assert not report['ok']
    assert report['current_resource_integrity']['ok']
    assert not report['historical_retained_resource_integrity']['ok']
