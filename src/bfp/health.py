"""Per-shop parse-success report, missing slots, optional Telegram alert."""

from __future__ import annotations

import os
from datetime import datetime

import httpx

from bfp.config import Config, Paths
from bfp.schedule import expected_slots
from bfp import storage

FAIL_COLS = ("parse_failed", "needs_js", "not_found", "http_error", "network_error", "blocked", "render_error", "skipped_blocked")


def success_rate(counts: dict[str, int]) -> float | None:
    """ok / (attempted − robots_disallowed). None when nothing was attempted."""
    denom = sum(counts.values()) - counts.get("robots_disallowed", 0)
    return None if denom <= 0 else counts.get("ok", 0) / denom


def shop_table(con, run_ids: list[str] | None = None) -> list[dict]:
    where = ""
    params: list = []
    if run_ids:
        where = f"WHERE run_id IN ({', '.join('?' for _ in run_ids)})"
        params = list(run_ids)
    rows = con.execute(
        f"""
        SELECT shop, status, count(*) AS n,
               count(*) FILTER (WHERE displayed_prev_price IS NOT NULL) AS with_prev,
               count(*) FILTER (WHERE displayed_rrp_price IS NOT NULL) AS with_rrp,
               count(*) FILTER (WHERE displayed_low30_price IS NOT NULL) AS with_low30,
               count(*) FILTER (WHERE displayed_discount_pct IS NOT NULL) AS with_pct,
               string_agg(DISTINCT parse_method, ',') FILTER (WHERE status = 'ok') AS methods
        FROM observations {where}
        GROUP BY shop, status ORDER BY shop, status
        """,
        params,
    ).fetchall()
    shops: dict[str, dict] = {}
    for shop, status, n, wp, wr, wl, wd, methods in rows:
        s = shops.setdefault(shop, {"shop": shop, "counts": {}, "with_prev": 0, "with_rrp": 0, "with_low30": 0, "with_pct": 0, "methods": set()})
        s["counts"][status] = n
        s["with_prev"] += wp
        s["with_rrp"] += wr
        s["with_low30"] += wl
        s["with_pct"] += wd
        if methods:
            s["methods"].update(methods.split(","))
    out = []
    for s in shops.values():
        s["total"] = sum(s["counts"].values())
        s["ok"] = s["counts"].get("ok", 0)
        s["success"] = success_rate(s["counts"])
        s["methods"] = ",".join(sorted(s["methods"]))
        out.append(s)
    return sorted(out, key=lambda x: x["shop"])


def render_markdown(table: list[dict], title: str) -> str:
    head = "| shop | total | ok | success | " + " | ".join(FAIL_COLS) + " | robots_disallowed | prev shown | RRP shown | low30 shown | % shown | methods |"
    sep = "|" + "---|" * (head.count("|") - 1)
    lines = [f"### {title}", "", head, sep]
    for s in table:
        c = s["counts"]
        rate = "—" if s["success"] is None else f"{s['success']:.0%}"
        lines.append(
            f"| {s['shop']} | {s['total']} | {s['ok']} | {rate} | "
            + " | ".join(str(c.get(k, 0)) for k in FAIL_COLS)
            + f" | {c.get('robots_disallowed', 0)} | {s['with_prev']} | {s['with_rrp']} | {s['with_low30']} | {s['with_pct']} | {s['methods']} |"
        )
    return "\n".join(lines)


def missing_slots(cfg: Config, con, until: datetime | None = None) -> list[str]:
    seen = {r[0] for r in con.execute("SELECT DISTINCT slot FROM observations").fetchall()}
    return [s for s in expected_slots(cfg.settings, until) if s not in seen]


def alerts_for(cfg: Config, table: list[dict], manifest: dict | None) -> list[str]:
    msgs = []
    thr = cfg.settings.alert_threshold
    for s in table:
        if s["success"] is not None and s["success"] < thr:
            msgs.append(f"{s['shop']}: success {s['success']:.0%} < {thr:.0%} ({s['counts']})")
    if manifest:
        for shop, info in manifest.get("shops", {}).items():
            if info.get("stopped_reason"):
                msgs.append(f"{shop}: stopped — {info['stopped_reason']}")
            if info.get("robots") in ("unavailable", "disallow_all", "blocked"):
                msgs.append(f"{shop}: robots.txt {info['robots']} — nothing collected")
    return msgs


def send_telegram(text: str) -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat, "text": text[:4000]}, timeout=20)
    return r.status_code == 200


def report(cfg: Config, paths: Paths, runs: int = 1, alert: bool = False) -> str:
    con = storage.connect(paths)
    manifests = storage.load_manifests(paths)
    last = manifests[-runs:] if manifests else []
    run_ids = [m["run_id"] for m in last]
    parts = []
    table = shop_table(con, run_ids) if run_ids else []
    label = run_ids[0] if len(run_ids) == 1 else f"last {len(run_ids)} runs ({run_ids[0]} … {run_ids[-1]})" if run_ids else "no runs yet"
    parts.append(render_markdown(table, f"Parse success — {label}"))
    if last:
        skipped = [(shop, i["skipped_reason"]) for m in last[-1:] for shop, i in m["shops"].items() if i.get("skipped_reason")]
        stopped = [(shop, i["stopped_reason"]) for m in last[-1:] for shop, i in m["shops"].items() if i.get("stopped_reason")]
        for title, items in (("Skipped shops", skipped), ("Stopped shops", stopped)):
            if items:
                parts.append(f"\n**{title}:** " + "; ".join(f"{a}: {b}" for a, b in items))
    miss = missing_slots(cfg, con)
    parts.append(
        f"\n**Missing scheduled slots since {cfg.settings.start_date}:** {len(miss)}"
        + (f" (latest: {', '.join(miss[-6:])})" if miss else "")
    )
    blocked = storage.load_blocked(paths)
    if blocked:
        parts.append("\n**Blocked (auto-skipped until cleared):** " + ", ".join(f"{k} since {v['since']}" for k, v in blocked.items()))
    text = "\n".join(parts)
    paths.reports.mkdir(parents=True, exist_ok=True)
    (paths.reports / "health.md").write_text(text + "\n", encoding="utf-8")
    if alert:
        msgs = alerts_for(cfg, shop_table(con, run_ids[-1:]) if run_ids else [], last[-1] if last else None)
        if msgs:
            send_telegram("BF tracker alert (" + (run_ids[-1] if run_ids else "?") + ")\n" + "\n".join(msgs))
    return text
