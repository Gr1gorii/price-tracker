"""Optional per-shop extra request: the shop's own '30-day lowest price' endpoint (shops.yaml: low30_api)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal

from bfp.config import Shop
from bfp.http import Fetcher
from bfp.parse.prices import parse_price
from bfp.robots import RobotsCache


@dataclass
class ApiLow30:
    value: Decimal | None = None
    error: str | None = None
    blocked: bool = False
    requested: bool = False


def _get(data, path: str):
    for key in path.split("."):
        if isinstance(data, dict):
            data = data.get(key)
        elif isinstance(data, list) and key.isdigit() and int(key) < len(data):
            data = data[int(key)]
        else:
            return None
    return data


async def low30_from_api(fetcher: Fetcher, robots: RobotsCache, shop: Shop, html: str) -> ApiLow30:
    api = shop.low30_api
    out = ApiLow30()
    if api is None:
        return out
    m = re.search(api.id_regex, html)
    if not m:
        out.error = "low30_api: product id not found on page"
        return out
    url = api.url.format(id=m.group(1))
    if not await robots.allowed(url):
        out.error = "low30_api: disallowed by robots.txt"
        return out
    out.requested = True
    res = await fetcher.get(url, allow=robots.allowed)
    if res.blocked:
        out.blocked, out.error = True, "low30_api: block/challenge"
        return out
    if not res.ok or res.content is None:
        out.error = f"low30_api: HTTP {res.status} {res.error or ''}".strip()
        return out
    try:
        data = json.loads(res.content)
    except ValueError:
        out.error = "low30_api: not JSON"
        return out
    if not data:  # e.g. [] when the shop publishes no lowest price for this product
        return out
    if api.days_path is not None:
        days = _get(data, api.days_path)
        if str(days) != "30":
            out.error = f"low30_api: window is {days} days, not 30"
            return out
    out.value = parse_price(_get(data, api.price_path))
    if out.value is None:
        out.error = "low30_api: no price at " + api.price_path
    return out
