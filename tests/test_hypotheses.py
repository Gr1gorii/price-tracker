from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from bfp import hypotheses as hyp
from bfp.compliance import prepare
from bfp.config import ComplianceSettings

TZ = ZoneInfo("Europe/Rome")


def obs(shop, url, title, fn, start=date(2026, 10, 5), end=date(2026, 11, 30)):
    """fn(day, hour) -> dict(price=…, prev=…, rrp=…) or None."""
    rows, d = [], start
    while d <= end:
        for h in (8, 20):
            v = fn(d, h)
            if v is None:
                continue
            rows.append({
                "shop": shop, "url": url, "category": "toys", "ean": None, "title": title,
                "ts": datetime(d.year, d.month, d.day, h, 5, tzinfo=TZ), "slot": f"{d}T{h:02d}",
                "price": v["price"], "currency": "EUR", "displayed_prev_price": v.get("prev"),
                "displayed_rrp_price": v.get("rrp"), "displayed_low30_price": None,
                "displayed_discount_pct": None, "availability": "InStock",
            })
        d += timedelta(days=1)
    return pd.DataFrame(rows)


def test_lego_sets_need_two_shops():
    flat = lambda d, h: {"price": 10.0}
    df = prepare(pd.concat([
        obs("euronics", "e/1", "LEGO City 60443 Treno", flat, end=date(2026, 10, 6)),
        obs("unieuro", "u/1", "Lego 60443 city", flat, end=date(2026, 10, 6)),
        obs("euronics", "e/2", "LEGO Technic 42204", flat, end=date(2026, 10, 6)),
        obs("unieuro", "u/3", "Puzzle 12345 pezzi", flat, end=date(2026, 10, 6)),
    ]))
    sets = hyp.lego_sets(df)
    assert set(sets["set"]) == {"60443"}
    assert sorted(sets["shop"]) == ["euronics", "unieuro"]


def test_tigota_bumped_rule():
    up = lambda d, h: {"price": 10.0 if d < date(2026, 11, 1) else 11.0}       # +10 %
    small = lambda d, h: {"price": 10.0 if d < date(2026, 11, 1) else 10.4}    # +4 %
    df = prepare(pd.concat([obs("tigota", "t/up", "A", up), obs("tigota", "t/small", "B", small)]))
    b = hyp.tigota_bumped(df).set_index("url")["bumped"]
    assert b["t/up"] and not b["t/small"]


def daily(rows):
    return pd.DataFrame(rows, columns=["set", "local_date", "euronics", "unieuro", "rrp"])


def test_h1_verdicts():
    base_days = [("1", date(2026, 10, 10) + timedelta(days=i), 50.0, 50.0, 50.0) for i in range(10)]
    split = daily(base_days + [("1", date(2026, 11, 21) + timedelta(days=i), 40.0, 50.0, 50.0) for i in range(5)])
    match = daily(base_days + [("1", date(2026, 11, 21) + timedelta(days=i), 40.0, 40.0, 50.0) for i in range(5)])
    b = hyp.h1_stats(split, hyp.BASELINE)
    assert b["equal_share"] == 1.0 and b["below_rrp_of_equal"] == 0.0
    assert hyp.h1_verdict(b, hyp.h1_stats(split, hyp.BF)).startswith("H1a")
    assert hyp.h1_verdict(b, hyp.h1_stats(match, hyp.BF)).startswith("H1b")
    assert hyp.h1_verdict(b, hyp.h1_stats(daily(base_days), hyp.BF)) == hyp.PENDING


def slots(e, u, set_="1"):
    idx = pd.MultiIndex.from_tuples([(set_, f"2026-11-{20 + i // 2:02d}T{'08' if i % 2 == 0 else '20'}")
                                     for i in range(len(e))], names=["set", "slot"])
    return pd.DataFrame({"euronics": e, "unieuro": u}, index=idx)


