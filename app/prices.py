import hashlib
import math
import random
import re
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

SYMBOL_RE = re.compile(r"^[A-Z0-9^][A-Z0-9.\-=^]{0,20}$")  # up to 21 chars: option contracts are long
OPTION_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")  # OCC format, e.g. AAPL261218C00180000
OPTION_MULTIPLIER = 100  # one contract covers 100 shares


class PriceError(Exception):
    def __init__(self, message: str, status: int = 404):
        super().__init__(message)
        self.status = status


def parse_option(symbol: str) -> dict | None:
    """AAPL261218C00180000 -> {'underlying': 'AAPL', 'expiry': '2026-12-18', 'right': 'C', 'strike': 180.0}.
    Returns None for anything that isn't an option contract symbol."""
    m = OPTION_RE.match(symbol or "")
    if not m:
        return None
    root, ymd, right, k = m.groups()
    try:
        d = datetime.strptime(ymd, "%y%m%d").date()
    except ValueError:
        return None
    return {"symbol": symbol, "underlying": root, "expiry": d.isoformat(), "right": right, "strike": int(k) / 1000}


def is_option(symbol: str) -> bool:
    return parse_option(symbol) is not None


def option_symbol(underlying: str, expiry: str, right: str, strike: float) -> str:
    """option_symbol('AAPL', '2026-12-18', 'C', 180) -> 'AAPL261218C00180000'"""
    d = datetime.strptime(expiry, "%Y-%m-%d")
    return f"{underlying.upper()}{d:%y%m%d}{right.upper()[0]}{int(round(strike * 1000)):08d}"


def normalize_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if not SYMBOL_RE.match(s):
        raise PriceError(f"invalid symbol: {symbol!r}", status=400)
    if OPTION_RE.match(s) and not parse_option(s):  # right shape, impossible date
        raise PriceError(f"invalid option symbol: {symbol!r}", status=400)
    return s


def _num(x) -> float:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    return x if x == x and x > 0 else 0.0  # NaN and negatives become 0


def _bs_price(spot: float, strike: float, years: float, vol: float, right: str, rate: float = 0.04) -> float:
    """Black-Scholes, used only for the offline mock provider."""
    intrinsic = max(spot - strike, 0.0) if right == "C" else max(strike - spot, 0.0)
    if years <= 0:
        return intrinsic
    cdf = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
    d1 = (math.log(spot / strike) + (rate + vol * vol / 2) * years) / (vol * math.sqrt(years))
    d2 = d1 - vol * math.sqrt(years)
    if right == "C":
        return spot * cdf(d1) - strike * math.exp(-rate * years) * cdf(d2)
    return strike * math.exp(-rate * years) * cdf(-d2) - spot * cdf(-d1)


class MockProvider:
    """Deterministic fake prices for tests and offline demos.
    set_price() pins a symbol; otherwise the price wobbles by minute."""

    def __init__(self):
        self.overrides: dict[str, float] = {}
        self.actions: dict[str, dict] = {}
        self.option_quotes: dict[str, dict] = {}
        self.closes: dict[tuple[str, str], float] = {}

    def set_price(self, symbol: str, price: float) -> None:
        self.overrides[symbol.upper()] = float(price)

    def set_actions(self, symbol: str, splits=(), dividends=()) -> None:
        """Pretend the data feed reports these: splits as (YYYY-MM-DD, ratio), dividends as (YYYY-MM-DD, per share)."""
        self.actions[symbol.upper()] = {"splits": list(splits), "dividends": list(dividends)}

    def get_actions(self, symbol: str) -> dict:
        return self.actions.get(symbol, {"splits": [], "dividends": []})

    # --- options (priced with Black-Scholes unless pinned) ---
    def set_option_quote(self, symbol: str, bid: float, ask: float, last: float | None = None) -> None:
        self.option_quotes[symbol.upper()] = {"bid": bid, "ask": ask, "last": last if last is not None else (bid + ask) / 2}

    def set_close(self, symbol: str, day: str, price: float) -> None:
        self.closes[(symbol.upper(), day)] = float(price)

    def get_close(self, symbol: str, day: str):
        return self.closes.get((symbol, day), self.get_price(symbol))

    def get_option_quote(self, symbol: str) -> dict:
        if symbol in self.option_quotes:
            return dict(self.option_quotes[symbol])
        o = parse_option(symbol)
        days = (date.fromisoformat(o["expiry"]) - date.today()).days
        if days < 0:
            raise PriceError(f"{symbol} has expired", status=404)
        mid = _bs_price(self.get_price(o["underlying"]), o["strike"], days / 365, 0.3, o["right"])
        spread = max(0.02, mid * 0.04)
        return {"bid": round(max(mid - spread / 2, 0), 2), "ask": round(mid + spread / 2, 2), "last": round(mid, 2)}

    def get_expiries(self, underlying: str) -> list[str]:
        d, out = date.today(), []
        while len(out) < 4:
            d += timedelta(days=1)
            if d.weekday() == 4:  # Fridays
                out.append(d.isoformat())
        return out

    def get_chain(self, underlying: str, expiry: str) -> dict:
        if expiry not in self.get_expiries(underlying):
            raise PriceError(f"no options for {underlying} expiring {expiry}", status=404)
        spot = self.get_price(underlying)
        step = 1 if spot < 50 else 5 if spot < 200 else 10
        atm = round(spot / step) * step
        out = {"calls": [], "puts": []}
        for k in range(-12, 13):
            strike = atm + k * step
            if strike <= 0:
                continue
            for right, key in (("C", "calls"), ("P", "puts")):
                sym = option_symbol(underlying, expiry, right, strike)
                q = self.get_option_quote(sym)
                itm = spot > strike if right == "C" else spot < strike
                out[key].append({"symbol": sym, "strike": float(strike), "bid": q["bid"], "ask": q["ask"], "last": q["last"],
                                 "volume": 0, "open_interest": 0, "implied_vol": 0.3, "in_the_money": itm})
        return out

    def get_price(self, symbol: str) -> float:
        if symbol in self.overrides:
            return self.overrides[symbol]
        base = 20 + (int(hashlib.md5(symbol.encode()).hexdigest()[:6], 16) % 48000) / 100
        rnd = random.Random(f"{symbol}{int(time.time() // 60)}")
        return round(base * (1 + rnd.uniform(-0.01, 0.01)), 2)


