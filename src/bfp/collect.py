"""One collection run: all enabled shops concurrently, each shop strictly sequential and rate-limited."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit

import httpx

from bfp import __version__
from bfp.config import Config, Paths, Product, Shop
from bfp.extras import low30_from_api
from bfp.http import DomainLimiter, Fetcher, FetchResult, make_client
from bfp.parse import parse_page
from bfp.robots import RobotsCache
from bfp.schedule import Slot, shop_done
from bfp import storage

log = logging.getLogger("bfp.collect")
CHECKPOINT_EVERY = 100


@dataclass
class ShopRun:
    shop: str
    rows: list[dict] = field(default_factory=list)
    robots_status: str | None = None
    crawl_delay: float | None = None
    interval_s: float = 0.0
    stopped_reason: str | None = None
    skipped_reason: str | None = None
    requests: int = 0
    seconds: float = 0.0
    file: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        return dict(Counter(r["status"] for r in self.rows))


def select_shops(cfg: Config, runner: str, only: list[str] | None) -> list[Shop]:
    if only:
        unknown = [s for s in only if s not in cfg.shops]
        if unknown:
            raise ValueError(f"unknown shops: {unknown}")
        shops = [cfg.shops[s] for s in only]
        disabled = [s.name for s in shops if not s.enabled]
        if disabled:
            raise ValueError(f"shops not enabled in shops.yaml: {disabled}")
        return shops
    return [s for s in cfg.shops.values() if s.enabled and s.runner == runner]


def _row(run_id: str, slot: Slot, shop: Shop, p: Product, **kw) -> dict:
    row = dict(
        run_id=run_id, slot=slot.label, ts=datetime.now(timezone.utc), shop=shop.name, url=p.url,
        final_url=None, category=p.category, ean=p.ean, page_gtin=None, sku=None, title=None,
        price=None, currency=None, displayed_prev_price=None, displayed_rrp_price=None, displayed_low30_price=None,
        displayed_discount_pct=None, availability=None, parse_method="none", display_method=None,
        status="parse_failed", http_status=None, rendered=shop.render, elapsed_ms=None, error=None,
        html_snapshot=None, collector_version=__version__,
    )
    row.update(kw)
    return row


def _redirected_away(original: str, final: str, product_pattern: str | None = None) -> bool:
    """Product URL redirected to home/category -> the product is gone."""
    a, b = urlsplit(original), urlsplit(final)
    if a.path.rstrip("/") == b.path.rstrip("/"):
        return False
    if product_pattern and not re.search(product_pattern, final):
        return True  # landed on something that is not a product page (e.g. its category)
    return b.path.strip("/") == "" or len(b.path.strip("/").split("/")) < len(a.path.strip("/").split("/")) - 1


async def run_shop(
    cfg: Config,
    paths: Paths,
    shop: Shop,
    products: list[Product],
    slot: Slot,
    runner: str,
    transport: httpx.AsyncBaseTransport | None = None,
    limiter_factory: Callable[[float], DomainLimiter] | None = None,
) -> ShopRun:
    run_id = f"{slot.label}__{runner}"
    st = cfg.settings
    out = ShopRun(shop=shop.name)
    t0 = time.monotonic()
    interval = max(st.min_interval_s, shop.min_interval_s or 0)
    limiter = (limiter_factory or DomainLimiter)(interval)
    failed_saved = 0
    block_signals = 0

    async with make_client(st, transport) as client:
        fetcher = Fetcher(client, limiter, st, sleep=limiter._sleep)
        robots = RobotsCache(fetcher, st.ua_token)
        main_robots = await robots.get(shop.base_url)
        out.robots_status = main_robots.status
        out.crawl_delay = main_robots.crawl_delay
        if out.crawl_delay and out.crawl_delay > limiter.interval:
            limiter.interval = out.crawl_delay
            if out.crawl_delay > st.max_crawl_delay_s:
                log.warning("%s: robots.txt Crawl-delay %.0fs (> %.0fs) — run will be slow", shop.name, out.crawl_delay, st.max_crawl_delay_s)
        out.interval_s = limiter.interval

        if main_robots.status == "blocked":
            out.stopped_reason = "robots.txt request blocked"
            block_signals = st.block_threshold
        elif main_robots.status == "unavailable":
            log.warning("%s: robots.txt unavailable (%s) — nothing fetched this run", shop.name, main_robots.http_status)

        renderer_cm = None
        if shop.render:
            from bfp.render import Renderer

            renderer_cm = Renderer(st, shop, limiter)
        renderer = None
        try:
            if renderer_cm is not None:
                try:
                    renderer = await renderer_cm.__aenter__()
                except Exception as e:  # RenderUnavailable or browser launch failure
                    out.stopped_reason = f"render unavailable: {e}"
                    renderer_cm = None
            for i, p in enumerate(products):
                if block_signals >= st.block_threshold or (shop.render and renderer is None):
                    out.rows.append(_row(run_id, slot, shop, p, status="skipped_blocked" if block_signals else "render_error",
                                         error=out.stopped_reason))
                    continue
                try:
                    row = await _collect_one(cfg, paths, shop, p, slot, run_id, fetcher, robots, renderer)
                except Exception as e:  # never lose a whole shop to one odd page
                    log.exception("%s: unexpected error on %s", shop.name, p.url)
                    row = _row(run_id, slot, shop, p, status="parse_failed", error=f"exception: {type(e).__name__}: {e}"[:300])
                if row["status"] == "blocked" or row.pop("_block_signal", False):
                    block_signals += 1
                    if block_signals >= st.block_threshold:
                        out.stopped_reason = f"{block_signals} block signals (last: HTTP {row['http_status']})"
                        log.error("%s: stopping — %s", shop.name, out.stopped_reason)
                html = row.pop("_html", None)
                if html is not None and failed_saved < st.max_failed_html_per_shop:
                    row["html_snapshot"] = storage.save_failed_html(paths, slot, shop.name, p.url, html)
                    failed_saved += 1
                out.rows.append(row)
                if (i + 1) % 25 == 0:
                    log.info("%s: %d/%d %s", shop.name, i + 1, len(products), dict(Counter(r["status"] for r in out.rows)))
                if (i + 1) % CHECKPOINT_EVERY == 0:  # survive a killed job (GitHub timeout)
                    storage.write_observations(paths, slot, runner, shop.name, out.rows)
        finally:
            if renderer_cm is not None:
                await renderer_cm.__aexit__(None, None, None)
        out.requests = fetcher.requests

    if out.stopped_reason and block_signals >= st.block_threshold:
        storage.mark_blocked(paths, shop.name, run_id, out.stopped_reason)
    out.seconds = round(time.monotonic() - t0, 1)
    if out.rows:
        out.file = storage.write_observations(paths, slot, runner, shop.name, out.rows).relative_to(paths.root).as_posix()
    return out


async def _collect_one(cfg, paths, shop, p, slot, run_id, fetcher: Fetcher, robots: RobotsCache, renderer) -> dict:
    if not await robots.allowed(p.url):
        r = await robots.get(p.url)
        return _row(run_id, slot, shop, p, status="robots_disallowed",
                    error=None if r.status == "parsed" else f"robots.txt {r.status}")
    if renderer is not None:
        res: FetchResult = await renderer.get(p.url)
        if res.final_url != p.url and not await robots.allowed(res.final_url):
            return _row(run_id, slot, shop, p, status="robots_disallowed", final_url=res.final_url,
                        error="rendered redirect to disallowed URL")
    else:
        res = await fetcher.get(p.url, allow=robots.allowed)
    base = dict(final_url=res.final_url, http_status=res.status, elapsed_ms=res.elapsed_ms, ts=datetime.now(timezone.utc))
    if res.error and res.error.startswith("redirect target disallowed"):
        return _row(run_id, slot, shop, p, status="robots_disallowed", error=res.error, **base)
    if res.blocked:
        return _row(run_id, slot, shop, p, status="blocked", error="block/challenge page", **base)
    if res.error or res.status is None:
        return _row(run_id, slot, shop, p, status="render_error" if renderer else "network_error", error=res.error, **base)
    if res.status in (404, 410):
        return _row(run_id, slot, shop, p, status="not_found", **base)
    if res.status != 200:
        return _row(run_id, slot, shop, p, status="http_error", error=f"HTTP {res.status}", **base)
    html = res.text or ""
    parsed = parse_page(html, p.url, shop, ean=p.ean, final_url=res.final_url)
    if parsed.status != "ok" and res.final_url != p.url and _redirected_away(p.url, res.final_url, shop.product_url_pattern):
        return _row(run_id, slot, shop, p, status="not_found", error=f"redirected to {res.final_url}", **base)
    row = _row(
        run_id, slot, shop, p,
        status=parsed.status, price=parsed.price, currency=parsed.currency, availability=parsed.availability,
        title=parsed.title, sku=parsed.sku, page_gtin=parsed.page_gtin, parse_method=parsed.parse_method,
        displayed_prev_price=parsed.displayed_prev_price, displayed_rrp_price=parsed.displayed_rrp_price,
        displayed_low30_price=parsed.displayed_low30_price,
        displayed_discount_pct=parsed.displayed_discount_pct, display_method=parsed.display_method,
        error=parsed.error, **base,
    )
    if parsed.status != "ok":
        row["_html"] = html
    elif shop.low30_api and parsed.displayed_low30_price is None and (
        not shop.low30_api.only_when_discount
        or parsed.displayed_discount_pct is not None
        or parsed.displayed_prev_price is not None
    ):
        api = await low30_from_api(fetcher, robots, shop, html)
        if api.value is not None:
            row["displayed_low30_price"] = api.value
            row["display_method"] = "+".join(filter(None, [row["display_method"], "api"]))
        if api.error:
            row["error"] = api.error
        row["_block_signal"] = api.blocked
    return row


async def run_collection(
    cfg: Config,
    paths: Paths,
    slot: Slot,
    runner: str,
    only: list[str] | None = None,
    limit: int | None = None,
    force: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    limiter_factory: Callable[[float], DomainLimiter] | None = None,
) -> dict:
    started = datetime.now(timezone.utc)
    run_id = f"{slot.label}__{runner}"
    blocked = storage.load_blocked(paths)
    shops = select_shops(cfg, runner, only)
    runs: list[ShopRun] = []
    tasks, task_shops = [], []
    for shop in shops:
        products = cfg.products_for(shop.name)[: limit or None]
        if shop.name in blocked:
            runs.append(ShopRun(shop.name, skipped_reason=f"blocked since {blocked[shop.name]['since']} — see data/state/blocked.json"))
            continue
        if not products:
            runs.append(ShopRun(shop.name, skipped_reason="no products in products.csv"))
            continue
        if not force and shop_done(paths.observations, slot, runner, shop.name):
            runs.append(ShopRun(shop.name, skipped_reason="already collected in this slot"))
            continue
        tasks.append(run_shop(cfg, paths, shop, products, slot, runner, transport, limiter_factory))
        task_shops.append(shop.name)
    log.info("run %s: %d shops to collect", run_id, len(tasks))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for shop_name, r in zip(task_shops, results):
        if isinstance(r, BaseException):
            log.exception("shop %s crashed", shop_name, exc_info=r)
            runs.append(ShopRun(shop_name, stopped_reason=f"crash: {type(r).__name__}: {r}"))
        else:
            runs.append(r)
    manifest = {
        "run_id": run_id,
        "slot": slot.label,
        "runner": runner,
        "scheduled": slot.scheduled,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "collector_version": __version__,
        "shops": {
            r.shop: {
                "counts": r.counts,
                "total": len(r.rows),
                "robots": r.robots_status,
                "crawl_delay": r.crawl_delay,
                "interval_s": r.interval_s,
                "requests": r.requests,
                "seconds": r.seconds,
                "stopped_reason": r.stopped_reason,
                "skipped_reason": r.skipped_reason,
                "file": r.file,
            }
            for r in sorted(runs, key=lambda x: x.shop)
        },
    }
    storage.write_manifest(paths, slot, runner, manifest)
    return manifest
