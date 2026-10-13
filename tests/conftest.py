import itertools
from datetime import datetime, timezone

import pytest

from radar.app import RadarApp
from radar.clock import FakeClock
from radar.config import Env, load_config
from radar.db import Database
from radar.models import Post
from radar.telegram import MockTelegram

T0 = datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc)
_ids = itertools.count(1)


class ListSource:
    """Test source: returns whatever posts are queued for the next poll."""
    name = "mock"

    def __init__(self):
        self.queue = []

    def push(self, *posts):
        self.queue.append([(p, {}) for p in posts])

    async def poll(self):
        return self.queue.pop(0) if self.queue else []

    async def poll_followups(self, handles, since):
        return []


def make_post(text, author="proj_acct", *, created=None, urls=None, followers=5000, pid=None, name=""):
    return Post(id=pid or str(next(_ids)), text=text, author_username=author, author_name=name,
                created_at=created or T0, urls=urls or [], author_followers=followers, source="mock")


@pytest.fixture
def config():
    cfg = load_config(load_env_file=False, environ={})
    cfg.env = Env()
    cfg.data["verification"]["fetch_official_sites"] = False
    return cfg


@pytest.fixture
def clock():
    return FakeClock(T0)


@pytest.fixture
def make_app(config, clock, tmp_path):
    def _make(db_path=None, telegram=None, source=None):
        db = Database(db_path or tmp_path / "radar.db")
        return RadarApp(config, clock=clock, db=db, source=source or ListSource(),
                        telegram=telegram or MockTelegram())
    return _make


@pytest.fixture
def app(make_app):
    return make_app()
