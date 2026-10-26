# OQG Paper Trader

REST API for paper trading, built for scripted strategies. Members run Python scripts that send orders over HTTP.
A user panel shows each member's portfolio and a leaderboard; an admin panel compares every portfolio.

Stack: FastAPI, SQLite (WAL), vanilla JS + Chart.js. Prices come from yfinance, cached so the whole club shares one lookup per symbol every 15 seconds.

## Run it

```bash
cp .env.example .env        # set PT_ADMIN_TOKEN and PT_INVITE_CODE
docker compose up --build   # http://localhost:8000
```

Without Docker:

```bash
pip install -r requirements.txt
export PT_ADMIN_TOKEN=... PT_INVITE_CODE=...
uvicorn app.main:get_app --factory --port 8000
```

Offline demo with fake prices: `PT_PRICE_PROVIDER=mock`.

- User panel: `/` (paste your API key)
- Admin panel: `/admin` (paste the admin token)
- Interactive API docs: `/docs`

## Getting an account

**Sign-up codes (recommended).** In the admin panel, open **Users** and scroll to "Make sign-up codes" and make one code per member (a code works once by
default; you can also set a use limit and an expiry, label them, and revoke unused ones). A member opens the Trade page,
types a username and their code under "Create your account", and gets their API key. The same works from a script:

```bash
curl -X POST localhost:8000/api/register -H 'Content-Type: application/json' \
  -d '{"username": "alice", "invite_code": "ABCD-EFGH-JKMN"}'
```

A code is only used up when the account is actually created, so a taken username doesn't burn it. Codes ignore case, spaces
and dashes. Admins can also create accounts directly (admin **Overview**, "Create account").

**Club-wide code (optional).** `PT_INVITE_CODE` is a single shared code anyone can use any number of times, which is what
lets one person make many accounts. Leave it empty (or delete the line) to turn it off and rely on single-use codes only.

The API key (`pt_...`) is shown once and only its hash is stored. Members can make a new key any time on the Competitions page (the old one stops working; or `POST /api/rotate-key`). If a key is lost, an admin can issue a new one from the Users page.

## Trading from a script

Copy `client/papertrader.py` next to your strategy (needs `pip install requests`).

```python
from papertrader import PaperTrader   # reads PT_URL and PT_API_KEY

pt = PaperTrader()
pt.buy("AAPL", 10)
pt.sell("AAPL", 4)
pt.short("TSLA", 10)   # negative position until you pt.cover("TSLA", 10)
pt.buy("NVDA", 5, stop=120.0)   # server sells the position if the price falls to 120
pt.set_stop("NVDA", 125.0)      # change it later; pt.clear_stop("NVDA") removes it
call = option_symbol("SPY", pt.expiries("SPY")[0], "C", 450)   # from papertrader import option_symbol
pt.buy(call, 2)                       # 2 contracts = 200 shares of exposure, paid at the ask
o = pt.buy("AAPL", 10, limit=180.0)   # limit order: buy at 180 or lower
if o["status"] == "open":             # not filled yet: the server fills it later
    pt.cancel(o["id"])                # or cancel it; pt.open_orders() lists them
print(pt.portfolio()["equity"], pt.position("AAPL"))
```

Or plain HTTP, header `X-API-Key`:

| Method | Path | Notes |
|---|---|---|
| GET | `/api/quote/{symbol}` | latest price |
| POST | `/api/orders` | `{"symbol","side":"buy"\|"sell"\|"short"\|"cover","qty","client_order_id"?,"stop_price"?,"limit_price"?}` (`limit_price` makes it a limit order) |
| GET | `/api/orders?limit=100` | your fills, newest first |
| GET | `/api/options/{underlying}/expiries` | expiry dates that have options |
| GET | `/api/options/{underlying}?expiry=YYYY-MM-DD` | the calls and puts for that date: symbol, strike, bid, ask, last, IV |
| GET | `/api/open-orders?status=open\|all` | your limit orders (`all` adds recent filled, cancelled, expired and rejected) |
| DELETE | `/api/open-orders/{id}` | cancel a resting limit order |
| GET | `/api/corporate-actions` | splits and dividends applied to your account |
| PUT | `/api/positions/{symbol}/stop` | `{"stop_price": 120}` sets or changes a stop, `{"stop_price": null}` clears it |
| GET | `/api/portfolio` | cash, positions, equity, return, drawdown |
| GET | `/api/equity` | equity curve snapshots; optional `?since=` and `?until=` (unix seconds) |
| GET | `/api/leaderboard` | everyone, ranked by return |

