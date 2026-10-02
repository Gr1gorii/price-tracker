"""bfp — command line."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections import Counter
from datetime import date
from typing import Optional

import typer

from bfp.config import Paths, load_config

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Black Friday 2026 price tracker")


def _setup(verbose: bool = False):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    paths = Paths()
    return paths, load_config(paths)


def _shops_arg(shops: Optional[str]) -> list[str] | None:
    return [s.strip() for s in shops.split(",") if s.strip()] if shops else None


@app.command()
def validate():
    """Check settings.yaml, shops.yaml and products.csv."""
    paths, cfg = _setup()
    enabled = [s for s in cfg.shops.values() if s.enabled]
    typer.echo(f"settings ok · UA: {cfg.settings.ua}")
    typer.echo(f"shops: {len(cfg.shops)} ({len(enabled)} enabled: {', '.join(s.name for s in enabled) or '—'})")
    per_shop = Counter(p.shop for p in cfg.products)
    per_cat = Counter(p.category for p in cfg.products)
    typer.echo(f"products: {len(cfg.products)} · by shop {dict(per_shop)} · by category {dict(per_cat)}")
    eans = Counter(p.ean for p in cfg.products if p.ean)
    multi = sum(1 for n in eans.values() if n > 1)
    typer.echo(f"EANs: {len(eans)} distinct, {multi} present in ≥ 2 shops")
    for s in cfg.shops.values():
        if per_shop.get(s.name) and not s.enabled:
            typer.echo(f"  note: {s.name} has {per_shop[s.name]} products but is disabled")
    for w in cfg.warnings:
        typer.echo(f"  warning: {w}")


@app.command()
def estimate():
    """Expected run duration per shop (1 request per interval, shops in parallel)."""
    paths, cfg = _setup()
    worst = 0.0
    for s in cfg.shops.values():
        n = len(cfg.products_for(s.name))
        if not s.enabled or not n:
            continue
        interval = max(cfg.settings.min_interval_s, s.min_interval_s or 0)
        secs = (n + 1) * interval
        worst = max(worst, secs) if s.runner == "actions" else worst
        typer.echo(f"{s.name:20s} {s.runner:7s} {n:5d} URLs × {interval:.0f}s ≈ {secs / 60:5.1f} min{' (+render)' if s.render else ''}")
    typer.echo(f"GitHub run ≈ {worst / 60:.0f} min (+ setup), robots.txt Crawl-delay may increase it")


@app.command()
def gate(
    runner: str = typer.Option("actions", help="actions | local"),
    force: bool = typer.Option(False, help="Run now even outside a slot window (manual slot label)"),
):
    """For cron: is it time to collect? Prints run=… and slot=… (also to $GITHUB_OUTPUT)."""
    from bfp.schedule import current_slot, manual_slot, run_done

    paths, cfg = _setup()
    slot = current_slot(cfg.settings)
    reason = ""
    if force:
        slot = slot if slot and not run_done(paths.health, slot, runner) else manual_slot(cfg.settings)
        run = True
    elif slot is None:
        run, reason = False, "outside slot windows"
    elif run_done(paths.health, slot, runner):
        run, reason = False, f"slot {slot.label} already done"
    else:
        run = True
    lines = [f"run={'true' if run else 'false'}", f"slot={slot.label if slot else ''}"]
    typer.echo("\n".join(lines) + (f"\n# {reason}" if reason else ""))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


@app.command()
def collect(
    shops: Optional[str] = typer.Option(None, help="Comma-separated shop names (default: all enabled for this runner)"),
    runner: str = typer.Option("local", help="actions | local"),
    slot: Optional[str] = typer.Option(None, help="Slot label from `bfp gate`; default: current slot or a manual label"),
    scheduled: bool = typer.Option(False, help="Apply the gate: collect only inside a slot window, once per slot"),
    limit: Optional[int] = typer.Option(None, help="Max products per shop"),
    force: bool = typer.Option(False, help="Re-collect shops already done in this slot"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Run one collection."""
    from bfp.collect import run_collection
    from bfp.schedule import current_slot, manual_slot, parse_slot, run_done

    paths, cfg = _setup(verbose)
    if scheduled:
        s = current_slot(cfg.settings)
        if s is None or run_done(paths.health, s, runner):
            typer.echo(f"nothing to do ({'outside slot windows' if s is None else f'slot {s.label} already done'})")
            raise typer.Exit(0)
    elif slot:
        s = parse_slot(slot)
    else:
        s = current_slot(cfg.settings) or manual_slot(cfg.settings)
    manifest = asyncio.run(run_collection(cfg, paths, s, runner, _shops_arg(shops), limit, force))
    for shop, info in manifest["shops"].items():
        extra = info.get("skipped_reason") or info.get("stopped_reason") or ""
        typer.echo(f"{shop:20s} {info['total']:5d} rows {info['counts']} {info['seconds']}s {extra}")


