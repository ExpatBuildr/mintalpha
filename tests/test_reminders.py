from datetime import timedelta

from radar.bot import apply_action
from tests.conftest import T0, make_post


def kinds(app):
    return [m["text"].split(":")[0] for m in app.telegram.sent]


async def run_minutes(app, clock, minutes, step=5):
    for _ in range(0, minutes, step):
        clock.advance(minutes=step)
        await app.scheduler_once()


async def test_pre_launch_live_and_repeat_schedule(app, clock):
    # launch exactly 48h after T0
    r = await app.pipeline.ingest(make_post("Moon Toads free mint Oct 15 00:00 UTC on Base", author="moontoads"))
    await app.notifier.flush_outbox()
    rows = {x["kind"]: x["due_at"] for x in app.db.reminders_for(r.project_id)}
    assert rows["pre_1440"].startswith("2026-10-14T00:00") and rows["pre_60"].startswith("2026-10-14T23:00")
    assert rows["live"].startswith("2026-10-15T00:00")

    await run_minutes(app, clock, 48 * 60 + 65, step=5)
    texts = [m["text"] for m in app.telegram.sent]
    assert sum("Mint in ~24 hours" in t for t in texts) == 1
    assert sum("Mint in ~1 hour" in t for t in texts) == 1
    assert sum("MINT IS LIVE" in t for t in texts) == 1
    assert sum("Still live" in t for t in texts) == 2  # at +30m and +60m


async def test_resolving_stops_reminders(app, clock):
    r = await app.pipeline.ingest(make_post("Moon Toads free mint is live now", author="moontoads"))
    await run_minutes(app, clock, 65)
    before = len(app.telegram.sent)
    apply_action(app.db, app.engine, clock, r.project_id, "minted")
    await run_minutes(app, clock, 180)
    assert len(app.telegram.sent) == before
    assert all(x["sent_at"] or x["cancelled_at"] for x in app.db.reminders_for(r.project_id))


async def test_snooze_pauses_then_resumes(app, clock):
    r = await app.pipeline.ingest(make_post("Moon Toads free mint is live now", author="moontoads"))
    await app.scheduler_once()
    apply_action(app.db, app.engine, clock, r.project_id, "snooze", 2)
    n = len(app.telegram.sent)
    await run_minutes(app, clock, 115)
    assert len(app.telegram.sent) == n
    await run_minutes(app, clock, 10)
    assert len(app.telegram.sent) == n + 1  # exactly one nag right after snooze ends


async def test_no_timed_reminders_without_exact_time(app, clock):
    r = await app.pipeline.ingest(make_post("Moon Toads free mint Oct 20 at 3pm", author="moontoads"))
    await app.scheduler_once()
    assert app.db.reminders_for(r.project_id) == []
    assert app.db.get_project(r.project_id)["launch_precision"] == "day"


async def test_date_change_reschedules(app, clock):
    r = await app.pipeline.ingest(make_post("Moon Toads free mint Oct 15 00:00 UTC", author="moontoads"))
    await app.pipeline.ingest(make_post("Moon Toads free mint moved to Oct 16 00:00 UTC", author="moontoads"))
    pending = app.db.pending_reminders_for(r.project_id)
    assert {x["kind"] for x in pending} == {"pre_1440", "pre_60", "live"}
    assert all("2026-10-15T00:00:00" not in x["dedupe_key"] for x in pending)
    live = [x for x in pending if x["kind"] == "live"][0]
    assert live["due_at"].startswith("2026-10-16T00:00")
    cancelled = [x for x in app.db.reminders_for(r.project_id) if x["cancelled_at"]]
    assert len(cancelled) == 2


async def test_repeated_ticks_do_not_duplicate(app, clock):
    r = await app.pipeline.ingest(make_post("Moon Toads free mint in 2 hours", author="moontoads", created=T0))
    for _ in range(20):
        await app.scheduler_once()  # same instant
    assert len(app.db.reminders_for(r.project_id)) == 2  # pre_60 + live (pre_1440 already past)
    # second identical announcement does not add reminders
    await app.pipeline.ingest(make_post("Moon Toads free mint in 2 hours", author="moontoads", created=T0))
    assert len(app.db.reminders_for(r.project_id)) == 2


async def test_stale_one_shot_cancelled_after_downtime(app, clock):
    r = await app.pipeline.ingest(make_post("Moon Toads free mint Oct 13 05:00 UTC", author="moontoads"))
    await app.notifier.flush_outbox()
    n = len(app.telegram.sent)
    clock.advance(hours=4, minutes=30)  # "process was down" past the 1h reminder window
    await app.scheduler_once()
    new = [m["text"] for m in app.telegram.sent[n:]]
    assert not any("~1 hour" in t for t in new)
    clock.advance(minutes=31)
    await app.scheduler_once()
    assert any("MINT IS LIVE" in m["text"] for m in app.telegram.sent[n:])


async def test_digest_for_unknown_dates(app, clock):
    await app.pipeline.ingest(make_post("Glimmer Kids free mint soon #GlimmerKids", author="glimmer"))
    await app.scheduler_once()  # first tick sets the digest anchor
    clock.advance(hours=12)
    await app.scheduler_once()
    assert sum("without an exact time" in m["text"] for m in app.telegram.sent) == 1
    await app.scheduler_once()
    assert sum("without an exact time" in m["text"] for m in app.telegram.sent) == 1


async def test_high_risk_live_gets_one_alert_no_nags(app, clock):
    await app.pipeline.ingest(make_post("Free NFT claim is live! Enter seed phrase to claim https://0pensea.io/c",
                                        author="scammer", followers=3))
    await run_minutes(app, clock, 120)
    assert sum("MINT IS LIVE" in m["text"] for m in app.telegram.sent) == 1
    assert not any("Still live" in m["text"] for m in app.telegram.sent)


async def test_interval_configurable(app, clock):
    app.settings.set("reminders.live_repeat_minutes", "10")
    await app.pipeline.ingest(make_post("Moon Toads free mint is live now", author="moontoads"))
    await run_minutes(app, clock, 60, step=5)
    assert sum("Still live" in m["text"] for m in app.telegram.sent) == 5  # 15,25,35,45,55 min
