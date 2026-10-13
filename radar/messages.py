"""Telegram message formatting (HTML parse mode)."""
from __future__ import annotations

from datetime import datetime
from html import escape

from .clock import fmt_dual, from_iso, humanize_delta
from .extract import confidence_label
from .models import PRICE_LABEL, PriceStatus

SAFETY_FOOTER = ("🔐 Never share a seed phrase. Check the domain yourself, use a burner wallet, "
                 "and read what you sign. Checks reduce risk; they don't guarantee safety.")

VERIFICATION_LABEL = {
    "unverified": "unverified (third-party posts only)",
    "project_account": "stated by the project's own X account",
    "site_crosslinked": "official site links back to the project's X account",
    "site_confirms_free": "official site / mint page shows it as free",
}

KIND_TITLE = {
    "pre_1440": "⏰ Mint in ~24 hours",
    "pre_60": "⏰ Mint in ~1 hour",
    "live": "🟢 MINT IS LIVE",
    "repeat": "🔁 Still live — not resolved yet",
}


def _e(v) -> str:
    return escape(str(v)) if v is not None else ""


def schedule_line(p: dict, display_tz: str, now: datetime) -> str:
    launch = from_iso(p.get("launch_at"))
    if p.get("launch_precision") == "exact" and launch:
        src = f" · stated as {_e(p['launch_tz'])}" if p.get("launch_tz") and p["launch_tz"] not in ("UTC", "relative") else ""
        return f"{fmt_dual(launch, display_tz)} ({humanize_delta(launch, now)}){src}"
    if p.get("launch_precision") == "day" and p.get("launch_date"):
        return f"{_e(p['launch_date'])} — time/zone not confirmed (no timed reminders)"
    return "unknown (tracking for updates)"


def project_block(p: dict, display_tz: str, now: datetime, fee_note: str | None, chain_label: str | None) -> list[str]:
    lines = []
    chain = _e(chain_label or p.get("chain")) if p.get("chain") else "unknown"
    lines.append(f"<b>Chain:</b> {chain}")
    price = PRICE_LABEL.get(PriceStatus(p.get("price_status") or "unknown"), "unknown")
    lines.append(f"<b>Price:</b> {_e(price)}" + (f" — “{_e(p['price_text'])}”" if p.get("price_text") else ""))
    if fee_note:
        lines.append(f"<i>{_e(fee_note)}</i>")
    lines.append(f"<b>Launch:</b> {schedule_line(p, display_tz, now)}")
    lines.append(f"<b>Status:</b> {_e(p['status'])}")
    if p.get("supply"):
        lines.append(f"<b>Supply:</b> {p['supply']:,}")
    if p.get("eligibility"):
        lines.append(f"<b>Eligibility:</b> {_e(', '.join(p['eligibility']))}")
    lines.append(f"<b>Confidence:</b> {confidence_label(p.get('confidence') or 0)} ({p.get('confidence') or 0:.2f}) · "
                 f"<b>Verification:</b> {_e(VERIFICATION_LABEL.get(p.get('verification'), p.get('verification')))}")
    if p.get("first_source_url"):
        lines.append(f"<b>Source:</b> <a href=\"{_e(p['first_source_url'])}\">X post</a>"
                     + (f" · @{_e(p['x_handle'])}" if p.get("x_handle") else ""))
    lines.append(f"<b>Website:</b> {_e(p['website_url'])}" if p.get("website_url") else "<b>Website:</b> not found")
    if p.get("mint_url"):
        plat = f" ({_e(p['mint_platform'])})" if p.get("mint_platform") else ""
        lines.append(f"<b>Mint link:</b> {_e(p['mint_url'])}{plat}")
    else:
        lines.append("<b>Mint link:</b> not found yet")
    flags = p.get("risk_flags") or []
    if flags:
        lines.append("⚠️ <b>Warnings:</b>")
        lines.extend(f"• {_e(f)}" for f in flags[:8])
    return lines


def discovery(p: dict, display_tz: str, now: datetime, fee_note: str | None, chain_label: str | None) -> str:
    head = f"🆕 <b>Free mint found:</b> {_e(p['name'])}  <code>#{p['id']}</code>"
    return "\n".join([head, *project_block(p, display_tz, now, fee_note, chain_label), "", SAFETY_FOOTER])


