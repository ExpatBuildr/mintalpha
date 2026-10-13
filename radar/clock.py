"""Time helpers. All persisted timestamps are UTC ISO-8601 strings with +00:00,
which sort correctly as text, so SQL comparisons on them are safe."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc).replace(microsecond=0)


class FakeClock(Clock):
    """Deterministic clock for tests and the demo."""

    def __init__(self, start: datetime):
        self._now = start.astimezone(timezone.utc).replace(microsecond=0)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> datetime:
        self._now += timedelta(**kwargs)
        return self._now

    def set(self, dt: datetime) -> None:
        self._now = dt.astimezone(timezone.utc)


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("naive datetime cannot be persisted")
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_dual(dt: datetime | None, display_tz: str) -> str:
    """'Oct 15, 2026 15:00 UTC (23:00 PHT)'."""
    if dt is None:
        return "unknown"
    utc = dt.astimezone(timezone.utc)
    local = dt.astimezone(ZoneInfo(display_tz))
    city = display_tz.split("/")[-1].replace("_", " ")
    return f"{utc:%b %d, %Y %H:%M} UTC ({local:%a %H:%M} {city} time)"


def humanize_delta(target: datetime, now: datetime) -> str:
    seconds = int((target - now).total_seconds())
    future = seconds >= 0
    seconds = abs(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if not days and minutes:
        parts.append(f"{minutes}m")
    if not parts:
        return "now"
    text = " ".join(parts)
    return f"in {text}" if future else f"{text} ago"
