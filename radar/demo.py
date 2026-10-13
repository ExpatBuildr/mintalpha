"""End-to-end demo with a simulated clock: no network, no API keys, no real messages.

Walks through discovery, duplicate handling, merging, a date change, pre-launch reminders,
live nags every 30 minutes, user actions from Telegram buttons, and a process restart."""
from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .app import RadarApp
from .clock import FakeClock
from .config import Env, load_config
from .db import Database
from .sources import MockXSource
from .telegram import MockTelegram


def _banner(text: str) -> None:
    print(f"\n\n=================== {text} ===================")


async def run_demo(echo: bool = True) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="radar-demo-"))
    config = load_config(load_env_file=False, environ={})
    config.env = Env()  # mock everything
    clock = FakeClock(datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc))
    db_path = tmp / "demo.db"
    tg = MockTelegram(echo=echo)
    source = MockXSource(config.root / "sample_data" / "mock_posts.json", clock)
    app = RadarApp(config, clock=clock, db=Database(db_path), source=source, telegram=tg)

    current = {"app": app}

    async def tick_for(minutes: int, step: int = 5) -> None:
        for _ in range(0, minutes, step):
            clock.advance(minutes=step)
            await current["app"].scheduler_once()

    _banner("Wave 1: five posts (1 irrelevant, 1 scam-looking)")
    print(await app.discovery_once())
    await tick_for(60)

    _banner("Wave 2: duplicate post, aggregator mention, date change, Sol Sprouts date")
    print(await app.discovery_once())

    _banner("Advance ~2 hours: Satoshi Shards 1h reminder + live + nags")
    await tick_for(150)

    shards = app.db.list_projects(q="Satoshi")[0]
    _banner(f"User taps ✅ Minted on Satoshi Shards (#{shards['id']})")
    await app.bot.handle_update({"callback_query": {"id": "cb1", "data": f"a:m:{shards['id']}",
                                                    "message": {"message_id": 1, "chat": {"id": app.chat_id}}}})
    scam = app.db.list_projects(q="claim")
    if scam:
        await app.bot.handle_update({"callback_query": {"id": "cb2", "data": f"a:d:{scam[0]['id']}",
                                                        "message": {"message_id": 2, "chat": {"id": app.chat_id}}}})

    _banner("Simulated restart (new process, same database)")
    app.db.close()
    app2 = RadarApp(config, clock=clock, db=Database(db_path), source=source, telegram=tg)
    current["app"] = app2
    await app2.scheduler_once()

    _banner("Advance to Pixel Pond launch (~28h): 24h + 1h reminders, then live")
    await tick_for(28 * 60 + 30, step=10)

    _banner("Wave 3: project posts 'mint is LIVE'")
    print(await app2.discovery_once())
    await tick_for(70)

    pond = app2.db.list_projects(q="Pixel")[0]
    _banner(f"User snoozes Pixel Pond 1h, then marks it missed")
    await app2.bot.handle_update({"callback_query": {"id": "cb3", "data": f"a:s1:{pond['id']}",
                                                     "message": {"message_id": 3, "chat": {"id": app2.chat_id}}}})
    await tick_for(90)
    await app2.bot.handle_update({"callback_query": {"id": "cb4", "data": f"a:x:{pond['id']}",
                                                     "message": {"message_id": 4, "chat": {"id": app2.chat_id}}}})
    await tick_for(120)

    _banner("/active and /status")
    await app2.bot.handle_update({"message": {"chat": {"id": app2.chat_id}, "text": "/active"}})
    await app2.bot.handle_update({"message": {"chat": {"id": app2.chat_id}, "text": "/status"}})

    summary = {
        "projects": len(app2.db.list_projects()),
        "messages_sent": len(tg.sent),
        "db": str(db_path),
    }
    _banner("Summary")
    for p in app2.db.list_projects():
        print(f"#{p['id']:<3} {p['name']:<28} {p['status']:<12} chain={p['chain']} conf={p['confidence']}")
    print(summary)
    app2.db.close()
    return summary


def main() -> None:
    asyncio.run(run_demo(echo=True))


if __name__ == "__main__":
    main()
