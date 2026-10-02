from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from bfp.config import Paths, Settings, Shop, load_config

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"


def fixture_html(name: str) -> str:
    return (FIXTURES / "synthetic" / name).read_text(encoding="utf-8")


@pytest.fixture
def settings() -> Settings:
    return Settings(contact_email="test@example.com")


@pytest.fixture
def shop() -> Shop:
    return Shop(name="testshop", domain="www.shop.test", enabled=True)


class FakeClock:
    """Monotonic clock + sleep that advance virtual time instantly."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s
        await asyncio.sleep(0)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def project(tmp_path: Path) -> Paths:
    """A throw-away project root with config files."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("contact_email: test@example.com\nstart_date: '2026-10-10'\n")
    (tmp_path / "config" / "shops.yaml").write_text(
        "shops:\n"
        "  - name: testshop\n    domain: www.shop.test\n    enabled: true\n"
        "    selectors:\n      prev_price: 'del.old'\n      discount_pct: '.badge'\n"
        "  - name: other\n    domain: other.test\n    enabled: false\n"
    )
    (tmp_path / "config" / "products.csv").write_text(
        "shop,url,category,ean\n"
        "testshop,https://www.shop.test/p/ok,electronics,8806094924541\n"
        "testshop,https://www.shop.test/p/gone,electronics,\n"
        "testshop,https://www.shop.test/private/secret,beauty,\n"
        "testshop,https://www.shop.test/p/broken,toys,\n"
    )
    return Paths(tmp_path)


@pytest.fixture
def cfg(project):
    return load_config(project)
