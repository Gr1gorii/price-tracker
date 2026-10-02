"""Loading and validating config/settings.yaml, config/shops.yaml, config/products.csv."""

from __future__ import annotations

import csv
import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from bfp import __version__

CATEGORIES = ("electronics", "appliances", "beauty", "toys")


class Paths:
    """All filesystem locations, relative to the project root (BFP_HOME or cwd)."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root or os.environ.get("BFP_HOME") or Path.cwd()).resolve()
        self.config = self.root / "config"
        self.data = self.root / "data"
        self.observations = self.data / "observations"
        self.health = self.data / "health"
        self.state = self.data / "state"
        self.failed_html = self.data / "failed_html"
        self.candidates = self.root / "candidates"
        self.reports = self.root / "reports"


class ComplianceSettings(BaseModel):
    window_days: int = 30
    min_history_days: int = 25
    reference_tolerance: float = 0.01
    bump_threshold: float = 0.10
    episode_prev_tolerance: float = 0.01
    in_stock_only: bool = True


class Settings(BaseModel):
    contact_email: str = ""
    user_agent: str = "BFPriceTracker/{version} (+mailto:{email}; studio prezzi Black Friday)"
    timezone: str = "Europe/Rome"
    slots: list[int] = [8, 20]
    slot_window_minutes: int = 210
    start_date: str = "2026-10-10"
    end_date: str | None = "2026-12-01"  # last day with scheduled slots (inclusive)
    min_interval_s: float = 5.0
    max_crawl_delay_s: float = 30.0
    timeout_s: float = 30.0
    block_threshold: int = 3
    alert_threshold: float = 0.80
    max_failed_html_per_shop: int = 10
    max_response_mb: int = 15
    compliance: ComplianceSettings = ComplianceSettings()

    @field_validator("contact_email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = os.environ.get("BFP_CONTACT_EMAIL") or v
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v or ""):
            raise ValueError(
                "contact e-mail missing: set BFP_CONTACT_EMAIL (local: .env file in the project root; "
                "GitHub: repository secret) — it goes into the User-Agent"
            )
        return v

    @field_validator("min_interval_s")
    @classmethod
    def _interval(cls, v: float) -> float:
        if v < 5.0:
            raise ValueError("min_interval_s must be >= 5 seconds")
        return v

    @property
    def ua(self) -> str:
        return self.user_agent.format(version=__version__, email=self.contact_email)

    @property
    def ua_token(self) -> str:
        """Product token used to pick the robots.txt group, e.g. 'BFPriceTracker'."""
        return self.ua.split("/", 1)[0].strip()


class Selectors(BaseModel):
    """CSS selectors; append '@attr' to read an attribute instead of text, e.g. 'meta[itemprop=price]@content'."""

    price: str | None = None
    prev_price: str | None = None
    rrp_price: str | None = None  # "prezzo consigliato" — NOT a previous price
    low30_price: str | None = None
    discount_pct: str | None = None
    availability: str | None = None
    out_of_stock: str | None = None  # element present => OutOfStock
    title: str | None = None


class Low30Api(BaseModel):
    """A shop's own public endpoint that its product page calls for the '30-day lowest' value.

    Called (rate-limited, robots-checked) only when the page shows a discount.
    """

    url: str  # template with {id}
    id_regex: str  # regex on the product page HTML; group 1 = id
    price_path: str  # dotted path in the JSON response, e.g. lowestPrice.price
    days_path: str | None = None  # if set, the value there must be 30
    only_when_discount: bool = True

    @field_validator("id_regex")
    @classmethod
    def _rx(cls, v: str) -> str:
        if re.compile(v).groups < 1:
            raise ValueError("id_regex needs a capturing group")
        return v


class Shop(BaseModel):
    name: str
    domain: str
    enabled: bool = False
    render: bool = False
    runner: Literal["actions", "local"] = "actions"
    notes: str = ""
    currency: str = "EUR"
    product_url_pattern: str | None = None
    wait_for: str | None = None
    min_interval_s: float | None = None
    selectors: Selectors = Field(default_factory=Selectors)
    low30_api: Low30Api | None = None
    text_discount_fallback: bool = False  # 'X € -N% Y €' next to the price, consistency-checked

    @field_validator("domain")
    @classmethod
    def _domain(cls, v: str) -> str:
        v = v.strip().lower()
        if "://" in v:
            v = urlsplit(v).netloc
        return v.rstrip("/")

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", v):
            raise ValueError("shop name must be lowercase [a-z0-9_-]")
        return v

    @field_validator("product_url_pattern")
    @classmethod
    def _pattern(cls, v: str | None) -> str | None:
        if v:
            re.compile(v)
        return v

    @property
    def base_url(self) -> str:
        return f"https://{self.domain}"

    def owns(self, url: str) -> bool:
        host = urlsplit(url).netloc.lower()
        return _bare(host) == _bare(self.domain)


class ShopsFile(BaseModel):
    shops: list[Shop]

    @model_validator(mode="after")
    def _unique(self) -> "ShopsFile":
        names = [s.name for s in self.shops]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate shop names: {sorted(dupes)}")
        return self


class Product(BaseModel):
    shop: str
    url: str
    category: str
    ean: str | None = None

    @field_validator("category")
    @classmethod
    def _cat(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in CATEGORIES:
            raise ValueError(f"category must be one of {CATEGORIES}, got {v!r}")
        return v

    @field_validator("ean")
    @classmethod
    def _ean(cls, v: str | None) -> str | None:
        v = re.sub(r"\D", "", v or "")
        if not v:
            return None
        if len(v) not in (8, 12, 13, 14):
            raise ValueError(f"EAN/GTIN must have 8/12/13/14 digits, got {v!r}")
        return v

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        if urlsplit(v).scheme not in ("http", "https"):
            raise ValueError(f"not an http(s) URL: {v!r}")
        return v


def _bare(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


class Config(BaseModel):
    settings: Settings
    shops: dict[str, Shop]
    products: list[Product]
    warnings: list[str] = []

    def products_for(self, shop: str) -> list[Product]:
        return [p for p in self.products if p.shop == shop]


def load_dotenv(paths: Paths) -> None:
    """KEY=VALUE lines from <root>/.env into the environment (existing variables win). Not committed."""
    env = paths.root / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def load_settings(paths: Paths) -> Settings:
    load_dotenv(paths)
    with open(paths.config / "settings.yaml", encoding="utf-8") as f:
        return Settings.model_validate(yaml.safe_load(f) or {})


def load_shops(paths: Paths) -> dict[str, Shop]:
    with open(paths.config / "shops.yaml", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return {s.name: s for s in ShopsFile.model_validate({"shops": raw.get("shops") or []}).shops}


def load_products(paths: Paths, shops: dict[str, Shop]) -> tuple[list[Product], list[str]]:
    """Read products.csv; invalid rows raise, soft problems (duplicates) become warnings."""
    errors: list[str] = []
    warnings: list[str] = []
    products: list[Product] = []
    seen: set[str] = set()
    path = paths.config / "products.csv"
    if not path.exists():
        return [], [f"{path} not found"]
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = {"shop", "url", "category"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"products.csv: missing columns {sorted(missing)}")
        for lineno, row in enumerate(reader, start=2):
            if not any((v or "").strip() for v in row.values()):
                continue
            try:
                p = Product.model_validate({k: (v or "").strip() for k, v in row.items() if k})
            except Exception as e:  # pydantic ValidationError
                errors.append(f"products.csv:{lineno}: {e}")
                continue
            if p.shop not in shops:
                errors.append(f"products.csv:{lineno}: unknown shop {p.shop!r}")
                continue
            if not shops[p.shop].owns(p.url):
                errors.append(f"products.csv:{lineno}: URL host does not match shop domain {shops[p.shop].domain}")
                continue
            if p.url in seen:
                warnings.append(f"products.csv:{lineno}: duplicate URL skipped")
                continue
            seen.add(p.url)
            products.append(p)
    if errors:
        raise ValueError("\n".join(errors))
    return products, warnings


def load_config(paths: Paths | None = None) -> Config:
    paths = paths or Paths()
    settings = load_settings(paths)
    shops = load_shops(paths)
    products, warnings = load_products(paths, shops)
    return Config(settings=settings, shops=shops, products=products, warnings=warnings)
