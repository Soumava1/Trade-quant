#!/usr/bin/env python3
"""The PAPER crypto desk: a 24-hour breakout, more reward for less risk, a hard 3 % drawdown cap.

The rule, on 1-hour bars (Alpaca's public crypto data):

  entry   checked every CHECK hours: one of the last CHECK closed bars closed above the high of
          the 24 bars before it, the price is above the 72-hour EMA (the trend), and the price
          now is at most max_chase_pct over that breakout level (never chased further)
  stop    entry - stop_atr x ATR(14, 1-hour bars), resting on Alpaca as a GTC stop-limit
  T1      at +t1_r R, half is sold and the rest's stop moves to the entry (breakeven)
  T2      the rest at +t2_r R. At base_hold_hours (24) the rest is sold, UNLESS T1 has paid
          and the price is still above its trend: then it is a winner, and its stop trails
          trail_atr x ATR under its highest price since the entry, out by max_hold_hours (72)
  risk    risk_per_trade_pct of the crypto budget at the stop, halved once the desk is
          halve_at_dd_pct under its peak. A new trade is refused unless the drawdown so far,
          plus the risk still open in every trade, plus this one, stays under
          max_drawdown_pct - dd_buffer_pct of the budget (the buffer is for gaps and fees):
          the desk can never be more than max_drawdown_pct (3 %) under its peak, even if every
          open stop is hit. Coins move together, so open risk is capped as a total too.

Alpaca has no bracket orders for crypto, so only the stop rests on the broker; targets, the
24-hour decision and the trail are checked at each run (every CHECK hours). The backtest
models exactly that, with fees on both sides. A coin is traded only if the rule made money on
its last 90 days AND on the most recent half of them (an out-of-sample check against luck),
with enough trades, and enough trading on Alpaca (a thin book fills badly).

Usage (from the project root):
    python crypto.py backtest                 # 90 days per coin -> trader/crypto/backtest.json
    python crypto.py signals                  # entries now, on the coins the backtest kept
    python crypto.py arm BTC/USD --ask A --bid B          # every check; prints the buy order
    python crypto.py stop BTC/USD --qty Q                 # the GTC stop-limit for what is held
    python crypto.py opened BTC/USD --qty Q --price P --stop-id ID
    python crypto.py manage --price BTC/USD=P --held BTC/USD=Q   # what open trades need now
    python crypto.py trailed BTC/USD --stop S                    # after replacing the stop
    python crypto.py done BTC/USD --event t1|t2|time|trail|stopped --qty Q --price P
    python crypto.py status                                      # equity, peak, drawdown
    python crypto.py lock --owner "crypto 04:05" / unlock --token T

Paper only: nothing here talks to a broker; the desk places what these commands print.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "trade-scout"))
import desk  # noqa: E402

DATA = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
STATE = Path("trader/crypto")
LOCK = Path("trader/state/crypto.lock")
MAX_BACKTEST_AGE = dt.timedelta(hours=48)  # an older verdict trades nothing
DEFAULTS: dict[str, Any] = {
    "coins": ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "HYPE/USD"],
    "budget": 5000,
    "risk_per_trade_pct": 0.5,
    "max_drawdown_pct": 3.0,
    "dd_buffer_pct": 0.5,
    "halve_at_dd_pct": 1.5,
    "max_open_risk_pct": 1.5,
    "max_position_pct": 40,
    "max_open": 3,
    "breakout_hours": 24,
    "trend_ema_hours": 72,
    "atr_hours": 14,
    "stop_atr": 1.5,
    "t1_r": 2.0,
    "t2_r": 4.0,
    "base_hold_hours": 24,
    "max_hold_hours": 72,
    "trail_atr": 3.0,
    "check_hours": 4,
    "max_chase_pct": 1.0,
    "max_spread_pct": 0.3,
    "fee_pct": 0.25,
    "backtest_days": 90,
    "min_trades": 5,
    "min_bar_trades": 10,
}
# (unix seconds, open, high, low, close[, trades in the bar])
Bar = tuple


def settings(cfg_path: Path = Path("trader/config.json")) -> dict[str, Any]:
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig")) if cfg_path.exists() else {}
    if cfg.get("paper") is not True:
        raise SystemExit('crypto: trader/config.json must have "paper": true')
    if not isinstance(cfg.get("crypto"), dict):
        raise SystemExit('crypto: trader/config.json has no "crypto" section; the desk stays off')
    return DEFAULTS | cfg["crypto"]


# --------------------------------------------------------------------------- data


def _get(url: str, tries: int = 5) -> dict[str, Any]:
    """GET JSON; the keys from the environment raise the rate limit when they are set."""
    hdrs = {}
    if os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"):
        hdrs = {
            "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
            "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
        }
    for n in range(tries):
        try:
            req = urllib.request.Request(url, headers=hdrs)  # noqa: S310 - fixed https host
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - fixed https host
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code != 429 or n == tries - 1:
                raise
            time.sleep(2 ** (n + 1))  # rate limited: back off and try again
    raise RuntimeError("unreachable")


def fetch_bars(symbol: str, days: int, now: dt.datetime | None = None) -> list[Bar]:
    """Closed 1-hour bars, oldest first, from Alpaca's public crypto feed (paged)."""
    now = now or dt.datetime.now(dt.UTC)
    start = (now - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:00:00Z")
    bars: list[Bar] = []
    token = None
    while True:
        q = {"symbols": symbol, "timeframe": "1Hour", "start": start, "limit": 10000}
        if token:
            q["page_token"] = token
        doc = _get(f"{DATA}?{urllib.parse.urlencode(q)}")
        for b in (doc.get("bars") or {}).get(symbol, []):
            t = int(dt.datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp())
            px = (float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]))
            bars.append((t, *px, int(b.get("n") or 0)))
        token = doc.get("next_page_token")
        if not token:
            break
        time.sleep(0.3)
    cutoff = int(now.timestamp()) - 3600  # the bar still forming is not closed
    return [b for b in bars if b[0] <= cutoff]


