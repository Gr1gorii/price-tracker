"""Saved (sanitised) real shop pages → expected values. Created with `bfp probe <shop> --save-fixtures`,
expected.json reviewed by hand. Uses the shop's selectors from config/shops.yaml."""

import gzip
import json
from decimal import Decimal
from pathlib import Path

import pytest

from bfp.config import Paths, load_shops
from bfp.parse import parse_page

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PROJECT = Path(__file__).resolve().parent.parent


def _cases():
    shops = load_shops(Paths(PROJECT))
    for exp in sorted(FIXTURES.glob("*/expected.json")):
        shop_name = exp.parent.name
        if shop_name == "synthetic":
            continue
        for slug, e in json.loads(exp.read_text(encoding="utf-8")).items():
            yield pytest.param(shops.get(shop_name), exp.parent / f"{slug}.html.gz", e, id=f"{shop_name}/{slug}")


CASES = list(_cases())


@pytest.mark.skipif(not CASES, reason="no real fixtures yet")
@pytest.mark.parametrize("shop,path,expected", CASES)
def test_real_page(shop, path, expected):
    assert shop is not None, "fixture shop missing from config/shops.yaml"
    html = gzip.open(path, "rt", encoding="utf-8").read()
    r = parse_page(html, expected["url"], shop)
    assert r.status == expected["status"]
    assert r.parse_method == expected["parse_method"]
    for field in ("price", "displayed_prev_price", "displayed_rrp_price", "displayed_low30_price", "displayed_discount_pct"):
        exp = expected[field]
        got = getattr(r, field)
        assert (None if got is None else got) == (None if exp is None else Decimal(exp)), field
    assert r.availability == expected["availability"]
