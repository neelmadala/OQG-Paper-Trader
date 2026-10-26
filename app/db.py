import sqlite3
from contextlib import contextmanager

LEGACY_ORDERS_DDL = """
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
)
"""

SCHEMA = """
-- A user is a person with a login (API key). Money lives in portfolios.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    api_key_hash  TEXT NOT NULL UNIQUE,
    created_at    REAL NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0
);

-- Who did what in the admin panel.
CREATE TABLE IF NOT EXISTS admin_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    actor  TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);

-- Admin-run contests: members join with a code (or are added by an admin) and each gets a fresh portfolio.
CREATE TABLE IF NOT EXISTS competitions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    code           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    starting_cash  REAL NOT NULL,
    start_ts       REAL,                      -- trading opens (NULL = already open)
    end_ts         REAL,                      -- trading closes and results freeze (NULL = never)
    allow_options  INTEGER NOT NULL DEFAULT 1,
    allow_shorting INTEGER NOT NULL DEFAULT 1,
    finalized      INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL
);

-- Sign-up codes an admin hands out. A code works max_uses times (1 = single use) until it expires or is revoked.
CREATE TABLE IF NOT EXISTS invite_codes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL UNIQUE,
    label      TEXT,                           -- who it is for, free text
    max_uses   INTEGER NOT NULL DEFAULT 1,
    uses       INTEGER NOT NULL DEFAULT 0,
    expires_at REAL,
    revoked    INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS invite_uses (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    code_id INTEGER NOT NULL REFERENCES invite_codes(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ts      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invite_uses_code ON invite_uses(code_id);

-- One account with its own cash, positions, orders and equity history.
-- Every user has a default ("Main") one; competitions add more.
CREATE TABLE IF NOT EXISTS portfolios (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name           TEXT NOT NULL COLLATE NOCASE,
    competition_id INTEGER REFERENCES competitions(id) ON DELETE CASCADE,
    is_default     INTEGER NOT NULL DEFAULT 0,
    cash           REAL NOT NULL,
    starting_cash  REAL NOT NULL,
    peak_equity    REAL NOT NULL,
    max_drawdown   REAL NOT NULL DEFAULT 0,
    final_equity   REAL,                      -- set when its competition ends: the frozen result
    created_at     REAL NOT NULL,
    UNIQUE (user_id, name)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_portfolio_default ON portfolios(user_id) WHERE is_default = 1;
CREATE UNIQUE INDEX IF NOT EXISTS uq_portfolio_comp ON portfolios(user_id, competition_id) WHERE competition_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_portfolio_comp ON portfolios(competition_id);

CREATE TABLE IF NOT EXISTS positions (
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    symbol       TEXT NOT NULL,
    qty          REAL NOT NULL,
    avg_cost     REAL NOT NULL,
    stop_price   REAL,
    PRIMARY KEY (portfolio_id, symbol)
);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id    INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    client_order_id TEXT,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell', 'short', 'cover')),
    qty             REAL NOT NULL,
    price           REAL NOT NULL,
    fee             REAL NOT NULL DEFAULT 0,
    realized_pnl    REAL NOT NULL DEFAULT 0,
    forced          INTEGER NOT NULL DEFAULT 0,
    ts              REAL NOT NULL,
    UNIQUE (portfolio_id, client_order_id)
);
CREATE INDEX IF NOT EXISTS idx_orders_pf_ts ON orders(portfolio_id, ts);

CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    ts           REAL NOT NULL,
    equity       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_pf_ts ON snapshots(portfolio_id, ts);

-- Resting limit orders. A fill creates a normal row in `orders` (client_order_id 'lim:<id>').
-- status: open | filling (claimed by the fill check) | filled | cancelled | expired | rejected
CREATE TABLE IF NOT EXISTS limit_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id    INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
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
CREATE INDEX IF NOT EXISTS idx_limit_pf ON limit_orders(portfolio_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_limit_coid ON limit_orders(portfolio_id, client_order_id) WHERE client_order_id IS NOT NULL;

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

-- What each action did to each portfolio (shown to members).
CREATE TABLE IF NOT EXISTS corp_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    symbol       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    ex_date      TEXT NOT NULL,
    qty          REAL NOT NULL,   -- shares that were entitled
    value        REAL NOT NULL,   -- dividend per share, or split ratio
    amount       REAL NOT NULL,   -- cash paid (+) or owed (-) for a dividend, shares added for a split
    ts           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_pf ON corp_events(portfolio_id, id);
"""

