"""The prop-firm challenge guard both desks pass before any new trade (paper).

Modelled on the strictest common challenge rules, on a challenge account of `size` dollars
(the stock desk's budget plus the crypto desk's) kept on the shared Alpaca paper account:

  trailing drawdown  equity, open trades included, may never fall max_drawdown_pct under its
                     highest point so far (intraday trailing, the strictest kind): the floor
                     only rises
  daily loss         equity may never fall daily_loss_pct under the day's starting equity
  profit target      at profit_target_pct the challenge is passed: no new trades
  consistency        once a day has made consistency_pct of the target, no new entries that
                     day (no single day may carry the challenge)
  risk per trade     at most max_risk_pct of the account, and at most risk_of_room of the
                     room left to the drawdown floor or to the daily limit, whichever is
                     smaller; every open trade's risk is counted in that room first
  weekend            nothing held over the weekend (the crypto desk flattens on Fridays)

`desk.py prop --equity E` (E: get_account_info's equity) records the equity, moves the
high-water mark and the day, and writes trader/state/prop.json. `allow()` reads it: a check
older than MAX_AGE, a breach, the target or the day's cap means no new trade.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
STATE = Path("trader/state/prop.json")
MAX_AGE = dt.timedelta(minutes=20)
DEFAULTS: dict[str, Any] = {
    "size": 30000,
    "max_drawdown_pct": 3.0,
    "daily_loss_pct": 2.0,
    "profit_target_pct": 10.0,
    "consistency_pct": 30.0,
    "max_risk_pct": 0.5,
    "risk_of_room": 0.25,
    "min_trading_days": 5,
}


def rules(cfg: dict[str, Any]) -> dict[str, Any]:
    return DEFAULTS | (cfg.get("prop") or {})


def load(path: Path = STATE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def update(
    equity: float, cfg: dict[str, Any], now: dt.datetime | None = None, path: Path = STATE
) -> dict[str, Any]:
    """Record the broker equity; returns the challenge state with its status and room."""
    now = (now or dt.datetime.now(NY)).astimezone(NY)
    r = rules(cfg)
    st = load(path)
    if "baseline" not in st:  # the challenge starts now, at `size`
        st = {
            "baseline": equity,
            "started": now.isoformat(timespec="minutes"),
            "hwm": r["size"],
            "days": {},
            "breach": None,
        }
    acct = round(r["size"] + equity - st["baseline"], 2)  # the challenge account's equity
    day = now.strftime("%Y-%m-%d")
    if st.get("day") != day:
        st |= {"day": day, "day_start": st.get("last", acct)}
    st["hwm"] = max(st["hwm"], acct)
    st["last"] = acct
    st["checked"] = now.isoformat(timespec="seconds")
    day_pnl = round(acct - st["day_start"], 2)
    st["days"][day] = day_pnl
    floor = st["hwm"] - r["size"] * r["max_drawdown_pct"] / 100
    day_floor = st["day_start"] - r["size"] * r["daily_loss_pct"] / 100
    target = r["size"] * r["profit_target_pct"] / 100
    profit = round(acct - r["size"], 2)
    reasons = []
    if acct <= floor:
        st["breach"] = st["breach"] or f"trailing drawdown: {acct:.2f} at or under {floor:.2f}"
    if acct <= day_floor:
        st["breach"] = st["breach"] or f"daily loss: {acct:.2f} at or under {day_floor:.2f}"
    if st["breach"]:
        reasons.append(f"challenge breached ({st['breach']})")
    if profit >= target:
        reasons.append(f"profit target reached (${profit:,.2f} of ${target:,.0f}): passed")
    if day_pnl >= target * r["consistency_pct"] / 100:
        reasons.append(f"today made ${day_pnl:,.2f}, the {r['consistency_pct']:g}% day cap")
    st |= {
        "equity": acct,
        "profit": profit,
        "day_pnl": day_pnl,
        "floor": round(floor, 2),
        "day_floor": round(day_floor, 2),
        "room": round(max(0.0, min(acct - floor, acct - day_floor)), 2),
        "status": "stop" if reasons else "ok",
        "reasons": reasons,
        "trading_days": sum(1 for v in st["days"].values() if v),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st, indent=2) + "\n", encoding="utf-8")
    return st


def open_risk(stock_trades: dict[str, Any], crypto_open: dict[str, Any]) -> float:
    """Dollars lost if every open stop is hit (a stop at or past the entry risks nothing)."""
    total = 0.0
    for t in stock_trades.values():
        if t.get("breakeven") or t.get("entry") is None or t.get("stop") is None:
            continue
        qty = sum(float(q) for q in (t.get("legs") or [0]))
        total += max(0.0, abs(float(t["entry"]) - float(t["stop"]))) * qty
    for p in crypto_open.values():
        if "entry" in p and "stop" in p:
            total += max(0.0, (p["entry"] - p["stop"]) * float(p.get("qty", 0)))
    return round(total, 2)


def allow(
    risk: float,
    cfg: dict[str, Any],
    open_usd: float,
    now: dt.datetime | None = None,
    path: Path = STATE,
) -> tuple[bool, str]:
    """May a new trade risking `risk` dollars be opened now? (False, why) if not."""
    now = (now or dt.datetime.now(NY)).astimezone(NY)
    r = rules(cfg)
    st = load(path)
    if not st.get("checked"):
        return False, "prop: no challenge check yet (run `desk.py prop --equity E` first)"
    age = now - dt.datetime.fromisoformat(st["checked"])
    if age > MAX_AGE:
        return False, f"prop: the challenge check is {age.seconds // 60} min old; check again"
    if st["status"] != "ok":
        return False, "prop: " + "; ".join(st["reasons"])
    cap = r["size"] * r["max_risk_pct"] / 100
    room = st["room"] - open_usd
    allowed = min(cap, max(0.0, room) * r["risk_of_room"])
    if risk > allowed + 0.01:
        return False, (
            f"prop: ${risk:.2f} at risk is over the ${allowed:.2f} allowed "
            f"(${cap:.0f} cap; ${st['room']:.2f} to the floor less ${open_usd:.2f} open risk, "
            f"x{r['risk_of_room']:g})"
        )
    return True, ""
