"""Stock-in-play features for Trade Scout, from Yahoo 5-minute bars.

The factors follow Zarattini, Barbon & Aziz, "A Profitable Day Trading Strategy For The
U.S. Equity Market" (Swiss Finance Institute RP 24-98): trade opening-range breakouts only in
"stocks in play", i.e. relative volume of the opening range of at least 100 %, average daily
volume of at least 1M shares, a 14-day ATR above $0.50 and a price above $5, ranked by relative
volume; a stop at 10 % of the daily ATR; hold to the close. Practitioner filters add VWAP
(long only above it) and the market's direction (SPY above its own VWAP).

One request gives everything: 60 days of 5-minute bars (`range=60d&interval=5m`), the same
file the Time Machine needs, so it is saved and handed to the Time Machine with --offline.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UA = "Mozilla/5.0 (compatible; trade-scout/1.0)"
OPEN_MIN = 9 * 60 + 30


def fetch_chart(symbol: str, range_: str = "60d", interval: str = "5m") -> dict[str, Any]:
    """Yahoo's chart JSON. query2 first, query1 on a 429 or 5xx, one retry after 5 s."""
    path = (
        f"/v8/finance/chart/{symbol.upper().replace('.', '-')}?range={range_}&interval={interval}"
    )
    last: Exception | None = None
    for attempt, host in enumerate(("query2", "query1", "query2")):
        if attempt == 2:
            time.sleep(5)
        req = urllib.request.Request(
            f"https://{host}.finance.yahoo.com{path}", headers={"User-Agent": UA}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - fixed https host
                data: dict[str, Any] = json.loads(r.read().decode("utf-8"))
                return data
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 429 and e.code < 500:
                break
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
    raise RuntimeError(f"Yahoo chart for {symbol}: {last}")


def load_chart(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if path.suffix == ".gz":
        raw = gzip.decompress(raw)
    data: dict[str, Any] = json.loads(raw.decode("utf-8"))
    return data


def save_chart(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


Bar = tuple[int, float, float, float, float, float]  # (minute of day, o, h, l, c, v)


def sessions(chart: dict[str, Any]) -> dict[str, list[Bar]]:
    """Regular-session 5-minute bars grouped by New York date, nulls dropped."""
    res = (chart.get("chart") or {}).get("result") or []
    if not res:
        return {}
    r = res[0]
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    out: dict[str, list[Bar]] = {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    for i, ts in enumerate(r.get("timestamp") or []):
        vals = [c[i] if i < len(c) else None for c in cols]
        if any(v is None for v in vals):
            continue
        t = dt.datetime.fromtimestamp(ts, NY)
        minute = t.hour * 60 + t.minute
        if minute < OPEN_MIN or minute >= 16 * 60 or minute % 5:
            continue
        o, h, lo, c, v = (float(x) for x in vals)  # type: ignore[arg-type]
        out.setdefault(t.date().isoformat(), []).append((minute, o, h, lo, c, v))
    for bars in out.values():
        bars.sort()
    return out


def compute(
    chart: dict[str, Any],
    date: str,
    entry_bar: int,
    or_minutes: int = 15,
    lookback: int = 14,
) -> dict[str, Any]:
    """Features for the session `date`, using today's bars up to and including entry_bar."""
    days = sessions(chart)
    today = days.get(date) or []
    past = [d for d in sorted(days) if d < date and len(days[d]) >= 70][-lookback:]
    if not today or len(past) < 5:
        return {"error": f"not enough bars ({len(past)} full past sessions, today {len(today)})"}
    n_or = max(1, or_minutes // 5)

    # Daily ATR(14) and average daily volume from the 5-minute bars.
    trs: list[float] = []
    vols: list[float] = []
    prev_close: float | None = None
    for d in sorted(days):
        if d >= date or len(days[d]) < 70:
            continue
        bars = days[d]
        hi, lo, cl = max(b[2] for b in bars), min(b[3] for b in bars), bars[-1][4]
        tr = (
            hi - lo
            if prev_close is None
            else max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        )
        trs.append(tr)
        vols.append(sum(b[5] for b in bars))
        prev_close = cl
    atr = sum(trs[-lookback:]) / len(trs[-lookback:]) if trs else 0.0
    adv = sum(vols[-lookback:]) / len(vols[-lookback:]) if vols else 0.0

    # Relative volume of the opening range vs the same minutes on past sessions.
    def or_vol(bars: list[Bar]) -> float:
        return sum(b[5] for b in bars if b[0] < OPEN_MIN + n_or * 5)

    past_or = [or_vol(days[d]) for d in past]
    avg_or = sum(past_or) / len(past_or) if past_or else 0.0
    today_or = or_vol(today)
    rvol = today_or / avg_or if avg_or else 0.0

    or_bars = [b for b in today if b[0] < OPEN_MIN + n_or * 5]
    or_high = max(b[2] for b in or_bars) if or_bars else None
    or_low = min(b[3] for b in or_bars) if or_bars else None

    # VWAP and price at the entry bar.
    upto = [b for b in today if b[0] <= OPEN_MIN + 5 * entry_bar]
    pv = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in upto)
    vv = sum(b[5] for b in upto)
    vwap = pv / vv if vv else None
    price = upto[-1][4] if upto else None
    gap = (today[0][1] / prev_close - 1) if prev_close else None
    return {
        "atr": round(atr, 4),
        "adv": round(adv),
        "rvol": round(rvol, 2),
        "gap_pct": round(gap * 100, 2) if gap is not None else None,
        "or_atr": round((or_high - or_low) / atr, 3) if (or_bars and atr) else None,
        "vwap": round(vwap, 4) if vwap is not None else None,
        "price": price,
        "above_vwap": (price is not None and vwap is not None and price > vwap),
        "prev_close": prev_close,
        "sessions": len(past),
    }


def market_regime(chart: dict[str, Any], date: str) -> dict[str, Any]:
    """SPY (or any index ETF): last price vs its session VWAP and vs yesterday's close."""
    days = sessions(chart)
    today = days.get(date) or []
    prior = [d for d in sorted(days) if d < date]
    if not today:
        return {"error": "no bars today"}
    pv = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in today)
    vv = sum(b[5] for b in today)
    vwap = pv / vv if vv else None
    last = today[-1][4]
    prev = days[prior[-1]][-1][4] if prior else None
    return {
        "last": last,
        "vwap": round(vwap, 4) if vwap else None,
        "above_vwap": bool(vwap and last > vwap),
        "vs_prev_pct": round((last / prev - 1) * 100, 2) if prev else None,
    }
