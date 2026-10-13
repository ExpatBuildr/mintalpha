"""JSON API for the MintAlpha mobile app, plus the installable web build at /app/.

Auth: every /api/v1 route except /ping needs `Authorization: Bearer <MOBILE_API_TOKEN>`.
If MOBILE_API_TOKEN is unset the API answers 503, so it is never open by accident.
CORS is open (`*`) because auth is a bearer header, not a cookie; the native app's
WebView origin (https://localhost) needs it.
"""
from __future__ import annotations

import hmac
import json
import re
from html import unescape
from pathlib import Path

from aiohttp import web

from . import __version__
from .bot import apply_action
from .clock import to_iso
from .config import RUNTIME_KEYS
from .extract import confidence_label
from .messages import KIND_TITLE, VERIFICATION_LABEL
from .models import PRICE_LABEL, RESOLVED, InvalidTransition, PriceStatus, Status
from .security import is_high_risk

API = "/api/v1"
WEBAPP_DIR = Path(__file__).parent / "webapp"
STATUS_GROUPS = {
    "open": ["live", "upcoming", "date_unknown", "discovered"],
    "live": ["live"],
    "upcoming": ["upcoming", "date_unknown", "discovered"],
    "done": ["minted", "missed", "dismissed", "ended"],
    "all": None,
}
ACTIONS = {"minted", "missed", "dismissed", "snooze", "unsnooze", "reopen"}


def _json(data, status: int = 200) -> web.Response:
    return web.Response(text=json.dumps(data, default=str), status=status, content_type="application/json")


def _error(status: int, message: str) -> web.Response:
    return _json({"error": message}, status)


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if not request.path.startswith(API):
        return await handler(request)
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as exc:
            resp = _error(exc.status, exc.reason or "error")
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, OPTIONS"
    resp.headers["Access-Control-Max-Age"] = "600"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _plain(html_text: str) -> list[str]:
    text = unescape(re.sub(r"<[^>]+>", "", html_text or ""))
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def project_json(ctx, p: dict, detail: bool = False) -> dict:
    nets = ctx.settings.networks()
    chain = p.get("chain")
    flags = [{"text": f, "severity": "high" if is_high_risk(f) else "warn"} for f in p.get("risk_flags") or []]
    price = PriceStatus(p.get("price_status") or "unknown")
    out = {
        "id": p["id"],
        "name": p["name"],
        "handle": p.get("x_handle"),
        "chain": chain,
        "chain_label": (nets.get(chain) or {}).get("label") if chain else None,
        "fee_note": (nets.get(chain) or {}).get("fee_note") if chain else None,
        "status": p["status"],
        "resolved": Status(p["status"]) in RESOLVED or p["status"] == "ended",
        "launch_at": p.get("launch_at"),
        "launch_date": p.get("launch_date"),
        "launch_precision": p.get("launch_precision"),
        "launch_tz": p.get("launch_tz"),
        "launch_text": p.get("launch_text"),
        "price_status": price.value,
        "price_label": PRICE_LABEL[price],
        "price_text": p.get("price_text"),
        "supply": p.get("supply"),
        "eligibility": p.get("eligibility") or [],
        "website_url": p.get("website_url"),
        "mint_url": p.get("mint_url"),
        "mint_platform": p.get("mint_platform"),
        "source_url": p.get("first_source_url"),
        "confidence": p.get("confidence") or 0,
        "confidence_label": confidence_label(p.get("confidence") or 0),
        "verification": p.get("verification"),
        "verification_label": VERIFICATION_LABEL.get(p.get("verification"), p.get("verification")),
        "flags": flags,
        "high_risk": any(f["severity"] == "high" for f in flags),
        "notes": p.get("notes") or [],
        "snoozed_until": p.get("snoozed_until"),
        "live_since": p.get("live_since"),
        "resolved_at": p.get("resolved_at"),
        "created_at": p.get("created_at"),
        "updated_at": p.get("updated_at"),
    }
    if detail:
        db = ctx.db
        out["evidence"] = [{"field": e["field"], "value": e["value"], "source_kind": e["source_kind"],
                            "source_url": e["source_url"], "observed_at": e["observed_at"]}
                           for e in db.evidence_for(p["id"])]
        out["reminders"] = [{"kind": r["kind"], "due_at": r["due_at"]} for r in db.pending_reminders_for(p["id"])]
        out["history"] = [{"status": h["new_status"], "reason": h["reason"], "at": h["at"]}
                          for h in db.status_history(p["id"])]
        out["posts"] = [{"url": x["url"], "author": x["author_username"], "text": x["text"], "created_at": x["created_at"]}
                        for x in db.posts_for_project(p["id"])]
        out["live_repeat_minutes"] = ctx.settings.get("reminders.live_repeat_minutes")
    else:
        out["post_count"] = ctx.db.conn.execute("SELECT COUNT(*) FROM posts WHERE project_id=?", (p["id"],)).fetchone()[0]
    return out


