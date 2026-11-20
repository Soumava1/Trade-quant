"""Trade Scout: the orchestrator over the vendored trader-* skills, run on saved data."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCOUT = ROOT / ".claude" / "skills" / "trade-scout" / "scout.py"
FIXTURES = ROOT / "tests" / "fixtures" / "trade_scout"
EXAMPLE_CONFIG = ROOT / ".claude" / "skills" / "trader-budget" / "config.example.json"
NOW = "2026-10-19 10:15"


def load_scout() -> ModuleType:
    spec = importlib.util.spec_from_file_location("trade_scout", SCOUT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["trade_scout"] = module
    spec.loader.exec_module(module)
    return module


scout = load_scout()


def run_offline(tmp_path: Path, *extra: str) -> tuple[int, dict]:  # type: ignore[type-arg]
    trader = tmp_path / "trader"
    trader.mkdir()
    shutil.copyfile(EXAMPLE_CONFIG, trader / "config.json")
    out = tmp_path / "run"
    code = scout.main(
        [
            "--trader-dir",
            str(trader),
            "--out",
            str(out),
            "--offline-dir",
            str(FIXTURES),
            "--now",
            NOW,
            "--replays",
            "2000",
            *extra,
        ]
    )
    return code, json.loads((out / "summary.json").read_text())


def test_tsla_reel_runs_every_step_and_is_not_high_conviction(tmp_path: Path) -> None:
    code, summary = run_offline(tmp_path, "--target", "1R")
    assert summary["ok"] is True
    assert summary["scan"]["hits"] == 41
    tsla = next(c for c in summary["candidates"] if c["symbol"] == "TSLA")
    # The reel's trade: entry 364.18 at 10:15, or-mid stop 358.32, 1R target 370.04.
    assert tsla["trigger"] == {"time": "10:15 AM", "price": 364.18, "pct_over": 0.55}
    assert tsla["plan"]["stop"] == 358.32
    assert tsla["plan"]["target"] == 370.04
    # The pack sizes 8 shares here; the low-conviction cap (10 % of $5,000) allows 1.
    assert tsla["size"]["qty"] == 1
    assert tsla["verdict"]
    # The odds lean a little toward the target, but the same rule lost money over the past
    # 59 sessions (avg R below zero), so it is not high conviction.
    assert tsla["tier"] != scout.HIGH
    assert any(r.startswith("history:") for r in tsla["reasons"])
    assert code == 1
    assert (tmp_path / "run" / "summary.md").read_text().startswith("# Trade Scout")


def test_missing_data_rejects_with_the_reason(tmp_path: Path) -> None:
    _, summary = run_offline(tmp_path)
    others = [c for c in summary["candidates"] if c["symbol"] != "TSLA"]
    assert others
    assert all(c["tier"] == scout.REJECT for c in others)
    assert all(c["reasons"][0].startswith("entry:") for c in others)


def test_stale_trigger_at_a_later_clock(tmp_path: Path) -> None:
    trader = tmp_path / "trader"
    trader.mkdir()
    shutil.copyfile(EXAMPLE_CONFIG, trader / "config.json")
    opts = scout.Options(
        trader_dir=trader,
        run_dir=tmp_path / "run",
        symbols=None,
        watchlist=None,
        max_candidates=1,
        top=3,
        stop="or-mid",
        target="2R",
        replays=500,
        seed=19,
        now="2026-10-19 10:40",
        offline_dir=FIXTURES,
        min_price=None,
        news_hours=24,
        th=scout.Thresholds(max_trigger_age=10, late_entry=False),
    )
    cand = scout.Candidate(symbol="TSLA", scan={"symbol": "TSLA"})
    assert scout.step_entry(opts, cand) is False
    assert cand.reasons[0].startswith("stale trigger: fired 25 min ago")

    # With late entry on, the same breakout is judged by where the price is now.
    opts.th = scout.Thresholds(max_trigger_age=10, late_entry=True, max_chase_pct=5.0)
    late = scout.Candidate(symbol="TSLA", scan={"symbol": "TSLA"})
    assert scout.step_entry(opts, late) is True
    # The 10:35 bar closed at 10:40 AM at 365.70, still 0.97 % over the 362.18 range high.
    assert late.late == {
        "price": 365.7,
        "bar": 13,
        "time": "10:40 AM",
        "first_trigger": "10:15 AM",
        "age_min": 25,
        "over_pct": 0.97,
    }
    # The plan and the Time Machine start from that late bar, not the 10:15 trigger.
    late.news = []
    scout.step_candidate(opts, late)
    assert late.plan is not None and late.plan["entry"] == 365.7
    assert late.plan["entry_time"] == "10:40 AM"
    assert late.tm is not None and late.tm["plan"]["entry_bar"] == 13

    # Too far over the range: extended, no late entry.
    opts.th = scout.Thresholds(max_trigger_age=10, late_entry=True, max_chase_pct=0.5)
    chased = scout.Candidate(symbol="TSLA", scan={"symbol": "TSLA"})
    assert scout.step_entry(opts, chased) is False
    assert chased.reasons[0].startswith("extended: fired at 10:15 AM, now +0.97%")


def test_paper_only(tmp_path: Path) -> None:
    trader = tmp_path / "trader"
    trader.mkdir()
    cfg = json.loads(EXAMPLE_CONFIG.read_text())
    cfg["paper"] = False
    (trader / "config.json").write_text(json.dumps(cfg))
    code = scout.main(
        [
            "--trader-dir",
            str(trader),
            "--out",
            str(tmp_path / "run"),
            "--offline-dir",
            str(FIXTURES),
            "--now",
            NOW,
        ]
    )
    assert code == 2
    assert not (tmp_path / "run" / "summary.json").exists()


def candidate_with(
    p_target: float, p_stop: float, mc_avg_r: float, hist: dict[str, float], vol_x: float = 1.5
) -> scout.Candidate:
    c = scout.Candidate(symbol="DEMO", scan={})
    c.entry = {"trigger": {"volume_x": vol_x}, "minutes_since": 0}
    c.plan = {"target_r": 2.0}
    c.tm = {
        "mc": {
            "p_target": p_target,
            "p_stop": p_stop,
            "p_neither": 1 - p_target - p_stop,
            "avg_R": mc_avg_r,
        },
        "history": hist,
        "verdict": "test",
    }
    return c


def test_score_high_when_every_gate_passes() -> None:
    c = candidate_with(0.30, 0.30, 0.25, {"triggered": 30, "target": 14, "stop": 12, "avg_R": 0.3})
    scout.score_candidate(c, scout.Thresholds())
    # 2R break-even is 33.3 %; half the decided replays hit the target: +16.7 points of edge.
    assert c.metrics["edge"] == pytest.approx(0.1667, abs=1e-4)
    assert c.tier == scout.HIGH, c.reasons
    assert c.score >= 55


def test_score_watch_when_history_is_thin() -> None:
    c = candidate_with(0.30, 0.30, 0.25, {"triggered": 4, "target": 3, "stop": 1, "avg_R": 0.8})
    scout.score_candidate(c, scout.Thresholds())
    assert c.tier == scout.WATCH
    assert any("fewer than 10" in r for r in c.reasons)


def test_score_reject_when_odds_favour_the_stop() -> None:
    c = candidate_with(0.10, 0.50, -0.4, {"triggered": 30, "target": 5, "stop": 20, "avg_R": -0.5})
    scout.score_candidate(c, scout.Thresholds())
    assert c.tier == scout.REJECT


def test_news_veto_and_caution() -> None:
    vetoes, cautions = scout.news_flags(
        [
            {"source": "SEC", "title": "8-K", "tags": [], "items": ["4.02"]},
            {"source": "Yahoo", "title": "Trading halted", "tags": ["halt"]},
            {"source": "Yahoo", "title": "Q3 beats", "tags": ["earnings"]},
            {"source": "Yahoo", "title": "Nothing much", "tags": []},
            {"source": "Yahoo", "title": "DOJ closes probe", "tags": ["legal", "regulatory"]},
        ]
    )
    assert len(vetoes) == 2
    assert len(cautions) == 2
    assert "earnings" in cautions[0]
    assert "legal, regulatory" in cautions[1]


def test_batch_risk_check_passes_the_reel_order(tmp_path: Path) -> None:
    trader = tmp_path / "trader"
    trader.mkdir()
    shutil.copyfile(EXAMPLE_CONFIG, trader / "config.json")
    opts = scout.Options(
        trader_dir=trader,
        run_dir=tmp_path / "run",
        symbols=None,
        watchlist=None,
        max_candidates=1,
        top=3,
        stop="or-mid",
        target="1R",
        replays=500,
        seed=19,
        now=NOW,
        offline_dir=None,
        min_price=None,
        news_hours=24,
        th=scout.Thresholds(),
    )
    order = {
        "symbol": "TSLA",
        "side": "buy",
        "qty": "8",
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit_limit_price": "370.04",
        "stop_loss_stop_price": "358.32",
        "plan": {
            "entry": 364.18,
            "stop": 358.32,
            "target": 370.04,
            "risk_per_share": 5.86,
            "max_loss": 46.88,
            "setup": "orb",
            "entry_time": "10:15 AM",
            "flat_by": "15:55",
        },
    }
    first = scout.Candidate(symbol="TSLA", scan={}, tier=scout.HIGH, order=order)
    # The same symbol twice in one batch is a duplicate: the second must be blocked.
    second = scout.Candidate(symbol="TSLA", scan={}, tier=scout.HIGH, order=dict(order))
    scout.step_risk(opts, [first, second])
    assert first.risk is not None and first.risk["verdict"] == "PASS"
    assert first.tier == scout.HIGH
    assert second.tier == scout.WATCH
    assert any("risk BLOCK" in r for r in second.reasons)
    # The user's own trader/ folder is never written.
    assert sorted(p.name for p in trader.iterdir()) == ["config.json"]


def test_position_cap_staggers_with_conviction() -> None:
    caps = scout.parse_caps("95:75,85:55,75:35,65:20,0:10")
    assert scout.position_cap(97, caps, 75) == 75
    assert scout.position_cap(95, caps, 75) == 75
    assert scout.position_cap(90, caps, 75) == 55
    assert scout.position_cap(85, caps, 75) == 55
    assert scout.position_cap(80, caps, 75) == 35
    assert scout.position_cap(60, caps, 75) == 10
    # The user's own max_position_pct is always the ceiling.
    assert scout.position_cap(99, caps, 50) == 50


def test_sizing_uses_the_conviction_cap(tmp_path: Path) -> None:
    _, summary = run_offline(tmp_path, "--target", "1R")
    tsla = next(c for c in summary["candidates"] if c["symbol"] == "TSLA")
    # TSLA scores ~20 on the reel day: the 10 % row, so $500 at most of the $5,000 budget.
    assert tsla["position_cap_pct"] == 10
    assert tsla["size"]["qty"] == 1
    assert tsla["size"]["bound_by"] == "position cap"


def test_config_scout_section_sets_defaults_and_max_price(tmp_path: Path) -> None:
    trader = tmp_path / "trader"
    trader.mkdir()
    cfg = json.loads(EXAMPLE_CONFIG.read_text())
    cfg["scout"] = {
        "stop": "or-low",
        "top": 2,
        "max_price": 100,
        "position_caps": "85:75,70:60,55:45,0:10",
    }
    (trader / "config.json").write_text(json.dumps(cfg))
    out = tmp_path / "run"
    scout.main(
        [
            "--trader-dir",
            str(trader),
            "--out",
            str(out),
            "--offline-dir",
            str(FIXTURES),
            "--now",
            NOW,
            "--replays",
            "500",
        ]
    )
    summary = json.loads((out / "summary.json").read_text())
    assert summary["settings"]["stop"] == "or-low"
    assert summary["settings"]["top"] == 2
    assert summary["settings"]["position_caps"][0] == [85.0, 75.0]
    # TSLA at $364 is over the $100 ceiling, so it never reaches the pipeline.
    assert "TSLA" not in [c["symbol"] for c in summary["candidates"]]


def test_monthly_stop_blocks_the_run(tmp_path: Path) -> None:
    trader = tmp_path / "trader"
    (trader / "state").mkdir(parents=True)
    cfg = json.loads(EXAMPLE_CONFIG.read_text())
    cfg["max_monthly_loss_usd"] = 80
    (trader / "config.json").write_text(json.dumps(cfg))
    fills = [
        {
            "status": "filled",
            "date": "2026-10-05",
            "symbol": "XYZ",
            "side": "buy",
            "qty": "10",
            "price": 50.0,
        },
        {
            "status": "filled",
            "date": "2026-10-05",
            "symbol": "XYZ",
            "side": "sell",
            "qty": "10",
            "price": 41.0,
        },
    ]
    (trader / "state" / "broker.json").write_text(json.dumps({"orders": fills}))
    assert scout.month_realized_pnl(trader / "state" / "broker.json", "2026-10") == -90.0
    out = tmp_path / "run"
    code = scout.main(
        [
            "--trader-dir",
            str(trader),
            "--out",
            str(out),
            "--offline-dir",
            str(FIXTURES),
            "--now",
            NOW,
        ]
    )
    assert code == 2
    assert "monthly stop" in json.loads((out / "summary.json").read_text())["error"]


def test_tries_each_target_and_keeps_the_best_expected_r(tmp_path: Path) -> None:
    _, summary = run_offline(tmp_path, "--target", "1R,2R,3R")
    tsla = next(c for c in summary["candidates"] if c["symbol"] == "TSLA")
    tried = {t["target"]: t["avg_R"] for t in tsla["targets_tried"]}
    assert set(tried) == {"or-mid / 1R", "or-mid / 2R", "or-mid / 3R"}
    best = max(tried, key=lambda k: tried[k])
    assert tsla["plan"]["target_r"] == float(best.split(" / ")[1].rstrip("R"))
    assert tsla["metrics"]["mc_avg_r"] == tried[best]


def test_stock_in_play_features_on_the_reel_day() -> None:
    import features  # type: ignore[import-not-found]

    chart = features.load_chart(FIXTURES / "timemachine" / "TSLA.json.gz")
    f = features.compute(chart, "2026-10-19", entry_bar=8, or_minutes=15)
    assert f["sessions"] == 14
    assert f["rvol"] == pytest.approx(1.02, abs=0.01)
    assert f["atr"] == pytest.approx(10.86, abs=0.01)
    assert f["adv"] > 1_000_000
    assert f["above_vwap"] is True
    assert f["gap_pct"] == pytest.approx(0.79, abs=0.01)


def test_atr_stop_spec_uses_ten_percent_of_daily_atr() -> None:
    c = scout.Candidate(symbol="TSLA", scan={})
    c.entry = {"trigger": {"price": 364.18, "bar_index": 8}}
    c.features = {"atr": 10.8585}
    # 364.18 - 0.1 x 10.8585 = 363.094..., rounded down to the cent.
    assert scout.stop_spec("atr-d:0.1", c) == "price:363.09"
    assert scout.stop_spec("or-low", c) == "or-low"


def test_low_rvol_is_not_in_play(tmp_path: Path) -> None:
    _, summary = run_offline(tmp_path, "--target", "1R", "--min-rvol", "1.5")
    tsla = next(c for c in summary["candidates"] if c["symbol"] == "TSLA")
    assert tsla["reasons"][0].startswith("not in play: opening-range volume 1.02x")


def test_journal_simulates_stop_target_and_close() -> None:
    import journal  # type: ignore[import-not-found]

    base = {
        "entry": 100.0,
        "stop": 99.0,
        "target": 102.0,
        "entry_bar": 0,
        "flat_by": "15:55",
        "qty": 10,
    }
    bars = [(570, 100, 100.5, 99.8, 100.2, 1), (575, 100.2, 102.5, 100.1, 102.2, 1)]
    hit = journal.simulate(base, bars)
    assert hit["exit_how"] == "target" and hit["r"] == 2.0 and hit["pnl"] == 20.0
    gap = journal.simulate(base, [(570, 0, 0, 0, 0, 1), (575, 98.0, 98.5, 97.5, 98.2, 1)])
    assert gap["exit_how"] == "stop (gap)" and gap["r"] == -2.0
    flat = journal.simulate(base, [(570, 0, 0, 0, 0, 1), (575, 100.1, 100.6, 99.5, 100.5, 1)])
    assert flat["exit_how"] == "flat by close" and flat["r"] == 0.5


def test_journal_kpis() -> None:
    import journal  # type: ignore[import-not-found]

    rows = [
        {"status": "settled", "date": "2026-11-02", "r": 2.0, "pnl": 30.0, "exit_how": "target"},
        {"status": "settled", "date": "2026-11-03", "r": -1.0, "pnl": -15.0, "exit_how": "stop"},
        {"status": "settled", "date": "2026-11-04", "r": -1.0, "pnl": -15.0, "exit_how": "stop"},
        {
            "status": "settled",
            "date": "2026-11-05",
            "r": 0.5,
            "pnl": 7.5,
            "exit_how": "flat by close",
        },
    ]
    k = journal.kpis(rows, budget=1000)
    assert k["trades"] == 4 and k["win_rate"] == 0.5
    assert k["avg_r"] == pytest.approx(0.125)
    assert k["profit_factor"] == pytest.approx(37.5 / 30, abs=0.01)
    assert k["max_drawdown_usd"] == 30.0 and k["longest_losing_streak"] == 2
    assert k["return_pct"] == pytest.approx(0.75)


def test_paper_pick_when_no_high(tmp_path: Path) -> None:
    trader = tmp_path / "trader"
    trader.mkdir()
    shutil.copyfile(EXAMPLE_CONFIG, trader / "config.json")
    opts = scout.Options(
        trader_dir=trader,
        run_dir=tmp_path / "run",
        symbols=None,
        watchlist=None,
        max_candidates=1,
        top=3,
        stop="or-mid",
        target="1R",
        replays=500,
        seed=19,
        now=NOW,
        offline_dir=None,
        min_price=None,
        news_hours=24,
        th=scout.Thresholds(),
    )
    order = {
        "symbol": "TSLA",
        "side": "buy",
        "qty": "1",
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit_limit_price": "370.04",
        "stop_loss_stop_price": "358.32",
        "plan": {
            "entry": 364.18,
            "stop": 358.32,
            "target": 370.04,
            "risk_per_share": 5.86,
            "max_loss": 5.86,
            "setup": "orb",
            "entry_time": "10:15 AM",
            "flat_by": "15:55",
        },
    }
    good = scout.Candidate(
        symbol="TSLA",
        scan={},
        tier=scout.WATCH,
        status="scored",
        order=order,
        score=40.0,
        metrics={"mc_avg_r": 0.05, "edge": 0.03},
    )
    weak = scout.Candidate(
        symbol="AAA",
        scan={},
        tier=scout.WATCH,
        status="scored",
        order=order,
        score=45.0,
        metrics={"mc_avg_r": -0.01, "edge": 0.02},
    )
    scout.step_pick(opts, [good, weak])
    assert good.tier == scout.PICK
    assert weak.tier == scout.WATCH
