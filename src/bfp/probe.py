"""`bfp probe`: what the parser sees on a few URLs; static vs rendered; optional fixture capture."""

from __future__ import annotations

import csv
import gzip
import json
import re
from pathlib import Path

import typer

from bfp.config import Config, Paths, Shop
from bfp.http import DomainLimiter, Fetcher, make_client
from bfp.parse import parse_page
from bfp.robots import RobotsCache
from bfp.sanitize import sanitize_html


def _urls(cfg: Config, paths: Paths, shop: Shop, n: int) -> list[tuple[str, str | None]]:
    items = [(p.url, p.ean) for p in cfg.products_for(shop.name)]
    cand = paths.candidates / f"{shop.name}.csv"
    if not items and cand.exists():
        with open(cand, encoding="utf-8") as f:
            items = [(r["url"], r.get("ean") or None) for r in csv.DictReader(f)]
    return items[:n]


def _fmt(pr) -> str:
    return (
        f"{pr.status:12s} method={pr.parse_method:9s} price={pr.price} {pr.currency or ''} "
        f"prev={pr.displayed_prev_price} rrp={pr.displayed_rrp_price} low30={pr.displayed_low30_price} pct={pr.displayed_discount_pct} "
        f"avail={pr.availability} gtin={pr.page_gtin} display={pr.display_method}"
        + (f"\n      error: {pr.error}" if pr.error else "")
        + (f"\n      title: {pr.title[:90]}" if pr.title else "")
    )


async def probe(cfg: Config, paths: Paths, shop: Shop, n: int, render: bool, save_fixtures: bool = False) -> None:
    urls = _urls(cfg, paths, shop, n)
    if not urls:
        typer.echo(f"no URLs for {shop.name}: add to products.csv or run `bfp sitemap {shop.name}` first")
        return
    st = cfg.settings
    limiter = DomainLimiter(max(st.min_interval_s, shop.min_interval_s or 0))
    async with make_client(st) as client:
        fetcher = Fetcher(client, limiter, st)
        robots = RobotsCache(fetcher, st.ua_token)
        r = await robots.get(shop.base_url)
        typer.echo(f"robots.txt: {r.status} (HTTP {r.http_status}) crawl-delay={r.crawl_delay} sitemaps={r.sitemaps[:5]}")
        renderer = None
        if render:
            from bfp.render import Renderer

            renderer = await Renderer(st, shop, limiter).__aenter__()
        try:
            for url, ean in urls:
                typer.echo(f"\n• {url}\n  robots: {r.explain(url)}")
                if not await robots.allowed(url):
                    typer.echo("  SKIPPED: disallowed by robots.txt")
                    continue
                res = await fetcher.get(url, allow=robots.allowed)
                typer.echo(f"  static: HTTP {res.status} {res.elapsed_ms}ms {len(res.text or '')} chars"
                           + (" BLOCKED" if res.blocked else "") + (f" error={res.error}" if res.error else ""))
                if res.ok and res.text:
                    pr = parse_page(res.text, url, shop, ean, res.final_url)
                    if shop.low30_api and pr.status == "ok" and pr.displayed_low30_price is None and (
                        pr.displayed_discount_pct is not None or pr.displayed_prev_price is not None
                    ):
                        from bfp.extras import low30_from_api

                        api = await low30_from_api(fetcher, robots, shop, res.text)
                        typer.echo(f"  low30_api: value={api.value} error={api.error}")
                    n_ld = len(re.findall(r'application/ld\+json', res.text))
                    typer.echo(f"    {_fmt(pr)}\n      json-ld blocks: {n_ld}")
                    if save_fixtures:
                        _save_fixture(paths, shop, url, res.text, pr)
                if renderer is not None:
                    rr = await renderer.get(url)
                    typer.echo(f"  rendered: HTTP {rr.status} {rr.elapsed_ms}ms {len(rr.text or '')} chars")
                    if rr.text:
                        typer.echo(f"    {_fmt(parse_page(rr.text, url, shop, ean, rr.final_url))}")
        finally:
            if renderer is not None:
                await renderer.__aexit__(None, None, None)


def _save_fixture(paths: Paths, shop: Shop, url: str, html: str, pr) -> None:
    """Sanitised page + expected values (review expected.json by hand before committing)."""
    d = paths.root / "tests" / "fixtures" / shop.name
    d.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", url.lower().split("://", 1)[-1])[-60:].strip("-")
    clean = sanitize_html(html)
    with gzip.open(d / f"{slug}.html.gz", "wt", encoding="utf-8") as f:
        f.write(clean)
    again = parse_page(clean, url, shop)
    fields = ("price", "displayed_prev_price", "displayed_rrp_price", "displayed_low30_price", "displayed_discount_pct")
    diff = [k for k in fields if getattr(again, k) != getattr(pr, k)]
    if diff:
        typer.echo(f"      WARNING: sanitised page parses differently for {diff} — fix sanitize.py before committing")
    exp_path = d / "expected.json"
    expected = json.loads(exp_path.read_text(encoding="utf-8")) if exp_path.exists() else {}
    expected[slug] = {
        "url": url,
        "status": pr.status,
        "parse_method": pr.parse_method,
        "price": str(pr.price) if pr.price is not None else None,
        "displayed_prev_price": str(pr.displayed_prev_price) if pr.displayed_prev_price is not None else None,
        "displayed_rrp_price": str(pr.displayed_rrp_price) if pr.displayed_rrp_price is not None else None,
        "displayed_low30_price": str(pr.displayed_low30_price) if pr.displayed_low30_price is not None else None,
        "displayed_discount_pct": str(pr.displayed_discount_pct) if pr.displayed_discount_pct is not None else None,
        "availability": pr.availability,
    }
    exp_path.write_text(json.dumps(expected, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
