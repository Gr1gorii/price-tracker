import asyncio
import gzip
import json
from datetime import date

import httpx
import pyarrow.parquet as pq
import respx

from bfp.collect import run_collection
from bfp.config import load_config
from bfp.http import DomainLimiter
from bfp.schedule import Slot
from bfp.storage import connect, load_blocked

from conftest import fixture_html

SLOT = Slot("2026-10-10T08", date(2026, 10, 10), True)
ROBOTS = "User-agent: *\nDisallow: /private/\n"


def _mock_shop(times, clock):
    def stamp(resp):
        def side_effect(request):
            times.append(clock.now)
            return resp
        return side_effect

    respx.get("https://www.shop.test/robots.txt").mock(side_effect=stamp(httpx.Response(200, text=ROBOTS)))
    respx.get("https://www.shop.test/p/ok").mock(side_effect=stamp(httpx.Response(200, html=fixture_html("jsonld_graph.html"))))
    respx.get("https://www.shop.test/p/gone").mock(side_effect=stamp(httpx.Response(404)))
    respx.get("https://www.shop.test/p/broken").mock(side_effect=stamp(httpx.Response(200, html=fixture_html("css_only.html"))))


@respx.mock
def test_end_to_end_run(project, cfg, clock):
    times: list[float] = []
    _mock_shop(times, clock)
    factory = lambda iv: DomainLimiter(iv, clock=clock, sleep=clock.sleep)  # noqa: E731
    manifest = asyncio.run(run_collection(cfg, project, SLOT, "actions", limiter_factory=factory))

    info = manifest["shops"]["testshop"]
    assert info["counts"] == {"ok": 1, "not_found": 1, "robots_disallowed": 1, "parse_failed": 1}
    assert info["requests"] == 4  # robots + 3 pages; the disallowed URL is never requested
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps and min(gaps) >= 5.0

    files = list((project.observations / "date=2026-10-10").glob("*.parquet"))
    assert [f.name for f in files] == ["08__actions__testshop.parquet"]
    t = pq.read_table(files[0]).to_pylist()
    ok = next(r for r in t if r["status"] == "ok")
    assert str(ok["price"]) == "649.90" and str(ok["displayed_prev_price"]) == "799.00"
    assert str(ok["displayed_discount_pct"]) == "19.00" and ok["parse_method"] == "jsonld"
    assert ok["ean"] == "8806094924541" and ok["page_gtin"] == "8806094924541"

    failed = next(r for r in t if r["status"] == "parse_failed")
    snap = project.root / failed["html_snapshot"]
    assert snap.exists() and "Bambola" in gzip.open(snap, "rt").read()

    con = connect(project)
    assert con.execute("select count(*) from observations").fetchone()[0] == 4
    assert (project.health / "2026-10-10T08__actions.json").exists()


@respx.mock
def test_second_run_same_slot_skips_done_shop(project, cfg, clock):
    _mock_shop([], clock)
    factory = lambda iv: DomainLimiter(iv, clock=clock, sleep=clock.sleep)  # noqa: E731
    asyncio.run(run_collection(cfg, project, SLOT, "actions", limiter_factory=factory))
    m2 = asyncio.run(run_collection(cfg, project, SLOT, "actions", limiter_factory=factory))
    assert m2["shops"]["testshop"]["skipped_reason"] == "already collected in this slot"


@respx.mock
def test_block_stops_shop_and_persists(project, cfg, clock):
    respx.get("https://www.shop.test/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n"))
    pages = respx.get(url__regex=r"https://www\.shop\.test/(p|private)/.*").mock(
        return_value=httpx.Response(200, html=fixture_html("challenge.html"))
    )
    cfg.settings.block_threshold = 2
    factory = lambda iv: DomainLimiter(iv, clock=clock, sleep=clock.sleep)  # noqa: E731
    m = asyncio.run(run_collection(cfg, project, SLOT, "actions", limiter_factory=factory))
    info = m["shops"]["testshop"]
    assert info["counts"] == {"blocked": 2, "skipped_blocked": 2}
    assert pages.call_count == 2  # nothing requested after the threshold
    assert "testshop" in load_blocked(project)

    later = Slot("2026-10-10T20", date(2026, 10, 10), True)
    m2 = asyncio.run(run_collection(cfg, project, later, "actions", limiter_factory=factory))
    assert m2["shops"]["testshop"]["skipped_reason"].startswith("blocked since")
    assert pages.call_count == 2


