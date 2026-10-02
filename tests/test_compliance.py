from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from bfp.compliance import check, episodes, prepare
from bfp.config import ComplianceSettings

TZ = ZoneInfo("Europe/Rome")
BF = date(2026, 11, 20)


def history(fn, start=date(2026, 10, 10), end=date(2026, 11, 30), url="https://s.test/p", skip=()):
    """fn(day) -> dict(price=…, prev=…, pct=…, low30=…, avail=…) or None for no observation."""
    rows, d = [], start
    while d <= end:
        for h in (8, 20):
            if (d, h) in skip:
                continue
            v = fn(d)
            if v is None:
                continue
            ts = datetime(d.year, d.month, d.day, h, 5, tzinfo=TZ)
            rows.append({
                "shop": "s", "url": url, "category": "electronics", "ean": None, "title": "T", "ts": ts,
                "slot": f"{d}T{h:02d}", "price": v["price"], "currency": "EUR",
                "displayed_prev_price": v.get("prev"), "displayed_low30_price": v.get("low30"),
                "displayed_discount_pct": v.get("pct"), "availability": v.get("avail", "InStock"),
            })
        d += timedelta(days=1)
    return pd.DataFrame(rows)


def run(df, **kw):
    return check(prepare(df), ComplianceSettings(**kw))


def bf(price, prev=100.0, **extra):
    return lambda d: {"price": price, "prev": prev, **extra} if d >= BF else {"price": 100.0}


def test_honest_discount_is_ok():
    out = run(history(bf(80.0)))
    assert len(out) == 22  # 11 days × 2
    r = out.iloc[0]
    assert r.verdict == "ok" and r.true_low30 == 100 and r.history_days == 30
    assert r.claimed_discount == pytest.approx(0.2) and r.honest_discount == pytest.approx(0.2)
    assert not r.flagged and out["episode_id"].nunique() == 1


def test_dip_within_window_flags_reference():
    def f(d):
        if date(2026, 11, 5) <= d <= date(2026, 11, 7):
            return {"price": 85.0}
        return bf(80.0)(d)

    r = run(history(f)).iloc[0]
    assert r.flag_reference_above_low30 and r.verdict == "flagged"
    assert r.true_low30 == 85 and r.true_low30_ts.tz_convert(TZ).date() == date(2026, 11, 7)
    assert r.honest_discount == pytest.approx(1 - 80 / 85)


def test_dip_older_than_30_days_is_not_counted():
    def f(d):
        if date(2026, 10, 12) <= d <= date(2026, 10, 14):  # before 21 Oct = BF − 30 d
            return {"price": 85.0}
        return bf(80.0)(d)

    r = run(history(f)).iloc[0]
    assert r.true_low30 == 100 and r.verdict == "ok"


def test_price_bump_before_discount():
    def f(d):
        if d >= BF:
            return {"price": 96.0, "prev": 120.0}
        return {"price": 120.0 if d >= date(2026, 11, 2) else 100.0}

    r = run(history(f)).iloc[0]
    assert r.flag_price_bump and r.bump_pct == pytest.approx(0.2)
    assert r.flag_reference_above_low30  # 120 shown, 100 seen
    assert r.honest_discount == pytest.approx(0.04) and r.claimed_discount == pytest.approx(0.2)


def test_insufficient_history():
    out = run(history(bf(80.0), start=date(2026, 11, 1)))
    r = out.iloc[0]
    assert r.verdict == "insufficient_history" and r.history_days == 19


def test_insufficient_history_still_records_proven_flag():
    def f(d):
        return {"price": 70.0} if d == date(2026, 11, 10) else bf(80.0)(d)

    r = run(history(f, start=date(2026, 11, 1))).iloc[0]
    assert r.verdict == "insufficient_history" and r.flag_reference_above_low30


