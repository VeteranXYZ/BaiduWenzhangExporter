import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from baidu_archive.client import FetchError, Response
from baidu_archive.config import Config
from baidu_archive.export import run_export
from baidu_archive.integrity import audit_stored_files
from baidu_archive.parsers import parse_list_html, parse_list_json
from baidu_archive.pipeline import run_fetch, run_index, run_parse
from baidu_archive.state import ArchiveState
from baidu_archive.storage import (
    IntegrityError, current_content_path, read_json, read_raw, save_raw, write_once,
)
from baidu_archive.utils import sha256_bytes


def save(path, body, **kwargs):
    return save_raw(path, body, url="https://example.com/?sign=FAKE", status_code=200, **kwargs)


def test_repeated_fetch_preserves_all_responses_and_first_metadata(tmp_path):
    path = tmp_path / "article.html"
    save(path, b"first")
    first_meta = Path(str(path) + ".meta.json").read_bytes()
    second = save(path, b"second")
    third = save(path, b"third")
    assert path.read_bytes() == b"first"
    assert Path(str(path) + ".meta.json").read_bytes() == first_meta
    assert Path(second["path"]).read_bytes() == b"second"
    assert Path(third["path"]).read_bytes() == b"third"
    assert read_raw(path).content == b"third"
    assert b"FAKE" not in Path(str(path) + ".meta.json").read_bytes()


def test_interrupted_pointer_publication_keeps_previous_snapshot(tmp_path, monkeypatch):
    from baidu_archive import storage
    path = tmp_path / "article.html"
    save(path, b"old")

    def interrupted(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(storage, "write_json_atomic", interrupted)
    with pytest.raises(OSError):
        save(path, b"new")
    assert read_raw(path).content == b"old"
    assert next((tmp_path / "article.html.versions").glob("*.html")).read_bytes() == b"new"


def test_concurrent_saves_do_not_overwrite_responses(tmp_path):
    path = tmp_path / "page.html"
    bodies = [str(n).encode() for n in range(8)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda body: save(path, body), bodies))
    assert {Path(meta["path"]).read_bytes() for meta in results} == set(bodies)
    assert read_raw(path).content in bodies


@pytest.mark.parametrize("broken", ["../outside.html", "/outside.html", "other.html", "page.html.versions/../other"])
def test_pointer_must_stay_within_its_own_versions(tmp_path, broken):
    path = tmp_path / "page.html"
    save(path, b"body")
    pointer = Path(str(path) + ".latest.json")
    data = json.loads(pointer.read_text())
    data["path"] = broken
    pointer.write_text(json.dumps(data))
    with pytest.raises(IntegrityError):
        read_raw(path)


def test_latest_corruption_does_not_silently_fall_back(tmp_path):
    path = tmp_path / "page.html"
    save(path, b"first")
    meta = save(path, b"second")
    Path(meta["path"]).write_bytes(b"corrupted")
    with pytest.raises(IntegrityError):
        read_raw(path)
    assert path.read_bytes() == b"first"


def test_missing_latest_pointer_does_not_hide_newer_snapshots(tmp_path):
    path = tmp_path / "page.html"
    save(path, b"first")
    save(path, b"second")
    Path(str(path) + ".latest.json").unlink()
    with pytest.raises(IntegrityError):
        read_raw(path)


def test_legacy_raw_and_charset_are_supported(tmp_path):
    path = tmp_path / "page.html"
    body = "旧时文字".encode("gb18030")
    path.write_bytes(body)
    assert read_raw(path).text == "旧时文字"
    save(path, body, headers={"content-type": "text/html; charset=gb18030"})
    assert read_raw(path).text == "旧时文字"
    assert read_raw(path).sha256 == sha256_bytes(body)


def test_write_once_is_idempotent_and_never_replaces(tmp_path):
    path = tmp_path / "original.html"
    write_once(path, b"first")
    write_once(path, b"first")
    with pytest.raises(IntegrityError):
        write_once(path, b"second")
    assert path.read_bytes() == b"first"


@pytest.fixture
def archive(tmp_path, list_response, detail_page, content_page):
    config = Config(root=tmp_path / "archive", concurrency=1, delay=0, jitter=0,
                    list_url_template="https://wenzhang.baidu.com/fav/list?start={start}&limit={limit}&_={ts}")
    config.ensure_dirs()
    state = ArchiveState(config.db_path)
    record = parse_list_html(parse_list_json(list_response)["html"])[0]
    key = record["key"]
    state.upsert_record(record)
    state.ensure_item(key)
    save(config.detail_dir / f"{key}.html", detail_page.encode())
    save(config.content_dir / f"{key}.html", content_page.encode())
    state.update_item(key, status="content_fetched", content_sha256=sha256_bytes(content_page.encode()))
    yield config, state, key
    state.close()


