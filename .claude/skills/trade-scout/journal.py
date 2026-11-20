#!/usr/bin/env python3
"""Paper journal for Trade Scout: record each pick, settle it from real bars, report the KPIs.

    python3 journal.py settle           # fill in outcomes for picks whose session has closed
    python3 journal.py report           # KPIs over every settled pick, split by tier
    python3 journal.py report --json

Every run of scout.py appends its HIGH and PICK trades to trader/journal/trades.jsonl (one per
symbol per day). `settle` replays each one on that day's real 5-minute bars after the entry bar:
the stop is checked before the target on every bar (a bar that touches both counts as the stop),
a bar that opens through the stop fills at its open (a gap is worse than the stop), and anything
still open is closed at the flat-by bar's close. Nothing here talks to a broker.

The KPIs are the ones that decide whether a strategy has an edge, in R (multiples of the risk):
win rate, average R (expectancy), profit factor, max drawdown, longest losing streak, trades per
day, and the dollar P&L against the budget. Judge them over at least 30 to 50 trades, and ideally
100 or more: a handful of trades says almost nothing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import features  # noqa: E402

OPEN_MIN = 9 * 60 + 30


def journal_path(trader_dir: Path) -> Path:
    """trader/journal/trades.jsonl, or <repo>/<journal_dir>/trades.jsonl when the config sets
    journal_dir (relative to the folder that holds trader/), so the history can be committed."""
    try:
        cfg = json.loads((trader_dir / "config.json").read_text())
    except (OSError, json.JSONDecodeError):
        cfg = {}
    jd = cfg.get("journal_dir") if isinstance(cfg, dict) else None
    base = trader_dir.parent / str(jd) if jd else trader_dir / "journal"
    return base / "trades.jsonl"


def load(trader_dir: Path) -> list[dict[str, Any]]:
    p = journal_path(trader_dir)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def save(trader_dir: Path, rows: list[dict[str, Any]]) -> None:
    p = journal_path(trader_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))


def record(trader_dir: Path, summary: dict[str, Any], run_id: str) -> int:
    """Append the run's HIGH and PICK trades; one row per symbol per day (first one wins)."""
    rows = load(trader_dir)
    seen = {(r["date"], r["symbol"]) for r in rows}
    added = 0
    for c in summary.get("candidates", []):
        if c.get("tier") not in ("HIGH", "PICK") or not c.get("plan") or not c.get("size"):
            continue
        p, z = c["plan"], c["size"]
        key = (p.get("date"), c["symbol"])
        if key in seen:
            continue
        rows.append(
            {
                "date": p.get("date"),
                "symbol": c["symbol"],
                "tier": c["tier"],
                "score": c["score"],
                "entry": p["entry"],
                "stop": p["stop"],
                "target": p["target"],
                "target_r": p.get("target_r"),
                "entry_bar": p.get("entry_bar"),
                "entry_time": p.get("entry_time"),
                "flat_by": p.get("flat_by") or "15:55",
                "qty": int(z.get("qty") or 0),
                "risk_per_share": p.get("risk_per_share"),
                "max_loss": z.get("max_loss"),
                "late": bool(c.get("late_entry")),
                "run": run_id,
                "status": "open",
            }
        )
        seen.add(key)
        added += 1
    if added:
        save(trader_dir, rows)
    return added


def simulate(row: dict[str, Any], bars: list[features.Bar]) -> dict[str, Any]:
    """Outcome of one trade on its day's bars."""
    entry, stop, target = float(row["entry"]), float(row["stop"]), float(row["target"])
    risk = entry - stop
    hh, mm = (int(x) for x in str(row["flat_by"]).split(":"))
    flat_min = hh * 60 + mm
    start = OPEN_MIN + 5 * (int(row["entry_bar"]) + 1)
    exit_price, how, at = None, None, None
    for minute, o, h, lo, c, _v in bars:
        if minute < start:
            continue
        if minute + 5 > flat_min:  # the flat-by bar: out at the previous close
            break
        if o <= stop:
            exit_price, how, at = o, "stop (gap)", minute
            break
        if lo <= stop:
            exit_price, how, at = stop, "stop", minute
            break
        if h >= target:
            exit_price, how, at = (o if o >= target else target), "target", minute
            break
        exit_price, at = c, minute
    if how is None:
        if exit_price is None:
            return {"status": "no bars"}
        how = "flat by close"
    r = (exit_price - entry) / risk if risk > 0 else 0.0
    qty = int(row.get("qty") or 0)
    return {
        "status": "settled",
        "exit": round(exit_price, 4),
        "exit_how": how,
        "exit_time": f"{(at or 0) // 60}:{(at or 0) % 60:02d}",
        "r": round(r, 3),
        "pnl": round((exit_price - entry) * qty, 2),
    }


def settle(trader_dir: Path, offline: Path | None = None, now: dt.datetime | None = None) -> int:
    rows = load(trader_dir)
    now = now or dt.datetime.now(features.NY)
    charts: dict[str, Any] = {}
    done = 0
    for row in rows:
        if row.get("status") != "open":
            continue
        hh, mm = (int(x) for x in str(row["flat_by"]).split(":"))
        closes = dt.datetime.fromisoformat(row["date"]).replace(
            hour=hh, minute=mm, tzinfo=features.NY
        )
        if now < closes:
            continue
        sym = row["symbol"]
        if sym not in charts:
            try:
                if offline:
                    f = offline / f"{sym}.json.gz"
                    charts[sym] = features.load_chart(f if f.exists() else offline / f"{sym}.json")
                else:
                    charts[sym] = features.fetch_chart(sym, "60d", "5m")
            except (OSError, RuntimeError) as e:
                row["note"] = f"could not fetch bars: {e}"
                continue
        bars = features.sessions(charts[sym]).get(row["date"]) or []
        row.update(simulate(row, bars))
        done += row.get("status") == "settled"
    save(trader_dir, rows)
    return done


