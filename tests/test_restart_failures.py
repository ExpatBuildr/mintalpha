import json

import httpx
import pytest

from radar.retry import FatalError, RetryableError
from radar.sources import BudgetExhausted, XApiSource, build_followup_queries, build_queries
from radar.telegram import MockTelegram, TelegramClient
from tests.conftest import T0, make_post


async def _nosleep(_):
    return None


async def test_restart_keeps_records_and_reminders(make_app, clock, tmp_path):
    db_path = tmp_path / "persist.db"
    tg = MockTelegram()
    app1 = make_app(db_path=db_path, telegram=tg)
    r = await app1.pipeline.ingest(make_post("Moon Toads free mint Oct 13 03:00 UTC", author="moontoads"))
    await app1.notifier.flush_outbox()
    pending_before = len(app1.db.pending_reminders_for(r.project_id))
    app1.db.close()  # process dies

    app2 = make_app(db_path=db_path, telegram=tg)
    assert app2.db.get_project(r.project_id)["name"] == "Moon Toads"
    assert len(app2.db.pending_reminders_for(r.project_id)) == pending_before == 2
    clock.advance(hours=2)
    await app2.scheduler_once()
    clock.advance(hours=1)
    await app2.scheduler_once()
    await app2.scheduler_once()
    texts = [m["text"] for m in tg.sent]
    assert sum("~1 hour" in t for t in texts) == 1 and sum("MINT IS LIVE" in t for t in texts) == 1


async def test_unsent_alert_survives_crash(make_app, tmp_path):
    db_path = tmp_path / "crash.db"
    app1 = make_app(db_path=db_path)
    await app1.pipeline.ingest(make_post("Moon Toads free mint on Base", author="moontoads"))
    assert len(app1.db.pending_alerts()) == 1
    app1.db.close()  # crashed before flushing
    tg = MockTelegram()
    app2 = make_app(db_path=db_path, telegram=tg)
    await app2.notifier.flush_outbox()
    await app2.notifier.flush_outbox()
    assert len(tg.sent) == 1


async def test_telegram_outage_retries_later(app):
    app.telegram.fail_next = 1
    await app.pipeline.ingest(make_post("Moon Toads free mint on Base", author="moontoads"))
    assert await app.notifier.flush_outbox() == 0
    assert app.db.pending_alerts()[0]["attempts"] == 1
    assert await app.notifier.flush_outbox() == 1


def _x_payload(n, start=1000):
    return {"data": [{"id": str(start + i), "text": f"Toad{i} free mint on Base", "author_id": "u1",
                      "created_at": "2026-10-13T00:00:00.000Z",
                      "entities": {"urls": [{"url": "https://t.co/x", "expanded_url": "https://toads.xyz/mint"}]}}
                     for i in range(n)],
            "includes": {"users": [{"id": "u1", "username": "toads", "name": "Toads",
                                    "public_metrics": {"followers_count": 900}}]},
            "meta": {"result_count": n}}


def _source(app, handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return XApiSource("token", app.db, app.settings, app.clock, http=http, sleep=_nosleep)


async def test_x_rate_limit_then_success(app):
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(429, headers={"x-rate-limit-reset": str(int(T0.timestamp()) + 30)})
        return httpx.Response(200, json=_x_payload(2))

    src = _source(app, handler)
    app.settings.config.data["discovery"]["search_phrases"] = ["free mint"]
    posts = await src.poll()
    assert len(posts) == 2 and len(calls) == 2
    assert calls[1].headers["Authorization"] == "Bearer token"
    assert posts[0][0].urls == ["https://toads.xyz/mint"]
    usage = app.db.usage("x_api", "2026-10-13")
    assert usage["reads"] == 2 and usage["errors"] == 1


async def test_x_since_id_and_lookback(app):
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        return httpx.Response(200, json=_x_payload(1, start=5000 + len(seen)))

    src = _source(app, handler)
    app.settings.config.data["discovery"]["search_phrases"] = ["free mint"]
    await src.poll()
    await src.poll()
    assert "start_time" in seen[0] and "since_id" not in seen[0]
    assert seen[1]["since_id"] == "5001"


async def test_x_auth_error_is_fatal(app):
    src = _source(app, lambda req: httpx.Response(401, json={"title": "Unauthorized"}))
    with pytest.raises(FatalError):
        await src.poll()


async def test_x_server_errors_retry_then_give_up(app):
    count = {"n": 0}

    def handler(req):
        count["n"] += 1
        return httpx.Response(503)

    src = _source(app, handler)
    app.settings.config.data["discovery"]["search_phrases"] = ["free mint"]
    with pytest.raises(RetryableError):
        await src.poll()
    assert count["n"] == 4


async def test_budget_blocks_requests(app):
    count = {"n": 0}

    def handler(req):
        count["n"] += 1
        assert req.url.params["max_results"] == "10"
        return httpx.Response(200, json=_x_payload(10))

    app.settings.set("discovery.daily_read_budget", "15")
    app.settings.config.data["discovery"]["search_phrases"] = ["free mint"]
    src = _source(app, handler)
    await src.poll()
    with pytest.raises(BudgetExhausted):
        await src.poll()
    assert count["n"] == 1


async def test_budget_exhaustion_does_not_crash_app(app):
    class Broke:
        name = "x_api"
        async def poll(self):
            raise BudgetExhausted("out")
    app.source = Broke()
    assert await app.discovery_once() == {"budget_exhausted": 1}


def test_query_builders_respect_length():
    phrases = [f"free mint phrase number {i}" for i in range(40)]
    qs = build_queries(phrases, "-is:retweet lang:en", 512)
    assert len(qs) > 1 and all(len(q) <= 512 for q in qs)
    fq = build_followup_queries([f"handle{i}" for i in range(80)] + ["bad handle!"], 512)
    assert all(len(q) <= 512 for q in fq) and "bad" not in " ".join(fq)


async def test_telegram_client_handles_429_and_html_fallback():
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 1}})
        if len(calls) == 2:
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: can't parse entities"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    client = TelegramClient("t", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), sleep=_nosleep)
    mid = await client.send_message("1", "<b>Hi</b> & bye", [[{"text": "x", "callback_data": "y"}]])
    assert mid == 42 and "parse_mode" not in calls[-1] and calls[-1]["text"] == "Hi & bye"


async def test_telegram_bad_token_is_fatal():
    client = TelegramClient("t", http=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(401, json={"ok": False, "description": "Unauthorized"}))), sleep=_nosleep)
    with pytest.raises(FatalError):
        await client.send_message("1", "hi")


async def test_budget_is_paced_across_the_day(app, clock):
    src = _source(app, lambda req: httpx.Response(200, json=_x_payload(10)))
    app.settings.config.data["discovery"]["search_phrases"] = ["free mint"]
    # at 00:00 UTC only the burst (20) is available
    assert src.remaining_budget() == 20
    clock.advance(hours=12)
    assert src.remaining_budget() == 170  # half of 300 + burst
