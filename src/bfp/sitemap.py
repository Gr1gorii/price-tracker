"""Propose product URLs from a shop's sitemaps (robots.txt respected, same 1 req / 5 s limit)."""

from __future__ import annotations

import csv
import gzip
import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
from lxml import etree

from bfp.config import Config, Paths, Shop
from bfp.http import DomainLimiter, Fetcher, make_client
from bfp.parse import parse_page
from bfp.robots import RobotsCache

log = logging.getLogger("bfp.sitemap")
PRODUCT_HINT = re.compile(r"product|prodott|articol|item|catalog|sku|\bp\b|pdp", re.I)
XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True, recover=True)


@dataclass
class Candidate:
    url: str
    lastmod: str | None = None
    title: str | None = None
    ean: str | None = None
    price: str | None = None
    status: str | None = None


def parse_sitemap(content: bytes) -> tuple[list[str], list[tuple[str, str | None]]]:
    """(child sitemaps, [(url, lastmod)]) — handles .gz and both sitemapindex/urlset."""
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    root = etree.fromstring(content, XML_PARSER)
    if root is None:
        return [], []
    tag = etree.QName(root).localname.lower()
    children, urls = [], []
    for el in root:
        if not isinstance(el.tag, str):
            continue
        loc = lastmod = None
        for sub in el:
            if not isinstance(sub.tag, str):
                continue
            name = etree.QName(sub).localname.lower()
            if name == "loc":
                loc = (sub.text or "").strip()
            elif name == "lastmod":
                lastmod = (sub.text or "").strip() or None
        if not loc:
            continue
        if tag == "sitemapindex":
            children.append(loc)
        else:
            urls.append((loc, lastmod))
    return children, urls


async def propose(
    cfg: Config,
    shop: Shop,
    pattern: str | None = None,
    keywords: list[str] | None = None,
    limit: int = 500,
    max_sitemaps: int = 20,
    enrich: int = 0,
    transport: httpx.AsyncBaseTransport | None = None,
    limiter: DomainLimiter | None = None,
) -> tuple[list[Candidate], dict]:
    st = cfg.settings
    limiter = limiter or DomainLimiter(max(st.min_interval_s, shop.min_interval_s or 0))
    rx = re.compile(pattern or shop.product_url_pattern) if (pattern or shop.product_url_pattern) else None
    kws = [k.strip().lower() for k in (keywords or []) if k.strip()]
    existing = {p.url for p in cfg.products}
    stats = {"sitemaps_fetched": 0, "urls_seen": 0, "disallowed": 0, "pattern_rejected": 0, "keyword_rejected": 0}

    async with make_client(st, transport) as client:
        fetcher = Fetcher(client, limiter, st, sleep=limiter._sleep)
        robots = RobotsCache(fetcher, st.ua_token)
        r = await robots.get(shop.base_url)
        if r.crawl_delay and r.crawl_delay > limiter.interval:
            limiter.interval = r.crawl_delay
        queue = list(r.sitemaps) or [f"{shop.base_url}/sitemap.xml", f"{shop.base_url}/sitemap_index.xml"]
        stats["robots"] = r.status
        stats["robots_sitemaps"] = list(r.sitemaps)
        seen_maps: set[str] = set()
        found: dict[str, Candidate] = {}
        while queue and stats["sitemaps_fetched"] < max_sitemaps and len(found) < limit:
            sm = queue.pop(0)
            if sm in seen_maps:
                continue
            seen_maps.add(sm)
            if not await robots.allowed(sm):
                log.info("sitemap disallowed by robots: %s", sm)
                continue
            res = await fetcher.get(sm, allow=robots.allowed)
            stats["sitemaps_fetched"] += 1
            if not res.ok or not res.content:
                log.info("sitemap %s: HTTP %s %s", sm, res.status, res.error or "")
                continue
            try:
                children, urls = parse_sitemap(res.content)
            except (etree.XMLSyntaxError, OSError, ValueError) as e:
                log.info("sitemap %s unparseable: %s", sm, e)
                continue
            # product-looking child sitemaps first
            children.sort(key=lambda u: 0 if PRODUCT_HINT.search(u.rsplit("/", 1)[-1]) else 1)
            queue = children + queue
            for url, lastmod in urls:
                stats["urls_seen"] += 1
                if url in existing or url in found or not shop.owns(url):
                    continue
                if rx and not rx.search(url):
                    stats["pattern_rejected"] += 1
                    continue
                if kws and not any(k in url.lower() for k in kws):
                    stats["keyword_rejected"] += 1
                    continue
                if not await robots.allowed(url):
                    stats["disallowed"] += 1
                    continue
                found[url] = Candidate(url, lastmod)
                if len(found) >= limit:
                    break
        cands = list(found.values())
        if enrich:
            sample = random.Random(42).sample(cands, min(enrich, len(cands)))
            for c in sample:
                res = await fetcher.get(c.url, allow=robots.allowed)
                if not res.ok or not res.text:
                    c.status = f"HTTP {res.status}" if not res.blocked else "blocked"
                    if res.blocked:
                        log.warning("blocked while enriching — stopping enrichment")
                        break
                    continue
                pr = parse_page(res.text, c.url, shop, final_url=res.final_url)
                c.title, c.ean, c.status = pr.title, pr.page_gtin, pr.status
                c.price = str(pr.price) if pr.price is not None else None
        stats["requests"] = fetcher.requests
    return cands, stats


