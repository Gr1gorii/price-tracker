"""Price / percentage / availability normalisation (Italian formats included)."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

MAX_PRICE = Decimal("100000")
_NUM = re.compile(r"\d{1,3}(?:[.,'’   ]\d{3})+(?:[.,]\d{1,2})?(?!\d)|\d+(?:[.,]\d{1,3})?")


def parse_price(value: object, machine: bool = False) -> Decimal | None:
    """'1.299,00 €' -> 1299.00, '1299.00' -> 1299.00, '€ 1.299' -> 1299.00.

    machine=True (JSON-LD): a lone '.' is always a decimal point ("12.990" = 12.99).
    machine=False (displayed text): a lone '.' followed by exactly 3 digits is a thousands
    separator, as in Italian "1.299".
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return _check(Decimal(str(value)))
    s = str(value).replace("−", "-")
    m = _NUM.search(s)
    if not m:
        return None
    num = re.sub(r"[\s'’  ]", "", m.group(0)).rstrip(".,")
    if not num:
        return None
    if "," in num and "." in num:
        dec = "," if num.rfind(",") > num.rfind(".") else "."
        thou = "." if dec == "," else ","
        num = num.replace(thou, "").replace(dec, ".")
    elif "," in num:
        parts = num.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            num = num.replace(",", ".")
        elif len(parts) == 2 and len(parts[1]) == 3 and machine:
            num = num.replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "." in num:
        parts = num.split(".")
        if not machine and (len(parts) > 2 or len(parts[-1]) == 3):
            num = num.replace(".", "")
        elif len(parts) > 2:
            num = num.replace(".", "")
    try:
        return _check(Decimal(num))
    except InvalidOperation:
        return None


def _check(d: Decimal) -> Decimal | None:
    if not d.is_finite() or d <= 0 or d >= MAX_PRICE:
        return None
    return d.quantize(Decimal("0.01"))


def parse_pct(value: object) -> Decimal | None:
    """'-20%' / 'Sconto 20 %' / '−12,5%' -> 20 / 20 / 12.5 (absolute value, 0 < pct < 100)."""
    if value is None:
        return None
    s = str(value).replace("−", "-")
    m = re.search(r"(\d{1,2}(?:[.,]\d{1,2})?)\s*%", s)
    if not m:
        return None
    d = Decimal(m.group(1).replace(",", "."))
    if not 0 < d < 100:
        return None
    return d.quantize(Decimal("0.01"))


_AVAIL_SCHEMA = {
    "instock": "InStock",
    "outofstock": "OutOfStock",
    "soldout": "SoldOut",
    "discontinued": "Discontinued",
    "preorder": "PreOrder",
    "presale": "PreSale",
    "backorder": "BackOrder",
    "limitedavailability": "LimitedAvailability",
    "onlineonly": "OnlineOnly",
    "instoreonly": "InStoreOnly",
    "madetoorder": "MadeToOrder",
}
NOT_PURCHASABLE = {"OutOfStock", "SoldOut", "Discontinued"}


def normalize_availability(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
        if value is None:
            return None
    s = str(value).strip()
    key = re.sub(r"[^a-z]", "", s.rsplit("/", 1)[-1].lower())
    if key in _AVAIL_SCHEMA:
        return _AVAIL_SCHEMA[key]
    low = s.lower()
    if re.search(r"non\s+disponibil|esaurit|terminat|sold\s*out|out\s+of\s+stock|non\s+acquistabil", low):
        return "OutOfStock"
    if re.search(r"preordin|pre-ordin|prenota", low):
        return "PreOrder"
    if re.search(r"disponibil|in\s+stock|in\s+magazzino|spedizione\s+(immediata|in)", low):
        return "InStock"
    return None


def is_purchasable(availability: str | None) -> bool:
    return availability not in NOT_PURCHASABLE
