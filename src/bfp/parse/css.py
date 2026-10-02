"""Shop-specific CSS selectors (selectolax/lexbor). 'selector@attr' reads an attribute."""

from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser, LexborNode


def _split(selector: str) -> tuple[str, str | None]:
    m = re.fullmatch(r"(.+?)@([\w:-]+)", selector.strip())
    if m and not m.group(1).endswith(("[", "=")):
        return m.group(1), m.group(2)
    return selector, None


def node_value(node: LexborNode, attr: str | None) -> str | None:
    if attr:
        v = node.attributes.get(attr)
    else:
        v = node.attributes.get("content") if node.tag == "meta" else node.text(separator=" ", strip=True)
    v = re.sub(r"\s+", " ", v or "").strip()
    return v or None


def select_value(tree: LexborHTMLParser, selector: str | None) -> str | None:
    """First non-empty value among nodes matching selector (document order)."""
    if not selector:
        return None
    css, attr = _split(selector)
    for node in tree.css(css):
        v = node_value(node, attr)
        if v:
            return v
    return None


def exists(tree: LexborHTMLParser, selector: str | None) -> bool:
    if not selector:
        return False
    css, _ = _split(selector)
    return tree.css_first(css) is not None
