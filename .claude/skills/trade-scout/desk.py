#!/usr/bin/env python3
"""The paper desk's plumbing: the fixed steps between the scout and the order.

The trade-desk agent calls these so the steps that never need judgment run as code:
picking the candidates, the research desks in parallel, the fresh-price and duplicate
checks, trader-risk, the approval record, and one log line per decision in
`trades-log.md`. The agent keeps the judgment (reading the news) and the Alpaca calls.

Usage (from the project root):
    python desk.py lock --owner "scan 10:12"     # exit 0: acquired, 3: another run holds it
    python desk.py candidates                    # newest scout run: what is tradable, as JSON
    python desk.py research MOD PDD              # news, earnings, Congress, in parallel
    python desk.py arm MOD --ask 141.40 --research CAUTION --note "+13% move, no news found"
                                                 # exit 0: prints the place_stock_order JSON
    python desk.py record MOD --status placed --order-id ID --qty 1 --price 141.38
    python desk.py log NONE - - - "0 HIGH, no PICK; 17 rejected, mostly failed breakouts."
    python desk.py unlock

Nothing here talks to a broker or places an order: `arm` prints the order, the agent
places it with Alpaca's tool, and `record` writes down what came back.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import subprocess  # noqa: S404 - runs the sibling trader-* scripts only
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
SKILLS = HERE.parent
NY = ZoneInfo("America/New_York")
LOG = Path("trades-log.md")
LOCK = Path("trader/state/desk.lock")
PENDING = Path("trader/state/desk-pending.json")
SCOUT_DIR = Path("trader/scout")
STALE_MIN = 45  # a run that died without unlocking stops blocking after this long
MAX_CHASE = 0.01  # the ask may be at most 1 % over the planned entry
ACTIONS = ("BUY", "SKIP", "ATTEMPT", "EXIT", "CLOSE", "NONE", "HALT")
VERDICTS = ("CLEAR", "CAUTION", "DOWNGRADE")
TIER_ORDER = {"HIGH": 0, "PICK": 1, "WATCH": 2}
STOP_RUN_WORDS = ("daily loss", "trades per day", "max_trades_per_day", "monthly", "daily limit")
REPLY = "yes - automatic paper desk, standing instruction from the user"
READY, SKIPPED, BUSY, STOP_RUN = 0, 1, 3, 4


def now_ny() -> dt.datetime:
    return dt.datetime.now(NY)


def fmt_size(size: str) -> str:
    size = str(size).strip()
    if size in ("", "-"):
        return "-"
    return size if size.endswith("sh") else f"{size} sh"


def fmt_price(price: str) -> str:
    """'364.2' -> '$364.20'; '~141.35' -> '~$141.35' (planned, not filled); '-' stays."""
    p = str(price).strip().lstrip("$")
    if p in ("", "-"):
        return "-"
    approx = p.startswith("~")
    p = p.lstrip("~$")
    try:
        p = f"{float(p):,.2f}"
    except ValueError as exc:
        raise ValueError(f"price must be a number, '~number' or '-', got {price!r}") from exc
    return f"{'~' if approx else ''}${p}"


def log_line(
    action: str,
    symbol: str,
    size: str,
    price: str,
    reason: str,
    path: Path = LOG,
    now: dt.datetime | None = None,
) -> str:
    """Append one decision to the log and return the line."""
    action = action.upper()
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}, got {action!r}")
    reason = " ".join(str(reason).split())  # one line, whatever came in
    if not reason:
        raise ValueError("every decision needs a reason")
    if reason[-1] not in ".!?":
        reason += "."
    stamp = (now or now_ny()).strftime("%Y-%m-%d %H:%M")
    sym = (symbol or "-").strip().upper() or "-"
    line = f"- {stamp} ET | {sym} | {action} | {fmt_size(size)} | {fmt_price(price)} | {reason}"
    new = not path.exists()
    with path.open("a", encoding="utf-8") as fh:
        if new:
            fh.write("# Paper trades log\n\n")
        fh.write(line + "\n")
    return line


def acquire(
    owner: str, path: Path = LOCK, now: dt.datetime | None = None, stale_min: int = STALE_MIN
) -> tuple[bool, str]:
    """Take the run lock. Returns (acquired, message). A lock older than stale_min is stale."""
    now = now or now_ny()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        held = dt.datetime.fromtimestamp(path.stat().st_mtime, NY)
        age = (now - held).total_seconds() / 60
        holder = path.read_text(encoding="utf-8").strip() or "unknown"
        if age < stale_min:
            return False, f"another desk run holds the lock ({holder}, {age:.0f} min old)"
        path.unlink()  # stale: its run died without unlocking
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False, "another desk run took the lock a moment ago"
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f"{owner} since {now:%H:%M} ET, pid {os.getpid()}\n")
    os.utime(path, (now.timestamp(), now.timestamp()))
    return True, f"lock taken by {owner}"


def release(path: Path = LOCK) -> None:
    path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- candidates


def py(script: str, *args: str, timeout: int = 900) -> subprocess.CompletedProcess:
    """Run one sibling trader-* script (e.g. 'trader-risk/check.py') from the project root."""
    return subprocess.run(  # noqa: S603 - our own scripts, fixed paths
        [sys.executable, str(SKILLS / script), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def newest_summary(scout_dir: Path = SCOUT_DIR) -> Path:
    runs = sorted(scout_dir.glob("*/summary.json"))
    if not runs:
        raise FileNotFoundError(f"no scout run in {scout_dir}: run scout.py first")
    return runs[-1]


def pick_candidates(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Tradable: HIGH, then the PICK (only exists without a HIGH), then sized WATCH."""
    out = [
        c for c in summary.get("candidates", []) if c.get("tier") in TIER_ORDER and c.get("order")
    ]
    out.sort(key=lambda c: (TIER_ORDER[c["tier"]], -(c.get("score") or 0)))
    return out


