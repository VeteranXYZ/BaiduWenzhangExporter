from __future__ import annotations

import json

import pytest

from conftest import seed_generation
from baidu_archive import cli, server
from baidu_archive.assets import run_assets
from baidu_archive.auth import Credentials
from baidu_archive.client import ArchiveClient
from baidu_archive.config import Config
from baidu_archive.pipeline import run_parse
from baidu_archive.state import ArchiveState
from baidu_archive.storage import save_raw
from baidu_archive.verify import run_verify, validation_result


@pytest.fixture
def complete_archive(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server, 'load_credentials', lambda: Credentials())
    cfg = Config(root=tmp_path/'archive')
    cfg.ensure_dirs()
    with ArchiveState(cfg.db_path) as state:
        state.upsert_record({'key':'one','type':'page','global_position':0})
        state.ensure_item('one')
        state.set_meta('total_count',1)
        seed_generation(state, ['one'])
        detail=b'<iframe class="pcs-article-iframe" src="https://wenzhang.baidu.com/content"></iframe>'
        content=b'<h1 id="detailArticleTitle_x">Test</h1><div id="detailArticleContent_x"><p>Content</p></div><div id="detailArticleFooter_x"></div>'
        for folder,body in [(cfg.detail_dir,detail),(cfg.content_dir,content)]:
            save_raw(folder/'one.html',body,url='https://wenzhang.baidu.com/test',status_code=200)
        run_parse(cfg,state)
        with ArchiveClient(cfg,Credentials()) as client:
            run_assets(cfg,state,client)
        yield cfg,state


def test_complete_archive_passes_cli_and_server(complete_archive):
    cfg,state=complete_archive
    summary=run_verify(cfg,state)
    assert validation_result(summary)['ok'] and summary['validation']['ok']
    assert cli.main(['--root',str(cfg.root),'verify']) == 0
    app=server.Application(cfg)
    app.start({'kind':'verify'})
    app.thread.join(5)
    assert not app.thread.is_alive()
    assert app.snapshot()['status']=='completed'
    assert app.snapshot()['result']['ok']


@pytest.mark.parametrize('problem,failed_check', [
    ('missing_index','index_complete'), ('unknown_expected','counts_match'),
    ('excess_index','counts_match'), ('order_gap','order_contiguous'),
    ('order_unknown','order_contiguous'), ('item_pending','no_problem_items'),
    ('file_corrupt','files_intact'), ('raw_missing','raw_content_present'),
    ('original_missing','original_content_present'), ('empty_original','no_empty_content'),
    ('asset_failed','no_failed_assets'),
])
def test_cli_and_server_share_all_integrity_requirements(complete_archive, problem, failed_check):
    cfg,state=complete_archive
    if problem=='missing_index':
        seed_generation(state, ['one'], 2)
    elif problem=='unknown_expected':
        generation=state.get_meta('index_generation'); generation['declared_total']=None
        state.set_meta('index_generation',generation)
    elif problem=='excess_index':
        seed_generation(state, ['one'], 0)
    elif problem=='order_gap':
        state.upsert_record({'key':'one','type':'page','global_position':1})
    elif problem=='order_unknown':
        state.upsert_record({'key':'one','type':'page','global_position':None})
    elif problem=='item_pending':
        state.update_item('one',status='pending')
    elif problem=='file_corrupt':
        (cfg.content_dir/'one.html').write_bytes(b'corrupt')
    elif problem=='raw_missing':
        (cfg.content_dir/'one.html').unlink()
    elif problem=='original_missing':
        (cfg.item_dir('one')/'content.original.html').unlink()
    elif problem=='empty_original':
        (cfg.item_dir('one')/'content.original.html').write_bytes(b'')
    elif problem=='asset_failed':
        state.upsert_asset('one',{'kind':'image','original_url':'https://x/image','attribute':'src'})
        state.record_asset_result('one','https://x/image','src',download_attempted=1,download_status='failed')
    summary=run_verify(cfg,state)
    expected=validation_result(summary)
    assert not expected['ok'] and not expected['checks'][failed_check]
    if problem=='missing_index':
        assert expected['expected']==2 and expected['indexed']==1 and expected['missing']==1
        assert summary['problem_items']==1 and summary['stored_file_integrity']['ok']
    assert cli.main(['--root',str(cfg.root),'verify']) == 1
    app=server.Application(cfg)
    app.start({'kind':'verify'})
    app.thread.join(5)
    assert not app.thread.is_alive()
    assert app.snapshot()['status']=='partial'
    assert app.snapshot()['result']==expected
    assert '校验未通过' in app.snapshot()['message']
