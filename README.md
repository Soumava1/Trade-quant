# Trade-quant

A paper-trading day-trade scout built as Claude Code skills. Each run finds
opening-range breakouts in US stocks that are "in play", checks the news,
plans the stop and target, replays the trade 10,000 times on the stock's own
recent bars, sizes it to your limits, and runs a risk check. You get a short
ranked list of trades, or an honest "nothing today". A paper journal then
measures how those picks actually did.

**Paper trading only. Nothing here places an order.** The scout refuses to
run unless `"paper": true` is in your config. Not financial advice: most
people who day trade lose money, and the odds shown are a reality check from
the last 60 days, not a forecast.

## How it works

```
scanner -> news -> entry -> stocks-in-play filter -> exit plan -> Time Machine -> budget -> risk
                                                                              \-> paper journal -> KPIs
```

| Step | Skill | What it does |
| --- | --- | --- |
| Scan | `trader-scanner` | Finds stocks whose 5- or 15-minute opening range has broken to the upside, across ~5,900 US stocks |
| News | `trader-news` | Yahoo headlines and SEC 8-Ks. Halts, risk headlines and serious 8-Ks veto a trade; legal, regulatory, earnings and deal news are cautions |
| Entry | `trader-entry` | A fresh breakout, or a late entry if the price is still holding within 1% above the range high |
| In play | `trade-scout` | Opening-range relative volume at least normal, daily ATR at least $0.50, at least 1M shares a day, and above VWAP; ranked by relative volume |
| Exit | `trader-exit` | Stop at the range low or 10% of the daily ATR; targets of 2R, 3R and 4R; always flat by 3:55 PM |
| Time Machine | `trader-timemachine` | 10,000 replays of the rest of the day for each stop/target pair, plus every past session the rule fired; keeps the best average R |
| Budget | `trader-budget` | Whole shares inside your max loss, position cap, cash buffer and order limits |
| Risk | `trader-risk` | One batch check: daily loss, trades per day, reward:risk, entry window, duplicates |
| Journal | `trade-scout/journal.py` | Records each pick, settles it on that day's real bars after the close, and reports the KPIs |

Each trade gets a score out of 100, built only from the tools' numbers: Time
Machine edge and average R, the rule's history, relative volume, VWAP and
the market's direction, and freshness. Position size follows the score.

- **HIGH:** passed every gate.
- **PICK:** when nothing is HIGH, the best trade with a positive expected
  outcome, lower conviction, so the journal has something to measure most days.
- **WATCH / REJECT:** each comes with the reason.

The stocks-in-play rules follow Zarattini, Barbon & Aziz, ["A Profitable Day
Trading Strategy For The U.S. Equity
Market"](https://ideas.repec.org/p/chf/rpseri/rp2498.html) (Swiss Finance
Institute RP 24-98).

## Requirements

- Python 3.11+ (the tools use only the standard library)
- Network access to `query1/query2.finance.yahoo.com`, `feeds.finance.yahoo.com`,
  `www.nasdaqtrader.com` and `www.sec.gov`
- [Claude Code](https://code.claude.com) to use the skills and the agent (the
  scripts also run on their own)

## Setup

```bash
mkdir -p trader
cp config/config.example.json trader/config.json   # then edit your limits
python3 .claude/skills/trader-scanner/scan.py universe   # stock list, refresh weekly
```

`trader/` holds your config and scan runs. It is gitignored and never
committed. The paper journal lives in `journal/trades.jsonl` (set by
`journal_dir` in the config) so its history is kept in git. Put your name and email in `sec_user_agent`, or in the
`SEC_USER_AGENT` environment variable, to include SEC filings: the SEC asks
every tool that reads EDGAR to identify itself.

The example config is set up for a $1,000 paper account:

| Setting | Value |
| --- | --- |
| Budget | $1,000, with 10% kept in cash |
| Max loss | $15 per trade, $30 per day, $80 per month (then the scout stops until next month) |
| Trades | 2 a day |
| Reward:risk | at least 2:1 |
| Prices | $5 to $150 |
| Position cap by score | 85+ → 75%, 70+ → 60%, 55+ → 45%, otherwise (paper picks) 25% |

## Use

In Claude Code:

- `/trade-scout`, or ask "find me high conviction trades", runs the full
  pipeline and summarises it.
- The `trade-scout` agent does the same and reads the news behind each HIGH
  trade before reporting.

From the shell:

```bash
python3 .claude/skills/trade-scout/scout.py                      # full scan
python3 .claude/skills/trade-scout/scout.py --symbols NVDA,AMD   # your list only
python3 .claude/skills/trade-scout/journal.py settle             # after the close
python3 .claude/skills/trade-scout/journal.py report             # the KPIs
```

**When to run:** after the opening range forms, 9:40 AM New York time for the
5-minute range and 9:50 AM for the 15-minute one. Most breakouts happen before
about 10:45, and late entries can still be found until 3:30 PM. A full scan
takes 5 to 10 minutes.

**Scheduled runs:** Claude runs the scout on trading days at 9:47, 9:58,
10:12, 10:27 and 10:44 AM and 12:33 PM New York time, and settles the journal
at 4:07 PM. It posts each summary and commits the journal to `main`. Entries
before 9:45 are blocked by the config, so 9:47 is the first useful scan.

**To take a trade on paper:** run `/trader-risk` on it (which records the
PASS), then `/trader-broker`. It needs your explicit yes and a connected
Alpaca paper account.

## KPIs

`journal.py report` shows HIGH and PICK separately:

- win rate
- average R (the expected gain per trade, in multiples of the risk)
- average win and average loss
- profit factor (gross wins / gross losses)
- P&L and return on the budget
- max drawdown, in dollars and in R
- longest losing streak
- trades per day
- how trades ended: target, stop or at the close

Judge the results over 30 to 50 or more trades. An average R above +0.2 with a
drawdown you can live with is the bar before risking real money, and real
fills (slippage at the open) will be worse than the replays.

## Repository layout

```
.claude/skills/trader-*      the 11 Day Trader skills (vendored, MIT, see LICENSE-day-trader-skill)
.claude/skills/trade-scout/  scout.py (pipeline), features.py (stocks in play), journal.py (KPIs), SKILL.md
.claude/agents/trade-scout.md
config/config.example.json   starting limits for trader/config.json
journal/trades.jsonl         the paper journal (picks and their settled outcomes)
tests/                       offline tests on saved market data (TSLA, 2026-10-19)
```

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
```

The tests run offline on fixtures from the upstream pack. Live runs need the
network hosts above.

## Credits

The `trader-*` skills come from
[Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
(MIT, see `.claude/skills/LICENSE-day-trader-skill`).
