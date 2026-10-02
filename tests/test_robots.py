import asyncio

import httpx
import respx

from bfp.http import DomainLimiter, Fetcher, make_client
from bfp.robots import Robots, RobotsCache, strict_allowed, strict_rules

TOKEN = "BFPriceTracker"
O = "https://www.shop.test"

ROBOTS = """
User-agent: *
Disallow: /checkout
Disallow: /*?sort=
Disallow: /*.json$
Disallow: /account/
Allow: /account/public
Crawl-delay: 8
Sitemap: https://www.shop.test/sitemap_index.xml

User-agent: BadBot
Disallow: /
"""


def test_wildcards_that_robotparser_misses():
    r = Robots.from_text(O, ROBOTS, TOKEN)
    # robotparser alone says yes for these (prefix-only matching) — strict check must say no
    assert r._rp.can_fetch(TOKEN, O + "/tv?sort=price")
    assert not r.allowed(O + "/tv?sort=price")
    assert not r.allowed(O + "/api/products.json")
    assert r.allowed(O + "/api/products.json?x=1")  # '$' anchors the end
    assert r.allowed(O + "/p/tv-55")
    assert not r.allowed(O + "/checkout/step1")


def test_longest_match_and_fail_closed_combination():
    r = Robots.from_text(O, ROBOTS, TOKEN)
    rules = strict_rules(ROBOTS, TOKEN)
    assert strict_allowed(rules, O + "/account/public")  # longer Allow wins in strict
    # robotparser takes the first matching rule (Disallow /account/) -> combined result is disallowed
    assert not r.allowed(O + "/account/public")
    assert "robotparser=False" in r.explain(O + "/account/public")


def test_crawl_delay_and_sitemaps():
    r = Robots.from_text(O, ROBOTS, TOKEN)
    assert r.crawl_delay == 8
    assert r.sitemaps == ["https://www.shop.test/sitemap_index.xml"]


def test_specific_group_beats_star():
    txt = "User-agent: *\nDisallow: /\n\nUser-agent: BFPriceTracker\nDisallow: /cart\n"
    r = Robots.from_text(O, txt, TOKEN)
    assert r.allowed(O + "/p/1") and not r.allowed(O + "/cart")


def test_disallow_all():
    r = Robots.from_text(O, "User-agent: *\nDisallow: /\n", TOKEN)
    assert not r.allowed(O + "/p/1")


def _cache(settings, clock):
    client = make_client(settings)
    limiter = DomainLimiter(5, clock=clock, sleep=clock.sleep)
    return client, RobotsCache(Fetcher(client, limiter, settings, sleep=clock.sleep), TOKEN)


@respx.mock
def test_status_codes(settings, clock):
    async def go():
        respx.get("https://a.test/robots.txt").mock(return_value=httpx.Response(404))
        respx.get("https://b.test/robots.txt").mock(return_value=httpx.Response(401))
        respx.get("https://c.test/robots.txt").mock(return_value=httpx.Response(503))
        respx.get("https://d.test/robots.txt").mock(return_value=httpx.Response(200, text=ROBOTS))
        client, cache = _cache(settings, clock)
        async with client:
            a, b, c, d = [await cache.get(f"https://{h}.test/x") for h in "abcd"]
        return a, b, c, d

    a, b, c, d = asyncio.run(go())
    assert a.status == "allow_all" and a.allowed("https://a.test/anything")
    assert b.status == "disallow_all" and not b.allowed("https://b.test/x")
    assert c.status == "unavailable" and not c.allowed("https://c.test/x")  # 5xx: fail closed
    assert d.status == "parsed"


@respx.mock
def test_robots_403_counts_as_block(settings, clock):
    async def go():
        respx.get("https://f.test/robots.txt").mock(return_value=httpx.Response(403))
        client, cache = _cache(settings, clock)
        async with client:
            return await cache.get("https://f.test/x")

    r = asyncio.run(go())
    assert r.status == "blocked" and not r.allowed("https://f.test/x")


@respx.mock
def test_robots_fetched_with_our_user_agent(settings, clock):
    async def go():
        route = respx.get("https://e.test/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n"))
        client, cache = _cache(settings, clock)
        async with client:
            await cache.get("https://e.test/p")
            await cache.get("https://e.test/q")  # cached: one request only
        return route

    route = asyncio.run(go())
    assert route.call_count == 1
    ua = route.calls[0].request.headers["user-agent"]
    assert ua.startswith("BFPriceTracker/") and "mailto:test@example.com" in ua