# --------------------------------------------------------------------------- the rule


def ema(xs: list[float], n: int) -> list[float]:
    k, out = 2 / (n + 1), []
    for x in xs:
        out.append(x if not out else out[-1] + k * (x - out[-1]))
    return out


def atr(bars: list[Bar], n: int) -> list[float]:
    out, prev = [], None
    for b in bars:
        h, lo, c = b[2], b[3], b[4]
        tr = h - lo if prev is None else max(h - lo, abs(h - prev), abs(lo - prev))
        out.append(tr if not out else out[-1] + (tr - out[-1]) / n)
        prev = c
    return out


def indicators(bars: list[Bar], s: dict[str, Any]) -> tuple[list[float], list[float]]:
    return ema([b[4] for b in bars], int(s["trend_ema_hours"])), atr(bars, int(s["atr_hours"]))


def signal_at(bars: list[Bar], i: int, s: dict[str, Any], ema_v: list[float], atr_v: list[float]):
    """A plan if bar i (the last closed bar at a check) has a fresh breakout, else None."""
    n, k = int(s["breakout_hours"]), int(s["check_hours"])
    if i < max(n + k, int(s["trend_ema_hours"])):
        return None
    price = bars[i][4]
    level = None
    for j in range(i - k + 1, i + 1):  # a close above the prior 24-bar high since the last check
        hi = max(b[2] for b in bars[j - n : j])
        if bars[j][4] > hi:
            level = hi
            break
    if level is None or price <= ema_v[i] or price > level * (1 + s["max_chase_pct"] / 100):
        return None
    stop = price - s["stop_atr"] * atr_v[i]
    if stop <= 0 or stop >= price:
        return None
    return {"entry": price, "stop": stop, "level": level}


def trail_stop(high_since: float, atr_now: float, cur: float, s: dict[str, Any]) -> float:
    """The trailing stop: trail_atr x ATR under the highest price since the entry; never lower."""
    return max(cur, high_since - s["trail_atr"] * atr_now)