# Child tables of the pre-portfolio layout (keyed by user_id), with the columns to carry over.
_LEGACY_CHILDREN = {
    "positions": ("portfolio_id, symbol, qty, avg_cost, stop_price", "user_id, symbol, qty, avg_cost, stop_price"),
    "orders": (
        "id, portfolio_id, client_order_id, symbol, side, qty, price, fee, realized_pnl, forced, ts",
        "id, user_id, client_order_id, symbol, side, qty, price, fee, realized_pnl, forced, ts",
    ),
    "snapshots": ("id, portfolio_id, ts, equity", "id, user_id, ts, equity"),
    "limit_orders": (
        "id, portfolio_id, client_order_id, symbol, side, qty, limit_price, status, reason, order_id, created_at, closed_at",
        "id, user_id, client_order_id, symbol, side, qty, limit_price, status, reason, order_id, created_at, closed_at",
    ),
    "corp_events": (
        "id, portfolio_id, symbol, kind, ex_date, qty, value, amount, ts",
        "id, user_id, symbol, kind, ex_date, qty, value, amount, ts",
    ),
}
_LEGACY_INDEXES = (
    "idx_orders_user_ts", "idx_snapshots_user_ts", "idx_limit_status", "idx_limit_user", "uq_limit_coid", "idx_events_user",
)


def _tables(conn) -> set[str]:
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _cols(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_legacy(conn) -> None:
    """Bring a database from before portfolios up to the last user-keyed layout.

    1. Before shorting, the orders table only allowed the sides 'buy' and 'sell' (a CHECK
       constraint SQLite can't alter in place), so rebuild it and keep every row and id.
    2. Before auto-liquidation, orders had no `forced` flag: add it (existing rows get 0).
    3. Before stop prices, positions had no `stop_price`: add it (existing rows get NULL)."""
    tables = _tables(conn)
    if "orders" in tables:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='orders'").fetchone()
        if "'short'" not in row["sql"]:
            cols = "id, user_id, client_order_id, symbol, side, qty, price, fee, realized_pnl, ts"
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("ALTER TABLE orders RENAME TO orders_old")
                conn.execute("DROP INDEX IF EXISTS idx_orders_user_ts")
                conn.execute(LEGACY_ORDERS_DDL.replace("IF NOT EXISTS ", ""))
                conn.execute(f"INSERT INTO orders ({cols}) SELECT {cols} FROM orders_old")
                conn.execute("DROP TABLE orders_old")
                conn.execute("CREATE INDEX idx_orders_user_ts ON orders(user_id, ts)")
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        if "forced" not in _cols(conn, "orders"):
            conn.execute("ALTER TABLE orders ADD COLUMN forced INTEGER NOT NULL DEFAULT 0")
    if "positions" in tables and "stop_price" not in _cols(conn, "positions"):
        conn.execute("ALTER TABLE positions ADD COLUMN stop_price REAL")


def _to_portfolios(conn) -> None:
    """Move a user-keyed database to portfolios, all in one transaction.

    Every existing user becomes a user plus one default portfolio named "Main". The portfolio gets the
    user's old id, so every order, position, snapshot and limit order keeps pointing at the same number."""
    tables = _tables(conn)
    present = [t for t in _LEGACY_CHILDREN if t in tables]
    conn.execute("PRAGMA foreign_keys=OFF")
    script = ["BEGIN;", "ALTER TABLE users RENAME TO users_old;"]
    script += [f"ALTER TABLE {t} RENAME TO {t}_old;" for t in present]
    script += [f"DROP INDEX IF EXISTS {i};" for i in _LEGACY_INDEXES]
    script.append(SCHEMA)
    script += [
        "INSERT INTO users (id, username, api_key_hash, created_at) SELECT id, username, api_key_hash, created_at FROM users_old;",
        "INSERT INTO portfolios (id, user_id, name, competition_id, is_default, cash, starting_cash, peak_equity, max_drawdown, created_at)"
        " SELECT id, id, 'Main', NULL, 1, cash, starting_cash, peak_equity, max_drawdown, created_at FROM users_old;",
    ]
    for t in present:
        new_cols, old_cols = _LEGACY_CHILDREN[t]
        script.append(f"INSERT INTO {t} ({new_cols}) SELECT {old_cols} FROM {t}_old;")
    script += [f"DROP TABLE {t}_old;" for t in present]
    script += ["DROP TABLE users_old;", "COMMIT;"]
    try:
        conn.executescript("\n".join(script))
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


class Database:
    def __init__(self, path: str):
        self.path = path
        conn = self.connect()
        try:
            if "users" in _tables(conn) and "cash" in _cols(conn, "users"):  # a database from before portfolios
                _migrate_legacy(conn)
                _to_portfolios(conn)
            else:
                conn.executescript(SCHEMA)
            if "is_admin" not in _cols(conn, "users"):                 # a database from before admin roles
                conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        # isolation_level=None: autocommit, we issue BEGIN/COMMIT ourselves
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def read(self):
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def tx(self):
        """Write transaction. BEGIN IMMEDIATE takes the write lock up front, so
        concurrent orders are serialized and cash/position checks can't race."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
