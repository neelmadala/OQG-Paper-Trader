import hashlib
import logging
import math
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from .db import Database
from .prices import OPTION_MULTIPLIER, PriceError, PriceService, is_option, normalize_symbol, parse_option
from .settings import Settings

log = logging.getLogger("paper_trader")

USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{3,32}$")
EPS = 1e-9
SIDES = ("buy", "sell", "short", "cover")
NOT_CROSSED = "limit price not reached"
REASONS = {1: "liquidation", 2: "stop", 3: "limit", 4: "expiry"}  # orders.forced: who/what placed the fill


class TradeError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def limit_dict(r) -> dict:
    return {
        "id": r["id"],
        "type": "limit",
        "client_order_id": r["client_order_id"],
        "symbol": r["symbol"],
        "side": r["side"],
        "qty": r["qty"],
        "limit_price": r["limit_price"],
        "status": "open" if r["status"] == "filling" else r["status"],
        "reason": r["reason"],
        "order_id": r["order_id"],
        "ts": r["created_at"],
        "time": iso(r["created_at"]),
        "closed_ts": r["closed_at"],
    }


def order_dict(r) -> dict:
    return {
        "id": r["id"],
        "client_order_id": r["client_order_id"],
        "symbol": r["symbol"],
        "side": r["side"],
        "qty": r["qty"],
        "price": round(r["price"], 4),
        "fee": r["fee"],
        "realized_pnl": round(r["realized_pnl"], 2),
        "forced": r["forced"] in (1, 2),  # true when the app closed something for the member (liquidation or stop)
        "reason": REASONS.get(r["forced"]),
        "ts": r["ts"],
        "time": iso(r["ts"]),
    }


DEFAULT_PORTFOLIO = "Main"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no look-alike characters
PORTFOLIO_RE = re.compile(r"^[\w .'()\-]{1,40}$")