def trade_from(
    bars: list[Bar],
    i: int,
    plan: dict[str, Any],
    s: dict[str, Any],
    ema_v: list[float],
    atr_v: list[float],
) -> dict:
    """The trade entered at bar i's close, managed bar by bar; R net of fees."""
    entry, stop = plan["entry"], plan["stop"]
    r = entry - stop
    t1, t2 = entry + s["t1_r"] * r, entry + s["t2_r"] * r
    k, base, cap = int(s["check_hours"]), int(s["base_hold_hours"]), int(s["max_hold_hours"])
    cur, h1, h2, how, trailing, high = stop, None, None, "open", False, entry
    for j in range(i + 1, len(bars)):
        o, hi, lo, c = bars[j][1], bars[j][2], bars[j][3], bars[j][4]
        if lo <= cur:  # the resting stop: at the stop, or the open on a gap under it
            px = min(cur, o)
            out = (px - entry) / r
            h1 = out if h1 is None else h1
            h2 = out
            how = "trail stop" if trailing else "stop" if h1 < 0 else "T1 then breakeven"
            break
        high = max(high, hi)
        if (j - i) % k:
            continue
        age = j - i  # a check: targets, the 24-hour decision and the trail, at this price
        if h1 is None and c >= t1:
            h1, cur = (s["t1_r"] if c < t2 else (c - entry) / r), entry
        if c >= t2 or age >= cap:
            out = (c - entry) / r
            h1 = out if h1 is None else h1
            h2, how = out, "T2" if c >= t2 else f"{cap}h exit"
            break
        if age >= base:
            if h1 is not None and h1 > 0 and c > ema_v[j]:  # a winner: let it run on a trail
                trailing = True
                cur = trail_stop(high, atr_v[j], cur, s)
            else:
                out = (c - entry) / r
                h1 = out if h1 is None else h1
                h2, how = out, f"{base}h exit"
                break
    else:
        return {"r": None, "how": "open"}
    fee_r = 2 * s["fee_pct"] / 100 * entry / r
    total = 0.5 * h1 + 0.5 * h2 - fee_r
    return {"r": round(total, 3), "how": how, "entry": round(entry, 6), "stop": round(stop, 6)}


def stats(rs: list[float]) -> dict[str, Any]:
    return {
        "n": len(rs),
        "avg_r": round(sum(rs) / len(rs), 3) if rs else 0.0,
        "win_rate": round(sum(1 for x in rs if x > 0) / len(rs), 3) if rs else 0.0,
        "total_r": round(sum(rs), 2),
    }


