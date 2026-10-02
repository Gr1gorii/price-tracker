"""robots.txt: urllib.robotparser AND a strict wildcard-aware matcher (fail-closed).

urllib.robotparser only does prefix matching (`filename.startswith(path)`), so rules like
`Disallow: /*?sort=` or `Disallow: /*.json$` are silently ignored by it. We fetch a URL only
if BOTH robotparser and the strict (Google-style: `*`, `$`, longest match wins) matcher allow it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit
from urllib.robotparser import RobotFileParser

from bfp.http import Fetcher, origin


@dataclass
class Rule:
    allow: bool
    pattern: str
    regex: re.Pattern

    @classmethod
    def build(cls, allow: bool, pattern: str) -> "Rule":
        pat = unquote(pattern)
        anchored = pat.endswith("$")
        if anchored:
            pat = pat[:-1]
        rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in pat)
        return cls(allow, pattern, re.compile("^" + rx + ("$" if anchored else "")))


def parse_groups(text: str) -> list[tuple[list[str], list[Rule]]]:
    groups: list[tuple[list[str], list[Rule]]] = []
    agents: list[str] = []
    rules: list[Rule] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, val = (s.strip() for s in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(val.lower())
        elif key in ("allow", "disallow") and agents:
            if val:
                rules.append(Rule.build(key == "allow", val))
            else:
                rules.append(Rule(True, "", re.compile("^$")))  # empty Disallow = allow all
    if agents:
        groups.append((agents, rules))
    return groups


def strict_rules(text: str, ua_token: str) -> list[Rule]:
    """Rules of all groups naming our token (robotparser-style substring match), else of '*'."""
    token = ua_token.lower()
    groups = parse_groups(text)
    specific = [r for agents, rules in groups if any(a != "*" and a in token for a in agents) for r in rules]
    if specific:
        return specific
    return [r for agents, rules in groups if "*" in agents for r in rules]


def strict_allowed(rules: list[Rule], url: str) -> bool:
    p = urlsplit(url)
    path = unquote(p.path or "/") + (f"?{unquote(p.query)}" if p.query else "")
    best: Rule | None = None
    for r in rules:
        if not r.pattern:
            continue
        if r.regex.match(path):
            if best is None or len(r.pattern) > len(best.pattern) or (
                len(r.pattern) == len(best.pattern) and r.allow and not best.allow
            ):
                best = r
    return True if best is None else best.allow


@dataclass
class Robots:
    origin: str
    status: str  # parsed | allow_all | disallow_all | unavailable | blocked
    http_status: int | None = None
    text: str = ""
    ua_token: str = ""
    _rp: RobotFileParser | None = None
    _rules: list[Rule] = field(default_factory=list)

    @classmethod
    def from_text(cls, origin_: str, text: str, ua_token: str, http_status: int | None = 200) -> "Robots":
        rp = RobotFileParser()
        rp.set_url(origin_ + "/robots.txt")
        rp.parse(text.splitlines())
        return cls(origin_, "parsed", http_status, text, ua_token, rp, strict_rules(text, ua_token))

    def allowed(self, url: str) -> bool:
        if self.status == "allow_all":
            return True
        if self.status != "parsed" or self._rp is None:
            return False
        return self._rp.can_fetch(self.ua_token, url) and strict_allowed(self._rules, url)

    def explain(self, url: str) -> str:
        """Which checker said what — for `bfp probe`/`bfp sitemap` diagnostics."""
        if self.status != "parsed" or self._rp is None:
            return self.status
        return f"robotparser={self._rp.can_fetch(self.ua_token, url)} strict={strict_allowed(self._rules, url)}"

    @property
    def crawl_delay(self) -> float | None:
        if self._rp is None:
            return None
        d = self._rp.crawl_delay(self.ua_token)
        rr = self._rp.request_rate(self.ua_token)
        delays = [float(d)] if d else []
        if rr and rr.requests:
            delays.append(rr.seconds / rr.requests)
        return max(delays) if delays else None

    @property
    def sitemaps(self) -> list[str]:
        return list(self._rp.site_maps() or []) if self._rp else []


class RobotsCache:
    """One robots.txt per origin per run, fetched through the shop's rate-limited Fetcher."""

    def __init__(self, fetcher: Fetcher, ua_token: str):
        self.fetcher = fetcher
        self.ua_token = ua_token
        self._cache: dict[str, Robots] = {}

    async def get(self, url: str) -> Robots:
        o = origin(url)
        if o not in self._cache:
            self._cache[o] = await self._fetch(o)
        return self._cache[o]

    async def allowed(self, url: str) -> bool:
        return (await self.get(url)).allowed(url)

    async def _fetch(self, o: str) -> Robots:
        # Redirects of robots.txt itself are followed (common: http->https, bare->www).
        res = await self.fetcher.get(o + "/robots.txt")
        if res.blocked:
            return Robots(o, "blocked", res.status)
        if res.error or res.status is None:
            return Robots(o, "unavailable", res.status)
        if res.status == 200:
            return Robots.from_text(o, res.text or "", self.ua_token)
        if res.status in (401, 403):
            return Robots(o, "disallow_all", res.status)
        if 400 <= res.status < 500:
            return Robots(o, "allow_all", res.status)
        return Robots(o, "unavailable", res.status)  # 5xx: fail closed for this run