class Broker:
    def __init__(self, db: Database, prices: PriceService, settings: Settings):
        self.db = db
        self.prices = prices
        self.s = settings
        # The margin-call level can never be stricter than the level orders are checked against,
        # otherwise a freshly opened short would be liquidated immediately.
        self.maint = min(settings.short_maint_margin, settings.short_margin)
        self._limit_lock = threading.Lock()  # one fill check at a time
        self._actions_last: float | None = None
        self._settle_tried: dict[tuple[str, str], float] = {}

    @staticmethod
    def _check_tradable(symbol: str) -> None:
        if symbol.startswith("^"):
            raise TradeError(
                f"{symbol} is an index and can't be traded (quotes only). Trade an ETF that tracks it, "
                "for example SPY for ^GSPC, QQQ for ^NDX or DIA for ^DJI",
                422,
            )

    # ---------- options helpers ----------

    @staticmethod
    def _mult(symbol: str) -> int:
        return OPTION_MULTIPLIER if is_option(symbol) else 1

    def _opt_end(self, expiry: str) -> float:
        """Options stop trading at 4pm New York on the expiry date."""
        return self._ex_ts(expiry) + 16 * 3600

    def _check_option_order(self, symbol: str, side: str, qty: float) -> None:
        o = parse_option(symbol)
        if o is None:
            return
        if not self.s.allow_options:
            raise TradeError("options are disabled on this server", 403)
        if side in ("short", "cover"):
            raise TradeError(
                "writing options isn't supported: you can buy options and sell ones you own (use buy and sell)", 422
            )
        if abs(qty - round(qty)) > 1e-9:
            raise TradeError("options trade in whole contracts (each one covers 100 shares)", 422)
        if time.time() > self._opt_end(o["expiry"]):
            raise TradeError(f"{symbol} expired on {o['expiry']}", 422)

    def _option_fill(self, q: dict, side: str) -> float:
        """What you actually trade at: buys pay the ask, sells get the bid. With no two-sided market
        (after hours the feed often shows zeros) it falls back to the last trade plus the usual slippage."""
        slip = self.s.slippage_bps / 10_000
        if side == "buy":
            return q["ask"] if q["ask"] > 0 else q["mark"] * (1 + slip)
        if q["bid"] > 0:
            return q["bid"]
        if q["ask"] > 0:
            return 0.01  # someone is offering but nobody is bidding: you'd get next to nothing
        return q["mark"] * (1 - slip)

    def _cross_price(self, symbol: str, side: str) -> float:
        """The price a limit order is compared with: the executable price for options, the live price otherwise."""
        if is_option(symbol):
            return self._option_fill(self.prices.get_quote(symbol), side)
        return self.prices.get(symbol)

    # ---------- users and portfolios ----------

    def create_user(self, username: str) -> tuple[int, str]:
        if not USERNAME_RE.match(username or ""):
            raise TradeError("username must be 3-32 chars: letters, digits, _ or -", 422)
        api_key = "pt_" + secrets.token_urlsafe(32)
        now = time.time()
        try:
            with self.db.tx() as c:
                cur = c.execute(
                    "INSERT INTO users (username, api_key_hash, created_at) VALUES (?,?,?)",
                    (username, hash_key(api_key), now),
                )
                uid = cur.lastrowid
                self._new_portfolio(c, uid, DEFAULT_PORTFOLIO, self.s.starting_cash, now, default=True)
        except sqlite3.IntegrityError:
            raise TradeError("username already taken", 409)
        return uid, api_key

    # ---------- sign-up codes ----------

    @staticmethod
    def _norm_code(code: str) -> str:
        """Codes are typed by people: ignore case, spaces and dashes."""
        return re.sub(r"[\s\-]", "", code or "").upper()

    @staticmethod
    def show_code(code: str) -> str:
        return "-".join(code[i:i + 4] for i in range(0, len(code), 4))

    def create_invites(self, count: int = 1, label: str | None = None, max_uses: int = 1,
                       expires_at: float | None = None) -> list[dict]:
        """Make `count` sign-up codes. Each works max_uses times (default once)."""
        if not (1 <= count <= 200):
            raise TradeError("count must be between 1 and 200", 422)
        if not (1 <= max_uses <= 1000):
            raise TradeError("max_uses must be between 1 and 1000", 422)
        now = time.time()
        if expires_at is not None and expires_at <= now:
            raise TradeError("the expiry must be in the future", 422)
        label = (label or "").strip()[:60] or None
        ids = []
        with self.db.tx() as c:
            for _ in range(count):
                while True:
                    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
                    if not c.execute("SELECT 1 FROM invite_codes WHERE code=?", (code,)).fetchone():
                        break
                ids.append(c.execute(
                    "INSERT INTO invite_codes (code, label, max_uses, expires_at, created_at) VALUES (?,?,?,?,?)",
                    (code, label, max_uses, expires_at, now),
                ).lastrowid)
        return [r for r in self.list_invites() if r["id"] in set(ids)]

    def list_invites(self) -> list[dict]:
        now = time.time()
        with self.db.read() as c:
            rows = c.execute("SELECT * FROM invite_codes ORDER BY id DESC").fetchall()
            users: dict[int, list[str]] = {}
            for r in c.execute(
                "SELECT i.code_id, u.username FROM invite_uses i JOIN users u ON u.id=i.user_id ORDER BY i.id"
            ):
                users.setdefault(r["code_id"], []).append(r["username"])
        out = []
        for r in rows:
            if r["revoked"]:
                status = "revoked"
            elif r["uses"] >= r["max_uses"]:
                status = "used"
            elif r["expires_at"] is not None and now >= r["expires_at"]:
                status = "expired"
            else:
                status = "unused" if r["uses"] == 0 else "partly used"
            out.append({
                "id": r["id"], "code": self.show_code(r["code"]), "label": r["label"], "max_uses": r["max_uses"],
                "uses": r["uses"], "expires_at": r["expires_at"], "status": status,
                "used_by": users.get(r["id"], []), "created_at": r["created_at"],
            })
        return out

    def revoke_invite(self, invite_id: int) -> None:
        with self.db.tx() as c:
            n = c.execute("UPDATE invite_codes SET revoked=1 WHERE id=?", (invite_id,)).rowcount
        if n == 0:
            raise TradeError("code not found", 404)

    def register(self, username: str, code: str) -> tuple[int, str]:
        """Sign up with a code from create_invites (or the optional club-wide PT_INVITE_CODE).
        A code is only used up when the account is actually created, so a taken username doesn't burn it."""
        if not USERNAME_RE.match(username or ""):
            raise TradeError("username must be 3-32 chars: letters, digits, _ or -", 422)
        norm = self._norm_code(code)
        shared = bool(self.s.invite_code) and secrets.compare_digest(  # the club-wide code is matched exactly
            (code or "").strip().encode(), self.s.invite_code.encode())
        api_key = "pt_" + secrets.token_urlsafe(32)
        now = time.time()
        try:
            with self.db.tx() as c:
                inv = None
                if not shared:
                    inv = c.execute("SELECT * FROM invite_codes WHERE code=?", (norm,)).fetchone() if norm else None
                    if inv is None:
                        raise TradeError("invalid invite code", 403)
                    if inv["revoked"]:
                        raise TradeError("that invite code has been cancelled; ask an admin for a new one", 403)
                    if inv["expires_at"] is not None and now >= inv["expires_at"]:
                        raise TradeError("that invite code has expired; ask an admin for a new one", 403)
                    if inv["uses"] >= inv["max_uses"]:
                        raise TradeError("that invite code has already been used; ask an admin for a new one", 403)
                uid = c.execute(
                    "INSERT INTO users (username, api_key_hash, created_at) VALUES (?,?,?)",
                    (username, hash_key(api_key), now),
                ).lastrowid
                self._new_portfolio(c, uid, DEFAULT_PORTFOLIO, self.s.starting_cash, now, default=True)
                if inv is not None:
                    c.execute("UPDATE invite_codes SET uses=uses+1 WHERE id=?", (inv["id"],))
                    c.execute("INSERT INTO invite_uses (code_id, user_id, ts) VALUES (?,?,?)", (inv["id"], uid, now))
        except sqlite3.IntegrityError:
            raise TradeError("username already taken", 409)
        return uid, api_key

    def list_users(self) -> list[dict]:
        """Every account for the admin user list: when they joined, when they last traded, what they hold across
        their portfolios, and which sign-up code made the account."""
        with self.db.read() as c:
            users = c.execute(
                "SELECT u.id, u.username, u.created_at,"
                " (SELECT MAX(o.ts) FROM orders o JOIN portfolios p ON p.id=o.portfolio_id WHERE p.user_id=u.id) AS last_order,"
                " u.is_admin, ic.code AS invite_code, ic.label AS invite_label"
                " FROM users u LEFT JOIN invite_uses iu ON iu.user_id=u.id LEFT JOIN invite_codes ic ON ic.id=iu.code_id"
                " ORDER BY u.id DESC"
            ).fetchall()
        by_user: dict[int, list[dict]] = {}
        for r in self.summaries(scope="all"):
            by_user.setdefault(r["user_id"], []).append(r)
        out = []
        for u in users:
            pfs = by_user.get(u["id"], [])
            main = next((p for p in pfs if p["is_default"]), None)
            out.append({
                "user_id": u["id"],
                "username": u["username"],
                "is_admin": bool(u["is_admin"]),
                "created_at": u["created_at"],
                "last_trade_at": u["last_order"],
                "portfolios": len(pfs),
                "competitions": [p["competition"] for p in pfs if p["competition"]],
                "main_portfolio_id": main["portfolio_id"] if main else None,
                "main_equity": main["equity"] if main else None,
                "main_return_pct": main["return_pct"] if main else None,
                "trades": sum(p["trades"] for p in pfs),
                "signed_up_with": self.show_code(u["invite_code"]) if u["invite_code"] else None,
                "invite_label": u["invite_label"],
            })
        return out

    def set_admin(self, user_id: int, make_admin: bool) -> str:
        with self.db.tx() as c:
            r = c.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
            if r is None:
                raise TradeError("user not found", 404)
            c.execute("UPDATE users SET is_admin=? WHERE id=?", (1 if make_admin else 0, user_id))
        return r["username"]

    def is_admin_user(self, user_id: int) -> bool:
        with self.db.read() as c:
            r = c.execute("SELECT is_admin FROM users WHERE id=?", (user_id,)).fetchone()
        return bool(r and r["is_admin"])

    def log_admin(self, actor: str, action: str, detail: str = "") -> None:
        with self.db.tx() as c:
            c.execute("INSERT INTO admin_log (ts, actor, action, detail) VALUES (?,?,?,?)", (time.time(), actor, action, detail))

    def admin_log(self, limit: int = 100) -> list[dict]:
        with self.db.read() as c:
            rows = c.execute("SELECT ts, actor, action, detail FROM admin_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def user_by_key(self, api_key: str):
        with self.db.read() as c:
            return c.execute("SELECT * FROM users WHERE api_key_hash=?", (hash_key(api_key),)).fetchone()

    @staticmethod
    def _new_portfolio(c, user_id: int, name: str, cash: float, now: float,
                       competition_id: int | None = None, default: bool = False) -> int:
        cur = c.execute(
            "INSERT INTO portfolios (user_id, name, competition_id, is_default, cash, starting_cash, peak_equity, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (user_id, name, competition_id, 1 if default else 0, cash, cash, cash, now),
        )
        c.execute("INSERT INTO snapshots (portfolio_id, ts, equity) VALUES (?,?,?)", (cur.lastrowid, now, cash))
        return cur.lastrowid

    def default_portfolio(self, user_id: int) -> int:
        with self.db.read() as c:
            r = c.execute("SELECT id FROM portfolios WHERE user_id=? AND is_default=1", (user_id,)).fetchone()
        if r is None:
            raise TradeError("user not found", 404)
        return r["id"]

    def resolve_portfolio(self, user_id: int, ref=None) -> int:
        """Which of this member's portfolios a request means: an id or a name, or the default when left out.
        A portfolio that belongs to someone else is reported as not found."""
        if ref is None or str(ref).strip() == "":
            return self.default_portfolio(user_id)
        ref = str(ref).strip()
        with self.db.read() as c:
            r = None
            if ref.isdigit():
                r = c.execute("SELECT id FROM portfolios WHERE id=? AND user_id=?", (int(ref), user_id)).fetchone()
            if r is None:
                r = c.execute("SELECT id FROM portfolios WHERE user_id=? AND name=?", (user_id, ref)).fetchone()
        if r is None:
            raise TradeError(f"portfolio '{ref}' not found", 404)
        return r["id"]

    def create_portfolio(self, user_id: int, name: str) -> dict:
        """An extra personal portfolio with the normal starting cash (practice account, separate strategy...)."""
        name = (name or "").strip()
        if not PORTFOLIO_RE.match(name) or name.isdigit():
            raise TradeError("portfolio name must be 1-40 chars (letters, digits, spaces, _ - . ( ) ') and not only digits", 422)
        now = time.time()
        try:
            with self.db.tx() as c:
                n = c.execute(
                    "SELECT COUNT(*) AS n FROM portfolios WHERE user_id=? AND is_default=0 AND competition_id IS NULL",
                    (user_id,),
                ).fetchone()["n"]
                if n >= self.s.max_portfolios:
                    raise TradeError(f"you can have at most {self.s.max_portfolios} extra portfolios; delete one first", 429)
                pid = self._new_portfolio(c, user_id, name, self.s.starting_cash, now)
        except sqlite3.IntegrityError:
            raise TradeError("you already have a portfolio with that name", 409)
        return self.summaries(scope="all", user_id=user_id, only=pid)[0]

    def delete_portfolio(self, user_id: int, portfolio_id: int) -> None:
        with self.db.tx() as c:
            r = c.execute("SELECT * FROM portfolios WHERE id=? AND user_id=?", (portfolio_id, user_id)).fetchone()
            if r is None:
                raise TradeError("portfolio not found", 404)
            if r["is_default"]:
                raise TradeError("the main portfolio can't be deleted (an admin can reset it)", 409)
            if r["competition_id"] is not None:
                raise TradeError("competition portfolios can only be removed by an admin", 409)
            c.execute("DELETE FROM portfolios WHERE id=?", (portfolio_id,))

    def reset_portfolio(self, portfolio_id: int) -> None:
        now = time.time()
        with self.db.tx() as c:
            u = c.execute("SELECT starting_cash FROM portfolios WHERE id=?", (portfolio_id,)).fetchone()
            if not u:
                raise TradeError("portfolio not found", 404)
            c.execute("DELETE FROM positions WHERE portfolio_id=?", (portfolio_id,))
            c.execute("DELETE FROM orders WHERE portfolio_id=?", (portfolio_id,))
            c.execute("DELETE FROM limit_orders WHERE portfolio_id=?", (portfolio_id,))
            c.execute("DELETE FROM corp_events WHERE portfolio_id=?", (portfolio_id,))
            c.execute("DELETE FROM snapshots WHERE portfolio_id=?", (portfolio_id,))
            c.execute(
                "UPDATE portfolios SET cash=starting_cash, peak_equity=starting_cash, max_drawdown=0, final_equity=NULL"
                " WHERE id=?",
                (portfolio_id,),
            )
            c.execute(
                "INSERT INTO snapshots (portfolio_id, ts, equity) VALUES (?,?,?)", (portfolio_id, now, u["starting_cash"])
            )

    def rotate_key(self, user_id: int, old_key: str) -> str:
        """Give a member a new API key; the old one stops working straight away."""
        api_key = "pt_" + secrets.token_urlsafe(32)
        with self.db.tx() as c:
            cur = c.execute("UPDATE users SET api_key_hash=? WHERE id=? AND api_key_hash=?",
                            (hash_key(api_key), user_id, hash_key(old_key)))
            if cur.rowcount != 1:
                raise TradeError("invalid API key", 401)
        return api_key

    def admin_new_key(self, user_id: int) -> tuple[str, str]:
        """Admin recovery: give a member a new key without needing the old one."""
        api_key = "pt_" + secrets.token_urlsafe(32)
        with self.db.tx() as c:
            r = c.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
            if r is None:
                raise TradeError("user not found", 404)
            c.execute("UPDATE users SET api_key_hash=? WHERE id=?", (hash_key(api_key), user_id))
        return r["username"], api_key

    def delete_user(self, user_id: int) -> str:
        """Remove an account and everything it owns (portfolios, orders, positions, history)."""
        with self.db.tx() as c:
            r = c.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
            if r is None:
                raise TradeError("user not found", 404)
            c.execute("DELETE FROM users WHERE id=?", (user_id,))   # cascades to portfolios and their rows
        return r["username"]

    def reset_user(self, user_id: int) -> None:
        """Reset a member's main portfolio back to its starting cash."""
        with self.db.read() as c:
            r = c.execute("SELECT id FROM portfolios WHERE user_id=? AND is_default=1", (user_id,)).fetchone()
        if r is None:
            raise TradeError("user not found", 404)
        self.reset_portfolio(r["id"])

    # ---------- competitions ----------

    @staticmethod
    def _comp_status(k, now: float) -> str:
        if k["start_ts"] is not None and now < k["start_ts"]:
            return "upcoming"
        if k["end_ts"] is not None and now >= k["end_ts"]:
            return "ended"
        return "active"

    def _comp_dict(self, k, now: float, admin: bool = False) -> dict:
        d = {
            "id": k["id"],
            "name": k["name"],
            "starting_cash": k["starting_cash"],
            "start_ts": k["start_ts"],
            "end_ts": k["end_ts"],
            "start": iso(k["start_ts"]) if k["start_ts"] is not None else None,
            "end": iso(k["end_ts"]) if k["end_ts"] is not None else None,
            "allow_options": bool(k["allow_options"]) and self.s.allow_options,
            "allow_shorting": bool(k["allow_shorting"]) and self.s.allow_shorting,
            "status": self._comp_status(k, now),
        }
        if "members" in k.keys():
            d["members"] = k["members"]
        if admin:
            d["code"] = k["code"]
        return d

    def create_competition(
        self,
        name: str,
        starting_cash: float | None = None,
        start_ts: float | None = None,
        end_ts: float | None = None,
        allow_options: bool = True,
        allow_shorting: bool = True,
    ) -> dict:
        name = (name or "").strip()
        if not PORTFOLIO_RE.match(name) or name.isdigit():
            raise TradeError("competition name must be 1-40 chars (letters, digits, spaces, _ - . ( ) ') and not only digits", 422)
        cash = self.s.starting_cash if starting_cash is None else float(starting_cash)
        if not (0 < cash < 1e12):
            raise TradeError("starting_cash must be positive", 422)
        now = time.time()
        if start_ts is not None and end_ts is not None and end_ts <= start_ts:
            raise TradeError("the end must be after the start", 422)
        if end_ts is not None and end_ts <= now:
            raise TradeError("the end must be in the future", 422)
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM competitions WHERE name=?", (name,)).fetchone():
                raise TradeError("a competition with that name already exists", 409)
            for _ in range(20):
                code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
                if not c.execute("SELECT 1 FROM competitions WHERE code=?", (code,)).fetchone():
                    break
            cur = c.execute(
                "INSERT INTO competitions (name, code, starting_cash, start_ts, end_ts, allow_options, allow_shorting, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (name, code, cash, start_ts, end_ts, 1 if allow_options else 0, 1 if allow_shorting else 0, now),
            )
        return self.get_competition(cur.lastrowid, admin=True)

    def get_competition(self, competition_id: int, admin: bool = False) -> dict:
        with self.db.read() as c:
            k = c.execute(
                "SELECT k.*, (SELECT COUNT(*) FROM portfolios p WHERE p.competition_id=k.id) AS members"
                " FROM competitions k WHERE k.id=?", (competition_id,),
            ).fetchone()
        if k is None:
            raise TradeError("competition not found", 404)
        return self._comp_dict(k, time.time(), admin)

    def list_competitions(self, user_id: int | None = None) -> list[dict]:
        """Admins (no user_id) see every competition with its join code; a member sees the ones they are in,
        with their portfolio and current rank."""
        now = time.time()
        with self.db.read() as c:
            rows = c.execute(
                "SELECT k.*, (SELECT COUNT(*) FROM portfolios p WHERE p.competition_id=k.id) AS members"
                " FROM competitions k ORDER BY k.id DESC"
            ).fetchall()
            mine = {}
            if user_id is not None:
                mine = {r["competition_id"]: r for r in c.execute(
                    "SELECT id, name, competition_id FROM portfolios WHERE user_id=? AND competition_id IS NOT NULL", (user_id,))}
        out = []
        for k in rows:
            if user_id is not None and k["id"] not in mine:
                continue
            d = self._comp_dict(k, now, admin=user_id is None)
            if user_id is not None:
                me = mine[k["id"]]
                d["portfolio_id"], d["portfolio"] = me["id"], me["name"]
                row = next((r for r in self.summaries(scope=k["id"]) if r["portfolio_id"] == me["id"]), None)
                d["rank"] = row["rank"] if row else None
                d["return_pct"] = row["return_pct"] if row else None
            out.append(d)
        return out

    def _enroll(self, c, k, user_id: int, now: float) -> int:
        if c.execute("SELECT 1 FROM portfolios WHERE user_id=? AND competition_id=?", (user_id, k["id"])).fetchone():
            raise TradeError("already in this competition", 409)
        if self._comp_status(k, now) == "ended":
            raise TradeError(f"{k['name']} has ended", 409)
        name = k["name"]
        if c.execute("SELECT 1 FROM portfolios WHERE user_id=? AND name=?", (user_id, name)).fetchone():
            name = f"{k['name']} (competition)"  # the member already has a portfolio with this name
        return self._new_portfolio(c, user_id, name, k["starting_cash"], now, competition_id=k["id"])

    def join_competition(self, user_id: int, code: str) -> dict:
        code = (code or "").strip()
        now = time.time()
        with self.db.tx() as c:
            k = c.execute("SELECT * FROM competitions WHERE code=?", (code,)).fetchone() if code else None
            if k is None:
                raise TradeError("no competition has that join code", 404)
            pid = self._enroll(c, k, user_id, now)
        return {"competition": self.get_competition(k["id"]), "portfolio_id": pid}

    def add_member(self, competition_id: int, username: str) -> dict:
        now = time.time()
        with self.db.tx() as c:
            k = c.execute("SELECT * FROM competitions WHERE id=?", (competition_id,)).fetchone()
            if k is None:
                raise TradeError("competition not found", 404)
            u = c.execute("SELECT id, username FROM users WHERE username=?", ((username or "").strip(),)).fetchone()
            if u is None:
                raise TradeError(f"no user named '{username}'", 404)
            pid = self._enroll(c, k, u["id"], now)
        return {"portfolio_id": pid, "username": u["username"]}

    def remove_member(self, competition_id: int, portfolio_id: int) -> None:
        with self.db.tx() as c:
            n = c.execute(
                "DELETE FROM portfolios WHERE id=? AND competition_id=?", (portfolio_id, competition_id)
            ).rowcount
        if n == 0:
            raise TradeError("that portfolio isn't in this competition", 404)

    def end_competition(self, competition_id: int) -> dict:
        """Close a competition now: trading stops and the results freeze."""
        now = time.time()
        with self.db.tx() as c:
            k = c.execute("SELECT * FROM competitions WHERE id=?", (competition_id,)).fetchone()
            if k is None:
                raise TradeError("competition not found", 404)
            if self._comp_status(k, now) == "ended":
                raise TradeError("this competition has already ended", 409)
            c.execute("UPDATE competitions SET end_ts=?, start_ts=MIN(COALESCE(start_ts, ?), ?) WHERE id=?",
                      (now, now, now, competition_id))
        self.finalize_competitions()
        return self.get_competition(competition_id, admin=True)

    def delete_competition(self, competition_id: int) -> None:
        """Remove a competition and every portfolio in it."""
        with self.db.tx() as c:
            n = c.execute("DELETE FROM competitions WHERE id=?", (competition_id,)).rowcount
        if n == 0:
            raise TradeError("competition not found", 404)

    def finalize_competitions(self) -> int:
        """Freeze the result of every competition that has ended: record each portfolio's final equity, cancel
        its resting limit orders and stop its equity curve. Returns the number of competitions closed out."""
        now = time.time()
        with self.db.read() as c:
            due = c.execute(
                "SELECT * FROM competitions WHERE end_ts IS NOT NULL AND end_ts <= ? AND finalized=0", (now,)
            ).fetchall()
        n = 0
        for k in due:
            with self.db.read() as c:
                pids = [r["id"] for r in c.execute("SELECT id FROM portfolios WHERE competition_id=?", (k["id"],))]
            finals = {pid: self.portfolio(pid)["equity"] for pid in pids}  # prices are fetched outside the write lock
            with self.db.tx() as c:
                for pid, eq in finals.items():
                    c.execute("UPDATE portfolios SET final_equity=? WHERE id=?", (eq, pid))
                    last = c.execute("SELECT MAX(ts) AS t FROM snapshots WHERE portfolio_id=?", (pid,)).fetchone()["t"] or 0
                    c.execute(  # the curve's last point: the result, stamped at the end (never before an earlier point)
                        "INSERT INTO snapshots (portfolio_id, ts, equity) VALUES (?,?,?)", (pid, max(k["end_ts"], last), eq)
                    )
                    c.execute(
                        "UPDATE limit_orders SET status='cancelled', reason='competition ended', closed_at=?"
                        " WHERE portfolio_id=? AND status='open'", (now, pid),
                    )
                c.execute("UPDATE competitions SET finalized=1 WHERE id=?", (k["id"],))
            log.info("competition %s ended: %d portfolios frozen", k["name"], len(finals))
            n += 1
        return n

    def _ended_ids(self) -> set[int]:
        """Portfolios whose competition is over: nothing runs for them any more."""
        with self.db.read() as c:
            return {r["id"] for r in c.execute(
                "SELECT p.id FROM portfolios p JOIN competitions k ON k.id=p.competition_id"
                " WHERE k.end_ts IS NOT NULL AND k.end_ts <= ?", (time.time(),))}

    def _check_competition(self, portfolio_id: int, symbol: str, side: str, trigger: int = 0) -> None:
        """Apply the rules of the competition a portfolio belongs to (nothing happens for personal portfolios)."""
        with self.db.read() as c:
            k = c.execute(
                "SELECT k.* FROM portfolios p JOIN competitions k ON k.id=p.competition_id WHERE p.id=?", (portfolio_id,)
            ).fetchone()
        if k is None:
            return
        status = self._comp_status(k, time.time())
        if status == "upcoming":
            raise TradeError(f"{k['name']} hasn't started yet (opens {iso(k['start_ts'])})", 403)
        if status == "ended":
            raise TradeError(f"{k['name']} has ended - trading is closed", 403)
        if trigger in (0, 3):  # rules a member's own orders must follow
            if is_option(symbol) and not k["allow_options"]:
                raise TradeError(f"options aren't allowed in {k['name']}", 403)
            if side == "short" and not k["allow_shorting"]:
                raise TradeError(f"short selling isn't allowed in {k['name']}", 403)

    # ---------- trading ----------

    def _find_dup(self, c, portfolio_id, client_order_id, symbol, side, qty):
        r = c.execute(
            "SELECT * FROM orders WHERE portfolio_id=? AND client_order_id=?", (portfolio_id, client_order_id)
        ).fetchone()
        if r is None:
            return None
        if r["symbol"] != symbol or r["side"] != side or abs(r["qty"] - qty) > EPS:
            raise TradeError("client_order_id already used for a different order", 409)
        d = order_dict(r)
        d["duplicate"] = True
        return d

    def place_order(
        self,
        portfolio_id: int,
        symbol: str,
        side: str,
        qty: float,
        client_order_id: str | None = None,
        stop_price: float | None = None,
        trigger: int = 0,  # 0 = member order, 1 = liquidation, 2 = stop, 3 = limit order (see REASONS)
        limit_price: float | None = None,  # fill only if the market is at this price or better
        passive: bool = False,  # a resting limit order being filled later: fills at the live price, no slippage
    ) -> dict:
        """Market order. Sides: buy / sell (long positions) and short / cover (short positions).
        Orders never flip a position through zero: close it first, then open the other way."""
        symbol = normalize_symbol(symbol)
        side = (side or "").lower()
        if side not in SIDES:
            raise TradeError("side must be 'buy', 'sell', 'short' or 'cover'", 422)
        self._check_tradable(symbol)
        qty = round(float(qty), 6)
        if qty <= 0:
            raise TradeError("qty must be positive", 422)
        self._check_competition(portfolio_id, symbol, side, trigger)
        opt = is_option(symbol)
        if opt:
            self._check_option_order(symbol, side, qty)
        elif side in ("short", "cover") and not self.s.allow_shorting and trigger not in (1, 2):
            raise TradeError("shorting is disabled on this server", 403)
        mult = OPTION_MULTIPLIER if opt else 1

        if client_order_id:
            with self.db.read() as c:
                dup = self._find_dup(c, portfolio_id, client_order_id, symbol, side, qty)
            if dup:
                return dup

        quote = self.prices.get_quote(symbol) if opt else None  # network calls happen outside the DB lock
        mid = quote["mark"] if opt else self.prices.get(symbol)
        if stop_price is not None:
            if side not in ("buy", "short"):
                raise TradeError("stop_price can only be sent with a buy or short; use PUT /api/positions/{symbol}/stop on an open position", 422)
            stop_price = self._check_stop(side, float(stop_price), mid)
        slip = self.s.slippage_bps / 10_000
        px = self._option_fill(quote, side) if opt else (
            mid * (1 + slip) if side in ("buy", "cover") else mid * (1 - slip)  # slippage always hurts
        )
        cross = px if opt else mid  # what a limit price is compared with
        if limit_price is not None:
            buying = side in ("buy", "cover")
            if (buying and cross > limit_price + EPS) or (not buying and cross < limit_price - EPS):
                raise TradeError(NOT_CROSSED, 409)
            if passive:  # a resting order fills at the live price, which is at or better than the limit
                px = cross
            else:  # an order that fills on arrival pays the usual slippage, but never worse than the limit
                px = min(px, limit_price) if buying else max(px, limit_price)
        fee = self.s.commission
        now = time.time()
        m = self.s.short_margin

        with self.db.read() as c:
            short_syms = [r["symbol"] for r in c.execute(
                "SELECT symbol FROM positions WHERE portfolio_id=? AND qty < 0", (portfolio_id,))]
        marks = self.prices.get_many(short_syms) if short_syms else {}

        with self.db.tx() as c:
            if client_order_id:
                dup = self._find_dup(c, portfolio_id, client_order_id, symbol, side, qty)
                if dup:
                    return dup
            cash = c.execute("SELECT cash FROM portfolios WHERE id=?", (portfolio_id,)).fetchone()["cash"]
            pos = c.execute(
                "SELECT qty, avg_cost FROM positions WHERE portfolio_id=? AND symbol=?", (portfolio_id, symbol)
            ).fetchone()
            held = pos["qty"] if pos else 0.0
            avg = pos["avg_cost"] if pos else 0.0

            # Value of everything currently short, at the freshest prices we have.
            short_value = 0.0
            for r in c.execute("SELECT symbol, qty, avg_cost FROM positions WHERE portfolio_id=? AND qty < 0", (portfolio_id,)):
                mark = mid if r["symbol"] == symbol else (marks.get(r["symbol"]) or r["avg_cost"])
                short_value += -r["qty"] * mark
            free_cash = cash - (1 + m) * short_value  # cash not locked up as short collateral
            realized = 0.0

            if side == "buy":
                if held < -EPS:
                    raise TradeError(f"you are short {-held:g} {symbol}; use 'cover' to buy it back")
                cost = qty * px * mult + fee
                if cost > free_cash + EPS:
                    if short_value > 0:
                        raise TradeError(
                            f"insufficient free cash: need {cost:.2f}, free {max(free_cash, 0):.2f} "
                            f"(your shorts lock up {(1 + m) * short_value:.2f})"
                        )
                    raise TradeError(f"insufficient cash: need {cost:.2f}, have {cash:.2f}")
                new_qty = held + qty
                new_avg = (held * avg + qty * px) / new_qty
                self._set_position(c, portfolio_id, symbol, new_qty, new_avg)
                self._store_stop(c, portfolio_id, symbol, stop_price)
                new_cash = cash - cost

            elif side == "sell":
                if held < -EPS:
                    raise TradeError(f"you are short {-held:g} {symbol}; use 'cover' to close it")
                if qty > held + EPS:
                    raise TradeError(f"insufficient shares of {symbol}: want {qty}, hold {held}")
                realized = (px - avg) * qty * mult - fee
                self._set_position(c, portfolio_id, symbol, held - qty, avg)
                new_cash = cash + qty * px * mult - fee

            elif side == "short":
                if held > EPS:
                    raise TradeError(f"you hold {held:g} {symbol} long; sell it before shorting")
                need = m * qty * px + fee  # proceeds stay as collateral, the margin comes from free cash
                if need > free_cash + EPS:
                    raise TradeError(
                        f"insufficient margin: need {need:.2f} free cash, have {max(free_cash, 0):.2f}"
                    )
                old_short = -held
                new_avg = (old_short * avg + qty * px) / (old_short + qty)
                self._set_position(c, portfolio_id, symbol, held - qty, new_avg)
                self._store_stop(c, portfolio_id, symbol, stop_price)
                new_cash = cash + qty * px - fee

            else:  # cover
                if held > -EPS:
                    raise TradeError(f"you have no short position in {symbol}")
                if qty > -held + EPS:
                    raise TradeError(f"cannot cover {qty}: you are only short {-held:g} {symbol}")
                realized = (avg - px) * qty - fee
                self._set_position(c, portfolio_id, symbol, held + qty, avg)
                new_cash = cash - (qty * px + fee)  # always allowed: it only reduces risk

            new_cash = round(new_cash, 6)
            if abs(new_cash) < 1e-6:
                new_cash = 0.0
            c.execute("UPDATE portfolios SET cash=? WHERE id=?", (new_cash, portfolio_id))
            cur = c.execute(
                "INSERT INTO orders (portfolio_id, client_order_id, symbol, side, qty, price, fee, realized_pnl, forced, ts)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (portfolio_id, client_order_id, symbol, side, qty, px, fee, realized, trigger, now),
            )
            row = c.execute("SELECT * FROM orders WHERE id=?", (cur.lastrowid,)).fetchone()

        result = order_dict(row)
        result["duplicate"] = False
        result["status"] = "filled"
        try:
            self.snapshot_user(portfolio_id)
        except Exception:  # a failed snapshot must never fail a filled order
            log.exception("snapshot after order failed")
        return result

    @staticmethod
    def _set_position(c, portfolio_id: int, symbol: str, qty: float, avg_cost: float) -> None:
        """qty > 0 is long, qty < 0 is short, ~0 closes the position."""
        if abs(qty) < 1e-6:
            c.execute("DELETE FROM positions WHERE portfolio_id=? AND symbol=?", (portfolio_id, symbol))
        else:
            c.execute(
                "INSERT INTO positions (portfolio_id, symbol, qty, avg_cost) VALUES (?,?,?,?)"
                " ON CONFLICT(portfolio_id, symbol) DO UPDATE SET qty=excluded.qty, avg_cost=excluded.avg_cost",
                (portfolio_id, symbol, qty, avg_cost),
            )

    # ---------- limit orders ----------

    def _check_limit_feasible(self, portfolio_id: int, symbol: str, side: str, qty: float, limit: float) -> None:
        """Refuse a limit order that could never work (same rules as a market order, at the limit price).
        Cash and margin are not reserved: they are checked again when the order fills."""
        p = self.portfolio(portfolio_id)
        held = next((x["qty"] for x in p["positions"] if x["symbol"] == symbol), 0.0)
        free, fee, m = p["free_cash"], self.s.commission, self.s.short_margin
        cost = qty * limit * self._mult(symbol) + fee
        if side == "buy":
            if held < -EPS:
                raise TradeError(f"you are short {-held:g} {symbol}; use 'cover' to buy it back")
            if cost > free + EPS:
                raise TradeError(f"insufficient cash: need up to {cost:.2f}, free {max(free, 0):.2f}")
        elif side == "sell":
            if held < -EPS:
                raise TradeError(f"you are short {-held:g} {symbol}; use 'cover' to close it")
            if qty > held + EPS:
                raise TradeError(f"insufficient shares of {symbol}: want {qty}, hold {held}")
        elif side == "short":
            if held > EPS:
                raise TradeError(f"you hold {held:g} {symbol} long; sell it before shorting")
            if m * qty * limit + fee > free + EPS:
                raise TradeError(f"insufficient margin: need {m * qty * limit + fee:.2f} free cash, have {max(free, 0):.2f}")
        else:
            if held > -EPS:
                raise TradeError(f"you have no short position in {symbol}")
            if qty > -held + EPS:
                raise TradeError(f"cannot cover {qty}: you are only short {-held:g} {symbol}")

    def place_limit_order(
        self, portfolio_id: int, symbol: str, side: str, qty: float, limit_price: float, client_order_id: str | None = None
    ) -> dict:
        """Buy / cover at the limit price or lower, sell / short at it or higher.
        If the market is already there the order fills now (market-style slippage, never worse than the limit);
        otherwise it rests, and the one-minute check fills it when the price gets there."""
        symbol = normalize_symbol(symbol)
        side = (side or "").lower()
        if side not in SIDES:
            raise TradeError("side must be 'buy', 'sell', 'short' or 'cover'", 422)
        self._check_tradable(symbol)
        qty = round(float(qty), 6)
        if qty <= 0:
            raise TradeError("qty must be positive", 422)
        self._check_competition(portfolio_id, symbol, side, 3)
        if is_option(symbol):
            self._check_option_order(symbol, side, qty)
        elif side in ("short", "cover") and not self.s.allow_shorting:
            raise TradeError("shorting is disabled on this server", 403)
        limit_price = float(limit_price)
        if not (limit_price > 0) or not math.isfinite(limit_price):
            raise TradeError("limit_price must be a positive number", 422)

        if client_order_id:
            with self.db.read() as c:
                r = c.execute(
                    "SELECT * FROM limit_orders WHERE portfolio_id=? AND client_order_id=?", (portfolio_id, client_order_id)
                ).fetchone()
                if r is not None:
                    if (r["symbol"], r["side"]) != (symbol, side) or abs(r["qty"] - qty) > EPS or abs(r["limit_price"] - limit_price) > EPS:
                        raise TradeError("client_order_id already used for a different order", 409)
                    d = limit_dict(r)
                    d["duplicate"] = True
                    return d
                dup = self._find_dup(c, portfolio_id, client_order_id, symbol, side, qty)  # it filled on arrival
            if dup:
                dup["status"] = "filled"
                return dup

        ref = self._cross_price(symbol, side)
        self._check_limit_feasible(portfolio_id, symbol, side, qty, limit_price)
        buying = side in ("buy", "cover")
        if (buying and ref <= limit_price + EPS) or (not buying and ref >= limit_price - EPS):
            try:
                return self.place_order(portfolio_id, symbol, side, qty, client_order_id, trigger=3, limit_price=limit_price)
            except TradeError as exc:
                if not (exc.status == 409 and str(exc) == NOT_CROSSED):
                    raise  # price moved away in between: fall through and rest instead

        now = time.time()
        try:
            with self.db.tx() as c:
                n = c.execute(
                    "SELECT COUNT(*) AS n FROM limit_orders WHERE portfolio_id=? AND status IN ('open','filling')", (portfolio_id,)
                ).fetchone()["n"]
                if n >= self.s.max_open_limits:
                    raise TradeError(f"too many open limit orders (max {self.s.max_open_limits}); cancel some first", 429)
                cur = c.execute(
                    "INSERT INTO limit_orders (portfolio_id, client_order_id, symbol, side, qty, limit_price, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (portfolio_id, client_order_id, symbol, side, qty, limit_price, now),
                )
                row = c.execute("SELECT * FROM limit_orders WHERE id=?", (cur.lastrowid,)).fetchone()
        except sqlite3.IntegrityError:  # the same client_order_id arrived twice at once
            return self.place_limit_order(portfolio_id, symbol, side, qty, limit_price, client_order_id)
        d = limit_dict(row)
        d["duplicate"] = False
        return d

    def list_limit_orders(self, portfolio_id: int, status: str = "open", limit: int = 50) -> list[dict]:
        where = "AND status IN ('open','filling')" if status == "open" else ""
        with self.db.read() as c:
            rows = c.execute(
                f"SELECT * FROM limit_orders WHERE portfolio_id=? {where} ORDER BY id DESC LIMIT ?", (portfolio_id, limit)
            ).fetchall()
        return [limit_dict(r) for r in rows]

    def cancel_limit_order(self, portfolio_id: int, order_id: int) -> dict:
        with self.db.tx() as c:
            r = c.execute("SELECT * FROM limit_orders WHERE id=? AND portfolio_id=?", (order_id, portfolio_id)).fetchone()
            if r is None:
                raise TradeError("limit order not found", 404)
            if r["status"] == "filling":
                raise TradeError("this order is being filled right now", 409)
            if r["status"] != "open":
                raise TradeError(f"this order is already {r['status']}", 409)
            c.execute("UPDATE limit_orders SET status='cancelled', closed_at=? WHERE id=?", (time.time(), order_id))
            r = c.execute("SELECT * FROM limit_orders WHERE id=?", (order_id,)).fetchone()
        return limit_dict(r)

    def _close_limit(self, order_id: int, status: str, reason: str | None = None, fill_id: int | None = None) -> None:
        with self.db.tx() as c:
            c.execute(
                "UPDATE limit_orders SET status=?, reason=?, order_id=?, closed_at=? WHERE id=?",
                (status, reason, fill_id, time.time(), order_id),
            )

    def _reopen_limit(self, order_id: int) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE limit_orders SET status='open' WHERE id=? AND status='filling'", (order_id,))

    def check_limits(self) -> int:
        """Fill resting limit orders whose price has been reached (fresh quotes only), and expire old ones.
        Returns the number of fills."""
        with self._limit_lock:
            now = time.time()
            # a crash between "claim" and "mark filled" leaves a row in 'filling': settle it from the orders table
            with self.db.read() as c:
                stuck = c.execute("SELECT id FROM limit_orders WHERE status='filling'").fetchall()
                found = {
                    r["id"]: c.execute(
                        "SELECT id FROM orders WHERE client_order_id=? AND portfolio_id=(SELECT portfolio_id FROM limit_orders WHERE id=?)",
                        (f"lim:{r['id']}", r["id"]),
                    ).fetchone()
                    for r in stuck
                }
            for oid, fill in found.items():
                if fill:
                    self._close_limit(oid, "filled", fill_id=fill["id"])
                else:
                    self._reopen_limit(oid)
            if self.s.limit_expiry_days > 0:
                with self.db.tx() as c:
                    c.execute(
                        "UPDATE limit_orders SET status='expired', reason='expired', closed_at=?"
                        " WHERE status='open' AND created_at < ?",
                        (now, now - self.s.limit_expiry_days * 86400),
                    )
            with self.db.read() as c:
                rows = c.execute("SELECT * FROM limit_orders WHERE status='open' ORDER BY id").fetchall()
            ended = self._ended_ids() if rows else set()
            rows = [r for r in rows if r["portfolio_id"] not in ended]
            n = 0
            for r in rows:
                try:
                    px = self._cross_price(r["symbol"], r["side"])  # fresh quotes only
                except PriceError:
                    continue
                if is_option(r["symbol"]) and time.time() > self._opt_end(parse_option(r["symbol"])["expiry"]):
                    continue  # expired contract: settle_expired_options closes the order
                buying = r["side"] in ("buy", "cover")
                if not ((buying and px <= r["limit_price"] + EPS) or (not buying and px >= r["limit_price"] - EPS)):
                    continue
                with self.db.tx() as c:  # claim it, so a cancel arriving now can't also win
                    got = c.execute("UPDATE limit_orders SET status='filling' WHERE id=? AND status='open'", (r["id"],))
                    if got.rowcount == 0:
                        continue
                try:
                    fill = self.place_order(
                        r["portfolio_id"], r["symbol"], r["side"], r["qty"], f"lim:{r['id']}",
                        trigger=3, limit_price=r["limit_price"], passive=True,
                    )
                except PriceError:
                    self._reopen_limit(r["id"])
                    continue
                except TradeError as exc:
                    if exc.status == 409 and str(exc) == NOT_CROSSED:  # price moved back before we could fill
                        self._reopen_limit(r["id"])
                    else:  # cash, shares or margin no longer there
                        self._close_limit(r["id"], "rejected", str(exc))
                        log.warning("limit order %s rejected at fill: %s", r["id"], exc)
                    continue
                self._close_limit(r["id"], "filled", fill_id=fill["id"])
                n += 1
            return n

    # ---------- option expiry ----------

    def settle_expired_options(self) -> int:
        """Cash-settle options once their expiry day is over. Each position is closed at its intrinsic value on
        the expiry day's closing price of the underlying (in the money: the difference per share, otherwise
        zero) and recorded as a sell, so it shows in orders and realized P&L. No shares change hands.
        Waits until 30 minutes after the 4pm close, and for a closing price. Returns positions settled."""
        now = time.time()
        with self.db.read() as c:
            positions = [r for r in c.execute("SELECT portfolio_id, symbol FROM positions") if is_option(r["symbol"])]
            resting = [r for r in c.execute("SELECT id, symbol FROM limit_orders WHERE status IN ('open','filling')")
                       if is_option(r["symbol"])]
        closes: dict[tuple[str, str], float | None] = {}
        for r in resting:  # a resting order on a contract that has expired can never fill
            o = parse_option(r["symbol"])
            if now > self._opt_end(o["expiry"]):
                self._close_limit(r["id"], "expired", "contract expired")
        n = 0
        for r in positions:
            o = parse_option(r["symbol"])
            if now < self._opt_end(o["expiry"]) + 30 * 60:
                continue
            key = (o["underlying"], o["expiry"])
            if key not in closes:
                last_try = self._settle_tried.get(key, 0.0)
                if time.monotonic() - last_try < 300 and last_try:
                    closes[key] = None
                else:
                    self._settle_tried[key] = time.monotonic()
                    closes[key] = self.prices.get_close(o["underlying"], o["expiry"])
            spot = closes[key]
            if spot is None:
                continue  # no closing price yet: try again in a few minutes
            intrinsic = max(spot - o["strike"], 0.0) if o["right"] == "C" else max(o["strike"] - spot, 0.0)
            with self.db.tx() as c:
                pos = c.execute(
                    "SELECT qty, avg_cost FROM positions WHERE portfolio_id=? AND symbol=?", (r["portfolio_id"], r["symbol"])
                ).fetchone()
                if pos is None:
                    continue
                qty, avg = pos["qty"], pos["avg_cost"]
                cash = c.execute("SELECT cash FROM portfolios WHERE id=?", (r["portfolio_id"],)).fetchone()["cash"]
                c.execute("UPDATE portfolios SET cash=? WHERE id=?", (round(cash + qty * intrinsic * OPTION_MULTIPLIER, 6), r["portfolio_id"]))
                try:
                    c.execute(
                        "INSERT INTO orders (portfolio_id, client_order_id, symbol, side, qty, price, fee, realized_pnl, forced, ts)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (r["portfolio_id"], f"exp:{r['symbol']}", r["symbol"], "sell", qty, intrinsic, 0.0,
                         (intrinsic - avg) * qty * OPTION_MULTIPLIER, 4, now),
                    )
                except sqlite3.IntegrityError:
                    continue  # already settled
                c.execute("DELETE FROM positions WHERE portfolio_id=? AND symbol=?", (r["portfolio_id"], r["symbol"]))
            n += 1
            log.warning("settled %s for user %s: %s contracts at %.4f (%s closed %.2f)", r["symbol"], r["portfolio_id"], qty, intrinsic, o["underlying"], spot)
            try:
                self.snapshot_user(r["portfolio_id"])
            except Exception:
                log.exception("snapshot after option settlement failed")
        return n

    # ---------- splits and dividends ----------

    @staticmethod
    def _ex_ts(ex_date: str) -> float:
        """Start of the ex-date, New York time. Shares you held before this moment are entitled."""
        d = datetime.strptime(ex_date, "%Y-%m-%d")
        try:
            from zoneinfo import ZoneInfo

            return d.replace(tzinfo=ZoneInfo("America/New_York")).timestamp()
        except Exception:  # no tzdata: fall back to a fixed UTC-5
            return d.replace(tzinfo=timezone(timedelta(hours=-5))).timestamp()

    def apply_action(self, symbol: str, kind: str, ex_date: str, value: float, source: str = "feed") -> dict | None:
        """Apply one split (value = ratio, 4 for 4-for-1, 0.1 for a 1-for-10 reverse split) or cash dividend
        (value = dollars per share) to every account that held the stock before the ex-date.
        Entitlement comes from each account's own order history, so it is right even if this runs days late.
        Returns None if this exact action was already applied."""
        symbol = normalize_symbol(symbol)
        if kind not in ("split", "dividend"):
            raise TradeError("kind must be 'split' or 'dividend'", 422)
        try:
            ex_ts = self._ex_ts(ex_date)
        except (ValueError, TypeError):
            raise TradeError("ex_date must look like 2026-03-02", 422)
        value = float(value)
        if not (value > 0) or not math.isfinite(value) or (kind == "split" and not 0.001 <= value <= 1000):
            raise TradeError("value must be a positive number (a split ratio is between 0.001 and 1000)", 422)
        now = time.time()
        if ex_ts > now:
            raise TradeError("ex_date is in the future", 422)

        affected: set[int] = set()
        with self.db.tx() as c:
            try:
                c.execute(
                    "INSERT INTO corp_actions (symbol, kind, ex_date, value, source, processed_at) VALUES (?,?,?,?,?,?)",
                    (symbol, kind, ex_date, value, source, now),
                )
            except sqlite3.IntegrityError:
                return None
            # Orders are stored in the share counts of their day, so re-express each one in today's terms by
            # applying the splits that came after it. For a split, only the splits before it count (the position
            # has been adjusted for those and not yet for this one). For a dividend every known split counts,
            # because the feed quotes dividends per share as of today.
            splits = [
                (self._ex_ts(r["ex_date"]), r["value"])
                for r in c.execute("SELECT ex_date, value FROM corp_actions WHERE symbol=? AND kind='split'", (symbol,))
            ]
            entitled: dict[int, float] = {}
            for o in c.execute("SELECT portfolio_id, side, qty, ts FROM orders WHERE symbol=? AND ts < ?", (symbol, ex_ts)):
                f = 1.0
                for s_ts, ratio in splits:
                    if s_ts > o["ts"] and not (kind == "split" and s_ts >= ex_ts):
                        f *= ratio
                sign = 1.0 if o["side"] in ("buy", "cover") else -1.0
                entitled[o["portfolio_id"]] = entitled.get(o["portfolio_id"], 0.0) + sign * o["qty"] * f
            for uid, e in entitled.items():
                e = round(e, 6)  # e > 0 was long going in, e < 0 was short
                if abs(e) < 1e-9:
                    continue
                if kind == "dividend":
                    amount = round(e * value, 6)  # shorts owe the dividend, so this is negative for them
                    cash = c.execute("SELECT cash FROM portfolios WHERE id=?", (uid,)).fetchone()["cash"]
                    c.execute("UPDATE portfolios SET cash=? WHERE id=?", (round(cash + amount, 6), uid))
                    c.execute(
                        "INSERT INTO corp_events (portfolio_id, symbol, kind, ex_date, qty, value, amount, ts) VALUES (?,?,?,?,?,?,?,?)",
                        (uid, symbol, kind, ex_date, e, value, amount, now),
                    )
                    affected.add(uid)
                    continue
                pos = c.execute(
                    "SELECT qty, avg_cost, stop_price FROM positions WHERE portfolio_id=? AND symbol=?", (uid, symbol)
                ).fetchone()
                if pos is None:
                    continue
                q = pos["qty"]
                if q > 0 and e > 0:
                    held = min(q, e)
                elif q < 0 and e < 0:
                    held = max(q, e)
                else:
                    continue  # the current position was opened after the split
                new_qty = round(q + held * (value - 1), 6)
                new_avg = q * pos["avg_cost"] / new_qty  # a split doesn't change what you paid in total
                self._set_position(c, uid, symbol, new_qty, new_avg)
                if pos["stop_price"] is not None and abs(held - q) < 1e-9:
                    c.execute(
                        "UPDATE positions SET stop_price=? WHERE portfolio_id=? AND symbol=?", (pos["stop_price"] / value, uid, symbol)
                    )
                c.execute(
                    "INSERT INTO corp_events (portfolio_id, symbol, kind, ex_date, qty, value, amount, ts) VALUES (?,?,?,?,?,?,?,?)",
                    (uid, symbol, kind, ex_date, held, value, round(new_qty - q, 6), now),
                )
                affected.add(uid)
            if kind == "split":  # resting limit orders were priced in old shares
                c.execute(
                    "UPDATE limit_orders SET qty=ROUND(qty*?, 6), limit_price=limit_price/? WHERE symbol=?"
                    " AND status IN ('open','filling') AND created_at < ?",
                    (value, value, symbol, ex_ts),
                )
        for uid in affected:
            try:
                self.snapshot_user(uid)
            except Exception:
                log.exception("snapshot after corporate action failed")
        log.warning("applied %s %s ex %s value %s (%s) to %d accounts", kind, symbol, ex_date, value, source, len(affected))
        return {"symbol": symbol, "kind": kind, "ex_date": ex_date, "value": value, "accounts": len(affected)}

    def apply_feed_actions(self) -> int:
        """Ask the data feed about splits and dividends for every stock people hold or recently traded."""
        now = time.time()
        with self.db.read() as c:
            syms = {r["symbol"] for r in c.execute("SELECT DISTINCT symbol FROM positions")}
            syms |= {r["symbol"] for r in c.execute("SELECT DISTINCT symbol FROM orders WHERE ts > ?", (now - 10 * 86400,))}
            syms |= {r["symbol"] for r in c.execute("SELECT DISTINCT symbol FROM limit_orders WHERE status IN ('open','filling')")}
        n = 0
        for sym in sorted(s for s in syms if not is_option(s)):
            acts = self.prices.get_actions(sym)
            if acts is None:
                continue  # feed down: try again next time, nothing is marked as done
            with self.db.read() as c:
                first = c.execute("SELECT MIN(ts) AS t FROM orders WHERE symbol=?", (sym,)).fetchone()["t"]
            for key, kind in (("splits", "split"), ("dividends", "dividend")):
                for ex_date, val in sorted(acts.get(key, [])):  # oldest first, splits before dividends
                    try:
                        if first is None or self._ex_ts(ex_date) <= first:
                            continue  # nobody held it before that date
                        if self.apply_action(sym, kind, ex_date, val, "feed"):
                            n += 1
                    except (TradeError, ValueError):
                        continue
        return n

    def maybe_apply_actions(self) -> int:
        """Rate-limited wrapper used by the background loop."""
        every = self.s.actions_check_seconds
        if every <= 0:
            return 0
        now = time.monotonic()
        if self._actions_last is not None and now - self._actions_last < every:
            return 0
        self._actions_last = now
        return self.apply_feed_actions()

    def corp_events(self, portfolio_id: int, limit: int = 50) -> list[dict]:
        with self.db.read() as c:
            rows = c.execute(
                "SELECT * FROM corp_events WHERE portfolio_id=? ORDER BY id DESC LIMIT ?", (portfolio_id, limit)
            ).fetchall()
        return [
            {
                "id": r["id"], "symbol": r["symbol"], "kind": r["kind"], "ex_date": r["ex_date"],
                "qty": r["qty"], "value": r["value"], "amount": round(r["amount"], 6), "ts": r["ts"], "time": iso(r["ts"]),
            }
            for r in rows
        ]

    # ---------- stops ----------

    @staticmethod
    def _check_stop(side: str, stop: float, price: float) -> float:
        """A stop is a per-share price on an open position: below the market for a long,
        above it for a short. Anything else would trigger immediately."""
        if not (stop > 0) or not math.isfinite(stop):
            raise TradeError("stop_price must be a positive number", 422)
        if side in ("buy", "sell") and stop >= price:
            raise TradeError(f"stop_price must be below the current price ({price:.2f}) for a long position", 422)
        if side in ("short", "cover") and stop <= price:
            raise TradeError(f"stop_price must be above the current price ({price:.2f}) for a short position", 422)
        return stop

    @staticmethod
    def _store_stop(c, portfolio_id: int, symbol: str, stop: float | None) -> None:
        if stop is not None:
            c.execute("UPDATE positions SET stop_price=? WHERE portfolio_id=? AND symbol=?", (stop, portfolio_id, symbol))

    def set_stop(self, portfolio_id: int, symbol: str, stop_price: float | None) -> dict:
        """Set, change or (with None) clear the stop on an open position."""
        symbol = normalize_symbol(symbol)
        with self.db.read() as c:
            pos = c.execute("SELECT qty FROM positions WHERE portfolio_id=? AND symbol=?", (portfolio_id, symbol)).fetchone()
        if pos is None:
            raise TradeError(f"you have no position in {symbol}", 404)
        if stop_price is not None:
            stop_price = self._check_stop("short" if pos["qty"] < 0 else "buy", float(stop_price), self.prices.get(symbol))
        with self.db.tx() as c:
            cur = c.execute(
                "UPDATE positions SET stop_price=? WHERE portfolio_id=? AND symbol=?", (stop_price, portfolio_id, symbol)
            )
            if cur.rowcount == 0:
                raise TradeError(f"you have no position in {symbol}", 404)
        return {"symbol": symbol, "stop_price": stop_price}

    def check_stops(self) -> int:
        """Close every position whose stop has been hit: longs at or below the stop are sold,
        shorts at or above it are covered. Fills at the live price (with slippage and fees),
        not at the stop. Returns the number of orders placed."""
        with self.db.read() as c:
            rows = c.execute("SELECT portfolio_id, symbol FROM positions WHERE stop_price IS NOT NULL").fetchall()
        ended = self._ended_ids() if rows else set()
        rows = [r for r in rows if r["portfolio_id"] not in ended]
        if not rows:
            return 0
        n = 0
        for r in rows:
            try:
                px = self.prices.get(r["symbol"])  # fresh quote only: never trigger on stale data
            except PriceError:
                continue
            with self.db.read() as c:  # re-read: the member may have traded since the scan
                pos = c.execute(
                    "SELECT qty, stop_price FROM positions WHERE portfolio_id=? AND symbol=?", (r["portfolio_id"], r["symbol"])
                ).fetchone()
            if pos is None or pos["stop_price"] is None:
                continue
            qty, stop = pos["qty"], pos["stop_price"]
            if qty > 0 and px <= stop:
                side = "sell"
            elif qty < 0 and px >= stop:
                side = "cover"
            else:
                continue
            try:
                self.place_order(r["portfolio_id"], r["symbol"], side, abs(qty), trigger=2)
                n += 1
                log.warning("stop hit: user %s %s %s at %.4f (stop %.4f)", r["portfolio_id"], side, r["symbol"], px, stop)
            except (TradeError, PriceError) as exc:
                log.error("stop order failed for user %s %s: %s", r["portfolio_id"], r["symbol"], exc)
        return n

    # ---------- liquidation ----------

    def liquidate_user(self, portfolio_id: int) -> list[dict]:
        """Force-cover shorts when cash falls below the maintenance level.

        Trigger: cash < (1 + maintenance) * short value. Cure: cover the largest short first,
        just enough to get back to the initial level (free cash >= 0). Fills use the normal
        cover price, so slippage and commission apply. Returns the forced orders placed."""
        if not (self.s.auto_liquidate and self.s.allow_shorting):
            return []
        m = self.s.short_margin
        placed: list[dict] = []
        for _ in range(25):  # a few passes at most: fees and slippage can leave a sliver uncured
            with self.db.read() as c:
                cash = c.execute("SELECT cash FROM portfolios WHERE id=?", (portfolio_id,)).fetchone()["cash"]
                shorts = c.execute("SELECT symbol, qty FROM positions WHERE portfolio_id=? AND qty < 0", (portfolio_id,)).fetchall()
            if not shorts:
                break
            marks = {}
            try:
                for r in shorts:
                    marks[r["symbol"]] = self.prices.get(r["symbol"])  # fresh quote or nothing
            except PriceError as exc:
                log.warning("liquidation of user %s skipped, no price: %s", portfolio_id, exc)
                break  # never liquidate on stale or missing data
            values = {r["symbol"]: -r["qty"] * marks[r["symbol"]] for r in shorts}
            short_value = sum(values.values())
            if cash >= (1 + self.maint) * short_value - EPS:
                break  # healthy
            free = cash - (1 + m) * short_value
            if free >= -EPS:
                break  # between maintenance and initial: warned, not liquidated
            sym = max(values, key=values.get)
            held = next(-r["qty"] for r in shorts if r["symbol"] == sym)
            mark = marks[sym]
            fill = mark * (1 + self.s.slippage_bps / 10_000)  # covers fill above the quote
            # covering x shares costs x*fill + fee in cash and releases (1+m)*x*mark of required collateral
            gain = (1 + m) * mark - fill
            need = (-free + self.s.commission) / gain if gain > EPS else held
            qty = min(held, math.ceil(need * 1e6) / 1e6)
            if qty < 1e-6:
                break
            try:
                placed.append(self.place_order(portfolio_id, sym, "cover", qty, trigger=1))
            except TradeError as exc:  # should not happen (covering is always allowed), but never loop on it
                log.error("forced cover failed for user %s %s: %s", portfolio_id, sym, exc)
                break
        if placed:
            log.warning("liquidated user %s: %s", portfolio_id, [(o["symbol"], o["qty"], o["price"]) for o in placed])
        return placed

    def liquidate_all(self) -> int:
        """Check every account that holds a short. Returns the number of forced orders."""
        if not (self.s.auto_liquidate and self.s.allow_shorting):
            return 0
        with self.db.read() as c:
            ids = [r["portfolio_id"] for r in c.execute("SELECT DISTINCT portfolio_id FROM positions WHERE qty < 0")]
        if ids:
            ended = self._ended_ids()
            ids = [i for i in ids if i not in ended]
        n = 0
        for uid in ids:
            try:
                n += len(self.liquidate_user(uid))
            except Exception:
                log.exception("liquidation check failed for user %s", uid)
        return n

    # ---------- valuation ----------

    @staticmethod
    def _value_positions(positions, prices) -> tuple[list[dict], float]:
        """Market value is signed: positive for longs, negative for shorts (a liability)."""
        out, total = [], 0.0
        for p in positions:
            px = prices.get(p["symbol"])
            stale = px is None
            if stale:
                px = p["avg_cost"]
            o = parse_option(p["symbol"])
            mult = OPTION_MULTIPLIER if o else 1  # an option price is per share, a contract is 100 shares
            mv = p["qty"] * px * mult
            cost = p["qty"] * p["avg_cost"] * mult
            unreal = mv - cost  # for a short this is (avg - price) * |qty|, so falling prices profit
            out.append(
                {
                    "multiplier": mult,
                    "option": (
                        {**o, "label": f"{o['underlying']} {o['expiry']} {o['strike']:g} {'Call' if o['right'] == 'C' else 'Put'}"}
                        if o else None
                    ),
                    "symbol": p["symbol"],
                    "side": "short" if p["qty"] < 0 else "long",
                    "qty": p["qty"],
                    "avg_cost": round(p["avg_cost"], 4),
                    "price": round(px, 4),
                    "market_value": round(mv, 2),
                    "unrealized_pnl": round(unreal, 2),
                    "unrealized_pnl_pct": round(unreal / abs(cost) * 100, 2) if cost else 0.0,
                    "price_stale": stale,
                    "stop_price": p["stop_price"],
                }
            )
            total += mv
        return out, total

    def portfolio(self, portfolio_id: int) -> dict:
        with self.db.read() as c:
            u = c.execute(
                "SELECT p.*, u.username, u.is_admin FROM portfolios p JOIN users u ON u.id=p.user_id WHERE p.id=?", (portfolio_id,)
            ).fetchone()
            if u is None:
                raise TradeError("portfolio not found", 404)
            positions = c.execute("SELECT * FROM positions WHERE portfolio_id=? ORDER BY symbol", (portfolio_id,)).fetchall()
            k = c.execute("SELECT * FROM competitions WHERE id=?", (u["competition_id"],)).fetchone() if u["competition_id"] else None
        prices = self.prices.get_many([p["symbol"] for p in positions])
        pos_out, mv = self._value_positions(positions, prices)
        frozen = u["final_equity"] is not None
        equity = u["final_equity"] if frozen else u["cash"] + mv
        long_value = sum(p["market_value"] for p in pos_out if p["qty"] > 0)
        short_value = -sum(p["market_value"] for p in pos_out if p["qty"] < 0)
        m = self.s.short_margin
        comp = self._comp_dict(k, time.time()) if k else None
        shorting = self.s.allow_shorting and (comp["allow_shorting"] if comp else True)
        return {
            "portfolio_id": u["id"],
            "portfolio": u["name"],
            "is_default": bool(u["is_default"]),
            "competition": comp,
            "frozen": frozen,
            "username": u["username"],
            "is_admin": bool(u["is_admin"]),
            "cash": round(u["cash"], 2),
            "positions_value": round(mv, 2),
            "long_value": round(long_value, 2),
            "short_value": round(short_value, 2),
            "free_cash": round(u["cash"] - (1 + m) * short_value, 2),
            "margin_call": bool(short_value > 0 and u["cash"] < (1 + self.maint) * short_value),
            "auto_liquidate": self.s.auto_liquidate and shorting,
            "shorting_enabled": shorting,
            "options_enabled": self.s.allow_options and (comp["allow_options"] if comp else True),
            "trading_open": comp is None or comp["status"] == "active",
            "short_margin": m,
            "equity": round(equity, 2),
            "starting_cash": u["starting_cash"],
            "pnl": round(equity - u["starting_cash"], 2),
            "return_pct": round((equity / u["starting_cash"] - 1) * 100, 3),
            "max_drawdown_pct": round(u["max_drawdown"] * 100, 2),
            "positions": pos_out,
        }

    def orders(self, portfolio_id: int, limit: int = 100) -> list[dict]:
        with self.db.read() as c:
            rows = c.execute(
                "SELECT * FROM orders WHERE portfolio_id=? ORDER BY id DESC LIMIT ?", (portfolio_id, limit)
            ).fetchall()
        return [order_dict(r) for r in rows]

    @staticmethod
    def _scope_sql(scope) -> tuple[str, list]:
        """Which portfolios a ranking covers: the default "Main" ones (None), one competition (its id), or all."""
        if scope is None or scope == "main":
            return "p.is_default=1", []
        if scope == "all":
            return "1=1", []
        return "p.competition_id=?", [int(scope)]

    def summaries(self, detail: bool = False, scope=None, user_id: int | None = None, only: int | None = None) -> list[dict]:
        """One row per portfolio with live equity (frozen once a competition has ended), ranked by return.
        scope: None = everyone's Main portfolio, a competition id, or "all". Used by the leaderboards and admin panel."""
        where, params = self._scope_sql(scope)
        if user_id is not None:
            where += " AND p.user_id=?"
            params.append(user_id)
        if only is not None:
            where += " AND p.id=?"
            params.append(only)
        with self.db.read() as c:
            pfs = c.execute(
                f"SELECT p.*, u.username, k.name AS comp_name FROM portfolios p JOIN users u ON u.id=p.user_id"
                f" LEFT JOIN competitions k ON k.id=p.competition_id WHERE {where}", params
            ).fetchall()
            ids = [p["id"] for p in pfs]
            marks = ",".join("?" * len(ids)) or "NULL"
            positions = c.execute(f"SELECT * FROM positions WHERE portfolio_id IN ({marks})", ids).fetchall()
            stats = {
                r["portfolio_id"]: r
                for r in c.execute(
                    "SELECT portfolio_id, COUNT(*) AS n,"
                    " SUM(CASE WHEN side IN ('sell','cover') THEN 1 ELSE 0 END) AS sells,"
                    " SUM(CASE WHEN side IN ('sell','cover') AND realized_pnl > 0 THEN 1 ELSE 0 END) AS wins"
                    f" FROM orders WHERE portfolio_id IN ({marks}) GROUP BY portfolio_id", ids
                )
            }
        prices = self.prices.get_many([p["symbol"] for p in positions])
        by_pf: dict[int, list] = {}
        for p in positions:
            by_pf.setdefault(p["portfolio_id"], []).append(p)

        rows = []
        for u in pfs:
            pos_out, mv = self._value_positions(by_pf.get(u["id"], []), prices)
            frozen = u["final_equity"] is not None
            equity = u["final_equity"] if frozen else u["cash"] + mv
            st = stats.get(u["id"])
            sells = st["sells"] if st else 0
            row = {
                "portfolio_id": u["id"],
                "portfolio": u["name"],
                "is_default": bool(u["is_default"]),
                "competition_id": u["competition_id"],
                "competition": u["comp_name"],
                "user_id": u["user_id"],
                "username": u["username"],
                "equity": round(equity, 2),
                "cash": round(u["cash"], 2),
                "return_pct": round((equity / u["starting_cash"] - 1) * 100, 3),
                "max_drawdown_pct": round(u["max_drawdown"] * 100, 2),
                "trades": st["n"] if st else 0,
                "win_rate_pct": round(st["wins"] / sells * 100, 1) if sells else None,
                "n_positions": len(pos_out),
                "frozen": frozen,
                "created_at": u["created_at"],
            }
            if detail:
                row["positions"] = pos_out
                row["starting_cash"] = u["starting_cash"]
            rows.append(row)
        rows.sort(key=lambda r: r["return_pct"], reverse=True)
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        return rows

    # ---------- history ----------

    def _record(self, c, portfolio_id: int, equity: float, ts: float) -> None:
        u = c.execute("SELECT peak_equity, max_drawdown FROM portfolios WHERE id=?", (portfolio_id,)).fetchone()
        if u is None:
            return
        if c.execute(  # a finished competition's curve stops at its end
            "SELECT 1 FROM portfolios p JOIN competitions k ON k.id=p.competition_id"
            " WHERE p.id=? AND k.end_ts IS NOT NULL AND k.end_ts <= ?", (portfolio_id, ts),
        ).fetchone():
            return
        peak = max(u["peak_equity"], equity)
        dd = (peak - equity) / peak if peak > 0 else 0.0
        c.execute(
            "UPDATE portfolios SET peak_equity=?, max_drawdown=? WHERE id=?", (peak, max(u["max_drawdown"], dd), portfolio_id)
        )
        c.execute("INSERT INTO snapshots (portfolio_id, ts, equity) VALUES (?,?,?)", (portfolio_id, ts, round(equity, 2)))

    def snapshot_user(self, portfolio_id: int) -> None:
        equity = self.portfolio(portfolio_id)["equity"]
        with self.db.tx() as c:
            self._record(c, portfolio_id, equity, time.time())

    def snapshot_all(self) -> None:
        rows = self.summaries(scope="all")
        now = time.time()
        with self.db.tx() as c:
            for r in rows:
                if not r["frozen"]:
                    self._record(c, r["portfolio_id"], r["equity"], now)

    @staticmethod
    def _curve_rows(c, portfolio_id: int, max_points: int, since=None, until=None) -> list:
        """Snapshots for one user inside [since, until], thinned inside SQLite to roughly
        max_points rows (first and last are always kept) so long histories never get
        loaded into Python."""
        where, params = "portfolio_id=?", [portfolio_id]
        if since is not None:
            where += " AND ts >= ?"
            params.append(since)
        if until is not None:
            where += " AND ts <= ?"
            params.append(until)
        n = c.execute(f"SELECT COUNT(*) FROM snapshots WHERE {where}", params).fetchone()[0]
        if n <= max_points:
            return c.execute(f"SELECT ts, equity FROM snapshots WHERE {where} ORDER BY ts", params).fetchall()
        step = -(-n // max_points)  # ceil division
        return c.execute(
            "SELECT ts, equity FROM ("
            f" SELECT ts, equity, ROW_NUMBER() OVER (ORDER BY ts, id) AS rn FROM snapshots WHERE {where}"
            ") WHERE (rn - 1) % ? = 0 OR rn = ? ORDER BY ts",
            params + [step, n],
        ).fetchall()

    @staticmethod
    def _carry_in(c, portfolio_id: int, since: float):
        """Equity as of the start of a range: the last snapshot taken before it."""
        return c.execute(
            "SELECT equity FROM snapshots WHERE portfolio_id=? AND ts < ? ORDER BY ts DESC LIMIT 1", (portfolio_id, since)
        ).fetchone()

    def equity_curve(self, portfolio_id: int, max_points: int = 1500, since=None, until=None) -> list[dict]:
        with self.db.read() as c:
            rows = self._curve_rows(c, portfolio_id, max_points, since, until)
            carry = self._carry_in(c, portfolio_id, since) if since is not None else None
        out = [{"ts": r["ts"], "equity": r["equity"]} for r in rows]
        if carry is not None:  # draw the line from the range start, not from the first in-range snapshot
            out.insert(0, {"ts": since, "equity": carry["equity"]})
        return out

    def all_curves(self, max_points: int = 800, since=None, until=None, scope=None) -> list[dict]:
        """Return-% curves for every portfolio in scope, aligned to one shared start.

        The shared start is `since`, or the oldest portfolio's creation time. Portfolios created
        later are shown as if they had existed from that start (flat at 0%, flagged
        `synthetic`); their real first snapshot is flagged `created` so the UI can mark it."""
        where, params = self._scope_sql(scope)
        with self.db.read() as c:
            users = c.execute(
                "SELECT p.id, p.name, p.is_default, u.username, p.starting_cash, p.created_at"
                f" FROM portfolios p JOIN users u ON u.id=p.user_id WHERE {where} ORDER BY u.username, p.id", params
            ).fetchall()
            if not users:
                return []
            start = since if since is not None else min(u["created_at"] for u in users)
            out = []
            for u in users:
                rows = self._curve_rows(c, u["id"], max_points, since, until)
                pts = []
                if u["created_at"] > start:
                    pts.append({"ts": start, "return_pct": 0.0, "synthetic": True})
                elif since is not None:
                    carry = self._carry_in(c, u["id"], since)
                    if carry is not None:
                        pts.append(
                            {"ts": since, "return_pct": round((carry["equity"] / u["starting_cash"] - 1) * 100, 3)}
                        )
                for k, r in enumerate(rows):
                    p = {"ts": r["ts"], "return_pct": round((r["equity"] / u["starting_cash"] - 1) * 100, 3)}
                    if k == 0 and u["created_at"] >= start:
                        p["created"] = True  # real start of this portfolio's history
                    pts.append(p)
                if not rows and until is not None and u["created_at"] > until:
                    pts.append({"ts": until, "return_pct": 0.0, "synthetic": True})
                label = u["username"] if (u["is_default"] or scope not in (None, "all", "main")) else f"{u['username']} / {u['name']}"
                if scope == "all" and not u["is_default"]:
                    label = f"{u['username']} / {u['name']}"
                out.append(
                    {"username": label, "portfolio_id": u["id"], "created_at": u["created_at"], "start": start, "points": pts}
                )
        return out
