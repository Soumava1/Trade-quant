"""shorts: opening-range breakdowns planned as scout-shaped short candidates."""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".claude" / "skills" / "trade-scout"))
import shorts  # noqa: E402

NY = shorts.features.NY
CFG = {
    "budget": 25000,
    "max_loss_per_trade_usd": 375,
    "allow_short": True,
    "scout": {
        "or_minutes": 5,
        "min_rvol": 1.0,
        "min_atr": 0.5,
        "min_adv": 1000,
        "min_stop_atr": 0.25,
    },
}


def chart(today_path, past_days=8):
    """Yahoo-shaped 5-minute chart: quiet past sessions, then today's bars from 9:30."""
    ts, q = [], {k: [] for k in ("open", "high", "low", "close", "volume")}
    day0 = dt.datetime(2026, 11, 2, 9, 30, tzinfo=NY)
    for d in range(past_days):
        for i in range(78):
            t = day0 + dt.timedelta(days=d, minutes=5 * i)
            ts.append(int(t.timestamp()))
            for k, x in zip(q, (100, 101, 99, 100, 1000.0), strict=True):
                q[k].append(x)
    today = day0 + dt.timedelta(days=past_days)
    for i, (o, h, lo, c, v) in enumerate(today_path):
        ts.append(int((today + dt.timedelta(minutes=5 * i)).timestamp()))
        for k, x in zip(q, (o, h, lo, c, v), strict=True):
            q[k].append(x)
    return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [q]}}]}}, today


def test_a_breakdown_under_vwap_is_a_market_short_with_its_stop_over_the_range(monkeypatch):
    monkeypatch.setattr(shorts, "borrowable", lambda s: (True, ""))
    path = [(100, 100.5, 99.5, 99.8, 5000)] + [(99.8, 99.9, 99.6, 99.7, 3000)] * 2
    path += [(99.6, 99.6, 99.3, 99.4, 3000), (99.4, 99.45, 99.2, 99.3, 3000)]
    c, today = chart(path)
    now = today + dt.timedelta(minutes=25)  # 9:55: the 9:45 and 9:50 bars are closed
    cand = shorts.plan_short("X", c, now, CFG, 2.0)
    assert cand["tier"] == "PICK" and cand["order"]["side"] == "sell", cand
    assert cand["order"]["type"] == "market" and cand["plan"]["stop"] >= 100.5
    assert cand["plan"]["target"] < cand["plan"]["entry"] < cand["plan"]["stop"]
    assert cand["size"]["max_loss"] <= 375


def test_no_short_when_it_is_back_over_the_range_low_or_not_borrowable(monkeypatch):
    path = [(100, 100.5, 99.5, 99.8, 5000)] + [(99.8, 99.9, 99.6, 99.7, 3000)] * 2
    path += [(99.6, 99.6, 99.3, 99.4, 3000), (99.4, 100.0, 99.4, 99.9, 3000)]
    c, today = chart(path)
    monkeypatch.setattr(shorts, "borrowable", lambda s: (True, ""))
    cand = shorts.plan_short("X", c, today + dt.timedelta(minutes=25), CFG, 2.0)
    assert cand["tier"] == "REJECT" and "failed breakdown" in cand["reasons"][0]
    monkeypatch.setattr(shorts, "borrowable", lambda s: (False, "hard to borrow on Alpaca"))
    path[-1] = (99.4, 99.45, 99.2, 99.3, 3000)
    c, today = chart(path)
    cand = shorts.plan_short("X", c, today + dt.timedelta(minutes=25), CFG, 2.0)
    assert cand["tier"] == "REJECT" and cand["reasons"] == ["hard to borrow on Alpaca"]
