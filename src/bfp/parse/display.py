"""What the shop DISPLAYS: previous/strikethrough price, '30-day lowest price', discount %.

Selectors from shops.yaml win. The 30-day-lowest value additionally has a generic
Italian text fallback, because art. 17-bis made the phrase near-standard.
"""

from __future__ import annotations

import re
from decimal import Decimal

from bfp.parse.prices import parse_pct, parse_price

_LOW30_PHRASE = re.compile(
    r"(?:prezzo|costo)\s+(?:pi[uù]|pi&ugrave;)\s+basso"
    r"|prezzo\s+minimo"
    r"|miglior\s+prezzo"
    r"|prezzo\s+precedente\s+(?:pi[uù]\s+basso|minimo)"
    r"|lowest\s+price",
    re.I,
)
# Omnibus wording that refers to the 30-day reference by itself (no "30 giorni" next to it)
_SELF_CONTAINED = re.compile(r"ultimo\s+prezzo\s+pi[uù]\s+basso|prezzo\s+pi[uù]\s+basso\s+recente", re.I)
_THIRTY = re.compile(r"(?:ultimi|precedenti|negli|nei|degli|dei|last|previous)?\s*\(?\s*(?:30|trenta)\s*(?:giorni|gg\.?|days)\s*\)?", re.I)
_PRICE_IN_TEXT = re.compile(
    r"(?:€|eur)\s*(\d{1,3}(?:[.\s]\d{3})*(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    r"|(\d{1,3}(?:[.\s]\d{3})*,\d{1,2}|\d+[.,]\d{2})\s*(?:€|eur)?"
    r"|(\d+)\s*(?:€|eur)",
    re.I,
)


def low30_from_text(text: str) -> Decimal | None:
    """'Prezzo più basso degli ultimi 30 giorni: 1.199,00 €' -> 1199.00."""
    for m in _LOW30_PHRASE.finditer(text):
        window = text[m.end(): m.end() + 160]
        self_contained = _SELF_CONTAINED.search(text, max(0, m.start() - 10), m.end())
        if not self_contained and not _THIRTY.search(window[:90]):
            continue
        cleaned = _THIRTY.sub(" ", window, count=1)
        pm = _PRICE_IN_TEXT.search(cleaned[:110])
        if pm:
            value = parse_price(next(g for g in pm.groups() if g))
            if value is not None:
                return value
    return None


_PCT_IN_TEXT = re.compile(r"[-\u2212]\s?\d{1,2}(?:[.,]\d{1,2})?\s?%")


def _it(price: Decimal) -> list[str]:
    s = f"{price:.2f}".replace(".", ",")
    whole, cents = s.split(",")
    grouped = f"{int(whole):,}".replace(",", ".") + "," + cents
    return list(dict.fromkeys([s, grouped]))


def discount_from_text(text: str, price: Decimal, exclude: set[Decimal] | None = None) -> tuple[Decimal, Decimal] | None:
    """'29,90 € -33% 19,90 €' around the actual price -> (29.90, 33).

    Accepted only if consistent: |(1 − price/prev)·100 − pct| ≤ 1.5. Opt-in per shop
    (shops.yaml: text_discount_fallback) because some shops put an RRP in the same pattern.
    """
    exclude = exclude or set()
    for needle in _it(price):
        start = 0
        for _ in range(6):
            i = text.find(needle, start)
            if i < 0:
                break
            start = i + len(needle)
            window = text[max(0, i - 90): i + len(needle) + 90]
            pcts = [parse_pct(m.group(0)) for m in _PCT_IN_TEXT.finditer(window)]
            prevs = []
            for m in _PRICE_IN_TEXT.finditer(window):
                v = parse_price(next(g for g in m.groups() if g))
                if v is not None and v > price and v not in exclude:
                    prevs.append(v)
            for prev in prevs:
                implied = (1 - price / prev) * 100
                for pct in pcts:
                    if pct is not None and abs(implied - pct) <= Decimal("1.5"):
                        return prev, pct
    return None


def visible_text(html_tree) -> str:
    """Visible-ish text of a lexbor tree (scripts/styles removed), whitespace collapsed."""
    clone = html_tree.clone() if hasattr(html_tree, "clone") else html_tree
    clone.strip_tags(["script", "style", "noscript", "template", "svg"])
    body = clone.body or clone.root
    if body is None:
        return ""
    return re.sub(r"\s+", " ", body.text(separator=" ", strip=True))