def test_reparse_preserves_first_extraction_and_exports_current_version(archive, content_page):
    config, state, key = archive
    run_parse(config, state)
    folder = config.item_dir(key)
    first = (folder / "content.original.html").read_bytes()
    metadata = read_json(folder / "metadata.json")
    (folder / "content.local.html").write_text("OUTDATED LOCAL BODY")
    metadata["localized_from_sha256"] = metadata["parsed_sha256"]
    metadata["localized_resource_fingerprint"] = metadata["resource_fingerprint"]
    (folder / "metadata.json").write_text(json.dumps(metadata))
    state.update_item(key, status="complete")
    latest = content_page.replace("第一段正文", "修订后正文")
    save(config.content_dir / f"{key}.html", latest.encode())
    stats = run_parse(config, state)  # 不必 force，raw 摘要变化即使 complete 也必须重新解析。
    assert stats["parsed"] == 1
    assert (folder / "content.original.html").read_bytes() == first
    assert "修订后正文" in current_content_path(folder).read_text()
    assert len(list((folder / "parsed").glob("*.html"))) == 2
    assert state.get_item(key)["status"] == "parsed"
    run_export(config, state, formats=["txt"])
    assert "修订后正文" in (folder / "content.txt").read_text()
    assert "OUTDATED" not in (folder / "content.txt").read_text()


def test_force_reparse_with_same_content_keeps_localization(archive):
    config, state, key = archive
    run_parse(config, state)
    folder = config.item_dir(key)
    metadata = read_json(folder / "metadata.json")
    (folder / "content.local.html").write_text("localized current body")
    metadata["localized_from_sha256"] = metadata["parsed_sha256"]
    metadata["localized_resource_fingerprint"] = metadata["resource_fingerprint"]
    (folder / "metadata.json").write_text(json.dumps(metadata))
    state.update_item(key, status="complete")
    run_parse(config, state, force=True)
    assert state.get_item(key)["status"] == "complete"
    assert current_content_path(folder).name == "content.local.html"
    assert len(list((folder / "parsed").glob("*.html"))) == 1


def test_removed_resources_remain_in_history_but_are_not_downloaded(archive, content_page):
    config, state, key = archive
    run_parse(config, state)
    before = len(state.assets_for(key))
    assert before > 0
    import re
    latest = re.sub(r"<img\b[^>]*>", "", content_page)
    save(config.content_dir / f"{key}.html", latest.encode())
    run_parse(config, state)
    assert not [a for a in state.assets_for(key) if a["kind"] == "image"]
    assert len(state.assets_for(key, include_inactive=True)) == before


def test_original_tampering_is_detected_before_reparse(archive):
    config, state, key = archive
    run_parse(config, state)
    path = config.item_dir(key) / "content.original.html"
    path.write_bytes(b"tampered")
    with pytest.raises(IntegrityError):
        run_parse(config, state, force=True)
    assert path.read_bytes() == b"tampered"
    assert audit_stored_files(config, state)["issue_count"] > 0


def test_verify_checks_historical_snapshots_and_stale_derivatives(archive, content_page):
    config, state, key = archive
    run_parse(config, state)
    assert audit_stored_files(config, state)["ok"]
    base = config.content_dir / f"{key}.html"
    save(base, content_page.replace("第一段正文", "下一版").encode())
    report = audit_stored_files(config, state)
    assert any(i["kind"] == "stale_content_derivative" for i in report["issues"])
    run_parse(config, state)
    assert audit_stored_files(config, state)["ok"]
    base.write_bytes(b"damaged historical snapshot")
    assert any(i["kind"] == "sha256_mismatch" for i in audit_stored_files(config, state)["issues"])


class FakeClient:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return Response(url, 200, value.encode(), {"content-type": "text/html"}, 0, 1)


