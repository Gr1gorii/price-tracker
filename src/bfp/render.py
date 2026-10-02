"""Playwright rendering for shops with `render: true` (optional dependency: `uv sync --extra render`)."""

from __future__ import annotations

import time

from bfp.config import Settings, Shop
from bfp.http import DomainLimiter, FetchResult, looks_blocked

BLOCKED_RESOURCES = {"image", "media", "font"}


class RenderUnavailable(RuntimeError):
    pass


class Renderer:
    """One headless Chromium per shop worker; every page navigation goes through the shop limiter."""

    def __init__(self, settings: Settings, shop: Shop, limiter: DomainLimiter):
        self.settings = settings
        self.shop = shop
        self.limiter = limiter
        self._pw = self._browser = self._context = None

    async def __aenter__(self) -> "Renderer":
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RenderUnavailable("playwright not installed: uv sync --extra render && uv run playwright install chromium") from e
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            user_agent=self.settings.ua,
            locale="it-IT",
            extra_http_headers={"From": self.settings.contact_email},
        )

        async def _route(route):
            if route.request.resource_type in BLOCKED_RESOURCES:
                await route.abort()
            else:
                await route.continue_()

        await self._context.route("**/*", _route)
        return self

    async def __aexit__(self, *exc) -> None:
        for obj in (self._context, self._browser):
            if obj is not None:
                await obj.close()
        if self._pw is not None:
            await self._pw.stop()

    async def get(self, url: str) -> FetchResult:
        await self.limiter.wait()
        t0 = time.monotonic()
        res = FetchResult(url=url, final_url=url)
        page = await self._context.new_page()
        timeout_ms = int(self.settings.timeout_s * 1000)
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            res.status = resp.status if resp else None
            try:
                if self.shop.wait_for:
                    await page.wait_for_selector(self.shop.wait_for, timeout=15_000)
                else:
                    await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass  # take whatever rendered; the parser decides
            res.final_url = page.url
            res.text = await page.content()
        except Exception as e:
            res.error = f"{type(e).__name__}: {e}"[:300]
        finally:
            await page.close()
            res.elapsed_ms = int((time.monotonic() - t0) * 1000)
        res.blocked = looks_blocked(res.status, res.text)
        return res