def settings_json(ctx) -> dict:
    s = ctx.settings
    return {
        "runtime": {k: s.get(k) for k in RUNTIME_KEYS},
        "networks": {name: {"label": net.get("label", name), "enabled": s.network_enabled(name)}
                     for name, net in s.networks().items()},
        "display_timezone": s.display_tz(),
        "before_launch_minutes": s.get("reminders.before_launch_minutes", [1440, 60]),
    }


def register_mobile(app: web.Application, ctx) -> None:
    def authorized(request: web.Request) -> web.Response | None:
        token = ctx.config.env.mobile_api_token
        if not token:
            return _error(503, "Mobile API is off. Set MOBILE_API_TOKEN on the server and restart it.")
        if len(token) < 16:
            return _error(503, "MOBILE_API_TOKEN is too short. Use at least 16 random characters (see README).")
        header = request.headers.get("Authorization", "")
        given = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not given or not hmac.compare_digest(given.encode(), token.encode()):
            return _error(401, "Wrong or missing access token.")
        return None

    def guarded(fn):
        async def wrapper(request):
            denied = authorized(request)
            if denied is not None:
                return denied
            return await fn(request)
        return wrapper

    async def ping(request):
        return _json({"app": "free-mint-radar", "version": __version__, "api": 1,
                      "auth_configured": bool(ctx.config.env.mobile_api_token),
                      "server_time": to_iso(ctx.clock.now())})

    async def me(request):
        h = ctx.health()
        return _json({"server_time": to_iso(ctx.clock.now()), "health": h, "settings": settings_json(ctx),
                      "counts": ctx.db.count_by_status()})

    async def projects(request):
        group = request.query.get("status", "open")
        statuses = STATUS_GROUPS.get(group, [group] if group in {s.value for s in Status} else None)
        if group not in STATUS_GROUPS and statuses is None:
            return _error(400, f"Unknown status filter '{group}'.")
        rows = ctx.db.list_projects(statuses=statuses, q=request.query.get("q") or None,
                                    chain=request.query.get("chain") or None, limit=500)
        return _json({"server_time": to_iso(ctx.clock.now()), "projects": [project_json(ctx, p) for p in rows]})

    async def project(request):
        p = ctx.db.get_project(int(request.match_info["pid"]))
        if p is None:
            return _error(404, "Project not found.")
        return _json({"server_time": to_iso(ctx.clock.now()), "project": project_json(ctx, p, detail=True)})

    async def action(request):
        pid = int(request.match_info["pid"])
        if ctx.db.get_project(pid) is None:
            return _error(404, "Project not found.")
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return _error(400, "Send JSON like {\"action\": \"minted\"}.")
        act = str(body.get("action", ""))
        if act not in ACTIONS:
            return _error(400, f"action must be one of {sorted(ACTIONS)}.")
        hours = body.get("hours")
        if act == "snooze":
            try:
                hours = float(hours or 1)
            except (TypeError, ValueError):
                return _error(400, "hours must be a number.")
            if not 0 < hours <= 168:
                return _error(400, "hours must be between 0 and 168.")
        try:
            message = apply_action(ctx.db, ctx.engine, ctx.clock, pid, act, hours)
        except (InvalidTransition, ValueError) as exc:
            return _error(409, str(exc))
        p = ctx.db.get_project(pid)
        return _json({"message": message, "project": project_json(ctx, p, detail=True)})

    async def feed(request):
        try:
            limit = max(1, min(200, int(request.query.get("limit", 60))))
        except ValueError:
            return _error(400, "limit must be a number.")
        after = request.query.get("after")
        names = {r["id"]: r["name"] for r in ctx.db.conn.execute("SELECT id, name FROM projects")}
        events = []
        q = "SELECT * FROM alerts WHERE sent_at IS NOT NULL" + (" AND sent_at > ?" if after else "") + " ORDER BY id DESC LIMIT ?"
        for a in ctx.db.conn.execute(q, ([after] if after else []) + [limit]):
            lines = _plain(a["message"])
            events.append({"id": f"a{a['id']}", "kind": a["kind"], "project_id": a["project_id"], "at": a["sent_at"],
                           "title": lines[0] if lines else a["kind"], "body": " · ".join(lines[1:3])[:280]})
        q = "SELECT * FROM reminders WHERE sent_at IS NOT NULL" + (" AND sent_at > ?" if after else "") + " ORDER BY sent_at DESC LIMIT ?"
        for r in ctx.db.conn.execute(q, ([after] if after else []) + [limit]):
            if r["kind"] == "digest":
                title, body = "Digest: mints without an exact time", "Open the Upcoming tab"
            else:
                kind = "pre_1440" if r["kind"] == "pre_1440" else r["kind"]
                title = f"{KIND_TITLE.get(kind, 'Reminder')}: {names.get(r['project_id'], '#' + str(r['project_id']))}"
                body = f"Due {r['due_at']}"
            events.append({"id": f"r{r['id']}", "kind": r["kind"], "project_id": r["project_id"], "at": r["sent_at"],
                           "title": title, "body": body})
        events.sort(key=lambda e: e["at"] or "", reverse=True)
        return _json({"server_time": to_iso(ctx.clock.now()), "events": events[:limit]})

    async def get_settings(request):
        return _json(settings_json(ctx))

    async def patch_settings(request):
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return _error(400, "Send a JSON object of settings.")
        if not isinstance(body, dict):
            return _error(400, "Send a JSON object of settings.")
        changed = {}
        for key, value in body.items():
            if isinstance(value, bool):
                value = "on" if value else "off"
            try:
                changed[key] = ctx.settings.set(str(key), str(value))
            except ValueError as exc:
                return _error(400, str(exc))
        return _json({"changed": changed, "settings": settings_json(ctx)})

    app.router.add_get(f"{API}/ping", ping)
    app.router.add_get(f"{API}/me", guarded(me))
    app.router.add_get(f"{API}/projects", guarded(projects))
    app.router.add_get(f"{API}/projects/{{pid:\\d+}}", guarded(project))
    app.router.add_post(f"{API}/projects/{{pid:\\d+}}/actions", guarded(action))
    app.router.add_get(f"{API}/feed", guarded(feed))
    app.router.add_get(f"{API}/settings", guarded(get_settings))
    app.router.add_patch(f"{API}/settings", guarded(patch_settings))
    # Preflight for every API path
    app.router.add_route("OPTIONS", API + "/{tail:.*}", lambda r: web.Response(status=204))

    # Installable web build of MintAlpha (same files the Android project bundles)
    if WEBAPP_DIR.exists():
        async def app_index(request):
            return web.FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-cache"})

        async def app_redirect(request):
            raise web.HTTPFound("/app/")

        app.router.add_get("/app", app_redirect)
        app.router.add_get("/app/", app_index)
        app.router.add_static("/app/", WEBAPP_DIR, show_index=False)