def test_progressive_discount_is_one_episode_and_rolling_differs():
    def f(d):
        if d >= date(2026, 11, 25):
            return {"price": 75.0, "prev": 100.0}
        return bf(90.0)(d)

    out = run(history(f))
    assert out["episode_id"].nunique() == 1 and (out["verdict"] == "ok").all()
    first75 = out[out["price"] == 75].iloc[0]
    last = out.iloc[-1]
    assert first75.honest_discount == pytest.approx(0.25) and last.honest_discount == pytest.approx(0.25)
    # the literal per-observation window sees the promo's own prices -> understates, then reaches 0
    assert first75.true_low30_rolling == 90 and first75.honest_discount_rolling == pytest.approx(1 - 75 / 90)
    assert last.true_low30_rolling == 75 and last.honest_discount_rolling == pytest.approx(0)


def test_reference_change_starts_new_episode():
    def f(d):
        if d >= date(2026, 11, 25):
            return {"price": 80.0, "prev": 95.0}
        return bf(90.0)(d)

    out = run(history(f))
    assert out["episode_id"].nunique() == 2
    second = out[out["ts"] >= pd.Timestamp("2026-11-25", tz=TZ)].iloc[0]
    assert second.true_low30 == 90 and second.flag_reference_above_low30


def test_missed_slots_do_not_break_episode():
    skip = {(date(2026, 11, 22), 8), (date(2026, 11, 22), 20), (date(2026, 11, 23), 8)}
    out = run(history(bf(80.0), skip=skip))
    assert out["episode_id"].nunique() == 1


def test_out_of_stock_low_price_ignored_by_default():
    def f(d):
        if d == date(2026, 11, 5):
            return {"price": 60.0, "avail": "OutOfStock"}
        return bf(80.0)(d)

    assert run(history(f)).iloc[0].true_low30 == 100
    assert run(history(f), in_stock_only=False).iloc[0].true_low30 == 60


def test_displayed_low30_above_what_we_saw():
    def f(d):
        if date(2026, 11, 3) <= d <= date(2026, 11, 4):
            return {"price": 92.0}
        return bf(85.0, low30=100.0)(d)

    r = run(history(f)).iloc[0]
    assert r.flag_displayed_low30_above_true and r.true_low30 == 92


def test_pct_only_discount_and_no_discount_rows_ignored():
    def f(d):
        return {"price": 80.0, "pct": 20.0} if d >= BF else {"price": 100.0}

    out = run(history(f))
    assert len(out) == 22 and out.iloc[0].claimed_discount == pytest.approx(0.2)
    assert run(history(lambda d: {"price": 100.0})).empty


def test_pct_only_claim_uses_implied_reference():
    """−20 % badge on 80 € implies a 100 € reference; we saw 90 € in the window → flagged."""
    def f(d):
        if d == date(2026, 11, 5):
            return {"price": 90.0}
        return {"price": 80.0, "pct": 20.0} if d >= BF else {"price": 100.0}

    r = run(history(f)).iloc[0]
    assert r.reference_source == "pct_implied" and r.reference_used == pytest.approx(100)
    assert r.flag_reference_above_low30 and r.true_low30 == 90


def test_self_reported_low30_below_price_needs_no_history():
    """Toys Center pattern: −10 % vs list price, while the page itself says the 30-day best was lower."""
    def f(d):
        return {"price": 404.99, "pct": 10.0, "low30": 382.49}

    out = run(history(f, start=date(2026, 11, 25)))
    r = out.iloc[0]
    assert r.verdict == "insufficient_history" and r.flag_self_reported_low30_below_price and r.flagged


def test_tolerance_one_percent():
    def f(d):
        if d == date(2026, 11, 5):
            return {"price": 99.5}  # 0.5 % below the reference: within tolerance
        return bf(80.0)(d)

    r = run(history(f)).iloc[0]
    assert not r.flag_reference_above_low30 and r.verdict == "ok"


def test_episode_table():
    a = history(bf(80.0), url="https://s.test/a")
    b = history(bf(80.0, prev=120.0), url="https://s.test/b")
    obs = run(pd.concat([a, b]))
    ep = episodes(obs)
    assert len(ep) == 2
    assert set(ep["verdict"]) == {"ok", "flagged"}
    assert ep["n_observations"].tolist() == [22, 22]