def missed_gate(c: dict[str, Any]) -> str:
    """The first gate a candidate missed, short: the text before the explanation."""
    reasons = c.get("reasons") or []
    return reasons[0].split(":")[0].strip() if reasons else "nothing"


def none_reason(summary: dict[str, Any]) -> str:
    cands = summary.get("candidates", [])
    tiers = collections.Counter(c.get("tier") for c in cands)
    rejected = [c for c in cands if c.get("tier") == "REJECT"]
    why = collections.Counter(
        "no Time Machine edge" if (g := missed_gate(c)).startswith("Time Machine edge") else g
        for c in rejected
    )
    top = ", ".join(f"{k} ({n})" for k, n in why.most_common(2)) or "none ran in full"
    return (
        f"{tiers['HIGH']} HIGH, {tiers['PICK']} PICK, {tiers['WATCH']} WATCH, none sized; "
        f"{len(rejected)} rejected, mostly {top}"
    )


def age_minutes(as_of: str, now: dt.datetime) -> int | None:
    try:
        t = dt.datetime.strptime(as_of.replace(" ET", ""), "%Y-%m-%d %H:%M").replace(tzinfo=NY)
    except (AttributeError, ValueError):
        return None
    return round((now - t).total_seconds() / 60)


def candidates_report(
    path: Path, now: dt.datetime | None = None, verdicts: dict[str, Any] | None = None
) -> dict[str, Any]:
    s = json.loads(path.read_text(encoding="utf-8"))
    picks = pick_candidates(s)
    known = load_verdicts(now) if verdicts is None else verdicts
    rows = []
    for c in picks:
        plan, size = c.get("plan") or {}, c.get("size") or {}
        rows.append(
            {
                "symbol": c["symbol"],
                "tier": c["tier"],
                "score": c.get("score"),
                "entry": plan.get("entry"),
                "stop": plan.get("stop"),
                "target": plan.get("target"),
                "target_r": plan.get("target_r"),
                "qty": size.get("qty"),
                "max_loss": size.get("max_loss"),
                "missed": None if c["tier"] == "HIGH" else missed_gate(c),
                "cautions": c.get("cautions") or [],
                "news": [n.get("title") for n in (c.get("news") or [])[:3]],
                "verdict_today": known.get(c["symbol"]),  # research from earlier today
            }
        )
    return {
        "summary": str(path),
        "as_of": s.get("as_of"),
        "age_min": age_minutes(s.get("as_of", ""), now or now_ny()),
        "scan": s.get("scan"),
        "candidates": rows,
        "none_reason": None if rows else none_reason(s),
    }


