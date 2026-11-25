#!/usr/bin/env python3
"""The desk's running log of rules and rejects, and its nightly self-tuning.

  log      scout.py calls append_log() after every run: the rules in force, then every
           rejected name with the gate that stopped it, into trader/scout-log.md.
  review   after the close: for each name the scout rejected today, what the desk's own
           trade (trade-backtest's session_trade: buy-stop over the range high, stop at the
           range low but at least min_stop_atr x ATR, half at +1R then breakeven, half at
           2R, out by 15:55) would have made today. Rows go to trader/optimizer/history.jsonl.
           Over the last WINDOW trading days, a gate whose rejects would have made money
           (n >= MIN_N, avg R >= +EDGE_R, win rate >= 50 %) is loosened one step; a gate
           already loosened whose rejects lose (avg R <= -EDGE_R) is tightened one step back
           toward its default; the names that passed every gate losing (avg R <= -EDGE_R)
           raise min_score one step. With --apply, at most ONE change a night, inside fixed
           bounds, written to trader/config.json and logged with the gate it moved.

Usage (from the project root):
    python .claude/skills/trade-scout/optimize.py review            # report only
    python .claude/skills/trade-scout/optimize.py review --apply    # and tune one gate

Paper only. A counterfactual is not a fill: the desk's real entries are worse.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "trade-backtest"))
import features  # noqa: E402,I001
import backtest  # noqa: E402

WINDOW, MIN_N, EDGE_R = 10, 15, 0.15
CHASE_TEST = 0.02  # a chase reject is replayed as a buy up to 2 % over the range high
# gate -> (pattern on the scout's reason, config key under "scout", step that LOOSENS,
#          (min, max), default)
GATES: dict[str, tuple[str, str, float, tuple[float, float], float]] = {
    "rvol": (r"^not in play", "min_rvol", -0.1, (0.7, 1.5), 1.0),
    "chase": (r"^extended", "max_chase_pct", 0.25, (0.5, 2.0), 1.0),
    "pending": (r"^pending: last close", "max_pending_pct", 0.5, (2.0, 5.0), 3.0),
    "edge": (r"^Time Machine edge", "min_edge", -0.01, (0.0, 0.10), 0.05),
    "score": (r"^score ", "min_score", -5.0, (40.0, 70.0), 55.0),
}
REPORT_ONLY = {
    "vwap": r"^below VWAP",
    "thin": r"^too thin",
    "quiet": r"^too quiet|ATR",
    "failed": r"failed breakout|back under",
    "history": r"^history",
    "data": r"no-data|not enough bars|no 60-day|no bars",
}


def gate_of(reason: str) -> str:
    for g, (pat, *_) in GATES.items():
        if re.search(pat, reason):
            return g
    return next((g for g, pat in REPORT_ONLY.items() if re.search(pat, reason)), "other")


# --------------------------------------------------------------------------- the running log


def rules_line(s: dict[str, Any]) -> str:
    th = s.get("thresholds") or {}
    return (
        f"chase <= {th.get('max_chase_pct', 1.0):g}% · RVOL >= {th.get('min_rvol', 1.0):g}x · "
        f"ATR >= ${th.get('min_atr', 0.5):.2f} · ADV >= {th.get('min_adv', 1e6):,.0f} · "
        f"{'above VWAP' if th.get('require_above_vwap', True) else 'VWAP not required'} · "
        f"TM edge >= {th.get('min_edge', 0.05) * 100:+.0f} pts (T1/T2) · "
        f"score >= {th.get('min_score', 55):g} · buy-stop within "
        f"{s.get('max_pending_pct', 3):g}% · stop {s.get('stop')} · T1 1R, T2 {s.get('target')}"
    )


def append_log(path: Path, summary: dict[str, Any], run: str) -> None:
    cands = summary.get("candidates") or []
    tiers = {t: sum(1 for c in cands if c.get("tier") == t) for t in ("HIGH", "PICK", "WATCH")}
    lines = [
        f"\n## {summary.get('as_of') or run} · run {run}\n",
        f"- **Rules:** {rules_line(summary.get('settings') or {})}",
        f"- **Result:** {tiers['HIGH']} HIGH, {tiers['PICK']} PICK, {tiers['WATCH']} WATCH; "
        f"{len(cands)} names checked\n",
    ]
    rejects = [c for c in cands if c.get("tier") == "REJECT"]
    if rejects:
        lines += ["| Symbol | Gate | Why |", "|---|---|---|"]
        for c in rejects:
            why = "; ".join(c.get("reasons") or ["no reason given"])
            gate = gate_of((c.get("reasons") or [""])[0])
            lines.append(f"| {c['symbol']} | {gate} | {why[:220].replace('|', '/')} |")
    if not path.exists():
        path.write_text(
            "# Trade desk: rules and rejects\n\nOne block per scout run (newest last), and one "
            "per nightly review. Written by the scout and optimize.py; paper only.\n",
            encoding="utf-8",
        )
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- nightly review


def todays_rejects(trader: Path, date: str) -> dict[str, dict[str, str]]:
    """symbol -> {gate, reason, tier}: the first run of the day that saw it decides the gate;
    a name that reached HIGH/PICK/WATCH in any run counts as "passed"."""
    out: dict[str, dict[str, str]] = {}
    for f in sorted((trader / "scout").glob(f"{date}_*/summary.json")):
        doc = json.loads(f.read_text(encoding="utf-8"))
        seen = str(doc.get("as_of") or "")[11:16]  # "HH:MM" New York
        for c in doc.get("candidates") or []:
            sym, tier = c["symbol"], c.get("tier")
            if tier in ("HIGH", "PICK", "WATCH"):
                out[sym] = {"gate": "passed", "reason": tier, "tier": tier}
            elif sym not in out:
                reason = (c.get("reasons") or [""])[0]
                out[sym] = {"gate": gate_of(reason), "reason": reason, "tier": tier or "REJECT"}
                out[sym]["seen"] = seen
    return out


def counterfactual(
    chart: dict[str, Any], date: str, rule: dict[str, Any], k: float, late: str | None = None
) -> Any:
    """The desk's trade on `date`. With `late` ("HH:MM", a chase reject), the entry is the
    first bar from that time, up to CHASE_TEST % over the range high: what chasing it then
    would have made, not the earlier breakout the desk had already missed."""
    if late:
        hh, mm = (int(x) for x in late.split(":"))
        m = hh * 60 + mm + (-(hh * 60 + mm)) % 5  # the next 5-minute bar
        rule = {**rule, "entry_from": f"{m // 60:02d}:{m % 60:02d}", "max_chase": CHASE_TEST}
    days = features.sessions(chart)
    if date not in days:
        return None
    atr = backtest.atr_before(days, date)
    return backtest.session_trade(days[date], atr, k, **rule) if atr else None


def gate_stats(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for g in {r["gate"] for r in rows}:
        rs = [r["r"] for r in rows if r["gate"] == g]
        out[g] = {
            "n": len(rs),
            "avg_r": round(sum(rs) / len(rs), 3),
            "win_rate": round(sum(1 for x in rs if x > 0) / len(rs), 3),
        }
    return out


def propose(stats: dict[str, dict[str, float]], sc: dict[str, Any]) -> list[dict[str, Any]]:
    """Every move the evidence supports, strongest first (avg R x sqrt(n))."""
    moves = []
    for g, (_, key, step, (lo, hi), default) in GATES.items():
        s = stats.get(g)
        if not s or s["n"] < MIN_N:
            continue
        cur = float(sc.get(key, default))
        loosened = (cur - default) * (1 if step > 0 else -1) > 1e-9
        if s["avg_r"] >= EDGE_R and s["win_rate"] >= 0.5:
            new, why = cur + step, "its rejects would have made money"
        elif s["avg_r"] <= -EDGE_R and loosened:
            new, why = cur - step, "its rejects lose, back toward the default"
        else:
            continue
        new = round(min(hi, max(lo, new)), 4)
        if new != cur:
            moves.append({"gate": g, "key": key, "from": cur, "to": new, "why": why, **s})
    p = stats.get("passed")
    cur = float(sc.get("min_score", 55.0))
    if p and p["n"] >= MIN_N and p["avg_r"] <= -EDGE_R and cur < 70:
        moves.append(
            {
                "gate": "passed",
                "key": "min_score",
                "from": cur,
                "to": min(70.0, cur + 5),
                "why": "names that passed every gate lose",
                **p,
            }
        )
    return sorted(moves, key=lambda m: -abs(m["avg_r"]) * math.sqrt(m["n"]))


def review(trader: Path, date: str, apply: bool, offline: Path | None = None) -> dict[str, Any]:
    cfg_path = trader / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig")) if cfg_path.exists() else {}
    if cfg.get("paper") is not True:
        raise SystemExit('optimize: trader/config.json must have "paper": true')
    sc = cfg.setdefault("scout", {})
    rule = {
        "or_minutes": int(sc.get("or_minutes", 5)),
        "min_stop_atr": float(sc.get("min_stop_atr", 0.25)),
        "entry_from": cfg.get("no_entries_before", "09:45"),
        "entry_until": cfg.get("no_entries_after", "15:30"),
        "flat_by": cfg.get("flat_by", "15:55"),
    }
    k = float(str(sc.get("target", "2R")).split(",")[0].rstrip("Rr") or 2)
    opt = trader / "optimizer"
    opt.mkdir(parents=True, exist_ok=True)
    hist_path = opt / "history.jsonl"
    rows = [json.loads(x) for x in hist_path.read_text().splitlines()] if hist_path.exists() else []
    rows = [r for r in rows if r["date"] != date]  # a re-run replaces today's rows
    today = []
    for sym, info in todays_rejects(trader, date).items():
        try:
            late = info.get("seen") if info["gate"] == "chase" else None
            t = counterfactual(backtest.get_chart(sym, offline), date, rule, k, late or None)
        except Exception as e:  # noqa: BLE001 - one bad chart must not stop the review
            print(f"optimize: {sym}: {e}", file=sys.stderr)
            continue
        if t:
            today.append({"date": date, "symbol": sym, **info, "r": t["r"], "how": t["how"]})
    rows += today
    hist_path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    dates = sorted({r["date"] for r in rows})[-WINDOW:]
    window = [r for r in rows if r["date"] in dates]
    stats = gate_stats(window)
    moves = propose(stats, sc)
    applied = None
    if apply and moves:
        applied = moves[0]
        sc[applied["key"]] = applied["to"]
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        with (opt / "changes.jsonl").open("a") as fh:
            fh.write(json.dumps({"date": date, **applied}) + "\n")
    lines = [
        f"\n## Nightly review {date} · {len(today)} rejected names replayed, "
        f"window {dates[0] if dates else '-'} to {dates[-1] if dates else '-'}\n",
        "What the desk's own trade would have made, by the gate that stopped it "
        f"(T2 {k:g}R; a counterfactual, real fills are worse):\n",
        "| Gate | Trades | Avg R | Win rate | Tunable |",
        "|---|---|---|---|---|",
    ]
    for g, s in sorted(stats.items(), key=lambda x: -x[1]["n"]):
        tun = GATES[g][1] if g in GATES else ("min_score" if g == "passed" else "report only")
        lines.append(f"| {g} | {s['n']} | {s['avg_r']:+.3f} | {s['win_rate']:.0%} | {tun} |")
    if applied:
        a = applied
        lines.append(
            f"\n**Changed:** `{a['key']}` {a['from']:g} -> {a['to']:g} (gate {a['gate']}: "
            f"{a['why']}; {a['n']} trades, avg R {a['avg_r']:+.3f})."
        )
    elif moves:
        lines.append(
            "\n**Proposed, not applied:** "
            + "; ".join(
                f"`{m['key']}` {m['from']:g} -> {m['to']:g} ({m['gate']}, avg R {m['avg_r']:+.3f}, "
                f"n {m['n']})"
                for m in moves
            )
        )
    else:
        lines.append(f"\nNo change: no gate has {MIN_N}+ replays with avg R beyond ±{EDGE_R}.")
    log = trader / "scout-log.md"
    if not log.exists():
        append_log(log, {"candidates": []}, "-")
    with log.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return {
        "date": date,
        "replayed": len(today),
        "stats": stats,
        "moves": moves,
        "applied": applied,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("review", help="replay today's rejects and tune one gate")
    r.add_argument("--date", default=dt.datetime.now(features.NY).date().isoformat())
    r.add_argument("--apply", action="store_true", help="write the best move to the config")
    r.add_argument("--trader-dir", type=Path, default=Path("trader"))
    r.add_argument("--offline-dir", type=Path, help="saved 60-day charts, SYM.json(.gz)")
    a = ap.parse_args(argv)
    res = review(a.trader_dir, a.date, a.apply, a.offline_dir)
    print(
        json.dumps(
            {k: v for k, v in res.items() if k != "stats"} | {"stats": res["stats"]}, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
