"""parse_page(): JSON-LD -> microdata -> CSS for the price; displayed fields alongside."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from selectolax.lexbor import LexborHTMLParser

from bfp.config import Shop
from bfp.parse import css, display, structured
from bfp.parse.prices import normalize_availability, parse_pct, parse_price

SPA_MARKERS = re.compile(
    r'id="__next"|__NEXT_DATA__|id="__nuxt"|window\.__NUXT__|ng-version=|data-reactroot|'
    r'<div id="(?:app|root)">\s*</div>|<app-root',
    re.I,
)


@dataclass
class ParseResult:
    status: str = "parse_failed"  # ok | parse_failed | needs_js
    price: Decimal | None = None
    currency: str | None = None
    availability: str | None = None
    title: str | None = None
    sku: str | None = None
    page_gtin: str | None = None
    parse_method: str = "none"
    displayed_prev_price: Decimal | None = None
    displayed_rrp_price: Decimal | None = None
    displayed_low30_price: Decimal | None = None
    displayed_discount_pct: Decimal | None = None
    display_method: str | None = None
    error: str | None = None


def parse_page(html: str, url: str, shop: Shop, ean: str | None = None, final_url: str | None = None) -> ParseResult:
    tree = LexborHTMLParser(html)
    page_urls = [u for u in (url, final_url) if u]
    canon = css.select_value(tree, 'link[rel="canonical"]@href')
    if canon:
        page_urls.append(canon)
    ld_scripts = [n.text() for n in tree.css('script[type="application/ld+json"]')]
    jsonld, micro = structured.extract(html, url, ld_scripts)

    res = ParseResult()
    errors: list[str] = []
    sres: structured.StructuredResult | None = None
    for method, items, machine in (("jsonld", jsonld, True), ("microdata", micro, False)):
        r = structured.from_items(items, page_urls, ean, machine=machine)
        if r.price is not None:
            sres, res.parse_method = r, method
            break
        if r.error and items:
            errors.append(f"{method}: {r.error}")
        sres = sres or (r if r.title or r.gtin else None)

    sel = shop.selectors
    if res.parse_method == "none":
        price = parse_price(css.select_value(tree, sel.price)) if sel.price else None
        if price is not None:
            res.parse_method = "css"
            res.price = price
            res.currency = shop.currency
        elif sel.price:
            errors.append("css: price selector matched nothing")
    else:
        res.price = sres.price
        res.currency = sres.currency or shop.currency

    if sres is not None:
        res.title = sres.title
        res.sku = sres.sku
        res.page_gtin = sres.gtin
        res.availability = sres.availability
    if sel.title:
        res.title = css.select_value(tree, sel.title) or res.title
    if res.title is None:
        res.title = css.select_value(tree, 'meta[property="og:title"]@content') or css.select_value(tree, "h1")
    if res.title:
        res.title = res.title[:300]

    # Displayed availability from the page overrides missing structured availability.
    if css.exists(tree, sel.out_of_stock):
        res.availability = "OutOfStock"
    elif res.availability is None and sel.availability:
        res.availability = normalize_availability(css.select_value(tree, sel.availability))

    _displayed(res, tree, shop, sres)

    if res.price is not None:
        res.status = "ok"
    else:
        found = any(structured.find_products(items) for items in (jsonld, micro))
        res.status = "needs_js" if _looks_like_spa(html, tree, found) else "parse_failed"
        res.error = "; ".join(errors) or "no price found (no structured data, no matching selector)"
    return res


def _displayed(res: ParseResult, tree: LexborHTMLParser, shop: Shop, sres) -> None:
    sel = shop.selectors
    methods: list[str] = []
    if sel.prev_price:
        res.displayed_prev_price = parse_price(css.select_value(tree, sel.prev_price))
        if res.displayed_prev_price is not None:
            methods.append("css")
    if res.displayed_prev_price is None and sres is not None and sres.strike_price is not None:
        res.displayed_prev_price = sres.strike_price
        methods.append("jsonld")
    if sel.rrp_price:
        res.displayed_rrp_price = parse_price(css.select_value(tree, sel.rrp_price))
        if res.displayed_rrp_price is not None and "css" not in methods:
            methods.append("css")
    if res.displayed_rrp_price is None and sres is not None and sres.rrp_price is not None:
        res.displayed_rrp_price = sres.rrp_price
        methods.append("jsonld")
    if sel.low30_price:
        res.displayed_low30_price = parse_price(css.select_value(tree, sel.low30_price))
        if res.displayed_low30_price is not None and "css" not in methods:
            methods.append("css")
    if res.displayed_low30_price is None:
        res.displayed_low30_price = display.low30_from_text(display.visible_text(tree))
        if res.displayed_low30_price is not None:
            methods.append("text")
    if sel.discount_pct:
        res.displayed_discount_pct = parse_pct(css.select_value(tree, sel.discount_pct))
        if res.displayed_discount_pct is not None and "css" not in methods:
            methods.append("css")
    if (shop.text_discount_fallback and res.price is not None
            and res.displayed_prev_price is None and res.displayed_discount_pct is None):
        hit = display.discount_from_text(display.visible_text(tree), res.price,
                                         exclude={res.displayed_rrp_price} if res.displayed_rrp_price else None)
        if hit:
            res.displayed_prev_price, res.displayed_discount_pct = hit
            methods.append("text")
    res.display_method = "+".join(dict.fromkeys(methods)) or None


def _looks_like_spa(html: str, tree: LexborHTMLParser, found_product: bool) -> bool:
    """No Product data and no visible euro amount, plus an SPA shell or almost no text."""
    if found_product:
        return False
    text = display.visible_text(tree)
    if "€" in text or re.search(r"\d+,\d{2}\s*EUR", text):
        return False
    return len(text) < 1500 or bool(SPA_MARKERS.search(html[:200_000]))
