"""prop: the strict prop-firm challenge guard (trailing drawdown, daily loss, target, caps)."""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".claude" / "skills" / "trade-scout"))
import prop  # noqa: E402

NY = prop.NY
CFG = {"prop": {"size": 30000}}
MON = dt.datetime(2026, 11, 30, 10, 0, tzinfo=NY)


def test_the_floor_trails_the_high_and_a_breach_stops_everything(tmp_path):
    f = tmp_path / "prop.json"
    st = prop.update(100000, CFG, MON, f)  # the challenge starts at $30,000
    assert st["equity"] == 30000 and st["floor"] == 29100 and st["status"] == "ok"
    st = prop.update(100500, CFG, MON + dt.timedelta(minutes=30), f)  # +$500: floor up to 29,600
    assert st["hwm"] == 30500 and st["floor"] == 29600
    st = prop.update(99550, CFG, MON + dt.timedelta(hours=1), f)  # 29,550: under the floor
    assert st["status"] == "stop" and "trailing drawdown" in st["reasons"][0]
    st = prop.update(100800, CFG, MON + dt.timedelta(days=1), f)  # a breach never clears
    assert st["status"] == "stop"


def test_daily_loss_counts_from_the_days_start(tmp_path):
    f = tmp_path / "prop.json"
    prop.update(100000, CFG, MON, f)
    prop.update(100700, CFG, MON + dt.timedelta(hours=5), f)  # Monday closes +$700
    st = prop.update(100700, CFG, MON + dt.timedelta(days=1), f)  # Tuesday starts at 30,700
    assert st["day_start"] == 30700 and st["day_floor"] == 30100
    st = prop.update(100050, CFG, MON + dt.timedelta(days=1, hours=2), f)  # -$650 on the day
    assert st["status"] == "stop" and "daily loss" in st["reasons"][0]


def test_target_and_the_consistency_cap_stop_new_trades(tmp_path):
    f = tmp_path / "prop.json"
    prop.update(100000, CFG, MON, f)
    st = prop.update(100950, CFG, MON + dt.timedelta(hours=2), f)  # $950 in a day > $900 cap
    assert st["status"] == "stop" and "day cap" in st["reasons"][0]
    st = prop.update(103100, CFG, MON + dt.timedelta(days=4), f)
    assert any("profit target reached" in r for r in st["reasons"])


def test_allow_caps_risk_by_the_room_left_counts_open_risk_and_needs_a_fresh_check(tmp_path):
    f = tmp_path / "prop.json"
    prop.update(100000, CFG, MON, f)  # room $600 (the daily limit is the nearer floor)
    assert prop.allow(150, CFG, 0, MON, f) == (True, "")  # min($150 cap, $600 x 0.25)
    ok, why = prop.allow(151, CFG, 0, MON, f)
    assert not ok and "over the $150.00 allowed" in why
    ok, why = prop.allow(100, CFG, 300, MON, f)  # ($600 - $300 open) x 0.25 = $75
    assert not ok and "$75.00 allowed" in why
    ok, why = prop.allow(10, CFG, 0, MON + dt.timedelta(minutes=30), f)
    assert not ok and "min old" in why
    assert not prop.allow(10, CFG, 0, MON, tmp_path / "none.json")[0]  # never checked


def test_open_risk_skips_breakeven_trades():
    stock = {
        "A": {"entry": 100.0, "stop": 99.0, "legs": ["5", "5"], "breakeven": False},
        "B": {"entry": 50.0, "stop": 49.0, "legs": ["10"], "breakeven": True},
        "S": {"entry": 20.0, "stop": 21.0, "legs": ["10"], "breakeven": False},
    }
    crypto = {"SOL/USD": {"entry": 70.0, "stop": 68.0, "qty": 5.0}}
    assert prop.open_risk(stock, crypto) == 10.0 + 10.0 + 10.0
