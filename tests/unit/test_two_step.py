"""scout.two_step: the Time Machine gate scores the desk's T1/T2 exit, not one far target."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".claude" / "skills" / "trade-scout"))
import scout  # noqa: E402


def chart(days):
    """Yahoo-shaped chart: `days` sessions of 78 five-minute bars, each a 0.5 % rise."""
    ts, q = [], {k: [] for k in ("open", "high", "low", "close", "volume")}
    base = 1795000000 - (1795000000 % 86400) + 14 * 3600 + 30 * 60  # 9:30 New York
    for d in range(days):
        p = 100.0
        for i in range(78):
            ts.append(base + d * 86400 + i * 300)
            for k, x in zip(q, (p, p * 1.005, p * 0.999, p * 1.004, 1e6), strict=True):
                q[k].append(x)
            p *= 1.004
    return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [q]}}]}}


def test_a_steady_riser_hits_t1_then_t2_and_the_history_agrees():
    c = chart(25)
    date = scout.features.sessions(c)
    last_day = sorted(date)[-1]
    plan = {"date": last_day, "entry_bar": 6, "entry": 100.0, "stop": 99.0}
    ts = scout.two_step(c, plan, 2.0, replays=500, seed=19)
    mc = ts["mc"]
    assert mc["p_target"] == 1.0 and mc["p_stop"] == 0.0 and mc["p_t2"] == 1.0
    assert mc["avg_R"] == 1.5 and mc["win_r"] == 1.5  # 0.5 x 1R + 0.5 x 2R
    assert ts["history"]["triggered"] > 0 and ts["history"]["stop"] == 0


def test_score_uses_the_two_step_break_even():
    th = scout.Thresholds(min_edge=0.05, min_history=3, min_score=0, max_trigger_age=10)
    c = scout.Candidate(symbol="X", scan={})
    c.entry = {"trigger": {}, "minutes_since": 0}
    c.plan = {"target_r": 3.0}
    # T1 first in 45 % of decided replays: a loser against a 3R target's 25 % break-even
    # under the old rule would pass, but here a win averages 1.2R, break-even 45.5 %.
    c.tm = {
        "mc": {"p_target": 0.01, "p_stop": 0.5, "avg_R": -0.4},
        "two_step": {
            "mc": {"p_target": 0.45, "p_stop": 0.55, "avg_R": 0.01, "win_r": 1.2},
            "history": {"triggered": 9, "target": 5, "stop": 4, "avg_R": 0.1},
        },
    }
    scout.score_candidate(c, th)
    assert c.metrics["breakeven"] == round(1 / 2.2, 4)
    assert c.metrics["p_target"] == 0.45 and c.metrics["mc_avg_r"] == 0.01
    assert any("Time Machine edge" in r for r in c.reasons)  # -0.5 pts: still fails +5
