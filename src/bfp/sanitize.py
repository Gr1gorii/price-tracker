"""Strip reviews, user content and personal data from HTML before it is stored anywhere.

Used for failed-parse snapshots and test fixtures. Best-effort and conservative: product
markup is kept, anything that looks like reviews/ratings/comments/Q&A/forms is dropped
(unless it wraps the product's own title/price), e-mail addresses and tel: links are masked.
"""

from __future__ import annotations

import json
import re

import lxml.html
from lxml import etree

USER_CONTENT = re.compile(
    r"review|recensi|rating|stars?\b|comment|feedback|valutazion|opinion|testimonial|"
    r"question|domand|q-?and-?a|\bqa\b|ugc|bazaarvoice|trustpilot|yotpo|feefo|reevoo|user-?content",
    re.I,
)
DROP_TAGS = ("noscript", "iframe", "input", "textarea", "select", "button", "object", "embed")
UNWRAP_TAGS = ("form",)  # add-to-cart forms often wrap the price block: keep content, drop the form
LD_DROP_KEYS = {"review", "reviews", "aggregateRating", "comment", "author", "creator", "contributor"}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
KEEP_XPATH = './/h1 | .//*[@itemprop="offers" or @itemprop="price" or @itemprop="lowPrice"]'
REVIEW_TYPES = re.compile(r"schema\.org/(Review|Rating|AggregateRating|Comment|Question|Answer|Person)", re.I)


def _clean_ld(obj):
    if isinstance(obj, dict):
        return {k: _clean_ld(v) for k, v in obj.items() if k not in LD_DROP_KEYS}
    if isinstance(obj, list):
        return [_clean_ld(x) for x in obj]
    return obj


def sanitize_html(html: str) -> str:
    try:
        doc = lxml.html.document_fromstring(html)
    except (etree.ParserError, ValueError):
        return ""
    for el in list(doc.iter(etree.Comment)):
        el.drop_tree()
    for el in list(doc.iter("script")):
        if (el.get("type") or "").lower() == "application/ld+json":
            try:
                el.text = _mask(json.dumps(_clean_ld(json.loads(el.text or "", strict=False)), ensure_ascii=False))
                continue
            except json.JSONDecodeError:
                pass
        el.drop_tree()
    for el in list(doc.iter(*DROP_TAGS)):
        el.drop_tree()
    for el in list(doc.iter(*UNWRAP_TAGS)):
        el.drop_tag()
    for el in list(doc.iter()):
        if not isinstance(el.tag, str):
            continue
        marker = " ".join(
            el.get(a, "") for a in ("id", "class", "itemprop", "data-testid", "data-component", "data-section", "aria-label")
        )
        if (marker.strip() and USER_CONTENT.search(marker)) or REVIEW_TYPES.search(el.get("itemtype", "")):
            if el.getparent() is not None and not el.xpath(KEEP_XPATH):
                el.drop_tree()
    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in list(el.attrib):
            if attr.startswith("on") or attr in ("data-user", "data-email", "value"):
                del el.attrib[attr]
        if el.text and el.tag not in ("script",):
            el.text = _mask(el.text)
        if el.tail:
            el.tail = _mask(el.tail)
        for attr, v in list(el.attrib.items()):
            if attr == "href" and v.startswith("tel:"):
                el.set(attr, "tel:[phone]")
            elif "@" in v:  # e-mails anywhere, e.g. shops echoing our User-Agent into data-user-agent
                el.set(attr, _mask(v))
    return lxml.html.tostring(doc, encoding="unicode", doctype="<!DOCTYPE html>")


def _mask(text: str) -> str:
    return EMAIL.sub("[email]", text)
