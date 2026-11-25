# Trade-quant

Paper-trading day-trade scout built from Claude Code skills. See README.md.

- Paper only. Orders reach Alpaca in three ways only: `trader-broker` after `trader-risk`
  passes and the user types an explicit yes; the `trade-desk` agent, which the user has
  authorised (2026-11-21) to place PAPER orders automatically after its paper guard,
  research and a `trader-risk` PASS; or the `crypto-desk` agent, which the user has
  authorised (2026-11-25) to place PAPER crypto orders automatically after its paper guard
  and its 90-day backtest. Each logs every decision in `trades-log.md`. Nothing else places
  orders, and nothing ever switches to a live account.
- `trader/` is runtime state (config, scout runs, journal): gitignored, never commit it. Never
  write the user's email or other personal details into committed files.
- `.claude/skills/trader-*` is vendored upstream code (MIT). Change it only to fix a real bug,
  and say so. Our code is `.claude/skills/trade-scout/` and `.claude/skills/crypto-desk/`.
- Before committing: `uv run pytest` and `uv run ruff check .` (and `ruff format`).
- Zero HIGH trades is a valid result. Do not loosen gates to manufacture one unless asked,
  and then say which gate was loosened.