def backtest_coin(bars: list[Bar], s: dict[str, Any]) -> dict[str, Any]:
    ema_v, atr_v = indicators(bars, s)
    trades, busy_until, k = [], -1, int(s["check_hours"])
    for i in range(len(bars)):
        if i <= busy_until or (bars[i][0] // 3600) % k != 0:  # checks on the k-hour grid only
            continue
        plan = signal_at(bars, i, s, ema_v, atr_v)
        if not plan:
            continue
        t = trade_from(bars, i, plan, s, ema_v, atr_v)
        if t["r"] is None:
            break
        trades.append(t | {"t": bars[i][0]})
        busy_until = i + int(s["max_hold_hours"])  # one trade per coin at a time
    mid = bars[len(bars) // 2][0] if bars else 0
    all_s = stats([t["r"] for t in trades])
    recent = stats([t["r"] for t in trades if t["t"] >= mid])
    counts = [b[5] for b in bars if len(b) > 5]
    liquidity = statistics.median(counts) if counts else None
    if liquidity is not None and liquidity < s["min_bar_trades"]:
        verdict = "thin book"
    elif all_s["n"] < s["min_trades"]:
        verdict = "thin"
    elif all_s["avg_r"] > 0 and recent["n"] and recent["avg_r"] > 0:
        verdict = "keep"
    else:
        verdict = "drop"
    return {
        "stats": all_s,
        "recent": recent,
        "trades_per_week": round(all_s["n"] / max(1.0, len(bars) / 168), 2),
        "median_trades_per_hour": liquidity,
        "verdict": verdict,
        "trades": trades[-20:],
    }


# --------------------------------------------------------------------------- live desk


def qty_round(q: float, price: float) -> float:
    d = 6 if price >= 1000 else 4 if price >= 10 else 2 if price >= 0.1 else 0
    return math.floor(q * 10**d) / 10**d


def load_state() -> dict[str, Any]:
    p = STATE / "positions.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"open": {}, "closed": []}


def save_state(st: dict[str, Any]) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "positions.json").write_text(json.dumps(st, indent=2) + "\n", encoding="utf-8")


def equity(st: dict[str, Any], s: dict[str, Any]) -> dict[str, float]:
    """Realized equity, its peak, the drawdown from that peak, and the risk still open."""
    eq = peak = float(s["budget"])
    for c in st["closed"]:
        eq += c["pnl"]
        peak = max(peak, eq)
    open_risk = sum(
        max(0.0, (p["entry"] - p["stop"]) * p["qty"] * (0.5 if p.get("t1_done") else 1.0))
        for p in st["open"].values()
        if "entry" in p
    )
    return {
        "equity": round(eq, 2),
        "peak": round(peak, 2),
        "dd": round(peak - eq, 2),
        "open_risk": round(open_risk, 2),
    }


def kept_coins(s: dict[str, Any], now: dt.datetime | None = None) -> list[str]:
    """Coins whose backtest made money; none if it is missing or over MAX_BACKTEST_AGE old."""
    p = STATE / "backtest.json"
    if not p.exists():
        return []
    doc = json.loads(p.read_text(encoding="utf-8"))
    age = (now or dt.datetime.now(dt.UTC)) - dt.datetime.fromisoformat(doc["at"])
    if age > MAX_BACKTEST_AGE:
        return []
    return [sym for sym in s["coins"] if (doc["coins"].get(sym) or {}).get("verdict") == "keep"]


def signals(s: dict[str, Any], bars_of=None) -> dict[str, Any]:
    bars_of = bars_of or (lambda sym: fetch_bars(sym, 10))
    out: dict[str, Any] = {}
    for sym in kept_coins(s):
        bars = bars_of(sym)
        ema_v, atr_v = indicators(bars, s)
        plan = signal_at(bars, len(bars) - 1, s, ema_v, atr_v) if bars else None
        out[sym] = plan or {"none": "no fresh 24-hour breakout above the 72-hour trend"}
    return out


def arm(sym: str, ask: float, bid: float, s: dict[str, Any], plans: dict[str, Any], now=None):
    """Every check before a crypto buy. Returns (code, order) and logs SKIP or ATTEMPT."""
    now = now or dt.datetime.now(dt.UTC)
    st = load_state()
    plan = plans.get(sym) or {}
    budget = float(s["budget"])

    def skip(why: str):
        desk.log_line("SKIP", sym, "-", f"~{ask}", why)
        return desk.SKIPPED, None

    if not s.get("enabled", True):
        return skip("Crypto desk disabled in the config (enabled: false)")
    if "entry" not in plan:
        return skip(f"No crypto entry: {plan.get('none', 'not a kept coin')}")
    if sym in st["open"]:
        return skip("Already holding it")
    if len(st["open"]) >= s["max_open"]:
        return skip(f"Already {len(st['open'])} crypto trades open (max {s['max_open']})")
    spread = (ask - bid) / ((ask + bid) / 2) * 100
    if spread > s["max_spread_pct"]:
        return skip(f"Spread {spread:.2f}% over the {s['max_spread_pct']}% limit (thin book)")
    if ask > plan["level"] * (1 + s["max_chase_pct"] / 100):
        return skip(f"Stale: ask {ask} is over {s['max_chase_pct']}% past the breakout level")
    stop = plan["stop"]
    if ask <= stop:
        return skip(f"Ask {ask} at or under the stop {stop:.6g}")

    e = equity(st, s)
    risk = budget * s["risk_per_trade_pct"] / 100
    if e["dd"] >= budget * s["halve_at_dd_pct"] / 100:
        risk /= 2  # under pressure: half size until the desk makes a new peak
    room = budget * (s["max_drawdown_pct"] - s["dd_buffer_pct"]) / 100
    if e["dd"] + e["open_risk"] + risk > room:
        return skip(
            f"Drawdown cap: ${e['dd']:.2f} down from the peak + ${e['open_risk']:.2f} open risk "
            f"+ ${risk:.2f} would pass ${room:.2f} ({s['max_drawdown_pct']:g}% cap less a "
            f"{s['dd_buffer_pct']:g}% gap buffer)"
        )
    if e["open_risk"] + risk > budget * s["max_open_risk_pct"] / 100:
        return skip(
            f"Open risk ${e['open_risk']:.2f} + ${risk:.2f} over the "
            f"{s['max_open_risk_pct']:g}% cap (coins move together)"
        )
    r = ask - stop
    qty = qty_round(min(risk / r, budget * s["max_position_pct"] / 100 / ask), ask)
    if qty * ask < 10:
        return skip(f"Size too small: {qty} for ${risk:.2f} at risk")
    t1, t2 = ask + s["t1_r"] * r, ask + s["t2_r"] * r
    pending = {"symbol": sym, "entry": ask, "stop": round(stop, 6), "t1": round(t1, 6)}
    pending |= {"t2": round(t2, 6), "qty": qty, "planned_at": now.isoformat()}
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "pending.json").write_text(json.dumps(pending, indent=2) + "\n", encoding="utf-8")
    desk.log_line(
        "ATTEMPT",
        sym,
        f"{qty:g}",
        f"~{ask}",
        f"Crypto 24h breakout: stop {stop:.6g} ({s['stop_atr']:g} ATR), half at T1 {t1:.6g} "
        f"(+{s['t1_r']:g}R), rest at T2 {t2:.6g} (+{s['t2_r']:g}R), out at 24h unless a "
        f"winner (then trailed, 72h max); risk ${qty * r:.2f}, drawdown ${e['dd']:.2f}",
    )
    order = {"symbol": sym, "side": "buy", "type": "market", "time_in_force": "gtc"}
    return desk.READY, order | {"qty": f"{qty:g}"}


