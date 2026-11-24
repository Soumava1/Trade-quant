"""scout.py --pending: names that have not broken out yet become buy-stop plans."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".claude" / "skills" / "trade-scout"))
import scout  # noqa: E402


def waiting(gap, level=14.92, close=14.22):
    return {
        "status": "waiting",
        "level": level,
        "last": {"bar_start": "10:05", "close": close, "vs_level_pct": gap},
        "reason": "under the range high",
    }


def test_a_name_near_its_range_high_gets_a_buy_stop_one_cent_over_it():
    c = scout.Candidate(symbol="APLD", scan={})
    assert scout.pending_entry(SimpleNamespace(max_pending_pct=3.0), c, waiting(-1.2))
    assert c.pending and c.late["price"] == 14.93 and c.late["time"] == "10:10 AM"
    assert c.late["bar"] == 7 and c.late["age_min"] == 0


def test_a_name_too_far_under_its_range_high_is_not_armed():
    c = scout.Candidate(symbol="APLD", scan={})
    assert not scout.pending_entry(SimpleNamespace(max_pending_pct=3.0), c, waiting(-4.69))
    assert c.status == "stopped" and "within 3%" in c.reasons[0] and not c.pending


def test_watch_symbols_reads_symbols_or_a_watchlist(tmp_path):
    wl = tmp_path / "shortlist.json"
    wl.write_text('{"hits": [{"symbol": "pdd"}, {"symbol": "MOD"}]}', encoding="utf-8")
    assert scout.watch_symbols(SimpleNamespace(symbols=None, watchlist=wl)) == ["PDD", "MOD"]
    assert scout.watch_symbols(SimpleNamespace(symbols="a, b", watchlist=None)) == ["A", "B"]
