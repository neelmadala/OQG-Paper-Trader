import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402
from app.settings import Settings  # noqa: E402

ADMIN = {"X-Admin-Token": "admin-secret"}



# The database layout from before portfolios, used to test upgrading old databases.
LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    api_key_hash  TEXT NOT NULL UNIQUE,
    cash          REAL NOT NULL,
    starting_cash REAL NOT NULL,
    peak_equity   REAL NOT NULL,
    max_drawdown  REAL NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    symbol   TEXT NOT NULL,
    qty      REAL NOT NULL,
    avg_cost REAL NOT NULL,
    stop_price REAL,
    PRIMARY KEY (user_id, symbol)
);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_order_id TEXT,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell', 'short', 'cover')),
    qty             REAL NOT NULL,
    price           REAL NOT NULL,
    fee             REAL NOT NULL DEFAULT 0,
    realized_pnl    REAL NOT NULL DEFAULT 0,
    forced          INTEGER NOT NULL DEFAULT 0,
    ts              REAL NOT NULL,
    UNIQUE (user_id, client_order_id)
);
CREATE INDEX IF NOT EXISTS idx_orders_user_ts ON orders(user_id, ts);

CREATE TABLE IF NOT EXISTS snapshots (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ts      REAL NOT NULL,
    equity  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_user_ts ON snapshots(user_id, ts);

-- Resting limit orders. A fill creates a normal row in `orders` (client_order_id 'lim:<id>').
-- status: open | filling (claimed by the fill check) | filled | cancelled | expired | rejected
CREATE TABLE IF NOT EXISTS limit_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_order_id TEXT,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell', 'short', 'cover')),
    qty             REAL NOT NULL,
    limit_price     REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    reason          TEXT,
    order_id        INTEGER,
    created_at      REAL NOT NULL,
    closed_at       REAL
);
CREATE INDEX IF NOT EXISTS idx_limit_status ON limit_orders(status, symbol);
CREATE INDEX IF NOT EXISTS idx_limit_user ON limit_orders(user_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_limit_coid ON limit_orders(user_id, client_order_id) WHERE client_order_id IS NOT NULL;

-- Splits and dividends that have been applied, so each one happens exactly once.
CREATE TABLE IF NOT EXISTS corp_actions (
    symbol       TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('split', 'dividend')),
    ex_date      TEXT NOT NULL,
    value        REAL NOT NULL,
    source       TEXT NOT NULL DEFAULT 'feed',
    processed_at REAL NOT NULL,
    PRIMARY KEY (symbol, kind, ex_date)
);

-- What each action did to each account (shown to members).
CREATE TABLE IF NOT EXISTS corp_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    symbol  TEXT NOT NULL,
    kind    TEXT NOT NULL,
    ex_date TEXT NOT NULL,
    qty     REAL NOT NULL,   -- shares that were entitled
    value   REAL NOT NULL,   -- dividend per share, or split ratio
    amount  REAL NOT NULL,   -- cash paid (+) or owed (-) for a dividend, shares added for a split
    ts      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_user ON corp_events(user_id, id);
