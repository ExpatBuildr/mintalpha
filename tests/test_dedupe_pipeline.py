from datetime import timedelta

from tests.conftest import T0, make_post


async def test_one_post_one_project_one_alert(app):
    p = make_post("Moon Toads free mint on Base Oct 20 15:00 UTC https://moontoads.xyz/mint",
                  author="moontoads", pid="100")
    r1 = await app.pipeline.ingest(p)
    r2 = await app.pipeline.ingest(p)
    assert r1.outcome == "created" and r2.outcome == "duplicate"
    assert len(app.db.list_projects()) == 1
    await app.notifier.flush_outbox()
    assert len(app.telegram.sent) == 1
    assert "Moon Toads" in app.telegram.sent[0]["text"]


async def test_reposts_from_other_accounts_merge(app):
    await app.pipeline.ingest(make_post("Moon Toads free mint on Base", author="moontoads"))
    await app.pipeline.ingest(make_post("@moontoads Moon Toads free mint tomorrow!", author="caller1"))
    await app.pipeline.ingest(make_post("\"Moon Toads\" free mint on Base, wl open", author="caller2"))
    await app.pipeline.ingest(make_post("moontoads.xyz free mint https://moontoads.xyz", author="moontoads",
                                        urls=["https://moontoads.xyz"]))
    assert len(app.db.list_projects()) == 1
    p = app.db.list_projects()[0]
    assert p["website_url"] == "https://moontoads.xyz"
    assert len(app.db.posts_for_project(p["id"])) == 4


async def test_incomplete_project_is_still_tracked_and_alerted(app):
    res = await app.pipeline.ingest(make_post("free mint soon 👀 #GlimmerKids", author="someone", followers=50))
    assert res.outcome == "created"
    p = app.db.get_project(res.project_id)
    assert p["status"] == "date_unknown" and p["mint_url"] is None and p["launch_at"] is None
    await app.notifier.flush_outbox()
    text = app.telegram.sent[0]["text"]
    assert "Mint link:</b> not found" in text and "unknown" in text


async def test_update_alert_on_date_change_without_duplicate_spam(app):
    await app.pipeline.ingest(make_post("Moon Toads free mint Oct 20 15:00 UTC", author="moontoads"))
    r = await app.pipeline.ingest(make_post("Moon Toads mint moved to Oct 21 15:00 UTC, still free mint",
                                            author="moontoads"))
    assert r.outcome == "updated" and any("launch time changed" in c for c in r.changes)
    # same claim again from the project -> no new alert
    r2 = await app.pipeline.ingest(make_post("Reminder: Moon Toads free mint Oct 21 15:00 UTC", author="moontoads"))
    assert r2.outcome == "unchanged"
    await app.notifier.flush_outbox()
    assert [m["text"].split("<")[0] for m in app.telegram.sent].count("🔄 ") <= 1
    assert len(app.telegram.sent) == 2


async def test_third_party_cannot_override_project_schedule(app):
    await app.pipeline.ingest(make_post("Moon Toads free mint Oct 20 15:00 UTC", author="moontoads"))
    await app.pipeline.ingest(make_post("@moontoads Moon Toads free mint Oct 25 15:00 UTC", author="randomcaller"))
    p = app.db.list_projects()[0]
    assert p["launch_at"].startswith("2026-10-20T15:00")
    assert any("third party" in n for n in p["notes"])


async def test_free_claim_from_third_party_is_not_confirmed(app):
    await app.pipeline.ingest(make_post("@moontoads Moon Toads free mint!", author="caller"))
    assert app.db.list_projects()[0]["price_status"] == "claimed_free"
    await app.pipeline.ingest(make_post("Our Moon Toads free mint is coming", author="moontoads"))
    assert app.db.list_projects()[0]["price_status"] == "project_claimed_free"


async def test_disabled_network_filtered(app):
    app.settings.set("network.solana", "off")
    r = await app.pipeline.ingest(make_post("Sol Sprouts free mint on Solana", author="solsprouts"))
    assert r.outcome == "filtered" and app.db.list_projects() == []


async def test_evidence_keeps_sources(app):
    p = make_post("Moon Toads free mint on Base Oct 20 15:00 UTC", author="moontoads", pid="555")
    r = await app.pipeline.ingest(p)
    ev = app.db.evidence_for(r.project_id)
    assert any(e["field"] == "launch" and e["source_url"].endswith("/status/555") for e in ev)
    assert all(e["source_kind"] == "project_account" for e in ev)


async def test_live_and_ended_signals(app, clock):
    r = await app.pipeline.ingest(make_post("Moon Toads free mint is live now!", author="moontoads"))
    assert app.db.get_project(r.project_id)["status"] == "live"
    clock.advance(hours=1)
    await app.pipeline.ingest(make_post("Moon Toads minted out! thanks fam free mint done", author="moontoads",
                                        created=T0 + timedelta(hours=1)))
    assert app.db.get_project(r.project_id)["status"] == "ended"