def test_redirect_to_category_is_not_found():
    from bfp.collect import _redirected_away
    pat = r"/prodotto/[^/]+/?$"
    assert _redirected_away("https://s.it/prodotto/lego-1/", "https://s.it/categoria-prodotto/giocattoli/mattoncini/", pat)
    assert not _redirected_away("https://s.it/prodotto/lego-1/", "https://s.it/prodotto/lego-1-new/", pat)
    assert _redirected_away("https://s.it/a/b/c/d", "https://s.it/")


def test_runner_and_enabled_selection(project):
    cfg = load_config(project)
    from bfp.collect import select_shops

    assert [s.name for s in select_shops(cfg, "actions", None)] == ["testshop"]
    assert select_shops(cfg, "local", None) == []
    try:
        select_shops(cfg, "actions", ["other"])
        raise AssertionError("disabled shop must be refused")
    except ValueError as e:
        assert "not enabled" in str(e)


def test_config_rejects_foreign_domain(project):
    (project.config / "products.csv").write_text("shop,url,category,ean\ntestshop,https://evil.test/p,toys,\n")
    try:
        load_config(project)
        raise AssertionError("expected failure")
    except ValueError as e:
        assert "does not match shop domain" in str(e)


def test_manifest_is_json(project, cfg, clock):
    with respx.mock:
        _mock_shop([], clock)
        factory = lambda iv: DomainLimiter(iv, clock=clock, sleep=clock.sleep)  # noqa: E731
        asyncio.run(run_collection(cfg, project, SLOT, "actions", limit=1, limiter_factory=factory))
    m = json.loads((project.health / "2026-10-10T08__actions.json").read_text())
    assert m["shops"]["testshop"]["total"] == 1


API_SHOPS = """shops:
  - name: testshop
    domain: www.shop.test
    enabled: true
    selectors:
      discount_pct: '.badge'
    low30_api:
      url: "https://www.shop.test/wp-json/omnibus/lowest/{id}"
      id_regex: '<body[^>]*\\bpostid-(\\d+)'
      price_path: lowestPrice.price
      days_path: lowestPrice.days
"""


def _page(pct: bool, pid: int) -> str:
    html = (fixture_html("jsonld_graph.html")
            .replace("<body>", f'<body class="product postid-{pid}">')
            .replace("Prezzo più basso degli ultimi 30 giorni: 699,00 €", ""))  # value only via the API
    if pct:
        return html
    return (html.replace('<span class="badge">-19%</span>', "")
                .replace("https://schema.org/StrikethroughPrice", "https://schema.org/ListPrice"))  # RRP ≠ discount


@respx.mock
def test_low30_api_called_only_for_discounted_pages(project, clock):
    (project.config / "shops.yaml").write_text(API_SHOPS)
    (project.config / "products.csv").write_text(
        "shop,url,category,ean\ntestshop,https://www.shop.test/p/a,toys,\ntestshop,https://www.shop.test/p/b,toys,\n"
        "testshop,https://www.shop.test/p/c,toys,\n")
    cfg = load_config(project)
    respx.get("https://www.shop.test/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /wp-json/omnibus/lowest/3\n"))
    respx.get("https://www.shop.test/p/a").mock(return_value=httpx.Response(200, html=_page(True, 1)))
    respx.get("https://www.shop.test/p/b").mock(return_value=httpx.Response(200, html=_page(False, 2)))
    respx.get("https://www.shop.test/p/c").mock(return_value=httpx.Response(200, html=_page(True, 3)))
    api1 = respx.get("https://www.shop.test/wp-json/omnibus/lowest/1").mock(
        return_value=httpx.Response(200, json={"lowestPrice": {"days": 30, "price": "382,49", "discount": "+6%"}}))
    api2 = respx.get("https://www.shop.test/wp-json/omnibus/lowest/2").mock(return_value=httpx.Response(200, json=[]))
    api3 = respx.get("https://www.shop.test/wp-json/omnibus/lowest/3").mock(return_value=httpx.Response(200, json=[]))
    factory = lambda iv: DomainLimiter(iv, clock=clock, sleep=clock.sleep)  # noqa: E731
    asyncio.run(run_collection(cfg, project, SLOT, "actions", limiter_factory=factory))

    rows = {r["url"][-1]: r for r in pq.read_table(next(project.observations.glob("*/*.parquet"))).to_pylist()}
    assert api1.call_count == 1 and api2.call_count == 0 and api3.call_count == 0
    assert rows["a"]["status"] == "ok" and str(rows["a"]["displayed_low30_price"]) == "382.49"
    assert rows["a"]["display_method"].endswith("api")
    assert rows["b"]["displayed_low30_price"] is None
    assert rows["c"]["error"] == "low30_api: disallowed by robots.txt" and rows["c"]["status"] == "ok"