def stop_order(sym: str, qty: float, stop: float) -> dict[str, Any]:
    """The resting GTC stop-limit sell, limit 0.5 % under the stop so a fast drop still fills."""
    return {
        "symbol": sym,
        "side": "sell",
        "type": "stop_limit",
        "time_in_force": "gtc",
        "qty": f"{qty:g}",
        "stop_price": f"{stop:.6g}",
        "limit_price": f"{stop * 0.995:.6g}",
    }


def manage(st, prices, held, s, now=None, bars_of=None):
    """What each open trade needs now, as steps for the desk."""
    now = now or dt.datetime.now(dt.UTC)
    bars_of = bars_of or (lambda sym: fetch_bars(sym, 10))
    steps = []
    for sym, p in st["open"].items():
        q = held.get(sym, 0.0)
        if q <= 0:
            steps.append({"symbol": sym, "do": "stopped", "why": "no longer held: the stop filled"})
            continue
        px = prices.get(sym)
        if px is None:
            steps.append({"symbol": sym, "do": "price", "why": "need the latest price"})
            continue
        age_h = (now - dt.datetime.fromisoformat(p["opened_at"])).total_seconds() / 3600
        close = {"symbol": sym, "do": "close", "qty": q, "cancel": p["stop_id"]}
        if px >= p["t2"]:
            steps.append(close | {"event": "t2", "why": "T2"})
            continue
        if age_h >= s["max_hold_hours"]:
            steps.append(close | {"event": "time", "why": f"{age_h:.0f}h held, the 72h cap"})
            continue
        if not p.get("t1_done") and px >= p["t1"]:
            half = qty_round(q / 2, px)
            steps.append(
                {
                    "symbol": sym,
                    "do": "t1",
                    "event": "t1",
                    "sell": half,
                    "cancel": p["stop_id"],
                    "new_stop": stop_order(sym, qty_round(q - half, px), p["entry"]),
                    "why": "T1",
                }
            )
            continue
        if age_h < s["base_hold_hours"]:
            continue
        recent = bars_of(sym)
        ema_v, atr_v = indicators(recent, s)
        opened = int(dt.datetime.fromisoformat(p["opened_at"]).timestamp())
        bars = [b for b in recent if b[0] >= opened]
        if p.get("t1_done") and px > ema_v[-1] and px > p["entry"]:  # a winner: keep it, trail it
            high = max([px, p.get("high", p["entry"])] + [b[2] for b in bars])
            new = trail_stop(high, atr_v[-1], p["stop"], s)
            if new < px and new > p["stop"] + 1e-12:
                steps.append(
                    {
                        "symbol": sym,
                        "do": "trail",
                        "replace": p["stop_id"],
                        "stop_price": f"{new:.6g}",
                        "limit_price": f"{new * 0.995:.6g}",
                        "high": high,
                        "why": f"winner past {s['base_hold_hours']}h: trail",
                    }
                )
        else:
            why = "24h, not a winner" if not p.get("t1_done") else "24h, back under its trend"
            steps.append(close | {"event": "time", "why": why})
    return steps


