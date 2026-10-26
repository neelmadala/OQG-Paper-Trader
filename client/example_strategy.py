"""Example: daily SMA crossover on a small universe. A template, not a good strategy.

Run once per day (cron / scheduler):
    export PT_URL=http://your-server:8000 PT_API_KEY=pt_...
    python example_strategy.py

Needs: pip install requests yfinance
"""
import sys

import yfinance as yf

from papertrader import PaperTrader

UNIVERSE = ["SPY", "QQQ", "IWM"]
FAST, SLOW = 20, 50
WEIGHT = 0.25  # fraction of equity per name when its signal is on


def signal(symbol: str) -> bool:
    closes = yf.Ticker(symbol).history(period="6mo")["Close"]
    if len(closes) < SLOW:
        return False
    return closes.tail(FAST).mean() > closes.tail(SLOW).mean()


def main() -> int:
    pt = PaperTrader()
    equity = pt.portfolio()["equity"]
    for sym in UNIVERSE:
        price = pt.quote(sym)
        held = pt.position(sym)
        target = int(equity * WEIGHT / price) if signal(sym) else 0
        delta = target - held
        if delta > 0:
            fill = pt.buy(sym, delta)
        elif delta < 0:
            fill = pt.sell(sym, -delta)
        else:
            print(f"{sym}: holding {held}, no change")
            continue
        print(f"{sym}: {fill['side']} {fill['qty']} @ {fill['price']}")
    p = pt.portfolio()
    print(f"equity {p['equity']:.2f}  return {p['return_pct']:.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
