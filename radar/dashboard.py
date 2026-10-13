"""Small server-rendered dashboard (aiohttp). Bound to 127.0.0.1 by default.
If DASHBOARD_TOKEN is set, HTTP Basic auth is required (any username, token as password)."""
from __future__ import annotations

import base64
import hmac
import json
from collections import defaultdict
from html import escape
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from aiohttp import web

from .bot import apply_action
from .clock import fmt_dual, from_iso, humanize_delta
from .extract import confidence_label
from .messages import VERIFICATION_LABEL, schedule_line
from .models import PRICE_LABEL, PriceStatus, Status

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--muted:#5d6472;--line:#e3e6ec;--accent:#2f6fde;--warn:#b45309;--ok:#15803d;--bad:#b91c1c}
@media (prefers-color-scheme: dark){:root{--bg:#111318;--card:#1a1d24;--ink:#e8eaef;--muted:#9aa1ad;--line:#2b2f38;--accent:#6ea0ff;--warn:#f59e0b;--ok:#4ade80;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;gap:16px;align-items:center;flex-wrap:wrap;padding:14px 16px;border-bottom:1px solid var(--line);background:var(--card)}
header b{font-size:17px}header a{color:var(--muted);text-decoration:none}header a.on{color:var(--ink);font-weight:600}
main{max-width:1100px;margin:0 auto;padding:16px}
form.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
input,select,button{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
button{cursor:pointer}button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
td a{color:var(--accent)}.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid var(--line)}
.live{color:var(--ok);border-color:var(--ok)}.upcoming{color:var(--accent);border-color:var(--accent)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:14px}
.muted{color:var(--muted)}.actions{display:flex;gap:6px;flex-wrap:wrap}.actions form{margin:0}
dl{display:grid;grid-template-columns:150px 1fr;gap:6px 12px;margin:0}dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}
@media (max-width:640px){dl{grid-template-columns:1fr}td:nth-child(4),th:nth-child(4),td:nth-child(6),th:nth-child(6){display:none}}
"""


def _page(title: str, body: str, active: str = "") -> web.Response:
    nav = "".join(f'<a href="{href}" class="{"on" if key == active else ""}">{label}</a>'
                  for key, href, label in (("list", "/", "Projects"), ("calendar", "/calendar", "Calendar"),
                                           ("health", "/healthz", "Health")))
    html = (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{escape(title)}</title><style>{CSS}</style></head><body><header><b>Free Mint Radar</b>{nav}</header>"
            f"<main>{body}</main></body></html>")
    return web.Response(text=html, content_type="text/html")


def _status_pill(status: str) -> str:
    return f'<span class="pill {escape(status)}">{escape(status)}</span>'


def make_app(ctx) -> web.Application:
    """ctx: the RadarApp (db, settings, clock, engine, health())."""

    @web.middleware
    async def auth(request, handler):
        token = ctx.config.env.dashboard_token
        # /api/v1 (bearer token) and /app (static PWA) have their own access rules
        if token and request.path != "/healthz" and not request.path.startswith(("/api/v1", "/app")):
            header = request.headers.get("Authorization", "")
            ok = False
            if header.startswith("Basic "):
                try:
                    _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                    ok = hmac.compare_digest(pw, token)
                except (ValueError, UnicodeDecodeError):
                    ok = False
            if not ok:
                return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="radar"'}, text="auth required")
        return await handler(request)

    from .api import cors_middleware, register_mobile
    app = web.Application(middlewares=[cors_middleware, auth])
    tz = lambda: ctx.settings.display_tz()  # noqa: E731

    async def index(request):
        q = request.query.get("q") or None
        status = request.query.get("status") or ""
        chain = request.query.get("chain") or None
        statuses = {"open": ["live", "upcoming", "date_unknown", "discovered"], "": None}.get(status, [status])
        rows = ctx.db.list_projects(statuses=statuses, q=q, chain=chain, limit=300)
        now = ctx.clock.now()
        opts = "".join(f'<option value="{v}" {"selected" if v == status else ""}>{label}</option>'
                       for v, label in [("", "All statuses"), ("open", "Open")] + [(s.value, s.value) for s in Status])
        chains = "".join(f'<option value="{c}" {"selected" if c == chain else ""}>{c}</option>'
                         for c in [""] + list(ctx.settings.networks()))
        body = [f"""<form class=filters><input name=q placeholder="Search name, handle, domain" value="{escape(q or '')}">
<select name=status>{opts}</select><select name=chain>{chains}</select><button class=primary>Filter</button></form>"""]
        body.append("<table><tr><th>#</th><th>Project</th><th>Status</th><th>Chain</th><th>Launch</th><th>Confidence</th></tr>")
        for p in rows:
            launch = from_iso(p["launch_at"])
            when = humanize_delta(launch, now) if launch else (p["launch_date"] or "—")
            warn = ' <span class="warn" title="warnings">⚠</span>' if p["risk_flags"] else ""
            body.append(f"<tr><td>{p['id']}</td><td><a href='/project/{p['id']}'>{escape(p['name'])}</a>{warn}"
                        f"<div class=muted>{('@' + escape(p['x_handle'])) if p['x_handle'] else ''}</div></td>"
                        f"<td>{_status_pill(p['status'])}</td><td>{escape(p['chain'] or '?')}</td><td>{escape(when)}</td>"
                        f"<td>{confidence_label(p['confidence'])} {p['confidence']:.2f}</td></tr>")
        if not rows:
            body.append("<tr><td colspan=6 class=muted>No projects match.</td></tr>")
        body.append("</table>")
        return _page("Projects", "".join(body), "list")

    async def project(request):
        pid = int(request.match_info["pid"])
        p = ctx.db.get_project(pid)
        if not p:
            raise web.HTTPNotFound()
        now = ctx.clock.now()
        price = PRICE_LABEL.get(PriceStatus(p["price_status"]), p["price_status"])
        rows = [("Status", _status_pill(p["status"])), ("Chain", escape(p["chain"] or "unknown")),
                ("Price", escape(price)), ("Fees", escape(ctx.pipeline.fee_note(p["chain"]) or "—")),
                ("Launch", schedule_line(p, tz(), now)),
                ("Verification", escape(VERIFICATION_LABEL.get(p["verification"], p["verification"]))),
                ("Confidence", f"{confidence_label(p['confidence'])} ({p['confidence']:.2f})"),
                ("X account", f"@{escape(p['x_handle'])}" if p["x_handle"] else "—"),
                ("Website", _link(p["website_url"])), ("Mint link", _link(p["mint_url"])),
                ("Supply", f"{p['supply']:,}" if p["supply"] else "—"),
                ("Eligibility", escape(", ".join(p["eligibility"]) or "—")),
                ("First source", _link(p["first_source_url"])),
                ("Snoozed until", escape(fmt_dual(from_iso(p["snoozed_until"]), tz())) if p["snoozed_until"] else "—")]
        dl = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
        actions = "".join(
            f"<form method=post action='/project/{pid}/action'><input type=hidden name=action value='{a}'>"
            + (f"<input type=hidden name=hours value='{h}'>" if h else "") + f"<button>{label}</button></form>"
            for a, h, label in (("minted", "", "✅ Minted"), ("missed", "", "❌ Missed"), ("dismissed", "", "🙈 Dismiss"),
                                ("snooze", "1", "😴 1h"), ("snooze", "4", "😴 4h"), ("snooze", "24", "😴 24h"),
                                ("unsnooze", "", "Unsnooze"), ("reopen", "", "↩️ Reopen")))
        flash = f"<div class='card'>{escape(request.query['msg'])}</div>" if request.query.get("msg") else ""
        warn = "".join(f"<li>{escape(f)}</li>" for f in p["risk_flags"]) or "<li class=muted>none detected</li>"
        notes = "".join(f"<li>{escape(n)}</li>" for n in p["notes"]) or "<li class=muted>—</li>"
        ev = "".join(f"<tr><td>{escape(e['field'])}</td><td>{escape(str(e['value'])[:120])}</td><td>{escape(e['source_kind'])}</td>"
                     f"<td>{_link(e['source_url'], 'source')}</td><td class=muted>{escape(e['observed_at'])}</td></tr>"
                     for e in ctx.db.evidence_for(pid))
        rem = "".join(f"<tr><td>{escape(r['kind'])}</td><td>{escape(fmt_dual(from_iso(r['due_at']), tz()))}</td>"
                      f"<td>{'sent ' + escape(r['sent_at']) if r['sent_at'] else ('cancelled: ' + escape(r['cancel_reason'] or '')) if r['cancelled_at'] else 'pending'}</td></tr>"
                      for r in ctx.db.reminders_for(pid))
        hist = " → ".join(escape(h["new_status"]) for h in ctx.db.status_history(pid))
        posts = "".join(f"<li>{_link(x['url'], '@' + (x['author_username'] or ''))}: {escape((x['text'] or '')[:200])}</li>"
                        for x in ctx.db.posts_for_project(pid))
        body = f"""{flash}<div class=card><h2 style="margin-top:0">{escape(p['name'])} <span class=muted>#{pid}</span></h2>
<div class=actions>{actions}</div></div>
<div class=card><dl>{dl}</dl></div>
<div class=card><b>Warnings</b><ul>{warn}</ul><p class=muted>Checks reduce risk; they don't prove a link is safe. Never share a seed phrase; use a burner wallet.</p></div>
<div class=card><b>Notes</b><ul>{notes}</ul><b>Status history</b><p>{hist}</p></div>
<div class=card><b>Source posts</b><ul>{posts}</ul></div>
<div class=card><b>Evidence</b><table><tr><th>Field</th><th>Value</th><th>Kind</th><th>Source</th><th>Seen</th></tr>{ev}</table></div>
<div class=card><b>Reminders</b><table><tr><th>Kind</th><th>Due</th><th>State</th></tr>{rem or '<tr><td colspan=3 class=muted>none</td></tr>'}</table></div>"""
        return _page(p["name"], body)

    async def action(request):
        pid = int(request.match_info["pid"])
        form = await request.post()
        hours = float(form.get("hours") or 0) or None
        try:
            msg = apply_action(ctx.db, ctx.engine, ctx.clock, pid, str(form.get("action")), hours)
        except ValueError as exc:
            msg = f"Not allowed: {exc}"
        raise web.HTTPSeeOther(f"/project/{pid}?{urlencode({'msg': msg})}")

    async def calendar(request):
        now = ctx.clock.now()
        rows = ctx.db.list_projects(statuses=["upcoming", "live", "date_unknown", "discovered"], limit=500)
        groups: dict[str, list] = defaultdict(list)
        for p in rows:
            launch = from_iso(p["launch_at"])
            key = launch.astimezone(ZoneInfo(tz())).strftime("%Y-%m-%d %a") if launch else (
                p["launch_date"] + " (day only)" if p["launch_date"] else "No date yet")
            groups[key].append(p)
        body = []
        for key in sorted(groups, key=lambda k: (k == "No date yet", k)):
            items = "".join(
                f"<li>{escape(from_iso(p['launch_at']).astimezone(ZoneInfo(tz())).strftime('%H:%M %Z')) + ' · ' if p['launch_at'] else ''}"
                f"<a href='/project/{p['id']}'>{escape(p['name'])}</a> {_status_pill(p['status'])} <span class=muted>{escape(p['chain'] or '')}</span></li>"
                for p in sorted(groups[key], key=lambda p: p["launch_at"] or ""))
            body.append(f"<div class=card><b>{escape(key)}</b><ul>{items}</ul></div>")
        return _page("Calendar", "".join(body) or "<p class=muted>Nothing scheduled.</p>", "calendar")

    async def healthz(request):
        h = ctx.health()
        return web.json_response(h, status=200 if h["healthy"] else 503, dumps=lambda o: json.dumps(o, default=str))

    async def api_projects(request):
        rows = ctx.db.list_projects(statuses=request.query.getall("status", None) or None, q=request.query.get("q"), limit=500)
        return web.json_response(rows)

    app.router.add_get("/", index)
    app.router.add_get("/project/{pid:\\d+}", project)
    app.router.add_post("/project/{pid:\\d+}/action", action)
    app.router.add_get("/calendar", calendar)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/api/projects", api_projects)
    register_mobile(app, ctx)
    return app


def _link(url: str | None, label: str | None = None) -> str:
    if not url:
        return "—"
    safe = escape(url)
    if not url.startswith(("http://", "https://")):
        return safe
    return f"<a href='{safe}' rel='noopener noreferrer nofollow' target=_blank>{escape(label or url)}</a>"
