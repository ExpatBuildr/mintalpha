import pytest

from radar.models import InvalidTransition, Status, check_transition
from tests.conftest import make_post


def cb(app, data, mid=1, chat=None):
    return {"callback_query": {"id": "cb", "data": data,
                               "message": {"message_id": mid, "chat": {"id": chat or app.chat_id}}}}


def msg(app, text, chat=None):
    return {"message": {"chat": {"id": chat or app.chat_id}, "text": text}}


async def test_minted_button_updates_db_and_removes_buttons(app):
    r = await app.pipeline.ingest(make_post("Moon Toads free mint is live now", author="moontoads"))
    await app.bot.handle_update(cb(app, f"a:m:{r.project_id}", mid=77))
    assert app.db.get_project(r.project_id)["status"] == "minted"
    assert (77, None) in app.telegram.edits
    assert "minted" in app.telegram.answers[-1][1]


async def test_snooze_menu_flow(app):
    r = await app.pipeline.ingest(make_post("Moon Toads free mint is live now", author="moontoads"))
    await app.bot.handle_update(cb(app, f"a:s:{r.project_id}"))
    assert app.telegram.edits[-1][1][0][0]["callback_data"] == f"a:s1:{r.project_id}"
    await app.bot.handle_update(cb(app, f"a:s4:{r.project_id}"))
    assert app.db.get_project(r.project_id)["snoozed_until"].startswith("2026-10-13T04:00")


async def test_dismiss_missed_details(app):
    r = await app.pipeline.ingest(make_post("Moon Toads free mint Oct 20 15:00 UTC", author="moontoads"))
    await app.bot.handle_update(cb(app, f"a:v:{r.project_id}"))
    assert "Evidence" in app.telegram.sent[-1]["text"]
    await app.bot.handle_update(cb(app, f"a:x:{r.project_id}"))
    assert app.db.get_project(r.project_id)["status"] == "missed"
    await app.bot.handle_update(msg(app, f"/reopen {r.project_id}"))
    assert app.db.get_project(r.project_id)["status"] == "upcoming"
    await app.bot.handle_update(msg(app, f"/dismiss {r.project_id}"))
    assert app.db.get_project(r.project_id)["status"] == "dismissed"


async def test_unauthorized_chat_is_ignored(app):
    r = await app.pipeline.ingest(make_post("Moon Toads free mint is live now", author="moontoads"))
    await app.bot.handle_update(cb(app, f"a:m:{r.project_id}", chat="999"))
    assert app.db.get_project(r.project_id)["status"] == "live"
    await app.bot.handle_update(msg(app, "/start", chat="999"))
    assert app.telegram.sent[-1]["chat_id"] == "999" and "chat id is" in app.telegram.sent[-1]["text"]
    await app.bot.handle_update(msg(app, "/active", chat="999"))
    assert "chat id" in app.telegram.sent[-1]["text"]  # nothing new sent for non-start commands


async def test_list_commands(app):
    await app.pipeline.ingest(make_post("Moon Toads free mint is live now", author="moontoads"))
    await app.pipeline.ingest(make_post("Glow Frogs free mint Oct 20 15:00 UTC", author="glowfrogs"))
    for cmd in ("/start", "/help", "/active", "/upcoming", "/live", "/status", "/settings"):
        await app.bot.handle_update(msg(app, cmd))
    texts = [m["text"] for m in app.telegram.sent[-7:]]
    assert "Free Mint Radar" in texts[0]
    assert "Moon Toads" in texts[2] and "Glow Frogs" in texts[2]
    assert "Glow Frogs" in texts[3] and "Moon Toads" not in texts[3]
    assert "Moon Toads" in texts[4] and "Glow Frogs" not in texts[4]
    assert "X reads today" in texts[5]
    assert "live_repeat_minutes" in texts[6]


async def test_set_command_validates(app):
    await app.bot.handle_update(msg(app, "/set reminders.live_repeat_minutes 15"))
    assert app.settings.get("reminders.live_repeat_minutes") == 15
    await app.bot.handle_update(msg(app, "/set reminders.live_repeat_minutes 1"))
    assert "must be &gt;=" in app.telegram.sent[-1]["text"]
    await app.bot.handle_update(msg(app, "/set network.polygon off"))
    assert not app.settings.network_enabled("polygon")
    await app.bot.handle_update(msg(app, "/set app.db_path x"))
    assert "not a runtime setting" in app.telegram.sent[-1]["text"]
    await app.bot.handle_update(msg(app, "/pause"))
    assert app.settings.get("discovery.paused") is True
    assert (await app.discovery_once()) == {"paused": 1}


async def test_invalid_transition_reported_not_crashing(app):
    r = await app.pipeline.ingest(make_post("Moon Toads free mint is live now", author="moontoads"))
    await app.bot.handle_update(msg(app, f"/minted {r.project_id}"))
    await app.bot.handle_update(msg(app, f"/missed {r.project_id}"))
    assert "cannot move" in app.telegram.sent[-1]["text"]
    assert app.db.get_project(r.project_id)["status"] == "minted"


def test_transition_rules():
    check_transition(Status.UPCOMING, Status.LIVE)
    check_transition(Status.LIVE, Status.MINTED)
    check_transition(Status.MINTED, Status.DATE_UNKNOWN)  # reopen
    with pytest.raises(InvalidTransition):
        check_transition(Status.MINTED, Status.LIVE)
    with pytest.raises(InvalidTransition):
        check_transition(Status.DISMISSED, Status.MISSED)
