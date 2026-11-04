"""Pre-registered Black Friday hypotheses (docs/hypotheses.md, written 2026-11-04).

Windows and thresholds below are the ones fixed in docs/hypotheses.md before any Black Friday
data existed. Do not tune them after looking at BF data; report any change as a deviation.

Frozen inputs (written once by `bfp hypotheses --freeze`, then committed):
  config/frozen/lego_sets.csv      set, shop, url   LEGO sets sold by at least two shops
  config/frozen/tigota_bumped.csv  url, base_median, after_median, bumped
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from bfp import compliance
from bfp.config import ComplianceSettings, Paths

BASELINE = (date(2026, 10, 5), date(2026, 11, 15))
BF = (date(2026, 11, 20), date(2026, 12, 1))
H3_START = (date(2026, 11, 20), date(2026, 11, 29))   # 30-day window still holds 30 Oct
EQUAL_TOL = 0.005        # "equal" prices: within 0.5 %
CUT_MIN = 0.03           # H2: a cut is a drop of at least 3 % from the previous slot
FOLLOW_SLOTS = 2         # H2: the other chain must reach the same price within 2 slots
LEGO_PAIR = ("euronics", "unieuro")
LEGO_SOURCES = ("euronics", "unieuro", "toyscenter")
PUBLISHING = ("notino", "tigota", "toyscenter", "unieuro")
NON_PUBLISHING = ("lookfantastic", "euronics")
SET_RE = r"\b(\d{5})\b"

PENDING = "pending (no BF data)"
DESCRIPTIVE = "descriptive only"


def frozen_dir(paths: Paths) -> Path:
    return paths.config / "frozen"


# ---------------------------------------------------------------- frozen groups

def lego_sets(df: pd.DataFrame) -> pd.DataFrame:
    """LEGO sets (five-digit number in a title containing LEGO) sold by at least two shops."""
    title = df["title"].fillna("").str.upper()
    t = df[df["shop"].isin(LEGO_SOURCES) & title.str.contains("LEGO")]
    t = t.assign(set=title[t.index].str.extract(SET_RE)[0]).dropna(subset=["set"])
    t = t[["set", "shop", "url"]].drop_duplicates()
    shops = t.groupby("set")["shop"].nunique()
    t = t[t["set"].isin(shops[shops >= 2].index)]
    return t.sort_values(["set", "shop", "url"]).reset_index(drop=True)


def tigota_bumped(df: pd.DataFrame) -> pd.DataFrame:
    """Tigotà products whose price rose more than 5 % when the October promotion ended."""
    t = df[df["shop"] == "tigota"]
    d = t["local_date"]
    base = t[(d >= date(2026, 10, 12)) & (d <= date(2026, 10, 21))].groupby("url")["price"].median()
    after = t[(d >= date(2026, 11, 2)) & (d <= date(2026, 11, 4))].groupby("url")["price"].median()
    out = pd.concat({"base_median": base, "after_median": after}, axis=1).dropna().reset_index()
    out["bumped"] = out["after_median"] > out["base_median"] * 1.05
    return out.sort_values("url").reset_index(drop=True)


def freeze(paths: Paths, df: pd.DataFrame) -> list[Path]:
    """Write the frozen groups once. Refuses to overwrite: they must not move after 2026-11-04."""
    out_dir = frozen_dir(paths)
    targets = {out_dir / "lego_sets.csv": lego_sets, out_dir / "tigota_bumped.csv": tigota_bumped}
    existing = [p for p in targets if p.exists()]
    if existing:
        raise FileExistsError(f"already frozen: {', '.join(p.name for p in existing)}")
    out_dir.mkdir(parents=True, exist_ok=True)
    for path, fn in targets.items():
        fn(df).to_csv(path, index=False)
    return list(targets)


def load_frozen(paths: Paths) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = frozen_dir(paths)
    sets = pd.read_csv(d / "lego_sets.csv", dtype={"set": str})
    bumped = pd.read_csv(d / "tigota_bumped.csv")
    return sets, bumped


# ---------------------------------------------------------------- helpers

def _in(d: pd.Series, window: tuple[date, date]) -> pd.Series:
    return (d >= window[0]) & (d <= window[1])


def _rome_date(ts: pd.Series) -> pd.Series:
    return pd.to_datetime(ts, utc=True).dt.tz_convert(compliance.TZ).dt.date


def _equal(a, b) -> bool:
    return bool(abs(a / b - 1) < EQUAL_TOL)


# ---------------------------------------------------------------- H1

def lego_daily(df: pd.DataFrame, sets: pd.DataFrame) -> pd.DataFrame:
    """One row per (set, Rome day) with both chains' median price and Unieuro's recommended price."""
    a, b = LEGO_PAIR
    m = df.merge(sets[sets["shop"].isin(LEGO_PAIR)][["url", "set"]], on="url")
    daily = (m.groupby(["set", "local_date", "shop"])
             .agg(price=("price", "median"), rrp=("displayed_rrp_price", "median")).reset_index())
    p = daily.pivot_table(index=["set", "local_date"], columns="shop", values="price")
    if a not in p or b not in p:
        return pd.DataFrame(columns=["set", "local_date", a, b, "rrp"])
    p = p.dropna(subset=[a, b])
    p["rrp"] = daily[daily["shop"] == b].set_index(["set", "local_date"])["rrp"].reindex(p.index)
    return p[[a, b, "rrp"]].reset_index()


def h1_stats(daily: pd.DataFrame, window: tuple[date, date]) -> dict:
    a, b = LEGO_PAIR
    w = daily[_in(daily["local_date"], window)]
    if w.empty:
        return {"set_days": 0, "equal_share": np.nan, "median_spread": np.nan, "below_rrp_of_equal": np.nan}
    lo, hi = w[[a, b]].min(axis=1), w[[a, b]].max(axis=1)
    spread = hi / lo - 1
    equal = spread < EQUAL_TOL
    below = equal & w["rrp"].notna() & (lo < w["rrp"] * (1 - EQUAL_TOL))
    return {
        "set_days": int(len(w)),
        "equal_share": float(equal.mean()),
        "median_spread": float(spread.median()),
        "below_rrp_of_equal": float(below.sum() / equal.sum()) if equal.any() else np.nan,
    }


def h1_verdict(base: dict, bf: dict) -> str:
    if not bf["set_days"]:
        return PENDING
    if bf["equal_share"] <= base["equal_share"] - 0.15 and bf["median_spread"] > 0.01:
        return "H1a supported (independent campaigns)"
    if bf["equal_share"] >= base["equal_share"] - 0.05 and bf["below_rrp_of_equal"] >= 0.5:
        return "H1b supported (matching)"
    return "inconclusive"


# ---------------------------------------------------------------- H2

def lego_slots(df: pd.DataFrame, sets: pd.DataFrame) -> pd.DataFrame:
    """Median price per (set, scheduled slot) for both chains; manual slots are dropped."""
    m = df[~df["slot"].str.endswith("m")].merge(sets[sets["shop"].isin(LEGO_PAIR)][["url", "set"]], on="url")
    s = m.groupby(["set", "slot", "shop"])["price"].median().unstack("shop")
    if not set(LEGO_PAIR) <= set(s.columns):
        return pd.DataFrame(columns=list(LEGO_PAIR))
    return s.dropna(subset=list(LEGO_PAIR)).sort_index()


def lego_cuts(slots: pd.DataFrame) -> pd.DataFrame:
    """Cuts of at least CUT_MIN while the other chain's price is unchanged.
    Unilateral cuts: followed | alone | open (fewer than FOLLOW_SLOTS slots of data left).
    Not counted as unilateral: matched_existing (the cut lands on the other chain's current price),
    same_slot (both cut to the same price in one slot), both (both cut, different prices)."""
    rows = []
    for set_, g in slots.groupby(level=0):
        g = g.droplevel(0)
        price = {s: g[s].to_numpy(dtype=float) for s in LEGO_PAIR}
        n = len(g)
        for i in range(1, n):
            ch = {s: price[s][i] / price[s][i - 1] - 1 for s in LEGO_PAIR}
            cut = {s: ch[s] <= -CUT_MIN for s in LEGO_PAIR}
            slot = g.index[i]
            if all(cut.values()):
                kind = "same_slot" if _equal(price[LEGO_PAIR[0]][i], price[LEGO_PAIR[1]][i]) else "both"
                rows.append((set_, slot, "both", kind))
                continue
            for x, y in (LEGO_PAIR, LEGO_PAIR[::-1]):
                if not cut[x] or abs(ch[y]) >= EQUAL_TOL:
                    continue
                if _equal(price[x][i], price[y][i]):
                    # x cut onto y's current price: x is the one matching, not a cut y could follow
                    rows.append((set_, slot, x, "matched_existing"))
                    continue
                later = range(i + 1, min(i + 1 + FOLLOW_SLOTS, n))
                followed = any(_equal(price[y][k], price[x][i]) and not _equal(price[y][k], price[y][i - 1])
                               for k in later)
                kind = "followed" if followed else ("open" if i + FOLLOW_SLOTS >= n else "alone")
                rows.append((set_, slot, x, kind))
    out = pd.DataFrame(rows, columns=["set", "slot", "leader", "kind"])
    out["local_date"] = pd.to_datetime(out["slot"].str[:10]).dt.date
    return out


def h2_stats(cuts: pd.DataFrame, window: tuple[date, date]) -> dict:
    w = cuts[_in(cuts["local_date"], window)] if len(cuts) else cuts
    k = w["kind"].value_counts() if len(w) else pd.Series(dtype=int)
    followed, alone = int(k.get("followed", 0)), int(k.get("alone", 0))
    return {"unilateral_cuts": followed + alone, "followed": followed,
            "follow_share": followed / (followed + alone) if followed + alone else np.nan,
            "same_slot": int(k.get("same_slot", 0)), "matched_existing": int(k.get("matched_existing", 0)),
            "open": int(k.get("open", 0))}


def h2_verdict(bf: dict, has_bf_data: bool) -> str:
    if not has_bf_data:
        return PENDING
    if bf["unilateral_cuts"] < 20:
        return DESCRIPTIVE + " (fewer than 20 cuts)"
    if bf["follow_share"] >= 0.15:
        return "supported"
    if bf["follow_share"] < 0.05:
        return "rejected"
    return "inconclusive"


# ---------------------------------------------------------------- episodes for H3 to H5

def bf_episodes(df: pd.DataFrame, cs: ComplianceSettings) -> pd.DataFrame:
    """Episodes with sufficient history that start inside the BF window."""
    eps = compliance.episodes(compliance.check(df, cs))
    if eps.empty:
        return eps
    eps = eps[eps["verdict"] != "insufficient_history"].copy()
    eps["start_date"] = _rome_date(eps["episode_start"])
    return eps[_in(eps["start_date"], BF)]


def h3_stats(eps: pd.DataFrame, bumped: pd.DataFrame) -> dict:
    t = eps[(eps["shop"] == "tigota") & _in(eps["start_date"], H3_START)] if len(eps) else eps
    t = t.merge(bumped[["url", "bumped"]], on="url") if len(t) else t.assign(bumped=pd.Series(dtype=bool))
    b, c = t[t["bumped"]], t[~t["bumped"]]
    return {"bumped_episodes": int(len(b)), "control_episodes": int(len(c)),
            "gap_bumped": float(b["discount_gap"].median()) if len(b) else np.nan,
            "gap_control": float(c["discount_gap"].median()) if len(c) else np.nan}


def h3_verdict(s: dict, has_bf_data: bool) -> str:
    if not has_bf_data:
        return PENDING
    if s["bumped_episodes"] < 10:
        return DESCRIPTIVE + " (fewer than 10 bumped episodes)"
    gb, gc = s["gap_bumped"], s["gap_control"]
    if gb < 0.01:
        return "rejected (compliant)"
    if gb >= 0.05 and (np.isnan(gc) or gc <= 0 or gb >= 2 * gc):
        return "supported"
    return "inconclusive"


def label_share(df: pd.DataFrame) -> pd.Series:
    """Per URL: share of baseline Rome days with at least one observation showing a discount."""
    w = df[_in(df["local_date"], BASELINE)]
    days = w.assign(disc=compliance.has_discount(w)).groupby(["url", "local_date"])["disc"].any()
    return days.groupby(level="url").mean()


def h4_stats(eps: pd.DataFrame, shares: pd.Series) -> pd.DataFrame:
    """Per shop: BF episodes and median honest discount for always-labelled vs rarely-labelled products."""
    cols = ["shop", "n_high", "n_low", "honest_high", "honest_low", "eligible", "holds"]
    if eps.empty:
        return pd.DataFrame(columns=cols)
    e = eps.assign(share=eps["url"].map(shares))
    e["group"] = np.select([e["share"] >= 0.5, e["share"] < 0.10], ["high", "low"], default="")
    rows = []
    for shop, g in e.groupby("shop"):
        hi, lo = g[g["group"] == "high"], g[g["group"] == "low"]
        mh = float(hi["honest_discount"].median()) if len(hi) else np.nan
        ml = float(lo["honest_discount"].median()) if len(lo) else np.nan
        eligible = len(hi) >= 20 and len(lo) >= 20
        rows.append((shop, len(hi), len(lo), mh, ml, eligible, bool(eligible and mh < ml)))
    return pd.DataFrame(rows, columns=cols)


def h4_verdict(per_shop: pd.DataFrame, has_bf_data: bool) -> str:
    if not has_bf_data:
        return PENDING
    eligible = int(per_shop["eligible"].sum()) if len(per_shop) else 0
    holds = int(per_shop["holds"].sum()) if len(per_shop) else 0
    if eligible < 3:
        return DESCRIPTIVE + f" ({eligible} eligible shops)"
    if holds >= 3:
        return "supported"
    if holds <= 1:
        return "rejected"
    return "inconclusive"


def h5_stats(eps: pd.DataFrame) -> dict:
    def share(shops):
        g = eps[eps["shop"].isin(shops)] if len(eps) else eps
        return (float(g["flag_reference_above_low30"].mean()) if len(g) else np.nan), g
    pub, gp = share(PUBLISHING)
    non, gn = share(NON_PUBLISHING)
    non_shops = int((gn["shop"].value_counts() >= 20).sum()) if len(gn) else 0
    low30 = (gp.groupby("shop")["flag_displayed_low30_above_true"].mean().round(3).to_dict() if len(gp) else {})
    return {"flag_share_publishing": pub, "flag_share_non_publishing": non,
            "non_publishing_shops": non_shops, "low30_above_true_by_shop": low30}


def h5_verdict(s: dict, has_bf_data: bool) -> str:
    if not has_bf_data:
        return PENDING
    if s["non_publishing_shops"] < 2:
        return DESCRIPTIVE + " (non-publishing group has fewer than 2 shops with 20+ episodes)"
    pub, non = s["flag_share_publishing"], s["flag_share_non_publishing"]
    if pub <= non - 0.05:
        return "supported"
    if pub >= non:
        return "rejected"
    return "inconclusive"


# ---------------------------------------------------------------- report

def _f(v, pct=False) -> str:
    if isinstance(v, float) and np.isnan(v):
        return "n/a"
    return f"{v * 100:.1f} %" if pct else str(v)


def run(paths: Paths, cs: ComplianceSettings, df: pd.DataFrame) -> tuple[str, dict]:
    """Compute all five hypotheses; returns the Markdown report and the verdicts."""
    sets, bumped = load_frozen(paths)
    has_bf = bool(_in(df["local_date"], BF).any()) if len(df) else False

    daily = lego_daily(df, sets)
    h1b, h1f = h1_stats(daily, BASELINE), h1_stats(daily, BF)
    cuts = lego_cuts(lego_slots(df, sets))
    h2b, h2f = h2_stats(cuts, BASELINE), h2_stats(cuts, BF)
    eps = bf_episodes(df, cs) if has_bf else pd.DataFrame()
    h3 = h3_stats(eps, bumped)
    h4 = h4_stats(eps, label_share(df))
    h5 = h5_stats(eps)

    verdicts = {
        "H1": h1_verdict(h1b, h1f), "H2": h2_verdict(h2f, has_bf), "H3": h3_verdict(h3, has_bf),
        "H4": h4_verdict(h4, has_bf), "H5": h5_verdict(h5, has_bf),
    }
    lines = [
        "# Hypotheses report",
        "",
        f"Windows: baseline {BASELINE[0]} to {BASELINE[1]}, BF {BF[0]} to {BF[1]} (Rome dates). "
        "Rules: docs/hypotheses.md.",
        "",
        "| | verdict |", "|---|---|",
        *[f"| {k} | {v} |" for k, v in verdicts.items()],
        "",
        "## H1. LEGO, Euronics vs Unieuro, per set-day",
        "",
        "| window | set-days | equal | median spread | below RRP, of equal |", "|---|---|---|---|---|",
        *[f"| {name} | {s['set_days']} | {_f(s['equal_share'], True)} | {_f(s['median_spread'], True)} | "
          f"{_f(s['below_rrp_of_equal'], True)} |" for name, s in (("baseline", h1b), ("BF", h1f))],
        "",
        "## H2. LEGO cuts of 3 % or more",
        "",
        "| window | unilateral cuts | followed | follow share | same-slot matches | cuts onto the other's price | open |",
        "|---|---|---|---|---|---|---|",
        *[f"| {name} | {s['unilateral_cuts']} | {s['followed']} | {_f(s['follow_share'], True)} | "
          f"{s['same_slot']} | {s['matched_existing']} | {s['open']} |" for name, s in (("baseline", h2b), ("BF", h2f))],
        "",
        "## H3. Tigotà, episodes starting 20 to 29 Nov",
        "",
        f"Frozen groups: {int(bumped['bumped'].sum())} bumped, {int((~bumped['bumped']).sum())} control products.",
        "",
        f"* bumped: {h3['bumped_episodes']} episodes, median discount_gap {_f(h3['gap_bumped'], True)}",
        f"* control: {h3['control_episodes']} episodes, median discount_gap {_f(h3['gap_control'], True)}",
        "",
        "## H4. Always-labelled (>= 50 % of baseline days) vs rarely-labelled (< 10 %)",
        "",
        *(["| shop | episodes high | episodes low | honest high | honest low | eligible | holds |",
           "|---|---|---|---|---|---|---|"]
          + [f"| {r.shop} | {r.n_high} | {r.n_low} | {_f(r.honest_high, True)} | {_f(r.honest_low, True)} | "
             f"{'yes' if r.eligible else 'no'} | {'yes' if r.holds else 'no'} |" for r in h4.itertuples()]
          if len(h4) else ["no BF episodes yet"]),
        "",
        "## H5. Share of BF episodes flagged reference_above_low30",
        "",
        f"* publishing ({', '.join(PUBLISHING)}): {_f(h5['flag_share_publishing'], True)}",
        f"* not publishing ({', '.join(NON_PUBLISHING)}): {_f(h5['flag_share_non_publishing'], True)}",
        f"* displayed_low30_above_true by shop: {h5['low30_above_true_by_shop'] or 'n/a'}",
        "",
    ]
    return "\n".join(lines), verdicts
