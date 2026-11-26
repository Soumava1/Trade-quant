"""The desk's short side: opening-range breakdowns on the shortlist, as scout-shaped candidates.

Only names whose short replay made money (the nightly backtest's `short_backtest` "keep" on
the shortlist) are looked at, and only while the market (SPY) trades under its own VWAP.

  entry   a sell-stop one cent under the 5-minute opening-range low, live from 9:45: if a
          bar has already traded through it, a market short while the price is at most 1 %
          under that trigger and under VWAP; if not, before 11:00 and within max_pending_pct
          over it, a resting sell-stop that fills only on the breakdown
  stop    the opening-range high, but at least min_stop_atr x the daily ATR over the entry
  T1/T2   half covered at -1R (the rest's stop to the entry), the rest at the backtest's best
          kR; flat by 15:55 like every desk trade
  gates   stocks in play (RVOL, ATR, average volume), the price range, and Alpaca must list
          it as shortable and easy to borrow
  size    max_loss_per_trade_usd at the stop, at most short_position_pct of the budget

`desk.py shorts` writes these to trader/shorts/<time>/summary.json in the scout's format, so
`desk.py arm SYM --ask <bid> --summary <that file>` runs the same risk check, approval and
T1/T2 split as a long. trader-risk refuses every short unless "allow_short" is true.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import urllib.request
from pathlib import Path
from typing import Any

import features

OPEN = 9 * 60 + 30
SHORT_POSITION_PCT = 25.0  # the scout's lowest position cap: shorts are lower conviction
PENDING_CUTOFF = dt.time(11, 0)


def borrowable(symbol: str) -> tuple[bool, str]:
    """Alpaca's word on shorting it: shortable and easy to borrow (paper API, keys from env)."""
    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not (key and secret):
        return False, "no Alpaca keys in the environment to check borrow"
    req = urllib.request.Request(  # noqa: S310 - fixed https host
        f"https://paper-api.alpaca.markets/v2/assets/{symbol}",
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - fixed https host
            a = json.loads(r.read())
    except OSError as e:
        return False, f"borrow check failed: {e}"
    if not a.get("shortable"):
        return False, "Alpaca lists it as not shortable"
    if not a.get("easy_to_borrow"):
        return False, "hard to borrow on Alpaca"
    return True, ""


def plan_short(
    sym: str,
    chart: dict[str, Any],
    now: dt.datetime,
    cfg: dict[str, Any],
    t2_r: float,
) -> dict[str, Any]:
    """One name: a scout-shaped candidate (tier PICK) or a REJECT with its reason."""
    sc = cfg.get("scout") or {}
    date = now.strftime("%Y-%m-%d")
    minute = now.hour * 60 + now.minute
    bar = (minute - OPEN) // 5 - 1  # the last closed 5-minute bar
    rej = {"symbol": sym, "tier": "REJECT", "side": "sell", "score": 0.0}
    if bar < 0:
        return rej | {"reasons": ["market not open long enough"]}
    or_min = int(sc.get("or_minutes", 5))
    f = features.compute(chart, date, bar, or_min)
    if "error" in f:
        return rej | {"reasons": [f"features: {f['error']}"]}
    today = [b for b in features.sessions(chart).get(date, []) if b[0] <= OPEN + 5 * bar]
    or_bars = [b for b in today if b[0] < OPEN + or_min]
    if not or_bars or not today:
        return rej | {"reasons": ["no bars for today's opening range"]}
    or_low, or_high = min(b[3] for b in or_bars), max(b[2] for b in or_bars)
    price = today[-1][4]
    if f["rvol"] < float(sc.get("min_rvol", 1.0)):
        return rej | {"reasons": [f"not in play: opening-range volume {f['rvol']:.2f}x normal"]}
    if f["atr"] < float(sc.get("min_atr", 0.5)):
        return rej | {"reasons": [f"too quiet: daily ATR ${f['atr']:.2f}"]}
    if f["adv"] < float(sc.get("min_adv", 1_000_000)):
        return rej | {"reasons": [f"too thin: {f['adv']:,} shares a day on average"]}
    lo, hi = sc.get("min_price"), sc.get("max_price")
    if (lo and price < lo) or (hi and price > hi):
        return rej | {"reasons": [f"price ${price:.2f} outside the scout's range"]}

    trigger = round(or_low - 0.01, 2)
    start = OPEN + max(or_min, 15)  # no entries before 9:45, like the long side
    broke = any(b[3] <= trigger for b in today if b[0] >= start)
    if broke:
        if price > trigger:
            return rej | {"reasons": ["failed breakdown: back over the range low"]}
        if price < trigger * 0.99:
            return rej | {"reasons": [f"extended: {1 - price / trigger:.2%} under the range low"]}
        if f["above_vwap"]:
            return rej | {"reasons": ["above VWAP: no short into strength"]}
        entry, kind = price, "market"
    else:
        gap = (price / trigger - 1) * 100
        cap = float(sc.get("max_pending_pct", 3.0))
        if now.time() >= PENDING_CUTOFF or gap > cap or minute < start:
            return rej | {"reasons": [f"no breakdown, {gap:.1f}% over the range low"]}
        entry, kind = trigger, "stop"

    stop = math.ceil(max(or_high, entry + float(sc.get("min_stop_atr", 0.25)) * f["atr"]) * 100)
    stop /= 100
    r = stop - entry
    if r <= 0:
        return rej | {"reasons": ["no room for a stop over the entry"]}
    target = round(entry - t2_r * r, 2)
    budget = float(cfg.get("budget", 0))
    max_loss = float(cfg.get("max_loss_per_trade_usd", 0))
    qty = int(min(max_loss / r, budget * SHORT_POSITION_PCT / 100 / entry))
    if qty < 1:
        return rej | {"reasons": [f"size: the ${r:.2f} risk a share is over the max loss"]}
    ok, why = borrowable(sym)
    if not ok:
        return rej | {"reasons": [why]}
    order = {
        "symbol": sym,
        "side": "sell",
        "qty": str(qty),
        "type": kind,
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit_limit_price": f"{target:.2f}",
        "stop_loss_stop_price": f"{stop:.2f}",
    }
    if kind == "stop":
        order["stop_price"] = f"{trigger:.2f}"
    cand = {
        "symbol": sym,
        "tier": "PICK",
        "side": "sell",
        "score": 0.0,
        "reasons": [f"short breakdown, {t2_r:g}R target"],
        "plan": {"entry": entry, "stop": stop, "target": target, "target_r": t2_r},
        "size": {"qty": qty, "max_loss": round(qty * r, 2)},
        "order": order,
        "features": f,
    }
    if kind == "stop":
        cand["pending"] = {"price": trigger}
    return cand


def run(now: dt.datetime, cfg: dict[str, Any], shortlist: Path, out_dir: Path, chart_of=None):
    """Plan every short-side name; write the scout-shaped summary; return its path."""
    chart_of = chart_of or (lambda s: features.fetch_chart(s, "1mo", "5m"))
    stamp = now.strftime("%Y-%m-%d_%H%M%S")
    run_dir = out_dir / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    doc = json.loads(shortlist.read_text(encoding="utf-8-sig")) if shortlist.exists() else {}
    names = [
        (h["symbol"].upper(), h["short_backtest"])
        for h in doc.get("hits", [])
        if (h.get("short_backtest") or {}).get("verdict") == "keep"
    ]
    cands: list[dict[str, Any]] = []
    notes = []
    if not cfg.get("allow_short"):
        notes.append('short selling is off ("allow_short": false)')
    elif not names:
        notes.append("no shortlist name has a winning short backtest")
    else:
        spy = features.market_regime(chart_of("SPY"), now.strftime("%Y-%m-%d"))
        if spy.get("above_vwap") or "error" in spy:
            notes.append("market (SPY) at or over its VWAP: no shorts")
        else:
            for sym, bt in names:
                t2 = float(str(bt.get("t2", "2R")).rstrip("R") or 2)
                try:
                    cands.append(plan_short(sym, chart_of(sym), now, cfg, t2))
                except (OSError, ValueError, KeyError) as e:
                    cands.append({"symbol": sym, "tier": "REJECT", "reasons": [f"data: {e}"]})
    summary = {
        "ok": True,
        "as_of": now.strftime("%Y-%m-%d %H:%M ET"),
        "side": "sell",
        "candidates": cands,
        "notes": notes,
    }
    path = run_dir / "summary.json"
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return path
