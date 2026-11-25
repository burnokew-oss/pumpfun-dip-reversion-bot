"""Pump.fun mean-reversion bot for agent-fun/replay/v2 (pump.fun bonding curves).

Pure standard library. Run by the grader as ``python3 -P -s /bot/main.py``
with ``PYTHONHASHSEED=0``.

Strategy: a pure-stdlib port of the kit pilot
``baselines/pilot/reversion/strategy.ts``. A curve that ran up and then
dropped sharply from its recent high, while buyers are still stepping in,
tends to bounce part of the way back. We buy into that dip in small
tranches (each within the per-reply size cap) and sell the whole position
on a partial rebound, on a stop, or when the trade has gone stale.

One hello starts the run; every batch carries events, marks and fills and
the bot answers with at most one reply of orders per batch. Fully
deterministic: no clock, no randomness, no environment reads, no set
iteration order dependence (sets are only counted / membership-tested,
never iterated).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script directory off sys.path)

SOL = makefun_bot.LAMPORTS_PER_SOL
BPS = 10_000

# --- Strategy parameters (lamports / fractions / slots) ----------------------
# Chosen from the middle of a broadly profitable region on the dev window
# (not its single best point): a ~2500-slot (~17 min) high, a 30-45% dip on
# strong curves, 20% take profit, 10% stop. (Kit pilot defaults.)
#
# TUNED (sim_local.py, dev window, 25 Nov 2026): the pilot defaults lose
# -3.78 SOL (maxDD 47%). Two changes dominate the improvement: fast exits
# (TP 6% / SL 3% / hold 200 slots) and NO tranche adds (add_band 0.0 —
# averaging down into a falling dip was the main bleed), plus dropping the
# weak tier entirely (weak_dd_min 0.99 — weak coins only lost money).
# Tuned result: -0.118 SOL, maxDD 4.8%, 32 coins, 303 fills.
# Still negative on dev — recorded as-is; the final is judged on a fresh
# 6-hour window. No variant tested reached positive PnL.
PARAMS = {
    "high_window": 2500,
    "dd_min": 0.30,
    "dd_max": 0.45,
    "min_real_sol": 3_000_000_000,
    "min_high_real_sol": 3_000_000_000,
    "flow_trades": 6,
    "min_buys": 3,
    "last_must_be_buy": True,
    "min_net_flow": 0,
    "tranche": 200_000_000,
    "max_position": 1_000_000_000,
    "min_buy": 8_000_000,
    "add_band": 0.0,
    "take_profit": 0.06,
    "recover_share": 0.0,
    "stop_loss": 0.03,
    "max_hold_slots": 200,
    "cooldown_slots": 300,
    "buy_slippage_bps": 500,
    "sell_slippage_bps": 3000,
    "max_entries_per_batch": 2,
    "strong_real_sol": 25_000_000_000,
    "weak_tranche": 10_000_000,
    "weak_max_position": 40_000_000,
    "max_weak_open": 3,
    "weak_max_hold_slots": 200,
    "weak_dd_min": 0.99,
    "weak_dd_max": 0.60,
    "sell_splits": 2,
}

# Curve fees: protocol 95 bps + creator 30 bps = 125 bps on every fill.
FEE_BPS = 125


def ceil_div(a, b):
    return -(-a // b)


def buy_cap(state, mint):
    """Per-reply buy cap, with a margin: the harness checks it against the
    reserves shifted by our own earlier flow on the coin."""
    return (state.max_buy_lamports(mint) * 97) // 100


def liquidation_value(mark, tokens):
    """What selling `tokens` would pay, curve fees deducted, against the
    reserves the harness marks for the mint."""
    if mark is None:
        return 0
    vq = int(mark["virtual_quote_reserves"])
    vt = int(mark["virtual_token_reserves"])
    rq = int(mark["real_quote_reserves"])
    if vt + tokens == 0:
        return 0
    q = (tokens * vq) // (vt + tokens)
    if q > rq:
        q = rq
    return q - ceil_div(q * FEE_BPS, BPS)


class Book:
    """Per-coin tick history and position bookkeeping."""

    __slots__ = (
        "ticks", "entry_price", "entry_slot", "entry_high", "committed",
        "cool_until", "exiting", "strong",
    )

    def __init__(self):
        self.ticks = []  # (slot, price, real_sol, is_buy, sol), oldest first
        self.entry_price = 0.0
        self.entry_slot = -1
        self.entry_high = 0.0
        self.committed = 0
        self.cool_until = -1
        self.exiting = False
        self.strong = False


class Reversion:
    def __init__(self, params=None):
        self.p = dict(PARAMS)
        if params:
            self.p.update(params)
        self.books = {}  # mint -> Book, first-seen order

    def book(self, mint):
        b = self.books.get(mint)
        if b is None:
            b = Book()
            self.books[mint] = b
        return b

    def on_batch(self, state, batch):
        p = self.p
        slot = batch["slot"]
        touched = []
        for e in batch["events"]:
            if e["event"] != "TradeEvent":
                continue
            b = self.book(e["mint"])
            vt = int(e["virtual_token_reserves"])
            price = 0.0 if vt == 0 else int(e["virtual_quote_reserves"]) / vt
            b.ticks.append((slot, price, int(e["real_quote_reserves"]),
                            bool(e["is_buy"]), int(e["sol_amount"])))
            if len(b.ticks) > 400:
                del b.ticks[:len(b.ticks) - 400]
            if e["mint"] not in touched:
                touched.append(e["mint"])

        # A position whose sell resolved (or failed) may need another exit
        # attempt.
        for f in batch["fills"]:
            b = self.books.get(f["mint"])
            if b is None:
                continue
            if f["side"] == "sell":
                b.exiting = False
                if f["mint"] not in state.positions:
                    b.committed = 0
                    b.entry_slot = -1
                    b.cool_until = slot + p["cooldown_slots"]
            elif f["status"] != "filled" and f["mint"] not in state.positions:
                # The opening buy never filled: forget the entry.
                still_pending = False
                for o in state.pending.values():
                    if o.order["mint"] == f["mint"]:
                        still_pending = True
                        break
                if not still_pending:
                    b.committed = 0
                    b.entry_slot = -1

        orders = []
        cap = state.hello["caps"]["max_orders_per_batch"]
        tx = state.tx_fee_lamports()
        marks = {}
        for m in batch["marks"]:
            marks[m["mint"]] = m
        pending_mints = set()
        for o in state.pending.values():
            pending_mints.add(o.order["mint"])

        # 1. Exits and adds on held positions, in mint order (deterministic).
        held = sorted(state.positions.keys())
        for mint in held:
            if len(orders) >= cap:
                break
            h = state.positions[mint]
            b = self.book(mint)
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                continue
            if b.exiting:
                continue
            value = liquidation_value(marks.get(mint), h.token_amount)
            ticks = b.ticks
            price = ticks[-1][1] if ticks else 0.0
            entry_slot = b.entry_slot if b.entry_slot >= 0 else slot
            target = b.entry_price + p["recover_share"] * (b.entry_high - b.entry_price)
            gain = 0.0
            if h.cost_lamports > 0:
                gain = (value - h.cost_lamports) / h.cost_lamports
            tp = gain >= p["take_profit"] or (
                p["recover_share"] > 0 and price >= target and gain > 0.03)
            sl = gain <= -p["stop_loss"]
            stale = slot - entry_slot >= (
                p["max_hold_slots"] if b.strong else p["weak_max_hold_slots"])
            if tp or sl or stale:
                if mint in pending_mints:
                    continue
                n = max(1, min(p["sell_splits"], cap - len(orders)))
                piece = h.token_amount // n
                for i in range(1, n + 1):
                    amount = (h.token_amount - piece * (n - 1)) if i == n else piece
                    orders.append({"side": "sell", "mint": mint,
                                   "token_amount": amount,
                                   "max_slippage_bps": p["sell_slippage_bps"]})
                b.exiting = True
                continue
            # Add a tranche while the dip holds near the first entry.
            max_pos = p["max_position"] if b.strong else p["weak_max_position"]
            if mint in pending_mints or b.committed >= max_pos:
                continue
            if b.entry_price <= 0 or abs(price / b.entry_price - 1) > p["add_band"]:
                continue
            amount = max_pos - b.committed
            tranche = p["tranche"] if b.strong else p["weak_tranche"]
            if amount > tranche:
                amount = tranche
            size_cap = buy_cap(state, mint)
            if amount > size_cap:
                amount = size_cap
            if amount < p["min_buy"]:
                continue
            if state.free_cash_lamports() - amount - tx < 0:
                continue
            orders.append({"side": "buy", "mint": mint,
                           "quote_lamports": amount,
                           "max_slippage_bps": p["buy_slippage_bps"]})
            b.committed += amount
            pending_mints.add(mint)

        # 2. New entries on coins that traded this slot.
        entries = 0
        open_count = state.committed_position_count()
        weak_open = 0
        for m in state.positions.keys():
            if not self.book(m).strong:
                weak_open += 1
        for o in state.pending.values():
            if (o.order["side"] == "buy" and o.order["mint"] not in state.positions
                    and not self.book(o.order["mint"]).strong):
                weak_open += 1
        for mint in touched:
            if len(orders) >= cap or entries >= p["max_entries_per_batch"]:
                break
            if open_count >= state.hello["caps"]["max_open_positions"]:
                break
            if mint in state.positions or mint in pending_mints:
                continue
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                continue
            b = self.book(mint)
            if slot < b.cool_until:
                continue
            ticks = b.ticks
            if len(ticks) < p["flow_trades"]:
                continue
            last = ticks[-1]
            if last[2] < p["min_real_sol"]:
                continue
            # Recent high within the window.
            hi = 0.0
            hi_real = 0
            for i in range(len(ticks) - 1, -1, -1):
                t = ticks[i]
                if slot - t[0] > p["high_window"]:
                    break
                if t[1] > hi:
                    hi = t[1]
                    hi_real = t[2]
            if hi <= 0 or hi_real < p["min_high_real_sol"]:
                continue
            dd = 1 - last[1] / hi
            strong = last[2] >= p["strong_real_sol"]
            if strong:
                if dd < p["dd_min"] or dd > p["dd_max"]:
                    continue
            else:
                if dd < p["weak_dd_min"] or dd > p["weak_dd_max"]:
                    continue
            # Buyers still there.
            buys = 0
            net = 0
            for t in ticks[-p["flow_trades"]:]:
                if t[3]:
                    buys += 1
                    net += t[4]
                else:
                    net -= t[4]
            if buys < p["min_buys"] or net < p["min_net_flow"]:
                continue
            if p["last_must_be_buy"] and not last[3]:
                continue
            if not strong and weak_open >= p["max_weak_open"]:
                continue
            amount = p["tranche"] if strong else p["weak_tranche"]
            max_pos = p["max_position"] if strong else p["weak_max_position"]
            if amount > max_pos:
                amount = max_pos
            size_cap = buy_cap(state, mint)
            if amount > size_cap:
                amount = size_cap
            if amount < p["min_buy"]:
                continue
            if state.free_cash_lamports() - amount - tx < 0:
                continue
            orders.append({"side": "buy", "mint": mint,
                           "quote_lamports": amount,
                           "max_slippage_bps": p["buy_slippage_bps"]})
            b.entry_price = last[1]
            b.entry_slot = slot
            b.entry_high = hi
            b.committed = amount
            b.exiting = False
            b.strong = strong
            pending_mints.add(mint)
            entries += 1
            open_count += 1
            if not strong:
                weak_open += 1
        return orders


def main():
    strategy = Reversion()
    makefun_bot.run(strategy.on_batch)


if __name__ == "__main__":
    main()
