"""crypto-desk: the 24-hour breakout, its exits and trail, the 3 % drawdown cap, the guards."""

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".claude" / "skills" / "crypto-desk"))
import crypto  # noqa: E402


@pytest.fixture(autouse=True)
def _no_prop_guard(monkeypatch):
    """The challenge guard has its own tests (test_prop.py); here it always allows."""
    monkeypatch.setattr(crypto.desk, "prop_allows", lambda risk, now=None: (True, ""))


S = crypto.DEFAULTS | {"fee_pct": 0.0}
T0 = 1_795_000_000 - (1_795_000_000 % (4 * 3600))  # on the 4-hour check grid
NOW = dt.datetime(2026, 11, 26, 12, tzinfo=dt.UTC)
PLAN = {"SOL/USD": {"entry": 70.0, "stop": 68.0, "level": 69.9}}


def bars_from(closes, spread=0.002):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        hi, lo = max(prev, c) * (1 + spread), min(prev, c) * (1 - spread)
        out.append((T0 + 3600 * i, prev, hi, lo, c, 100))
        prev = c
    return out


def flat_ind(bars, ema_level=0.0, atr_level=0.5):
    return [ema_level] * len(bars), [atr_level] * len(bars)


def test_t1_then_breakeven_and_fees_in_r():
    plan = {"entry": 100.0, "stop": 99.0}
    bars = [(T0, 100, 100, 100, 100)] + [
        (T0 + 3600 * i, 100, 102.1, 100.5, 102.0) for i in (1, 2, 3, 4)
    ]
    bars += [(T0 + 3600 * 5, 101, 101, 99.5, 99.6)]
    t = crypto.trade_from(bars, 0, plan, S, *flat_ind(bars))
    assert t["r"] == 1.0 and t["how"] == "T1 then breakeven"  # 0.5 x 2R + 0.5 x 0
    t = crypto.trade_from(bars, 0, plan, S | {"fee_pct": 0.25}, *flat_ind(bars))
    assert t["r"] == round(1.0 - 0.005 * 100 / 1.0, 3)  # 0.5 % round trip = 0.5R here


def test_a_stop_gap_fills_at_the_open_and_a_loser_is_out_at_24h():
    plan = {"entry": 100.0, "stop": 99.0}
    gap = [(T0, 100, 100, 100, 100), (T0 + 3600, 98.0, 98.5, 97.5, 98.0)]
    assert crypto.trade_from(gap, 0, plan, S, *flat_ind(gap))["r"] == -2.0  # gapped 2R under
    flat = [(T0, 100, 100, 100, 100)] + [
        (T0 + 3600 * i, 100.2, 100.4, 99.9, 100.3) for i in range(1, 30)
    ]
    t = crypto.trade_from(flat, 0, plan, S, *flat_ind(flat))
    assert t["how"] == "24h exit" and t["r"] == 0.3  # no T1 by 24h: not a winner, out


def test_a_winner_past_24h_is_trailed_and_out_by_72h():
    plan = {"entry": 100.0, "stop": 99.0}
    # T1 (+2R) by 4h, then a slow grind higher that never reaches T2 (+4R = 104)
    bars = [(T0, 100, 100, 100, 100)]
    for i in range(1, 90):
        c = min(102.0 + 0.02 * i, 103.9)
        bars.append((T0 + 3600 * i, c - 0.01, c + 0.01, c - 0.05, c))
    t = crypto.trade_from(bars, 0, plan, S, *flat_ind(bars, ema_level=101.0, atr_level=0.1))
    assert t["how"] == "72h exit" and t["r"] > 1.9  # held past 24h, out at the 72h cap
    # the same path under its trend at 24h is not let run
    t = crypto.trade_from(bars, 0, plan, S, *flat_ind(bars, ema_level=200.0, atr_level=0.1))
    assert t["how"] == "24h exit"


def test_signal_needs_a_fresh_breakout_above_the_trend_and_no_chase():
    up = [100 + 0.1 * i for i in range(80)] + [100 + 0.1 * 79 + 0.9]
    bars = bars_from(up)
    e, a = crypto.indicators(bars, S)
    plan = crypto.signal_at(bars, len(bars) - 1, S, e, a)
    assert plan and plan["stop"] < plan["entry"] and plan["level"] < plan["entry"]
    assert crypto.signal_at(bars, len(bars) - 1, S | {"max_chase_pct": 0.1}, e, a) is None
    bars = bars_from(list(reversed(up)))
    e, a = crypto.indicators(bars, S)
    assert crypto.signal_at(bars, len(bars) - 1, S, e, a) is None  # under the trend


