"""Post sources: the official X API v2 recent-search endpoint, and a mock source for demos/tests.

Cost control is built in for X: every request is checked against a daily and monthly post-read
budget stored in the DB, max_results is clamped to what the budget allows, queries use since_id so
already-seen posts are not re-read, and a new query never backfills more than
`initial_lookback_minutes`. The counter is our estimate; X's developer console is the source of truth.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .clock import Clock, from_iso, to_iso
from .config import Settings
from .db import Database
from .models import Post
from .retry import FatalError, RetryableError, with_retries

log = logging.getLogger("radar.sources")
X_SEARCH_URL = "https://api.x.com/2/tweets/search/recent"


class BudgetExhausted(Exception):
    pass


def build_queries(phrases: list[str], suffix: str, max_len: int) -> list[str]:
    """Pack OR-ed quoted phrases into as few queries as fit the length limit."""
    queries, current = [], []
    suffix = f" {suffix.strip()}" if suffix.strip() else ""
    for phrase in phrases:
        term = f'"{phrase}"' if " " in phrase else phrase
        candidate = current + [term]
        text = "(" + " OR ".join(candidate) + ")" + suffix
        if len(text) > max_len and current:
            queries.append("(" + " OR ".join(current) + ")" + suffix)
            current = [term]
        else:
            current = candidate
    if current:
        queries.append("(" + " OR ".join(current) + ")" + suffix)
    return queries


def build_followup_queries(handles: list[str], max_len: int) -> list[str]:
    queries, current = [], []
    for h in sorted({h.lower() for h in handles if re.fullmatch(r"[A-Za-z0-9_]{1,15}", h or "")}):
        candidate = current + [f"from:{h}"]
        if len(" OR ".join(candidate)) + 14 > max_len and current:
            queries.append("(" + " OR ".join(current) + ") -is:retweet")
            current = [f"from:{h}"]
        else:
            current = candidate
    if current:
        queries.append("(" + " OR ".join(current) + ") -is:retweet")
    return queries


def parse_x_response(data: dict) -> list[tuple[Post, dict]]:
    users = {u["id"]: u for u in (data.get("includes") or {}).get("users", [])}
    out = []
    for t in data.get("data") or []:
        u = users.get(t.get("author_id"), {})
        urls = [x.get("unwound_url") or x.get("expanded_url") or x.get("url")
                for x in ((t.get("entities") or {}).get("urls") or [])]
        metrics = u.get("public_metrics") or {}
        out.append((Post(
            id=str(t["id"]), text=t.get("note_tweet", {}).get("text") or t.get("text", ""),
            author_username=u.get("username") or f"user{t.get('author_id')}",
            author_name=u.get("name", ""), author_id=t.get("author_id"),
            created_at=from_iso(t.get("created_at")) or datetime.now(timezone.utc),
            urls=[x for x in urls if x],
            author_followers=metrics.get("followers_count"),
            author_created_at=from_iso(u.get("created_at")), author_verified=u.get("verified"),
            source="x_api"), t))
    return out


class XApiSource:
    name = "x_api"

    def __init__(self, bearer_token: str, db: Database, settings: Settings, clock: Clock,
                 http: httpx.AsyncClient | None = None, sleep=None):
        self.token = bearer_token
        self.db = db
        self.settings = settings
        self.clock = clock
        self.http = http or httpx.AsyncClient(timeout=30)
        self.sleep = sleep

    # -- budget -----------------------------------------------------------------
    def remaining_budget(self) -> int:
        now = self.clock.now()
        day = now.strftime("%Y-%m-%d")
        reads_today = self.db.usage(self.name, day)["reads"]
        reads_month = self.db.usage(self.name, now.strftime("%Y-%m"))["reads"]
        daily = int(self.settings.get("discovery.daily_read_budget", 300))
        monthly = int(self.settings.get("discovery.monthly_read_budget", 9000))
        allowance = daily
        if self.settings.get("discovery.pace_budget", True):
            # Spread the daily budget across the UTC day so it isn't spent by breakfast.
            elapsed = (now - now.replace(hour=0, minute=0, second=0)).total_seconds()
            allowance = min(daily, int(daily * elapsed / 86400) + int(self.settings.get("discovery.pace_burst", 20)))
        return max(0, min(allowance - reads_today, monthly - reads_month))

    async def _search(self, query: str, *, since_id: str | None, start_time: datetime | None) -> list[tuple[Post, dict]]:
        remaining = self.remaining_budget()
        if remaining < 10:  # X requires max_results >= 10
            raise BudgetExhausted(f"X read budget exhausted (remaining {remaining})")
        max_results = max(10, min(100, int(self.settings.get("discovery.max_results_per_query", 25)), remaining))
        params = {
            "query": query, "max_results": str(max_results),
            "tweet.fields": "created_at,author_id,entities,lang,note_tweet",
            "expansions": "author_id", "user.fields": "username,name,created_at,public_metrics,verified",
        }
        if since_id:
            params["since_id"] = since_id
        elif start_time:
            params["start_time"] = start_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        day = self.clock.now().strftime("%Y-%m-%d")

        async def do():
            resp = await self.http.get(X_SEARCH_URL, params=params, headers={"Authorization": f"Bearer {self.token}"})
            self.db.record_usage(self.name, day, requests=1)
            if resp.status_code == 429:
                reset = resp.headers.get("x-rate-limit-reset")
                wait = max(1.0, float(reset) - self.clock.now().timestamp()) if reset else 60.0
                self.db.record_usage(self.name, day, errors=1)
                raise RetryableError("X API rate limited", retry_after=min(wait, 900))
            if resp.status_code >= 500:
                self.db.record_usage(self.name, day, errors=1)
                raise RetryableError(f"X API HTTP {resp.status_code}")
            if resp.status_code in (401, 403):
                self.db.record_usage(self.name, day, errors=1)
                raise FatalError(f"X API HTTP {resp.status_code}: credentials invalid, no API access on this app, "
                                 f"or no credits — {resp.text[:200]}")
            if resp.status_code == 402:
                raise FatalError("X API HTTP 402: payment required — add credits in the X developer console")
            if resp.status_code >= 400:
                raise FatalError(f"X API HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json()

        kwargs = {"attempts": 4, "op": "x.search", "max_delay": 900}
        if self.sleep:
            kwargs["sleep"] = self.sleep
        data = await with_retries(do, **kwargs)
        posts = parse_x_response(data)
        self.db.record_usage(self.name, day, reads=len(posts))
        return posts

    async def poll(self) -> list[tuple[Post, dict]]:
        now = self.clock.now()
        queries = build_queries(self.settings.get("discovery.search_phrases", []),
                                self.settings.get("discovery.query_suffix", ""),
                                int(self.settings.get("discovery.query_max_length", 512)))
        lookback = timedelta(minutes=int(self.settings.get("discovery.initial_lookback_minutes", 30)))
        results: list[tuple[Post, dict]] = []
        for q in queries:
            state = self.db.get_query_state(q)
            since_id = state["since_id"] if state else None
            posts = await self._search(q, since_id=since_id, start_time=None if since_id else now - lookback)
            newest = max((p.id for p, _ in posts), key=int, default=None)
            self.db.set_query_state(q, newest, now)
            results.extend(posts)
        return results

    async def poll_followups(self, handles: list[str], since: datetime) -> list[tuple[Post, dict]]:
        results = []
        for q in build_followup_queries(handles, int(self.settings.get("discovery.query_max_length", 512))):
            state = self.db.get_query_state(q)
            since_id = state["since_id"] if state else None
            posts = await self._search(q, since_id=since_id, start_time=None if since_id else since)
            newest = max((p.id for p, _ in posts), key=int, default=None)
            self.db.set_query_state(q, newest, self.clock.now())
            results.extend(posts)
        return results

    async def check_access(self) -> str:
        """One minimal request (≈10 reads at most) to confirm credentials and access."""
        posts = await self._search('"free mint" -is:retweet', since_id=None,
                                   start_time=self.clock.now() - timedelta(minutes=15))
        return f"X API OK — {len(posts)} posts returned"


_TEMPLATE = re.compile(r"\{t([+-]\d+)([dhm])\|([^}]+)\}")


def render_template(text: str, now: datetime) -> str:
    """'{t+26h|%B %d %H:%M}' -> time relative to now (UTC), so demo dates are always in the future."""
    def sub(m: re.Match) -> str:
        n, unit, fmt = int(m.group(1)), m.group(2), m.group(3)
        delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]
        return (now + delta).strftime(fmt)
    return _TEMPLATE.sub(sub, text)


class MockXSource:
    """Replays sample_data/mock_posts.json: each poll returns the next 'wave'."""
    name = "mock"

    def __init__(self, path: str | Path, clock: Clock):
        self.clock = clock
        self.data = json.loads(Path(path).read_text())
        self.waves = self.data["waves"]
        self.index = 0
        self.anchor: datetime | None = None

    def _to_post(self, item: dict) -> tuple[Post, dict]:
        anchor = self.anchor or self.clock.now()
        created = self.clock.now() + timedelta(minutes=int(item.get("created_offset_minutes", 0)))
        return Post(
            id=str(item["id"]), text=render_template(item["text"], anchor), author_username=item["author"],
            author_name=item.get("author_name", ""), created_at=created,
            urls=[render_template(u, anchor) for u in item.get("urls", [])],
            author_followers=item.get("followers"), source="mock"), item

    async def poll(self) -> list[tuple[Post, dict]]:
        if self.anchor is None:
            self.anchor = self.clock.now()
        if self.index >= len(self.waves):
            return []
        wave = self.waves[self.index]
        self.index += 1
        return [self._to_post(item) for item in wave]

    async def poll_followups(self, handles, since) -> list[tuple[Post, dict]]:
        return []  # follow-up posts are part of the waves in mock mode

    async def check_access(self) -> str:
        return f"mock source with {len(self.waves)} waves"
