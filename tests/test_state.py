import pytest

from baidu_archive.state import ArchiveState


@pytest.fixture
def state(tmp_path):
    with ArchiveState(tmp_path / "state.db") as st:
        yield st


def _record(key: str, position: int, record_type: str = "page") -> dict:
    return {
        "key": key,
        "type": record_type,
        "title": f"标题{position}",
        "list_date": "2011-02-08",
        "tags": ["旧文"],
        "global_position": position,
        "list_offset": (position // 30) * 30,
        "position_in_page": position % 30,
        "raw_html": f"<li>{key}</li>",
    }


def test_upsert_and_read(state):
    state.upsert_record(_record("K1", 0))
    row = state.get_record("K1")
    assert row["title"] == "标题0"
    assert row["global_position"] == 0


def test_upsert_is_idempotent(state):
    state.upsert_record(_record("K1", 0))
    state.upsert_record(_record("K1", 0))
    assert state.count_records() == 1


def test_status_advances_monotonically(state):
    state.upsert_record(_record("K1", 0))
    state.ensure_item("K1")
    state.advance_status("K1", "detail_fetched")
    state.advance_status("K1", "content_fetched")
    state.advance_status("K1", "detail_fetched")  # 回退必须被忽略
    assert state.get_item("K1")["status"] == "content_fetched"


def test_side_status_overrides(state):
    """failed/warning 等旁路状态可以覆盖主线状态。"""
    state.upsert_record(_record("K1", 0))
    state.advance_status("K1", "content_fetched")
    state.advance_status("K1", "failed")
    assert state.get_item("K1")["status"] == "failed"


def test_mark_attempt_tracks_timestamps(state):
    state.upsert_record(_record("K1", 0))
    state.mark_attempt("K1")
    state.mark_attempt("K1")
    item = state.get_item("K1")
    assert item["attempts"] == 2
    assert item["first_attempt_at"] is not None
    assert item["last_attempt_at"] is not None


def test_records_keep_original_order(state):
    for position, key in enumerate(["Kc", "Ka", "Kb"]):
        state.upsert_record(_record(key, position))
    keys = [r["key"] for r in state.iter_records()]
    assert keys == ["Kc", "Ka", "Kb"]  # 原始顺序，而非字母序


def test_type_counts(state):
    state.upsert_record(_record("K1", 0, "page"))
    state.upsert_record(_record("K2", 1, "article"))
    state.upsert_record(_record("K3", 2, "other"))
    assert state.type_counts() == {"page": 1, "article": 1, "other": 1}


def test_assets_roundtrip(state):
    state.upsert_record(_record("K1", 0))
    state.upsert_asset("K1", {
        "kind": "image", "element_type": "img", "attribute": "src",
        "original_url": "http://x/a.jpg", "resolved_url": "http://x/a.jpg",
        "scheme_kind": "http", "domain": "x", "downloadable": True,
    })
    state.record_asset_result("K1", "http://x/a.jpg", "src",
                              download_attempted=1, download_status="ok",
                              local_path="assets/0001_ab.jpg", sha256="ab", size=10)
    assets = state.assets_for("K1")
    assert len(assets) == 1
    assert assets[0]["download_status"] == "ok"


def test_asset_upsert_is_unique_per_attribute(state):
    """同一 URL 出现在 src 与 data-src 上应算两条，重复 upsert 不增行。"""
    state.upsert_record(_record("K1", 0))
    base = {"kind": "image", "element_type": "img", "original_url": "http://x/a.jpg",
            "resolved_url": "http://x/a.jpg", "scheme_kind": "http", "domain": "x", "downloadable": True}
    state.upsert_asset("K1", {**base, "attribute": "src"})
    state.upsert_asset("K1", {**base, "attribute": "data-src"})
    state.upsert_asset("K1", {**base, "attribute": "src"})
    assert len(state.assets_for("K1")) == 2


def test_failures_recorded_and_cleared(state):
    state.upsert_record(_record("K1", 0))
    state.record_failure("K1", "content", error="HTTP 500", http_status=500, attempt=3)
    assert len(state.all_failures()) == 1
    state.clear_failures("K1")
    assert len(state.all_failures()) == 0


def test_duplicate_detection_reports_without_deleting(state):
    state.upsert_record({**_record("K1", 0), "title": "同名"})
    state.upsert_record({**_record("K2", 1), "title": "同名"})
    dupes = state.duplicate_titles()
    assert len(dupes) == 1 and dupes[0]["n"] == 2
    assert state.count_records() == 2  # 两条都还在，未被删除


def test_meta_roundtrip(state):
    state.set_meta("total_count", 504)
    state.set_meta("nested", {"a": [1, 2]})
    assert state.get_meta("total_count") == 504
    assert state.get_meta("nested") == {"a": [1, 2]}
    assert state.get_meta("missing", "fallback") == "fallback"


def test_state_survives_reopen(tmp_path):
    """断点续传的地基：关掉再开，状态必须还在。"""
    db = tmp_path / "state.db"
    with ArchiveState(db) as st:
        st.upsert_record(_record("K1", 0))
        st.advance_status("K1", "content_fetched")
    with ArchiveState(db) as st:
        assert st.get_item("K1")["status"] == "content_fetched"
        assert st.count_records() == 1
