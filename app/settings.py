import os
from dataclasses import dataclass


@dataclass
class Settings:
    db_path: str = "paper_trader.db"
    starting_cash: float = 100_000.0
    admin_token: str = ""  # empty = admin endpoints disabled
    invite_code: str = ""  # empty = self-registration closed (admin creates users)
    price_provider: str = "yfinance"  # "yfinance" or "mock"
    quote_ttl: float = 15.0  # seconds a quote is shared between all users
    slippage_bps: float = 0.0  # applied against you on every fill
    commission: float = 0.0  # flat fee per order
    snapshot_seconds: int = 60  # equity snapshot interval, 0 disables
    snapshot_market_hours_only: bool = True  # skip timed snapshots outside US market hours
    allow_shorting: bool = True  # enables the short and cover order sides
    short_margin: float = 0.5  # extra cash held on top of short proceeds, as a fraction of short value
    short_maint_margin: float = 0.3  # margin-call level (same units; capped at short_margin)
    auto_liquidate: bool = False  # force-cover shorts below the maintenance level (needs snapshot_seconds > 0)
    allow_options: bool = True  # long-only option contracts (buy to open, sell to close)
    max_open_limits: int = 50  # resting limit orders allowed per account
    limit_expiry_days: float = 30.0  # resting limit orders are cancelled after this long (0 = never)
    max_portfolios: int = 10  # extra personal portfolios a member may create (competition ones don't count)
    actions_check_seconds: int = 3600  # how often to look for splits and dividends (0 = never; admin can still enter them)

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(
            db_path=e("PT_DB_PATH", "paper_trader.db"),
            starting_cash=float(e("PT_STARTING_CASH", "100000")),
            admin_token=e("PT_ADMIN_TOKEN", ""),
            invite_code=e("PT_INVITE_CODE", ""),
            price_provider=e("PT_PRICE_PROVIDER", "yfinance"),
            quote_ttl=float(e("PT_QUOTE_TTL_SECONDS", "15")),
            slippage_bps=float(e("PT_SLIPPAGE_BPS", "0")),
            commission=float(e("PT_COMMISSION_PER_ORDER", "0")),
            snapshot_seconds=int(e("PT_SNAPSHOT_SECONDS", "60")),
            snapshot_market_hours_only=e("PT_SNAPSHOT_MARKET_HOURS_ONLY", "1").lower() in ("1", "true", "yes"),
            allow_shorting=e("PT_ALLOW_SHORTING", "1").lower() in ("1", "true", "yes"),
            short_margin=float(e("PT_SHORT_MARGIN", "0.5")),
            short_maint_margin=float(e("PT_SHORT_MAINT_MARGIN", "0.3")),
            auto_liquidate=e("PT_AUTO_LIQUIDATE", "0").lower() in ("1", "true", "yes"),
            allow_options=e("PT_ALLOW_OPTIONS", "1").lower() in ("1", "true", "yes"),
            max_open_limits=int(e("PT_MAX_OPEN_LIMITS", "50")),
            limit_expiry_days=float(e("PT_LIMIT_EXPIRY_DAYS", "30")),
            max_portfolios=int(e("PT_MAX_PORTFOLIOS", "10")),
            actions_check_seconds=int(e("PT_ACTIONS_CHECK_SECONDS", "3600")),
        )