# --------------------------------------------------------------------------- shortlist

SHORTLIST = Path("trader/shortlist.json")
# Daily-chart setups that lean the same way as a long opening-range breakout. The oversold
# setups are mean-reversion and left out.
MOMENTUM = ("breakout_20d", "bull_flag", "reclaim_ma20")


def build_shortlist(
    daily: dict[str, Any],
    add: list[str],
    min_price: float,
    max_price: float,
    size: int = 40,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """The names worth watching at the open: the ones you add (movers, news) first, then
    the daily scan's momentum setups inside your price range, by setup then dollar volume.

    ponytail: a heuristic. The paper's "in play" test (opening relative volume) can only run
    after the open; the full-universe scans later in the morning catch what this misses."""
    now = now or now_ny()
    rank = {s: i for i, s in enumerate(MOMENTUM)}
    rows = []
    for h in daily.get("hits", []):
        setups = [s for s in h.get("setups", []) if s in rank]
        price = h.get("price") or 0
        if setups and min_price <= price <= max_price:
            best = min(rank[s] for s in setups)
            rows.append((best, -price * (h.get("volume") or 0), h["symbol"], setups))
    rows.sort()
    hits, seen = [], set()
    for sym in add:
        sym = sym.upper()
        if sym not in seen:
            seen.add(sym)
            hits.append({"symbol": sym, "why": "added: mover or news"})
    for _, _, sym, setups in rows:
        if len(hits) >= size:
            break
        if sym not in seen:
            seen.add(sym)
            hits.append({"symbol": sym, "why": "daily: " + ", ".join(setups)})
    return {
        "mode": "shortlist",
        "date": now.date().isoformat(),
        "as_of": now.strftime("%Y-%m-%d %H:%M ET"),
        "daily_as_of": daily.get("as_of"),
        "hits": hits[:size],
    }


def shortlist_today(path: Path = SHORTLIST, now: dt.datetime | None = None) -> bool:
    """True when the shortlist file exists and was built today (New York time)."""
    if not path.exists():
        return False
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return False
    return doc.get("date") == (now or now_ny()).date().isoformat()


def read_config(path: Path = Path("trader/config.json")) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads((SKILLS / "trader-budget" / "config.example.json").read_text("utf-8"))


# --------------------------------------------------------------------------- verdicts


def verdicts_path(now: dt.datetime | None = None) -> Path:
    return Path("trader/state") / f"verdicts-{(now or now_ny()):%Y-%m-%d}.json"


def save_verdict(
    symbol: str, verdict: str, note: str, now: dt.datetime | None = None, path: Path | None = None
) -> dict[str, Any]:
    """Keep today's research verdict per ticker, so the open can act without re-reading."""
    now = now or now_ny()
    path = path or verdicts_path(now)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data[symbol.upper()] = {"verdict": verdict, "note": note.strip(), "at": f"{now:%H:%M}"}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data[symbol.upper()]


def load_verdicts(now: dt.datetime | None = None, path: Path | None = None) -> dict[str, Any]:
    path = path or verdicts_path(now)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# --------------------------------------------------------------------------- research


def research(tickers: list[str], now: dt.datetime | None = None, timeout: int = 300) -> dict:
    """News, earnings and Congress trades for these tickers, the three desks in parallel."""
    now = now or now_ny()
    out = Path("trader/research") / now.strftime("%Y-%m-%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    since = (now - dt.timedelta(days=30)).date().isoformat()
    jobs = {
        "news": (
            ["trader-news/news.py", "--tickers", *tickers, "--all", "--hours", "72"]
            + ["--dry-run", "--json"],
            "news.json",
        ),
        "analyst": (
            ["trader-analyst/facts.py", *tickers, "--quarters", "5", "--json"],
            "analyst.json",
        ),
        "pelosi": (
            ["trader-pelosi/ptr.py", "new", "--since", since, "--read", "--dry-run"]
            + ["--state", str(out / "pelosi-state.json"), "--tickers", *tickers],
            "pelosi.txt",
        ),
    }
    procs, files = {}, []
    for name, (args, fname) in jobs.items():
        fo = (out / fname).open("w", encoding="utf-8")
        fe = (out / f"{name}.err").open("w", encoding="utf-8")
        files += [fo, fe]
        procs[name] = subprocess.Popen(  # noqa: S603 - our own scripts, fixed paths
            [sys.executable, str(SKILLS / args[0]), *args[1:]], stdout=fo, stderr=fe
        )
    status = {}
    deadline = dt.datetime.now().timestamp() + timeout
    for name, p in procs.items():
        try:
            status[name] = p.wait(timeout=max(1, deadline - dt.datetime.now().timestamp()))
        except subprocess.TimeoutExpired:
            p.kill()
            status[name] = "timeout"
    for f in files:
        f.close()
    return {"dir": str(out), "exit": status, "files": sorted(p.name for p in out.iterdir())}


# --------------------------------------------------------------------------- arm and record


def describe(c: dict[str, Any], verdict: str, note: str = "") -> str:
    """The honest one-sentence reason for a trade: weak cases say they are weak."""
    score = c.get("score") or 0
    tr = (c.get("plan") or {}).get("target_r")
    if c["tier"] == "HIGH":
        base = (
            f"Passed every gate (score {score:.0f}, {tr:g}R target)" if tr else "Passed every gate"
        )
    elif c["tier"] == "PICK":
        base = (
            f"PICK only, lower conviction: no trade passed every gate; "
            f"missed {missed_gate(c)} (score {score:.0f})"
        )
    else:
        base = f"WATCH, lower conviction: missed {missed_gate(c)} (score {score:.0f})"
    text = f"{base}; research {verdict}"
    return f"{text} ({note.strip().rstrip('.')})" if note.strip() else text


def traded_today(log: Path, symbol: str, now: dt.datetime) -> bool:
    if not log.exists():
        return False
    day = f"- {now:%Y-%m-%d}"
    marks = (f"| {symbol} | BUY |", f"| {symbol} | ATTEMPT |")
    return any(
        line.startswith(day) and any(m in line for m in marks)
        for line in log.read_text(encoding="utf-8").splitlines()
    )


def arm(
    symbol: str,
    ask: float,
    verdict: str,
    note: str = "",
    summary_path: Path | None = None,
    now: dt.datetime | None = None,
    log: Path = LOG,
    orders: Path = Path("trader/orders.json"),
    pending: Path = PENDING,
    run=py,
) -> tuple[int, dict | None]:
    """Every check before the order. Returns (READY, place_stock_order args) or a skip code.

    Every outcome but READY writes its SKIP line; READY writes the ATTEMPT line."""
    now = now or now_ny()
    sym = symbol.upper()
    s = json.loads((summary_path or newest_summary()).read_text(encoding="utf-8"))
    c = next((c for c in pick_candidates(s) if c["symbol"] == sym), None)
    if c is None:
        log_line(
            "SKIP", sym, "-", "-", "Not a tradable candidate in the newest scout run.", log, now
        )
        return SKIPPED, None
    plan = c["plan"]
    entry, stop = float(plan["entry"]), float(plan["stop"])

    def skip(why: str, code: int = SKIPPED) -> tuple[int, None]:
        log_line("SKIP", sym, "-", f"~{entry}", f"{why}; {describe(c, verdict, note)}", log, now)
        return code, None

    if verdict == "DOWNGRADE":
        return skip("Research DOWNGRADE")
    if c["tier"] == "WATCH" and verdict != "CLEAR":
        return skip("WATCH needs research CLEAR")
    if traded_today(log, sym, now):
        return skip("Already traded today")
    if ask > entry * (1 + MAX_CHASE):
        return skip(
            f"Stale: ask ${ask:.2f} is {ask / entry - 1:.1%} over the planned entry (limit 1%)"
        )
    if ask <= stop:
        return skip(f"Stale: ask ${ask:.2f} is at or under the stop ${stop:.2f}")

    orders.parent.mkdir(parents=True, exist_ok=True)
    orders.write_text(json.dumps({"orders": [c["order"]]}, indent=2) + "\n", encoding="utf-8")
    r = run("trader-risk/check.py", "--json")
    try:
        result = json.loads(r.stdout)["results"][0]
    except (ValueError, KeyError, IndexError):
        return skip(f"Risk check did not run: {(r.stderr or r.stdout).strip()[:200]}")
    if result["verdict"] != "PASS":
        reasons = "; ".join(result.get("reasons") or ["no reason given"])
        stop_run = any(w in reasons.lower() for w in STOP_RUN_WORDS)
        return skip(f"Risk BLOCK: {reasons}", STOP_RUN if stop_run else SKIPPED)
    a = run("trader-broker/ticket.py", "approve", "--index", "1", "--reply", REPLY)
    if "Recorded" not in a.stdout:
        return skip(f"Approval not recorded: {(a.stdout or a.stderr).strip()[:200]}")
    order, _ = json.JSONDecoder().raw_decode(a.stdout[a.stdout.index("{") :])
    reason = describe(c, verdict, note)
    qty = (c.get("size") or {}).get("qty") or order.get("qty")
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(
        json.dumps({"symbol": sym, "qty": qty, "entry": entry, "reason": reason}), encoding="utf-8"
    )
    log_line(
        "ATTEMPT",
        sym,
        str(qty),
        f"~{entry}",
        f"Bracket: stop ${stop:.2f}, target ${float(plan['target']):.2f}; {reason}",
        log,
        now,
    )
    return READY, order


def record(
    symbol: str,
    status: str,
    order_id: str = "",
    qty: str = "",
    price: str = "",
    note: str = "",
    now: dt.datetime | None = None,
    log: Path = LOG,
    pending: Path = PENDING,
    run=py,
) -> str:
    """What came back from place_stock_order: the broker record, and BUY or SKIP in the log."""
    sym = symbol.upper()
    if not pending.exists():
        raise ValueError("nothing armed: run `desk.py arm` first")
    p = json.loads(pending.read_text(encoding="utf-8"))
    if p["symbol"] != sym:
        raise ValueError(f"the armed order is for {p['symbol']}, not {sym}")
    args = ["result", "--index", "1", "--status", status]
    if order_id:
        args += ["--broker-id", order_id]
    if note:
        args += ["--note", note]
    if qty and price:
        args += ["--filled-qty", str(qty), "--filled-price", str(price)]
    r = run("trader-broker/ticket.py", *args)
    if r.returncode:
        print(f"ticket.py result: {(r.stderr or r.stdout).strip()}", file=sys.stderr)
    if status == "placed":
        fill = str(price) if price else f"~{p['entry']}"
        tail = "" if price else " Fill not reported yet."
        line = log_line("BUY", sym, str(qty or p["qty"]), fill, p["reason"] + "." + tail, log, now)
    else:
        why = f"Order {status}: {note or 'no detail came back'}; {p['reason']}"
        line = log_line("SKIP", sym, "-", f"~{p['entry']}", why, log, now)
    pending.unlink()
    return line


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("log", help="append one decision to trades-log.md")
    p.add_argument("action", choices=ACTIONS, type=str.upper)
    p.add_argument("symbol")
    p.add_argument("size", help="shares, or -")
    p.add_argument("price", help="fill price, ~planned price, or -")
    p.add_argument("reason", help="one honest sentence")
    p.add_argument("--log", type=Path, default=LOG)
    p = sub.add_parser("lock", help="take the run lock (exit 3 if another run holds it)")
    p.add_argument("--owner", default="desk")
    p.add_argument("--path", type=Path, default=LOCK)
    p = sub.add_parser("unlock", help="release the run lock")
    p.add_argument("--path", type=Path, default=LOCK)
    p = sub.add_parser("candidates", help="the newest scout run's tradable trades, as JSON")
    p.add_argument("--summary", type=Path, help="a summary.json (default: the newest run)")
    p = sub.add_parser("shortlist", help="before the open: the names to watch at the open")
    p.add_argument("--add", nargs="*", default=[], type=str.upper, help="movers, news names")
    p.add_argument("--size", type=int, default=40)
    p.add_argument("--daily", type=Path, help="a saved `scan.py daily --json` (default: run it)")
    p.add_argument("--check", action="store_true", help="exit 0 if today's shortlist exists")
    p = sub.add_parser("verdict", help="keep today's research verdict for a ticker")
    p.add_argument("symbol", type=str.upper)
    p.add_argument("verdict", choices=VERDICTS, type=str.upper)
    p.add_argument("note", nargs="?", default="")
    p = sub.add_parser("research", help="news, earnings and Congress desks in parallel")
    p.add_argument("tickers", nargs="+", type=str.upper)
    p = sub.add_parser("arm", help="every check before an order; prints its JSON when ready")
    p.add_argument("symbol", type=str.upper)
    p.add_argument("--ask", type=float, required=True, help="the latest ask from Alpaca")
    p.add_argument("--research", required=True, choices=VERDICTS, type=str.upper)
    p.add_argument("--note", default="", help="the research facts behind the verdict, short")
    p.add_argument("--summary", type=Path)
    p = sub.add_parser("record", help="write down what place_stock_order returned")
    p.add_argument("symbol", type=str.upper)
    p.add_argument("--status", required=True, choices=["placed", "failed", "denied"])
    p.add_argument("--order-id", default="")
    p.add_argument("--qty", default="")
    p.add_argument("--price", default="")
    p.add_argument("--note", default="")
    a = ap.parse_args(argv)

    if a.cmd == "candidates":
        try:
            print(json.dumps(candidates_report(a.summary or newest_summary()), indent=2))
        except FileNotFoundError as exc:
            print(exc, file=sys.stderr)
            return 2
        return 0
    if a.cmd == "shortlist":
        if a.check:
            ok = shortlist_today()
            print(f"{SHORTLIST} is {'from today' if ok else 'missing or old'}")
            return 0 if ok else 1
        if a.daily:
            daily = json.loads(a.daily.read_text(encoding="utf-8-sig"))  # PowerShell adds a BOM
        else:
            r = py("trader-scanner/scan.py", "daily", "--json", "--no-write", timeout=900)
            try:
                daily = json.loads(r.stdout)
            except ValueError:
                print(f"daily scan failed: {(r.stderr or r.stdout).strip()[:300]}", file=sys.stderr)
                daily = {"hits": []}  # the added names still make a shortlist
        scout_cfg = read_config().get("scout", {})
        doc = build_shortlist(
            daily, a.add, scout_cfg.get("min_price", 5), scout_cfg.get("max_price", 150), a.size
        )
        SHORTLIST.parent.mkdir(parents=True, exist_ok=True)
        SHORTLIST.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {SHORTLIST}: {len(doc['hits'])} names (daily scan of {daily.get('as_of')}).")
        print(" ".join(h["symbol"] for h in doc["hits"]))
        return 0 if doc["hits"] else 1
    if a.cmd == "verdict":
        print(json.dumps({a.symbol: save_verdict(a.symbol, a.verdict, a.note)}))
        return 0
    if a.cmd == "research":
        print(json.dumps(research(a.tickers), indent=2))
        return 0
    if a.cmd == "arm":
        code, order = arm(a.symbol, a.ask, a.research, a.note, a.summary)
        if code == READY:
            print("READY. Call place_stock_order with exactly:")
            print(json.dumps(order, indent=2))
        else:
            print(
                {
                    SKIPPED: "SKIPPED (logged)",
                    STOP_RUN: "STOP: a daily or monthly limit is hit; "
                    "place nothing more this run (logged)",
                }[code]
            )
        return code
    if a.cmd == "record":
        try:
            print(record(a.symbol, a.status, a.order_id, a.qty, a.price, a.note))
        except ValueError as exc:
            print(f"Not recorded: {exc}", file=sys.stderr)
            return 2
        return 0
    if a.cmd == "log":
        try:
            print(log_line(a.action, a.symbol, a.size, a.price, a.reason, path=a.log))
        except ValueError as exc:
            print(f"Not logged: {exc}", file=sys.stderr)
            return 2
        return 0
    if a.cmd == "lock":
        ok, msg = acquire(a.owner, path=a.path)
        print(msg)
        return 0 if ok else BUSY
    release(a.path)
    print("lock released")
    return 0


if __name__ == "__main__":
    sys.exit(main())
