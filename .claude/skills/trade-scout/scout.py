#!/usr/bin/env python3
"""Trade Scout: run the Day Trader pipeline over a scan and rank what survives.

One run does, in order:

  1. trader-scanner      orb scan (the whole universe, a watchlist or a list of symbols)
  2. trader-news         headlines and 8-Ks for the candidates, one call
  3. trader-entry        per candidate: has the opening-range breakout triggered, and when
  4. trader-exit         per candidate: stop and target (or-mid / 2R by default)
  5. trader-timemachine  per candidate: 10,000 replays plus every past session the rule fired
  6. trader-budget       per candidate: whole shares inside your limits, one bracket order
  7. trader-risk         ONE batch check of the shortlisted orders, best first, so the
                         daily loss limit and trades-per-day hold across the whole list

Each candidate runs in its own folder (the tools read and write `trader/...` relative to
the working directory), seeded with copies of your `trader/config.json`, `portfolio.json`
and `state/broker.json`. Your own `trader/` folder is only read, never written, except the
run folder under `trader/scout/` (and `universe.txt`, cached after the first live scan).
Nothing here places an order, and the risk check runs with `--no-write`, so trader-broker
will not show any of these orders until you run /trader-risk on the one you choose.

Usage:
    python3 scout.py                                   # live: full-universe orb scan
    python3 scout.py --symbols TSLA,NVDA,AMD           # or --watchlist trader/watchlist.json
    python3 scout.py --top 3 --target 1.5R --stop or-low
    python3 scout.py --offline-dir DIR --now "2026-10-19 10:15"   # replay saved data

Exit code 0 when at least one HIGH conviction trade passed every gate, 1 when none did,
2 when the run could not start.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import random
import shutil
import subprocess  # noqa: S404 - runs the sibling trader-* scripts only
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SKILLS = HERE.parent
TOOLS = {
    "scan": SKILLS / "trader-scanner" / "scan.py",
    "news": SKILLS / "trader-news" / "news.py",
    "entry": SKILLS / "trader-entry" / "entry.py",
    "exit": SKILLS / "trader-exit" / "exit.py",
    "tm": SKILLS / "trader-timemachine" / "timemachine.py",
    "size": SKILLS / "trader-budget" / "size.py",
    "risk": SKILLS / "trader-risk" / "check.py",
}
EXAMPLE_CONFIG = SKILLS / "trader-budget" / "config.example.json"

# News that vetoes a day trade outright: halts, risk items,
# and 8-K items for bankruptcy (1.03), cyber incident (1.05), impairment (2.06),
# delisting notice (3.01) and non-reliance on past financials (4.02).
VETO_TAGS = {"halt", "risk"}
VETO_8K_ITEMS = {"1.03", "1.05", "2.06", "3.01", "4.02"}
# Legal and regulatory headlines are cautions: the keyword tags fire on routine and even
# good news (a probe closed, an approval), so they are shown on the trade, not a veto.
CAUTION_TAGS = {"earnings", "deal", "capital", "people", "analyst", "legal", "regulatory"}

HIGH = "HIGH"
PICK = "PICK"
WATCH = "WATCH"
REJECT = "REJECT"
TIER_ORDER = {HIGH: 0, PICK: 1, WATCH: 2, REJECT: 3}

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SKILLS / "trade-backtest"))
import features  # noqa: E402,I001
import journal  # noqa: E402
import backtest  # noqa: E402
import optimize  # noqa: E402


# --------------------------------------------------------------------------- running tools


@dataclass
class ToolResult:
    code: int
    data: Any
    text: str


def run_tool(name: str, args: list[str], cwd: Path, timeout: int = 900) -> ToolResult:
    """Run one trader-* script and parse the JSON it printed (or keep its last line)."""
    cmd = [sys.executable, str(TOOLS[name]), *args]
    try:
        proc = subprocess.run(  # noqa: S603 - fixed interpreter and sibling script paths
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return ToolResult(124, None, f"{name} timed out after {timeout}s")
    out = proc.stdout.strip()
    data: Any = None
    if out:
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            data = None
    lines = (proc.stderr.strip() or out).splitlines()
    msg = lines[-1] if lines and data is None else ""
    return ToolResult(proc.returncode, data, msg)


def seed_workspace(dest: Path, trader_dir: Path) -> Path:
    """A fresh working folder with copies of the user's config, portfolio and broker log."""
    tdir = dest / "trader"
    (tdir / "state").mkdir(parents=True, exist_ok=True)
    cfg = trader_dir / "config.json"
    shutil.copyfile(cfg if cfg.exists() else EXAMPLE_CONFIG, tdir / "config.json")
    for rel in ("portfolio.json", "prices.json", "universe.txt", "state/broker.json"):
        src = trader_dir / rel
        if src.exists():
            shutil.copyfile(src, tdir / rel)
    return dest


# --------------------------------------------------------------------------- scoring


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# Conviction-staggered position cap: (minimum score, max % of the budget in one position).
# The first row whose minimum the score meets wins; the user's own max_position_pct is
# always the ceiling. The max loss per trade still applies on top.
DEFAULT_CAPS: tuple[tuple[float, float], ...] = (
    (95, 75),
    (85, 55),
    (75, 35),
    (65, 20),
    (0, 10),
)


def parse_caps(text: str) -> tuple[tuple[float, float], ...]:
    """'95:75,85:55,0:10' -> ((95, 75), (85, 55), (0, 10)), highest score first."""
    rows = []
    for part in text.split(","):
        score, cap = part.split(":")
        rows.append((float(score), float(cap)))
    return tuple(sorted(rows, reverse=True))


def position_cap(score: float, caps: tuple[tuple[float, float], ...], ceiling: float) -> float:
    for min_score, cap in caps:
        if score >= min_score:
            return min(cap, ceiling)
    return min(caps[-1][1], ceiling) if caps else ceiling


@dataclass
class Thresholds:
    max_trigger_age: int = 10
    min_edge: float = 0.05
    min_history: int = 10
    min_score: float = 55.0
    late_entry: bool = True
    max_chase_pct: float = 1.0
    # Stocks in play (Zarattini, Barbon & Aziz): unusual volume, enough range, enough liquidity.
    min_rvol: float = 1.0
    min_atr: float = 0.5
    min_adv: float = 1_000_000
    require_above_vwap: bool = True


@dataclass
class Candidate:
    symbol: str
    scan: dict[str, Any]
    status: str = "pending"
    tier: str = REJECT
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    entry: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    tm: dict[str, Any] | None = None
    sizing: dict[str, Any] | None = None
    order: dict[str, Any] | None = None
    risk: dict[str, Any] | None = None
    news: list[dict[str, Any]] = field(default_factory=list)
    move: dict[str, Any] | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    position_cap_pct: float | None = None
    late: dict[str, Any] | None = None
    # --pending: not broken out yet; planned as a buy-stop one cent over the range high
    # (carried in `late`, so the plan, replays and size start from that price)
    pending: bool = False
    targets_tried: list[dict[str, Any]] = field(default_factory=list)
    features: dict[str, Any] | None = None
    regime: dict[str, Any] | None = None

    def stop_here(self, why: str) -> None:
        self.status = "stopped"
        self.reasons.append(why)


