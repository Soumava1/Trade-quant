# Trade-quant

Paper-trading day-trade scout built from Claude Code skills. See README.md.

- Paper only. Never place, approve or schedule an order on the user's behalf. Only
  `trader-broker` can lead to an order, and only after `trader-risk` passes it and the user
  types an explicit yes.
- `trader/` is runtime state (config, scout runs, journal): gitignored, never commit it. Never
  write the user's email or other personal details into committed files.
- `.claude/skills/trader-*` is vendored upstream code (MIT). Change it only to fix a real bug,
  and say so. Our code is `.claude/skills/trade-scout/`.
- Before committing: `uv run pytest` and `uv run ruff check .` (and `ruff format`).
- Zero HIGH trades is a valid result. Do not loosen gates to manufacture one unless asked,
  and then say which gate was loosened.
