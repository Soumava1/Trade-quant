"""optimize: the rules-and-rejects log and the bounded, one-step-a-night gate tuning."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".claude" / "skills" / "trade-scout"))
import optimize  # noqa: E402


def test_reasons_map_to_gates():
    assert optimize.gate_of("not in play: opening-range volume 0.62x normal") == "rvol"
    assert optimize.gate_of("extended: fired at 9:45 AM, now +1.4% over") == "chase"
    assert optimize.gate_of("Time Machine edge -2.0 pts vs break-even") == "edge"
    assert optimize.gate_of("below VWAP: price 26.88 under VWAP 26.92") == "vwap"
    assert optimize.gate_of("something new") == "other"


def test_loosen_on_winning_rejects_tighten_back_on_losers_and_respect_bounds():
    win = {"chase": {"n": 20, "avg_r": 0.3, "win_rate": 0.6}}
    assert optimize.propose(win, {"max_chase_pct": 1.0})[0]["to"] == 1.25
    assert optimize.propose(win, {"max_chase_pct": 2.0}) == []  # at the bound
    lose = {"chase": {"n": 20, "avg_r": -0.3, "win_rate": 0.3}}
    assert optimize.propose(lose, {"max_chase_pct": 1.5})[0]["to"] == 1.25
    assert optimize.propose(lose, {"max_chase_pct": 1.0}) == []  # never past the default
    few = {"chase": {"n": 5, "avg_r": 0.9, "win_rate": 1.0}}
    assert optimize.propose(few, {}) == []  # too few replays
    rvol = {"rvol": {"n": 30, "avg_r": 0.2, "win_rate": 0.55}}
    assert optimize.propose(rvol, {"min_rvol": 1.0})[0]["to"] == 0.9  # loosening lowers it


def test_passed_names_losing_raises_min_score():
    p = {"passed": {"n": 16, "avg_r": -0.2, "win_rate": 0.3}}
    m = optimize.propose(p, {"min_score": 55})
    assert m[0]["key"] == "min_score" and m[0]["to"] == 60


def test_append_log_writes_rules_and_each_reject(tmp_path):
    log = tmp_path / "scout-log.md"
    summary = {
        "as_of": "2026-11-25 10:02 ET",
        "settings": {"stop": "orl-atr:0.25", "target": "2R,3R,4R", "thresholds": {}},
        "candidates": [
            {"symbol": "WMT", "tier": "REJECT", "reasons": ["not in play: 0.90x normal"]},
            {"symbol": "BAX", "tier": "WATCH", "reasons": []},
        ],
    }
    optimize.append_log(log, summary, "2026-11-25_100215")
    text = log.read_text(encoding="utf-8")
    assert "Rules:" in text and "chase <= 1%" in text and "| WMT | rvol |" in text
    assert "0 HIGH, 0 PICK, 1 WATCH" in text and "BAX |" not in text


def test_review_applies_one_change_and_logs_it(tmp_path, monkeypatch):
    trader = tmp_path / "trader"
    (trader / "scout" / "2026-11-25_1000").mkdir(parents=True)
    (trader / "config.json").write_text(json.dumps({"paper": True, "scout": {}}))
    cands = [
        {"symbol": f"S{i}", "tier": "REJECT", "reasons": ["extended: +1.2%"]} for i in range(16)
    ]
    (trader / "scout" / "2026-11-25_1000" / "summary.json").write_text(
        json.dumps({"candidates": cands})
    )
    monkeypatch.setattr(optimize.backtest, "get_chart", lambda sym, off: {})
    monkeypatch.setattr(optimize, "counterfactual", lambda *a: {"r": 0.5, "how": "T1 then T2"})
    res = optimize.review(trader, "2026-11-25", apply=True)
    assert res["applied"]["key"] == "max_chase_pct" and res["applied"]["to"] == 1.25
    assert json.loads((trader / "config.json").read_text())["scout"]["max_chase_pct"] == 1.25
    log = (trader / "scout-log.md").read_text(encoding="utf-8")
    assert "**Changed:** `max_chase_pct` 1 -> 1.25" in log
    optimize.review(trader, "2026-11-25", apply=False)  # a re-run replaces today's rows
    assert len((trader / "optimizer" / "history.jsonl").read_text().splitlines()) == 16


def test_a_chase_reject_is_replayed_from_when_it_was_seen_not_from_the_breakout():
    bt = optimize.backtest
    o = 9 * 60 + 30
    # range 99-100, breaks out at 9:35, sits 1.3 % over the trigger from 10:00, then runs
    path = [(100, 99, 99.5), (100.6, 99.5, 100.5)] + [(101.4, 101.2, 101.3)] * 4
    path += [(103.5, 101.2, 103.4)] + [(103.4, 103.4, 103.4)] * 60
    bars, prev = [], 99.5
    for i, (h, lo, c) in enumerate(path):
        bars.append((o + 5 * i, prev, h, lo, c, 1000.0))
        prev = c
    assert bt.session_trade(bars, 2.0, 2.0) is None  # 9:45 opens 1.3 % over: the desk skips
    late = bt.session_trade(bars, 2.0, 2.0, entry_from="10:00", max_chase=0.02)
    assert late["entry"] == 101.3 and late["time"] == "10:00"  # bought where it was seen
