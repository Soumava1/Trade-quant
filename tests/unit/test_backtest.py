"""trade-backtest: the desk's trade on synthetic 5-minute sessions."""

import json
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / ".claude" / "skills" / "trade-backtest")
)
import backtest  # noqa: E402

OPEN = 9 * 60 + 30


def session(path):
    """Bars from (high, low, close) tuples, one per 5 minutes from 9:30; open = prior close."""
    bars, prev = [], path[0][2]
    for i, (h, lo, c) in enumerate(path):
        bars.append((OPEN + 5 * i, prev, h, lo, c, 1000.0))
        prev = c
    return bars


def flat_to_close(bars, price):
    m = bars[-1][0]
    while m < 16 * 60 - 5:
        m += 5
        bars.append((m, price, price, price, price, 1000.0))
    return bars


# 9:30 range 100-99 (high 100, low 99); ATR 2 -> stop floor 0.5 under the entry.
RANGE = [(100, 99, 99.5), (99.8, 99.4, 99.6), (99.9, 99.5, 99.7)]  # 9:30, 9:35, 9:40


def test_t1_then_t2_is_half_one_r_plus_half_k():
    # entry 100.01, stop min(99, 99.51) = 99 -> R 1.01; T1 101.02, T2 (2R) 102.03
    bars = flat_to_close(
        session(RANGE + [(100.2, 99.7, 100.1), (101.5, 100.5, 101.2), (102.5, 101.0, 102.2)]), 102.2
    )
    t = backtest.session_trade(bars, atr=2.0, k=2.0)
    assert t["entry"] == 100.01 and t["stop"] == 99.0 and t["t1"] == 101.02
    assert t["r"] == 1.5 and t["how"] == "T1 then T2"  # 0.5 x 1 + 0.5 x 2


def test_t1_then_back_to_breakeven_keeps_half_an_r():
    bars = flat_to_close(
        session(RANGE + [(100.2, 99.7, 100.1), (101.5, 100.5, 101.2), (101.3, 99.9, 100.0)]), 100.0
    )
    t = backtest.session_trade(bars, atr=2.0, k=2.0)
    assert t["r"] == 0.5 and t["how"] == "T1 then breakeven"


def test_a_bar_that_hits_t1_and_dips_under_the_entry_stops_the_rest_at_breakeven():
    # entry 100.01, stop 99, T1 101.02: one bar spans 99.80-101.50, then T2 trades.
    # Ties go against the trade: the rest is out at breakeven in that bar, never at T2.
    bars = flat_to_close(
        session(RANGE + [(100.2, 99.7, 100.1), (101.5, 99.8, 101.2), (102.5, 101.0, 102.2)]),
        102.2,
    )
    t = backtest.session_trade(bars, atr=2.0, k=2.0)
    assert t["r"] == 0.5 and t["how"] == "T1 then breakeven"


def test_stop_floor_widens_a_tight_range_and_a_stop_is_minus_one():
    tight = [(100, 99.9, 99.95), (99.98, 99.92, 99.95), (99.99, 99.93, 99.96)]
    bars = flat_to_close(session(tight + [(100.2, 99.95, 100.1), (100.1, 99.0, 99.2)]), 99.2)
    t = backtest.session_trade(bars, atr=2.0, k=3.0)
    assert t["stop"] == 99.51  # 100.01 - 0.25 x 2, not the 99.90 range low
    assert t["r"] == -1.0 and t["how"] == "stop"


def test_no_fill_before_945_or_when_it_would_chase():
    # breaks out at 9:35, before the desk may enter, and is back under the range by 9:45
    early = session([(100, 99, 99.5), (101, 99.5, 99.6), (99.8, 99.3, 99.5), (99.7, 99.2, 99.4)])
    assert backtest.session_trade(flat_to_close(early, 99.4), atr=2.0, k=2.0) is None
    # 0.8 % over the trigger at 9:45 is within the 1 % chase limit: it fills at the open
    late = session(
        [(100, 99, 99.5), (101, 99.5, 100.8), (100.9, 100.7, 100.8), (101, 100.7, 100.9)]
    )
    assert backtest.session_trade(flat_to_close(late, 100.9), atr=2.0, k=2.0)["entry"] == 100.8
    gap = session(RANGE + [(103, 102, 102.5)])  # 9:45 opens 2.5 % over the trigger
    gap[-1] = (gap[-1][0], 102.6, 103, 102, 102.5, 1000.0)
    assert backtest.session_trade(flat_to_close(gap, 102.5), atr=2.0, k=2.0) is None


def test_a_cached_result_is_reused_only_for_the_same_rule(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []

    def fake(chart, sym, **rule):
        calls.append(rule["min_stop_atr"])
        best = {"n": 9, "avg_r": 0.1, "win_rate": 0.5, "total_r": 0.9, "max_dd_r": -1.0}
        return {"symbol": sym, "best_t2": "2R", "best": best}

    monkeypatch.setattr(backtest, "backtest_symbol", fake)
    monkeypatch.setattr(backtest, "get_chart", lambda sym, off: {})
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"scout": {"min_stop_atr": 0.25}}))
    backtest.main(["--symbols", "X", "--config", str(cfg)])
    backtest.main(["--symbols", "X", "--config", str(cfg)])  # same rule: from the cache
    cfg.write_text(json.dumps({"scout": {"min_stop_atr": 0.5}}))
    backtest.main(["--symbols", "X", "--config", str(cfg)])  # new rule: run again
    assert calls == [0.25, 0.5]


