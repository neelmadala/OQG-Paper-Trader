from datetime import datetime, time as dtime, timedelta

try:
    from zoneinfo import ZoneInfo

    _NY = ZoneInfo("America/New_York")
except Exception:  # tzdata missing: behave as "always open" rather than never snapshotting
    _NY = None

OPEN = dtime(9, 30)
CLOSE = dtime(16, 0)


def is_us_market_open(now: datetime | None = None, grace_minutes: int = 5) -> bool:
    """Regular US equity session, Mon-Fri 9:30-16:00 ET, plus a few minutes after the
    close so the closing equity gets captured. Does not know about holidays."""
    if _NY is None:
        return True
    now = now or datetime.now(_NY)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    ny = now.astimezone(_NY)
    if ny.weekday() >= 5:
        return False
    close = (datetime.combine(ny.date(), CLOSE, tzinfo=_NY) + timedelta(minutes=grace_minutes)).time()
    return OPEN <= ny.time() <= close