Admin endpoints use header `X-Admin-Token`: `GET /api/admin/portfolios`, `GET /api/admin/curves` (also takes `since`/`until`), `POST /api/admin/users`, `POST /api/admin/users/{id}/reset`, `POST /api/admin/corporate-actions` (enter a split or dividend by hand, see below) and `POST /api/admin/corporate-actions/check` (ask the data feed now).

`examples`: `client/example_strategy.py` is an SMA crossover template.

## Pages

Trader pages (sign in once with your API key, the portfolio you pick follows you between pages):
**Trade** (`/`: ticket, options chain, positions and stops, limit orders), **Activity** (`/activity`: equity curve,
order history, splits and dividends) and **Competitions** (`/competitions`: your portfolios, join with a code,
leaderboards). Admin pages (admin token): **Overview** (`/admin`: return chart, portfolios table, create accounts) and
**Admin roles:** `PT_ADMIN_TOKEN` is the owner key. The owner can make any member an admin (Users page, "Make admin"). Admins sign in to the admin pages with their own API key, can manage members, competitions and codes, but can't make or remove admins or touch other admin accounts; only the owner can. Every admin action is logged on the Users page. Tip: keep admin accounts separate from the accounts whose keys sit in trading scripts.

**Users** (`/admin/users`: every account with last trade, portfolios and competitions; search, sort, reset, new key, make admin, delete; plus sign-up codes and the admin activity log), **Competitions** (`/admin/competitions`: create, add members, end, delete). Admin pages have their own menu (Overview, Users, Competitions); "Go to User Menu" and "Go to Admin Menu" in the top right switch between the two sides. The trader pages have a collapsible sidebar listing your portfolios.

## Portfolios and competitions

Money lives in **portfolios**, not users. Every member has a **Main** portfolio, can create extra personal ones
(practice account, second strategy; up to `PT_MAX_PORTFOLIOS`, default 10), and gets a fresh one for each competition joined.
Each portfolio has its own cash, positions, orders, stops, limit orders, dividends and equity curve. The main
leaderboard ranks Main portfolios only.

- **Picking a portfolio:** every API call takes `?portfolio=<name or id>`, an `X-Portfolio` header, or (for orders) a
  `"portfolio"` field in the body. Leave it out and you get your Main portfolio, so existing scripts keep working.
  In Python: `PaperTrader(portfolio="Fall Cup")`, or `pt.use("Practice")`.
- **Competitions** (admin panel, or `POST /api/admin/competitions`): name, starting cash, optional open and close times,
  and rules (options on/off, short selling on/off). Each one has an 8-character join code.
  Members join with `POST /api/competitions/join {"code": ...}` (or the box on the Trade page), or an admin adds
  them by username. Trading is closed before the open and after the close. Closing (or **End now**) freezes every result:
  final equity is recorded, resting limit orders are cancelled, stops stop running and the equity curve ends.
  The final ranking is what the leaderboard keeps showing.
- Members see only competitions they are in (`GET /api/competitions`, `/api/competitions/{id}/leaderboard`).
  Admins can view any competition, all Main portfolios, or every portfolio (`?scope=main|all|<id>` on the admin endpoints),
  and can reset or remove a portfolio.
- Upgrading: an existing database is converted on first start. Each old account becomes a user with one "Main"
  portfolio (same ids, cash, history). Back up the `.db` file first anyway.

## How fills work

