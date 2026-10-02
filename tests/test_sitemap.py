import asyncio
import gzip

import httpx
import respx

from bfp.config import Shop
from bfp.http import DomainLimiter
from bfp.sitemap import parse_sitemap, propose

INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.shop.test/sitemap-pages.xml</loc></sitemap>
  <sitemap><loc>https://www.shop.test/sitemap-products-1.xml.gz</loc></sitemap>
</sitemapindex>"""
PRODUCTS = b"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.shop.test/p/tv-samsung-55</loc><lastmod>2026-09-30</lastmod></url>
  <url><loc>https://www.shop.test/p/friggitrice-5l</loc></url>
  <url><loc>https://www.shop.test/p/hidden-item</loc></url>
  <url><loc>https://www.shop.test/c/televisori</loc></url>
</urlset>"""
XXE = b"""<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://a.test/&e;</loc></url></urlset>"""


def test_parse_index_and_gz():
    children, urls = parse_sitemap(INDEX)
    assert len(children) == 2 and urls == []
    children, urls = parse_sitemap(gzip.compress(PRODUCTS))
    assert urls[0] == ("https://www.shop.test/p/tv-samsung-55", "2026-09-30") and len(urls) == 4


def test_no_external_entities():
    _, urls = parse_sitemap(XXE)
    assert all("root:" not in u for u, _ in urls)


@respx.mock
def test_propose_filters_and_respects_robots(cfg, clock):
    respx.get("https://www.shop.test/robots.txt").mock(return_value=httpx.Response(
        200, text="User-agent: *\nDisallow: /p/hidden\nSitemap: https://www.shop.test/sitemap_index.xml\n"))
    respx.get("https://www.shop.test/sitemap_index.xml").mock(return_value=httpx.Response(200, content=INDEX,
                                                                                            headers={"content-type": "application/xml"}))
    pages = respx.get("https://www.shop.test/sitemap-pages.xml").mock(return_value=httpx.Response(200, content=b"<urlset/>"))
    respx.get("https://www.shop.test/sitemap-products-1.xml.gz").mock(
        return_value=httpx.Response(200, content=gzip.compress(PRODUCTS), headers={"content-type": "application/x-gzip"}))
    shop = Shop(name="testshop", domain="www.shop.test", enabled=True, product_url_pattern=r"/p/[^/]+$")
    lim = DomainLimiter(5, clock=clock, sleep=clock.sleep)
    cands, stats = asyncio.run(propose(cfg, shop, keywords=None, limit=10, limiter=lim))
    urls = [c.url for c in cands]
    assert urls == ["https://www.shop.test/p/tv-samsung-55", "https://www.shop.test/p/friggitrice-5l"]
    assert stats["disallowed"] == 1 and stats["pattern_rejected"] == 1
    assert stats["sitemaps_fetched"] == 3 and pages.call_count == 1
    # product sitemap was fetched before the pages sitemap
    assert all(abs(s - 5.0) < 1e-9 for s in clock.sleeps)

    cands, _ = asyncio.run(propose(cfg, shop, keywords=["friggitrice"], limit=10, limiter=DomainLimiter(5, clock=clock, sleep=clock.sleep)))
    assert [c.url for c in cands] == ["https://www.shop.test/p/friggitrice-5l"]
