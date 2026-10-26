import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .broker import Broker, TradeError
from .db import Database
from .market import is_us_market_open
from .prices import MockProvider, PriceError, PriceService, YFinanceProvider, is_option, normalize_symbol, parse_option
from .settings import Settings

log = logging.getLogger("paper_trader")
STATIC = Path(__file__).parent / "static"


class AdminFlagIn(BaseModel):
    is_admin: bool


class RegisterIn(BaseModel):
    username: str = Field(max_length=64)
    invite_code: str = Field(max_length=100)


class InvitesIn(BaseModel):
    count: int = Field(default=1, ge=1, le=200)
    label: Optional[str] = Field(default=None, max_length=60)
    max_uses: int = Field(default=1, ge=1, le=1000)
    expires_at: Optional[float] = Field(default=None, ge=0)  # unix seconds; empty = never


class NewUserIn(BaseModel):
    username: str


class OrderIn(BaseModel):
    portfolio: Optional[str] = Field(default=None, max_length=40)  # id or name; the main portfolio when left out
    symbol: str = Field(min_length=1, max_length=24)
    side: Literal["buy", "sell", "short", "cover"]
    qty: float = Field(gt=0, lt=1e9)
    client_order_id: Optional[str] = Field(default=None, max_length=64)
    stop_price: Optional[float] = Field(default=None, gt=0, lt=1e12)  # buy/short only
    limit_price: Optional[float] = Field(default=None, gt=0, lt=1e12)  # makes it a limit order


class ActionIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)
    kind: Literal["split", "dividend"]
    ex_date: str = Field(min_length=10, max_length=10)  # YYYY-MM-DD
    value: float = Field(gt=0, lt=1e9)  # split ratio (4 = 4-for-1) or dividend per share


class PortfolioIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class JoinIn(BaseModel):
    code: str = Field(min_length=1, max_length=40)


class CompetitionIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    starting_cash: Optional[float] = Field(default=None, gt=0, lt=1e12)
    start_ts: Optional[float] = Field(default=None, ge=0)  # unix seconds; empty = open now
    end_ts: Optional[float] = Field(default=None, ge=0)  # unix seconds; empty = no end
    allow_options: bool = True
    allow_shorting: bool = True


class MemberIn(BaseModel):
    username: str = Field(min_length=1, max_length=40)