def test_cut_kinds():
    followed = hyp.lego_cuts(slots([50, 40, 40, 40, 40], [50, 50, 40, 40, 40]))
    # the follower's own move is not a second unilateral cut
    assert list(followed["kind"]) == ["followed", "matched_existing"]
    assert list(followed["leader"]) == ["euronics", "unieuro"]
    alone = hyp.lego_cuts(slots([50, 40, 40, 40, 40], [50, 50, 50, 50, 50]))
    assert list(alone["kind"]) == ["alone"]
    same = hyp.lego_cuts(slots([50, 40, 40], [50, 40, 40]))
    assert list(same["kind"]) == ["same_slot"]
    small = hyp.lego_cuts(slots([50, 49, 49, 49], [50, 50, 49, 49]))     # 2 % is not a cut
    assert small.empty
    late = hyp.lego_cuts(slots([50, 50, 50, 40], [50, 50, 50, 50]))      # no 2 slots left to follow
    assert list(late["kind"]) == ["open"]
    # a cut down to the other chain's existing price is a match, not a cut the other could follow
    to_existing = hyp.lego_cuts(slots([50, 40, 40, 40], [40, 40, 40, 40]))
    assert list(to_existing["kind"]) == ["matched_existing"]
    assert hyp.h2_stats(to_existing, hyp.BF)["unilateral_cuts"] == 0


def test_h2_verdict_thresholds():
    s = lambda cuts, followed: {"unilateral_cuts": cuts, "followed": followed,
                                "follow_share": followed / cuts if cuts else np.nan}
    assert hyp.h2_verdict(s(30, 5), True) == "supported"
    assert hyp.h2_verdict(s(30, 1), True) == "rejected"
    assert hyp.h2_verdict(s(30, 3), True) == "inconclusive"
    assert hyp.h2_verdict(s(19, 19), True).startswith(hyp.DESCRIPTIVE)
    assert hyp.h2_verdict(s(30, 5), False) == hyp.PENDING


def test_h3_h4_h5_verdict_thresholds():
    h3 = lambda n, gb, gc: {"bumped_episodes": n, "gap_bumped": gb, "gap_control": gc}
    assert hyp.h3_verdict(h3(12, 0.08, 0.02), True) == "supported"
    assert hyp.h3_verdict(h3(12, 0.08, 0.0), True) == "supported"
    assert hyp.h3_verdict(h3(12, 0.08, 0.05), True) == "inconclusive"
    assert hyp.h3_verdict(h3(12, 0.005, 0.0), True).startswith("rejected")
    assert hyp.h3_verdict(h3(9, 0.08, 0.0), True).startswith(hyp.DESCRIPTIVE)

    h4 = lambda eligible, holds: pd.DataFrame({"eligible": [True] * eligible + [False],
                                               "holds": [True] * holds + [False] * (eligible - holds) + [False]})
    assert hyp.h4_verdict(h4(4, 3), True) == "supported"
    assert hyp.h4_verdict(h4(4, 1), True) == "rejected"
    assert hyp.h4_verdict(h4(4, 2), True) == "inconclusive"
    assert hyp.h4_verdict(h4(2, 2), True).startswith(hyp.DESCRIPTIVE)

    h5 = lambda pub, non, shops=2: {"flag_share_publishing": pub, "flag_share_non_publishing": non,
                                    "non_publishing_shops": shops}
    assert hyp.h5_verdict(h5(0.10, 0.20), True) == "supported"
    assert hyp.h5_verdict(h5(0.20, 0.20), True) == "rejected"
    assert hyp.h5_verdict(h5(0.17, 0.20), True) == "inconclusive"
    assert hyp.h5_verdict(h5(0.10, 0.20, shops=1), True).startswith(hyp.DESCRIPTIVE)


def test_run_end_to_end(project):
    rrp = 50.0
    euro = lambda d, h: {"price": 40.0 if d >= date(2026, 11, 20) else rrp}
    uni = lambda d, h: {"price": 40.0 if d >= date(2026, 11, 20) else rrp, "rrp": rrp}
    tig = lambda d, h: ({"price": 8.0} if d <= date(2026, 10, 30)
                        else {"price": 9.0, "prev": 12.0} if d >= date(2026, 11, 22)
                        else {"price": 12.0})
    df = pd.concat([
        obs("euronics", "e/1", "LEGO City 60443", euro),
        obs("unieuro", "u/1", "LEGO 60443 Treno", uni),
        obs("tigota", "t/1", "Crema", tig),
    ])
    df = prepare(df)
    written = hyp.freeze(project, df)
    assert {p.name for p in written} == {"lego_sets.csv", "tigota_bumped.csv"}
    with pytest.raises(FileExistsError):
        hyp.freeze(project, df)

    report, v = hyp.run(project, ComplianceSettings(), df)
    assert v["H1"].startswith("H1b")                 # both at 40, below the recommended 50
    assert v["H2"].startswith(hyp.DESCRIPTIVE)       # a same-slot match, no unilateral cut
    assert v["H3"].startswith(hyp.DESCRIPTIVE)       # one bumped episode only
    assert "| H1 |" in report and "Frozen groups: 1 bumped" in report
