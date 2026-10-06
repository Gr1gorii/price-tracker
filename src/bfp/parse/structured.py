"""schema.org Product/Offer extraction from JSON-LD and microdata (via extruct)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

import extruct

from bfp.parse.prices import normalize_availability, parse_price

log = logging.getLogger(__name__)

# Keys that hold *other* products (carousels, accessories) or user content — never descend.
SKIP_KEYS = {
    "isRelatedTo", "isSimilarTo", "isAccessoryOrSparePartFor", "isConsumableFor",
    "review", "reviews", "aggregateRating", "itemListElement", "comment", "author",
}
GTIN_KEYS = ("gtin13", "gtin", "gtin14", "gtin12", "gtin8", "ean")
STRIKE_TYPES = ("strikethroughprice",)
RRP_TYPES = ("listprice", "msrp", "suggestedretailprice", "srp")


@dataclass
class OfferCandidate:
    price: Decimal | None
    currency: str | None
    availability: str | None
    url: str | None
    sku: str | None
    gtin: str | None
    title: str | None
    strike_price: Decimal | None
    rrp_price: Decimal | None
    product_index: int


@dataclass
class StructuredResult:
    price: Decimal | None = None
    currency: str | None = None
    availability: str | None = None
    sku: str | None = None
    gtin: str | None = None
    title: str | None = None
    strike_price: Decimal | None = None
    rrp_price: Decimal | None = None
    n_products: int = 0
    error: str | None = None


def _types(node: dict) -> set[str]:
    t = node.get("@type") or node.get("type") or []
    if isinstance(t, str):
        t = [t]
    return {str(x).rsplit("/", 1)[-1] for x in t}


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _text(v) -> str | None:
    v = _first(v)
    if isinstance(v, dict):
        v = v.get("name") or v.get("@value") or v.get("@id")
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _gtin(node: dict) -> str | None:
    for k in GTIN_KEYS:
        v = _text(node.get(k))
        if v:
            d = re.sub(r"\D", "", v)
            if len(d) in (8, 12, 13, 14):
                return d
    pid = _text(node.get("productID"))
    if pid and re.fullmatch(r"(?:ean|gtin\d*)?:?\s*\d{8,14}", pid, re.I):
        return re.sub(r"\D", "", pid)
    return None


def find_products(data) -> list[dict]:
    """Depth-first, document order; does not descend into related-product/review keys."""
    out: list[dict] = []

    def walk(node, depth=0):
        if depth > 12:
            return
        if isinstance(node, list):
            for x in node:
                walk(x, depth + 1)
        elif isinstance(node, dict):
            types = _types(node)
            if types & {"Product", "ProductGroup", "IndividualProduct", "ProductModel"}:
                out.append(node)
                for v in node.get("hasVariant") or []:
                    if isinstance(v, dict):
                        out.append({**v, "_parent": node})
                return
            for k, v in node.items():
                if k not in SKIP_KEYS:
                    walk(v, depth + 1)

    walk(data)
    return out


def _price_specs(offer: dict, machine: bool) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """(current, strikethrough, list/RRP) from priceSpecification."""
    cur = strike = rrp = None
    specs = offer.get("priceSpecification") or []
    if isinstance(specs, dict):
        specs = [specs]
    for s in specs:
        if not isinstance(s, dict):
            continue
        p = parse_price(s.get("price"), machine=machine)
        ptype = str(s.get("priceType") or "").rsplit("/", 1)[-1].lower()
        if any(t in ptype for t in STRIKE_TYPES):
            strike = strike or p
        elif any(t in ptype for t in RRP_TYPES):
            rrp = rrp or p
        elif p is not None and cur is None:
            cur = p
    return cur, strike, rrp


def _offers_of(product: dict) -> list[dict]:
    offers = product.get("offers")
    if offers is None and "_parent" in product:
        offers = product["_parent"].get("offers")
    if isinstance(offers, dict):
        offers = [offers]
    flat: list[dict] = []
    for o in offers or []:
        if not isinstance(o, dict):
            continue
        if "AggregateOffer" in _types(o) and o.get("offers"):
            inner = o["offers"] if isinstance(o["offers"], list) else [o["offers"]]
            flat.extend(x for x in inner if isinstance(x, dict))
        else:
            flat.append(o)
    return flat


def _same_variant(offer: dict, product_sku: str | None) -> bool:
    """A product-level GTIN describes the product-level sku; offers of other skus are other variants."""
    osku = _text(offer.get("sku"))
    return not (osku and product_sku and osku != product_sku)


def candidates_from(products: list[dict], machine: bool = True) -> list[OfferCandidate]:
    cands: list[OfferCandidate] = []
    for i, prod in enumerate(products):
        parent = prod.get("_parent") or {}
        title = _text(prod.get("name")) or _text(parent.get("name"))
        p_gtin = _gtin(prod) or _gtin(parent)
        p_sku = _text(prod.get("sku")) or _text(parent.get("sku"))
        for o in _offers_of(prod):
            types = _types(o)
            price = parse_price(o.get("price"), machine=machine)
            spec_cur, spec_strike, spec_rrp = _price_specs(o, machine)
            if price is None:
                price = spec_cur
            if price is None and "AggregateOffer" in types:
                lo = parse_price(o.get("lowPrice"), machine=machine)
                hi = parse_price(o.get("highPrice"), machine=machine)
                if lo is not None and (hi is None or hi == lo or str(o.get("offerCount", "")) == "1"):
                    price = lo
            currency = _text(o.get("priceCurrency"))
            if currency is None:
                spec = o.get("priceSpecification")
                spec = _first(spec) if isinstance(spec, list) else spec
                if isinstance(spec, dict):
                    currency = _text(spec.get("priceCurrency"))
            cands.append(
                OfferCandidate(
                    price=price,
                    currency=currency.upper() if currency else None,
                    availability=normalize_availability(o.get("availability") or prod.get("availability")),
                    url=_text(o.get("url")) or _text(prod.get("url")),
                    sku=_text(o.get("sku")) or p_sku,
                    gtin=_gtin(o) or (p_gtin if _same_variant(o, p_sku) else None),
                    title=title,
                    strike_price=spec_strike,
                    rrp_price=spec_rrp,
                    product_index=i,
                )
            )
    return cands


def _norm_url(u: str | None) -> str | None:
    """Path + query only: offer URLs in JSON-LD are often relative, and the host is the shop's anyway."""
    if not u:
        return None
    p = urlsplit(u)
    return urlunsplit(("", "", p.path.rstrip("/") or "/", p.query, ""))