class StopIn(BaseModel):
    stop_price: Optional[float] = Field(default=None, gt=0, lt=1e12)  # null clears the stop


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.db_path)
    provider = MockProvider() if settings.price_provider == "mock" else YFinanceProvider()
    prices = PriceService(provider, settings.quote_ttl)
    broker = Broker(db, prices, settings)

    async def run_step(label: str, fn):
        try:
            await asyncio.to_thread(fn)
        except Exception:
            log.exception("periodic %s failed", label)

    async def snapshot_loop():
        while True:
            await asyncio.sleep(settings.snapshot_seconds)
            # splits and dividends are looked up at any hour, so they are in before the market opens
            await run_step("corporate actions check", broker.maybe_apply_actions)
            await run_step("option expiry settlement", broker.settle_expired_options)
            await run_step("competition close-out", broker.finalize_competitions)
            if settings.snapshot_market_hours_only and not is_us_market_open():
                continue  # orders still snapshot immediately; this only skips idle timed points
            # these all run before the snapshot so the saved point includes their fills
            await run_step("limit order check", broker.check_limits)
            await run_step("stop check", broker.check_stops)
            await run_step("liquidation check", broker.liquidate_all)
            await run_step("snapshot", broker.snapshot_all)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(snapshot_loop()) if settings.snapshot_seconds > 0 else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="OQG Paper Trader", lifespan=lifespan)
    app.state.broker = broker
    app.state.provider = provider
    app.state.settings = settings

    @app.exception_handler(TradeError)
    async def trade_error_handler(request, exc: TradeError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    @app.exception_handler(PriceError)
    async def price_error_handler(request, exc: PriceError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    # ---------- auth ----------

    def current_user(x_api_key: str | None = Header(default=None)):
        if not x_api_key:
            raise HTTPException(401, "missing X-API-Key header")
        user = broker.user_by_key(x_api_key)
        if user is None:
            raise HTTPException(401, "invalid API key")
        return user

    def time_range(since: Optional[float] = Query(default=None, ge=0), until: Optional[float] = Query(default=None, ge=0)):
        if since is not None and until is not None and until < since:
            raise HTTPException(422, "until must not be before since")
        return since, until

    def current_pid(
        user=Depends(current_user),
        portfolio: Optional[str] = Query(default=None, max_length=40),
        x_portfolio: Optional[str] = Header(default=None),
    ) -> int:
        """The portfolio a request is about: ?portfolio= or the X-Portfolio header (id or name), else the main one."""
        return broker.resolve_portfolio(user["id"], portfolio or x_portfolio)

    OWNER = "owner"

    def require_admin(x_admin_token: str | None = Header(default=None), x_api_key: str | None = Header(default=None)):
        """The owner (PT_ADMIN_TOKEN) or a member an owner has made an admin (their own API key).
        Returns who it is: {"role": "owner"|"admin", "name": ...}."""
        if x_admin_token:
            if not settings.admin_token:
                raise HTTPException(403, "admin endpoints are disabled (PT_ADMIN_TOKEN not set)")
            if not secrets.compare_digest(x_admin_token, settings.admin_token):
                raise HTTPException(401, "invalid admin token")
            return {"role": "owner", "name": OWNER}
        if x_api_key:
            user = broker.user_by_key(x_api_key)
            if user is None:
                raise HTTPException(401, "invalid API key")
            if not user["is_admin"]:
                raise HTTPException(403, "this account is not an admin")
            return {"role": "admin", "name": user["username"], "user_id": user["id"]}
        if not settings.admin_token:
            raise HTTPException(403, "admin endpoints are disabled (PT_ADMIN_TOKEN not set)")
        raise HTTPException(401, "invalid admin token")

    def require_owner(who=Depends(require_admin)):
        if who["role"] != "owner":
            raise HTTPException(403, "only the owner (admin token) can do that")
        return who

    def not_an_admin_unless_owner(who, user_id: int):
        """Admins can't take over, or delete, other admins - only the owner can."""
        if who["role"] != "owner" and broker.is_admin_user(user_id):
            raise HTTPException(403, "only the owner can do that to an admin account")

    # ---------- public / user API ----------

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.post("/api/register")
    def register(body: RegisterIn):
        """Create your account with a sign-up code from an admin. The API key is shown once."""
        _, api_key = broker.register(body.username, body.invite_code)
        return {"username": body.username, "api_key": api_key, "note": "store this key, it is shown only once"}

    @app.get("/api/me")
    def me(pid=Depends(current_pid)):
        return broker.portfolio(pid)

    @app.get("/api/portfolio")
    def portfolio(pid=Depends(current_pid)):
        return broker.portfolio(pid)

    @app.get("/api/portfolios")
    def my_portfolios(user=Depends(current_user)):
        """All your portfolios (main, extra and competition ones) with their current value."""
        return broker.summaries(scope="all", user_id=user["id"])

    @app.post("/api/portfolios")
    def new_portfolio(body: PortfolioIn, user=Depends(current_user)):
        return broker.create_portfolio(user["id"], body.name)

    @app.delete("/api/portfolios/{ref}")
    def drop_portfolio(ref: str, user=Depends(current_user)):
        broker.delete_portfolio(user["id"], broker.resolve_portfolio(user["id"], ref))
        return {"ok": True}

    @app.get("/api/competitions")
    def my_competitions(user=Depends(current_user)):
        """The competitions you are in."""
        return broker.list_competitions(user["id"])

    @app.post("/api/competitions/join")
    def join_competition(body: JoinIn, user=Depends(current_user)):
        return broker.join_competition(user["id"], body.code)

    def _member_of(competition_id: int, user) -> None:
        if not any(c["id"] == competition_id for c in broker.list_competitions(user["id"])):
            raise HTTPException(404, "you are not in that competition")

    @app.get("/api/competitions/{competition_id}/leaderboard")
    def competition_leaderboard(competition_id: int, user=Depends(current_user)):
        _member_of(competition_id, user)
        return broker.summaries(scope=competition_id)

    @app.get("/api/competitions/{competition_id}/curves")
    def competition_curves(competition_id: int, rng=Depends(time_range), user=Depends(current_user)):
        _member_of(competition_id, user)
        return broker.all_curves(since=rng[0], until=rng[1], scope=competition_id)

    @app.get("/api/quote/{symbol}")
    def quote(symbol: str, user=Depends(current_user)):
        sym = normalize_symbol(symbol)
        if is_option(sym):
            if not settings.allow_options:
                raise HTTPException(403, "options are disabled on this server")
            q = prices.get_quote(sym)
            return {"symbol": sym, "price": q["mark"], "bid": q["bid"], "ask": q["ask"], "last": q["last"],
                    "multiplier": 100, "option": parse_option(sym)}
        return {"symbol": sym, "price": prices.get(sym)}

    def _underlying(symbol: str) -> str:
        u = normalize_symbol(symbol)
        if not settings.allow_options:
            raise HTTPException(403, "options are disabled on this server")
        if is_option(u) or u.startswith("^"):
            raise HTTPException(422, "give the stock or ETF ticker, for example SPY")
        return u

    @app.get("/api/options/{underlying}/expiries")
    def option_expiries(underlying: str, user=Depends(current_user)):
        """Expiry dates that have listed options, soonest first."""
        u = _underlying(underlying)
        return {"underlying": u, "expiries": prices.get_expiries(u)}

    @app.get("/api/options/{underlying}")
    def option_chain(underlying: str, expiry: str = Query(pattern=r"^\d{4}-\d{2}-\d{2}$"), user=Depends(current_user)):
        """The calls and puts for one expiry. Prices are per share; a contract is 100 shares."""
        u = _underlying(underlying)
        chain = prices.get_chain(u, expiry)
        try:
            spot = prices.get(u)
        except PriceError:
            spot = None
        return {"underlying": u, "expiry": expiry, "multiplier": 100, "underlying_price": spot, **chain}

    @app.post("/api/orders")
    def place_order(
        body: OrderIn,
        user=Depends(current_user),
        portfolio: Optional[str] = Query(default=None, max_length=40),
        x_portfolio: Optional[str] = Header(default=None),
    ):
        pid = broker.resolve_portfolio(user["id"], body.portfolio or portfolio or x_portfolio)
        if body.limit_price is not None:
            if body.stop_price is not None:
                raise HTTPException(422, "stop_price can't be combined with limit_price; set the stop after the order fills")
            return broker.place_limit_order(pid, body.symbol, body.side, body.qty, body.limit_price, body.client_order_id)
        return broker.place_order(pid, body.symbol, body.side, body.qty, body.client_order_id, body.stop_price)

    @app.get("/api/open-orders")
    def open_orders(status: Literal["open", "all"] = "open", limit: int = 50, pid=Depends(current_pid)):
        """Resting limit orders. status=all also lists recent filled, cancelled, expired and rejected ones."""
        return broker.list_limit_orders(pid, status, max(1, min(limit, 200)))

    @app.delete("/api/open-orders/{order_id}")
    def cancel_open_order(order_id: int, pid=Depends(current_pid)):
        return broker.cancel_limit_order(pid, order_id)

    @app.get("/api/corporate-actions")
    def corporate_actions(limit: int = 50, pid=Depends(current_pid)):
        """Splits and dividends that were applied to your account."""
        return broker.corp_events(pid, max(1, min(limit, 200)))

    @app.put("/api/positions/{symbol}/stop")
    def set_stop(symbol: str, body: StopIn, pid=Depends(current_pid)):
        return broker.set_stop(pid, symbol, body.stop_price)

    @app.get("/api/orders")
    def list_orders(limit: int = 100, pid=Depends(current_pid)):
        return broker.orders(pid, max(1, min(limit, 1000)))

    @app.get("/api/equity")
    def equity(rng=Depends(time_range), pid=Depends(current_pid)):
        """Equity snapshots. Optional ?since= and ?until= (unix seconds) limit the range."""
        return broker.equity_curve(pid, since=rng[0], until=rng[1])

    @app.get("/api/leaderboard")
    def leaderboard(user=Depends(current_user)):
        return broker.summaries()

    # ---------- admin API ----------

    def scope_param(scope: str = Query(default="main", pattern=r"^(main|all|\d+)$")):
        return int(scope) if scope.isdigit() else scope

    @app.get("/api/admin/portfolios")
    def admin_portfolios(scope=Depends(scope_param), _=Depends(require_admin)):
        """scope=main (everyone's main portfolio, the default), a competition id, or all."""
        return broker.summaries(detail=True, scope=scope)

    @app.get("/api/admin/curves")
    def admin_curves(rng=Depends(time_range), scope=Depends(scope_param), _=Depends(require_admin)):
        return broker.all_curves(since=rng[0], until=rng[1], scope=scope)

    @app.get("/api/admin/competitions")
    def admin_competitions(_=Depends(require_admin)):
        return broker.list_competitions()

    @app.post("/api/admin/competitions")
    def admin_new_competition(body: CompetitionIn, who=Depends(require_admin)):
        res = broker.create_competition(
            body.name, body.starting_cash, body.start_ts, body.end_ts, body.allow_options, body.allow_shorting
        )
        broker.log_admin(who["name"], "created competition", body.name)
        return res

    @app.post("/api/admin/competitions/{competition_id}/members")
    def admin_add_member(competition_id: int, body: MemberIn, who=Depends(require_admin)):
        res = broker.add_member(competition_id, body.username)
        broker.log_admin(who["name"], "added to competition", f"{body.username} -> competition {competition_id}")
        return res

    @app.delete("/api/admin/competitions/{competition_id}/members/{portfolio_id}")
    def admin_remove_member(competition_id: int, portfolio_id: int, who=Depends(require_admin)):
        broker.remove_member(competition_id, portfolio_id)
        broker.log_admin(who["name"], "removed from competition", f"portfolio {portfolio_id} from competition {competition_id}")
        return {"ok": True}

    @app.post("/api/admin/competitions/{competition_id}/end")
    def admin_end_competition(competition_id: int, who=Depends(require_admin)):
        res = broker.end_competition(competition_id)
        broker.log_admin(who["name"], "ended competition", f"competition {competition_id}")
        return res

    @app.delete("/api/admin/competitions/{competition_id}")
    def admin_delete_competition(competition_id: int, who=Depends(require_admin)):
        broker.delete_competition(competition_id)
        broker.log_admin(who["name"], "deleted competition", f"competition {competition_id}")
        return {"ok": True}

    @app.post("/api/rotate-key")
    def rotate_key(user=Depends(current_user), x_api_key: str = Header()):
        """Get a new API key. The key you send now stops working, so save the new one."""
        return {"api_key": broker.rotate_key(user["id"], x_api_key), "note": "store this key, it is shown only once"}

    @app.post("/api/admin/portfolios/{portfolio_id}/reset")
    def admin_reset_portfolio(portfolio_id: int, who=Depends(require_admin)):
        broker.reset_portfolio(portfolio_id)
        broker.log_admin(who["name"], "reset portfolio", f"portfolio {portfolio_id}")
        return {"ok": True}

    @app.post("/api/admin/users")
    def admin_create_user(body: NewUserIn, who=Depends(require_admin)):
        _, api_key = broker.create_user(body.username)
        broker.log_admin(who["name"], "created account", body.username)
        return {"username": body.username, "api_key": api_key, "note": "store this key, it is shown only once"}

    @app.get("/api/admin/users")
    def admin_users(_=Depends(require_admin)):
        """Every account, newest first."""
        return broker.list_users()

    @app.post("/api/admin/corporate-actions")
    def admin_corporate_action(body: ActionIn, who=Depends(require_admin)):
        """Enter a split or dividend by hand (for when the data feed misses one)."""
        res = broker.apply_action(body.symbol, body.kind, body.ex_date, body.value, source="admin")
        if res is None:
            raise HTTPException(409, "that action was already applied")
        broker.log_admin(who["name"], "entered " + body.kind, f"{body.symbol} {body.ex_date} {body.value}")
        return res

    @app.post("/api/admin/corporate-actions/check")
    def admin_check_actions(_=Depends(require_admin)):
        """Run the data-feed check for splits and dividends now."""
        return {"applied": broker.apply_feed_actions()}

    @app.get("/api/admin/invites")
    def admin_invites(_=Depends(require_admin)):
        return broker.list_invites()

    @app.post("/api/admin/invites")
    def admin_new_invites(body: InvitesIn, who=Depends(require_admin)):
        """Make sign-up codes: each works once by default (max_uses), until it expires or is revoked."""
        res = broker.create_invites(body.count, body.label, body.max_uses, body.expires_at)
        broker.log_admin(who["name"], "made sign-up codes", f"{body.count} ({body.label or 'no label'})")
        return res

    @app.delete("/api/admin/invites/{invite_id}")
    def admin_revoke_invite(invite_id: int, who=Depends(require_admin)):
        broker.revoke_invite(invite_id)
        broker.log_admin(who["name"], "revoked sign-up code", f"code {invite_id}")
        return {"ok": True}

    @app.post("/api/admin/users/{user_id}/key")
    def admin_new_key(user_id: int, who=Depends(require_admin)):
        """Replace a member's API key (for a lost key). The old key stops working; the new one is shown once."""
        not_an_admin_unless_owner(who, user_id)
        username, api_key = broker.admin_new_key(user_id)
        broker.log_admin(who["name"], "issued new API key", username)
        return {"username": username, "api_key": api_key, "note": "store this key, it is shown only once"}

    @app.delete("/api/admin/users/{user_id}")
    def admin_delete_user(user_id: int, who=Depends(require_admin)):
        """Delete an account and all of its portfolios, orders and history. Can't be undone."""
        not_an_admin_unless_owner(who, user_id)
        name = broker.delete_user(user_id)
        broker.log_admin(who["name"], "deleted account", name)
        return {"ok": True, "deleted": name}

    @app.put("/api/admin/users/{user_id}/admin")
    def admin_set_admin(user_id: int, body: AdminFlagIn, who=Depends(require_owner)):
        """Owner only: make a member an admin, or take it away."""
        name = broker.set_admin(user_id, body.is_admin)
        broker.log_admin(who["name"], "made admin" if body.is_admin else "removed admin", name)
        return {"ok": True, "username": name, "is_admin": body.is_admin}

    @app.get("/api/admin/me")
    def admin_me(who=Depends(require_admin)):
        return {"role": who["role"], "name": who["name"]}

    @app.get("/api/admin/log")
    def admin_log(limit: int = Query(default=100, ge=1, le=500), _=Depends(require_admin)):
        return broker.admin_log(limit)

    @app.post("/api/admin/users/{user_id}/reset")
    def admin_reset(user_id: int, who=Depends(require_admin)):
        broker.reset_user(user_id)
        broker.log_admin(who["name"], "reset main portfolio", f"user {user_id}")
        return {"ok": True}

    # ---------- dashboards ----------

    @app.get("/", include_in_schema=False)
    def user_panel():
        return FileResponse(STATIC / "index.html")

    @app.get("/activity", include_in_schema=False)
    def activity_page():
        return FileResponse(STATIC / "activity.html")

    @app.get("/competitions", include_in_schema=False)
    def competitions_page():
        return FileResponse(STATIC / "competitions.html")

    @app.get("/admin", include_in_schema=False)
    def admin_panel():
        return FileResponse(STATIC / "admin.html")

    @app.get("/admin/users", include_in_schema=False)
    def admin_users_page():
        return FileResponse(STATIC / "admin_users.html")

    @app.get("/admin/codes", include_in_schema=False)
    def admin_codes_page():
        return RedirectResponse("/admin/users#codes-top")

    @app.get("/admin/competitions", include_in_schema=False)
    def admin_competitions_page():
        return FileResponse(STATIC / "admin_competitions.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def get_app() -> FastAPI:
    """uvicorn entrypoint: uvicorn app.main:get_app --factory"""
    return create_app()
