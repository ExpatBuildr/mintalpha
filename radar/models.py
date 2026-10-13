"""Domain types: statuses, transitions, posts, extraction results."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Status(str, Enum):
    DISCOVERED = "discovered"      # just created; immediately re-derived from evidence
    UPCOMING = "upcoming"          # launch date known (exact time or day)
    DATE_UNKNOWN = "date_unknown"  # tracked, no usable date yet
    LIVE = "live"                  # mint reported live or exact launch time reached
    ENDED = "ended"                # minted out / closed / auto-ended
    MINTED = "minted"              # you minted (resolved)
    MISSED = "missed"              # you missed it (resolved)
    DISMISSED = "dismissed"        # not interested / scam (resolved)


RESOLVED = {Status.MINTED, Status.MISSED, Status.DISMISSED}
SYSTEM_TERMINAL = {Status.ENDED}
OPEN = {Status.DISCOVERED, Status.UPCOMING, Status.DATE_UNKNOWN, Status.LIVE}

_ALLOWED: dict[Status, set[Status]] = {
    Status.DISCOVERED: {Status.UPCOMING, Status.DATE_UNKNOWN, Status.LIVE, Status.ENDED} | RESOLVED,
    Status.UPCOMING: {Status.DATE_UNKNOWN, Status.LIVE, Status.ENDED} | RESOLVED,
    Status.DATE_UNKNOWN: {Status.UPCOMING, Status.LIVE, Status.ENDED} | RESOLVED,
    Status.LIVE: {Status.ENDED, Status.UPCOMING} | RESOLVED,   # UPCOMING: "live" was premature, date moved
    Status.ENDED: RESOLVED | {Status.LIVE},                    # LIVE: reopened phase / extended
    # Resolved states only change via an explicit reopen (back to date_unknown, then re-derived)
    Status.MINTED: {Status.DATE_UNKNOWN},
    Status.MISSED: {Status.DATE_UNKNOWN},
    Status.DISMISSED: {Status.DATE_UNKNOWN},
}


class InvalidTransition(ValueError):
    pass


def check_transition(old: Status, new: Status) -> None:
    if old == new:
        return
    if new not in _ALLOWED[old]:
        raise InvalidTransition(f"cannot move from {old.value} to {new.value}")


class PriceStatus(str, Enum):
    UNKNOWN = "unknown"
    CLAIMED_FREE = "claimed_free"                # third party says free
    PROJECT_CLAIMED_FREE = "project_claimed_free"  # project's own account says free
    CONFIRMED_FREE = "confirmed_free"            # official site/mint page shows free
    PAID = "paid"                                # a non-zero price was stated


PRICE_RANK = {
    PriceStatus.UNKNOWN: 0,
    PriceStatus.CLAIMED_FREE: 1,
    PriceStatus.PROJECT_CLAIMED_FREE: 2,
    PriceStatus.CONFIRMED_FREE: 3,
    PriceStatus.PAID: 2,  # a project/site-stated price outranks third-party "free" claims
}

PRICE_LABEL = {
    PriceStatus.UNKNOWN: "unknown",
    PriceStatus.CLAIMED_FREE: "Free (third-party claim, unverified)",
    PriceStatus.PROJECT_CLAIMED_FREE: "Free (stated by the project's own account)",
    PriceStatus.CONFIRMED_FREE: "Free (shown on official site/mint page)",
    PriceStatus.PAID: "NOT free (a price was stated)",
}


@dataclass
class Post:
    id: str
    text: str
    author_username: str
    created_at: datetime
    author_name: str = ""
    author_id: str | None = None
    urls: list[str] = field(default_factory=list)
    author_followers: int | None = None
    author_created_at: datetime | None = None
    author_verified: bool | None = None
    source: str = "x_api"

    @property
    def url(self) -> str:
        return f"https://x.com/{self.author_username}/status/{self.id}"


@dataclass
class Schedule:
    launch_at: datetime | None = None       # only set when exact (date + time + zone)
    launch_date: str | None = None           # YYYY-MM-DD when at least the day is known
    precision: str = "unknown"               # exact | day | unknown
    tz_name: str | None = None
    raw: str | None = None
    assumptions: list[str] = field(default_factory=list)
    live_signal: bool = False
    ended_signal: bool = False
    tba: bool = False


@dataclass
class Extraction:
    relevant: bool
    name: str | None = None
    name_source: str | None = None
    project_handle: str | None = None
    author_is_project: bool = False
    chain: str | None = None
    chain_candidates: list[str] = field(default_factory=list)
    price_text: str | None = None
    reported_free: bool = False
    paid_price: str | None = None
    schedule: Schedule = field(default_factory=Schedule)
    supply: int | None = None
    eligibility: list[str] = field(default_factory=list)
    website_url: str | None = None
    mint_url: str | None = None
    mint_platform: str | None = None
    other_links: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)
