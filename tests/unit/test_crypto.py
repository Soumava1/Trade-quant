"""crypto-desk: the 24-hour breakout, its exits, sizing and the live checks."""

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".claude" / "skills" / "crypto-desk"))
import crypto  # noqa: E402

S = crypto.DEFAULTS | {"fee_pct": 0.0}
T0 = 1_795_000_000 - (1_795_000_000 % (4 * 3600))  # on the 4-hour check grid


def bars_from(closes, spread=0.002):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        hi, lo = max(prev, c) * (1 + spread), min(prev, c) * (1 - spread)
        out.append((T0 + 3600 * i, prev, hi, lo, c))
        prev = c
    return out


def test_t1_then_breakeven_and_fees_in_r():
    plan = {"entry": 100.0, "stop": 99.0}
    # +2R at the first check (4 bars), then back down through the entry
    bars = [(T0, 100, 100, 100, 100)] + [
        (T0 + 3600 * i, 100, 102.1, 100.5, 102.0) for i in (1, 2, 3, 4)
    ]
    bars += [(T0 + 3600 * 5, 101, 101, 99.5, 99.6)]
    t = crypto.trade_from(bars, 0, plan, S)
    assert t["r"] == 1.0 and t["how"] == "T1 then breakeven"  # 0.5 x 2R + 0.5 x 0
    t = crypto.trade_from(bars, 0, plan, S | {"fee_pct": 0.25})
    assert t["r"] == round(1.0 - 0.005 * 100 / 1.0, 3)  # 0.5 % round trip = 0.5R here


def test_a_stop_gap_fills_at_the_open_and_the_24h_limit_closes_the_rest():
    plan = {"entry": 100.0, "stop": 99.0}
    gap = [(T0, 100, 100, 100, 100), (T0 + 3600, 98.0, 98.5, 97.5, 98.0)]
    assert crypto.trade_from(gap, 0, plan, S)["r"] == -2.0  # opened 2R under: worse than -1R
    flat = [(T0, 100, 100, 100, 100)] + [
        (T0 + 3600 * i, 100.2, 100.4, 99.9, 100.3) for i in range(1, 30)
    ]
    t = crypto.trade_from(flat, 0, plan, S)
    assert t["how"] == "24h exit" and t["r"] == 0.3


def test_signal_needs_a_fresh_breakout_above_the_trend_and_no_chase():
    up = [100 + 0.1 * i for i in range(80)] + [
        100 + 0.1 * 79 + 0.9
    ]  # a jump just over the 24h high
    bars = bars_from(up)
    e = crypto.ema([b[4] for b in bars], 72)
    a = crypto.atr(bars, 14)
    plan = crypto.signal_at(bars, len(bars) - 1, S, e, a)
    assert plan and plan["stop"] < plan["entry"] and plan["level"] < plan["entry"]
    assert crypto.signal_at(bars, len(bars) - 1, S | {"max_chase_pct": 0.1}, e, a) is None
    down = list(reversed(up))
    bars = bars_from(down)
    e, a = crypto.ema([b[4] for b in bars], 72), crypto.atr(bars, 14)
    assert crypto.signal_at(bars, len(bars) - 1, S, e, a) is None  # under the trend


def test_arm_sizes_to_the_risk_and_refuses_thin_books_and_full_slots(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plans = {"SOL/USD": {"entry": 70.0, "stop": 68.0, "level": 69.9}}
    code, order = crypto.arm("SOL/USD", 70.1, 70.05, S, plans)
    assert code == crypto.desk.READY and order["type"] == "market"
    qty = float(order["qty"])
    assert abs(qty * (70.1 - 68.0) - 37.5) < 0.05  # 0.75 % of $5,000 at the stop
    assert qty * 70.1 <= 5000 * 0.40
    assert crypto.arm("SOL/USD", 70.1, 69.5, S, plans)[0] == crypto.desk.SKIPPED  # 0.86 % spread
    crypto.save_state({"open": {f"X{i}": {} for i in range(3)}, "closed": []})
    assert crypto.arm("SOL/USD", 70.1, 70.05, S, plans)[0] == crypto.desk.SKIPPED  # 3 open
    log = (tmp_path / "trades-log.md").read_text(encoding="utf-8")
    assert "Spread 0.86% over the 0.3% limit" in log and "3 crypto trades open" in log


def test_manage_takes_t1_then_closes_on_t2_or_the_24h_limit():
    now = dt.datetime(2026, 11, 26, 12, tzinfo=dt.UTC)
    p = {"entry": 70.0, "stop": 68.0, "t1": 74.0, "t2": 78.0, "stop_id": "s1"}
    st = {"open": {"SOL/USD": p | {"opened_at": (now - dt.timedelta(hours=5)).isoformat()}}}
    step = crypto.manage(st, {"SOL/USD": 74.5}, {"SOL/USD": 10.0}, S, now)[0]
    assert step["do"] == "t1" and step["sell"] == 5.0 and step["cancel"] == "s1"
    assert step["new_stop"]["stop_price"] == "70" and step["new_stop"]["qty"] == "5"
    st["open"]["SOL/USD"]["opened_at"] = (now - dt.timedelta(hours=25)).isoformat()
    step = crypto.manage(st, {"SOL/USD": 71.0}, {"SOL/USD": 10.0}, S, now)[0]
    assert step["do"] == "close" and step["event"] == "time"
    assert crypto.manage(st, {}, {}, S, now)[0]["do"] == "stopped"


def test_done_records_pnl_and_the_daily_limit_then_blocks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    now = dt.datetime(2026, 11, 26, 12, tzinfo=dt.UTC)
    p = {"entry": 70.0, "stop": 68.0, "t1": 74.0, "t2": 78.0, "stop_id": "s1", "opened_at": "x"}
    crypto.save_state({"open": {"SOL/USD": p}, "closed": []})
    line = crypto.done("SOL/USD", "stopped", 40.0, 68.0, None, now)
    assert "| SOL/USD | EXIT | 40 sh |" in line and "Stop filled: -1.0R (P&L $-80.00)" in line
    st = json.loads((tmp_path / "trader" / "crypto" / "positions.json").read_text())
    assert st["open"] == {} and st["closed"][0]["pnl"] == -80.0
    plans = {"SOL/USD": {"entry": 70.0, "stop": 68.0, "level": 69.9}}
    assert crypto.arm("SOL/USD", 70.1, 70.05, S, plans, now)[0] == crypto.desk.SKIPPED


def test_guards_no_crypto_section_a_stale_backtest_and_the_kill_switch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"paper": True}))
    try:
        crypto.settings(cfg)
        raise AssertionError("no crypto section must refuse")
    except SystemExit as e:
        assert 'no "crypto" section' in str(e)
    now = dt.datetime(2026, 11, 26, 12, tzinfo=dt.UTC)
    st = tmp_path / "trader" / "crypto"
    st.mkdir(parents=True)
    doc = {
        "at": (now - dt.timedelta(hours=10)).isoformat(),
        "coins": {"SOL/USD": {"verdict": "keep"}},
    }
    (st / "backtest.json").write_text(json.dumps(doc))
    assert crypto.kept_coins(S, now) == ["SOL/USD"]
    assert crypto.kept_coins(S, now + dt.timedelta(hours=48)) == []  # 58h old: trades nothing
    plans = {"SOL/USD": {"entry": 70.0, "stop": 68.0, "level": 69.9}}
    assert (
        crypto.arm("SOL/USD", 70.1, 70.05, S | {"enabled": False}, plans)[0] == crypto.desk.SKIPPED
    )