@respx.mock
def test_low30_api_fills_value(project, clock, settings):
    from bfp.config import Low30Api, Shop
    from bfp.extras import low30_from_api
    from bfp.http import Fetcher, make_client
    from bfp.robots import RobotsCache

    shop = Shop(name="t", domain="www.shop.test", low30_api=Low30Api(
        url="https://www.shop.test/api/{id}", id_regex=r"postid-(\d+)", price_path="lowestPrice.price", days_path="lowestPrice.days"))
    respx.get("https://www.shop.test/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://www.shop.test/api/7").mock(return_value=httpx.Response(200, json={"lowestPrice": {"days": 30, "price": "1.382,49"}}))
    respx.get("https://www.shop.test/api/8").mock(return_value=httpx.Response(200, json={"lowestPrice": {"days": 60, "price": "9,99"}}))

    async def go():
        async with make_client(settings) as c:
            f = Fetcher(c, DomainLimiter(5, clock=clock, sleep=clock.sleep), settings, sleep=clock.sleep)
            rc = RobotsCache(f, settings.ua_token)
            return (await low30_from_api(f, rc, shop, '<body class="postid-7">'),
                    await low30_from_api(f, rc, shop, '<body class="postid-8">'),
                    await low30_from_api(f, rc, shop, '<body>'))

    a, b, c = asyncio.run(go())
    assert str(a.value) == "1382.49" and a.error is None
    assert b.value is None and "60 days" in b.error
    assert c.value is None and "id not found" in c.error and not c.requested


@respx.mock
def test_blocked_shop_is_retried_once_per_slot_and_unblocked(project, cfg, clock):
    import json
    from datetime import datetime, timedelta, timezone
    robots = respx.get("https://www.shop.test/robots.txt").mock(return_value=httpx.Response(403))
    factory = lambda iv: DomainLimiter(iv, clock=clock, sleep=clock.sleep)  # noqa: E731
    asyncio.run(run_collection(cfg, project, SLOT, "actions", limiter_factory=factory))
    assert "testshop" in load_blocked(project) and robots.call_count == 1

    # same slot later: within retry interval -> skipped, no request
    later = Slot("2026-10-10T20", date(2026, 10, 10), True)
    m = asyncio.run(run_collection(cfg, project, later, "actions", limiter_factory=factory))
    assert m["shops"]["testshop"]["skipped_reason"].startswith("blocked since") and robots.call_count == 1

    # 12 h later the shop answers again -> retried, collected, unblocked
    data = load_blocked(project)
    data["testshop"]["last_check"] = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
    (project.state / "blocked.json").write_text(json.dumps(data))
    robots.mock(return_value=httpx.Response(200, text=ROBOTS))
    _mock_shop([], clock)
    nxt = Slot("2026-10-11T08", date(2026, 10, 11), True)
    m = asyncio.run(run_collection(cfg, project, nxt, "actions", limiter_factory=factory))
    assert m["shops"]["testshop"]["counts"].get("ok") == 1 and "testshop" not in load_blocked(project)
