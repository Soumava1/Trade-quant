"""The paper desk's log and run lock."""

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".claude" / "skills" / "trade-scout"))
import desk  # noqa: E402


@pytest.fixture(autouse=True)
def _no_prop_guard(monkeypatch):
    """The challenge guard has its own tests (test_prop.py); here it always allows."""
    monkeypatch.setattr(desk, "prop_allows", lambda risk, now=None: (True, ""))


T = dt.datetime(2026, 11, 23, 10, 24, tzinfo=desk.NY)


def test_log_writes_header_once_and_one_line_per_decision(tmp_path):
    log = tmp_path / "trades-log.md"
    desk.log_line("skip", "mod", "-", "~141.35", "Stale: ask 2.0% over the entry", path=log, now=T)
    desk.log_line("BUY", "TSLA", "8", "364.2", "Passed every gate.", path=log, now=T)
    assert log.read_text(encoding="utf-8").splitlines() == [
        "# Paper trades log",
        "",
        "- 2026-11-23 10:24 ET | MOD | SKIP | - | ~$141.35 | Stale: ask 2.0% over the entry.",
        "- 2026-11-23 10:24 ET | TSLA | BUY | 8 sh | $364.20 | Passed every gate.",
    ]


def test_log_keeps_the_reason_on_one_line_and_rejects_bad_input(tmp_path):
    log = tmp_path / "trades-log.md"
    line = desk.log_line("NONE", "", "", "", "0 HIGH,\n no PICK", path=log, now=T)
    assert line.endswith("| - | NONE | - | - | 0 HIGH, no PICK.")
    with pytest.raises(ValueError):
        desk.log_line("YOLO", "TSLA", "1", "1", "x", path=log, now=T)
    with pytest.raises(ValueError):
        desk.log_line("BUY", "TSLA", "1", "abc", "x", path=log, now=T)
    with pytest.raises(ValueError):
        desk.log_line("BUY", "TSLA", "1", "1", "   ", path=log, now=T)


def test_lock_blocks_a_second_run_until_released_or_stale(tmp_path):
    lock = tmp_path / "state" / "desk.lock"
    ok, _, first = desk.acquire("scan 10:12", path=lock, now=T)
    assert ok and first and f"token {first}" in lock.read_text(encoding="utf-8")
    ok, msg, tok = desk.acquire("scan 10:27", path=lock, now=T + dt.timedelta(minutes=15))
    assert not ok and tok is None and "scan 10:12" in msg
    ok, _, second = desk.acquire("scan 11:30", path=lock, now=T + dt.timedelta(minutes=50))
    assert ok and second != first  # the stale lock was taken over
    assert not desk.release(lock, first)  # the stale run's late unlock: the takeover keeps it
    assert lock.exists() and "scan 11:30" in lock.read_text(encoding="utf-8")
    assert desk.release(lock, second) and not lock.exists()
    assert desk.acquire("scan 12:33", path=lock, now=T)[0]


def cand(sym, tier, score, entry=100.0, stop=99.0, target=102.0, reason=""):
    return {
        "symbol": sym,
        "tier": tier,
        "score": score,
        "reasons": [reason] if reason else [],
        "plan": {"entry": entry, "stop": stop, "target": target, "target_r": 2.0},
        "size": {"qty": 3, "max_loss": 3.0},
        "order": {
            "symbol": sym,
            "side": "buy",
            "qty": "3",
            "type": "market",
            "time_in_force": "day",
            "order_class": "bracket",
            "take_profit_limit_price": str(target),
            "stop_loss_stop_price": str(stop),
        }
        if tier != "REJECT"
        else None,
    }


def write_summary(tmp_path, cands):
    p = tmp_path / "summary.json"
    p.write_text(json.dumps({"as_of": "2026-11-23 10:16 ET", "candidates": cands}))
    return p


TM = "Time Machine edge +2.1 pts vs break-even 25% (needs +5): Close to break-even"


