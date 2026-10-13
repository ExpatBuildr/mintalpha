from aiohttp.test_utils import TestClient, TestServer

from radar.dashboard import make_app
from tests.conftest import make_post

TOKEN = "test-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


async def _client(app):
    app.config.env.mobile_api_token = TOKEN
    client = TestClient(TestServer(make_app(app)))
    await client.start_server()
    return client


async def test_ping_is_public_and_reports_auth(app):
    client = await _client(app)
    try:
        r = await client.get("/api/v1/ping")
        data = await r.json()
        assert r.status == 200 and data["app"] == "free-mint-radar" and data["auth_configured"] is True
        assert r.headers["Access-Control-Allow-Origin"] == "*"
    finally:
        await client.close()


async def test_api_disabled_without_token(app):
    app.config.env.mobile_api_token = None
    async with TestClient(TestServer(make_app(app))) as client:
        r = await client.get("/api/v1/projects")
        assert r.status == 503 and "MOBILE_API_TOKEN" in (await r.json())["error"]


async def test_short_token_refused(app):
    app.config.env.mobile_api_token = "short"
    async with TestClient(TestServer(make_app(app))) as client:
        r = await client.get("/api/v1/projects", headers={"Authorization": "Bearer short"})
        assert r.status == 503 and "too short" in (await r.json())["error"]


async def test_auth_required_and_wrong_token_rejected(app):
    client = await _client(app)
    try:
        assert (await client.get("/api/v1/projects")).status == 401
        assert (await client.get("/api/v1/projects", headers={"Authorization": "Bearer nope"})).status == 401
        assert (await client.get("/api/v1/projects", headers=AUTH)).status == 200
    finally:
        await client.close()


async def test_cors_preflight(app):
    client = await _client(app)
    try:
        r = await client.options("/api/v1/projects/1/actions", headers={"Origin": "https://localhost",
                                                                          "Access-Control-Request-Method": "POST"})
        assert r.status == 204
        assert "Authorization" in r.headers["Access-Control-Allow-Headers"]
    finally:
        await client.close()


async def test_list_detail_and_action_flow(app):
    r1 = await app.pipeline.ingest(make_post("Moon Toads free mint on Base Oct 20 15:00 UTC https://moontoads.xyz/mint",
                                             author="moontoads"))
    await app.pipeline.ingest(make_post("Glow Frogs free mint is live now on Polygon", author="glowfrogs"))
    await app.scheduler_once()
    client = await _client(app)
    try:
        data = await (await client.get("/api/v1/projects?status=open", headers=AUTH)).json()
        names = {p["name"]: p for p in data["projects"]}
        assert set(names) == {"Moon Toads", "Glow Frogs"}
        toads = names["Moon Toads"]
        assert toads["chain"] == "base" and toads["chain_label"] == "Base" and toads["launch_precision"] == "exact"
        assert toads["price_status"] == "project_claimed_free" and toads["fee_note"]
        assert toads["mint_url"] == "https://moontoads.xyz/mint" and toads["post_count"] == 1

        live = await (await client.get("/api/v1/projects?status=live", headers=AUTH)).json()
        assert [p["name"] for p in live["projects"]] == ["Glow Frogs"]

        d = await (await client.get(f"/api/v1/projects/{r1.project_id}", headers=AUTH)).json()
        kinds = {r["kind"] for r in d["project"]["reminders"]}
        assert kinds == {"pre_1440", "pre_60", "live"} and d["project"]["evidence"]

        r = await client.post(f"/api/v1/projects/{r1.project_id}/actions", json={"action": "snooze", "hours": 4}, headers=AUTH)
        assert r.status == 200 and (await r.json())["project"]["snoozed_until"]
        r = await client.post(f"/api/v1/projects/{r1.project_id}/actions", json={"action": "minted"}, headers=AUTH)
        body = await r.json()
        assert body["project"]["status"] == "minted" and body["project"]["resolved"] is True
        assert app.db.get_project(r1.project_id)["status"] == "minted"
        assert app.db.pending_reminders_for(r1.project_id) == []

        r = await client.post(f"/api/v1/projects/{r1.project_id}/actions", json={"action": "missed"}, headers=AUTH)
        assert r.status == 409
        r = await client.post(f"/api/v1/projects/{r1.project_id}/actions", json={"action": "reopen"}, headers=AUTH)
        assert (await r.json())["project"]["status"] == "upcoming"
    finally:
        await client.close()


async def test_action_validation(app):
    r1 = await app.pipeline.ingest(make_post("Moon Toads free mint is live now", author="moontoads"))
    client = await _client(app)
    try:
        url = f"/api/v1/projects/{r1.project_id}/actions"
        assert (await client.post(url, json={"action": "delete"}, headers=AUTH)).status == 400
        assert (await client.post(url, json={"action": "snooze", "hours": 999}, headers=AUTH)).status == 400
        assert (await client.post(url, data="nope", headers=AUTH)).status == 400
        assert (await client.post("/api/v1/projects/9999/actions", json={"action": "minted"}, headers=AUTH)).status == 404
        assert (await client.get("/api/v1/projects?status=bogus", headers=AUTH)).status == 400
    finally:
        await client.close()


async def test_feed_includes_alerts_and_reminders(app, clock):
    await app.pipeline.ingest(make_post("Moon Toads free mint is live now on Base", author="moontoads"))
    await app.scheduler_once()  # flushes discovery alert + sends live reminder
    client = await _client(app)
    try:
        data = await (await client.get("/api/v1/feed", headers=AUTH)).json()
        kinds = [e["kind"] for e in data["events"]]
        assert "discovery" in kinds and "live" in kinds
        disc = next(e for e in data["events"] if e["kind"] == "discovery")
        assert disc["title"].startswith("🆕 Free mint found: Moon Toads") and "<" not in disc["body"]
    finally:
        await client.close()


async def test_settings_get_and_patch(app):
    client = await _client(app)
    try:
        s = await (await client.get("/api/v1/settings", headers=AUTH)).json()
        assert s["runtime"]["reminders.live_repeat_minutes"] == 30 and s["networks"]["solana"]["enabled"] is True
        r = await client.patch("/api/v1/settings", json={"reminders.live_repeat_minutes": 15, "network.solana": False,
                                                         "discovery.paused": True}, headers=AUTH)
        body = await r.json()
        assert r.status == 200 and body["settings"]["runtime"]["reminders.live_repeat_minutes"] == 15
        assert body["settings"]["networks"]["solana"]["enabled"] is False and app.settings.get("discovery.paused") is True
        r = await client.patch("/api/v1/settings", json={"reminders.live_repeat_minutes": 1}, headers=AUTH)
        assert r.status == 400
        r = await client.patch("/api/v1/settings", json={"app.db_path": "/x"}, headers=AUTH)
        assert r.status == 400
    finally:
        await client.close()


async def test_webapp_served_and_not_behind_dashboard_password(app):
    app.config.env.dashboard_token = "dash"
    client = await _client(app)
    try:
        r = await client.get("/app/")
        assert r.status == 200 and "MintAlpha" in await r.text()
        assert (await client.get("/app/manifest.webmanifest")).status == 200
        assert (await client.get("/app/icons/icon-512.png")).status == 200
        assert (await client.get("/")).status == 401  # dashboard still protected
    finally:
        await client.close()