def done(sym: str, event: str, qty: float, price: float, stop_id: str | None, now=None) -> str:
    now = now or dt.datetime.now(dt.UTC)
    st = load_state()
    p = st["open"][sym]
    pnl = round((price - p["entry"]) * qty, 2)
    r = (price - p["entry"]) / (p["entry"] - p["first_stop"]) if p.get("first_stop") else 0.0
    st["closed"].append(
        {
            "symbol": sym,
            "event": event,
            "qty": qty,
            "price": price,
            "pnl": pnl,
            "at": now.strftime("%Y-%m-%d %H:%M"),
        }
    )
    if event == "t1":
        p |= {"t1_done": True, "stop_id": stop_id or p["stop_id"], "stop": p["entry"]}
        why = f"T1 hit: +{r:.1f}R on half; the rest's stop moved to breakeven ${p['entry']:g}"
        action = "EXIT"
    else:
        del st["open"][sym]
        why = {
            "t2": f"T2 hit: {r:+.1f}R",
            "time": f"Time exit: out at {r:+.1f}R",
            "trail": f"Trailing stop filled: {r:+.1f}R",
            "stopped": f"Stop filled: {r:+.1f}R",
        }[event]
        action = "CLOSE" if event == "time" else "EXIT"
    save_state(st)
    e = equity(st, settings_or_defaults())
    return desk.log_line(
        action,
        sym,
        f"{qty:g}",
        f"{price}",
        f"{why} (P&L ${pnl:+.2f}; crypto equity ${e['equity']:.2f}, ${e['dd']:.2f} under peak)",
    )


def settings_or_defaults() -> dict[str, Any]:
    try:
        return settings()
    except SystemExit:
        return DEFAULTS


# --------------------------------------------------------------------------- CLI