def test_signature_refresh_keeps_old_detail_and_correct_hash(archive, detail_page, content_page):
    config, state, key = archive
    detail_path = config.detail_dir / f"{key}.html"
    original = detail_path.read_bytes()
    (config.content_dir / f"{key}.html").unlink()
    Path(str(config.content_dir / f"{key}.html") + ".latest.json").unlink()
    Path(str(config.content_dir / f"{key}.html") + ".meta.json").unlink()
    state.update_item(key, status="pending")
    fresh = detail_page.replace("FAKESIGNVALUE", "FRESH_FAKE_SIGNATURE")
    client = FakeClient(FetchError("expired", status=403), fresh, content_page)
    assert run_fetch(config, state, client)["ok"] == 1
    assert detail_path.read_bytes() == original
    assert read_raw(detail_path).content == fresh.encode()
    assert state.get_item(key)["detail_sha256"] == sha256_bytes(fresh.encode())
    assert "FRESH_FAKE_SIGNATURE" in client.calls[-1]


def test_forced_fetch_resets_current_version_state_without_losing_old_body(archive, detail_page, content_page):
    config, state, key = archive
    run_parse(config, state)
    state.update_item(key, status="complete")
    client = FakeClient(detail_page, content_page.replace("第一段正文", "强制抓取新正文"))
    assert run_fetch(config, state, client, force=True)["ok"] == 1
    assert state.get_item(key)["status"] == "content_fetched"
    assert state.get_item(key)["parsed_at"] is None
    assert (config.content_dir / f"{key}.html").read_bytes() == content_page.encode()


def test_index_reuses_latest_snapshot_without_network(archive, list_response):
    config, state, key = archive
    path = config.lists_dir / "list_start00000.json"
    save(path, list_response.encode())
    payload = json.loads(list_response)
    payload["data"]["totalCount"] = 3
    save(path, json.dumps(payload).encode())
    run_index(config, state, FakeClient(), max_pages=1)
    assert state.get_meta("total_count") == 3
    assert path.read_bytes() == list_response.encode()


def test_empty_selection_does_not_fetch_or_parse_everything(archive):
    config, state, key = archive
    assert run_fetch(config, state, FakeClient(), force=True, only_keys=[])["total"] == 0
    assert run_parse(config, state, force=True, only_keys=[])["parsed"] == 0
    assert not (config.item_dir(key) / "content.original.html").exists()


def test_resource_retry_does_not_refetch_pages(tmp_path, monkeypatch):
    from baidu_archive import cli
    monkeypatch.chdir(tmp_path)
    config = Config(root=tmp_path / "archive")
    config.ensure_dirs()
    with ArchiveState(config.db_path) as state:
        for key, status in (("complete-key", "complete"), ("resource-failed-key", "assets_partial")):
            state.upsert_record({"key": key, "type": "page"})
            state.update_item(key, status=status)
    monkeypatch.setattr(cli, "load_config", lambda **kwargs: config)

    class Client:
        def reset_auth_streak(self):
            pass

        def close(self):
            pass

    def forbid_fetch(*args, **kwargs):
        raise AssertionError("resource retry must not refetch HTML")

    touched = {}

    def parse(*args, **kwargs):
        touched["parse"] = kwargs["only_keys"]
        return {}

    def assets(*args, **kwargs):
        touched["assets"] = kwargs["only_keys"]
        return {"failed": 0}

    monkeypatch.setattr(cli, "_client", lambda *args, **kwargs: Client())
    monkeypatch.setattr(cli, "run_fetch", forbid_fetch)
    monkeypatch.setattr(cli, "run_parse", parse)
    monkeypatch.setattr(cli, "run_assets", assets)
    assert cli.main(["retry-failed"]) == 0
    assert touched == {"parse": ["resource-failed-key"], "assets": ["resource-failed-key"]}


def test_missing_page_does_not_create_fake_original_or_empty_export(archive):
    config, state, key = archive
    save(config.content_dir / f"{key}.html", b'<title>404</title><p>gone</p>')
    assert run_parse(config, state)["warning"] == 1
    folder = config.item_dir(key)
    assert not (folder / "content.original.html").exists()
    assert not current_content_path(folder).exists()
    assert run_export(config, state, formats=["md", "txt"])["md"] == 0
    assert audit_stored_files(config, state)["ok"]


def test_image_only_page_is_not_marked_empty(archive):
    config, state, key = archive
    content = '<h1 id="detailArticleTitle_test">照片</h1><div id="detailArticleContent_test"><img src="https://image.example/a.jpg"></div><div id="detailArticleFooter_test"></div>'
    save(config.content_dir / f"{key}.html", content.encode())
    assert run_parse(config, state)["warning"] == 0
    assert state.get_item(key)["status"] == "parsed"
