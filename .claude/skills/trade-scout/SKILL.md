---
name: trade-scout
description: >-
  Trade Scout. Runs the Day Trader pipeline end to end over a market scan and
  summarises the high conviction trades that passed every gate: trader-scanner
  (opening-range breakouts), trader-news (vetoes halts, risk headlines and
  bad 8-Ks), trader-entry (triggered, and fresh), trader-exit (stop and
  target), trader-timemachine (10,000 replays plus the rule's own history),
  trader-budget (whole shares inside your limits) and one batch trader-risk
  check. Ranks by a documented score built only from the tools' numbers, and
  never places an order. Use when the user says "find me high conviction
  trades", "what can I trade right now", "run the whole pipeline", "scout the
  market", "best setups today", "summary of trades I can place", or
  "/trade-scout".
argument-hint: "[--symbols A,B,C | --watchlist FILE] [--top N] [--target 2R] [--stop or-mid]"
---

# trade-scout

Trade Scout chains seven trader skills so the user gets one answer: which
setups, right now, survived the scan, the news, the entry rule, the exit plan,
the Time Machine, sizing and the risk limits. Most runs find none or one.
That is the tool working: most breakouts are close to a coin flip once the
stop and the target are fixed.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scout.py"                               # full-universe orb scan
python3 "${CLAUDE_SKILL_DIR}/scout.py" --symbols TSLA,NVDA,AMD,PLTR  # fast: your list only
python3 "${CLAUDE_SKILL_DIR}/scout.py" --watchlist trader/watchlist.json   # e.g. the pre-market daily scan
python3 "${CLAUDE_SKILL_DIR}/scout.py" --target 1.5R --stop or-low --top 5
python3 "${CLAUDE_SKILL_DIR}/scout.py" --json                        # the summary as JSON
```

Replay a saved day (what the tests use):

```bash
python3 "${CLAUDE_SKILL_DIR}/scout.py" --offline-dir tests/fixtures/trade_scout --now "2026-10-19 10:15" --target 1R
```

Run it from the project root, after 9:50 AM New York time on a trading day
(the opening range needs to form and a bar has to close above it). Before that
the scanner says so in one line and nothing is ranked.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | your limits (read only). Missing: the example config is used and the user is told |
| `trader/portfolio.json`, `trader/state/broker.json` | read only, so the risk check counts today's trades and losses |
| `trader/scout/<time>/` | the run: one folder per step and per candidate, plus `summary.md` and `summary.json` |

Each candidate runs in its own folder, so nothing it writes (entry, plan,
orders) touches the user's real `trader/` files. The risk check runs with
`--no-write`: trader-broker will not show these orders until the user runs
`/trader-risk` on the one they pick.

## The gates

A trade is **HIGH** conviction only if all of these hold:

1. **News**: no halt or risk headline, and no 8-K for
   bankruptcy, a cyber incident, an impairment, a delisting notice or
   non-reliance on past financials. Legal, regulatory, earnings, deal and
   analyst headlines are cautions, shown on the trade but not vetoes.
2. **Entry**: triggered, and tradable now. A trigger on a bar that closed at
   most `--max-trigger-age` minutes ago (10) is a fresh entry. An older one is
   a **late entry** only if the last closed bar is still above the range high
   and no more than `--max-chase-pct` (1 %) over it: the plan, the Time Machine
   replays and the size then start from that bar's close, not the old trigger.
   Back under the range high is a failed breakout; further over is extended.
   `--no-late-entry` turns this off. The history still measures the rule at
   its first trigger, so a late entry's history is a guide, not an exact match.
3. **In play** (Zarattini, Barbon & Aziz, *A Profitable Day Trading Strategy
   For The U.S. Equity Market*, SFI RP 24-98): the opening range traded at
   least `min_rvol` (1.0x) its normal volume for those minutes over the last
   14 sessions, daily ATR at least `min_atr` ($0.50), average volume at least
   `min_adv` (1M shares), and the entry bar closed above VWAP. Candidates are
   ranked by that relative volume, highest first. A gap of 8 %+, an opening
   range over 1x the daily ATR, or SPY under its own VWAP are cautions.
4. **Exit**: trader-exit accepted the stop and target.
5. **Time Machine edge**: of the replays that hit a side, the target came
   first at least `--min-edge` (5 points) more often than break-even, which is
   1/(1+R): 50 % at 1R, 33 % at 2R.
6. **Time Machine average R** is above zero.
7. **History**: the same rule fired at least `--min-history` times (10) over
   the past ~60 sessions, hit the target at least break-even often, and its
   average R is above zero.
8. **Budget**: trader-budget sized at least one share inside every limit.
9. **Score** at least `--min-score` (55 of 100). See below.
10. **Risk**: PASS in ONE batch check of the top `--top` (3), best first, so
   the daily loss limit, trades per day and duplicates hold across the list.

**PICK**: when nothing is HIGH, the best WATCH with a positive Time Machine
average R and edge, sized and passing the risk check, becomes the run's paper
pick: lower conviction, there so the journal has a trade most days to measure.
`--no-pick` turns it off. **WATCH** means the odds were positive but a gate
failed (thin history, a risk BLOCK, outside the top N). **REJECT** says the
first gate that failed.

## The score (0-100)

Only numbers the tools computed:

| points | from |
| --- | --- |
| 35 | Time Machine edge over break-even, full marks at +20 points |
| 20 | Time Machine average R, full marks at +0.30R |
| 20 | history's target share over break-even, full marks at +20 points, weighted by sample size (full weight at 3x `--min-history`) |
| 15 | opening-range relative volume: nothing at 1x, full marks at 3x |
| 5 | above VWAP (2.5) and SPY above its VWAP (2.5) |
| 5 | freshness: the bar that just closed scores full marks |

## Position size follows conviction

Before sizing, the score sets how much of the budget one position may use
(`--position-caps`, the user's `max_position_pct` is always the ceiling, and
the max loss per trade still applies on top):

| score | max of the budget in one position |
| --- | --- |
| 95 or more | 75 % |
| 85 to 94 | 55 % |
| 75 to 84 | 35 % |
| 65 to 74 | 20 % |
| under 65 | 10 % |

## Your defaults and the monthly stop

Put defaults in a `scout` section of `trader/config.json` (a flag on the
command line still wins), and a monthly loss limit at the top level:

```json
"max_monthly_loss_usd": 80,
"scout": {"stop": "or-low", "target": "2R", "top": 2, "min_price": 5, "max_price": 150,
          "position_caps": "85:75,70:60,55:45,0:10"}
```

`max_price` drops hits priced above it before anything runs (whole shares on a
small budget). When this month's realized P&L in trader-broker's fill log is
at or past `-max_monthly_loss_usd`, the run stops before scanning and says so.

Late entries mean a run at any time until `no_entries_after` (15:30) can find
a trade, not only in the first hour. The entry check runs on up to
`--entry-pool` (40) hits, then the best `--max-candidates` (12) go through
the rest: fresh triggers first, then late entries closest to the range high.

**Several targets, held to the close.** `--target 2R,3R,4R` (or `"target"`
in the `scout` section) plans and replays each one, every replay holding the
trade until the flat-by time (15:55, a hard rule: nothing is held overnight).
The trade keeps the target with the best Time Machine average R, and the
summary lists every target tried. A wider target is only chosen when it pays
more on average over the rest of the day, after the replays that end at the
close without hitting either side.

**Stops tried.** `stop` takes a list too: `or-low,atr-d:0.1` tries the
opening-range low and a stop 10 % of the daily ATR under the entry (the
paper's rule), each with every target, and keeps the best average R.
`or_minutes` 5 uses the 5-minute opening range the paper found strongest.

## The paper journal and its KPIs

Every run appends its HIGH and PICK trades to `trader/journal/trades.jsonl`.
After the close, settle them on the day's real bars and read the KPIs:

```bash
python3 "${CLAUDE_SKILL_DIR}/journal.py" settle
python3 "${CLAUDE_SKILL_DIR}/journal.py" report
```

The KPIs: win rate, average R (expectancy per trade), average win and loss in
R, profit factor (gross wins / gross losses), P&L and return on the budget,
max drawdown in dollars and R, longest losing streak, trades per day, and how
trades ended (target, stop, at the close), split HIGH vs PICK. Judge them over
30-50+ trades; an average R above +0.2 with a drawdown you can live with is
the bar before real money. While paper trading, the monthly stop counts the
journal's settled P&L.

**Paper only.** The run refuses to start unless `"paper": true` is in
`trader/config.json`.

## Steps

1. Check `trader/config.json` exists. If not, say the example limits will be
   used ($5,000 budget, $50 max loss, 3 trades a day) and offer `/trader-budget`
   to set real ones first.
2. Run `scout.py` (add `--symbols` for a quick run on names the user trades).
   A full-universe run takes several minutes, so run it in the background.
3. Read `trader/scout/<time>/summary.json`. For each HIGH trade, open its top
   headlines (and any 8-K) and read them. Headlines are leads, not facts: say
   what the story actually says, with the link, and say so when it is paywalled.
4. Present the summary: the HIGH trades first, each with trigger, entry, stop,
   target, shares, max loss, the Time Machine odds against break-even, the
   history, the news, and the cautions. Then WATCH in one line each, and how
   many were rejected and the most common reason.
5. If the user wants one, hand off: `/trader-risk` on that order (it records
   the PASS), then `/trader-broker`. Never place an order from this skill.

## Rules

- Say what was scanned (charts, usable, hits) and the time of the data.
- Say "passed every gate" or "high conviction by these rules". Never "a sure
  thing", "a winner" or a profit figure. A target is a plan, not a forecast.
- Always give the seed and the replay count with the odds.
- Zero HIGH trades is a valid answer. Do not loosen the thresholds to produce
  one unless the user asks, and then say which gate was loosened.
- Re-run before acting: a summary goes stale within one or two bars.

## What it never does

It never places, approves or schedules an order, never writes to the user's
`trader/orders.json` or `trader/state/risk.json`, and never runs
trader-broker.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval".
- Long side only, no options, margin or short selling.