- Market orders only, filled immediately at the latest cached price.
- Four sides: `buy` and `sell` trade longs, `short` sells borrowed shares and `cover` buys them back. `sell` never opens a short, so a script that sells more than it holds gets an error instead of a surprise position.
- An order never flips a position through zero. To go from long to short, `sell` first and then `short`.
- Shorts are backed by cash only. Short proceeds stay in your cash balance but count as locked collateral, and after any order your cash must cover `(1 + PT_SHORT_MARGIN)` times the value of your shorts (default margin 0.5). What is left is "free cash", which is all you can spend on buys, so you can't use short proceeds to buy more stock. Opening a short needs `PT_SHORT_MARGIN` times its value in free cash, so with 100k and the default you can short about 200k of stock.
- Covering is always allowed, even when you're under water. If prices move against a short and cash falls below `1 + PT_SHORT_MAINT_MARGIN` times short value, `margin_call` turns true in `/api/portfolio` and new risk is rejected.
- Auto-liquidation (`PT_AUTO_LIQUIDATE=1`, off by default): when `margin_call` is true, the one-minute check covers your largest short, only as many shares as it takes to get free cash back to zero, at the live price with slippage and fees. These show in the orders table as `cover` with a Forced tag (`forced: true` in the API). It needs the timed check, so it runs only while snapshots run (market hours by default, never if `PT_SNAPSHOT_SECONDS=0`), and it skips an account when no fresh price is available. A fast gap can still push equity negative before the next check. The maintenance level is capped at `PT_SHORT_MARGIN`.
- Stops: each open position can have one per-share stop price. A long's stop must be below the market and a short's above it. When the price falls to a long's stop (or rises to a short's), the app sells (or covers) the whole position. Set it with `stop_price` on a `buy` or `short` order, or with `PUT /api/positions/{symbol}/stop`; it shows in `/api/portfolio` as `stop_price`. Adding to a position keeps its stop, and closing the position removes it. Stop fills are in the orders table with a Stop tag (`forced: true`, `reason: "stop"`).
- Stops are checked once a minute, only while the timed check runs (market hours by default, never if `PT_SNAPSHOT_SECONDS=0`), and only on a fresh price. They fill at the live price with normal slippage and fees, not at the stop price, so a gap or fast move can fill well past it. That is a market-style stop, and it is also why it's not a guarantee.
- Limit orders: add `limit_price` to any order. A buy or cover fills at that price or lower, a sell or short at that price or higher. If the market is already there it fills immediately and pays the usual slippage, but never a price worse than your limit. If not, the response has `"status": "open"` and an `id`, and the one-minute check fills it when the live price reaches your limit. A resting fill happens at the live price (which is at or better than the limit) with no slippage, since you were the one waiting. The order shows in the orders table with a Limit tag. Details:
  - Checked about once a minute, only while the timed check runs (market hours by default) and only on a fresh price. A quick dip between checks can be missed.
  - Cash, shares and margin are not set aside while an order rests. They are checked when it fills, and if they are gone it ends as `rejected` with the reason (see `/api/open-orders?status=all`).
  - Obviously impossible orders (selling shares you don't have, buying more than your cash at the limit price) are refused when you place them.
  - Orders expire after `PT_LIMIT_EXPIRY_DAYS` (30) and you can have `PT_MAX_OPEN_LIMITS` (50) resting at once.
  - A `client_order_id` makes retries safe here too. `stop_price` can't be combined with a limit order: set the stop after it fills.
- Options (long only): trade an option contract by its symbol, like `SPY261218C00450000` (SPY, expires 2026-12-18, call, strike 450). `option_symbol()` in the client builds it, or use the Options tab in the trade box on the user page: load a stock, pick an expiry, switch between Calls and Puts, and click a row to pick the contract. The stock's price appears as you type its ticker, and each row shows its break-even (strike plus the ask for a call, strike minus the ask for a put) and how far the stock must move to reach it. The Futures tab is a placeholder for now. Quantity is whole contracts; prices are per share and one contract is 100 shares, so a $3.10 option costs $310. You can `buy` (to open) and `sell` (to close). `short` and `cover` are refused, so there is no writing and no way to lose more than you paid. Everything else works on options too: limit orders, stop prices, the equity curve and the leaderboard.
  - Fills: buys pay the ask and sells get the bid, because the spread is a real cost. Slippage is not added on top. When there is no two-sided market (the feed shows zeros after hours) it uses the last trade price plus your usual slippage. A contract with an offer but no bid sells for $0.01.
  - Value: an open contract is valued at the middle of the bid and ask (the last trade if there is no market), so you start slightly under water by about half the spread.
  - Limit orders compare with the price you would actually trade at: a buy limit fills when the ask reaches it, a sell limit when the bid does.
  - Expiry: options stop trading at 4pm New York on the expiry date. About 30 minutes after, the server cash-settles what you still hold, using the underlying's closing price that day: a call pays `max(close - strike, 0)` per share, a put `max(strike - close, 0)`, to your cash with no shares changing hands. It shows in orders as a sell tagged Expired and counts in realized P&L and win rate. If the closing price isn't available yet it retries every few minutes. Resting limit orders on an expired contract are cancelled.
  - Data: Yahoo's option quotes are delayed and often stale, especially for thinly traded contracts. `PT_ALLOW_OPTIONS=0` turns options off.
  - Not handled: early exercise (there is none, only European-style cash settlement), and adjustments to contracts when the underlying splits or pays a special dividend. Options are left out of the split and dividend feed, so check those by hand.
- Indices (symbols starting with `^`, like `^GSPC`) have quotes but can't be traded. Use an ETF that tracks them (SPY, QQQ, DIA).
- Splits and dividends: the server asks the data feed (yfinance) about every stock people hold or traded in the last 10 days, once an hour (`PT_ACTIONS_CHECK_SECONDS`, 0 turns it off), at any hour so a morning split is in before the open. Each action is applied once.
  - Who gets it comes from each account's own order history: you are entitled if you held the shares before the ex-date (midnight New York time). So it is right even if the check runs late, and a person who sold afterwards still gets the dividend.
  - Dividend: cash is added to your balance when the action is applied (around the ex-date, not the later pay date). If you were short, the same amount is taken out, as a real short owes it. Special dividends are paid too. No taxes, no reinvestment.
  - Split: your share count, your average cost (so your total cost stays the same), your stop price and your resting limit orders are all adjusted, so nothing changes in value. Reverse splits work the same way and can leave fractional shares. Splits that happened before you bought are ignored, as are positions opened after the ex-date.
  - What you see: a Splits and dividends card on the user page, and `GET /api/corporate-actions`.
  - Yahoo's data is sometimes late or wrong. If it misses one (or you have mergers, spin-offs or anything else unusual), an admin can enter it: `curl -X POST localhost:8000/api/admin/corporate-actions -H 'X-Admin-Token: ...' -H 'Content-Type: application/json' -d '{"symbol":"NVDA","kind":"split","ex_date":"2024-06-10","value":10}'`. For a split `value` is the ratio (10 = 10-for-1, 0.1 = 1-for-10), for a dividend it is dollars per share as the feed shows it today (after any later splits). A date that was already applied returns 409.
  - Until the check runs on an ex-date, a split briefly looks like a big loss on the chart. Enter it by hand to fix that sooner.
  - If you traded the same stock after a split but before the check ran, your average cost can be a little off. It is the one approximation here.
- `PT_ALLOW_SHORTING=0` turns shorting off (short and cover return 403).
- Fractional quantities are allowed (6 decimals).
- `PT_SLIPPAGE_BPS` moves the fill price against you (buys higher, sells lower). `PT_COMMISSION_PER_ORDER` is a flat fee. Both default to 0 in code; `.env.example` suggests 5 bps so results are not flattering.
- Orders are serialized per database write, so parallel requests can't overspend.
- Send a `client_order_id` and retry safely: a repeat returns the original fill with `"duplicate": true` instead of trading again. The bundled client sets one automatically.

## Things to know

- Liquidation is checked once a minute, not on every tick, so it is a safety net and not a precise stop. For tighter control, cover in your own script.
- Short selling is simplified: no borrow fees, no hard-to-borrow list, no dividends owed on shorted shares, and any ticker can be shorted.
- Quotes are whatever yfinance returns, which can be delayed and outside market hours is the last close. There is no market-hours check, so a script can "trade" on a closed market at a stale price.
- Equity snapshots are taken after every order and every `PT_SNAPSHOT_SECONDS` (default 60, so the charts update once a minute). Timed snapshots only run Mon-Fri 9:30-16:05 ET (`PT_SNAPSHOT_MARKET_HOURS_ONLY=0` turns that off); the check doesn't know about market holidays. Max drawdown is tracked from these snapshots, so it can miss dips between them.
- Both charts have 1D / 1W / 1M / 3M / All buttons and a custom From/To date picker. A range starts with the equity held at that moment, so the line never begins mid-air.
- The admin chart puts every account on one shared timeline that starts when the oldest account was created. Later joiners are drawn as a dashed line at 0% until their real join date, which is marked with a dot and listed in the Joined column.
- Futures symbols (`=F`) are not blocked yet but are priced like shares, with no contract multiplier or expiry. Treat them as unsupported.
- Hover (or tap, or Tab to) any column label for a short explanation of it. The text lives in `GLOSSARY` in `app/static/common.js`.
- Chart data is thinned in SQLite to about 1,500 points per user (800 on the admin chart), so long histories stay fast.
- Run a single uvicorn worker. The quote cache and snapshot loop are in-process.
- SQLite is fine for a club. Back up the `/data` volume if the history matters.
- Put it behind HTTPS (Caddy, nginx, a tunnel) before exposing it, since API keys travel in headers.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q tests
```
