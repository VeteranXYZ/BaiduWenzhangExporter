import json


from baidu_archive.storage import (
    read_json,
    read_text,
    save_raw,
    write_atomic,
    write_json_atomic,
)


def test_write_atomic_creates_parents(tmp_path):
    target = tmp_path / "a" / "b" / "c.txt"
    write_atomic(target, b"hello")
    assert target.read_bytes() == b"hello"


def test_write_atomic_leaves_no_temp_files(tmp_path):
    target = tmp_path / "x.txt"
    write_atomic(target, b"data")
    leftovers = [p for p in tmp_path.iterdir() if p.name != "x.txt"]
    assert leftovers == []


def test_write_atomic_returns_sha256(tmp_path):
    import hashlib

    digest = write_atomic(tmp_path / "x", b"abc")
    assert digest == hashlib.sha256(b"abc").hexdigest()


def test_write_atomic_replaces_existing(tmp_path):
    target = tmp_path / "x.txt"
    write_atomic(target, b"old")
    write_atomic(target, b"new")
    assert target.read_bytes() == b"new"


def test_json_is_utf8_without_ascii_escapes(tmp_path):
    target = tmp_path / "x.json"
    write_json_atomic(target, {"标题": "助人为乐！"})
    raw = target.read_text(encoding="utf-8")
    assert "助人为乐" in raw          # 中文必须可直接阅读
    assert "\\u" not in raw
    assert json.loads(raw)["标题"] == "助人为乐！"


def test_read_json_missing_returns_default(tmp_path):
    assert read_json(tmp_path / "nope.json", {"d": 1}) == {"d": 1}


def test_read_json_corrupt_returns_default(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert read_json(bad, "fallback") == "fallback"


def test_read_text_tolerates_invalid_bytes(tmp_path):
    """十几年前的网页可能含非法字节，读取不许抛错。"""
    target = tmp_path / "x.html"
    target.write_bytes(b"<p>\xff\xfe bad bytes</p>")
    assert "bad bytes" in read_text(target)


def test_save_raw_writes_body_and_redacted_meta(tmp_path):
    target = tmp_path / "raw.html"
    meta = save_raw(
        target,
        b"<html>body</html>",
        url="https://x.com/c?key=K&sign=SECRET&expire=99",
        status_code=200,
        headers={"content-type": "text/html", "etag": "W/abc", "last-modified": "Mon"},
        extra={"stage": "content", "key": "K"},
    )
    assert target.read_bytes() == b"<html>body</html>"
    saved = json.loads((tmp_path / "raw.html.meta.json").read_text(encoding="utf-8"))
    assert "SECRET" not in json.dumps(saved)      # 签名绝不落进 meta
    assert saved["http_status"] == 200
    assert saved["sha256"] == meta["sha256"]
    assert saved["headers"]["etag"] == "W/abc"    # 缓存头保留，便于将来校验
    assert saved["headers"]["last-modified"] == "Mon"
    assert saved["stage"] == "content"
