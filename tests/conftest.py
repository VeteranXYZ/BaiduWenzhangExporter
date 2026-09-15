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