def kpis(rows: list[dict[str, Any]], budget: float | None = None) -> dict[str, Any]:
    s = [r for r in rows if r.get("status") == "settled"]
    if not s:
        return {"trades": 0}
    rs = [float(r["r"]) for r in s]
    pnl = [float(r.get("pnl") or 0) for r in s]
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x < 0]
    gross_w = sum(p for p in pnl if p > 0)
    gross_l = -sum(p for p in pnl if p < 0)
    eq = peak = dd = 0.0
    eq_r = peak_r = dd_r = 0.0
    streak = worst = 0
    for x, p in zip(rs, pnl, strict=True):
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        eq_r += x
        peak_r = max(peak_r, eq_r)
        dd_r = max(dd_r, peak_r - eq_r)
        streak = streak + 1 if x < 0 else 0
        worst = max(worst, streak)
    days = sorted({r["date"] for r in s})
    out: dict[str, Any] = {
        "trades": len(s),
        "days": len(days),
        "trades_per_day": round(len(s) / len(days), 2),
        "win_rate": round(len(wins) / len(s), 3),
        "avg_r": round(sum(rs) / len(s), 3),
        "avg_win_r": round(sum(wins) / len(wins), 3) if wins else 0.0,
        "avg_loss_r": round(sum(losses) / len(losses), 3) if losses else 0.0,
        "profit_factor": round(gross_w / gross_l, 2) if gross_l else None,
        "total_pnl": round(sum(pnl), 2),
        "avg_pnl": round(sum(pnl) / len(s), 2),
        "max_drawdown_usd": round(dd, 2),
        "max_drawdown_r": round(dd_r, 2),
        "longest_losing_streak": worst,
        "target_hits": sum(1 for r in s if r.get("exit_how") == "target"),
        "stops": sum(1 for r in s if str(r.get("exit_how", "")).startswith("stop")),
        "closed_at_flat_by": sum(1 for r in s if r.get("exit_how") == "flat by close"),
    }
    if budget:
        out["return_pct"] = round(sum(pnl) / budget * 100, 2)
    return out


def render(rows: list[dict[str, Any]], budget: float | None) -> str:
    lines = ["# Paper journal KPIs", ""]
    groups = {
        "all picks": rows,
        "HIGH": [r for r in rows if r["tier"] == "HIGH"],
        "PICK (lower conviction)": [r for r in rows if r["tier"] == "PICK"],
    }
    for name, grp in groups.items():
        k = kpis(grp, budget)
        if not k["trades"]:
            lines.append(f"- {name}: no settled trades yet")
            continue
        pf = k["profit_factor"] if k["profit_factor"] is not None else "n/a (no losses)"
        lines.append(
            f"- **{name}**: {k['trades']} trades over {k['days']} days "
            f"({k['trades_per_day']}/day); win rate {k['win_rate']:.0%}; avg R {k['avg_r']:+.3f} "
            f"(wins {k['avg_win_r']:+.2f}R, losses {k['avg_loss_r']:+.2f}R); profit factor {pf}; "
            f"P&L ${k['total_pnl']:+,.2f}"
            + (f" ({k['return_pct']:+.2f}% of the budget)" if "return_pct" in k else "")
            + f"; max drawdown ${k['max_drawdown_usd']:,.2f} ({k['max_drawdown_r']:.2f}R); "
            f"longest losing streak {k['longest_losing_streak']}; "
            f"exits: {k['target_hits']} target, {k['stops']} stop, "
            f"{k['closed_at_flat_by']} at the close"
        )
    open_n = sum(1 for r in rows if r.get("status") == "open")
    lines += ["", f"{open_n} pick(s) still open or waiting for the close."]
    lines.append(
        "Read these over 30-50+ trades. An avg R above +0.2 with a drawdown you can live "
        "with is the bar before risking real money."
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Trade Scout paper journal")
    ap.add_argument("cmd", choices=["settle", "report"])
    ap.add_argument("--trader-dir", default="trader")
    ap.add_argument("--offline", help="folder of saved <SYMBOL>.json(.gz) 60-day 5m charts")
    ap.add_argument("--now", help='New York time "YYYY-MM-DD HH:MM"')
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    trader_dir = Path(a.trader_dir).resolve()
    now = (
        dt.datetime.strptime(a.now, "%Y-%m-%d %H:%M").replace(tzinfo=features.NY) if a.now else None
    )
    if a.cmd == "settle":
        n = settle(trader_dir, Path(a.offline) if a.offline else None, now)
        print(f"Settled {n} pick(s).")
    rows = load(trader_dir)
    try:
        budget = float(json.loads((trader_dir / "config.json").read_text()).get("budget") or 0)
    except (OSError, json.JSONDecodeError, ValueError):
        budget = 0.0
    if a.json:
        print(
            json.dumps(
                {
                    "all": kpis(rows, budget or None),
                    "HIGH": kpis([r for r in rows if r["tier"] == "HIGH"], budget or None),
                    "PICK": kpis([r for r in rows if r["tier"] == "PICK"], budget or None),
                },
                indent=2,
            )
        )
    elif a.cmd == "report" or rows:
        print(render(rows, budget or None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