def test_candidates_order_high_pick_then_sized_watch_and_explain_none(tmp_path):
    p = write_summary(
        tmp_path,
        [
            cand("W1", "WATCH", 30, reason=TM),
            cand("H1", "HIGH", 70),
            cand("R1", "REJECT", 0, reason="failed breakout: fired at 9:45"),
            cand("H2", "HIGH", 80),
        ],
    )
    rep = desk.candidates_report(p, now=T)
    assert [c["symbol"] for c in rep["candidates"]] == ["H2", "H1", "W1"]
    assert rep["age_min"] == 8 and rep["none_reason"] is None
    assert rep["candidates"][2]["missed"].startswith("Time Machine edge +2.1")

    p = write_summary(tmp_path, [cand("R1", "REJECT", 0, reason="failed breakout: x")] * 3)
    assert desk.candidates_report(p, now=T)["none_reason"].endswith("mostly failed breakout (3)")


class FakeRun:
    """Stands in for trader-risk and trader-broker."""

    def __init__(self, verdict="PASS", reasons=()):
        self.calls, self.verdict, self.reasons = [], verdict, list(reasons)

    def __call__(self, script, *args):
        self.calls.append((script, args))
        if script.endswith("check.py"):
            out = {"results": [{"verdict": self.verdict, "reasons": self.reasons}]}
            return subprocess.CompletedProcess(args, 0, json.dumps(out), "")
        if args[0] == "approve":
            order = {"symbol": "H1", "side": "buy", "qty": "3", "stop_loss_stop_price": "99.0"}
            order["take_profit_limit_price"] = "102.0"
            text = "Recorded your yes.\nNow call place_stock_order with exactly:\n"
            return subprocess.CompletedProcess(args, 0, text + json.dumps(order) + "\nmore", "")
        return subprocess.CompletedProcess(args, 0, "Recorded order 1 as placed.", "")


def arm(tmp_path, sym, ask, verdict="CLEAR", run=None, cands=None, now=T):
    p = write_summary(
        tmp_path, cands or [cand("H1", "HIGH", 70), cand("W1", "WATCH", 30, reason=TM)]
    )
    run = run or FakeRun()
    files = {
        "log": tmp_path / "trades-log.md",
        "orders": tmp_path / "orders.json",
        "pending": tmp_path / "pending.json",
    }
    out = desk.arm(sym, ask, verdict, summary_path=p, now=now, run=run, **files)
    return out, files, run


def test_arm_skips_a_buy_stop_plan_from_the_1100_cutoff(tmp_path):
    stop_entry = cand("PDD", "HIGH", 60)
    stop_entry["order"] |= {"type": "stop", "stop_price": "100.00"}
    late = T.replace(hour=11, minute=5)
    (code, _), f, run = arm(tmp_path, "PDD", 99.8, cands=[stop_entry], now=late)
    assert code == desk.SKIPPED and not run.calls
    assert "Buy-stop plan after the 11:00 cutoff" in f["log"].read_text(encoding="utf-8")
    other = tmp_path / "market"
    other.mkdir()
    assert arm(other, "H1", 100.2, now=late)[0][0] == desk.READY  # a market entry still goes


def test_arm_skips_stale_downgraded_watch_caution_and_repeats(tmp_path):
    (code, _), f, run = arm(tmp_path, "H1", 101.50)
    assert code == desk.SKIPPED and not run.calls
    assert "Stale: ask $101.50 is 1.5% over" in f["log"].read_text(encoding="utf-8")
    assert arm(tmp_path, "H1", 98.9)[0][0] == desk.SKIPPED  # at or under the stop
    assert arm(tmp_path, "H1", 100.1, "DOWNGRADE")[0][0] == desk.SKIPPED
    (code, _), f, _ = arm(tmp_path, "W1", 100.1, "CAUTION")
    assert code == desk.SKIPPED and "WATCH needs research CLEAR" in f["log"].read_text(
        encoding="utf-8"
    )
    assert arm(tmp_path, "ZZZ", 100.1)[0][0] == desk.SKIPPED


