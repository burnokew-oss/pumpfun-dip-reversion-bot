# Pump.fun Dip-Reversion Bot

[![make.fun](https://img.shields.io/badge/make.fun-Pump.fun%20Bot%20Sprint-blue)](https://make.fun/c/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.13-blue.svg)](main.py)

A mean-reversion trading bot for pump.fun bonding curves, entered in the
[make.fun](https://make.fun) competition **Pump.fun Bot Sprint: Make Bot And
Win** (`u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed`).
Replay-only harness (`pumpfun-replay/v2`): no live trading, no deposits,
no signatures, no fees beyond the simulated ones.

## Strategy

A curve that ran up and then dropped sharply from its recent high, while
buyers are still stepping in, tends to bounce part of the way back. The bot
buys into that dip in a single tranche and sells the whole position on a
partial rebound, on a stop, or when the trade has gone stale.

- **Entry:** price 30–45% below the 2500-slot (~17 min) high, on a strong
  curve (≥ 25 SOL real reserves), with at least 3 buys among the last
  6 trades, non-negative net flow, and the most recent trade a buy.
- **Size:** 0.20 SOL per coin, one tranche, no adds (averaging down into a
  falling dip was the main source of losses in testing).
- **Exit:** take profit +6%, stop loss −3%, or hold longer than 200 slots;
  300-slot cooldown before the same coin can be entered again.
- **Weak tier disabled:** curves below 25 SOL real reserves are not traded
  (they only lost money in backtests).

Pure Python standard library — no third-party dependencies. Fully
deterministic: no clock, no randomness, no environment reads.

## Files

- `main.py` — the bot (entrypoint, run as `python3 -P -s main.py`).
- `vendor/makefun_bot.py` — the harness SDK shim shipped with the kit.
- `.makefun/owner.json` — payout wallet claim file.

## Local backtest (dev window)

Local replay simulator results on the kit's dev dataset (1,222 coins):

| Variant | Net PnL | Max drawdown | Coins |
| --- | --- | --- | --- |
| Kit pilot defaults (TP 20% / SL 10% / hold 600, adds on) | −3.78 SOL | 47.3% | 199 |
| Tuned (this entry: TP 6% / SL 3% / hold 200, no adds, strong-only) | **−0.118 SOL** | **4.8%** | 32 |

Dev scores are practice only — the final is judged on a fresh 6-hour
window after entries close. No variant tested reached positive PnL on dev;
the tuned variant above is the best of ~20 tested and is submitted as-is,
with its dev score recorded honestly.

## License

MIT — see [LICENSE](LICENSE).
