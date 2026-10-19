"""Shared Shanghai completed-daily-bar cutoff; no exchange-calendar inference."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def resolve_as_of(value: str | None, now: datetime | None = None) -> str:
    """Latest closed calendar date, not a guessed exchange-session date."""
    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        raise ValueError("current time must be timezone-aware")
    current = current.astimezone(SHANGHAI)
    latest = current.date()
    if current.time() < time(15):
        latest -= timedelta(days=1)
    if value is None:
        return latest.isoformat()
    requested = date.fromisoformat(value)
    if requested.isoformat() != value or requested > latest:
        raise ValueError("as-of must be YYYY-MM-DD and no later than the latest Shanghai close")
    return value