def news_flags(items: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """(vetoes, cautions) from the news items that touch one ticker."""
    vetoes: list[str] = []
    cautions: list[str] = []
    for it in items:
        tags = set(it.get("tags") or [])
        items_8k = set(it.get("items") or [])
        title = (it.get("title") or "").strip()[:110]
        label = f"{it.get('source', 'news')}: {title}"
        bad = (tags & VETO_TAGS) | (items_8k & VETO_8K_ITEMS)
        if bad:
            vetoes.append(f"{label} [{', '.join(sorted(bad))}]")
        elif tags & CAUTION_TAGS or it.get("important"):
            kinds = ", ".join(sorted(tags & CAUTION_TAGS)) or "8-K"
            cautions.append(f"{label} [{kinds}]")
    return vetoes, cautions


def two_step(
    chart: dict[str, Any],
    plan: dict[str, Any],
    r2: float,
    replays: int,
    seed: int,
    until: str = "15:55",
    or_minutes: int = 5,
    min_stop_atr: float = 0.25,
    window: int = 2,
) -> dict[str, Any] | None:
    """The desk's real exit, scored the way the Time Machine scores a single target.

    The desk sells half at +1R, moves the rest's stop to the entry and sells it at +r2 R,
    all out by the flat-by time. trader-timemachine knows one target only, so this replays
    the same kind of pool (each past full session's bars as ratios to the previous close,
    the slot drawn within +/- `window` bars) with that exit, and runs the same exit on the
    real past sessions (trade-backtest's session_trade). Ties inside a bar go against it.
    """
    days = features.sessions(chart)
    date, k = str(plan.get("date") or ""), plan.get("entry_bar")
    entry, stop = float(plan["entry"]), float(plan["stop"])
    risk = entry - stop
    if k is None or risk <= 0:
        return None
    full = [days[d] for d in sorted(days) if d < date and len(days[d]) == 78]
    if len(full) < 20:
        return None
    pool: list[list[tuple[float, float, float]]] = [[] for _ in range(78)]
    for bars in full:
        for i in range(1, 78):
            pc = bars[i - 1][4]
            pool[i].append((bars[i][2] / pc, bars[i][3] / pc, bars[i][4] / pc))
    hh, mm = (int(x) for x in until.split(":"))
    last = min(77, (hh * 60 + mm - (9 * 60 + 30)) // 5 - 1)
    t1, t2 = entry + risk, entry + r2 * risk
    rng = random.Random(seed)  # noqa: S311 - a replay seed, not security
    n_t1 = n_stop = n_t2 = 0
    rs, wins = [], []
    for _ in range(replays):
        p, cur, h1, h2 = entry, stop, None, None
        for i in range(int(k) + 1, last + 1):
            j = i if window == 0 else min(77, max(1, i + rng.randint(-window, window)))
            hx, lx, cx = rng.choice(pool[j])
            hi, lo = p * hx, p * lx
            if h2 is None:
                if lo <= cur:  # the stop first: the worst reading of the bar
                    out = (cur - entry) / risk
                    h1 = out if h1 is None else h1
                    h2 = out
                elif h1 is None and hi >= t1:
                    h1, cur = 1.0, entry
                if h2 is None and h1 is not None and hi >= t2:
                    h2 = r2
            p *= cx
        close_r = (p - entry) / risk
        h1 = close_r if h1 is None else h1
        h2 = close_r if h2 is None else h2
        rs.append(0.5 * h1 + 0.5 * h2)
        if h1 == 1.0:
            n_t1 += 1
            wins.append(rs[-1])
            n_t2 += h2 == r2
        elif h1 <= -0.999:
            n_stop += 1
    mc = {
        "p_target": round(n_t1 / replays, 4),
        "p_stop": round(n_stop / replays, 4),
        "p_neither": round(1 - (n_t1 + n_stop) / replays, 4),
        "p_t2": round(n_t2 / replays, 4),
        "avg_R": round(sum(rs) / len(rs), 3),
        "win_r": round(sum(wins) / len(wins), 3) if wins else 1.0,
    }
    hist_rs = []
    for d in sorted(days):
        atr = backtest.atr_before(days, d) if d < date else None
        t = backtest.session_trade(days[d], atr, r2, or_minutes, min_stop_atr) if atr else None
        if t:
            hist_rs.append(t["r"])
    history = {
        "triggered": len(hist_rs),
        "target": sum(1 for x in hist_rs if x > 0),
        "stop": sum(1 for x in hist_rs if x <= 0),
        "avg_R": round(sum(hist_rs) / len(hist_rs), 3) if hist_rs else 0.0,
    }
    verdict = (
        f"T1/T2 plan: +1R first in {mc['p_target']:.1%} of replays, the stop first in "
        f"{mc['p_stop']:.1%}, T2 ({r2:g}R) in {mc['p_t2']:.1%}; avg R {mc['avg_R']:+.3f}"
    )
    return {"mc": mc, "history": history, "verdict": verdict, "t2_r": r2}


def score_candidate(c: Candidate, th: Thresholds) -> None:
    """Fill metrics, score (0-100), tier and the reasons, from the tool outputs already on c.

    The score only reads numbers the tools computed:
      35 pts  Time Machine edge: share of decided replays that hit the target first, minus
              the break-even share 1/(1+R). Full marks at +20 points of edge.
      20 pts  Time Machine average R. Full marks at +0.30R.
      20 pts  History: the same rule on the past ~60 sessions, target share minus break-even,
              scaled by how many times it fired (full weight at 3x the minimum sample).
      15 pts  Relative volume of the opening range vs the same minutes on the past 14
              sessions: nothing at 1x, full marks at 3x (the stocks-in-play factor).
       5 pts  Above VWAP (2.5) and the market (SPY) above its own VWAP (2.5).
       5 pts  Freshness: a trigger on the bar that just closed scores full marks.
    """
    if c.plan is None or c.tm is None or c.entry is None:
        raise ValueError("score_candidate needs the entry, the plan and the Time Machine run")
    # The T1/T2 plan when two_step() ran: "target" is +1R first, and break-even uses the
    # average R of the replays that got there, since a win pays more than 1R on average.
    ts = c.tm.get("two_step") or {}
    mc = ts.get("mc") or c.tm.get("mc") or {}
    hist = ts.get("history") or c.tm.get("history") or {}
    r = float(c.plan.get("target_r") or 1.0)
    breakeven = 1.0 / (1.0 + float(mc.get("win_r") or r))
    p_t, p_s = float(mc.get("p_target", 0)), float(mc.get("p_stop", 0))
    decided = p_t + p_s
    win_share = p_t / decided if decided else 0.0
    edge = win_share - breakeven
    mc_avg_r = float(mc.get("avg_R", 0.0))
    n_hist = int(hist.get("triggered", 0))
    h_t, h_s = int(hist.get("target", 0)), int(hist.get("stop", 0))
    h_share = h_t / (h_t + h_s) if (h_t + h_s) else 0.0
    h_edge = h_share - breakeven
    h_avg_r = float(hist.get("avg_R", 0.0))
    trig = c.entry.get("trigger") or {}
    vol_x = float(trig.get("volume_x") or 0.0)
    age = int(c.entry.get("minutes_since") or 0)

    feats = c.features or {}
    rvol = float(feats.get("rvol") or 0.0)
    above_vwap = bool(feats.get("above_vwap"))
    mkt_up = bool((c.regime or {}).get("above_vwap"))
    sample_w = clamp(n_hist / (3.0 * th.min_history)) if th.min_history else 1.0
    score = (
        35 * clamp(edge / 0.20)
        + 20 * clamp(mc_avg_r / 0.30)
        + 20 * clamp(h_edge / 0.20) * sample_w
        + 15 * clamp((rvol - 1.0) / 2.0)
        + 2.5 * above_vwap
        + 2.5 * mkt_up
        + 5 * clamp(1 - (th.max_trigger_age if c.late else age) / max(th.max_trigger_age, 1))
    )
    c.metrics = {
        "target_r": r,
        "breakeven": round(breakeven, 4),
        "p_target": p_t,
        "p_stop": p_s,
        "p_neither": float(mc.get("p_neither", 0)),
        "win_share": round(win_share, 4),
        "edge": round(edge, 4),
        "mc_avg_r": mc_avg_r,
        "hist_triggered": n_hist,
        "hist_target": h_t,
        "hist_stop": h_s,
        "hist_share": round(h_share, 4),
        "hist_avg_r": h_avg_r,
        "volume_x": vol_x,
        "trigger_age_min": age,
        "rvol": rvol,
        "atr": float(feats.get("atr") or 0.0),
        "gap_pct": feats.get("gap_pct"),
        "or_atr": feats.get("or_atr"),
        "above_vwap": above_vwap,
        "market_above_vwap": mkt_up,
    }
    c.score = round(score, 1)

    fails: list[str] = []
    if edge < th.min_edge:
        fails.append(
            f"Time Machine edge {edge * 100:+.1f} pts vs break-even {breakeven:.0%} "
            f"(needs {th.min_edge * 100:+.0f}): {c.tm.get('verdict', '')}"
        )
    if mc_avg_r <= 0:
        fails.append(f"Time Machine average R {mc_avg_r:+.3f} is not positive")
    if n_hist < th.min_history:
        fails.append(f"history: the rule fired {n_hist} times, fewer than {th.min_history}")
    elif h_avg_r <= 0 or h_edge < 0:
        fails.append(
            f"history: {h_t} of {h_t + h_s} decided past sessions hit the target "
            f"({h_share:.0%}, break-even {breakeven:.0%}), avg R {h_avg_r:+.3f}"
        )
    if c.score < th.min_score:
        fails.append(f"score {c.score:.1f} under the {th.min_score:.0f} minimum")
    c.reasons.extend(fails)
    if not fails:
        c.tier = HIGH
    elif edge > 0 and mc_avg_r > 0:
        c.tier = WATCH
    else:
        c.tier = REJECT


# --------------------------------------------------------------------------- pipeline


@dataclass
class Options:
    trader_dir: Path
    run_dir: Path
    symbols: str | None
    watchlist: Path | None
    max_candidates: int
    top: int
    stop: str
    target: str
    replays: int
    seed: int
    now: str | None
    offline_dir: Path | None
    min_price: float | None
    news_hours: float
    th: Thresholds
    caps: tuple[tuple[float, float], ...] = DEFAULT_CAPS
    max_price: float | None = None
    entry_pool: int = 40
    or_minutes: int = 15
    daily_pick: bool = True
    regime: dict[str, Any] | None = None
    pending: bool = False
    max_pending_pct: float = 3.0


def offline_path(opts: Options, *parts: str) -> Path | None:
    if not opts.offline_dir:
        return None
    p = opts.offline_dir.joinpath(*parts)
    return p if p.exists() else None


def common(opts: Options) -> list[str]:
    return ["--now", opts.now] if opts.now else []


def step_scan(opts: Options) -> tuple[dict[str, Any] | None, str]:
    ws = seed_workspace(opts.run_dir / "scan", opts.trader_dir)
    args = ["orb", "--json", "--or-minutes", str(opts.or_minutes), *common(opts)]
    if opts.symbols:
        args += ["--symbols", opts.symbols]
    elif opts.watchlist:
        args += ["--watchlist", str(opts.watchlist.resolve())]
    if opts.min_price is not None:
        args += ["--min-price", str(opts.min_price)]
    off = offline_path(opts, "scan.json.gz") or offline_path(opts, "scan.json")
    if off:
        args += ["--offline", str(off)]
    elif not (ws / "trader" / "universe.txt").exists() and not (opts.symbols or opts.watchlist):
        res = run_tool("scan", ["universe"], ws)
        if res.code != 0:
            return None, f"could not build the universe: {res.text}"
        shutil.copyfile(ws / "trader" / "universe.txt", opts.trader_dir / "universe.txt")
    res = run_tool("scan", args, ws)
    if not isinstance(res.data, dict):
        return None, res.text or "the scan returned nothing"
    return res.data, ""


def pick_candidates(
    scan: dict[str, Any], n: int, max_price: float | None = None
) -> list[Candidate]:
    """Latest triggers first (freshest), then the closest to the range (least chased)."""
    hits = [h for h in scan.get("hits", []) if "orb" in (h.get("setups") or ["orb"])]
    if max_price is not None:
        hits = [h for h in hits if float(h.get("price") or 0) <= max_price]
    hits.sort(key=lambda h: (-int(h.get("bar_index", 0)), float(h.get("pct_over", 0))))
    return [Candidate(symbol=h["symbol"], scan=h) for h in hits[:n]]


def watch_symbols(opts: Options) -> list[str]:
    """The tickers in --symbols or --watchlist (a watchlist.json's hits, or one per line)."""
    if opts.symbols:
        return [s.strip().upper() for s in opts.symbols.split(",") if s.strip()]
    if not opts.watchlist or not opts.watchlist.exists():
        return []
    text = opts.watchlist.read_text(encoding="utf-8-sig")
    if text.lstrip().startswith(("{", "[")):
        doc = json.loads(text)
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        return [str(x.get("symbol") if isinstance(x, dict) else x).upper() for x in items if x]
    return [
        ln.split("|")[0].strip().upper() for ln in text.splitlines() if ln.strip()[:1].isalpha()
    ]


def _ny_to_utc_iso(local: dt.datetime) -> str:
    try:
        from zoneinfo import ZoneInfo

        aware = local.replace(tzinfo=ZoneInfo("America/New_York"))
        return aware.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:  # noqa: BLE001 - no tz database: assume EDT, fine for a news window
        return (local + dt.timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")


def step_news(opts: Options, cands: list[Candidate]) -> str:
    if not cands:
        return ""
    ws = seed_workspace(opts.run_dir / "news", opts.trader_dir)
    args = ["--tickers", *[c.symbol for c in cands], "--json", "--dry-run", "--all"]
    args += ["--hours", str(opts.news_hours)]
    if opts.now:
        args += ["--now", _ny_to_utc_iso(dt.datetime.strptime(opts.now, "%Y-%m-%d %H:%M"))]
    off = offline_path(opts, "news")
    if off:
        args += ["--offline", str(off)]
    res = run_tool("news", args, ws)
    if not isinstance(res.data, dict):
        return f"news unavailable: {res.text or 'no output'}"
    by = {c.symbol: c for c in cands}
    for it in res.data.get("items", []):
        for t in it.get("touches") or [it.get("ticker")]:
            if t in by:
                by[t].news.append(it)
    for m in res.data.get("moves", []):
        if m.get("ticker") in by:
            by[m["ticker"]].move = m
    notes = list(res.data.get("notes") or [])
    if len(notes) > 3:
        notes = [*notes[:3], f"(+{len(notes) - 3} more news notes)"]
    return "; ".join(notes)


def _bar_close(bar_start: str) -> tuple[int, str]:
    """'10:10' (a 5-minute bar's start) -> (bar index 8, '10:15 AM' its close)."""
    h, m = (int(x) for x in bar_start.split(":"))
    start = h * 60 + m
    close = start + 5
    hh, mm = divmod(close, 60)
    label = f"{(hh - 1) % 12 + 1}:{mm:02d} {'AM' if hh < 12 else 'PM'}"
    return (start - (9 * 60 + 30)) // 5, label


def step_entry(opts: Options, c: Candidate) -> bool:
    """Is there a trade to plan right now? A fresh trigger, or (late entry) a breakout that
    is still holding above the range high without having run more than max_chase_pct."""
    ws = seed_workspace(opts.run_dir / "candidates" / c.symbol, opts.trader_dir)
    args = ["check", c.symbol, "--json", "--write", "--or-minutes", str(opts.or_minutes)]
    args += common(opts)
    off = offline_path(opts, "entry")
    if off:
        args += ["--offline", str(off)]
    res = run_tool("entry", args, ws)
    if not isinstance(res.data, dict):
        c.stop_here(f"entry: {res.text or 'no data'}")
        return False
    c.entry = res.data
    if res.data.get("status") == "waiting" and opts.pending:
        return pending_entry(opts, c, res.data)
    if res.data.get("status") != "triggered":
        c.stop_here(f"entry {res.data.get('status')}: {res.data.get('reason', '')}")
        return False
    age = int(res.data.get("minutes_since") or 0)
    if age <= opts.th.max_trigger_age:
        return True
    when = (res.data.get("trigger") or {}).get("time")
    last = res.data.get("last") or {}
    chase = last.get("vs_level_pct")
    if not opts.th.late_entry or chase is None or not last.get("bar_start"):
        c.stop_here(
            f"stale trigger: fired {age} min ago at {when} (limit {opts.th.max_trigger_age})"
        )
        return False
    if chase <= 0:
        c.stop_here(
            f"failed breakout: fired at {when}, now back under the range high ({chase:+.2f}%)"
        )
        return False
    if chase > opts.th.max_chase_pct:
        c.stop_here(
            f"extended: fired at {when}, now {chase:+.2f}% over the range high "
            f"(late entries allowed up to {opts.th.max_chase_pct:g}%)"
        )
        return False
    bar, close_label = _bar_close(str(last["bar_start"]))
    c.late = {
        "price": float(last["close"]),
        "bar": bar,
        "time": close_label,
        "first_trigger": when,
        "age_min": age,
        "over_pct": chase,
    }
    return True


def pending_entry(opts: Options, c: Candidate, data: dict[str, Any]) -> bool:
    """Not broken out yet: plan a buy-stop one cent over the range high, if the last close
    is within max_pending_pct of it. The replays then start from that price, as if the
    breakout had just happened.

    ponytail: assumes the odds after a later trigger match the odds now; the Time Machine
    still stops every replay at the flat-by time, and unfilled entries are cancelled late
    morning so a 2 PM trigger never uses a 9:45 plan."""
    level, last = data.get("level"), data.get("last") or {}
    gap = last.get("vs_level_pct")
    if level is None or gap is None or not last.get("bar_start"):
        c.stop_here(f"pending: no range high or closed bar yet ({data.get('reason', '')})")
        return False
    if gap < -opts.max_pending_pct:
        c.stop_here(
            f"pending: last close {gap:+.2f}% from the range high "
            f"(buy-stops only within {opts.max_pending_pct:g}%)"
        )
        return False
    bar, close_label = _bar_close(str(last["bar_start"]))
    c.late = {
        "price": round(float(level) + 0.01, 2),
        "bar": bar,
        "time": close_label,
        "first_trigger": None,
        "age_min": 0,
        "over_pct": gap,
    }
    c.pending = True
    return True


def entry_rank(c: Candidate) -> tuple[float, int, float]:
    """Highest relative volume first (the stocks-in-play ranking): fresh, late, pending."""
    rvol = float((c.features or {}).get("rvol") or 0.0)
    if c.late is None:
        return (-rvol, 0, float((c.entry or {}).get("minutes_since") or 0))
    return (-rvol, 2 if c.pending else 1, abs(float(c.late["over_pct"])))


def entry_point(c: Candidate) -> tuple[float, int]:
    """(price, bar index) the trade would be entered at."""
    if c.late:
        return float(c.late["price"]), int(c.late["bar"])
    trig = (c.entry or {}).get("trigger") or {}
    return float(trig.get("price") or 0), int(trig.get("bar_index") or 0)


def chart_file(opts: Options, c: Candidate) -> Path | None:
    off = offline_path(opts, "timemachine", f"{c.symbol}.json.gz") or offline_path(
        opts, "timemachine", f"{c.symbol}.json"
    )
    if off or opts.offline_dir:
        return off
    return opts.run_dir / "candidates" / c.symbol / "trader" / "bars-60d.json"


def step_features(opts: Options, c: Candidate) -> bool:
    """Stocks-in-play filter from 60 days of 5-minute bars (saved for the Time Machine)."""
    path = chart_file(opts, c)
    if path is None:
        c.stop_here(f"features: no saved 60-day bars for {c.symbol} in the offline folder")
        return False
    try:
        if not path.exists():
            features.save_chart(features.fetch_chart(c.symbol, "60d", "5m"), path)
        chart = features.load_chart(path)
    except (OSError, RuntimeError, ValueError) as e:
        c.stop_here(f"features: {e}")
        return False
    date = str((c.entry or {}).get("date") or "")
    _, bar = entry_point(c)
    f = features.compute(chart, date, bar, opts.or_minutes)
    if f.get("error"):
        c.stop_here(f"features: {f['error']}")
        return False
    c.features = f
    c.regime = opts.regime
    th = opts.th
    if f["rvol"] < th.min_rvol:
        c.stop_here(
            f"not in play: opening-range volume {f['rvol']:.2f}x normal (needs {th.min_rvol:g}x)"
        )
    elif f["atr"] < th.min_atr:
        c.stop_here(f"too quiet: daily ATR ${f['atr']:.2f} (needs ${th.min_atr:.2f})")
    elif f["adv"] < th.min_adv:
        c.stop_here(f"too thin: {f['adv']:,} shares a day on average (needs {th.min_adv:,.0f})")
    elif th.require_above_vwap and not f["above_vwap"]:
        c.stop_here(
            f"below VWAP: price {f['price']:.2f} under VWAP {f['vwap']:.2f} at the entry bar"
        )
    else:
        if f.get("gap_pct") is not None and abs(f["gap_pct"]) >= 8:
            c.cautions.append(f"gapped {f['gap_pct']:+.1f}% at the open")
        if f.get("or_atr") is not None and f["or_atr"] > 1.0:
            c.cautions.append(
                f"opening range is {f['or_atr']:.1f}x the daily ATR: "
                "much of the day's move may be spent"
            )
        if opts.regime and not opts.regime.get("above_vwap"):
            c.cautions.append(
                "market (SPY) is below its VWAP: breakouts fail more often against the tape"
            )
        return True
    return False


def stop_spec(spec: str, c: Candidate) -> str:
    """'atr-d:0.1' -> 'price:X' (X = entry - 0.1 x daily ATR, rounded down);
    'orl-atr:0.25' -> the opening-range low, but never closer to the entry than 0.25 x the
    daily ATR (so a tight range cannot give a stop the noise takes out); others unchanged."""
    if spec.startswith("orl-atr:"):
        k = float(spec.split(":", 1)[1])
        entry, _ = entry_point(c)
        atr = float((c.features or {}).get("atr") or 0)
        or_low = ((c.entry or {}).get("opening_range") or {}).get("low")
        if or_low is None:
            return "or-low"
        stop = min(float(or_low), entry - k * atr) if atr else float(or_low)
        return f"price:{math.floor(stop * 100) / 100:.2f}"
    if not spec.startswith("atr-d:"):
        return spec
    k = float(spec.split(":", 1)[1])
    entry, _ = entry_point(c)
    atr = float((c.features or {}).get("atr") or 0)
    if not atr:
        return "or-low"
    return f"price:{math.floor((entry - k * atr) * 100) / 100:.2f}"


def step_candidate(opts: Options, c: Candidate) -> None:
    ws = seed_workspace(opts.run_dir / "candidates" / c.symbol, opts.trader_dir)
    now = common(opts)

    vetoes, cautions = news_flags(c.news)
    c.cautions.extend(cautions)
    if c.move and abs(float(c.move.get("pct", 0))) >= 8:
        c.cautions.append(f"already {float(c.move['pct']):+.1f}% from yesterday's close")
    if vetoes:
        c.stop_here("news veto: " + " | ".join(vetoes[:3]))
        return

    # Exit plan + Time Machine, once per target: hold to the flat-by time (a hard rule),
    # and keep the target with the best expected R over the rest of the session.
    tm_off = chart_file(opts, c)
    if tm_off is None or not tm_off.exists():
        c.stop_here(f"time machine: no 60-day bars for {c.symbol}")
        return
    chart = features.load_chart(tm_off)
    tried: list[dict[str, Any]] = []
    errors: list[str] = []
    combos = [
        (st.strip(), tg.strip())
        for st in opts.stop.split(",")
        if st.strip()
        for tg in opts.target.split(",")
        if tg.strip()
    ]
    for stop_name, target in combos:
        tag = f"{stop_name}-{target}".replace(".", "_").replace(":", "_")
        plan_path = f"trader/plan-{tag}.json"
        args = ["plan", "--stop", stop_spec(stop_name, c), "--target", target, "--json", *now]
        args += ["--write", plan_path]
        if c.late:
            args += ["--entry", f"{c.late['price']:.2f}", "--time", c.late["time"]]
            args += ["--entry-bar", str(c.late["bar"])]
        res = run_tool("exit", args, ws)
        if not isinstance(res.data, dict) or res.code != 0:
            detail = res.text or (res.data or {}).get("error", "refused")
            errors.append(f"{stop_name}/{target}: {detail}")
            continue
        plan = res.data
        args = ["run", "--json", "--plan", plan_path, "--replays", str(opts.replays)]
        args += ["--seed", str(opts.seed), "--until", str(plan.get("flat_by") or "15:55"), *now]
        if tm_off:
            args += ["--offline", str(tm_off)]
        res = run_tool("tm", args, ws)
        if not isinstance(res.data, dict) or "mc" not in res.data:
            errors.append(f"{stop_name}/{target}: time machine: {res.text or 'no result'}")
            continue
        tm_doc = res.data
        # The desk exits in two steps (T1 at +1R, the rest at this target): score that.
        r2 = float(plan.get("target_r") or 0)
        msa = float(stop_name.split(":")[1]) if stop_name.startswith("orl-atr:") else 0.0
        ts = (
            two_step(
                chart,
                plan,
                r2,
                opts.replays,
                opts.seed,
                str(plan.get("flat_by") or "15:55"),
                opts.or_minutes,
                msa,
            )
            if r2 > 1
            else None
        )
        if ts:
            tm_doc = {**tm_doc, "two_step": ts, "verdict": ts["verdict"]}
        avg_r = float((ts or tm_doc)["mc"].get("avg_R", 0.0))
        res.data = tm_doc
        tried.append(
            {
                "target": f"{stop_name} / {target}",
                "plan": plan,
                "tm": res.data,
                "plan_path": plan_path,
                "avg_R": avg_r,
                "r": float(plan.get("target_r") or 0),
            }
        )
    if not tried:
        c.stop_here("exit/time machine: " + "; ".join(errors or ["no plan"]))
        return
    # Best expected R; on a tie (to 0.001R) the closer target, which is likelier to fill.
    best = max(tried, key=lambda t: (round(t["avg_R"], 3), -t["r"]))
    c.plan, c.tm = best["plan"], best["tm"]
    c.targets_tried = [
        {
            "target": t["target"],
            "avg_R": round(t["avg_R"], 3),
            "p_target": (t["tm"].get("two_step") or t["tm"])["mc"].get("p_target"),
            "p_stop": (t["tm"].get("two_step") or t["tm"])["mc"].get("p_stop"),
            "verdict": t["tm"].get("verdict"),
        }
        for t in tried
    ]
    shutil.copyfile(ws / best["plan_path"], ws / "trader" / "plan.json")

    # Score first: the conviction sets how much of the budget this one position may use.
    score_candidate(c, opts.th)
    cfg_path = ws / "trader" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    ceiling = float(cfg.get("max_position_pct", 75))
    c.position_cap_pct = position_cap(c.score, opts.caps, ceiling)
    cfg["max_position_pct"] = c.position_cap_pct
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")

    # Budget
    res = run_tool("size", ["--write", "--json", *now], ws)
    data = res.data if isinstance(res.data, dict) else {}
    sizing = data.get("sizing") or {}
    c.sizing = sizing or None
    if sizing.get("refusal") or not data.get("orders"):
        c.stop_here(f"budget refused: {sizing.get('refusal') or res.text or 'no order'}")
        c.tier = REJECT
        return
    c.order = data["orders"][0]
    if c.pending:  # a buy-stop at the planned entry: it only fills on the breakout
        c.order["type"] = "stop"
        c.order["stop_price"] = f"{float(c.late['price']):.2f}"
    c.status = "scored"


def step_risk(opts: Options, shortlist: list[Candidate]) -> str:
    """One batch check, best first: day limits apply across the whole list."""
    if not shortlist:
        return ""
    ws = seed_workspace(opts.run_dir / "risk", opts.trader_dir)
    orders = [c.order for c in shortlist]
    (ws / "trader" / "orders.json").write_text(json.dumps(orders, indent=2) + "\n")
    res = run_tool("risk", ["--json", "--no-write", *common(opts)], ws)
    if not isinstance(res.data, dict):
        for c in shortlist:
            c.tier = WATCH
            c.reasons.append(f"risk check did not run: {res.text}")
        return res.text
    results = res.data.get("results", [])
    for c, r in zip(shortlist, results, strict=False):
        c.risk = r
        if r.get("verdict") != "PASS":
            c.tier = WATCH
            c.reasons.append("risk BLOCK: " + "; ".join(r.get("reasons") or ["blocked"]))
        for w in r.get("warnings") or []:
            c.cautions.append("risk: " + w)
    return "; ".join(res.data.get("warnings") or [])


def run(opts: Options) -> dict[str, Any]:
    opts.run_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    scan, err = step_scan(opts)
    if scan is None:
        return {"ok": False, "error": "scan: " + err, "candidates": [], "notes": notes}
    opts.regime = step_regime(opts)
    pool = pick_candidates(scan, opts.entry_pool, opts.max_price)
    if opts.pending:  # the names that have not broken out yet get an entry check too
        have = {c.symbol for c in pool}
        extra = [s for s in watch_symbols(opts) if s not in have]
        pool += [Candidate(symbol=s, scan={}) for s in extra[: max(0, opts.entry_pool - len(pool))]]
    for c in pool:
        if step_entry(opts, c):
            step_features(opts, c)
    live = sorted((c for c in pool if c.status != "stopped"), key=entry_rank)
    cands = live[: opts.max_candidates]
    for c in live[opts.max_candidates :]:
        c.stop_here(f"not run in full: only the best {opts.max_candidates} entries are")
    rest = [c for c in pool if c not in cands]
    note = step_news(opts, cands)
    if note:
        notes.append(note)
    for c in cands:
        step_candidate(opts, c)
    highs = sorted((c for c in cands if c.tier == HIGH), key=lambda c: -c.score)
    shortlist = highs[: opts.top]
    for c in highs[opts.top :]:
        c.tier = WATCH
        c.reasons.append(f"outside the top {opts.top}")
    note = step_risk(opts, shortlist)
    if note:
        notes.append(note)
    if opts.daily_pick and not any(c.tier == HIGH for c in cands):
        step_pick(opts, cands)
    cands += rest
    cands.sort(key=lambda c: (TIER_ORDER[c.tier], -c.score))
    scan_keys = ("mode", "universe", "scanned", "failed", "unconfirmed")
    return {
        "ok": True,
        "as_of": scan.get("as_of"),
        "scan": {k: scan.get(k) for k in scan_keys} | {"hits": len(scan.get("hits", []))},
        "settings": {
            "stop": opts.stop,
            "target": opts.target,
            "replays": opts.replays,
            "seed": opts.seed,
            "max_candidates": opts.max_candidates,
            "top": opts.top,
            "max_price": opts.max_price,
            "late_entry": opts.th.late_entry,
            "max_chase_pct": opts.th.max_chase_pct,
            "entry_pool": opts.entry_pool,
            "or_minutes": opts.or_minutes,
            "max_pending_pct": opts.max_pending_pct,
            "daily_pick": opts.daily_pick,
            "thresholds": vars(opts.th),
            "position_caps": [list(r) for r in opts.caps],
        },
        "candidates": [candidate_json(c) for c in cands],
        "regime": opts.regime,
        "notes": notes,
    }


def step_regime(opts: Options) -> dict[str, Any] | None:
    """SPY against its own VWAP today: the tape the breakouts trade against."""
    try:
        off = offline_path(opts, "SPY.json.gz") or offline_path(opts, "SPY.json")
        if off:
            chart = features.load_chart(off)
        elif opts.offline_dir:
            return None
        else:
            chart = features.fetch_chart("SPY", "5d", "5m")
    except (OSError, RuntimeError, ValueError):
        return None
    date = (opts.now or dt.datetime.now(features.NY).strftime("%Y-%m-%d"))[:10]
    reg = features.market_regime(chart, date)
    return None if reg.get("error") else reg


def step_pick(opts: Options, cands: list[Candidate]) -> None:
    """No HIGH trade: the best positive-expectancy WATCH becomes the paper PICK of the run,
    if it was sized and passes the risk check. Lower conviction, for the paper journal."""
    pool = [
        c
        for c in cands
        if c.tier == WATCH
        and c.status == "scored"
        and c.order
        and c.metrics.get("mc_avg_r", 0) > 0
        and c.metrics.get("edge", 0) > 0
    ]
    if not pool:
        return
    best = max(pool, key=lambda c: c.score)
    best.tier = PICK
    step_risk(opts, [best])
    if best.tier == PICK:
        best.cautions.append(
            "PICK: lower conviction than HIGH (missed: " + "; ".join(best.reasons) + ")"
        )


PLAN_KEYS = (
    "entry",
    "stop",
    "stop_label",
    "target",
    "target_r",
    "risk_per_share",
    "flat_by",
    "date",
    "entry_bar",
    "entry_time",
)
SIZE_KEYS = ("qty", "cost", "max_loss", "bound_by")


def candidate_json(c: Candidate) -> dict[str, Any]:
    trig = (c.entry or {}).get("trigger") or {}
    return {
        "symbol": c.symbol,
        "tier": c.tier,
        "score": c.score,
        "status": c.status,
        "reasons": c.reasons,
        "cautions": c.cautions,
        "trigger": {k: trig.get(k) for k in ("time", "price", "pct_over")} if trig else None,
        "plan": {k: c.plan.get(k) for k in PLAN_KEYS} if c.plan else None,
        "verdict": (c.tm or {}).get("verdict"),
        "metrics": c.metrics,
        "size": {k: c.sizing.get(k) for k in SIZE_KEYS} if c.sizing else None,
        "order": {k: v for k, v in (c.order or {}).items() if k not in ("why", "plan")} or None,
        "risk": {k: c.risk.get(k) for k in ("verdict", "reasons", "fingerprint")}
        if c.risk
        else None,
        "news": [
            {k: n.get(k) for k in ("title", "link", "time", "tags", "source")} for n in c.news[:5]
        ],
        "move_pct": (c.move or {}).get("pct"),
        "late_entry": None if c.pending else c.late,
        "pending": c.late if c.pending else None,
        "targets_tried": c.targets_tried,
        "features": c.features,
        "position_cap_pct": c.position_cap_pct,
    }


# --------------------------------------------------------------------------- report


def pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.1%}"


def render_high(i: int, c: dict[str, Any]) -> list[str]:
    m, p, z, t = c["metrics"], c["plan"], c["size"], c["trigger"]
    lines = [
        f"### {i}. {c['symbol']}  (score {c['score']:.1f})",
        "",
        f"- Trigger {t['time']} at ${t['price']:.2f}, {t['pct_over'] or 0:+.2f}% over the range "
        f"high ({m['trigger_age_min']} min ago, volume {m['volume_x']:.1f}x)"
        + (
            f"; LATE ENTRY at the {c['late_entry']['time']} close, still "
            f"{c['late_entry']['over_pct']:+.2f}% over the range high"
            if c.get("late_entry")
            else ""
        ),
        "- In play: "
        + (
            f"opening-range volume {m['rvol']:.2f}x normal, daily ATR ${m['atr']:.2f}, "
            f"gap {m['gap_pct'] if m['gap_pct'] is not None else 0:+.2f}%, opening range "
            f"{m['or_atr'] or 0:.2f}x ATR, {'above' if m['above_vwap'] else 'below'} VWAP; "
            f"market {'above' if m['market_above_vwap'] else 'below/unknown vs'} its VWAP"
            if "rvol" in m
            else "n/a"
        ),
        f"- Plan: entry ${p['entry']:.2f}, stop ${p['stop']:.2f} ({p['stop_label']}), "
        f"target ${p['target']:.2f} ({p['target_r']:g}R), flat by {p['flat_by']}",
        f"- Time Machine: target first {pct(m['p_target'])}, stop first {pct(m['p_stop'])}, "
        f"neither {pct(m['p_neither'])}; target share of decided {pct(m['win_share'])} vs "
        f"break-even {pct(m['breakeven'])}; avg R {m['mc_avg_r']:+.3f}. {c['verdict']}",
        "- Targets tried (hold to the close): "
        + ", ".join(f"{t['target']} avg R {t['avg_R']:+.3f}" for t in c.get("targets_tried") or [])
        + f"; kept {p['target_r']:g}R",
        f"- History: fired {m['hist_triggered']} times in the pool, {m['hist_target']} target / "
        f"{m['hist_stop']} stop, avg R {m['hist_avg_r']:+.3f}",
        f"- Size: {z['qty']} shares (${z['cost']}), max loss ${z['max_loss']} at the stop, "
        f"bound by {z['bound_by']} (conviction cap {c['position_cap_pct']:g}% of the budget)",
        f"- Risk: {(c['risk'] or {}).get('verdict') or 'not checked'}",
    ]
    if c["cautions"]:
        lines.append("- Cautions: " + "; ".join(c["cautions"]))
    lines += [f"- News: [{n['title']}]({n['link']})" for n in c["news"][:3]]
    return [*lines, ""]


def render(summary: dict[str, Any]) -> str:
    if not summary.get("ok"):
        return f"Trade Scout could not run: {summary.get('error')}\n"
    s, sc, cands = summary["settings"], summary["scan"], summary["candidates"]
    out = [
        f"# Trade Scout: {summary.get('as_of')}",
        "",
        f"Scanned {sc.get('scanned')} of {sc.get('universe')} charts ({sc.get('failed')} "
        f"failed); {sc.get('hits')} matched the opening-range breakout rule; "
        f"{sum(1 for c in cands if c['plan'])} reached a plan after news and entry "
        f"(a fresh trigger, or a late entry within {s.get('max_chase_pct', 1):g}% of the "
        f"range high), exit ({s['stop']} stop, {s['target']} target), "
        f"Time Machine ({s['replays']} replays, seed {s['seed']}), budget and one batch "
        "risk check.",
        "",
    ]
    high = [c for c in cands if c["tier"] == HIGH]
    picks = [c for c in cands if c["tier"] == PICK]
    watch = [c for c in cands if c["tier"] == WATCH]
    rest = [c for c in cands if c["tier"] == REJECT]
    out += [f"## High conviction ({len(high)})", ""]
    if not high:
        out += [
            "None passed every gate. That is a normal outcome: most setups are close to a "
            "coin flip once the stop and the target are fixed.",
            "",
        ]
    for i, c in enumerate(high, 1):
        out += render_high(i, c)
    if picks:
        out += ["## Paper pick (lower conviction, for the journal)", ""]
        for i, c in enumerate(picks, 1):
            out += render_high(i, c)
    if watch:
        out += [f"## Watch ({len(watch)}): positive odds, missed a gate", ""]
        out += [
            f"- **{c['symbol']}** (score {c['score']:.1f}): {'; '.join(c['reasons'])}"
            for c in watch
        ]
        out.append("")
    if rest:
        out += [f"## Rejected ({len(rest)})", ""]
        out += [f"- {c['symbol']}: {'; '.join(c['reasons']) or 'no edge'}" for c in rest]
        out.append("")
    if summary.get("notes"):
        out += ["Notes: " + " | ".join(summary["notes"]), ""]
    out.append(
        "Nothing was placed. The risk check ran with --no-write: to trade one of these, run "
        "/trader-risk on it, then /trader-broker (paper by default, your yes required). "
        "Odds come from the stock's own last 60 days and are a reality check, not a promise. "
        "Not financial advice."
    )
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- cli


def read_config(trader_dir: Path) -> dict[str, Any]:
    path = trader_dir / "config.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def month_realized_pnl(state_path: Path, month: str) -> float:
    """Realized P&L of this month's fills in trader-broker's log, average cost per symbol."""
    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return 0.0
    held: dict[str, list[float]] = {}  # symbol -> [shares, total cost]
    pnl = 0.0
    for rec in state.get("orders", []):
        if rec.get("status") != "filled" or not str(rec.get("date", "")).startswith(month):
            continue
        sym, qty, price = rec.get("symbol"), float(rec.get("qty", 0)), float(rec.get("price", 0))
        pos = held.setdefault(str(sym), [0.0, 0.0])
        if rec.get("side") == "buy":
            pos[0] += qty
            pos[1] += qty * price
        elif pos[0] > 0:
            sold = min(qty, pos[0])
            avg = pos[1] / pos[0]
            pnl += sold * (price - avg)
            pos[0] -= sold
            pos[1] -= sold * avg
    return pnl


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--trader-dir", default="trader", help="your trader/ folder (read only)")
    ap.add_argument("--out", help="run folder (default trader/scout/<timestamp>)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--symbols", help="comma-separated tickers instead of the universe")
    g.add_argument("--watchlist", help="a watchlist.json or text file of tickers")
    ap.add_argument("--max-candidates", type=int, default=12, help="scan hits to run in full")
    ap.add_argument("--top", type=int, help="most HIGH conviction trades to keep (3)")
    ap.add_argument("--stop", help="or-mid (default), or-low, pct:X, atr:K, price:X")
    ap.add_argument("--target", help="trader-exit target: 1R, 1.5R, 2R (default) ...")
    ap.add_argument("--replays", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=19)
    ap.add_argument("--max-trigger-age", type=int, default=10, help="minutes since trigger")
    ap.add_argument(
        "--max-chase-pct", type=float, help="late entry: max %% over the range high (1)"
    )
    ap.add_argument("--no-late-entry", action="store_true", help="only fresh triggers")
    ap.add_argument("--entry-pool", type=int, default=40, help="scan hits given an entry check")
    ap.add_argument("--min-edge", type=float, help="edge over break-even (0.05)")
    ap.add_argument("--min-history", type=int, default=10, help="past sessions it must fire")
    ap.add_argument("--min-score", type=float, help="minimum score for HIGH (55)")
    ap.add_argument(
        "--position-caps",
        help="score:max-position-%% rows, e.g. 95:75,85:55,75:35,65:20,0:10",
    )
    ap.add_argument("--min-price", type=float)
    ap.add_argument("--max-price", type=float, help="skip hits priced above this")
    ap.add_argument("--news-hours", type=float, default=24)
    ap.add_argument("--now", help='pin the clock, New York time "YYYY-MM-DD HH:MM"')
    ap.add_argument("--offline-dir", help="scan.json.gz, entry/, timemachine/<SYM>.json.gz, news/")
    ap.add_argument("--or-minutes", type=int, help="opening range length: 5 or 15 (default)")
    ap.add_argument("--min-rvol", type=float, help="stocks in play: opening-range RVOL (1.0)")
    ap.add_argument("--no-pick", action="store_true", help="no lower-conviction paper PICK")
    ap.add_argument("--no-journal", action="store_true", help="do not record picks")
    ap.add_argument(
        "--pending",
        action="store_true",
        help="also plan names that have not broken out yet, as buy-stops at the range high "
        "(needs --symbols or --watchlist)",
    )
    ap.add_argument("--max-pending-pct", type=float, help="--pending: within this %% (3)")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    missing = [str(p) for p in TOOLS.values() if not p.exists()]
    if missing:
        print(f"trade-scout: missing trader skills: {', '.join(missing)}", file=sys.stderr)
        return 2
    trader_dir = Path(a.trader_dir).resolve()
    trader_dir.mkdir(parents=True, exist_ok=True)
    if a.now:
        stamp = a.now.replace(" ", "_").replace(":", "")
    else:
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = Path(a.out).resolve() if a.out else trader_dir / "scout" / stamp
    cfg = read_config(trader_dir)
    if cfg and cfg.get("paper") is not True:
        print(
            "trade-scout: this tool is for paper trading only, and trader/config.json has "
            '"paper" set to something other than true. Set "paper": true to run it.',
            file=sys.stderr,
        )
        return 2
    sc = cfg.get("scout") or {}
    caps = a.position_caps or sc.get("position_caps")
    opts = Options(
        trader_dir=trader_dir,
        run_dir=run_dir,
        symbols=a.symbols,
        watchlist=Path(a.watchlist) if a.watchlist else None,
        max_candidates=a.max_candidates,
        top=a.top or int(sc.get("top", 3)),
        stop=a.stop or str(sc.get("stop", "or-mid")),
        target=a.target or str(sc.get("target", "2R")),
        replays=a.replays,
        seed=a.seed,
        now=a.now,
        offline_dir=Path(a.offline_dir).resolve() if a.offline_dir else None,
        min_price=a.min_price if a.min_price is not None else sc.get("min_price"),
        max_price=a.max_price if a.max_price is not None else sc.get("max_price"),
        news_hours=a.news_hours,
        th=Thresholds(
            a.max_trigger_age,
            a.min_edge if a.min_edge is not None else float(sc.get("min_edge", 0.05)),
            a.min_history,
            a.min_score if a.min_score is not None else float(sc.get("min_score", 55.0)),
            late_entry=not a.no_late_entry and bool(sc.get("late_entry", True)),
            max_chase_pct=a.max_chase_pct
            if a.max_chase_pct is not None
            else float(sc.get("max_chase_pct", 1.0)),
            min_rvol=a.min_rvol if a.min_rvol is not None else float(sc.get("min_rvol", 1.0)),
            min_atr=float(sc.get("min_atr", 0.5)),
            min_adv=float(sc.get("min_adv", 1_000_000)),
            require_above_vwap=bool(sc.get("require_above_vwap", True)),
        ),
        entry_pool=a.entry_pool,
        or_minutes=a.or_minutes or int(sc.get("or_minutes", 15)),
        daily_pick=not a.no_pick and bool(sc.get("daily_pick", True)),
        caps=parse_caps(caps) if caps else DEFAULT_CAPS,
        pending=a.pending,
        max_pending_pct=a.max_pending_pct
        if a.max_pending_pct is not None
        else float(sc.get("max_pending_pct", 3.0)),
    )
    if a.pending and not (a.symbols or a.watchlist):
        print("trade-scout: --pending needs --symbols or --watchlist", file=sys.stderr)
        return 2
    if not (trader_dir / "config.json").exists():
        print(
            f"trade-scout: no {trader_dir / 'config.json'}, using the example config "
            "($5,000 budget, $50 max loss). Run /trader-budget to set your own rules.",
            file=sys.stderr,
        )
    limit = cfg.get("max_monthly_loss_usd")
    month = (a.now or dt.datetime.now(features.NY).strftime("%Y-%m-%d"))[:7]
    pnl = month_realized_pnl(trader_dir / "state" / "broker.json", month)
    if not pnl:  # paper trading: no broker fills, so use the paper journal's settled picks
        pnl = sum(
            float(r.get("pnl") or 0)
            for r in journal.load(trader_dir)
            if r.get("status") == "settled" and str(r.get("date", "")).startswith(month)
        )
    if limit is not None and pnl <= -float(limit):
        summary: dict[str, Any] = {
            "ok": False,
            "error": f"monthly stop: realized P&L this month is ${pnl:,.2f}, at or past the "
            f"${float(limit):,.0f} limit. No new trades until next month; review the journal.",
            "candidates": [],
            "notes": [],
        }
    else:
        summary = run(opts)
        summary["month_realized_pnl"] = round(pnl, 2)
        if not a.no_journal:
            summary["journaled"] = journal.record(trader_dir, summary, run_dir.name)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    report = render(summary)
    (run_dir / "summary.md").write_text(report)
    try:  # the running log of rules and rejects; never fails the run
        optimize.append_log(trader_dir / "scout-log.md", summary, run_dir.name)
    except Exception as e:  # noqa: BLE001
        print(f"trade-scout: scout-log.md not written: {e}", file=sys.stderr)
    print(json.dumps(summary, indent=2) if a.json else report)
    if not summary.get("ok"):
        return 2
    return 0 if any(c["tier"] == HIGH for c in summary["candidates"]) else 1


if __name__ == "__main__":
    sys.exit(main())