def test_arm_ready_writes_order_and_attempt_then_record_logs_buy(tmp_path):
    (code, legs), f, run = arm(tmp_path, "H1", 100.2)
    assert code == desk.READY and [leg["qty"] for leg in legs] == ["1", "2"]  # T1 half, T2 rest
    assert legs[0]["take_profit_limit_price"] == "101.00"  # entry 100 + 1R (stop 99)
    assert all(leg["stop_loss_stop_price"] == "99.0" for leg in legs)
    assert [c[0] for c in run.calls] == ["trader-risk/check.py", "trader-broker/ticket.py"]
    written = json.loads(f["orders"].read_text())["orders"]
    assert len(written) == 1 and written[0]["symbol"] == "H1"  # risk sees ONE trade
    assert written[0]["plan"] == {"entry": 100.0, "stop": 99.0, "target": 102.0}
    log = f["log"].read_text(encoding="utf-8")
    assert "| H1 | ATTEMPT | 3 sh | ~$100.00 | Stop $99.00; 1 sh at T1 $101.00, 2 sh at T2" in log
    assert "Passed every gate (score 70, 2R target); research CLEAR" in log
    assert arm(tmp_path, "H1", 100.2)[0][0] == desk.SKIPPED  # already traded today

    line = desk.record(
        "H1",
        "placed",
        "abc,def",
        "3",
        "100.21",
        now=T,
        log=f["log"],
        pending=f["pending"],
        run=FakeRun(),
        trades=tmp_path / "desk-trades.json",
    )
    assert "| H1 | BUY | 3 sh | $100.21 | 1 sh to T1 $101.00 (then the rest's stop to" in line
    kept = desk.load_trades(path=tmp_path / "desk-trades.json")["H1"]
    assert kept["order_ids"] == ["abc", "def"] and kept["entry"] == 100.0 and not kept["breakeven"]
    assert "2 sh to T2 $102.0; Passed every gate" in line
    assert not f["pending"].exists()


def test_an_exit_fill_reaches_the_broker_log_once_per_order(tmp_path):
    tf = tmp_path / "desk-trades.json"
    desk.save_trade({"symbol": "H1", "entry": 100.0, "order_ids": ["a", "b"]}, T, tf)
    run = FakeRun()
    assert "recorded the sale of 1 at 101.0" in desk.record_fill(
        "H1", "1", "101.0", "tp-a", T, tf, run
    )
    assert "already recorded" in desk.record_fill("H1", "1", "101.0", "tp-a", T, tf, run)
    desk.record_fill("H1", "2", "100.0", "sl-b", T, tf, run)
    fills = [c for c in run.calls if c[1][0] == "fill"]
    assert len(fills) == 2 and desk.load_trades(path=tf)["H1"]["fills"] == ["tp-a", "sl-b"]
    assert desk.load_trades(path=tf)["H1"]["order_ids"] == ["a", "b"]  # the rest is kept


def test_next_weekday_skips_the_weekend():
    friday = dt.datetime(2026, 11, 27, 16, 45, tzinfo=desk.NY)
    assert desk.next_weekday(friday).date() == dt.date(2026, 11, 30)
    assert desk.next_weekday(T).date() == dt.date(2026, 11, 24)


def test_one_share_goes_all_at_t1_and_a_missing_bracket_is_said():
    order = {
        "symbol": "X",
        "qty": "1",
        "take_profit_limit_price": "103",
        "stop_loss_stop_price": "99",
    }
    assert desk.split_t1_t2(order, 100.0, 99.0) == [order | {"take_profit_limit_price": "101.00"}]
    legs = desk.split_t1_t2(order | {"qty": "5"}, 100.0, 99.0)
    assert [(leg["qty"], leg["take_profit_limit_price"]) for leg in legs] == [
        ("2", "101.00"),
        ("3", "103"),
    ]


def test_arm_risk_block_on_a_daily_limit_stops_the_run(tmp_path):
    run = FakeRun("BLOCK", ["daily loss: $45 at the stops would pass the $30 limit"])
    (code, _), f, _ = arm(tmp_path, "H1", 100.2, run=run)
    assert code == desk.STOP_RUN and "Risk BLOCK: daily loss" in f["log"].read_text(
        encoding="utf-8"
    )