def kv(items: list[str]) -> dict[str, float]:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        out[k.upper()] = float(v)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backtest", help="replay the rule on each coin's last 90 days")
    sub.add_parser("signals", help="entries now, on the coins the backtest kept")
    sub.add_parser("status", help="crypto equity, its peak, the drawdown and open risk")
    p = sub.add_parser("arm", help="every check before a buy; prints the order")
    p.add_argument("symbol", type=str.upper)
    p.add_argument("--ask", type=float, required=True)
    p.add_argument("--bid", type=float, required=True)
    p = sub.add_parser("stop", help="the resting stop-limit for what is held")
    p.add_argument("symbol", type=str.upper)
    p.add_argument("--qty", type=float, required=True)
    p = sub.add_parser("opened", help="record a filled buy and its resting stop")
    p.add_argument("symbol", type=str.upper)
    p.add_argument("--qty", type=float, required=True)
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--stop-id", required=True)
    p = sub.add_parser("manage", help="what open trades need now")
    p.add_argument("--price", nargs="*", default=[], help="SYM=PRICE")
    p.add_argument("--held", nargs="*", default=[], help="SYM=QTY from get_all_positions")
    p = sub.add_parser("trailed", help="record a stop raised by the trail")
    p.add_argument("symbol", type=str.upper)
    p.add_argument("--stop", type=float, required=True)
    p.add_argument("--high", type=float)
    p.add_argument("--stop-id", help="the stop order's id after the replace, if it changed")
    p = sub.add_parser("done", help="record an exit")
    p.add_argument("symbol", type=str.upper)
    p.add_argument("--event", required=True, choices=["t1", "t2", "time", "trail", "stopped"])
    p.add_argument("--qty", type=float, required=True)
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--stop-id", help="t1: the new breakeven stop's order id")
    p = sub.add_parser("lock")
    p.add_argument("--owner", default="crypto")
    p = sub.add_parser("unlock")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--token")
    g.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "lock":
        ok, msg, token = desk.acquire(a.owner, path=LOCK)
        print(msg + (f"\ntoken {token}" if ok else ""))
        return 0 if ok else desk.BUSY
    if a.cmd == "unlock":
        if desk.release(LOCK, None if a.force else a.token):
            print("lock released")
        return 0
    s = settings()
    if a.cmd == "status":
        print(
            json.dumps(equity(load_state(), s) | {"cap": s["budget"] * s["max_drawdown_pct"] / 100})
        )
        return 0
    if a.cmd == "backtest":
        res = {}
        for sym in s["coins"]:
            try:
                res[sym] = backtest_coin(fetch_bars(sym, int(s["backtest_days"])), s)
            except Exception as e:  # noqa: BLE001 - one coin's data must not stop the rest
                res[sym] = {"verdict": "no data", "error": str(e)[:200]}
        STATE.mkdir(parents=True, exist_ok=True)
        doc = {"at": dt.datetime.now(dt.UTC).isoformat(timespec="minutes"), "coins": res}
        (STATE / "backtest.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        kept = [x for x in res.values() if x["verdict"] == "keep"]
        for sym, r in sorted(
            res.items(), key=lambda x: -(x[1].get("stats") or {}).get("avg_r", -9)
        ):
            st_, rc = r.get("stats") or {}, r.get("recent") or {}
            print(
                f"{sym:11} {r['verdict']:9} n {st_.get('n', 0):3} avg R {st_.get('avg_r', 0):+.3f} "
                f"win {st_.get('win_rate', 0):.0%} | last 45d n {rc.get('n', 0):2} avg R "
                f"{rc.get('avg_r', 0):+.3f} | {r.get('trades_per_week', 0):.1f}/week"
            )
        print(
            f"kept {len(kept)}: about {sum(x['trades_per_week'] for x in kept):.1f} trades a week"
        )
        return 0
    if a.cmd == "signals":
        print(json.dumps(signals(s), indent=2))
        return 0
    if a.cmd == "arm":
        code, order = arm(a.symbol, a.ask, a.bid, s, signals(s))
        if code == desk.READY:
            print("READY. Call place_stock_order once, exactly:\n" + json.dumps(order, indent=2))
        else:
            print("SKIPPED (logged)")
        return code
    if a.cmd == "stop":
        pend = json.loads((STATE / "pending.json").read_text(encoding="utf-8"))
        print(json.dumps(stop_order(a.symbol, a.qty, pend["stop"]), indent=2))
        return 0
    if a.cmd == "opened":
        pend = json.loads((STATE / "pending.json").read_text(encoding="utf-8"))
        st = load_state()
        r = a.price - pend["stop"]
        st["open"][a.symbol] = pend | {
            "entry": a.price,
            "first_stop": pend["stop"],
            "t1": round(a.price + s["t1_r"] * r, 6),
            "t2": round(a.price + s["t2_r"] * r, 6),
            "qty": a.qty,
            "stop_id": a.stop_id,
            "opened_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }
        save_state(st)
        (STATE / "pending.json").unlink()
        p = st["open"][a.symbol]
        print(
            desk.log_line(
                "BUY",
                a.symbol,
                f"{a.qty:g}",
                f"{a.price}",
                f"Crypto breakout: stop {p['stop']:g} resting (GTC), T1 {p['t1']:g}, "
                f"T2 {p['t2']:g}, out at 24h unless a winner (trailed, 72h max)",
            )
        )
        return 0
    if a.cmd == "manage":
        print(json.dumps(manage(load_state(), kv(a.price), kv(a.held), s), indent=2))
        return 0
    if a.cmd == "trailed":
        st = load_state()
        p = st["open"][a.symbol]
        if a.stop <= p["stop"]:
            print(
                f"Not raised: {a.stop} is not above the current stop {p['stop']}", file=sys.stderr
            )
            return 2
        p |= {"stop": a.stop, "high": a.high or p.get("high", p["entry"])}
        if a.stop_id:
            p["stop_id"] = a.stop_id
        save_state(st)
        print(f"{a.symbol}: trailing stop raised to {a.stop:g}.")
        return 0
    if a.cmd == "done":
        print(done(a.symbol, a.event, a.qty, a.price, a.stop_id))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
