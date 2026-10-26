"""Tiny client for the OQG paper trader. Copy this one file next to your strategy.

    from papertrader import PaperTrader
    pt = PaperTrader()                      # reads PT_URL and PT_API_KEY from the environment
    pt.buy("AAPL", 10)
    print(pt.portfolio()["equity"])

    pt.join("K7QX2M9P")                     # join a competition with the code an admin gave you
    cup = PaperTrader(portfolio="Fall Cup") # trade in that competition's portfolio instead of your main one
    cup.buy("AAPL", 10)

Requires: pip install requests
"""
import os
import uuid

import requests


def option_symbol(underlying: str, expiry: str, right: str, strike: float) -> str:
    """Contract name for an option: option_symbol("SPY", "2026-12-18", "C", 450) -> "SPY261218C00450000".
    right is "C" (call) or "P" (put). Use it as the symbol in buy() and sell()."""
    y, m, d = expiry.split("-")
    return f"{underlying.upper()}{y[2:]}{m}{d}{right.upper()[0]}{int(round(strike * 1000)):08d}"


class PaperTraderError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class PaperTrader:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: float = 20.0,
                 portfolio: str | int | None = None):
        """portfolio: the name or id of the portfolio every call should use (default: your main one).
        Make one client per portfolio, or change it any time with use()."""
        self.base_url = (base_url or os.environ.get("PT_URL", "http://localhost:8000")).rstrip("/")
        self.api_key = api_key or os.environ.get("PT_API_KEY")
        if not self.api_key:
            raise PaperTraderError("no API key: pass api_key= or set PT_API_KEY")
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers["X-API-Key"] = self.api_key
        self.use(portfolio)

    def use(self, portfolio: str | int | None) -> "PaperTrader":
        """Switch which portfolio the following calls use (None = your main portfolio). Returns self."""
        self.portfolio_ref = None if portfolio is None else str(portfolio)
        if self.portfolio_ref:
            self._s.headers["X-Portfolio"] = self.portfolio_ref
        else:
            self._s.headers.pop("X-Portfolio", None)
        return self

    def _request(self, method: str, path: str, **kw):
        try:
            r = self._s.request(method, self.base_url + path, timeout=self.timeout, **kw)
        except requests.RequestException as exc:
            raise PaperTraderError(f"could not reach {self.base_url}: {exc}") from exc
        if not r.ok:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise PaperTraderError(f"{r.status_code}: {detail}", status=r.status_code)
        return r.json()

    # --- market data ---
    def quote(self, symbol: str) -> float:
        return self._request("GET", f"/api/quote/{symbol}")["price"]

    def option_quote(self, symbol: str) -> dict:
        """Bid, ask, last and mark (per share) for an option contract."""
        return self._request("GET", f"/api/quote/{symbol}")

    def expiries(self, underlying: str) -> list[str]:
        """Expiry dates (YYYY-MM-DD) that have options for this stock or ETF."""
        return self._request("GET", f"/api/options/{underlying}/expiries")["expiries"]

    def chain(self, underlying: str, expiry: str) -> dict:
        """{'underlying_price', 'calls': [...], 'puts': [...]}, each row has symbol, strike, bid, ask, last, ..."""
        return self._request("GET", f"/api/options/{underlying}", params={"expiry": expiry})

    # --- trading ---
    def order(
        self,
        symbol: str,
        side: str,
        qty: float,
        client_order_id: str | None = None,
        stop: float | None = None,
        limit: float | None = None,
    ) -> dict:
        """Order. Works for options too (symbol from option_symbol(); qty is whole contracts, price per share).
        You can buy options and sell ones you own, but not write them (no short or cover).
        Market by default; pass limit= for a limit order (buy/cover at that price or lower,
        sell/short at that price or higher). A limit order that can't fill yet comes back with
        status "open" and an id; the server fills it later, see open_orders() and cancel().
        Pass the same client_order_id when retrying so a network hiccup can never trade twice
        (a duplicate returns the original result).
        stop= (buy and short only, not with limit=) also sets a stop price on the resulting position."""
        body = {"symbol": symbol, "side": side, "qty": qty, "client_order_id": client_order_id or uuid.uuid4().hex}
        if stop is not None:
            body["stop_price"] = stop
        if limit is not None:
            body["limit_price"] = limit
        return self._request("POST", "/api/orders", json=body)

    def buy(self, symbol: str, qty: float, client_order_id: str | None = None, stop: float | None = None, limit: float | None = None) -> dict:
        """Buy. stop= is a per-share price below the market: if the price falls to it, the server sells the position.
        limit= makes it a limit order: buy at that price or lower."""
        return self.order(symbol, "buy", qty, client_order_id, stop, limit)

    def sell(self, symbol: str, qty: float, client_order_id: str | None = None, limit: float | None = None) -> dict:
        """Sell shares you own. Fails (instead of going short) if you hold fewer than qty.
        limit= makes it a limit order: sell at that price or higher."""
        return self.order(symbol, "sell", qty, client_order_id, None, limit)

    def short(self, symbol: str, qty: float, client_order_id: str | None = None, stop: float | None = None, limit: float | None = None) -> dict:
        """Sell borrowed shares. The money stays locked as collateral, plus extra margin from free cash.
        stop= is a per-share price above the market: if the price rises to it, the server covers the position.
        limit= makes it a limit order: short at that price or higher."""
        return self.order(symbol, "short", qty, client_order_id, stop, limit)

    def cover(self, symbol: str, qty: float, client_order_id: str | None = None, limit: float | None = None) -> dict:
        """Buy back shares you are short. limit= makes it a limit order: cover at that price or lower."""
        return self.order(symbol, "cover", qty, client_order_id, None, limit)

    def open_orders(self, status: str = "open") -> list[dict]:
        """Your limit orders. status="all" also lists recent filled, cancelled, expired and rejected ones."""
        return self._request("GET", "/api/open-orders", params={"status": status})

    def cancel(self, order_id: int) -> dict:
        """Cancel a resting limit order by the id returned when you placed it."""
        return self._request("DELETE", f"/api/open-orders/{order_id}")

    def corporate_actions(self) -> list[dict]:
        """Splits and dividends that were applied to your account."""
        return self._request("GET", "/api/corporate-actions")

    def set_stop(self, symbol: str, price: float) -> dict:
        """Set or change the stop on an open position. Checked about once a minute during market hours;
        it fills at the live price, not at the stop. Longs need a price below the market, shorts above."""
        return self._request("PUT", f"/api/positions/{symbol}/stop", json={"stop_price": price})

    def clear_stop(self, symbol: str) -> dict:
        return self._request("PUT", f"/api/positions/{symbol}/stop", json={"stop_price": None})

    # --- account ---
    def portfolio(self) -> dict:
        return self._request("GET", "/api/portfolio")

    def position(self, symbol: str) -> float:
        """Shares currently held: positive for long, negative for short, 0 if none."""
        symbol = symbol.upper()
        for p in self.portfolio()["positions"]:
            if p["symbol"] == symbol:
                return p["qty"]
        return 0.0

    def cash(self) -> float:
        return self.portfolio()["cash"]

    def orders(self, limit: int = 100) -> list[dict]:
        return self._request("GET", "/api/orders", params={"limit": limit})

    def equity_curve(self) -> list[dict]:
        return self._request("GET", "/api/equity")

    def leaderboard(self) -> list[dict]:
        return self._request("GET", "/api/leaderboard")

    # --- portfolios and competitions ---
    def portfolios(self) -> list[dict]:
        """All your portfolios (main, extra and competition ones) with equity and return."""
        return self._request("GET", "/api/portfolios")

    def create_portfolio(self, name: str) -> dict:
        """Make an extra portfolio with the normal starting cash (a practice account, a second strategy...)."""
        return self._request("POST", "/api/portfolios", json={"name": name})

    def delete_portfolio(self, name: str | int) -> None:
        """Delete one of your extra portfolios. The main and competition portfolios can't be deleted."""
        self._request("DELETE", f"/api/portfolios/{name}")

    def competitions(self) -> list[dict]:
        """The competitions you are in, with your portfolio, rank and return."""
        return self._request("GET", "/api/competitions")

    def join(self, code: str) -> dict:
        """Join a competition with its join code. You get a fresh portfolio named after it."""
        return self._request("POST", "/api/competitions/join", json={"code": code})

    def competition_leaderboard(self, competition_id: int) -> list[dict]:
        return self._request("GET", f"/api/competitions/{competition_id}/leaderboard")