def test_arm_sizes_to_half_a_percent_and_refuses_thin_books_and_full_slots(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, order = crypto.arm("SOL/USD", 70.1, 70.05, S, PLAN)
    assert code == crypto.desk.READY and order["type"] == "market"
    qty = float(order["qty"])
    assert abs(qty * (70.1 - 68.0) - 25.0) < 0.05  # 0.5 % of $5,000 at the stop
    assert crypto.arm("SOL/USD", 70.1, 69.5, S, PLAN)[0] == crypto.desk.SKIPPED  # 0.86 % spread
    crypto.save_state({"open": {f"X{i}": {} for i in range(3)}, "closed": []})
    assert crypto.arm("SOL/USD", 70.1, 70.05, S, PLAN)[0] == crypto.desk.SKIPPED  # 3 open
    log = (tmp_path / "trades-log.md").read_text(encoding="utf-8")
    assert "Spread 0.86% over the 0.3% limit" in log and "3 crypto trades open" in log


def test_the_drawdown_cap_counts_open_risk_and_halves_size_under_pressure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # $80 under the peak: past 1.5 % ($75), so the next trade risks $12.50, not $25
    closed = [{"symbol": "A", "pnl": 40.0, "at": "x"}, {"symbol": "A", "pnl": -80.0, "at": "x"}]
    crypto.save_state({"open": {}, "closed": closed})
    e = crypto.equity(crypto.load_state(), S)
    assert e["peak"] == 5040.0 and e["dd"] == 80.0
    code, order = crypto.arm("SOL/USD", 70.1, 70.05, S, PLAN)
    assert code == crypto.desk.READY and abs(float(order["qty"]) * 2.1 - 12.5) < 0.05
    # $80 down + $40 open risk + $12.50 > $125 (3 % less the 0.5 % buffer): refused
    held = {"B/USD": {"entry": 10.0, "stop": 9.0, "qty": 40.0}}
    crypto.save_state({"open": held, "closed": closed})
    assert crypto.arm("SOL/USD", 70.1, 70.05, S, PLAN)[0] == crypto.desk.SKIPPED
    assert "Drawdown cap" in (tmp_path / "trades-log.md").read_text(encoding="utf-8")


def test_worst_case_never_passes_three_percent():
    """Take every trade the guards allow and lose each at its stop: still under 3 %."""
    st, s = {"open": {}, "closed": []}, S
    budget = s["budget"]
    for _ in range(40):
        e = crypto.equity(st, s)
        risk = budget * s["risk_per_trade_pct"] / 100
        if e["dd"] >= budget * s["halve_at_dd_pct"] / 100:
            risk /= 2
        room = budget * (s["max_drawdown_pct"] - s["dd_buffer_pct"]) / 100
        if e["dd"] + e["open_risk"] + risk > room:
            break
        st["closed"].append({"pnl": -risk, "at": "x"})
    assert crypto.equity(st, s)["dd"] <= budget * s["max_drawdown_pct"] / 100


def test_manage_takes_t1_then_trails_a_winner_and_closes_a_loser_at_24h():
    p = {"entry": 70.0, "stop": 68.0, "first_stop": 68.0, "t1": 74.0, "t2": 78.0, "stop_id": "s1"}
    st = {"open": {"SOL/USD": p | {"opened_at": (NOW - dt.timedelta(hours=5)).isoformat()}}}
    step = crypto.manage(st, {"SOL/USD": 74.5}, {"SOL/USD": 10.0}, S, NOW)[0]
    assert step["do"] == "t1" and step["sell"] == 5.0 and step["cancel"] == "s1"
    assert step["new_stop"]["stop_price"] == "70" and step["new_stop"]["qty"] == "5"

    start = int((NOW - dt.timedelta(hours=30)).timestamp())
    bars = [(start + 3600 * i, 75.4, 75.8, 75.2, 75.5, 50) for i in range(100)]
    win = p | {"t1_done": True, "stop": 70.0}
    win["opened_at"] = (NOW - dt.timedelta(hours=25)).isoformat()
    st = {"open": {"SOL/USD": win}}
    step = crypto.manage(st, {"SOL/USD": 76.0}, {"SOL/USD": 5.0}, S, NOW, lambda sym: bars)[0]
    assert step["do"] == "trail" and float(step["stop_price"]) > 70.0 and step["replace"] == "s1"

    lose = p | {"opened_at": (NOW - dt.timedelta(hours=25)).isoformat()}
    step = crypto.manage(
        {"open": {"SOL/USD": lose}}, {"SOL/USD": 71.0}, {"SOL/USD": 10.0}, S, NOW, lambda s: bars
    )[0]
    assert step["do"] == "close" and step["event"] == "time" and "not a winner" in step["why"]
    old = win | {"opened_at": (NOW - dt.timedelta(hours=73)).isoformat()}
    step = crypto.manage({"open": {"SOL/USD": old}}, {"SOL/USD": 75.5}, {"SOL/USD": 5.0}, S, NOW)
    assert step[0]["do"] == "close" and "72h cap" in step[0]["why"]
    assert crypto.manage(st, {}, {}, S, NOW)[0]["do"] == "stopped"


def test_done_records_pnl_equity_and_the_cap_then_blocks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = {"entry": 70.0, "stop": 68.0, "first_stop": 68.0, "t1": 74.0, "t2": 78.0}
    p |= {"stop_id": "s1", "opened_at": "x", "qty": 60.0}
    crypto.save_state({"open": {"SOL/USD": p}, "closed": []})
    line = crypto.done("SOL/USD", "stopped", 60.0, 68.0, None, NOW)
    assert "| SOL/USD | EXIT | 60 sh |" in line and "Stop filled: -1.0R (P&L $-120.00" in line
    assert "$120.00 under peak" in line
    st = json.loads((tmp_path / "trader" / "crypto" / "positions.json").read_text())
    assert st["open"] == {} and st["closed"][0]["pnl"] == -120.0
    assert crypto.arm("SOL/USD", 70.1, 70.05, S, PLAN, NOW)[0] == crypto.desk.SKIPPED  # the cap


def test_backtest_marks_a_coin_that_barely_trades_on_alpaca_as_a_thin_book():
    thin = [b[:5] + (2,) for b in bars_from([100 + 0.1 * i for i in range(300)])]
    assert crypto.backtest_coin(thin, S | {"min_trades": 1})["verdict"] == "thin book"


def test_guards_no_crypto_section_a_stale_backtest_and_the_kill_switch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"paper": True}))
    try:
        crypto.settings(cfg)
        raise AssertionError("no crypto section must refuse")
    except SystemExit as e:
        assert 'no "crypto" section' in str(e)
    st = tmp_path / "trader" / "crypto"
    st.mkdir(parents=True)
    doc = {"at": (NOW - dt.timedelta(hours=10)).isoformat()}
    doc["coins"] = {"SOL/USD": {"verdict": "keep"}}
    (st / "backtest.json").write_text(json.dumps(doc))
    assert crypto.kept_coins(S, NOW) == ["SOL/USD"]
    assert crypto.kept_coins(S, NOW + dt.timedelta(hours=48)) == []  # 58h old: trades nothing
    off = S | {"enabled": False}
    assert crypto.arm("SOL/USD", 70.1, 70.05, off, PLAN)[0] == crypto.desk.SKIPPED


def test_weekend_rule_no_entries_from_friday_noon_and_flat_from_friday_3pm(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fri_1pm = dt.datetime(2026, 11, 27, 13, tzinfo=crypto.desk.NY)
    assert crypto.weekend(fri_1pm) and not crypto.weekend(fri_1pm - dt.timedelta(hours=2))
    assert crypto.arm("SOL/USD", 70.1, 70.05, S, PLAN, fri_1pm)[0] == crypto.desk.SKIPPED
    p = {"entry": 70.0, "stop": 68.0, "t1": 74.0, "t2": 78.0, "stop_id": "s1"}
    p["opened_at"] = (fri_1pm - dt.timedelta(hours=3)).isoformat()
    steps = crypto.manage(
        {"open": {"SOL/USD": p}},
        {"SOL/USD": 71.0},
        {"SOL/USD": 10.0},
        S,
        fri_1pm + dt.timedelta(hours=2),
    )
    assert steps[0]["do"] == "close" and "weekend" in steps[0]["why"]
