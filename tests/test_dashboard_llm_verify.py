import base64
import json

import httpx
from aiohttp.test_utils import TestClient, TestServer

from radar.classifier import LLMClassifier, merge_llm
from radar.dashboard import make_app
from radar.models import Extraction
from radar.verify import fetch_and_check
from tests.conftest import make_post


async def test_dashboard_pages_and_actions(app):
    r = await app.pipeline.ingest(make_post("Moon Toads free mint Oct 20 15:00 UTC on Base", author="moontoads"))
    await app.scheduler_once()
    async with TestClient(TestServer(make_app(app))) as client:
        resp = await client.get("/?q=Moon")
        assert resp.status == 200 and "Moon Toads" in await resp.text()
        resp = await client.get(f"/project/{r.project_id}")
        body = await resp.text()
        assert "Evidence" in body and "pre_1440" in body
        resp = await client.get("/calendar")
        assert "2026-10" in await resp.text()
        resp = await client.post(f"/project/{r.project_id}/action", data={"action": "dismissed"}, allow_redirects=False)
        assert resp.status == 303
        assert app.db.get_project(r.project_id)["status"] == "dismissed"
        resp = await client.get("/healthz")
        assert resp.status in (200, 503) and "pending_reminders" in json.loads(await resp.text())
        resp = await client.get("/api/projects")
        assert len(await resp.json()) == 1


async def test_dashboard_token_auth(app):
    app.config.env.dashboard_token = "s3cret"
    async with TestClient(TestServer(make_app(app))) as client:
        assert (await client.get("/")).status == 401
        auth = "Basic " + base64.b64encode(b"me:s3cret").decode()
        assert (await client.get("/", headers={"Authorization": auth})).status == 200
        assert (await client.get("/healthz")).status in (200, 503)


async def test_llm_fills_gaps_but_never_dates_or_links():
    def handler(req):
        out = {"is_free_mint_announcement": True, "project_name": "Glimmer Kids", "chain": "base",
               "supply": 777, "eligibility": ["allowlist"], "is_scam_likely": False, "reason": "",
               "mint_url": "https://evil.example/mint", "launch": "2026-10-20T00:00Z"}
        return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(out)}]})

    clf = LLMClassifier("k", "claude-haiku-5-5", ["base", "solana"],
                        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = await clf.classify(make_post("gm, glimmer drop for everyone, no cost, claim yours"))
    ex = merge_llm(Extraction(relevant=False), result, ["base", "solana"])
    assert ex.relevant and ex.name == "Glimmer Kids" and ex.chain == "base" and ex.supply == 777
    assert ex.mint_url is None and ex.schedule.launch_at is None


async def test_llm_failure_falls_back():
    clf = LLMClassifier("k", "m", [], http=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(500, text="boom"))))
    assert await clf.classify(make_post("x")) is None


async def test_verify_detects_free_and_backlink():
    page = "<html><body><h1>Moon Toads</h1><p>Price: FREE</p><a href='https://x.com/moontoads'>X</a></body></html>"
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=page)))
    res = await fetch_and_check(http, "https://moontoads.xyz", "moontoads", max_bytes=100000, timeout=5, check_host=False)
    assert res.ok and res.confirms_free and res.links_back_to_handle


async def test_verify_paid_and_js_only_pages():
    paid = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text="Mint price: 0.02 ETH")))
    res = await fetch_and_check(paid, "https://x.xyz", None, max_bytes=100000, timeout=5, check_host=False)
    assert res.paid_price == "0.02 ETH" and not res.confirms_free
    js = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text="<div id=root></div>")))
    res = await fetch_and_check(js, "https://x.xyz", None, max_bytes=100000, timeout=5, check_host=False)
    assert "inconclusive" in res.note


async def test_pipeline_verification_upgrades_price(app):
    page = "<p>Free mint — 0 ETH</p><a href='https://twitter.com/moontoads'>tw</a>"
    app.pipeline.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=page)))
    app.settings.config.data["verification"]["fetch_official_sites"] = True
    import radar.verify as v
    orig = v._is_public_host
    v._is_public_host = lambda host: True
    try:
        r = await app.pipeline.ingest(make_post("@moontoads Moon Toads free mint https://moontoads.xyz",
                                                author="caller", urls=["https://moontoads.xyz"]))
    finally:
        v._is_public_host = orig
    p = app.db.get_project(r.project_id)
    assert p["price_status"] == "confirmed_free" and p["verification"] == "site_confirms_free"


async def test_demo_runs_end_to_end():
    from radar.demo import run_demo
    summary = await run_demo(echo=False)
    assert summary["projects"] == 4 and summary["messages_sent"] > 10