def _series_to_list(series) -> list[tuple[str, float]]:
    out = []
    for ts, v in series.items():
        v = float(v)
        if v == v and v > 0:  # drop NaN and zeros
            out.append((ts.strftime("%Y-%m-%d"), v))
    return out


class YFinanceProvider:
    def get_actions(self, symbol: str) -> dict:
        """Splits (ratio: 4.0 for 4-for-1, 0.1 for a 1-for-10 reverse split) and cash dividends per share.
        Raises PriceError if the feed can't be reached, so the caller retries later."""
        try:
            import yfinance as yf

            t = yf.Ticker(symbol)
            return {"splits": _series_to_list(t.splits), "dividends": _series_to_list(t.dividends)}
        except Exception as exc:
            raise PriceError(f"corporate actions unavailable for {symbol}: {exc}", status=502)

    def __init__(self):
        self._chains: dict[tuple[str, str], tuple[float, object]] = {}
        self._chain_lock = threading.Lock()

    def _chain(self, underlying: str, expiry: str):
        """One option-chain download per underlying and expiry, shared for 15 seconds."""
        key = (underlying, expiry)
        with self._chain_lock:
            hit = self._chains.get(key)
            if hit and time.time() - hit[0] < 15:
                return hit[1]
            try:
                import yfinance as yf

                chain = yf.Ticker(underlying).option_chain(expiry)
            except ValueError:
                raise PriceError(f"no options for {underlying} expiring {expiry}", status=404)
            except Exception as exc:
                raise PriceError(f"option data unavailable for {underlying}: {exc}", status=502)
            self._chains[key] = (time.time(), chain)
            return chain

    @staticmethod
    def _rows(df) -> list[dict]:
        rows = []
        for r in df.to_dict("records"):
            rows.append({
                "symbol": r.get("contractSymbol"), "strike": float(r.get("strike") or 0),
                "bid": _num(r.get("bid")), "ask": _num(r.get("ask")), "last": _num(r.get("lastPrice")),
                "volume": int(_num(r.get("volume"))), "open_interest": int(_num(r.get("openInterest"))),
                "implied_vol": round(_num(r.get("impliedVolatility")), 4), "in_the_money": bool(r.get("inTheMoney")),
            })
        return rows

    def get_option_quote(self, symbol: str) -> dict:
        o = parse_option(symbol)
        chain = self._chain(o["underlying"], o["expiry"])
        for row in self._rows(chain.calls if o["right"] == "C" else chain.puts):
            if row["symbol"] == symbol:
                return {"bid": row["bid"], "ask": row["ask"], "last": row["last"]}
        raise PriceError(f"no such contract: {symbol}", status=404)

    def get_expiries(self, underlying: str) -> list[str]:
        try:
            import yfinance as yf

            exp = list(yf.Ticker(underlying).options)
        except Exception as exc:
            raise PriceError(f"option data unavailable for {underlying}: {exc}", status=502)
        if not exp:
            raise PriceError(f"no options listed for {underlying}", status=404)
        return exp

    def get_chain(self, underlying: str, expiry: str) -> dict:
        chain = self._chain(underlying, expiry)
        return {"calls": self._rows(chain.calls), "puts": self._rows(chain.puts)}

    def get_close(self, symbol: str, day: str):
        """Closing price of that trading day, or None if it isn't available yet."""
        try:
            import yfinance as yf

            d = date.fromisoformat(day)
            hist = yf.Ticker(symbol).history(start=day, end=(d + timedelta(days=1)).isoformat())
            if hist.empty:
                return None
            return float(hist["Close"].iloc[-1])
        except Exception:
            return None

    def get_price(self, symbol: str) -> float:
        try:
            import yfinance as yf

            t = yf.Ticker(symbol)
            px = None
            try:
                px = t.fast_info["last_price"]
            except Exception:
                px = None
            if not px or px != px or px <= 0:  # None, 0, NaN
                hist = t.history(period="1d")
                if hist.empty:
                    raise PriceError(f"no price available for {symbol}", status=404)
                px = float(hist["Close"].iloc[-1])
            return float(px)
        except PriceError:
            raise
        except Exception as exc:
            raise PriceError(f"price provider error for {symbol}: {exc}", status=502)