def write_candidates(paths: Paths, shop: Shop, cands: list[Candidate]) -> Path:
    paths.candidates.mkdir(parents=True, exist_ok=True)
    out = paths.candidates / f"{shop.name}.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["shop", "url", "category", "ean", "title", "price", "parse_status", "lastmod"])
        for c in cands:
            w.writerow([shop.name, c.url, "", c.ean or "", c.title or "", c.price or "", c.status or "", c.lastmod or ""])
    return out


async def from_listings(
    cfg: Config,
    shop: Shop,
    listing_urls: list[str],
    pattern: str | None = None,
    limit: int = 500,
    transport: httpx.AsyncBaseTransport | None = None,
    limiter: DomainLimiter | None = None,
) -> tuple[list[Candidate], dict]:
    """Product URLs linked from category/listing pages (for shops whose sitemap lacks products)."""
    from urllib.parse import urljoin

    st = cfg.settings
    limiter = limiter or DomainLimiter(max(st.min_interval_s, shop.min_interval_s or 0))
    rx = re.compile(pattern or shop.product_url_pattern or r".")
    existing = {p.url for p in cfg.products}
    found: dict[str, Candidate] = {}
    stats = {"listings_fetched": 0, "listings_disallowed": 0, "links_seen": 0, "disallowed": 0}
    async with make_client(st, transport) as client:
        fetcher = Fetcher(client, limiter, st, sleep=limiter._sleep)
        robots = RobotsCache(fetcher, st.ua_token)
        for lu in listing_urls:
            if len(found) >= limit:
                break
            if not await robots.allowed(lu):
                stats["listings_disallowed"] += 1
                continue
            res = await fetcher.get(lu, allow=robots.allowed)
            stats["listings_fetched"] += 1
            if res.blocked:
                log.warning("blocked on %s — stopping", lu)
                break
            if not res.ok or not res.text:
                continue
            for href in re.findall(r'href="([^"#]+)"', res.text):
                url = urljoin(res.final_url, href).split("?", 1)[0]
                stats["links_seen"] += 1
                if url in found or url in existing or not shop.owns(url) or not rx.search(url):
                    continue
                if not await robots.allowed(url):
                    stats["disallowed"] += 1
                    continue
                found[url] = Candidate(url, status=lu.rsplit("/", 1)[-1])
                if len(found) >= limit:
                    break
        stats["requests"] = fetcher.requests
    return list(found.values()), stats
