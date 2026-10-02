import asyncio

import httpx
import respx

from bfp.http import DomainLimiter, Fetcher, looks_blocked, make_client

from conftest import fixture_html


def test_limiter_spacing(clock):
    lim = DomainLimiter(5, clock=clock, sleep=clock.sleep)

    async def go():
        starts = []
        for _ in range(4):
            await lim.wait()
            starts.append(clock.now)
            clock.now += 1.2  # request takes 1.2 s
        return starts

    starts = asyncio.run(go())
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(abs(g - 5.0) < 1e-9 for g in gaps), gaps


def test_limiter_no_wait_after_slow_request(clock):
    lim = DomainLimiter(5, clock=clock, sleep=clock.sleep)

    async def go():
        await lim.wait()
        clock.now += 7  # slower than the interval
        await lim.wait()

    asyncio.run(go())
    assert clock.sleeps == []


def _fetcher(settings, clock):
    client = make_client(settings)
    return client, Fetcher(client, DomainLimiter(5, clock=clock, sleep=clock.sleep), settings, sleep=clock.sleep)


@respx.mock
def test_redirect_hops_are_rate_limited_and_checked(settings, clock):
    respx.get("https://s.test/a").mock(return_value=httpx.Response(301, headers={"location": "/b"}))
    respx.get("https://s.test/b").mock(return_value=httpx.Response(200, text="<html>ok</html>"))
    checked = []

    async def allow(u):
        checked.append(u)
        return True

    async def go():
        client, f = _fetcher(settings, clock)
        async with client:
            return await f.get("https://s.test/a", allow=allow), f

    res, f = asyncio.run(go())
    assert res.ok and res.final_url == "https://s.test/b" and res.redirects == ["https://s.test/a"]
    assert checked == ["https://s.test/b"]
    assert f.requests == 2 and clock.sleeps == [5.0]


@respx.mock
def test_redirect_to_disallowed_is_not_followed(settings, clock):
    respx.get("https://s.test/a").mock(return_value=httpx.Response(302, headers={"location": "/private"}))
    priv = respx.get("https://s.test/private").mock(return_value=httpx.Response(200))

    async def deny(u):
        return False

    async def go():
        client, f = _fetcher(settings, clock)
        async with client:
            return await f.get("https://s.test/a", allow=deny)

    res = asyncio.run(go())
    assert res.error.startswith("redirect target disallowed") and priv.call_count == 0


@respx.mock
def test_retry_once_on_503_then_ok(settings, clock):
    route = respx.get("https://s.test/x").mock(side_effect=[httpx.Response(503), httpx.Response(200, text="ok")])

    async def go():
        client, f = _fetcher(settings, clock)
        async with client:
            return await f.get("https://s.test/x")

    res = asyncio.run(go())
    assert res.ok and route.call_count == 2


@respx.mock
def test_no_retry_on_403(settings, clock):
    route = respx.get("https://s.test/x").mock(return_value=httpx.Response(403, text="Access Denied"))

    async def go():
        client, f = _fetcher(settings, clock)
        async with client:
            return await f.get("https://s.test/x")

    res = asyncio.run(go())
    assert res.blocked and route.call_count == 1


def test_block_detection():
    assert looks_blocked(200, fixture_html("challenge.html"))
    assert looks_blocked(429, "")
    assert not looks_blocked(200, fixture_html("jsonld_graph.html"))
    assert not looks_blocked(404, "<title>Access Denied</title>")
    # normal big page that happens to load a Cloudflare script is not a block
    assert not looks_blocked(200, "<html>" + "x" * 70_000 + "/cdn-cgi/challenge-platform/scripts/jsd/main.js</html>")
