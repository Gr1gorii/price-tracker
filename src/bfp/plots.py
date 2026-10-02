"""The three compliance PNGs (matplotlib, static).

Colors: reference categorical slots in fixed order (validated: scripts/validate_palette.js),
thin marks, hairline solid grid, text in ink tokens — never in series colors.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from bfp.compliance import TZ, assign_episodes, rolling_min, window_stats  # noqa: E402
from bfp.config import ComplianceSettings  # noqa: E402

plt.rcParams["font.family"] = ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"]

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
S1, S2, S3, S4 = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"

WATERMARK: str | None = None  # set by `bfp demo`

WINDOW_START = datetime(2026, 10, 28, tzinfo=ZoneInfo(TZ))
BLACK_FRIDAY = datetime(2026, 11, 27, tzinfo=ZoneInfo(TZ))


def _style(ax, title: str, subtitle: str | None = None) -> None:
    fig = ax.figure
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.spines["bottom"].set_linewidth(1)
    ax.tick_params(colors=MUTED, labelsize=9, length=0, pad=6)
    ax.grid(True, axis="y", color=GRID, linewidth=1, linestyle="-")
    ax.set_axisbelow(True)
    n_sub = subtitle.count("\n") + 1 if subtitle else 0
    ax.set_title(title, loc="left", fontsize=13, color=INK, fontweight="bold", pad=12 + 14 * n_sub)
    if subtitle:
        ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=9.5, color=INK_2, va="bottom", linespacing=1.3)
    if WATERMARK:
        fig.text(0.99, 0.01, WATERMARK, ha="right", va="bottom", fontsize=9, color=MUTED, fontweight="bold")


def _legend(ax, **kw) -> None:
    leg = ax.legend(frameon=False, fontsize=9, labelcolor=INK_2, **kw)
    for t in leg.get_texts():
        t.set_color(INK_2)


def reference_low(g: pd.DataFrame, cs: ComplianceSettings) -> np.ndarray:
    """Legal reference per observation: inside a discount episode the 30-day min *before the
    episode started* (frozen), elsewhere the trailing 30-day min."""
    low = np.array([rolling_min(g, t, cs.window_days, cs.in_stock_only) for t in g["ts"]])
    eps = assign_episodes(g, cs.episode_prev_tolerance)
    days = pd.Timedelta(days=cs.window_days)
    for ep in eps.dropna().unique():
        mask = (eps == ep).to_numpy()
        start = g["ts"][mask].iloc[0]
        low[mask] = window_stats(g, start - days, start, cs).true_low30
    return low


def price_history(df: pd.DataFrame, url: str, out: Path, cs: ComplianceSettings | None = None) -> Path:
    """(a) price steps, 30-day-min reference band, displayed previous price and displayed 30-day low."""
    cs = cs or ComplianceSettings()
    g = df[df["url"] == url].sort_values("ts").reset_index(drop=True)
    if g.empty:
        raise ValueError(f"no ok observations for {url}")
    ts = g["ts"].dt.tz_convert(TZ)
    low = reference_low(g, cs)
    fig, ax = plt.subplots(figsize=(11, 5.2), dpi=150)
    title = (g["title"].dropna().iloc[-1] if g["title"].notna().any() else url)[:80]
    _style(ax, title, f"{g['shop'].iloc[0]} · {url[:110]}")
    ax.fill_between(ts, low, low * (1 + cs.reference_tolerance), step="post", color=S3, alpha=0.18, linewidth=0)
    ax.step(ts, low, where="post", color=S3, linewidth=2, label="lowest price in previous 30 days (reference)")
    ax.step(ts, g["price"], where="post", color=S1, linewidth=2, label="price")
    prev = g["displayed_prev_price"]
    if prev.notna().any():
        ax.step(ts, prev, where="post", color=S2, linewidth=2, label="displayed previous price")
    low30 = g["displayed_low30_price"]
    if low30.notna().any():
        ax.scatter(ts[low30.notna()], low30[low30.notna()], s=40, marker="^", color=S4, edgecolor=SURFACE,
                   linewidth=1.5, zorder=4, label="displayed '30-day lowest'")
    oos = g[~g["purchasable"]] if "purchasable" in g else g.iloc[0:0]
    if not oos.empty:
        ax.scatter(oos["ts"].dt.tz_convert(TZ), oos["price"], s=30, marker="x", color=MUTED, zorder=5, label="out of stock")
    for when, label in ((WINDOW_START, "28 Oct: BF reference window"), (BLACK_FRIDAY, "27 Nov: Black Friday")):
        if ts.min() <= when <= ts.max() + pd.Timedelta(days=1):
            ax.axvline(when, color=AXIS, linewidth=1)
            ax.text(when, 1.0, f" {label}", transform=ax.get_xaxis_transform(), fontsize=8.5, color=MUTED, va="top")
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"€{v:,.0f}".replace(",", ".")))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b", tz=ZoneInfo(TZ)))
    _legend(ax, loc="upper left", bbox_to_anchor=(0, -0.08), ncol=4)
    fig.tight_layout()
    fig.savefig(out, facecolor=SURFACE)
    plt.close(fig)
    return out


def claimed_vs_honest(eps: pd.DataFrame, out: Path) -> Path:
    """(b) claimed vs honest discount per episode (sufficient history only), y = x diagonal."""
    e = eps[(eps["verdict"] != "insufficient_history") & eps["claimed_discount"].notna() & eps["honest_discount"].notna()]
    fig, ax = plt.subplots(figsize=(7.5, 7), dpi=150)
    _style(ax, "Claimed vs honest discount", f"one dot per discount episode · n = {len(e)} · below the line = overstated")
    ax.grid(True, axis="x", color=GRID, linewidth=1)
    hi = max(0.5, float(np.nanmax(e[["claimed_discount", "honest_discount"]].to_numpy())) + 0.05) if len(e) else 0.6
    lo = min(-0.1, float(np.nanmin(e["honest_discount"])) - 0.05) if len(e) else -0.1
    ax.plot([0, hi], [0, hi], color=MUTED, linewidth=1, zorder=1)
    ax.text(hi, hi, " honest = claimed", color=MUTED, fontsize=8.5, ha="right", va="bottom")
    ax.axhline(0, color=AXIS, linewidth=1, zorder=1)
    for flagged, color, marker, label in ((False, S1, "o", "no flag"), (True, S2, "^", "flagged")):
        sub = e[e["flagged"] == flagged]
        ax.scatter(sub["claimed_discount"], sub["honest_discount"], s=42, marker=marker, color=color, alpha=0.85,
                   edgecolor=SURFACE, linewidth=1.5, zorder=3, label=f"{label} ({len(sub)})")
    pct = matplotlib.ticker.PercentFormatter(1.0, decimals=0)
    ax.xaxis.set_major_formatter(pct)
    ax.yaxis.set_major_formatter(pct)
    ax.set_xlim(0, hi)
    ax.set_ylim(lo, hi)
    ax.set_xlabel("claimed discount", color=INK_2, fontsize=9.5)
    ax.set_ylabel("honest discount vs true 30-day low", color=INK_2, fontsize=9.5)
    _legend(ax, loc="upper left")
    fig.tight_layout()
    fig.savefig(out, facecolor=SURFACE)
    plt.close(fig)
    return out


def flagged_by_category(eps: pd.DataFrame, out: Path) -> Path:
    """(c) share of discount episodes (sufficient history) with any flag, per category."""
    e = eps[eps["verdict"] != "insufficient_history"]
    stats = e.groupby("category")["flagged"].agg(["sum", "count"]).reindex(
        ["electronics", "appliances", "beauty", "toys"]).dropna()
    stats = stats[stats["count"] > 0]
    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=150)
    _style(ax, "Share of flagged discounts by category",
           "discount episodes with ≥ 25 days of history · any flag: reference above 30-day low, price bump ≥ 10 %,\n'30-day lowest' shown above what we observed, or shown below the discounted price")
    ax.grid(False)
    ax.grid(True, axis="x", color=GRID, linewidth=1)
    ax.spines["bottom"].set_visible(False)
    share = (stats["sum"] / stats["count"]).to_numpy() if len(stats) else np.array([])
    y = np.arange(len(stats))
    ax.barh(y, share, height=0.3, color=S1, zorder=2)
    for yi, s, (cat, row) in zip(y, share, stats.iterrows()):
        ax.text(s + 0.01, yi, f"{s:.0%}  ({int(row['sum'])}/{int(row['count'])})", va="center", fontsize=9, color=INK_2)
    ax.set_yticks(y, list(stats.index))
    ax.tick_params(axis="y", colors=INK_2, labelsize=10)
    ax.invert_yaxis()
    ax.set_xlim(0, 1.0)
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
    if not len(stats):
        ax.text(0.5, 0.5, "no episodes with sufficient history yet", transform=ax.transAxes, ha="center", color=MUTED)
    fig.tight_layout()
    fig.savefig(out, facecolor=SURFACE)
    plt.close(fig)
    return out


def pick_history_url(obs: pd.DataFrame) -> str | None:
    """Default URL for chart (a): the flagged episode with the largest discount gap."""
    if obs.empty:
        return None
    cand = obs[obs["flagged"] & (obs["verdict"] != "insufficient_history")]
    cand = cand if not cand.empty else obs
    return cand.sort_values("discount_gap", ascending=False, na_position="last")["url"].iloc[0]
