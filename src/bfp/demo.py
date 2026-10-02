"""Synthetic price histories (clearly fake) to exercise the checker and the charts."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

TZ = ZoneInfo("Europe/Rome")
CATS = ["electronics", "appliances", "beauty", "toys"]
# scenario -> weight
SCENARIOS = {"honest": 4, "dip_before": 3, "bump": 2, "false_low30": 2, "progressive": 2, "no_discount": 2, "late_listing": 1}


def _slots(start: date, end: date):
    d = start
    while d <= end:
        for h in (8, 20):
            local = datetime(d.year, d.month, d.day, h, 7, tzinfo=TZ)
            yield local, f"{d.isoformat()}T{h:02d}"
        d += timedelta(days=1)


def synthetic_history(seed: int = 7, n_products: int = 48) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    names = list(SCENARIOS)
    weights = np.array(list(SCENARIOS.values()), dtype=float)
    rows = []
    bf_start = date(2026, 11, 20)
    for i in range(n_products):
        scen = names[rng.choice(len(names), p=weights / weights.sum())]
        cat = CATS[i % 4]
        base = float(rng.choice([29.9, 49.9, 79.0, 129.0, 249.0, 499.0, 899.0]))
        start = date(2026, 11, 10) if scen == "late_listing" else date(2026, 10, 10)
        url = f"https://demo.invalid/{cat}/{scen}-{i}"
        dip_day = date(2026, 11, int(rng.integers(1, 12)))
        k = float(rng.uniform(0.6, 0.92))   # BF price factor
        dip = float(rng.uniform(0.75, 0.95))
        for local, slot in _slots(start, date(2026, 11, 30)):
            d = local.date()
            price, prev, low30, pct, avail = base, None, None, None, "InStock"
            if scen == "dip_before" and dip_day <= d < dip_day + timedelta(days=3):
                price = round(base * max(dip, k + 0.02), 2)
            if scen == "bump" and date(2026, 11, 2) <= d < bf_start:
                price = round(base * 1.2, 2)
            if d >= bf_start and scen != "no_discount":
                if scen in ("honest", "dip_before", "late_listing"):
                    price, prev = round(base * k, 2), base
                elif scen == "bump":
                    price, prev = round(base * 1.2 * k, 2), round(base * 1.2, 2)
                elif scen == "false_low30":
                    price, prev, low30 = round(base * k, 2), base, base
                elif scen == "progressive":
                    price = round(base * (min(0.95, k + 0.1) if d < date(2026, 11, 25) else k), 2)
                    prev = base
                pct = round((1 - price / prev) * 100) if prev else None
            if scen == "false_low30" and date(2026, 11, 3) <= d < date(2026, 11, 6):
                price = round(base * max(dip, k + 0.02), 2)
            if rng.random() < 0.03:
                continue  # missed observation
            rows.append({
                "shop": f"demo-{i % 3}", "url": url, "category": cat, "ean": None, "title": f"DEMO {scen} #{i}",
                "ts": local.astimezone(ZoneInfo("UTC")), "slot": slot, "price": price, "currency": "EUR",
                "displayed_prev_price": prev, "displayed_low30_price": low30, "displayed_discount_pct": pct,
                "availability": avail,
            })
    return pd.DataFrame(rows)