def test_record_failed_order_logs_skip_with_the_reason(tmp_path):
    _, f, _ = arm(tmp_path, "W1", 100.2)
    line = desk.record(
        "W1",
        "failed",
        note="insufficient buying power",
        now=T,
        log=f["log"],
        pending=f["pending"],
        run=FakeRun(),
    )
    assert "| W1 | SKIP | - | ~$100.00 | Order failed: insufficient buying power;" in line
    assert line.endswith(
        "WATCH, lower conviction: missed Time Machine edge +2.1 pts vs break-even 25% (needs +5)"
        " (score 30); research CLEAR."
    )


def test_shortlist_puts_added_names_first_then_momentum_setups_in_range(tmp_path):
    daily = {
        "as_of": "2026-11-23",
        "hits": [
            {"symbol": "NVDA", "setups": ["oversold_bounce"], "price": 165, "volume": 9e8},
            {"symbol": "AAPL", "setups": ["reclaim_ma20"], "price": 349, "volume": 5e7},  # $150+
            {"symbol": "RCL", "setups": ["reclaim_ma20"], "price": 90, "volume": 1e7},
            {"symbol": "BIG", "setups": ["breakout_20d"], "price": 50, "volume": 4e7},
            {"symbol": "SML", "setups": ["breakout_20d"], "price": 20, "volume": 2e6},
            {"symbol": "FLG", "setups": ["bull_flag", "oversold"], "price": 30, "volume": 1e6},
        ],
    }
    doc = desk.build_shortlist(daily, ["pdd", "BIG"], 5, 150, size=5, now=T)
    assert [h["symbol"] for h in doc["hits"]] == ["PDD", "BIG", "SML", "FLG", "RCL"]
    assert doc["hits"][2]["why"] == "daily: breakout_20d" and doc["date"] == "2026-11-23"

    path = tmp_path / "shortlist.json"
    path.write_text(json.dumps(doc))
    assert desk.shortlist_today(path, now=T)
    assert not desk.shortlist_today(path, now=T + dt.timedelta(days=1))


def test_verdicts_are_kept_per_day_and_shown_with_candidates(tmp_path):
    vp = tmp_path / "verdicts.json"
    desk.save_verdict("h1", "CAUTION", "earnings Thursday", now=T, path=vp)
    desk.save_verdict("H1", "CLEAR", "nothing new", now=T, path=vp)
    assert desk.load_verdicts(path=vp) == {
        "H1": {"verdict": "CLEAR", "note": "nothing new", "at": "10:24"}
    }
    p = write_summary(tmp_path, [cand("H1", "HIGH", 70), cand("H2", "HIGH", 60)])
    rows = desk.candidates_report(p, now=T, verdicts=desk.load_verdicts(path=vp))["candidates"]
    assert rows[0]["verdict_today"]["verdict"] == "CLEAR" and rows[1]["verdict_today"] is None


