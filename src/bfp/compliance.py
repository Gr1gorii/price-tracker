"""Art. 17-bis compliance checker.

For every ok observation that shows a discount:
  episode       contiguous discount-showing observations of a URL with the same displayed
                previous price (±tolerance); failed fetches/missed slots don't break it,
                an ok observation without discount does.
  window        [episode_start − 30 d, episode_start)   — the law counts from the reduction
  history_days  distinct Rome days with an ok observation in the window (< 25 → insufficient)
  true_low30    min in-stock price in the window (upper bound of the real min: 2 samples/day)
  honest        1 − price / true_low30
  reference     displayed previous price, else implied by a shown −N %: price / (1 − N %)
  flags         reference_above_low30 (reference > true_low30 × 1.01), price_bump,
                displayed_low30_above_true, self_reported_low30_below_price (the shop's own
                "30-day lowest" is below the discounted price — evidence that needs no history)
Also `true_low30_rolling` = min over [ts − 30 d, ts), i.e. the literal per-observation window.
"Prezzo consigliato" (RRP, `displayed_rrp_price`) is NOT treated as an announced reduction;
it is carried along for reporting only.
Flags are computed even with insufficient history (a lower price we saw is proof either way),
but such rows keep verdict = insufficient_history and are excluded from shares.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from bfp.config import ComplianceSettings, Config, Paths
from bfp.parse.prices import NOT_PURCHASABLE
from bfp import storage

TZ = "Europe/Rome"

FLAG_COLUMNS = [
    "flag_reference_above_low30", "flag_price_bump", "flag_displayed_low30_above_true",
    "flag_self_reported_low30_below_price",
]
OBS_COLUMNS = [
    "shop", "url", "category", "ean", "title", "ts", "slot", "price", "displayed_prev_price",
    "displayed_rrp_price", "displayed_low30_price", "displayed_discount_pct", "claimed_discount", "reference_used",
    "reference_source", "episode_id", "episode_start",
    "window_start", "window_end", "history_days", "true_low30", "true_low30_ts", "honest_discount",
    "discount_gap", "true_low30_rolling", "honest_discount_rolling", "flag_reference_above_low30",
    "flag_price_bump", "bump_pct", "bump_ts", "flag_displayed_low30_above_true",
    "flag_self_reported_low30_below_price", "flagged", "verdict",
]


def load_ok_observations(paths: Paths) -> pd.DataFrame:
    con = storage.connect(paths)
    df = con.execute(
        """
        SELECT shop, url, category, ean, title, ts, slot,
               CAST(price AS DOUBLE) AS price, currency,
               CAST(displayed_prev_price AS DOUBLE) AS displayed_prev_price,
               CAST(displayed_rrp_price AS DOUBLE) AS displayed_rrp_price,
               CAST(displayed_low30_price AS DOUBLE) AS displayed_low30_price,
               CAST(displayed_discount_pct AS DOUBLE) AS displayed_discount_pct,
               availability
        FROM observations
        WHERE status = 'ok' AND price IS NOT NULL
        """
    ).df()
    return prepare(df)


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.sort_values("ts").drop_duplicates(["url", "slot"], keep="last")
    df["local_date"] = df["ts"].dt.tz_convert(TZ).dt.date
    df["purchasable"] = ~df["availability"].isin(NOT_PURCHASABLE)
    return df.sort_values(["url", "ts"]).reset_index(drop=True)


def has_discount(df: pd.DataFrame) -> pd.Series:
    prev = df["displayed_prev_price"]
    pct = df["displayed_discount_pct"]
    return ((prev.notna() & (df["price"] < prev - 0.005)) | (pct.notna() & (pct > 0))).fillna(False)


def assign_episodes(g: pd.DataFrame, tol: float) -> pd.Series:
    """Episode number per row (NaN for rows without discount). `g` is one URL, sorted by ts."""
    disc = has_discount(g).to_numpy()
    prev = g["displayed_prev_price"].to_numpy(dtype=float)
    out = np.full(len(g), np.nan)
    ep, last_ref, in_ep = -1, np.nan, False
    for i in range(len(g)):
        if not disc[i]:
            in_ep, last_ref = False, np.nan
            continue
        ref = prev[i]
        ref_changed = not np.isnan(ref) and not np.isnan(last_ref) and abs(ref - last_ref) > tol * last_ref
        if not in_ep or ref_changed:
            ep += 1
            in_ep = True
        if not np.isnan(ref):
            last_ref = ref
        out[i] = ep
    return pd.Series(out, index=g.index)


@dataclass
class WindowStats:
    history_days: int
    true_low30: float
    true_low30_ts: pd.Timestamp | None
    bump_pct: float
    bump_ts: pd.Timestamp | None


def window_stats(g: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp, cs: ComplianceSettings) -> WindowStats:
    w = g[(g["ts"] >= start) & (g["ts"] < end)]
    history_days = int(w["local_date"].nunique())
    wp = w[w["purchasable"]] if cs.in_stock_only else w
    if wp.empty:
        return WindowStats(history_days, np.nan, None, np.nan, None)
    low = float(wp["price"].min())
    low_ts = wp.loc[wp["price"] == low, "ts"].max()
    prices = wp["price"].to_numpy(dtype=float)
    running_min = np.minimum.accumulate(prices)
    ratio = np.full(len(prices), np.nan)
    ratio[1:] = prices[1:] / running_min[:-1]
    if len(prices) > 1 and np.nanmax(ratio) > 0:
        k = int(np.nanargmax(ratio))
        bump, bump_ts = float(ratio[k] - 1), wp["ts"].iloc[k]
    else:
        bump, bump_ts = 0.0, None
    return WindowStats(history_days, low, low_ts, bump, bump_ts)


def rolling_min(g: pd.DataFrame, ts: pd.Timestamp, days: int, in_stock_only: bool) -> float:
    w = g[(g["ts"] >= ts - pd.Timedelta(days=days)) & (g["ts"] < ts)]
    if in_stock_only:
        w = w[w["purchasable"]]
    return float(w["price"].min()) if not w.empty else np.nan


def _episode_id(shop: str, url: str, start: pd.Timestamp) -> str:
    return f"{shop}:{hashlib.sha1(url.encode()).hexdigest()[:8]}:{start.tz_convert(TZ):%Y%m%dT%H%M}"


def check(df: pd.DataFrame, cs: ComplianceSettings) -> pd.DataFrame:
    """Per-observation compliance rows for all discount-showing ok observations."""
    if df.empty:
        return pd.DataFrame(columns=OBS_COLUMNS)
    rows = []
    days = pd.Timedelta(days=cs.window_days)
    for url, g in df.groupby("url", sort=False):
        g = g.sort_values("ts")
        eps = assign_episodes(g, cs.episode_prev_tolerance)
        for ep in sorted(eps.dropna().unique()):
            members = g[eps == ep]
            start = members["ts"].iloc[0]
            ws = window_stats(g, start - days, start, cs)
            eid = _episode_id(members["shop"].iloc[0], url, start)
            for _, r in members.iterrows():
                prev, pct, low30_shown = r["displayed_prev_price"], r["displayed_discount_pct"], r["displayed_low30_price"]
                claimed = pct / 100 if pd.notna(pct) and pct > 0 else (1 - r["price"] / prev if pd.notna(prev) else np.nan)
                # reference the shop implies: shown previous price, else price / (1 − shown %)
                if pd.notna(prev):
                    ref, ref_src = prev, "prev"
                elif pd.notna(pct) and 0 < pct < 100:
                    ref, ref_src = r["price"] / (1 - pct / 100), "pct_implied"
                else:
                    ref, ref_src = np.nan, None
                honest = 1 - r["price"] / ws.true_low30 if pd.notna(ws.true_low30) else np.nan
                rmin = rolling_min(g, r["ts"], cs.window_days, cs.in_stock_only)
                has_low = pd.notna(ws.true_low30)
                f_ref = bool(has_low and pd.notna(ref) and ref > ws.true_low30 * (1 + cs.reference_tolerance))
                # the shop's own "30-day lowest" is below the price it calls discounted — needs no history
                f_self = bool(pd.notna(low30_shown) and low30_shown < r["price"] * (1 - cs.reference_tolerance))
                f_bump = bool(pd.notna(ws.bump_pct) and ws.bump_pct >= cs.bump_threshold)
                f_low30 = bool(has_low and pd.notna(low30_shown) and low30_shown > ws.true_low30 * (1 + cs.reference_tolerance))
                flagged = f_ref or f_bump or f_low30 or f_self
                sufficient = has_low and ws.history_days >= cs.min_history_days
                rows.append({
                    "shop": r["shop"], "url": url, "category": r["category"], "ean": r["ean"], "title": r["title"],
                    "ts": r["ts"], "slot": r["slot"], "price": r["price"], "displayed_prev_price": prev,
                    "displayed_rrp_price": r.get("displayed_rrp_price"),
                    "displayed_low30_price": low30_shown, "displayed_discount_pct": pct,
                    "claimed_discount": claimed, "reference_used": ref, "reference_source": ref_src, "episode_id": eid, "episode_start": start,
                    "window_start": start - days, "window_end": start, "history_days": ws.history_days,
                    "true_low30": ws.true_low30, "true_low30_ts": ws.true_low30_ts, "honest_discount": honest,
                    "discount_gap": claimed - honest if pd.notna(claimed) and pd.notna(honest) else np.nan,
                    "true_low30_rolling": rmin,
                    "honest_discount_rolling": 1 - r["price"] / rmin if pd.notna(rmin) else np.nan,
                    "flag_reference_above_low30": f_ref, "flag_price_bump": f_bump,
                    "bump_pct": ws.bump_pct, "bump_ts": ws.bump_ts,
                    "flag_displayed_low30_above_true": f_low30, "flag_self_reported_low30_below_price": f_self,
                    "flagged": flagged,
                    "verdict": "insufficient_history" if not sufficient else ("flagged" if flagged else "ok"),
                })
    out = pd.DataFrame(rows, columns=OBS_COLUMNS)
    return out.sort_values(["shop", "url", "ts"]).reset_index(drop=True)


def filter_dates(obs: pd.DataFrame, date_from: date | None, date_to: date | None) -> pd.DataFrame:
    if obs.empty:
        return obs
    d = pd.to_datetime(obs["ts"], utc=True).dt.tz_convert(TZ).dt.date
    mask = pd.Series(True, index=obs.index)
    if date_from:
        mask &= d >= date_from
    if date_to:
        mask &= d <= date_to
    return obs[mask]


def episodes(obs: pd.DataFrame) -> pd.DataFrame:
    """One row per episode: its headline (max claimed) observation; flags = any within the episode."""
    if obs.empty:
        return obs.copy()
    flags = obs.groupby("episode_id")[FLAG_COLUMNS].any()
    n = obs.groupby("episode_id").size().rename("n_observations")
    idx = obs.assign(_c=obs["claimed_discount"].fillna(-1)).groupby("episode_id")["_c"].idxmax()
    head = obs.loc[idx].set_index("episode_id").drop(columns=flags.columns)
    ep = head.join(flags).join(n).reset_index()
    ep["flagged"] = ep[flags.columns].any(axis=1)
    ep["verdict"] = np.where(ep["verdict"] == "insufficient_history", "insufficient_history",
                             np.where(ep["flagged"], "flagged", "ok"))
    return ep


def run(cfg: Config, paths: Paths, date_from: date | None = None, date_to: date | None = None,
        df: pd.DataFrame | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = load_ok_observations(paths) if df is None else prepare(df)
    obs = filter_dates(check(df, cfg.settings.compliance), date_from, date_to)
    eps = episodes(obs)
    paths.reports.mkdir(parents=True, exist_ok=True)
    _to_csv(obs, paths.reports / "compliance.csv")
    _to_csv(eps, paths.reports / "episodes.csv")
    return obs, eps


def _to_csv(df: pd.DataFrame, path) -> None:
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.tz_convert(TZ).dt.strftime("%Y-%m-%d %H:%M")
        elif c.endswith(("_ts", "_start", "_end")) and out[c].dtype == object:
            out[c] = out[c].map(lambda v: v.tz_convert(TZ).strftime("%Y-%m-%d %H:%M") if isinstance(v, pd.Timestamp) else "")
    for c in ("claimed_discount", "honest_discount", "discount_gap", "honest_discount_rolling", "bump_pct"):
        if c in out:
            out[c] = out[c].round(4)
    for c in ("reference_used", "true_low30", "true_low30_rolling"):
        if c in out:
            out[c] = pd.to_numeric(out[c], errors="coerce").round(2)
    out.to_csv(path, index=False)