"""

def make_app(tmp_path, **overrides):
    s = Settings(**{
        "db_path": str(tmp_path / "t.db"),
        "admin_token": "admin-secret",
        "invite_code": "club-code",
        "price_provider": "mock",
        "snapshot_seconds": 0,
        **overrides,
    })
    return create_app(s)


@pytest.fixture
def app(tmp_path, request):
    # tests marked @pytest.mark.liq run with auto-liquidation on (it is off by default)
    kw = {"auto_liquidate": True} if request.node.get_closest_marker("liq") else {}
    return make_app(tmp_path, **kw)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def register(client, name="alice"):
    r = client.post("/api/register", json={"username": name, "invite_code": "club-code"})
    assert r.status_code == 200, r.text
    return {"X-API-Key": r.json()["api_key"]}


def test_register_rules(client):
    assert client.post("/api/register", json={"username": "bob", "invite_code": "nope"}).status_code == 403
    h = register(client, "bob")
    assert client.post("/api/register", json={"username": "BOB", "invite_code": "club-code"}).status_code == 409
    assert client.post("/api/register", json={"username": "a b", "invite_code": "club-code"}).status_code == 422
    assert client.get("/api/me", headers=h).json()["cash"] == 100000


def test_registration_closed_without_invite(tmp_path):
    s = Settings(db_path=str(tmp_path / "x.db"), price_provider="mock", snapshot_seconds=0)
    with TestClient(create_app(s)) as c:
        assert c.post("/api/register", json={"username": "bob", "invite_code": ""}).status_code == 403
        assert c.get("/api/admin/portfolios", headers=ADMIN).status_code == 403  # admin disabled


def test_auth_required(client):
    assert client.get("/api/portfolio").status_code == 401
    assert client.get("/api/portfolio", headers={"X-API-Key": "pt_bogus"}).status_code == 401
    assert client.get("/api/admin/portfolios").status_code == 401
    assert client.get("/api/admin/portfolios", headers={"X-Admin-Token": "wrong"}).status_code == 401


def test_buy_sell_roundtrip_and_pnl(app, client):
    app.state.provider.set_price("AAPL", 100.0)
    h = register(client)
    r = client.post("/api/orders", json={"symbol": "aapl", "side": "buy", "qty": 10}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["symbol"] == "AAPL" and r.json()["price"] == 100.0

    p = client.get("/api/portfolio", headers=h).json()
    assert p["cash"] == 99000 and p["equity"] == 100000
    assert p["positions"][0]["qty"] == 10

    # price moves, cache TTL means we need a fresh service: clear the cache
    app.state.provider.set_price("AAPL", 110.0)
    app.state.broker.prices._cache.clear()
    p = client.get("/api/portfolio", headers=h).json()
    assert p["equity"] == 100100 and p["pnl"] == 100 and p["return_pct"] == 0.1

    s = client.post("/api/orders", json={"symbol": "AAPL", "side": "sell", "qty": 10}, headers=h).json()
    assert s["realized_pnl"] == 100.0
    p = client.get("/api/portfolio", headers=h).json()
    assert p["cash"] == 100100 and p["positions"] == []


def test_average_cost_and_partial_sell(app, client):
    h = register(client)
    prov = app.state.provider
    prices = app.state.broker.prices
    prov.set_price("XYZ", 10.0)
    client.post("/api/orders", json={"symbol": "XYZ", "side": "buy", "qty": 10}, headers=h)
    prov.set_price("XYZ", 20.0)
    prices._cache.clear()
    client.post("/api/orders", json={"symbol": "XYZ", "side": "buy", "qty": 10}, headers=h)
    pos = client.get("/api/portfolio", headers=h).json()["positions"][0]
    assert pos["qty"] == 20 and pos["avg_cost"] == 15.0

    s = client.post("/api/orders", json={"symbol": "XYZ", "side": "sell", "qty": 5}, headers=h).json()
    assert s["realized_pnl"] == 25.0  # (20 - 15) * 5
    pos = client.get("/api/portfolio", headers=h).json()["positions"][0]
    assert pos["qty"] == 15 and pos["avg_cost"] == 15.0


def test_rejections(app, client):
    app.state.provider.set_price("AAPL", 100.0)
    h = register(client)
    r = client.post("/api/orders", json={"symbol": "AAPL", "side": "buy", "qty": 1001}, headers=h)
    assert r.status_code == 400 and "insufficient cash" in r.json()["detail"]
    r = client.post("/api/orders", json={"symbol": "AAPL", "side": "sell", "qty": 1}, headers=h)
    assert r.status_code == 400 and "insufficient shares" in r.json()["detail"]
    assert client.post("/api/orders", json={"symbol": "AAPL", "side": "buy", "qty": 0}, headers=h).status_code == 422
    assert client.post("/api/orders", json={"symbol": "AAPL", "side": "hold", "qty": 1}, headers=h).status_code == 422
    assert client.post("/api/orders", json={"symbol": "BAD SYM!", "side": "buy", "qty": 1}, headers=h).status_code == 400
    # spending exactly all cash is allowed
    assert client.post("/api/orders", json={"symbol": "AAPL", "side": "buy", "qty": 1000}, headers=h).status_code == 200
    assert client.get("/api/portfolio", headers=h).json()["cash"] == 0


def test_idempotent_client_order_id(app, client):
    app.state.provider.set_price("AAPL", 100.0)
    h = register(client)
    body = {"symbol": "AAPL", "side": "buy", "qty": 5, "client_order_id": "abc"}
    a = client.post("/api/orders", json=body, headers=h).json()
    b = client.post("/api/orders", json=body, headers=h).json()
    assert a["duplicate"] is False and b["duplicate"] is True and a["id"] == b["id"]
    assert client.get("/api/portfolio", headers=h).json()["positions"][0]["qty"] == 5
    clash = client.post("/api/orders", json={**body, "qty": 6}, headers=h)
    assert clash.status_code == 409


def test_concurrent_orders_cannot_overspend(app, client):
    app.state.provider.set_price("AAPL", 100.0)
    h = register(client)
    results = []

    def go():
        r = client.post("/api/orders", json={"symbol": "AAPL", "side": "buy", "qty": 600}, headers=h)
        results.append(r.status_code)

    threads = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [200, 400, 400, 400]
    p = client.get("/api/portfolio", headers=h).json()
    assert p["cash"] == 40000 and p["positions"][0]["qty"] == 600


def test_slippage_and_commission(tmp_path):
    app = make_app(tmp_path, slippage_bps=10, commission=1.0)
    app.state.provider.set_price("AAPL", 100.0)
    with TestClient(app) as c:
        h = register(c)
        buy = c.post("/api/orders", json={"symbol": "AAPL", "side": "buy", "qty": 10}, headers=h).json()
        assert buy["price"] == 100.1  # 10 bps against the buyer
        assert c.get("/api/portfolio", headers=h).json()["cash"] == pytest.approx(100000 - 1001 - 1)
        sell = c.post("/api/orders", json={"symbol": "AAPL", "side": "sell", "qty": 10}, headers=h).json()
        assert sell["price"] == 99.9


def test_stale_price_fallback(app, client):
    app.state.provider.set_price("AAPL", 100.0)
    h = register(client)
    client.post("/api/orders", json={"symbol": "AAPL", "side": "buy", "qty": 10}, headers=h)

    def boom(sym):
        from app.prices import PriceError

        raise PriceError("down", 502)

    app.state.provider.get_price = boom
    app.state.broker.prices._cache["AAPL"] = (0, 100.0)  # expired but known
    p = client.get("/api/portfolio", headers=h)
    assert p.status_code == 200 and p.json()["equity"] == 100000
    # trading needs a fresh price and must fail loudly
    r = client.post("/api/orders", json={"symbol": "AAPL", "side": "buy", "qty": 1}, headers=h)
    assert r.status_code == 502


def test_leaderboard_drawdown_and_admin(app, client):
    prov, prices = app.state.provider, app.state.broker.prices
    prov.set_price("AAPL", 100.0)
    a, b = register(client, "alice"), register(client, "bobby")
    client.post("/api/orders", json={"symbol": "AAPL", "side": "buy", "qty": 100}, headers=a)  # 10k position

    prov.set_price("AAPL", 80.0)  # -2k
    prices._cache.clear()
    client.post("/api/orders", json={"symbol": "AAPL", "side": "buy", "qty": 1}, headers=a)  # snapshot at 98k
    prov.set_price("AAPL", 120.0)
    prices._cache.clear()

    board = client.get("/api/leaderboard", headers=b).json()
    assert [r["username"] for r in board] == ["alice", "bobby"]
    alice = board[0]
    # cash = 100000 - 100*100 - 1*80 = 89920; 101 shares at 120 = 12120; equity 102040
    assert alice["equity"] == 102040 and alice["return_pct"] == 2.04
    # equity dipped to 98000 from a 100000 peak
    assert alice["max_drawdown_pct"] == 2.0
    assert alice["trades"] == 2 and alice["win_rate_pct"] is None

    full = client.get("/api/admin/portfolios", headers=ADMIN).json()
    assert full[0]["positions"][0]["symbol"] == "AAPL"
    curves = client.get("/api/admin/curves", headers=ADMIN).json()
    assert {c["username"] for c in curves} == {"alice", "bobby"}


def test_admin_create_and_reset(app, client):
    app.state.provider.set_price("AAPL", 100.0)
    r = client.post("/api/admin/users", json={"username": "carol"}, headers=ADMIN)
    h = {"X-API-Key": r.json()["api_key"]}
    client.post("/api/orders", json={"symbol": "AAPL", "side": "buy", "qty": 10}, headers=h)
    uid = client.get("/api/admin/portfolios", headers=ADMIN).json()[0]["user_id"]
    assert client.post(f"/api/admin/users/{uid}/reset", headers=ADMIN).status_code == 200
    p = client.get("/api/portfolio", headers=h).json()
    assert p["cash"] == 100000 and p["positions"] == [] and p["max_drawdown_pct"] == 0
    assert client.get("/api/orders", headers=h).json() == []
    assert client.post("/api/admin/users/999/reset", headers=ADMIN).status_code == 404


def test_pages_served(client):
    assert client.get("/").status_code == 200
    assert client.get("/admin").status_code == 200
    assert client.get("/static/common.js").status_code == 200


def test_market_hours():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.market import is_us_market_open

    ny = ZoneInfo("America/New_York")
    assert is_us_market_open(datetime(2026, 10, 26, 10, 0, tzinfo=ny))  # Monday morning
    assert is_us_market_open(datetime(2026, 10, 26, 9, 30, tzinfo=ny))  # the open
    assert is_us_market_open(datetime(2026, 10, 26, 16, 3, tzinfo=ny))  # grace after close
    assert not is_us_market_open(datetime(2026, 10, 26, 9, 29, tzinfo=ny))
    assert not is_us_market_open(datetime(2026, 10, 26, 16, 6, tzinfo=ny))
    assert not is_us_market_open(datetime(2026, 10, 24, 11, 0, tzinfo=ny))  # Saturday
    assert not is_us_market_open(datetime(2026, 10, 25, 11, 0, tzinfo=ny))  # Sunday
    # timezone-aware input from another zone is converted: 14:00 UTC Monday = 10:00 ET (EDT)
    assert is_us_market_open(datetime(2026, 10, 26, 14, 0, tzinfo=ZoneInfo("UTC")))


def test_equity_curve_thinned_in_sql(app, client):
    import time

    broker = app.state.broker
    base = time.time() + 10  # after the account's own starting snapshot
    h = register(client)
    uid = broker.user_by_key(h["X-API-Key"])["id"]
    with broker.db.tx() as c:  # 5000 extra points on top of the initial one
        c.executemany(
            "INSERT INTO snapshots (portfolio_id, ts, equity) VALUES (?,?,?)",
            [(uid, base + i * 60, 100000 + i) for i in range(5000)],
        )
    curve = client.get("/api/equity", headers=h).json()
    assert len(curve) <= 1501
    ts = [p["ts"] for p in curve]
    assert ts == sorted(ts)
    assert curve[-1]["equity"] == 100000 + 4999  # newest point always kept
    assert curve[0]["ts"] < base and curve[0]["equity"] == 100000  # starting point kept

    small = client.get("/api/admin/curves", headers=ADMIN).json()[0]["points"]
    assert len(small) <= 801 and small[-1]["return_pct"] == round(4999 / 1000, 3)


def _seed(broker, uid, points):
    with broker.db.tx() as c:
        c.executemany("INSERT INTO snapshots (portfolio_id, ts, equity) VALUES (?,?,?)", [(uid, t, e) for t, e in points])


def test_equity_range_and_carry_in(app, client):
    import time

    broker = app.state.broker
    h = register(client)
    uid = broker.user_by_key(h["X-API-Key"])["id"]
    now = time.time()
    with broker.db.tx() as c:
        c.execute("DELETE FROM snapshots WHERE portfolio_id=?", (uid,))
    _seed(broker, uid, [(now - 5000, 100000), (now - 4000, 101000), (now - 2000, 103000), (now - 1000, 102000)])

    full = client.get("/api/equity", headers=h).json()
    assert [p["equity"] for p in full] == [100000, 101000, 103000, 102000]

    since = now - 2500  # falls between the 2nd and 3rd snapshot
    r = client.get("/api/equity", params={"since": since}, headers=h).json()
    # starts at the range start with the equity held at that moment, then the real in-range points
    assert r[0] == {"ts": since, "equity": 101000}
    assert [p["equity"] for p in r[1:]] == [103000, 102000]

    r = client.get("/api/equity", params={"since": since, "until": now - 1500}, headers=h).json()
    assert [p["equity"] for p in r] == [101000, 103000]

    # a window with no snapshots still draws a flat line from the carried-in value
    r = client.get("/api/equity", params={"since": now - 500}, headers=h).json()
    assert r == [{"ts": now - 500, "equity": 102000}]
    assert client.get("/api/equity", params={"since": 10, "until": 5}, headers=h).status_code == 422
    assert client.get("/api/equity", params={"since": "abc"}, headers=h).status_code == 422


def test_admin_curves_backfill_late_joiners(app, client):
    broker = app.state.broker
    a = register(client, "alice")
    # age alice by 10 days so bob is a late joiner
    with broker.db.tx() as c:
        c.execute("UPDATE users SET created_at = created_at - 864000"); c.execute("UPDATE portfolios SET created_at = created_at - 864000")
        c.execute("UPDATE snapshots SET ts = ts - 864000")
    b = register(client, "bobby")

    curves = {c["username"]: c for c in client.get("/api/admin/curves", headers=ADMIN).json()}
    alice, bob = curves["alice"], curves["bobby"]
    assert alice["start"] == bob["start"] == alice["created_at"]  # shared start = oldest account
    assert bob["created_at"] > alice["created_at"] + 800000  # true creation date is preserved

    # oldest account: real data from the start, marked as created
    assert alice["points"][0]["created"] is True and "synthetic" not in alice["points"][0]
    # late joiner: pinned at 0% from the shared start, then its real first point, marked as created
    assert bob["points"][0] == {"ts": alice["start"], "return_pct": 0.0, "synthetic": True}
    assert bob["points"][1]["created"] is True and bob["points"][1]["ts"] == pytest.approx(bob["created_at"], abs=1)

    # a range that starts after bob joined has no synthetic part and no "created" marker
    since = bob["created_at"] + 1
    curves = {c["username"]: c for c in client.get("/api/admin/curves", params={"since": since}, headers=ADMIN).json()}
    for name in ("alice", "bobby"):
        assert not any(p.get("synthetic") or p.get("created") for p in curves[name]["points"])
        assert curves[name]["points"][0]["ts"] == since  # carried-in start

    # a range that ends before bob joined keeps him flat at 0% instead of dropping him
    until = bob["created_at"] - 100
    curves = {c["username"]: c for c in client.get("/api/admin/curves", params={"until": until}, headers=ADMIN).json()}
    assert [p["return_pct"] for p in curves["bobby"]["points"]] == [0.0, 0.0]
    assert curves["bobby"]["points"][-1]["ts"] == until

    rows = client.get("/api/admin/portfolios", headers=ADMIN).json()
    assert {r["username"]: r["created_at"] for r in rows}["bobby"] == bob["created_at"]


# ---------------------------------------------------------------- shorting

def _px(app, symbol, price):
    app.state.provider.set_price(symbol, price)
    app.state.broker.prices._cache.clear()


def _order(client, h, symbol, side, qty, **kw):
    return client.post("/api/orders", json={"symbol": symbol, "side": side, "qty": qty, **kw}, headers=h)


def test_short_profit_and_cover(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    r = _order(client, h, "XYZ", "short", 10)
    assert r.status_code == 200 and r.json()["side"] == "short" and r.json()["realized_pnl"] == 0
    p = client.get("/api/portfolio", headers=h).json()
    assert p["cash"] == 101000 and p["equity"] == 100000  # proceeds in, liability out: no instant profit
    pos = p["positions"][0]
    assert pos["side"] == "short" and pos["qty"] == -10 and pos["avg_cost"] == 100.0 and pos["market_value"] == -1000
    assert p["short_value"] == 1000 and p["long_value"] == 0
    assert p["free_cash"] == 101000 - 1.5 * 1000

    _px(app, "XYZ", 90.0)  # price falls, the short gains
    p = client.get("/api/portfolio", headers=h).json()
    assert p["equity"] == 100100 and p["positions"][0]["unrealized_pnl"] == 100
    assert p["positions"][0]["unrealized_pnl_pct"] == 10.0  # +10% on a short that fell 10%

    c = _order(client, h, "XYZ", "cover", 10).json()
    assert c["realized_pnl"] == 100.0
    p = client.get("/api/portfolio", headers=h).json()
    assert p["cash"] == 100100 and p["positions"] == [] and p["short_value"] == 0


def test_short_loss(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    _order(client, h, "XYZ", "short", 10)
    _px(app, "XYZ", 110.0)
    p = client.get("/api/portfolio", headers=h).json()
    assert p["equity"] == 99900 and p["positions"][0]["unrealized_pnl"] == -100
    assert p["positions"][0]["unrealized_pnl_pct"] == -10.0
    assert _order(client, h, "XYZ", "cover", 4).json()["realized_pnl"] == -40.0
    pos = client.get("/api/portfolio", headers=h).json()["positions"][0]
    assert pos["qty"] == -6 and pos["avg_cost"] == 100.0  # partial cover keeps the entry price


def test_adding_to_a_short_averages_the_entry_price(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    _order(client, h, "XYZ", "short", 10)
    _px(app, "XYZ", 120.0)
    _order(client, h, "XYZ", "short", 10)
    pos = client.get("/api/portfolio", headers=h).json()["positions"][0]
    assert pos["qty"] == -20 and pos["avg_cost"] == 110.0
    _px(app, "XYZ", 100.0)
    assert _order(client, h, "XYZ", "cover", 20).json()["realized_pnl"] == 200.0  # (110 - 100) * 20


def test_margin_limits_shorts_and_locks_cash(app, client):
    _px(app, "XYZ", 100.0)
    _px(app, "ABC", 10.0)
    h = register(client)
    # margin 50%: shorting N shares needs 0.5 * N * 100 free cash -> at most 2000 shares on 100k
    assert _order(client, h, "XYZ", "short", 2001).status_code == 400
    assert _order(client, h, "XYZ", "short", 2000).status_code == 200
    p = client.get("/api/portfolio", headers=h).json()
    assert p["cash"] == 300000 and p["short_value"] == 200000 and p["free_cash"] == 0
    r = _order(client, h, "XYZ", "short", 1)
    assert r.status_code == 400 and "insufficient margin" in r.json()["detail"]
    # short proceeds can't be used to buy more stock
    r = _order(client, h, "ABC", "buy", 1)
    assert r.status_code == 400 and "insufficient free cash" in r.json()["detail"]
    # covering is always allowed and frees cash again
    assert _order(client, h, "XYZ", "cover", 1000).status_code == 200
    assert _order(client, h, "ABC", "buy", 1000).status_code == 200  # 10k, fits in the freed cash


def test_long_and_short_together(app, client):
    _px(app, "LONG", 50.0)
    _px(app, "SHRT", 100.0)
    h = register(client)
    assert _order(client, h, "LONG", "buy", 1000).status_code == 200  # 50k, cash 50k
    # cash 50k: shorting N shares needs 0.5*N*100 = 50N free cash -> 1000 shares
    assert _order(client, h, "SHRT", "short", 1001).status_code == 400
    assert _order(client, h, "SHRT", "short", 1000).status_code == 200
    p = client.get("/api/portfolio", headers=h).json()
    assert p["long_value"] == 50000 and p["short_value"] == 100000 and p["equity"] == 100000
    assert p["cash"] == 150000 and p["free_cash"] == 0


def test_margin_call_flag(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    _order(client, h, "XYZ", "short", 1000)  # cash 200k, short 100k
    assert client.get("/api/portfolio", headers=h).json()["margin_call"] is False
    _px(app, "XYZ", 160.0)  # short now 160k; maintenance needs cash >= 1.3 * 160k = 208k, cash is 200k
    p = client.get("/api/portfolio", headers=h).json()
    assert p["margin_call"] is True and p["free_cash"] < 0
    assert _order(client, h, "XYZ", "short", 1).status_code == 400  # no new risk while under water
    assert _order(client, h, "XYZ", "cover", 500).status_code == 200  # reducing risk is always allowed


def test_no_accidental_flips_or_wrong_side(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    r = _order(client, h, "XYZ", "cover", 1)
    assert r.status_code == 400 and "no short position" in r.json()["detail"]
    r = _order(client, h, "XYZ", "sell", 1)  # selling shares you don't own is NOT a short
    assert r.status_code == 400 and "insufficient shares" in r.json()["detail"]
    _order(client, h, "XYZ", "buy", 5)
    r = _order(client, h, "XYZ", "short", 1)
    assert r.status_code == 400 and "sell it before shorting" in r.json()["detail"]
    _order(client, h, "XYZ", "sell", 5)
    _order(client, h, "XYZ", "short", 5)
    r = _order(client, h, "XYZ", "buy", 1)
    assert r.status_code == 400 and "use 'cover'" in r.json()["detail"]
    r = _order(client, h, "XYZ", "sell", 1)
    assert r.status_code == 400 and "use 'cover'" in r.json()["detail"]
    r = _order(client, h, "XYZ", "cover", 6)
    assert r.status_code == 400 and "only short 5" in r.json()["detail"]
    assert client.get("/api/portfolio", headers=h).json()["positions"][0]["qty"] == -5  # nothing changed


def test_slippage_and_commission_on_shorts(tmp_path):
    app = make_app(tmp_path, slippage_bps=10, commission=2.0)
    app.state.provider.set_price("XYZ", 100.0)
    with TestClient(app) as c:
        h = register(c)
        s = _order(c, h, "XYZ", "short", 10).json()
        assert s["price"] == 99.9  # selling short fills below the quote
        assert c.get("/api/portfolio", headers=h).json()["cash"] == pytest.approx(100000 + 999 - 2)
        cv = _order(c, h, "XYZ", "cover", 10).json()
        assert cv["price"] == 100.1  # buying back fills above the quote
        assert cv["realized_pnl"] == pytest.approx((99.9 - 100.1) * 10 - 2)
        # round trip cost: slippage 2.00 + two commissions 4.00
        assert c.get("/api/portfolio", headers=h).json()["cash"] == pytest.approx(100000 - 2 - 4)


def test_shorting_can_be_disabled(tmp_path):
    app = make_app(tmp_path, allow_shorting=False)
    app.state.provider.set_price("XYZ", 100.0)
    with TestClient(app) as c:
        h = register(c)
        r = _order(c, h, "XYZ", "short", 1)
        assert r.status_code == 403 and "disabled" in r.json()["detail"]
        assert c.get("/api/portfolio", headers=h).json()["shorting_enabled"] is False
        assert _order(c, h, "XYZ", "buy", 1).status_code == 200  # longs unaffected


def test_win_rate_counts_covers(app, client):
    _px(app, "XYZ", 100.0)
    a = register(client, "alice")
    _order(client, a, "XYZ", "short", 10)
    _px(app, "XYZ", 90.0)
    _order(client, a, "XYZ", "cover", 10)  # profitable close
    row = client.get("/api/leaderboard", headers=a).json()[0]
    assert row["trades"] == 2 and row["win_rate_pct"] == 100.0


def test_concurrent_shorts_cannot_exceed_margin(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    results = []

    def go():
        results.append(_order(client, h, "XYZ", "short", 1500).status_code)  # needs 75k free cash each

    threads = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [200, 400, 400, 400]
    p = client.get("/api/portfolio", headers=h).json()
    assert p["short_value"] == 150000 and p["free_cash"] >= 0


def test_old_database_is_migrated_without_losing_orders(tmp_path):
    import sqlite3

    from app.db import Database

    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA.replace("'buy', 'sell', 'short', 'cover'", "'buy', 'sell'"))  # pre-shorting schema
    conn.execute("INSERT INTO users (username, api_key_hash, cash, starting_cash, peak_equity, created_at)"
                 " VALUES ('old', 'h', 99000, 100000, 100000, 1)")
    conn.execute("INSERT INTO orders (user_id, client_order_id, symbol, side, qty, price, fee, realized_pnl, ts)"
                 " VALUES (1, 'c1', 'AAPL', 'buy', 10, 100, 0, 0, 5)")
    conn.commit()
    conn.execute("INSERT INTO positions (user_id, symbol, qty, avg_cost) VALUES (1, 'AAPL', 10, 100)")
    conn.execute("INSERT INTO snapshots (user_id, ts, equity) VALUES (1, 5, 99900)")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):  # the old constraint really does reject shorts
        conn.execute("INSERT INTO orders (user_id, symbol, side, qty, price, ts) VALUES (1, 'X', 'short', 1, 1, 1)")
    conn.close()

    for _ in range(2):  # opening twice must be a no-op the second time
        db = Database(path)
        with db.read() as c:
            rows = c.execute("SELECT id, client_order_id, side, qty FROM orders").fetchall()
            assert [tuple(r) for r in rows] == [(1, "c1", "buy", 10.0)]
            # the old account became a user plus a "Main" portfolio with the same id, cash and history
            u = c.execute("SELECT username FROM users WHERE id=1").fetchone()
            p = c.execute("SELECT * FROM portfolios WHERE id=1").fetchone()
            assert u["username"] == "old" and p["user_id"] == 1 and p["name"] == "Main" and p["is_default"] == 1
            assert p["cash"] == 99000 and p["starting_cash"] == 100000 and p["competition_id"] is None
            assert c.execute("SELECT qty FROM positions WHERE portfolio_id=1 AND symbol='AAPL'").fetchone()["qty"] == 10
            assert c.execute("SELECT equity FROM snapshots WHERE portfolio_id=1").fetchone()["equity"] == 99900
            assert "cash" not in {r["name"] for r in c.execute("PRAGMA table_info(users)")}
    with db.tx() as c:
        c.execute("INSERT INTO orders (portfolio_id, symbol, side, qty, price, ts) VALUES (1, 'X', 'short', 1, 1, 6)")
        c.execute("INSERT INTO orders (portfolio_id, symbol, side, qty, price, ts) VALUES (1, 'X', 'cover', 1, 1, 7)")
    with pytest.raises(sqlite3.IntegrityError):  # and the constraint still rejects garbage
        with db.tx() as c:
            c.execute("INSERT INTO orders (portfolio_id, symbol, side, qty, price, ts) VALUES (1, 'X', 'hold', 1, 1, 8)")


# ---------------------------------------------------------------- liquidation

@pytest.mark.liq
def test_liquidation_covers_just_enough(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    _order(client, h, "XYZ", "short", 1000)
    _px(app, "XYZ", 160.0)  # cash 200k < 1.3 * 160k -> breach
    assert app.state.broker.liquidate_all() == 1
    p = client.get("/api/portfolio", headers=h).json()
    pos = p["positions"][0]
    assert pos["qty"] == -500  # 500 covered is exactly enough to restore the initial margin
    assert p["free_cash"] >= -0.01 and p["margin_call"] is False
    o = client.get("/api/orders", headers=h).json()[0]
    assert o["side"] == "cover" and o["forced"] is True and o["qty"] == 500
    assert client.get("/api/orders", headers=h).json()[1]["forced"] is False
    assert app.state.broker.liquidate_all() == 0  # idempotent: healthy now


@pytest.mark.liq
def test_no_liquidation_when_healthy_or_between_levels(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    _order(client, h, "XYZ", "short", 1000)
    assert app.state.broker.liquidate_all() == 0
    _px(app, "XYZ", 130.0)  # cash 200k >= 1.3 * 130k = 169k, but below the 1.5 initial level: warned, not liquidated
    assert app.state.broker.liquidate_all() == 0
    assert client.get("/api/portfolio", headers=h).json()["positions"][0]["qty"] == -1000


def test_liquidation_disabled_and_costs(tmp_path):
    app = make_app(tmp_path, auto_liquidate=False)
    with TestClient(app) as c:
        _px(app, "XYZ", 100.0)
        h = register(c)
        _order(c, h, "XYZ", "short", 1000)
        _px(app, "XYZ", 160.0)
        assert app.state.broker.liquidate_all() == 0
        assert c.get("/api/portfolio", headers=h).json()["margin_call"] is True
    app = make_app(tmp_path / "b", auto_liquidate=True, slippage_bps=100, commission=5) if (tmp_path / "b").mkdir() is None else None
    with TestClient(app) as c:
        _px(app, "XYZ", 100.0)
        h = register(c)
        _order(c, h, "XYZ", "short", 1000)
        _px(app, "XYZ", 170.0)
        assert app.state.broker.liquidate_all() >= 1
        o = c.get("/api/orders", headers=h).json()[0]
        assert o["forced"] and o["price"] == pytest.approx(171.7)  # slippage applies to forced covers
        p = c.get("/api/portfolio", headers=h).json()
        assert p["free_cash"] >= -0.5


@pytest.mark.liq
def test_liquidation_skips_when_no_price_and_covers_largest_first(app, client):
    _px(app, "AAA", 100.0)
    _px(app, "BBB", 100.0)
    h = register(client)
    _order(client, h, "AAA", "short", 300)
    _order(client, h, "BBB", "short", 700)
    _px(app, "AAA", 200.0)
    _px(app, "BBB", 200.0)
    # shorts worth 200k; cash 200k -> far below 1.3x
    broker = app.state.broker
    orig = broker.prices.get
    broker.prices.get = lambda s: (_ for _ in ()).throw(__import__("app.prices", fromlist=["PriceError"]).PriceError("down"))
    assert broker.liquidate_user(1) == []  # never liquidate on missing data
    broker.prices.get = orig
    placed = broker.liquidate_user(1)
    assert placed and placed[0]["symbol"] == "BBB"  # largest short first
    assert client.get("/api/portfolio", headers=h).json()["margin_call"] is False


def test_liquidation_forced_flag_survives_migration(tmp_path):
    import sqlite3
    from app.db import Database
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, api_key_hash TEXT, cash REAL, starting_cash REAL, peak_equity REAL, max_drawdown REAL DEFAULT 0, created_at REAL);"
        "INSERT INTO users VALUES (1,'u','h',100,100,100,0,1);"
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, client_order_id TEXT, symbol TEXT, "
        "side TEXT NOT NULL CHECK (side IN ('buy','sell','short','cover')), qty REAL, price REAL, fee REAL, realized_pnl REAL, ts REAL);"
        "INSERT INTO orders VALUES (1,1,NULL,'A','buy',1,1,0,0,1);"
    )
    con.commit(); con.close()
    db = Database(str(p))
    with db.read() as c:
        assert c.execute("SELECT forced FROM orders WHERE id=1").fetchone()["forced"] == 0


def test_liquidation_is_off_by_default(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    _order(client, h, "XYZ", "short", 1000)
    _px(app, "XYZ", 160.0)
    assert app.state.broker.liquidate_all() == 0
    p = client.get("/api/portfolio", headers=h).json()
    assert p["positions"][0]["qty"] == -1000 and p["margin_call"] is True and p["auto_liquidate"] is False


# ---------------------------------------------------------------- stops

def _stop(client, h, sym, price):
    return client.put(f"/api/positions/{sym}/stop", json={"stop_price": price}, headers=h)


def test_long_stop_sells_everything_when_hit(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    r = _order(client, h, "XYZ", "buy", 50, stop_price=90)
    assert r.status_code == 200
    assert client.get("/api/portfolio", headers=h).json()["positions"][0]["stop_price"] == 90
    _px(app, "XYZ", 95.0)
    assert app.state.broker.check_stops() == 0  # above the stop: nothing happens
    _px(app, "XYZ", 89.0)
    assert app.state.broker.check_stops() == 1
    o = client.get("/api/orders", headers=h).json()[0]
    assert o["side"] == "sell" and o["qty"] == 50 and o["forced"] and o["reason"] == "stop"
    assert o["price"] == 89.0  # fills at the live price, not the stop
    assert client.get("/api/portfolio", headers=h).json()["positions"] == []
    assert app.state.broker.check_stops() == 0  # nothing left to stop


def test_stop_triggers_exactly_at_the_price(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    _order(client, h, "XYZ", "buy", 10, stop_price=90)
    _px(app, "XYZ", 90.0)
    assert app.state.broker.check_stops() == 1


def test_short_stop_covers_when_price_rises(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    assert _order(client, h, "XYZ", "short", 100, stop_price=110).status_code == 200
    _px(app, "XYZ", 105.0)
    assert app.state.broker.check_stops() == 0
    _px(app, "XYZ", 112.0)
    assert app.state.broker.check_stops() == 1
    o = client.get("/api/orders", headers=h).json()[0]
    assert o["side"] == "cover" and o["reason"] == "stop" and o["realized_pnl"] == pytest.approx(-1200)
    assert client.get("/api/portfolio", headers=h).json()["positions"] == []


def test_stop_validation(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    assert _order(client, h, "XYZ", "buy", 1, stop_price=100).status_code == 422  # not below the price
    assert _order(client, h, "XYZ", "buy", 1, stop_price=120).status_code == 422
    assert _order(client, h, "XYZ", "short", 1, stop_price=90).status_code == 422  # not above the price
    assert _order(client, h, "XYZ", "buy", 1, stop_price=-5).status_code == 422
    assert client.get("/api/portfolio", headers=h).json()["positions"] == []  # rejected orders did not trade
    _order(client, h, "XYZ", "buy", 10)
    assert _order(client, h, "XYZ", "sell", 1, stop_price=90).status_code == 422  # only buy/short take a stop


def test_set_change_and_clear_stop(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    assert _stop(client, h, "XYZ", 90).status_code == 404  # no position yet
    _order(client, h, "XYZ", "buy", 10)
    assert _stop(client, h, "XYZ", 90).json() == {"symbol": "XYZ", "stop_price": 90}
    assert _stop(client, h, "XYZ", 100).status_code == 422
    assert _stop(client, h, "XYZ", 95).status_code == 200
    _order(client, h, "XYZ", "buy", 5)  # adding keeps the stop
    assert client.get("/api/portfolio", headers=h).json()["positions"][0]["stop_price"] == 95
    assert _stop(client, h, "XYZ", None).status_code == 200  # clear
    assert client.get("/api/portfolio", headers=h).json()["positions"][0]["stop_price"] is None
    _px(app, "XYZ", 50.0)
    assert app.state.broker.check_stops() == 0
    assert client.put("/api/positions/XYZ/stop", json={"stop_price": 0}, headers=h).status_code == 422
    assert client.put("/api/positions/XYZ/stop", json={"stop_price": 9}).status_code == 401


def test_stop_does_not_survive_closing_the_position(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    _order(client, h, "XYZ", "buy", 10, stop_price=90)
    _order(client, h, "XYZ", "sell", 10)
    _order(client, h, "XYZ", "buy", 10)  # a fresh position starts with no stop
    assert client.get("/api/portfolio", headers=h).json()["positions"][0]["stop_price"] is None


def test_stop_skipped_without_a_fresh_price(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    _order(client, h, "XYZ", "buy", 10, stop_price=90)
    _px(app, "XYZ", 80.0)

    def boom(sym):
        from app.prices import PriceError
        raise PriceError("down")

    app.state.provider.get_price = boom
    assert app.state.broker.check_stops() == 0
    assert client.get("/api/portfolio", headers=h).json()["positions"][0]["qty"] == 10


def test_stop_checked_by_the_background_loop(tmp_path):
    import time
    app = create_app(Settings(db_path=str(tmp_path / "t.db"), admin_token="admin-secret", invite_code="club-code",
                              price_provider="mock", snapshot_seconds=1, snapshot_market_hours_only=False))
    with TestClient(app) as c:
        _px(app, "XYZ", 100.0)
        h = register(c)
        _order(c, h, "XYZ", "buy", 10, stop_price=90)
        _px(app, "XYZ", 80.0)
        for _ in range(40):
            time.sleep(0.25)
            if not c.get("/api/portfolio", headers=h).json()["positions"]:
                break
        assert c.get("/api/portfolio", headers=h).json()["positions"] == []
        assert c.get("/api/orders", headers=h).json()[0]["reason"] == "stop"


def test_old_positions_table_gets_stop_column(tmp_path):
    import sqlite3
    from app.db import Database
    p = tmp_path / "old2.db"
    con = sqlite3.connect(p)
    con.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, api_key_hash TEXT, cash REAL, starting_cash REAL, peak_equity REAL, max_drawdown REAL DEFAULT 0, created_at REAL);"
        "INSERT INTO users VALUES (1,'u','h',100,100,100,0,1);"
        "CREATE TABLE positions (user_id INTEGER NOT NULL, symbol TEXT NOT NULL, qty REAL NOT NULL, avg_cost REAL NOT NULL, PRIMARY KEY (user_id, symbol));"
        "INSERT INTO positions VALUES (1,'A',5,10);"
    )
    con.commit(); con.close()
    db = Database(str(p))
    with db.read() as c:
        r = c.execute("SELECT qty, stop_price FROM positions").fetchone()
    assert r["qty"] == 5 and r["stop_price"] is None


# ---------------------------------------------------------------- indices

def test_indices_cannot_be_traded_but_can_be_quoted(app, client):
    h = register(client)
    for side in ("buy", "short"):
        r = _order(client, h, "^GSPC", side, 1)
        assert r.status_code == 422 and "index" in r.json()["detail"] and "SPY" in r.json()["detail"]
    r = client.post("/api/orders", json={"symbol": "^GSPC", "side": "buy", "qty": 1, "limit_price": 5000}, headers=h)
    assert r.status_code == 422
    q = client.get("/api/quote/%5EGSPC", headers=h)
    assert q.status_code == 200 and q.json()["symbol"] == "^GSPC"
    assert client.get("/api/portfolio", headers=h).json()["positions"] == []


# ---------------------------------------------------------------- limit orders

def _limit(client, h, symbol, side, qty, price, **kw):
    return client.post(
        "/api/orders", json={"symbol": symbol, "side": side, "qty": qty, "limit_price": price, **kw}, headers=h
    )


def _open(client, h, status="open"):
    return client.get(f"/api/open-orders?status={status}", headers=h).json()


def test_resting_buy_limit_fills_when_price_drops(tmp_path):
    app = make_app(tmp_path, slippage_bps=100)
    with TestClient(app) as c:
        _px(app, "XYZ", 100.0)
        h = register(c)
        r = _limit(c, h, "XYZ", "buy", 10, 95)
        assert r.status_code == 200 and r.json()["status"] == "open" and r.json()["limit_price"] == 95
        assert c.get("/api/portfolio", headers=h).json()["cash"] == 100000  # nothing reserved or spent yet
        assert app.state.broker.check_limits() == 0
        _px(app, "XYZ", 94.0)
        assert app.state.broker.check_limits() == 1
        p = c.get("/api/portfolio", headers=h).json()
        assert p["positions"][0]["qty"] == 10 and p["positions"][0]["avg_cost"] == 94.0  # live price, no slippage
        o = c.get("/api/orders", headers=h).json()[0]
        assert o["reason"] == "limit" and o["forced"] is False and o["price"] == 94.0
        lo = _open(c, h, "all")[0]
        assert lo["status"] == "filled" and lo["order_id"] == o["id"]
        assert _open(c, h) == []
        assert app.state.broker.check_limits() == 0  # filled once only
        assert len(c.get("/api/orders", headers=h).json()) == 1


def test_marketable_limit_fills_now_with_slippage_capped_at_the_limit(tmp_path):
    app = make_app(tmp_path, slippage_bps=100)
    with TestClient(app) as c:
        _px(app, "XYZ", 100.0)
        h = register(c)
        r = _limit(c, h, "XYZ", "buy", 10, 105).json()
        assert r["status"] == "filled" and r["price"] == 101.0 and r["reason"] == "limit"  # pays slippage like a market order
        r = _limit(c, h, "XYZ", "buy", 10, 100.5).json()
        assert r["status"] == "filled" and r["price"] == 100.5  # but never worse than the limit
        assert _open(c, h) == []


def test_sell_short_and_cover_limits_use_the_right_direction(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    _order(client, h, "XYZ", "buy", 10)
    assert _limit(client, h, "XYZ", "sell", 10, 110).json()["status"] == "open"  # above the market: rests
    _px(app, "XYZ", 109.0)
    assert app.state.broker.check_limits() == 0
    _px(app, "XYZ", 111.0)
    assert app.state.broker.check_limits() == 1
    assert client.get("/api/portfolio", headers=h).json()["positions"] == []
    o = client.get("/api/orders", headers=h).json()[0]
    assert o["side"] == "sell" and o["price"] == 111.0 and o["realized_pnl"] == pytest.approx(110)

    assert _limit(client, h, "XYZ", "short", 10, 120).json()["status"] == "open"
    _px(app, "XYZ", 120.0)  # exactly at the limit counts
    assert app.state.broker.check_limits() == 1
    assert client.get("/api/portfolio", headers=h).json()["positions"][0]["qty"] == -10
    assert _limit(client, h, "XYZ", "cover", 10, 100).json()["status"] == "open"
    _px(app, "XYZ", 101.0)
    assert app.state.broker.check_limits() == 0
    _px(app, "XYZ", 99.0)
    assert app.state.broker.check_limits() == 1
    assert client.get("/api/portfolio", headers=h).json()["positions"] == []


def test_cancel_limit_order(app, client):
    _px(app, "XYZ", 100.0)
    h, h2 = register(client), register(client, "bobby")
    oid = _limit(client, h, "XYZ", "buy", 10, 90).json()["id"]
    assert client.delete(f"/api/open-orders/{oid}", headers=h2).status_code == 404  # not yours
    assert client.delete(f"/api/open-orders/{oid}", headers=h).json()["status"] == "cancelled"
    assert client.delete(f"/api/open-orders/{oid}", headers=h).status_code == 409  # already cancelled
    assert client.delete("/api/open-orders/9999", headers=h).status_code == 404
    _px(app, "XYZ", 80.0)
    assert app.state.broker.check_limits() == 0  # a cancelled order never fills
    assert client.get("/api/portfolio", headers=h).json()["positions"] == []
    assert [o["status"] for o in _open(client, h, "all")] == ["cancelled"]
    oid2 = _limit(client, h, "XYZ", "buy", 1, 70).json()["id"]
    _px(app, "XYZ", 60.0)
    app.state.broker.check_limits()
    assert client.delete(f"/api/open-orders/{oid2}", headers=h).status_code == 409  # already filled


def test_limit_order_idempotency(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    a = _limit(client, h, "XYZ", "buy", 10, 90, client_order_id="abc").json()
    b = _limit(client, h, "XYZ", "buy", 10, 90, client_order_id="abc").json()
    assert a["id"] == b["id"] and b["duplicate"] is True and len(_open(client, h)) == 1
    assert _limit(client, h, "XYZ", "buy", 10, 91, client_order_id="abc").status_code == 409  # different order
    _px(app, "XYZ", 89.0)
    app.state.broker.check_limits()
    again = _limit(client, h, "XYZ", "buy", 10, 90, client_order_id="abc").json()
    assert again["id"] == a["id"] and again["status"] == "filled"  # a retry after the fill doesn't trade again
    assert client.get("/api/portfolio", headers=h).json()["positions"][0]["qty"] == 10
    # filled on arrival, then retried
    f1 = _limit(client, h, "XYZ", "buy", 1, 200, client_order_id="now").json()
    f2 = _limit(client, h, "XYZ", "buy", 1, 200, client_order_id="now").json()
    assert f1["status"] == "filled" and f2["id"] == f1["id"] and f2["duplicate"] is True
    assert client.get("/api/portfolio", headers=h).json()["positions"][0]["qty"] == 11


def test_limit_order_validation_and_caps(tmp_path):
    app = make_app(tmp_path, max_open_limits=2)
    with TestClient(app) as c:
        _px(app, "XYZ", 100.0)
        h = register(c)
        assert _limit(c, h, "XYZ", "buy", 100000, 90).status_code == 400  # can never be afforded
        assert _limit(c, h, "XYZ", "sell", 1, 110).status_code == 400  # nothing to sell
        assert _limit(c, h, "XYZ", "cover", 1, 90).status_code == 400
        assert _limit(c, h, "XYZ", "buy", 1, 90, stop_price=80).status_code == 422
        assert c.post("/api/orders", json={"symbol": "XYZ", "side": "buy", "qty": 1, "limit_price": 0}, headers=h).status_code == 422
        assert _limit(c, h, "XYZ", "buy", 1, 90).status_code == 200
        assert _limit(c, h, "XYZ", "buy", 1, 89).status_code == 200
        assert _limit(c, h, "XYZ", "buy", 1, 88).status_code == 429  # too many resting orders
    app = make_app(tmp_path / "x", allow_shorting=False) if (tmp_path / "x").mkdir() is None else None
    with TestClient(app) as c:
        _px(app, "XYZ", 100.0)
        h = register(c)
        assert _limit(c, h, "XYZ", "short", 1, 110).status_code == 403


def test_limit_rejected_at_fill_when_cash_is_gone(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    lid = _limit(client, h, "XYZ", "buy", 1000, 95).json()["id"]  # $95k of a $100k account
    _order(client, h, "XYZ", "buy", 500)  # spend $50k meanwhile
    _px(app, "XYZ", 94.0)
    assert app.state.broker.check_limits() == 0
    lo = [o for o in _open(client, h, "all") if o["id"] == lid][0]
    assert lo["status"] == "rejected" and "insufficient" in lo["reason"]
    assert client.get("/api/portfolio", headers=h).json()["positions"][0]["qty"] == 500


def test_limit_orders_expire(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    _limit(client, h, "XYZ", "buy", 1, 90)
    with app.state.broker.db.tx() as c:
        c.execute("UPDATE limit_orders SET created_at = created_at - ?", (31 * 86400,))
    _px(app, "XYZ", 80.0)
    assert app.state.broker.check_limits() == 0
    assert _open(client, h, "all")[0]["status"] == "expired" and _open(client, h) == []


def test_limit_not_filled_without_a_fresh_price(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    _limit(client, h, "XYZ", "buy", 1, 90)
    _px(app, "XYZ", 80.0)

    def boom(sym):
        from app.prices import PriceError
        raise PriceError("down")

    app.state.provider.get_price = boom
    assert app.state.broker.check_limits() == 0
    assert _open(client, h)[0]["status"] == "open"


def test_stuck_filling_orders_are_recovered(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    lid = _limit(client, h, "XYZ", "buy", 10, 90).json()["id"]
    with app.state.broker.db.tx() as c:
        c.execute("UPDATE limit_orders SET status='filling' WHERE id=?", (lid,))
    app.state.broker.check_limits()  # no fill happened before the "crash": back to open
    assert _open(client, h)[0]["status"] == "open"
    _px(app, "XYZ", 89.0)
    app.state.broker.place_order(1, "XYZ", "buy", 10, f"lim:{lid}", trigger=3, limit_price=90, passive=True)
    with app.state.broker.db.tx() as c:
        c.execute("UPDATE limit_orders SET status='filling' WHERE id=?", (lid,))
    app.state.broker.check_limits()  # the fill did happen before the "crash": mark it, don't trade twice
    assert _open(client, h, "all")[0]["status"] == "filled"
    assert client.get("/api/portfolio", headers=h).json()["positions"][0]["qty"] == 10


def test_limit_checked_by_the_background_loop(tmp_path):
    import time
    app = create_app(Settings(db_path=str(tmp_path / "t.db"), admin_token="admin-secret", invite_code="club-code",
                              price_provider="mock", snapshot_seconds=1, snapshot_market_hours_only=False))
    with TestClient(app) as c:
        _px(app, "XYZ", 100.0)
        h = register(c)
        _limit(c, h, "XYZ", "buy", 10, 90)
        _px(app, "XYZ", 85.0)
        for _ in range(40):
            time.sleep(0.25)
            if c.get("/api/portfolio", headers=h).json()["positions"]:
                break
        assert c.get("/api/portfolio", headers=h).json()["positions"][0]["qty"] == 10


def test_reset_clears_limit_orders(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    _limit(client, h, "XYZ", "buy", 1, 90)
    assert client.post("/api/admin/users/1/reset", headers={"X-Admin-Token": "admin-secret"}).status_code == 200
    assert _open(client, h, "all") == []


# ---------------------------------------------------------------- splits and dividends

from datetime import datetime, timedelta, timezone  # noqa: E402


def _days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def _backdate(app, days, user_id=None):
    with app.state.broker.db.tx() as c:
        if user_id is None:
            c.execute("UPDATE orders SET ts = ts - ?", (days * 86400,))
        else:
            c.execute("UPDATE orders SET ts = ts - ? WHERE portfolio_id=?", (days * 86400, user_id))


def _action(app, sym, kind, date, value):
    return app.state.broker.apply_action(sym, kind, date, value, "test")


def test_dividend_is_paid_to_longs_once(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    _order(client, h, "XYZ", "buy", 100)
    _backdate(app, 10)
    cash0 = client.get("/api/portfolio", headers=h).json()["cash"]
    assert _action(app, "XYZ", "dividend", _days_ago(5), 0.5)["accounts"] == 1
    assert client.get("/api/portfolio", headers=h).json()["cash"] == pytest.approx(cash0 + 50)
    assert _action(app, "XYZ", "dividend", _days_ago(5), 0.5) is None  # same action never pays twice
    ev = client.get("/api/corporate-actions", headers=h).json()
    assert len(ev) == 1 and ev[0]["kind"] == "dividend" and ev[0]["amount"] == 50 and ev[0]["qty"] == 100


def test_shorts_owe_dividends(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    _order(client, h, "XYZ", "short", 100)
    _backdate(app, 10)
    cash0 = client.get("/api/portfolio", headers=h).json()["cash"]
    _action(app, "XYZ", "dividend", _days_ago(5), 0.5)
    assert client.get("/api/portfolio", headers=h).json()["cash"] == pytest.approx(cash0 - 50)
    assert client.get("/api/corporate-actions", headers=h).json()[0]["amount"] == -50


def test_only_holders_before_the_ex_date_are_entitled(app, client):
    _px(app, "XYZ", 100.0)
    a, b, c3 = register(client, "aaaa"), register(client, "bbbb"), register(client, "cccc")
    _order(client, a, "XYZ", "buy", 100)   # held going in
    _order(client, b, "XYZ", "buy", 100)   # bought after the ex-date
    _order(client, c3, "XYZ", "buy", 100)  # held going in, sold after the ex-date
    _order(client, c3, "XYZ", "sell", 100)
    _backdate(app, 10, user_id=1)
    with app.state.broker.db.tx() as c:  # user 3: buy 10 days ago, sell 2 days ago
        c.execute("UPDATE orders SET ts = ts - ? WHERE portfolio_id=3 AND side='buy'", (10 * 86400,))
        c.execute("UPDATE orders SET ts = ts - ? WHERE portfolio_id=3 AND side='sell'", (2 * 86400,))
    before = {n: client.get("/api/portfolio", headers=h).json()["cash"] for n, h in (("a", a), ("b", b), ("c", c3))}
    assert _action(app, "XYZ", "dividend", _days_ago(5), 1.0)["accounts"] == 2
    after = {n: client.get("/api/portfolio", headers=h).json()["cash"] for n, h in (("a", a), ("b", b), ("c", c3))}
    assert after["a"] == pytest.approx(before["a"] + 100)
    assert after["b"] == before["b"]  # not entitled
    assert after["c"] == pytest.approx(before["c"] + 100)  # sold afterwards, still gets it


def test_forward_split_adjusts_position_stop_and_limit_orders(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    _order(client, h, "XYZ", "buy", 100, stop_price=90)
    _limit(client, h, "XYZ", "sell", 100, 120)
    _backdate(app, 10)
    with app.state.broker.db.tx() as c:
        c.execute("UPDATE limit_orders SET created_at = created_at - ?", (10 * 86400,))
    eq0 = client.get("/api/portfolio", headers=h).json()["equity"]
    _px(app, "XYZ", 25.0)  # the market is now trading post-split
    assert _action(app, "XYZ", "split", _days_ago(5), 4.0)["accounts"] == 1
    p = client.get("/api/portfolio", headers=h).json()
    pos = p["positions"][0]
    assert pos["qty"] == 400 and pos["avg_cost"] == 25 and pos["stop_price"] == 22.5
    assert p["equity"] == pytest.approx(eq0)  # a split changes nothing about what you own
    lo = _open(client, h)[0]
    assert lo["qty"] == 400 and lo["limit_price"] == 30
    ev = client.get("/api/corporate-actions", headers=h).json()[0]
    assert ev["kind"] == "split" and ev["value"] == 4 and ev["amount"] == 300


def test_reverse_and_short_splits(app, client):
    _px(app, "AAA", 10.0)
    _px(app, "BBB", 100.0)
    h = register(client)
    _order(client, h, "AAA", "buy", 1000)
    _order(client, h, "BBB", "short", 100)
    _backdate(app, 10)
    eq0 = client.get("/api/portfolio", headers=h).json()["equity"]
    _px(app, "AAA", 100.0)
    _px(app, "BBB", 50.0)
    _action(app, "AAA", "split", _days_ago(5), 0.1)  # 1-for-10 reverse split
    _action(app, "BBB", "split", _days_ago(5), 2.0)
    p = client.get("/api/portfolio", headers=h).json()
    pos = {x["symbol"]: x for x in p["positions"]}
    assert pos["AAA"]["qty"] == 100 and pos["AAA"]["avg_cost"] == 100
    assert pos["BBB"]["qty"] == -200 and pos["BBB"]["avg_cost"] == 50
    assert p["equity"] == pytest.approx(eq0)


def test_split_leaves_positions_opened_afterwards_alone(app, client):
    _px(app, "XYZ", 25.0)
    h = register(client)
    _order(client, h, "XYZ", "buy", 100)  # bought today, at the post-split price
    assert _action(app, "XYZ", "split", _days_ago(5), 4.0)["accounts"] == 0
    assert client.get("/api/portfolio", headers=h).json()["positions"][0]["qty"] == 100


def test_actions_from_the_feed_and_retries(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    _order(client, h, "XYZ", "buy", 100)
    _backdate(app, 10)
    broker = app.state.broker
    app.state.provider.set_actions("XYZ", splits=[(_days_ago(6), 2.0)], dividends=[(_days_ago(4), 0.25)])
    good = app.state.provider.get_actions

    def boom(sym):
        raise RuntimeError("feed down")

    app.state.provider.get_actions = boom
    assert broker.apply_feed_actions() == 0  # feed down: nothing applied, nothing marked as done
    app.state.provider.get_actions = good
    assert broker.apply_feed_actions() == 2
    p = client.get("/api/portfolio", headers=h).json()
    assert p["positions"][0]["qty"] == 200
    assert p["cash"] == pytest.approx(100000 - 10000 + 0.25 * 200)  # the dividend is on the post-split shares
    assert broker.apply_feed_actions() == 0
    _backdate(app, 0)


def test_actions_before_the_first_trade_are_ignored(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    _order(client, h, "XYZ", "buy", 100)
    app.state.provider.set_actions("XYZ", splits=[(_days_ago(400), 3.0)], dividends=[(_days_ago(300), 1.0)])
    assert app.state.broker.apply_feed_actions() == 0
    assert client.get("/api/portfolio", headers=h).json()["positions"][0]["qty"] == 100


def test_action_check_is_rate_limited(tmp_path):
    app = make_app(tmp_path, actions_check_seconds=3600)
    with TestClient(app) as c:
        _px(app, "XYZ", 100.0)
        h = register(c)
        _order(c, h, "XYZ", "buy", 10)
        _backdate(app, 10)
        app.state.provider.set_actions("XYZ", dividends=[(_days_ago(5), 1.0)])
        assert app.state.broker.maybe_apply_actions() == 1
        app.state.provider.set_actions("XYZ", dividends=[(_days_ago(5), 1.0), (_days_ago(4), 1.0)])
        assert app.state.broker.maybe_apply_actions() == 0  # too soon to ask again
    app = make_app(tmp_path / "off", actions_check_seconds=0) if (tmp_path / "off").mkdir() is None else None
    with TestClient(app) as c:
        assert app.state.broker.maybe_apply_actions() == 0


def test_admin_can_enter_actions_by_hand(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    _order(client, h, "XYZ", "buy", 100)
    _backdate(app, 10)
    adm = {"X-Admin-Token": "admin-secret"}
    body = {"symbol": "xyz", "kind": "dividend", "ex_date": _days_ago(5), "value": 0.5}
    assert client.post("/api/admin/corporate-actions", json=body).status_code in (401, 403)
    assert client.post("/api/admin/corporate-actions", json=body, headers=h).status_code in (401, 403)  # members can't
    r = client.post("/api/admin/corporate-actions", json=body, headers=adm)
    assert r.status_code == 200 and r.json()["accounts"] == 1
    assert client.post("/api/admin/corporate-actions", json=body, headers=adm).status_code == 409
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
    assert client.post("/api/admin/corporate-actions", json={**body, "ex_date": tomorrow}, headers=adm).status_code == 422
    assert client.post("/api/admin/corporate-actions", json={**body, "ex_date": "garbage!!!"}, headers=adm).status_code == 422
    assert client.post("/api/admin/corporate-actions", json={**body, "kind": "merger"}, headers=adm).status_code == 422
    assert client.post("/api/admin/corporate-actions", json={**body, "kind": "split", "value": 5000}, headers=adm).status_code == 422
    assert client.post("/api/admin/corporate-actions/check", headers=adm).json() == {"applied": 0}


def test_reset_clears_corporate_events(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    _order(client, h, "XYZ", "buy", 10)
    _backdate(app, 10)
    _action(app, "XYZ", "dividend", _days_ago(5), 1.0)
    client.post("/api/admin/users/1/reset", headers={"X-Admin-Token": "admin-secret"})
    assert client.get("/api/corporate-actions", headers=h).json() == []


def test_dividend_then_later_split_and_two_splits_in_a_row(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    _order(client, h, "XYZ", "buy", 100)
    _backdate(app, 20)
    cash0 = client.get("/api/portfolio", headers=h).json()["cash"]
    _action(app, "XYZ", "dividend", _days_ago(12), 0.5)  # paid on the 100 shares held then
    _action(app, "XYZ", "split", _days_ago(8), 2.0)
    _action(app, "XYZ", "split", _days_ago(4), 3.0)
    p = client.get("/api/portfolio", headers=h).json()
    assert p["positions"][0]["qty"] == 600 and p["positions"][0]["avg_cost"] == pytest.approx(100 / 6, abs=1e-3)
    assert p["cash"] == pytest.approx(cash0 + 50)
    # a dividend quoted per today's share (after both splits) is paid on all 600
    _action(app, "XYZ", "dividend", _days_ago(2), 0.1)
    assert client.get("/api/portfolio", headers=h).json()["cash"] == pytest.approx(cash0 + 50 + 60)


# ---------------------------------------------------------------- options (long only)

from app.prices import PriceError, normalize_symbol, option_symbol, parse_option  # noqa: E402


def _osym(days=30, right="C", strike=100, under="XYZ"):
    exp = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")
    return option_symbol(under, exp, right, strike)


def _oq(app, sym, bid, ask, last=None):
    app.state.provider.set_option_quote(sym, bid, ask, last)
    app.state.broker.prices._qcache.clear()
    app.state.broker.prices._cache.clear()


def test_option_symbols_parse_and_validate():
    o = parse_option("AAPL261218C00180000")
    assert o == {"symbol": "AAPL261218C00180000", "underlying": "AAPL", "expiry": "2026-12-18", "right": "C", "strike": 180.0}
    assert option_symbol("aapl", "2026-12-18", "call", 180) == "AAPL261218C00180000"
    assert option_symbol("SPY", "2027-01-15", "P", 452.5) == "SPY270115P00452500"
    assert parse_option("AAPL") is None and parse_option("BRK-B") is None
    assert normalize_symbol("aapl261218c00180000") == "AAPL261218C00180000"
    with pytest.raises(PriceError):
        normalize_symbol("AAPL261340C00180000")  # no such date


def test_buy_and_sell_option_contracts(app, client):
    sym = _osym()
    _oq(app, sym, 2.90, 3.10)
    h = register(client)
    r = _order(client, h, sym, "buy", 2)
    assert r.status_code == 200 and r.json()["price"] == 3.10  # buys pay the ask
    p = client.get("/api/portfolio", headers=h).json()
    pos = p["positions"][0]
    assert p["cash"] == pytest.approx(100000 - 620)  # 2 contracts x 100 shares x 3.10
    assert pos["qty"] == 2 and pos["avg_cost"] == 3.1 and pos["multiplier"] == 100
    assert pos["price"] == 3.0 and pos["market_value"] == 600  # valued at the middle of the market
    assert pos["unrealized_pnl"] == pytest.approx(-20) and pos["option"]["right"] == "C" and "Call" in pos["option"]["label"]
    assert p["equity"] == pytest.approx(99980)
    assert client.get("/api/leaderboard", headers=h).json()[0]["equity"] == pytest.approx(99980)
    _oq(app, sym, 3.90, 4.10)
    s = _order(client, h, sym, "sell", 1).json()
    assert s["price"] == 3.90 and s["realized_pnl"] == pytest.approx(80)  # sells get the bid
    p = client.get("/api/portfolio", headers=h).json()
    assert p["positions"][0]["qty"] == 1 and p["cash"] == pytest.approx(100000 - 620 + 390)


def test_option_order_rules(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as c:
        sym = _osym()
        _oq(app, sym, 2.90, 3.10)
        h = register(c)
        assert _order(c, h, sym, "buy", 1.5).status_code == 422  # whole contracts only
        for side in ("short", "cover"):
            r = _order(c, h, sym, side, 1)
            assert r.status_code == 422 and "writing" in r.json()["detail"]
        assert _order(c, h, sym, "sell", 1).status_code == 400  # nothing to sell
        assert _order(c, h, sym, "buy", 1000).status_code == 400  # $310k
        assert _order(c, h, _osym(days=-1), "buy", 1).status_code == 422  # already expired
        assert _limit(c, h, sym, "short", 1, 3.0).status_code == 422
        assert c.get("/api/portfolio", headers=h).json()["positions"] == []
    app = make_app(tmp_path / "off", allow_options=False) if (tmp_path / "off").mkdir() is None else None
    with TestClient(app) as c:
        h = register(c)
        assert _order(c, h, sym, "buy", 1).status_code == 403
        assert c.get(f"/api/quote/{sym}", headers=h).status_code == 403
        assert c.get("/api/options/XYZ/expiries", headers=h).status_code == 403


def test_option_fills_without_a_two_sided_market(tmp_path):
    app = make_app(tmp_path, slippage_bps=100)
    with TestClient(app) as c:
        sym = _osym()
        h = register(c)
        _oq(app, sym, 0, 0, 1.00)  # after hours: only a last trade
        assert _order(c, h, sym, "buy", 1).json()["price"] == pytest.approx(1.01)
        _oq(app, sym, 0, 0.05, 0.50)  # an offer but no bid
        assert _order(c, h, sym, "sell", 1).json()["price"] == 0.01
        _oq(app, sym, 0, 0, 0)
        assert _order(c, h, sym, "buy", 1).status_code == 404  # no price at all


def test_option_limit_orders_compare_with_the_ask_and_bid(app, client):
    sym = _osym()
    _oq(app, sym, 3.00, 3.20)
    h = register(client)
    r = _limit(client, h, sym, "buy", 1, 3.00).json()
    assert r["status"] == "open"  # the mid is 3.10 but you would pay 3.20
    assert app.state.broker.check_limits() == 0
    _oq(app, sym, 2.90, 3.00)
    assert app.state.broker.check_limits() == 1
    o = client.get("/api/orders", headers=h).json()[0]
    assert o["price"] == 3.00 and o["reason"] == "limit"
    _oq(app, sym, 3.10, 3.20)
    f = _limit(client, h, sym, "buy", 1, 3.50).json()
    assert f["status"] == "filled" and f["price"] == 3.20  # fills at the ask, not at your limit
    assert client.get("/api/portfolio", headers=h).json()["positions"][0]["qty"] == 2


def test_option_stop(app, client):
    sym = _osym()
    _oq(app, sym, 2.90, 3.00)
    h = register(client)
    assert _order(client, h, sym, "buy", 1, stop_price=2.95).status_code == 422  # not below the market
    assert _order(client, h, sym, "buy", 1, stop_price=2.0).status_code == 200
    _oq(app, sym, 1.85, 1.95)
    assert app.state.broker.check_stops() == 1
    o = client.get("/api/orders", headers=h).json()[0]
    assert o["side"] == "sell" and o["reason"] == "stop" and o["price"] == 1.85
    assert client.get("/api/portfolio", headers=h).json()["positions"] == []


def test_expired_options_are_cash_settled(app, client):
    day = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
    call, put = option_symbol("XYZ", day, "C", 100), option_symbol("XYZ", day, "P", 100)
    live = _osym(days=2)
    h = register(client)
    broker = app.state.broker
    with broker.db.tx() as c:
        for s, q, a in ((call, 2, 4.0), (put, 1, 2.0), (live, 1, 1.0)):
            c.execute("INSERT INTO positions (portfolio_id, symbol, qty, avg_cost) VALUES (1,?,?,?)", (s, q, a))
        c.execute("INSERT INTO limit_orders (portfolio_id, symbol, side, qty, limit_price, created_at) VALUES (1,?,?,?,?,?)",
                  (call, "buy", 1, 1.0, time.time()))
    app.state.provider.set_close("XYZ", day, 110.0)
    app.state.provider.get_close, good = (lambda s, d: None), app.state.provider.get_close
    assert broker.settle_expired_options() == 0  # no closing price yet: wait
    app.state.provider.get_close = good
    broker._settle_tried.clear()
    assert broker.settle_expired_options() == 2
    p = client.get("/api/portfolio", headers=h).json()
    assert [x["symbol"] for x in p["positions"]] == [live]  # the live contract is untouched
    assert p["cash"] == pytest.approx(100000 + 2 * 10 * 100)  # call worth 10 a share, put worthless
    orders = {o["symbol"]: o for o in client.get("/api/orders", headers=h).json()}
    assert orders[call]["side"] == "sell" and orders[call]["price"] == 10 and orders[call]["reason"] == "expiry"
    assert orders[call]["realized_pnl"] == pytest.approx(1200) and orders[call]["forced"] is False
    assert orders[put]["price"] == 0 and orders[put]["realized_pnl"] == pytest.approx(-200)
    assert _open(client, h, "all")[0]["status"] == "expired"  # the resting order on it is gone
    assert broker.settle_expired_options() == 0  # once only
    assert client.get("/api/leaderboard", headers=h).json()[0]["win_rate_pct"] == 50.0


import time  # noqa: E402


def test_option_chain_and_quote_endpoints(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    assert client.get("/api/options/XYZ/expiries").status_code == 401
    exp = client.get("/api/options/xyz/expiries", headers=h).json()
    assert exp["underlying"] == "XYZ" and len(exp["expiries"]) == 4
    ch = client.get(f"/api/options/XYZ?expiry={exp['expiries'][0]}", headers=h).json()
    assert ch["multiplier"] == 100 and ch["underlying_price"] == 100.0
    strikes = [r["strike"] for r in ch["calls"]]
    assert strikes == sorted(strikes) and 100.0 in strikes and len(ch["puts"]) == len(strikes)
    row = [r for r in ch["calls"] if r["strike"] == 100.0][0]
    assert parse_option(row["symbol"])["strike"] == 100.0 and row["ask"] > row["bid"] >= 0 and row["in_the_money"] is False
    assert [r for r in ch["calls"] if r["strike"] == 90.0][0]["in_the_money"] is True
    q = client.get(f"/api/quote/{row['symbol']}", headers=h).json()
    assert q["multiplier"] == 100 and q["bid"] == row["bid"] and q["option"]["right"] == "C"
    assert client.get("/api/options/XYZ?expiry=2030-01-01", headers=h).status_code == 404
    assert client.get("/api/options/XYZ?expiry=soon", headers=h).status_code == 422
    assert client.get(f"/api/options/{row['symbol']}/expiries", headers=h).status_code == 422
    assert client.get("/api/options/%5EGSPC/expiries", headers=h).status_code == 422
    # the offline mock prices contracts too, so a chain row can be bought as is
    assert _order(client, h, row["symbol"], "buy", 1).status_code == 200


def test_options_are_left_out_of_the_split_and_dividend_feed(app, client):
    sym = _osym()
    _oq(app, sym, 2.9, 3.1)
    _px(app, "XYZ", 100.0)
    h = register(client)
    _order(client, h, sym, "buy", 1)
    _order(client, h, "XYZ", "buy", 1)
    asked = []
    app.state.provider.get_actions = lambda s: asked.append(s) or {"splits": [], "dividends": []}
    app.state.broker.apply_feed_actions()
    assert asked == ["XYZ"]


# ---------------------------------------------------------------- portfolios and competitions

import time as _time


def _comp(client, name="Fall Cup", **kw):
    r = client.post("/api/admin/competitions", json={"name": name, **kw}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def _join(client, h, code):
    return client.post("/api/competitions/join", json={"code": code}, headers=h)


def test_new_user_has_a_main_portfolio(client):
    h = register(client)
    pf = client.get("/api/portfolios", headers=h).json()
    assert [(p["portfolio"], p["is_default"], p["equity"]) for p in pf] == [("Main", True, 100000)]
    me = client.get("/api/me", headers=h).json()
    assert me["portfolio"] == "Main" and me["competition"] is None and me["trading_open"] is True


def test_extra_portfolios_are_separate_accounts(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    r = client.post("/api/portfolios", json={"name": "Practice"}, headers=h)
    assert r.status_code == 200 and r.json()["portfolio"] == "Practice" and r.json()["cash"] == 100000
    # the same stock in each portfolio, picked by body field, header and query string
    assert _order(client, h, "XYZ", "buy", 10, portfolio="Practice").status_code == 200
    assert client.post("/api/orders", json={"symbol": "XYZ", "side": "buy", "qty": 3},
                       headers={**h, "X-Portfolio": "Practice"}).status_code == 200
    assert client.post("/api/orders?portfolio=Practice", json={"symbol": "XYZ", "side": "buy", "qty": 2},
                       headers=h).status_code == 200
    assert _order(client, h, "XYZ", "buy", 1).status_code == 200  # no selector: the main portfolio
    main = client.get("/api/portfolio", headers=h).json()
    prac = client.get("/api/portfolio?portfolio=Practice", headers=h).json()
    assert main["positions"][0]["qty"] == 1 and main["cash"] == 99900
    assert prac["positions"][0]["qty"] == 15 and prac["cash"] == 98500
    assert len(client.get("/api/orders", headers=h).json()) == 1
    assert len(client.get("/api/orders?portfolio=Practice", headers=h).json()) == 3
    # by id works too, and the curves are separate
    pid = prac["portfolio_id"]
    assert client.get(f"/api/portfolio?portfolio={pid}", headers=h).json()["portfolio"] == "Practice"
    assert client.get("/api/equity?portfolio=Practice", headers=h).json()[-1]["equity"] == 100000


def test_portfolio_rules(app, client):
    h, other = register(client), register(client, "bobby")
    assert client.post("/api/portfolios", json={"name": "A"}, headers=h).status_code == 200
    assert client.post("/api/portfolios", json={"name": "a"}, headers=h).status_code == 409  # same name, any case
    assert client.post("/api/portfolios", json={"name": "Main"}, headers=h).status_code == 409
    assert client.post("/api/portfolios", json={"name": "123"}, headers=h).status_code == 422  # would look like an id
    assert client.post("/api/portfolios", json={"name": "bad/name"}, headers=h).status_code == 422
    assert client.post("/api/portfolios", json={"name": "A"}, headers=other).status_code == 200  # names are per member
    # someone else's portfolio is invisible
    other_id = client.get("/api/portfolios", headers=other).json()[0]["portfolio_id"]
    assert client.get(f"/api/portfolio?portfolio={other_id}", headers=h).status_code == 404
    assert client.get("/api/portfolio?portfolio=nope", headers=h).status_code == 404
    assert _order(client, h, "XYZ", "buy", 1, portfolio=str(other_id)).status_code == 404
    # delete: extras yes, main no
    assert client.delete("/api/portfolios/A", headers=h).status_code == 200
    assert client.delete("/api/portfolios/Main", headers=h).status_code == 409
    assert [p["portfolio"] for p in client.get("/api/portfolios", headers=h).json()] == ["Main"]


def test_portfolio_limit(app, client):
    app.state.settings.max_portfolios = 2
    h = register(client)
    assert client.post("/api/portfolios", json={"name": "A"}, headers=h).status_code == 200
    assert client.post("/api/portfolios", json={"name": "B"}, headers=h).status_code == 200
    assert client.post("/api/portfolios", json={"name": "C"}, headers=h).status_code == 429


def test_stops_limits_and_dividends_are_per_portfolio(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    client.post("/api/portfolios", json={"name": "P2"}, headers=h)
    _order(client, h, "XYZ", "buy", 10)
    _order(client, h, "XYZ", "buy", 10, portfolio="P2", stop_price=90)
    assert client.get("/api/portfolio", headers=h).json()["positions"][0]["stop_price"] is None
    _px(app, "XYZ", 85.0)
    assert app.state.broker.check_stops() == 1  # only P2 had a stop
    assert client.get("/api/portfolio", headers=h).json()["positions"][0]["qty"] == 10
    assert client.get("/api/portfolio?portfolio=P2", headers=h).json()["positions"] == []
    # a resting limit order belongs to one portfolio and can't be cancelled from another
    lo = _limit(client, h, "XYZ", "buy", 1, 50, portfolio="P2").json()
    assert client.get("/api/open-orders", headers=h).json() == []
    assert len(client.get("/api/open-orders?portfolio=P2", headers=h).json()) == 1
    assert client.delete(f"/api/open-orders/{lo['id']}", headers=h).status_code == 404
    assert client.delete(f"/api/open-orders/{lo['id']}?portfolio=P2", headers=h).status_code == 200
    # a dividend is paid to each portfolio on what that portfolio held
    _order(client, h, "XYZ", "buy", 5, portfolio="P2")
    _backdate(app, 10)
    ex = _time.strftime("%Y-%m-%d", _time.gmtime(_time.time() - 3 * 86400))
    app.state.broker.apply_action("XYZ", "dividend", ex, 1.0, "test")
    assert client.get("/api/portfolio", headers=h).json()["cash"] == pytest.approx(100000 - 1000 + 10)
    assert client.get("/api/portfolio?portfolio=P2", headers=h).json()["cash"] == pytest.approx(100000 - 1000 + 850 - 425 + 5)
    assert len(client.get("/api/corporate-actions", headers=h).json()) == 1
    assert len(client.get("/api/corporate-actions?portfolio=P2", headers=h).json()) == 1


def test_old_users_keep_everything_through_the_upgrade(tmp_path):
    import sqlite3
    from app.db import Database
    from app.broker import Broker, hash_key
    from app.prices import MockProvider, PriceService

    path = str(tmp_path / "pre.db")
    con = sqlite3.connect(path)
    con.executescript(LEGACY_SCHEMA)
    for i, name in enumerate(("amy", "ben"), 1):
        con.execute("INSERT INTO users (id, username, api_key_hash, cash, starting_cash, peak_equity, created_at)"
                    " VALUES (?,?,?,?,?,?,?)", (i, name, hash_key(f"k{i}"), 90000 + i, 100000, 100000, 1))
        con.execute("INSERT INTO positions (user_id, symbol, qty, avg_cost) VALUES (?, 'XYZ', ?, 100)", (i, 10 * i))
        con.execute("INSERT INTO orders (user_id, client_order_id, symbol, side, qty, price, ts) VALUES (?, 'o1', 'XYZ', 'buy', ?, 100, 2)", (i, 10 * i))
        con.execute("INSERT INTO limit_orders (user_id, symbol, side, qty, limit_price, created_at) VALUES (?, 'XYZ', 'buy', 1, 50, 3)", (i,))
        con.execute("INSERT INTO corp_events (user_id, symbol, kind, ex_date, qty, value, amount, ts) VALUES (?, 'XYZ', 'dividend', '2026-01-01', 1, 1, 1, 4)", (i,))
    con.commit(); con.close()
    s = Settings(db_path=path, price_provider="mock", snapshot_seconds=0, admin_token="a")
    prov = MockProvider(); prov.set_price("XYZ", 100.0)
    b = Broker(Database(path), PriceService(prov, 15), s)
    for i in (1, 2):
        pid = b.resolve_portfolio(i)
        assert pid == i
        p = b.portfolio(pid)
        assert p["cash"] == 90000 + i and p["positions"][0]["qty"] == 10 * i and p["portfolio"] == "Main"
        assert len(b.orders(pid)) == 1 and len(b.list_limit_orders(pid)) == 1 and len(b.corp_events(pid)) == 1
    assert b.user_by_key("k1")["username"] == "amy"
    b.place_order(1, "XYZ", "buy", 1)  # trading works on the upgraded database
    b.create_portfolio(1, "Extra")
    assert b.create_user("cara")[0] == 3
    # new ids don't collide with the carried-over ones
    assert sorted(r["portfolio_id"] for r in b.summaries(scope="all")) == [1, 2, 3, 4]


def test_competition_join_codes_and_leaderboards(app, client):
    h, hb, hc = register(client), register(client, "bobby"), register(client, "cara")
    comp = _comp(client, starting_cash=50000)
    assert len(comp["code"]) == 8 and comp["status"] == "active" and comp["starting_cash"] == 50000
    assert _join(client, h, "WRONG123").status_code == 404
    r = _join(client, h, comp["code"].lower())  # codes aren't case sensitive
    assert r.status_code == 200 and r.json()["competition"]["members"] == 1
    assert _join(client, h, comp["code"]).status_code == 409  # already in
    assert client.post(f"/api/admin/competitions/{comp['id']}/members", json={"username": "BOBBY"}, headers=ADMIN).status_code == 200
    assert client.post(f"/api/admin/competitions/{comp['id']}/members", json={"username": "bobby"}, headers=ADMIN).status_code == 409
    assert client.post(f"/api/admin/competitions/{comp['id']}/members", json={"username": "nobody"}, headers=ADMIN).status_code == 404
    # members get a fresh portfolio with the competition's cash; main is untouched
    pf = {p["portfolio"]: p for p in client.get("/api/portfolios", headers=h).json()}
    assert set(pf) == {"Main", "Fall Cup"} and pf["Fall Cup"]["cash"] == 50000 and pf["Main"]["cash"] == 100000
    assert client.get("/api/portfolio?portfolio=Fall Cup", headers=h).json()["competition"]["name"] == "Fall Cup"
    mine = client.get("/api/competitions", headers=h).json()
    assert [(c["name"], c["portfolio"], c["rank"]) for c in mine] == [("Fall Cup", "Fall Cup", mine[0]["rank"])]
    assert "code" not in mine[0]  # only admins see codes
    assert client.get("/api/competitions", headers=hc).json() == []
    # leaderboards: members only, competition portfolios stay off the main one
    _px(app, "XYZ", 100.0)
    _order(client, h, "XYZ", "buy", 100, portfolio="Fall Cup")
    _px(app, "XYZ", 110.0)
    lb = client.get(f"/api/competitions/{comp['id']}/leaderboard", headers=h).json()
    assert [(r["username"], r["rank"]) for r in lb] == [("alice", 1), ("bobby", 2)]
    assert lb[0]["return_pct"] == pytest.approx(2.0) and lb[0]["equity"] == 51000
    assert client.get(f"/api/competitions/{comp['id']}/leaderboard", headers=hc).status_code == 404
    assert client.get(f"/api/competitions/{comp['id']}/curves", headers=hb).status_code == 200
    assert client.get(f"/api/competitions/{comp['id']}/curves", headers=hc).status_code == 404
    main_lb = client.get("/api/leaderboard", headers=h).json()
    assert sorted(r["username"] for r in main_lb) == ["alice", "bobby", "cara"] and all(r["portfolio"] == "Main" for r in main_lb)
    # the admin panel can look at any one competition, or everything
    adm = client.get(f"/api/admin/portfolios?scope={comp['id']}", headers=ADMIN).json()
    assert len(adm) == 2 and len(client.get("/api/admin/portfolios", headers=ADMIN).json()) == 3
    assert len(client.get("/api/admin/portfolios?scope=all", headers=ADMIN).json()) == 5
    assert client.get("/api/admin/portfolios?scope=bogus", headers=ADMIN).status_code == 422
    curves = client.get(f"/api/admin/curves?scope={comp['id']}", headers=ADMIN).json()
    assert sorted(c["username"] for c in curves) == ["alice", "bobby"]
    assert len(client.get("/api/admin/competitions", headers=ADMIN).json()) == 1
    assert client.get("/api/admin/competitions", headers=h).status_code in (401, 403)


def test_competition_name_clash_and_validation(client):
    h = register(client)
    client.post("/api/portfolios", json={"name": "Fall Cup"}, headers=h)  # same name as the competition
    comp = _comp(client)
    assert _join(client, h, comp["code"]).status_code == 200
    assert {p["portfolio"] for p in client.get("/api/portfolios", headers=h).json()} == {"Main", "Fall Cup", "Fall Cup (competition)"}
    assert client.post("/api/admin/competitions", json={"name": "fall cup"}, headers=ADMIN).status_code == 409
    assert client.post("/api/admin/competitions", json={"name": "42"}, headers=ADMIN).status_code == 422
    now = _time.time()
    assert client.post("/api/admin/competitions", json={"name": "X", "end_ts": now - 5}, headers=ADMIN).status_code == 422
    assert client.post("/api/admin/competitions", json={"name": "X", "start_ts": now + 100, "end_ts": now + 50}, headers=ADMIN).status_code == 422
    assert client.post("/api/admin/competitions", json={"name": "X", "starting_cash": 0}, headers=ADMIN).status_code == 422
    assert client.post("/api/admin/competitions", json={"name": "Y"}, headers=h).status_code in (401, 403)


def test_competition_rules(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    now = _time.time()
    soon = _comp(client, "Later", start_ts=now + 3600)
    assert soon["status"] == "upcoming"
    _join(client, h, soon["code"])
    r = _order(client, h, "XYZ", "buy", 1, portfolio="Later")
    assert r.status_code == 403 and "hasn't started" in r.json()["detail"]
    assert _limit(client, h, "XYZ", "buy", 1, 50, portfolio="Later").status_code == 403
    assert client.get("/api/portfolio?portfolio=Later", headers=h).json()["trading_open"] is False
    with app.state.broker.db.tx() as c:  # time passes: it opens
        c.execute("UPDATE competitions SET start_ts=? WHERE name='Later'", (now - 1,))
    assert _order(client, h, "XYZ", "buy", 1, portfolio="Later").status_code == 200

    strict = _comp(client, "Strict", allow_options=False, allow_shorting=False)
    _join(client, h, strict["code"])
    p = client.get("/api/portfolio?portfolio=Strict", headers=h).json()
    assert p["shorting_enabled"] is False and p["options_enabled"] is False
    r = _order(client, h, "XYZ", "short", 1, portfolio="Strict")
    assert r.status_code == 403 and "short selling" in r.json()["detail"]
    assert _limit(client, h, "XYZ", "short", 1, 150, portfolio="Strict").status_code == 403
    opt = "XYZ" + _time.strftime("%y%m%d", _time.gmtime(now + 30 * 86400)) + "C00100000"
    r = _order(client, h, opt, "buy", 1, portfolio="Strict")
    assert r.status_code == 403 and "options" in r.json()["detail"]
    assert _order(client, h, "XYZ", "buy", 1, portfolio="Strict").status_code == 200  # plain buys are fine
    assert _order(client, h, "XYZ", "short", 1).status_code == 200  # main portfolio is not affected


def test_ending_a_competition_freezes_it(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    comp = _comp(client, "Sprint", starting_cash=10000, end_ts=_time.time() + 3600)
    _join(client, h, comp["code"])
    _order(client, h, "XYZ", "buy", 50, portfolio="Sprint", stop_price=80)
    _limit(client, h, "XYZ", "buy", 10, 50, portfolio="Sprint")
    assert len(client.get("/api/open-orders?portfolio=Sprint", headers=h).json()) == 1
    _px(app, "XYZ", 120.0)
    assert client.get("/api/portfolio?portfolio=Sprint", headers=h).json()["equity"] == 11000

    with app.state.broker.db.tx() as c:  # the clock passes the end
        c.execute("UPDATE competitions SET end_ts=? WHERE name='Sprint'", (_time.time() - 1,))
    assert app.state.broker.finalize_competitions() == 1
    assert app.state.broker.finalize_competitions() == 0  # only once
    _px(app, "XYZ", 60.0)  # later moves no longer count
    p = client.get("/api/portfolio?portfolio=Sprint", headers=h).json()
    assert p["frozen"] is True and p["equity"] == 11000 and p["return_pct"] == pytest.approx(10.0)
    assert p["trading_open"] is False and p["competition"]["status"] == "ended"
    lb = client.get(f"/api/competitions/{comp['id']}/leaderboard", headers=h).json()
    assert lb[0]["equity"] == 11000 and lb[0]["frozen"] is True
    # no trading, no triggers, no more snapshots, resting orders were cancelled
    r = _order(client, h, "XYZ", "sell", 1, portfolio="Sprint")
    assert r.status_code == 403 and "ended" in r.json()["detail"]
    assert _limit(client, h, "XYZ", "buy", 1, 10, portfolio="Sprint").status_code == 403
    assert app.state.broker.check_stops() == 0 and app.state.broker.check_limits() == 0
    assert client.get("/api/open-orders?portfolio=Sprint", headers=h).json() == []
    assert client.get("/api/open-orders?status=all&portfolio=Sprint", headers=h).json()[0]["status"] == "cancelled"
    n = len(client.get("/api/equity?portfolio=Sprint", headers=h).json())
    app.state.broker.snapshot_all()
    assert len(client.get("/api/equity?portfolio=Sprint", headers=h).json()) == n
    assert client.get("/api/equity?portfolio=Sprint", headers=h).json()[-1]["equity"] == 11000
    # joining after the end is refused, and the main portfolio still trades
    h2 = register(client, "bobby")
    assert _join(client, h2, comp["code"]).status_code == 409
    assert _order(client, h, "XYZ", "buy", 1).status_code == 200


def test_admin_can_end_a_competition_early(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    comp = _comp(client, "Open ended")
    _join(client, h, comp["code"])
    _order(client, h, "XYZ", "buy", 10, portfolio="Open ended")
    r = client.post(f"/api/admin/competitions/{comp['id']}/end", headers=ADMIN)
    assert r.status_code == 200 and r.json()["status"] == "ended"
    assert client.post(f"/api/admin/competitions/{comp['id']}/end", headers=ADMIN).status_code == 409
    assert client.get("/api/portfolio?portfolio=Open ended", headers=h).json()["frozen"] is True
    assert _order(client, h, "XYZ", "sell", 1, portfolio="Open ended").status_code == 403
    assert client.post("/api/admin/competitions/999/end", headers=ADMIN).status_code == 404


def test_admin_can_remove_members_reset_and_delete(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client)
    comp = _comp(client, "Temp")
    _join(client, h, comp["code"])
    _order(client, h, "XYZ", "buy", 10, portfolio="Temp")
    pid = client.get("/api/portfolio?portfolio=Temp", headers=h).json()["portfolio_id"]
    assert client.delete("/api/portfolios/Temp", headers=h).status_code == 409  # members can't leave on their own
    r = client.post(f"/api/admin/portfolios/{pid}/reset", headers=ADMIN)
    assert r.status_code == 200
    p = client.get("/api/portfolio?portfolio=Temp", headers=h).json()
    assert p["cash"] == 100000 and p["positions"] == []
    assert client.delete(f"/api/admin/competitions/{comp['id']}/members/{pid + 99}", headers=ADMIN).status_code == 404
    assert client.delete(f"/api/admin/competitions/{comp['id']}/members/{pid}", headers=ADMIN).status_code == 200
    assert client.get("/api/portfolio?portfolio=Temp", headers=h).status_code == 404
    _join(client, h, comp["code"])  # rejoining gives a fresh portfolio
    assert client.delete(f"/api/admin/competitions/{comp['id']}", headers=ADMIN).status_code == 200
    assert client.get("/api/competitions", headers=h).json() == []
    assert {p["portfolio"] for p in client.get("/api/portfolios", headers=h).json()} == {"Main"}
    assert client.delete(f"/api/admin/competitions/{comp['id']}", headers=ADMIN).status_code == 404
    # the old per-user reset still resets the main portfolio
    _order(client, h, "XYZ", "buy", 5)
    uid = client.get("/api/admin/portfolios", headers=ADMIN).json()[0]["user_id"]
    assert client.post(f"/api/admin/users/{uid}/reset", headers=ADMIN).status_code == 200
    assert client.get("/api/portfolio", headers=h).json()["positions"] == []


def test_every_page_is_served(client):
    for path, marker in [("/", "Place a trade"), ("/activity", "Equity curve"), ("/competitions", "Join competition"),
                         ("/admin", "Return by trader"), ("/admin/competitions", "Create competition"), ("/admin/codes", "Make sign-up codes"), ("/admin/users", "Search username")]:
        r = client.get(path)
        assert r.status_code == 200 and marker in r.text, path
        assert "/static/common.js" in r.text


# ---------------------------------------------------------------- sign-up codes

def _codes(client, **body):
    r = client.post("/api/admin/invites", json=body, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def _signup(client, name, code):
    return client.post("/api/register", json={"username": name, "invite_code": code})


def test_single_use_code_works_once(client):
    [c] = _codes(client, label="for dana")
    assert len(c["code"]) == 14 and c["code"].count("-") == 2 and c["status"] == "unused" and c["label"] == "for dana"
    r = _signup(client, "dana", c["code"].lower().replace("-", " "))  # typed sloppily: any case, spaces for dashes
    assert r.status_code == 200 and r.json()["api_key"].startswith("pt_")
    r = _signup(client, "eve", c["code"])
    assert r.status_code == 403 and "already been used" in r.json()["detail"]
    row = client.get("/api/admin/invites", headers=ADMIN).json()[0]
    assert row["status"] == "used" and row["uses"] == 1 and row["used_by"] == ["dana"]


def test_new_account_from_a_code_can_trade(client, app):
    [c] = _codes(client)
    key = _signup(client, "dana", c["code"]).json()["api_key"]
    p = client.get("/api/me", headers={"X-API-Key": key}).json()
    assert p["username"] == "dana" and p["portfolio"] == "Main" and p["cash"] == 100000


def test_bad_codes_are_refused(client):
    assert _signup(client, "dana", "NOPE").status_code == 403
    assert _signup(client, "dana", "").status_code == 403
    assert _signup(client, "dana", "----").status_code == 403
    assert _signup(client, "bad name", "club-code").status_code == 422  # the club-wide code still works for valid names
    assert _signup(client, "dana", "CLUB-CODE").status_code == 403       # but it is matched exactly, not loosely
    assert _signup(client, "dana", "club-code").status_code == 200


def test_a_taken_username_does_not_burn_the_code(client):
    register(client, "dana")
    [c] = _codes(client)
    assert _signup(client, "dana", c["code"]).status_code == 409
    assert client.get("/api/admin/invites", headers=ADMIN).json()[0]["uses"] == 0
    assert _signup(client, "dana2", c["code"]).status_code == 200


def test_codes_can_be_limited_expired_and_revoked(client, app):
    multi = _codes(client, max_uses=2)[0]
    assert _signup(client, "u_one", multi["code"]).status_code == 200
    assert client.get("/api/admin/invites", headers=ADMIN).json()[0]["status"] == "partly used"
    assert _signup(client, "u_two", multi["code"]).status_code == 200
    assert _signup(client, "u_three", multi["code"]).status_code == 403

    soon = _codes(client, expires_at=_time.time() + 3600)[0]
    with app.state.broker.db.tx() as c:
        c.execute("UPDATE invite_codes SET expires_at=? WHERE id=?", (_time.time() - 1, soon["id"]))
    r = _signup(client, "late", soon["code"])
    assert r.status_code == 403 and "expired" in r.json()["detail"]
    assert next(x for x in client.get("/api/admin/invites", headers=ADMIN).json() if x["id"] == soon["id"])["status"] == "expired"

    gone = _codes(client)[0]
    assert client.delete(f"/api/admin/invites/{gone['id']}", headers=ADMIN).status_code == 200
    r = _signup(client, "nope", gone["code"])
    assert r.status_code == 403 and "cancelled" in r.json()["detail"]
    assert client.delete("/api/admin/invites/9999", headers=ADMIN).status_code == 404


def test_code_batches_and_admin_only(client):
    batch = _codes(client, count=5, label="week 1")
    assert len({c["code"] for c in batch}) == 5 and all(c["status"] == "unused" for c in batch)
    assert client.post("/api/admin/invites", json={"count": 0}, headers=ADMIN).status_code == 422
    assert client.post("/api/admin/invites", json={"count": 201}, headers=ADMIN).status_code == 422
    assert client.post("/api/admin/invites", json={"expires_at": 5}, headers=ADMIN).status_code == 422  # in the past
    h = register(client)
    assert client.get("/api/admin/invites", headers=h).status_code in (401, 403, 422)
    assert client.post("/api/admin/invites", json={}, headers={"X-Admin-Token": "wrong"}).status_code == 401


def test_the_club_wide_code_can_be_switched_off(tmp_path):
    with TestClient(make_app(tmp_path, invite_code="")) as c:
        assert _signup(c, "dana", "").status_code == 403
        assert _signup(c, "dana", "club-code").status_code == 403
        [code] = _codes(c)
        assert _signup(c, "dana", code["code"]).status_code == 200


def test_codes_survive_the_portfolio_upgrade(tmp_path):
    # a database that was already converted to portfolios (before codes existed) gains the code tables on start
    import sqlite3
    from app.db import Database
    path = str(tmp_path / "up.db")
    Database(path)
    con = sqlite3.connect(path); con.execute("DROP TABLE invite_uses"); con.execute("DROP TABLE invite_codes"); con.commit(); con.close()
    db = Database(path)
    with db.read() as c:
        assert {"invite_codes", "invite_uses"} <= {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_admin_user_list(app, client):
    _px(app, "XYZ", 100.0)
    [c] = _codes(client, label="for dana")
    key = _signup(client, "dana", c["code"]).json()["api_key"]
    register(client, "bobby")                                  # signed up with the club-wide code
    client.post("/api/admin/users", json={"username": "cara"}, headers=ADMIN)  # made by an admin
    h = {"X-API-Key": key}
    comp = _comp(client, "Fall Cup")
    _join(client, h, comp["code"])
    client.post("/api/portfolios", json={"name": "Practice"}, headers=h)
    _order(client, h, "XYZ", "buy", 10)
    _order(client, h, "XYZ", "buy", 5, portfolio="Practice")
    _px(app, "XYZ", 110.0)

    rows = client.get("/api/admin/users", headers=ADMIN).json()
    assert [r["username"] for r in rows] == ["cara", "bobby", "dana"]          # newest first
    dana = rows[2]
    assert dana["portfolios"] == 3 and dana["competitions"] == ["Fall Cup"] and dana["trades"] == 2
    assert dana["main_equity"] == 100100 and dana["main_return_pct"] == pytest.approx(0.1)
    assert dana["signed_up_with"] == c["code"] and dana["invite_label"] == "for dana" and dana["last_trade_at"] is not None
    assert rows[1]["signed_up_with"] is None and rows[1]["last_trade_at"] is None and rows[1]["trades"] == 0
    assert rows[0]["portfolios"] == 1 and rows[0]["main_equity"] == 100000
    # the reset button's target is the main portfolio
    assert client.post(f"/api/admin/portfolios/{dana['main_portfolio_id']}/reset", headers=ADMIN).status_code == 200
    assert client.get("/api/portfolio", headers=h).json()["positions"] == []
    assert client.get("/api/portfolio?portfolio=Practice", headers=h).json()["positions"][0]["qty"] == 5
    assert client.get("/api/admin/users", headers=h).status_code in (401, 403, 422)


def test_member_rotates_own_key(app, client):
    h = register(client, "rina")
    old = h["X-API-Key"]
    r = client.post("/api/rotate-key", headers=h)
    assert r.status_code == 200
    new = r.json()["api_key"]
    assert new != old and new.startswith("pt_")
    assert client.get("/api/portfolio", headers=h).status_code == 401           # old key is dead
    h2 = {"X-API-Key": new}
    assert client.get("/api/portfolio", headers=h2).status_code == 200          # same account, new key
    assert client.post("/api/rotate-key", headers=h).status_code == 401
    assert client.post("/api/rotate-key").status_code in (401, 422)


def test_admin_deletes_user(app, client):
    _px(app, "XYZ", 100.0)
    h = register(client, "gone")
    keep = register(client, "stay")
    comp = _comp(client, "Cup")
    _join(client, h, comp["code"])
    _order(client, h, "XYZ", "buy", 5)
    client.post("/api/portfolios", json={"name": "Side"}, headers=h)
    _order(client, keep, "XYZ", "buy", 3)
    rows = client.get("/api/admin/users", headers=ADMIN).json()
    uid = next(r["user_id"] for r in rows if r["username"] == "gone")
    assert client.delete(f"/api/admin/users/{uid}", headers=h).status_code in (401, 403, 422)
    assert client.delete(f"/api/admin/users/{uid}", headers=ADMIN).json()["deleted"] == "gone"
    assert client.get("/api/portfolio", headers=h).status_code == 401           # key no longer works
    assert [r["username"] for r in client.get("/api/admin/users", headers=ADMIN).json()] == ["stay"]
    with app.state.broker.db.read() as c:
        for t in ("portfolios", "orders", "positions", "snapshots"):
            assert c.execute(f"SELECT COUNT(*) FROM {t} WHERE portfolio_id NOT IN (SELECT id FROM portfolios)" if t != "portfolios" else "SELECT COUNT(*) FROM portfolios WHERE user_id=?", (uid,) if t == "portfolios" else ()).fetchone()[0] == 0
    assert client.get("/api/portfolio", headers=keep).json()["positions"][0]["qty"] == 3
    assert client.delete(f"/api/admin/users/{uid}", headers=ADMIN).status_code == 404
    client.post("/api/admin/users", json={"username": "gone"}, headers=ADMIN)    # the name is free again


def test_admin_new_key_for_lost_key(app, client):
    h = register(client, "lost")
    uid = client.get("/api/admin/users", headers=ADMIN).json()[0]["user_id"]
    assert client.post(f"/api/admin/users/{uid}/key", headers=h).status_code in (401, 403, 422)
    r = client.post(f"/api/admin/users/{uid}/key", headers=ADMIN).json()
    assert r["username"] == "lost" and r["api_key"].startswith("pt_")
    assert client.get("/api/portfolio", headers=h).status_code == 401
    assert client.get("/api/portfolio", headers={"X-API-Key": r["api_key"]}).status_code == 200
    assert client.post("/api/admin/users/9999/key", headers=ADMIN).status_code == 404


def test_admin_roles(app, client):
    boss = register(client, "boss")           # a member the owner promotes
    other = register(client, "other")
    plain = register(client, "plain")
    users = {u["username"]: u for u in client.get("/api/admin/users", headers=ADMIN).json()}
    assert not users["boss"]["is_admin"]
    assert client.get("/api/admin/me", headers=ADMIN).json() == {"role": "owner", "name": "owner"}
    # a plain member is not an admin
    assert client.get("/api/admin/users", headers=plain).status_code == 403
    # only the owner can grant or remove admin
    uid = lambda n: users[n]["user_id"]
    assert client.put(f"/api/admin/users/{uid('boss')}/admin", json={"is_admin": True}, headers=plain).status_code in (401, 403)
    assert client.put(f"/api/admin/users/{uid('boss')}/admin", json={"is_admin": True}, headers=ADMIN).status_code == 200
    assert client.put(f"/api/admin/users/{uid('other')}/admin", json={"is_admin": True}, headers=ADMIN).status_code == 200
    # the admin signs in with their own API key
    assert client.get("/api/admin/me", headers=boss).json() == {"role": "admin", "name": "boss"}
    assert client.get("/api/admin/users", headers=boss).status_code == 200
    assert client.post("/api/admin/users", json={"username": "newbie"}, headers=boss).status_code == 200
    # admins can't make or remove admins, and can't touch other admins' keys or accounts
    assert client.put(f"/api/admin/users/{uid('plain')}/admin", json={"is_admin": True}, headers=boss).status_code == 403
    assert client.put(f"/api/admin/users/{uid('other')}/admin", json={"is_admin": False}, headers=boss).status_code == 403
    assert client.post(f"/api/admin/users/{uid('other')}/key", headers=boss).status_code == 403
    assert client.delete(f"/api/admin/users/{uid('other')}", headers=boss).status_code == 403
    # but they can manage ordinary members
    assert client.post(f"/api/admin/users/{uid('plain')}/key", headers=boss).status_code == 200
    # the owner can remove an admin, and the key stops working for admin pages (but still trades)
    assert client.put(f"/api/admin/users/{uid('boss')}/admin", json={"is_admin": False}, headers=ADMIN).status_code == 200
    assert client.get("/api/admin/users", headers=boss).status_code == 403
    assert client.get("/api/portfolio", headers=boss).status_code == 200
    assert client.delete(f"/api/admin/users/{uid('other')}", headers=ADMIN).status_code == 200
    # a bad key or token is a 401
    assert client.get("/api/admin/users", headers={"X-API-Key": "pt_bogus"}).status_code == 401
    assert client.get("/api/admin/users", headers={"X-Admin-Token": "wrong"}).status_code == 401
    # the log says who did what
    log = client.get("/api/admin/log", headers=ADMIN).json()
    assert any(r["actor"] == "boss" and r["action"] == "created account" and r["detail"] == "newbie" for r in log)
    assert any(r["actor"] == "owner" and r["action"] == "made admin" and r["detail"] == "boss" for r in log)
    assert any(r["actor"] == "boss" and r["action"] == "issued new API key" and r["detail"] == "plain" for r in log)
    assert client.get("/api/admin/log", headers=plain).status_code in (401, 403)


def test_admin_flag_added_to_older_database(tmp_path):
    import sqlite3
    from app.db import Database
    path = str(tmp_path / "old.db")
    Database(path)                                                    # current schema
    c = sqlite3.connect(path)
    c.execute("ALTER TABLE users DROP COLUMN is_admin")               # pretend it predates admin roles
    c.execute("INSERT INTO users (username, api_key_hash, created_at) VALUES ('old','h',1)")
    c.commit(); c.close()
    Database(path)
    c = sqlite3.connect(path)
    assert c.execute("SELECT is_admin FROM users WHERE username='old'").fetchone()[0] == 0


def test_header_flags_and_old_codes_page(client):
    h = register(client, "navuser")
    assert client.get("/api/portfolio", headers=h).json()["is_admin"] is False
    uid = client.get("/api/admin/users", headers=ADMIN).json()[0]["user_id"]
    client.put(f"/api/admin/users/{uid}/admin", json={"is_admin": True}, headers=ADMIN)
    assert client.get("/api/portfolio", headers=h).json()["is_admin"] is True
    r = client.get("/admin/codes", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"].startswith("/admin/users")