def test_arm_and_record_with_the_real_risk_check_and_ticket(tmp_path, monkeypatch):
    """No fakes: trader-risk and trader-broker run offline on a market and a buy-stop entry."""
    repo = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(tmp_path)
    (tmp_path / "trader").mkdir()
    (tmp_path / "trader" / "config.json").write_text(
        (repo / "config" / "config.example.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    stop_entry = cand("PDD", "HIGH", 60, entry=86.03, stop=84.80, target=88.50)
    stop_entry["order"] |= {"type": "stop", "stop_price": "86.03", "qty": "2"}
    stop_entry["pending"] = {"price": 86.03}
    summary = write_summary(tmp_path, [cand("MOD", "HIGH", 70, 141.35, 140.01, 145.37), stop_entry])

    code, legs = desk.arm("MOD", 141.40, "CLEAR", summary_path=summary, now=T)
    assert code == desk.READY, (tmp_path / "trades-log.md").read_text(encoding="utf-8")
    assert all(leg["type"] == "market" and "plan" not in leg for leg in legs)  # ours stays home
    assert [leg["take_profit_limit_price"] for leg in legs] == ["142.69", "145.37"]  # T1, T2
    desk.record("MOD", "placed", "id-1,id-2", "3", "141.38", now=T)

    code, legs = desk.arm("PDD", 85.70, "CLEAR", summary_path=summary, now=T)
    assert code == desk.READY and all(leg["type"] == "stop" for leg in legs)
    assert [leg["qty"] for leg in legs] == ["1", "1"] and legs[0]["stop_price"] == "86.03"
    line = desk.record("PDD", "placed", "id-3", now=T)
    assert "Buy-stop resting at the range high" in line and "not broken out yet" in line
    assert "only 1 of 2 brackets placed" in line


def test_cli_lock_exit_codes(tmp_path):
    lock = tmp_path / "desk.lock"
    assert desk.main(["lock", "--owner", "a", "--path", str(lock)]) == 0
    token = lock.read_text(encoding="utf-8").strip().partition(" token ")[2]
    assert desk.main(["lock", "--owner", "b", "--path", str(lock)]) == desk.BUSY
    assert desk.main(["unlock", "--path", str(lock), "--token", "someone-else"]) == 0
    assert lock.exists()  # not this run's lock: left in place, with a warning
    assert desk.main(["unlock", "--path", str(lock), "--token", token]) == 0
    assert not lock.exists()
    desk.main(["lock", "--owner", "c", "--path", str(lock)])
    assert desk.main(["unlock", "--path", str(lock), "--force"]) == 0 and not lock.exists()
    with pytest.raises(SystemExit):
        desk.main(["unlock", "--path", str(lock)])  # --token or --force is required


def test_breakeven_logs_the_t1_exit_once_with_the_dollar_sign_intact(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    trade = {"symbol": "BAX", "entry": 27.1, "stop": 26.7, "t1": "27.50", "t2": "28.30"}
    desk.save_trade(trade | {"legs": ["4", "5"], "order_ids": ["a", "b"], "breakeven": False})
    assert desk.main(["breakeven", "BAX", "--price", "27.50"]) == 0
    assert desk.main(["breakeven", "BAX", "--price", "27.50"]) == 0  # second run: no new line
    lines = [x for x in (tmp_path / "trades-log.md").read_text(encoding="utf-8").splitlines()]
    exits = [x for x in lines if "| EXIT |" in x]
    assert len(exits) == 1 and "| BAX | EXIT | 4 sh | $27.50 |" in exits[0]
    assert "moved to breakeven $27.10." in exits[0]
    assert desk.load_trades()["BAX"]["breakeven"] is True


def test_a_short_is_checked_against_the_bid_split_downward_and_logged_as_short(tmp_path):
    short = cand("S1", "PICK", 0, entry=100.0, stop=101.0, target=98.0, reason="short breakdown")
    short["side"] = "sell"
    short["order"] |= {"side": "sell"}
    (code, _), f, _ = arm(tmp_path, "S1", 98.5, cands=[short])  # 1.5 % under the entry
    assert code == desk.SKIPPED and "Stale: bid $98.50 is 1.5% under" in f["log"].read_text(
        encoding="utf-8"
    )
    other = tmp_path / "ok"
    other.mkdir()
    (code, legs), f, _ = arm(other, "S1", 99.6, cands=[short])
    assert code == desk.READY and legs[0]["take_profit_limit_price"] == "99.00"  # entry - 1R
    assert "SHORT, lower conviction" in f["log"].read_text(encoding="utf-8")
    line = desk.record(
        "S1",
        "placed",
        "a,b",
        "3",
        "99.6",
        now=T,
        log=f["log"],
        pending=f["pending"],
        run=FakeRun(),
        trades=other / "trades.json",
    )
    assert "| S1 | SHORT | 3 sh |" in line
    assert desk.load_trades(path=other / "trades.json")["S1"]["side"] == "sell"