class PriceService:
    """Caches quotes so the whole club shares one provider call per symbol per TTL.
    Falls back to the last known price when the provider is down (valuation only)."""

    def __init__(self, provider, ttl: float):
        self.provider = provider
        self.ttl = ttl
        self._cache: dict[str, tuple[float, float]] = {}
        self._qcache: dict[str, tuple[float, dict]] = {}  # option quotes: bid, ask, last, mark
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._guard = threading.Lock()

    def _lock_for(self, symbol: str) -> threading.Lock:
        with self._guard:
            return self._locks[symbol]

    def get_quote(self, symbol: str) -> dict:
        """Bid, ask, last and mark for an option; for a stock just its price as the mark.
        mark = the middle of a two-sided market, otherwise the last trade. Raises PriceError without a price."""
        if not is_option(symbol):
            px = self.get(symbol)
            return {"mark": px, "bid": None, "ask": None, "last": px}
        hit = self._qcache.get(symbol)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        with self._lock_for(symbol):
            hit = self._qcache.get(symbol)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
            fn = getattr(self.provider, "get_option_quote", None)
            if fn is None:
                raise PriceError("this price provider has no option data", status=501)
            raw = fn(symbol)
            bid, ask, last = _num(raw.get("bid")), _num(raw.get("ask")), _num(raw.get("last"))
            if bid > 0 and ask > 0:
                mark = (bid + ask) / 2
            elif last > 0:
                mark = last
            else:
                raise PriceError(f"no price available for {symbol} right now", status=404)
            q = {"bid": bid, "ask": ask, "last": last, "mark": mark}
            self._qcache[symbol] = (time.time(), q)
            self._cache[symbol] = (time.time(), mark)  # so valuation can fall back to it
            return q

    def get_expiries(self, underlying: str) -> list[str]:
        fn = getattr(self.provider, "get_expiries", None)
        if fn is None:
            raise PriceError("this price provider has no option data", status=501)
        return fn(underlying)

    def get_chain(self, underlying: str, expiry: str) -> dict:
        fn = getattr(self.provider, "get_chain", None)
        if fn is None:
            raise PriceError("this price provider has no option data", status=501)
        return fn(underlying, expiry)

    def get_close(self, symbol: str, day: str):
        """Closing price on a past trading day, or None if unavailable (try again later)."""
        fn = getattr(self.provider, "get_close", None)
        if fn is None:
            return None
        try:
            return fn(symbol, day)
        except Exception:
            return None

    def get(self, symbol: str) -> float:
        """Fresh price for trading. Raises PriceError if it can't get one."""
        if is_option(symbol):
            return self.get_quote(symbol)["mark"]
        hit = self._cache.get(symbol)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        with self._lock_for(symbol):
            hit = self._cache.get(symbol)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
            px = self.provider.get_price(symbol)
            self._cache[symbol] = (time.time(), px)
            return px

    def get_actions(self, symbol: str):
        """Splits and dividends for a symbol, or None if the feed failed (try again later)."""
        fn = getattr(self.provider, "get_actions", None)
        if fn is None:
            return None
        try:
            return fn(symbol)
        except Exception:
            return None

    def get_many(self, symbols) -> dict[str, float | None]:
        """Prices for valuation. Uses stale values on failure, None if never seen."""
        out: dict[str, float | None] = {}
        for s in set(symbols):
            try:
                out[s] = self.get(s)
            except PriceError:
                hit = self._cache.get(s)
                out[s] = hit[1] if hit else None
        return out