def update(p: dict, changes: list[str], display_tz: str, now: datetime) -> str:
    head = f"🔄 <b>Update:</b> {_e(p['name'])}  <code>#{p['id']}</code>"
    body = [f"• {_e(c)}" for c in changes]
    tail = [f"<b>Launch:</b> {schedule_line(p, display_tz, now)}", f"<b>Status:</b> {_e(p['status'])}"]
    if p.get("mint_url"):
        tail.append(f"<b>Mint link:</b> {_e(p['mint_url'])}")
    return "\n".join([head, *body, *tail])


def reminder(kind: str, p: dict, display_tz: str, now: datetime) -> str:
    head = f"{KIND_TITLE.get(kind, '⏰ Reminder')}: <b>{_e(p['name'])}</b>  <code>#{p['id']}</code>"
    lines = [head, f"<b>Launch:</b> {schedule_line(p, display_tz, now)}"]
    if p.get("chain"):
        lines.append(f"<b>Chain:</b> {_e(p['chain'])}")
    lines.append(f"<b>Mint link:</b> {_e(p['mint_url'])}" if p.get("mint_url")
                 else "<b>Mint link:</b> not found — check the source post")
    if p.get("first_source_url"):
        lines.append(f"<a href=\"{_e(p['first_source_url'])}\">Source post</a>")
    flags = p.get("risk_flags") or []
    if flags:
        lines.append(f"⚠️ {len(flags)} warning(s) — tap Details")
    if kind in ("live", "repeat"):
        lines.append("Tap <b>Minted</b>, <b>Missed</b> or <b>Dismiss</b> to stop these reminders, or <b>Snooze</b>.")
    return "\n".join(lines)


def digest(projects: list[dict], display_tz: str, now: datetime) -> str:
    lines = [f"📋 <b>Tracking {len(projects)} mint(s) without an exact time</b>"]
    for p in projects[:30]:
        when = p.get("launch_date") or "date unknown"
        lines.append(f"• <code>#{p['id']}</code> {_e(p['name'])} — {_e(p.get('chain') or 'chain ?')} — {_e(when)}")
    if len(projects) > 30:
        lines.append(f"…and {len(projects) - 30} more (see dashboard or /active)")
    lines.append("Use /details &lt;id&gt; for sources. Timed reminders start once an exact time + zone is posted.")
    return "\n".join(lines)


def details(p: dict, evidence: list, history: list, reminders: list, display_tz: str, now: datetime,
            fee_note: str | None, chain_label: str | None) -> str:
    lines = [f"🔎 <b>{_e(p['name'])}</b>  <code>#{p['id']}</code>", *project_block(p, display_tz, now, fee_note, chain_label)]
    if p.get("notes"):
        lines.append("<b>Notes:</b>")
        lines.extend(f"• {_e(n)}" for n in p["notes"][:6])
    lines.append("<b>Evidence:</b>")
    for ev in evidence[-10:]:
        src = f" <a href=\"{_e(ev['source_url'])}\">src</a>" if ev["source_url"] else ""
        lines.append(f"• {_e(ev['field'])} = {_e(str(ev['value'])[:60])} [{_e(ev['source_kind'])}]{src}")
    pending = [r for r in reminders if not r["sent_at"] and not r["cancelled_at"]]
    if pending:
        lines.append("<b>Scheduled reminders:</b>")
        lines.extend(f"• {_e(r['kind'])} at {fmt_dual(from_iso(r['due_at']), display_tz)}" for r in pending[:5])
    if p.get("snoozed_until"):
        lines.append(f"😴 Snoozed until {fmt_dual(from_iso(p['snoozed_until']), display_tz)}")
    lines.append(f"<b>History:</b> " + " → ".join(_e(h["new_status"]) for h in history[-6:]))
    text = "\n".join(lines)
    return text[:4000]


def project_line(p: dict, display_tz: str, now: datetime) -> str:
    launch = from_iso(p.get("launch_at"))
    when = (humanize_delta(launch, now) if launch else p.get("launch_date") or "date ?")
    warn = " ⚠️" if p.get("risk_flags") else ""
    return f"<code>#{p['id']}</code> {_e(p['name'])} · {_e(p.get('chain') or '?')} · {_e(p['status'])} · {_e(when)}{warn}"
