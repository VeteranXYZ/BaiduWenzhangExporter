from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def list_response() -> str:
    return _read("list_response.json")


@pytest.fixture
def detail_page() -> str:
    return _read("detail_page.html")


@pytest.fixture
def content_page() -> str:
    return _read("content_page.html")


@pytest.fixture
def login_page() -> str:
    return _read("login_page.html")


def seed_generation(state, keys, expected=None):
    """Synthetic observed generation for unrelated test setup; RC tests use run_index."""
    expected = len(keys) if expected is None else expected
    complete = len(set(keys)) == len(keys) == expected
    state.set_meta('index_generation', {'run_id': 'fixture', 'seen_keys': keys,
        'declared_total': expected, 'pagination_complete': complete, 'complete': complete,
        'status': 'completed' if complete else 'partial', 'errors': [] if complete else ['fixture_partial']})