def test_rank_keeps_winners_first_then_thin_and_drops_losers():
    def res(sym, n, avg):
        return {"symbol": sym, "best_t2": "2R", "best": {"n": n, "avg_r": avg}}

    ranked = backtest.rank(
        [res("A", 20, 0.1), res("B", 3, 0.9), res("C", 30, -0.2), res("D", 25, 0.3)]
    )
    assert [r["symbol"] for r in ranked] == ["D", "A", "B"]
    assert [r["verdict"] for r in ranked] == ["keep", "keep", "thin"]


def test_cli_rewrites_the_shortlist_from_offline_charts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    charts = tmp_path / "charts"
    charts.mkdir()
    day = 86400
    base = 1795000000 - (1795000000 % day) + 14 * 3600 + 30 * 60  # 9:30 New York (UTC-5)
    for sym, winner in (("WIN", True), ("LOSE", False)):
        ts, q = [], {k: [] for k in ("open", "high", "low", "close", "volume")}
        for d in range(25):
            path = RANGE + (
                [(100.2, 99.7, 100.1), (101.5, 100.5, 101.2), (102.5, 101, 102.2)]
                if winner
                else [(100.2, 99.95, 100.1), (100.1, 98.0, 98.2)]
            )
            bars = flat_to_close(session(path), path[-1][2])
            for m, o, h, lo, c, v in bars:
                ts.append(base + d * day + (m - OPEN) * 60)
                for k, x in zip(q, (o, h, lo, c, v), strict=True):
                    q[k].append(x)
        chart = {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [q]}}]}}
        (charts / f"{sym}.json").write_text(json.dumps(chart))
    sl = tmp_path / "shortlist.json"
    sl.write_text(json.dumps({"date": "x", "hits": [{"symbol": "LOSE"}, {"symbol": "WIN"}]}))
    assert (
        backtest.main(["--shortlist", str(sl), "--drop-losers", "--offline-dir", str(charts)]) == 0
    )
    doc = json.loads(sl.read_text())
    assert [h["symbol"] for h in doc["hits"]] == ["WIN"] and doc["dropped"] == ["LOSE"]
    assert doc["hits"][0]["backtest"]["verdict"] == "keep"


def test_short_mirror_t1_then_t2_and_a_stop_is_minus_one():
    # range 100-99 (low 99); trigger 98.99, stop max(100, 98.99 + 0.5) = 100 -> R 1.01
    down = RANGE + [(99.6, 98.9, 99.0), (98.5, 97.5, 97.8), (97.6, 96.8, 97.0)]
    t = backtest.session_trade_short(flat_to_close(session(down), 97.0), atr=2.0, k=2.0)
    assert t["entry"] == 98.99 and t["stop"] == 100.0 and t["t1"] == 97.98
    assert t["r"] == 1.5 and t["how"] == "T1 then T2" and t["side"] == "sell"
    squeeze = RANGE + [(99.6, 98.9, 99.0), (100.5, 99.0, 100.3)]
    t = backtest.session_trade_short(flat_to_close(session(squeeze), 100.3), atr=2.0, k=2.0)
    assert t["r"] == -1.0 and t["how"] == "stop"


def test_cli_keeps_a_name_for_shorts_when_only_its_short_side_wins(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    charts = tmp_path / "charts"
    charts.mkdir()
    day = 86400
    base = 1795000000 - (1795000000 % day) + 14 * 3600 + 30 * 60
    ts, q = [], {k: [] for k in ("open", "high", "low", "close", "volume")}
    for d in range(25):  # every session breaks down through the range low and keeps falling
        path = RANGE + [(99.6, 98.9, 99.0), (98.5, 97.5, 97.8), (97.6, 96.8, 97.0)]
        for m, o, h, lo, c, v in flat_to_close(session(path), 97.0):
            ts.append(base + d * day + (m - OPEN) * 60)
            for k, x in zip(q, (o, h, lo, c, v), strict=True):
                q[k].append(x)
    chart = {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [q]}}]}}
    (charts / "DOWN.json").write_text(json.dumps(chart))
    sl = tmp_path / "shortlist.json"
    sl.write_text(json.dumps({"date": "x", "hits": [{"symbol": "DOWN"}]}))
    args = ["--shortlist", str(sl), "--drop-losers", "--offline-dir", str(charts), "--shorts"]
    backtest.main(args)
    doc = json.loads(sl.read_text())
    assert [h["symbol"] for h in doc["hits"]] == ["DOWN"] and doc["dropped"] == []
    assert doc["hits"][0]["short_backtest"]["verdict"] == "keep"
