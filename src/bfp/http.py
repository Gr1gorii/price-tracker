"""httpx client with a per-domain rate limiter, manual redirects and block detection."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import httpx

from bfp.config import Settings

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]

RETRY_STATUSES = {500, 502, 503, 504}
BLOCK_STATUSES = {403, 429}
# Markers of bot-challenge / block pages. Only trusted on short pages without product data,
# because some of these scripts are injected into normal pages too.
CHALLENGE_MARKERS = re.compile(
    r"<title>\s*(just a moment\.\.\.|attention required! \| cloudflare|access denied|"
    r"pardon our interruption|request unsuccessful|robot check|are you a robot)"
    r"|_cf_chl_opt|cf-chl-|captcha-delivery\.com|geo\.captcha-delivery|px-captcha|"
    r"_incapsula_resource|incapsula incident|/distil_r_captcha|perimeterx",
    re.I,
)


class DomainLimiter:
    """At least `interval` seconds between the *starts* of two requests to one domain."""

    def __init__(self, interval: float, clock: Clock = time.monotonic, sleep: Sleep = asyncio.sleep):
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None
        self._lock = asyncio.Lock()
        self.waited = 0.0

    async def wait(self) -> None:
        async with self._lock:
            if self._last is not None:
                delay = self._last + self.interval - self._clock()
                if delay > 0:
                    self.waited += delay
                    await self._sleep(delay)
            self._last = self._clock()


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None = None
    text: str | None = None
    content: bytes | None = None
    elapsed_ms: int = 0
    error: str | None = None
    blocked: bool = False
    redirects: list[str] = field(default_factory=list)
    retry_after: float | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.blocked and self.error is None


def looks_blocked(status: int | None, text: str | None) -> bool:
    if status in BLOCK_STATUSES:
        return True
    if not text or status not in (200, 202, 503):
        return False
    if len(text) > 60_000 or '"Product"' in text or "schema.org/Product" in text:
        return False
    return bool(CHALLENGE_MARKERS.search(text))


def make_client(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    headers = {
        "User-Agent": settings.ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.5",
        "From": settings.contact_email,
    }
    return httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(settings.timeout_s),
        follow_redirects=False,
        transport=transport,
    )


class Fetcher:
    """Fetches through one limiter. Every hop (redirect, retry) is a separate rate-limited request.

    `allow` is called for every redirect target (robots.txt check); a False aborts the fetch.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        limiter: DomainLimiter,
        settings: Settings,
        sleep: Sleep = asyncio.sleep,
        max_redirects: int = 5,
    ):
        self.client = client
        self.limiter = limiter
        self.settings = settings
        self._sleep = sleep
        self.max_redirects = max_redirects
        self.max_bytes = settings.max_response_mb * 1024 * 1024
        self.requests = 0

    async def _one(self, url: str) -> FetchResult:
        await self.limiter.wait()
        self.requests += 1
        t0 = time.monotonic()
        res = FetchResult(url=url, final_url=url)
        try:
            async with self.client.stream("GET", url) as r:
                res.status = r.status_code
                if r.is_redirect:
                    res.final_url = urljoin(url, r.headers.get("location", ""))
                    return res
                chunks, size = [], 0
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > self.max_bytes:
                        res.error = f"response larger than {self.settings.max_response_mb} MB"
                        return res
                    chunks.append(chunk)
                res.content = b"".join(chunks)
                ra = r.headers.get("retry-after")
                if ra and ra.strip().isdigit():
                    res.retry_after = float(ra)
                ctype = r.headers.get("content-type", "text")
                if "text" in ctype or "xml" in ctype or "json" in ctype:
                    res.text = _decode(res.content, r)
        except httpx.HTTPError as e:
            res.error = f"{type(e).__name__}: {e}"[:300]
        finally:
            res.elapsed_ms = int((time.monotonic() - t0) * 1000)
        res.blocked = looks_blocked(res.status, res.text)
        return res

    async def get(
        self,
        url: str,
        allow: Callable[[str], Awaitable[bool]] | None = None,
        retries: int = 1,
    ) -> FetchResult:
        redirects: list[str] = []
        current = url
        attempt = 0
        while True:
            res = await self._one(current)
            if res.status in (301, 302, 303, 307, 308) and res.error is None:
                nxt = res.final_url
                if not nxt or len(redirects) >= self.max_redirects:
                    res.error = "too many redirects"
                    break
                if allow is not None and not await allow(nxt):
                    res.error = f"redirect target disallowed: {nxt}"
                    res.status = None
                    break
                redirects.append(current)
                current = nxt
                continue
            retryable = res.error is not None or res.status in RETRY_STATUSES or res.status == 429
            if retryable and attempt < retries and not (res.blocked and res.status != 429):
                attempt += 1
                wait = 10.0 * attempt
                if res.status == 429:
                    if res.retry_after is None or res.retry_after > 120:
                        break
                    wait = max(wait, res.retry_after)
                await self._sleep(wait)
                continue
            break
        res.url = url
        res.final_url = current
        res.redirects = redirects
        return res


def _decode(content: bytes, r: httpx.Response) -> str:
    if r.charset_encoding:
        try:
            return content.decode(r.charset_encoding, errors="replace")
        except LookupError:
            pass
    head = content[:4096].decode("ascii", errors="ignore")
    m = re.search(r"""<meta[^>]+charset=["']?([\w-]+)""", head, re.I)
    if m:
        try:
            return content.decode(m.group(1), errors="replace")
        except LookupError:
            pass
    return content.decode("utf-8", errors="replace")


def origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc.lower()}"