def _one_variant(cands: list[OfferCandidate]) -> bool:
    return len({c.sku for c in cands}) == 1 and len({_norm_url(c.url) for c in cands if c.url}) <= 1


def select_offer(
    cands: list[OfferCandidate], page_urls: list[str], ean: str | None, tiebreak: str = "none"
) -> tuple[OfferCandidate | None, str | None]:
    """Pick the offer of *this* page; returns (offer, error).

    tiebreak (per shop): when the remaining offers are all the SAME variant but carry two prices
    (e.g. Notino: regular + promo-code price), take the max ("max") or min ("min"). Never applied
    across different variants.
    """
    priced = [c for c in cands if c.price is not None]
    if not priced:
        return None, "no priced offer" if cands else "no offer"
    if len({c.price for c in priced}) == 1:
        return priced[0], None

    def decide(hits: list[OfferCandidate]) -> OfferCandidate | None:
        if hits and len({c.price for c in hits}) == 1:
            return hits[0]
        if hits and tiebreak in ("max", "min") and _one_variant(hits):
            return (max if tiebreak == "max" else min)(hits, key=lambda c: c.price)
        return None

    if (hit := decide(priced)) is not None:
        return hit, None
    filters = []
    if ean:
        filters.append(lambda c: c.gtin == ean)
    norm_pages = {_norm_url(u) for u in page_urls}
    filters.append(lambda c: _norm_url(c.url) in norm_pages)
    filters.append(lambda c: bool(c.sku) and any(c.sku.lower() in u.lower() for u in page_urls))
    for f in filters:
        if (hit := decide([c for c in priced if f(c)])) is not None:
            return hit, None
    return None, f"ambiguous: {len({c.price for c in priced})} different prices"


def lenient_jsonld(scripts: list[str]) -> list:
    """Fallback for JSON-LD that extruct rejects (trailing commas, raw newlines, comments)."""
    out = []
    for raw in scripts:
        s = raw.strip()
        s = re.sub(r"^\s*<!\[CDATA\[|\]\]>\s*$", "", s)
        s = re.sub(r"^\s*<!--|-->\s*$", "", s)
        for attempt in (s, re.sub(r",\s*([}\]])", r"\1", s)):
            try:
                out.append(json.loads(attempt, strict=False))
                break
            except json.JSONDecodeError:
                continue
        else:
            log.debug("unparseable JSON-LD block skipped")
    return out


def extract(html: str, url: str, ld_scripts: list[str]) -> tuple[list, list]:
    """(json-ld items, microdata items)."""
    jsonld: list = []
    micro: list = []
    try:
        jsonld = extruct.extract(html, base_url=url, syntaxes=["json-ld"], uniform=True, errors="strict")["json-ld"]
    except Exception:
        jsonld = lenient_jsonld(ld_scripts)
    if not jsonld and ld_scripts:
        jsonld = lenient_jsonld(ld_scripts)
    try:
        micro = extruct.extract(html, base_url=url, syntaxes=["microdata"], uniform=True, errors="ignore")["microdata"]
    except Exception as e:
        log.debug("microdata extraction failed: %s", e)
    return jsonld, micro


def from_items(items: list, page_urls: list[str], ean: str | None, machine: bool = True,
               tiebreak: str = "none") -> StructuredResult:
    products = find_products(items)
    res = StructuredResult(n_products=len(products))
    if not products:
        res.error = "no Product"
        return res
    cands = candidates_from(products, machine)
    offer, err = select_offer(cands, page_urls, ean, tiebreak)
    if offer is None:
        res.error = err
        first = products[0]
        res.title = _text(first.get("name"))
        res.gtin = _gtin(first)
        avail = {c.availability for c in cands if c.price is not None}
        if len(avail) == 1:  # e.g. regular + promo-code offer of the same variant
            res.availability = avail.pop()
        gtins = {c.gtin for c in cands if c.gtin}
        if res.gtin is None and len(gtins) == 1:
            res.gtin = gtins.pop()
        return res
    res.price = offer.price
    res.currency = offer.currency
    res.availability = offer.availability
    res.sku = offer.sku
    res.gtin = offer.gtin
    res.title = offer.title
    res.strike_price = offer.strike_price
    res.rrp_price = offer.rrp_price
    return res