@app.command()
def health(
    runs: int = typer.Option(1, help="Aggregate over the last N runs"),
    alert: bool = typer.Option(False, help="Send Telegram alert if a shop is below threshold"),
):
    """Per-shop parse-success table (+ missing slots); also written to reports/health.md."""
    from bfp.health import report

    paths, cfg = _setup()
    typer.echo(report(cfg, paths, runs=runs, alert=alert))


@app.command()
def unblock(shop: str):
    """Remove a shop from data/state/blocked.json (after you re-checked it)."""
    from bfp.storage import unblock as _unblock

    paths, _ = _setup()
    typer.echo("unblocked" if _unblock(paths, shop) else "was not blocked")


@app.command()
def probe(
    shop: str,
    n: int = typer.Option(5, help="URLs to probe"),
    render: bool = typer.Option(False, help="Also try Playwright"),
    save_fixtures: bool = typer.Option(False, help="Save sanitised pages + expected.json to tests/fixtures/<shop>/"),
):
    """What the parser sees on N product URLs of a shop (products.csv, else candidates/<shop>.csv)."""
    from bfp.probe import probe as _probe

    paths, cfg = _setup()
    asyncio.run(_probe(cfg, paths, cfg.shops[shop], n, render, save_fixtures))


@app.command()
def sitemap(
    shop: str,
    pattern: Optional[str] = typer.Option(None, help="Regex for product URLs (default: shop.product_url_pattern)"),
    keywords: Optional[str] = typer.Option(None, help="Comma-separated words that must appear in the URL"),
    limit: int = typer.Option(500),
    max_sitemaps: int = typer.Option(20, help="Max sitemap files to download"),
    enrich: int = typer.Option(0, help="Fetch N candidate pages to fill title/EAN/price"),
):
    """Propose product URLs from sitemaps → candidates/<shop>.csv (you pick, then copy to products.csv)."""
    from bfp.sitemap import propose, write_candidates

    paths, cfg = _setup()
    s = cfg.shops[shop]
    cands, stats = asyncio.run(propose(cfg, s, pattern, _shops_arg(keywords), limit, max_sitemaps, enrich))
    out = write_candidates(paths, s, cands)
    typer.echo(f"{len(cands)} candidates → {out.relative_to(paths.root)}  stats={stats}")


@app.command()
def listing(
    shop: str,
    urls: list[str] = typer.Argument(..., help="Category/listing page URLs (or @file with one URL per line)"),
    pattern: Optional[str] = typer.Option(None, help="Regex for product URLs (default: shop.product_url_pattern)"),
    limit: int = typer.Option(500),
):
    """Product URLs linked from listing pages → candidates/<shop>.csv (for shops without product sitemaps)."""
    from bfp.sitemap import from_listings, write_candidates

    paths, cfg = _setup()
    s = cfg.shops[shop]
    expanded: list[str] = []
    for u in urls:
        if u.startswith("@"):
            expanded += [x.strip() for x in open(u[1:], encoding="utf-8") if x.strip() and not x.startswith("#")]
        else:
            expanded.append(u)
    cands, stats = asyncio.run(from_listings(cfg, s, expanded, pattern, limit))
    out = write_candidates(paths, s, cands)
    typer.echo(f"{len(cands)} candidates → {out.relative_to(paths.root)}  stats={stats}")


