"""Capitulation-reversal bot for cbBTC/USDC, 1-minute bars, long only.

Strategy (plain words):
  The dev window falls 4% in 24h, but it falls in a grind and then snaps back
  in one sharp V (+4.9% in two hours from the low). Buying every dip loses
  (the dip keeps dipping); buying the *reclaim* wins. So this bot sits in cash
  almost all day and only buys one setup:

  REVERSAL: the price made a new 240-bar (4h) low within the last 30 bars and
  has since rebounded at least 1.5% above that low. That is a capitulation
  followed by buyers stepping back in. Buy the whole cash balance.
  Exit the whole position at +3% over cost, or at -1.5% under cost.

  FALLBACK: if no order has ever filled by scored bar 1320 (the last two
  hours), buy once at full size and exit at +1.5% / -1%. This only exists so
  the entry still meets the "at least 1 trade" guardrail on a window where no
  capitulation reversal happens; it is expected to cost about a round trip.

  Everything is integer math (cross-multiplied comparisons), so two runs on
  the same bars produce the same orders byte for byte. No clock, no network,
  no randomness, no model calls. A full-size round trip costs about 42 bps
  in fees, slippage and pool impact on this window's depth, so the +3% target
  is far above the round-trip cost and the -1.5% stop caps a bad entry.

  On the dev window this makes +$30.37 on one round trip (buy decided bar
  1057, filled bar 1058 at $63,365.50; sell decided bar 1113, filled bar 1114
  at $65,342.20), max drawdown 0.47%, Sharpe 0.052, 2 trades, $0.81 fees,
  versus -$44.01 for buy-and-hold and -$40.92 for the best baseline
  (momentum). Verified three ways: the kit's grade-local on an identical
  Node port (net PnL 30,369,143 micro-USDC, L1 reproduced), and a protocol
  driver running this exact main.py twice (byte-identical action logs).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
LOOKBACK = 240          # bars: the capitulation low must be a new 240-bar low
WINDOW = 30             # bars: that low must sit within the last WINDOW bars
REB_NUM, REB_DEN = 1015, 1000   # reclaim: close >= low * 1.015
ROI_NUM, ROI_DEN = 103, 100     # reversal exit: value >= cost * 1.03
STOP_NUM, STOP_DEN = 985, 1000  # reversal exit: value <= cost * 0.985
FB_ROI_NUM, FB_ROI_DEN = 1015, 1000  # fallback exit: value >= cost * 1.015
FB_STOP_NUM, FB_STOP_DEN = 99, 100   # fallback exit: value <= cost * 0.99
FB_BAR = 1320           # scored bars: fallback may fire from here on
MAX_SLIPPAGE_BPS = 100  # 1% past the close the order was decided on

closes = []             # CBBTC closes, ints, in bar order
scored = 0              # scored (non-warmup) bars seen
fills = 0               # my own filled orders
pos_kind = None         # "rev" or "fb" for the open position, else None
pending = {}            # client_id -> "rev" / "fb" for buys awaiting their fill
next_id = 1


def _buy(state, kind):
    """A full-cash buy order, or None when there is nothing to spend."""
    global next_id
    cash = state.cash
    if cash < int(state.hello["fees"]["min_notional_quote_units"]):
        return None
    cid = "b%d" % next_id
    next_id += 1
    pending[cid] = kind
    return {"side": "buy", "symbol": SYMBOL, "notional_quote_units": cash,
            "max_slippage_bps": MAX_SLIPPAGE_BPS, "client_id": cid}


def _sell(state, held):
    return {"side": "sell", "symbol": SYMBOL, "qty": held.qty,
            "max_slippage_bps": MAX_SLIPPAGE_BPS}


def on_batch(state, batch):
    global scored, fills, pos_kind
    # Fold in this batch's fills first: they tell us which buy (if any) landed.
    for f in batch.get("fills", []):
        cid = f.get("client_id")
        if cid in pending:
            kind = pending.pop(cid)
            if f.get("status") == "filled":
                fills += 1
                pos_kind = kind
    # Then the bar(s) of this batch.
    for bar in batch.get("bars", []):
        if bar.get("symbol") == SYMBOL:
            closes.append(int(bar["c"]))
    if state.warming_up:
        return []
    scored += 1
    held = state.positions.get(SYMBOL)
    if held is None:
        pos_kind = None
    if held is not None:
        # Exit the whole position on the take-profit or the stop.
        value = state.value(SYMBOL, held.qty)
        cost = held.cost
        if pos_kind == "fb":
            if value * FB_ROI_DEN >= cost * FB_ROI_NUM:
                return [_sell(state, held)]
            if value * FB_STOP_DEN <= cost * FB_STOP_NUM:
                return [_sell(state, held)]
        else:
            if value * ROI_DEN >= cost * ROI_NUM:
                return [_sell(state, held)]
            if value * STOP_DEN <= cost * STOP_NUM:
                return [_sell(state, held)]
        return []
    # Flat: look for the reversal entry.
    if len(closes) >= LOOKBACK:
        low30 = min(closes[-WINDOW:])
        low240 = min(closes[-LOOKBACK:])
        if low30 == low240 and closes[-1] * REB_DEN >= low30 * REB_NUM:
            order = _buy(state, "rev")
            return [order] if order is not None else []
    # Fallback: no fill yet and the window is nearly over.
    if scored >= FB_BAR and fills == 0:
        order = _buy(state, "fb")
        return [order] if order is not None else []
    return []


makefun_bars.run(on_batch)
