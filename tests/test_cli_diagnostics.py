import json

import pytest

from baidu_archive import cli
from baidu_archive.auth import Credentials
from baidu_archive.client import Response
from baidu_archive.config import Config


@pytest.fixture
def diagnostic_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_credentials", lambda: Credentials(cookie_header="BDUSS=FAKE"))
    config = Config(root=tmp_path / "archive", delay=0, jitter=0)
    monkeypatch.setattr(cli, "load_config", lambda **kwargs: config)
    return config


def install_responses(monkeypatch, *bodies):
    calls = []
    pending = iter(bodies)

    class Client:
        def get(self, url, **kwargs):
            calls.append(url)
            body = next(pending)
            return Response(url, 200, body.encode(), {"content-type": "text/html"}, 0, 1)

        def close(self):
            pass

    monkeypatch.setattr(cli, "_client", lambda *args, **kwargs: Client())
    return calls


def test_doctor_probe_formats_timestamp_and_saves_one_response(diagnostic_env, monkeypatch, list_response):
    calls = install_responses(monkeypatch, list_response)
    assert cli.main(["doctor", "--probe"]) == 0
    assert len(calls) == 1
    assert "_=" in calls[0] and "{" not in calls[0]
    files = list(diagnostic_env.raw_dir.glob("diagnostics/doctor-*/list-0.json"))
    assert len(files) == 1 and files[0].read_text() == list_response


def test_page_limit_override_reaches_list_request(diagnostic_env, monkeypatch, list_response):
    calls = install_responses(monkeypatch, list_response)
    assert cli.main(["--page-limit", "100", "doctor", "--probe"]) == 0
    assert "limit=100" in calls[0]


def test_doctor_invalid_json_is_failure_without_response_in_log(diagnostic_env, monkeypatch, capsys):
    diagnostic_env.list_url_template = "https://wenzhang.baidu.com/fav/list?start={start}"
    install_responses(monkeypatch, '{"status":-1,"msg":"PRIVATE_VALUE"}')
    assert cli.main(["doctor"]) == 1
    assert "PRIVATE_VALUE" not in capsys.readouterr().out


def test_dry_run_preserves_each_run_and_saves_before_parsing(
    diagnostic_env, monkeypatch, list_response, detail_page, content_page,
):
    diagnostic_env.list_url_template = "https://wenzhang.baidu.com/fav/list?start={start}"
    calls = install_responses(monkeypatch, list_response, detail_page, content_page,
                              list_response, "<html>No iframe</html>")
    assert cli.main(["dry-run"]) == 0
    assert cli.main(["dry-run"]) == 1
    folders = list(diagnostic_env.raw_dir.glob("diagnostics/dry-run-*"))
    assert len(folders) == 2 and len(calls) == 5
    assert sorted(len(list(folder.glob("*.meta.json"))) for folder in folders) == [2, 3]
    content_path = next(diagnostic_env.raw_dir.glob("diagnostics/dry-run-*/content.html"))
    assert content_path.read_text() == content_page
    meta = json.loads(content_path.with_name("content.html.meta.json").read_text())
    assert "FAKESIGNVALUE" not in meta["url_redacted"]


def test_dry_run_accepts_image_only_article(diagnostic_env, monkeypatch, list_response, detail_page):
    diagnostic_env.list_url_template = "https://wenzhang.baidu.com/fav/list?start={start}"
    content = '<div id="detailArticleContent_test"><img src="https://image.example/a.jpg"></div>'
    install_responses(monkeypatch, list_response, detail_page, content)
    assert cli.main(["dry-run"]) == 0