@app.command("ean-matches")
def ean_matches():
    """EANs present in more than one shop (products.csv + observed page GTINs)."""
    from bfp.storage import connect

    paths, cfg = _setup()
    con = connect(paths)
    rows = con.execute(
        """
        WITH e AS (
          SELECT DISTINCT shop, coalesce(ean, page_gtin) AS ean, url FROM observations WHERE coalesce(ean, page_gtin) IS NOT NULL
        )
        SELECT ean, count(DISTINCT shop) AS shops, string_agg(DISTINCT shop, ',') FROM e GROUP BY ean HAVING shops > 1 ORDER BY shops DESC
        """
    ).fetchall()
    cfg_eans: dict[str, set] = {}
    for p in cfg.products:
        if p.ean:
            cfg_eans.setdefault(p.ean, set()).add(p.shop)
    typer.echo(f"products.csv: {sum(1 for v in cfg_eans.values() if len(v) > 1)} EANs in ≥ 2 shops")
    typer.echo(f"observed:     {len(rows)} EANs in ≥ 2 shops")
    for ean, k, shops_ in rows[:50]:
        typer.echo(f"  {ean}  {k}  {shops_}")


@app.command()
def sql(query: str):
    """Run SQL against the `observations` view, e.g. bfp sql "select shop, count(*) from observations group by 1"."""
    from bfp.storage import connect

    paths, _ = _setup()
    typer.echo(connect(paths).sql(query).df().to_string(max_rows=200, max_colwidth=60))


@app.command()
def check(
    date_from: Optional[str] = typer.Option(None, "--from", help="YYYY-MM-DD (Rome), filter output"),
    date_to: Optional[str] = typer.Option(None, "--to", help="YYYY-MM-DD (Rome)"),
    history_url: Optional[str] = typer.Option(None, help="URL for chart (a); default: worst flagged episode"),
):
    """Compliance check → reports/compliance.csv, episodes.csv and 3 PNGs."""
    from bfp import compliance, plots

    paths, cfg = _setup()
    df = compliance.load_ok_observations(paths)
    obs, eps = compliance.run(cfg, paths, _d(date_from), _d(date_to), df=df)
    _report_check(paths, cfg, df, obs, eps, history_url)


def _d(s: Optional[str]) -> date | None:
    return date.fromisoformat(s) if s else None


def _report_check(paths, cfg, df, obs, eps, history_url) -> None:
    from bfp import compliance, plots

    v = Counter(eps["verdict"]) if len(eps) else Counter()
    typer.echo(f"discount observations: {len(obs)} · episodes: {len(eps)} · verdicts {dict(v)}")
    typer.echo(f"→ {paths.reports.relative_to(paths.root)}/compliance.csv, episodes.csv")
    if df.empty:
        typer.echo("no ok observations yet — no charts")
        return
    url = history_url or plots.pick_history_url(obs) or df["url"].iloc[0]
    prepared = df if "purchasable" in df else compliance.prepare(df)
    outs = [
        plots.price_history(prepared, url, paths.reports / "a_price_history.png", cfg.settings.compliance),
        plots.claimed_vs_honest(eps, paths.reports / "b_claimed_vs_honest.png"),
        plots.flagged_by_category(eps, paths.reports / "c_flagged_by_category.png"),
    ]
    for o in outs:
        typer.echo(f"→ {o.relative_to(paths.root)}")


@app.command()
def demo(seed: int = 7):
    """Charts + CSV from a SYNTHETIC 50-day history (no real data) → reports/demo/."""
    from bfp import compliance, plots
    from bfp.demo import synthetic_history

    paths, cfg = _setup()
    plots.WATERMARK = "SYNTHETIC DATA — demo, not real observations"
    paths.reports = paths.reports / "demo"
    df = synthetic_history(seed)
    obs, eps = compliance.run(cfg, paths, date(2026, 11, 20), date(2026, 11, 30), df=df)
    _report_check(paths, cfg, compliance.prepare(df), obs, eps, None)


if __name__ == "__main__":
    sys.exit(app())
