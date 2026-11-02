---
name: delta-trader
description: >-
  Delta Exchange crypto trader (BTCUSD and other perpetuals). Pulls candles
  from Delta's public API, measures trend (EMA 20/50/200), RSI, ATR, the
  20-bar range, swing levels and volatility, flags setups (breakout,
  breakdown, pullback, oversold/overbought, volume spike), builds a long or
  short plan (entry, stop, target), replays it 10,000 times from real bars
  plus an honest history test, sizes contracts to your risk and leverage
  with a liquidation check, and prints a ticket for the user to place
  themselves. Never places orders. Use when the user says "analyze BTC",
  "Delta Exchange", "BTCUSD", "crypto setup", "plan a BTC trade", "how many
  contracts", "liquidation price", "simulate this crypto trade".
argument-hint: "[SYMBOL, default BTCUSD]"
---

# delta-trader

Tool: `python3 "${CLAUDE_SKILL_DIR}/delta.py"` (standard library, Python 3.8+).
Output files go to `delta/` in the current folder.

```bash
python3 "${CLAUDE_SKILL_DIR}/delta.py" analyze BTCUSD --res 1h --days 30
python3 "${CLAUDE_SKILL_DIR}/delta.py" plan BTCUSD --side long --stop atr:1.5 --target 2R
python3 "${CLAUDE_SKILL_DIR}/delta.py" simulate --res 1h --days 30 --horizon 24
python3 "${CLAUDE_SKILL_DIR}/delta.py" size --capital 1000 --risk 1 --leverage 5
python3 "${CLAUDE_SKILL_DIR}/delta.py" ticket
python3 "${CLAUDE_SKILL_DIR}/delta.py" balance            # optional, needs API key env vars
```

Settings: `DELTA_BASE_URL` (default `https://api.india.delta.exchange`;
global is `https://api.delta.exchange`), `DELTA_API_KEY` / `DELTA_API_SECRET`
only for `balance`. Use a **read-only** key. Never ask for keys in chat.

## Steps

1. `analyze` the symbol. Report trend, RSI, ATR, the range, levels and flags
   in plain numbers. If several timeframes help, run `--res 4h` and `--res 15m` too.
2. Ask the user which side and stop rule they want, or propose one tied to a
   level from the analysis (for example a stop below the last swing low), then `plan`.
3. `simulate` with the same `--res`/`--days`. Report target-first, stop-first,
   neither, expected R against the break-even rate, the history line, and the verdict.
4. `size` with the user's capital, risk % and leverage. Always say the
   estimated liquidation price and whether the stop is hit first. If not,
   say lower the leverage before anything else.
5. `ticket` prints the order. The user places it on Delta themselves.

## Rules

- Measurements and odds, never "buy" or "sell" advice. Say when a result is a coin flip.
- The liquidation price is an isolated-margin estimate; Delta's own figure wins.
- If the API is unreachable, say so; never invent prices.
- Education and tooling, not financial advice.
