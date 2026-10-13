"""Telegram command/callback handling, shared project actions, and the alert outbox."""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from html import escape
from typing import Callable

from . import messages
from .clock import Clock, from_iso, to_iso
from .config import RUNTIME_KEYS, Settings
from .db import Database
from .models import RESOLVED, Status
from .pipeline import derive_status
from .reminders import ReminderEngine
from .telegram import project_keyboard, snooze_keyboard

log = logging.getLogger("radar.bot")

COMMANDS = [
    ("start", "Show help and check the bot is linked"),
    ("help", "All commands"),
    ("active", "Open opportunities"),
    ("upcoming", "Mints with a known date"),
    ("live", "Mints live right now"),
    ("details", "Details for a project: /details 12"),
    ("status", "Bot health, API usage and budget"),
    ("settings", "Show settings; change with /set"),
]

HELP = """<b>Free Mint Radar</b>
/active — open opportunities
/upcoming — mints with a date
/live — live right now
/details &lt;id&gt; — sources, evidence, reminders
/minted &lt;id&gt; · /missed &lt;id&gt; · /dismiss &lt;id&gt;
/snooze &lt;id&gt; &lt;hours&gt; · /reopen &lt;id&gt;
/status — health, X API usage &amp; budget
/settings — current settings
/set &lt;key&gt; &lt;value&gt; — e.g. <code>/set reminders.live_repeat_minutes 15</code>
/set network.solana off — toggle a chain
/pause · /resume — stop/start X polling (saves API spend)
Buttons under each alert do the same as the commands."""


def apply_action(db: Database, engine: ReminderEngine, clock: Clock, pid: int, action: str,
                 hours: float | None = None) -> str:
    """Shared by Telegram and the dashboard. Returns a short confirmation."""
    now = clock.now()
    p = db.get_project(pid)
    if p is None:
        return f"Project #{pid} not found"
    name = p["name"]
    with db.tx():
        if action in ("minted", "missed", "dismissed"):
            target = Status(action)
            db.set_status(pid, target, f"user marked {action}", now)
            db.update_project(pid, {"snoozed_until": None}, now)
            db.add_evidence(pid, "user_action", action, None, "user", None, now)
            msg = f"#{pid} {name}: marked {action}. Reminders stopped."
        elif action == "snooze":
            hrs = hours or 1
            until = now + timedelta(hours=hrs)
            db.update_project(pid, {"snoozed_until": to_iso(until)}, now)
            msg = f"#{pid} {name}: snoozed for {hrs:g}h."
        elif action == "unsnooze":
            db.update_project(pid, {"snoozed_until": None}, now)
            msg = f"#{pid} {name}: snooze cleared."
        elif action == "reopen":
            cur = Status(p["status"])
            if cur in RESOLVED or cur == Status.ENDED:
                db.set_status(pid, Status.DATE_UNKNOWN, "user reopened", now, force=True)
            new = derive_status(Status.DATE_UNKNOWN, from_iso(p["launch_at"]), p["launch_precision"], False, False, now)
            if new != Status.DATE_UNKNOWN:
                db.set_status(pid, new, "re-derived after reopen", now)
            msg = f"#{pid} {name}: reopened ({new.value})."
        else:
            return f"Unknown action '{action}'"
    engine.sync_project(pid)
    log.info("user_action", extra={"project_id": pid, "action": action})
    return msg


class Notifier:
    def __init__(self, db: Database, telegram, chat_id: str, clock: Clock):
        self.db = db
        self.telegram = telegram
        self.chat_id = chat_id
        self.clock = clock

    async def send(self, text: str, pid: int | None, buttons: bool) -> int:
        kb = project_keyboard(pid) if (buttons and pid is not None) else None
        return await self.telegram.send_message(self.chat_id, text, kb)

    async def flush_outbox(self) -> int:
        sent = 0
        for a in self.db.pending_alerts():
            p = self.db.get_project(a["project_id"]) if a["project_id"] else None
            if p and a["kind"] == "update" and Status(p["status"]) in RESOLVED:
                self.db.mark_alert_sent(a["id"], None, self.clock.now())  # muted: resolved meanwhile
                continue
            try:
                mid = await self.send(a["message"], a["project_id"], bool(a["with_buttons"]))
            except Exception as exc:
                self.db.mark_alert_failed(a["id"], str(exc))
                log.warning("alert_send_failed", extra={"alert_id": a["id"], "error": str(exc)})
                break  # keep order; retry next loop
            self.db.mark_alert_sent(a["id"], mid, self.clock.now())
            sent += 1
        return sent


class BotHandler:
    def __init__(self, db: Database, settings: Settings, clock: Clock, telegram, chat_id: str,
                 engine: ReminderEngine, health: Callable[[], dict], fee_note, chain_label):
        self.db = db
        self.settings = settings
        self.clock = clock
        self.telegram = telegram
        self.chat_id = str(chat_id)
        self.engine = engine
        self.health = health
        self.fee_note = fee_note
        self.chain_label = chain_label

    async def handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            await self._callback(update["callback_query"])
        elif "message" in update:
            await self._message(update["message"])

    async def _reply(self, text: str, buttons=None) -> None:
        await self.telegram.send_message(self.chat_id, text, buttons)

    async def _message(self, msg: dict) -> None:
        chat = str((msg.get("chat") or {}).get("id"))
        text = (msg.get("text") or "").strip()
        if chat != self.chat_id:
            if text.startswith("/start"):
                await self.telegram.send_message(chat, f"This bot is private. Your chat id is <code>{escape(chat)}</code> — "
                                                       "put it in TELEGRAM_CHAT_ID if this is your bot.")
            log.warning("unauthorized_message", extra={"chat_id": chat})
            return
        if not text.startswith("/"):
            return
        parts = text.split()
        cmd = parts[0].split("@")[0][1:].lower()
        args = parts[1:]
        now = self.clock.now()
        tz = self.settings.display_tz()
        try:
            if cmd in ("start", "help"):
                await self._reply(HELP)
            elif cmd in ("active", "upcoming", "live"):
                statuses = {"active": ["live", "upcoming", "date_unknown", "discovered"],
                            "upcoming": ["upcoming"], "live": ["live"]}[cmd]
                rows = self.db.list_projects(statuses=statuses, limit=40)
                if not rows:
                    await self._reply(f"No {cmd} projects.")
                else:
                    lines = [f"<b>{cmd.title()} ({len(rows)})</b>"] + [messages.project_line(p, tz, now) for p in rows]
                    lines.append("/details &lt;id&gt; for more")
                    await self._reply("\n".join(lines)[:4000])
            elif cmd == "details":
                await self._details(int(args[0]))
            elif cmd in ("minted", "missed", "dismiss", "dismissed", "reopen", "unsnooze"):
                action = {"dismiss": "dismissed"}.get(cmd, cmd)
                await self._reply(escape(apply_action(self.db, self.engine, self.clock, int(args[0]), action)))
            elif cmd == "snooze":
                hours = float(args[1]) if len(args) > 1 else 1.0
                await self._reply(escape(apply_action(self.db, self.engine, self.clock, int(args[0]), "snooze", hours)))
            elif cmd == "status":
                await self._reply(self._status_text())
            elif cmd == "settings":
                await self._reply(self._settings_text())
            elif cmd == "set":
                if len(args) < 2:
                    await self._reply("Usage: /set &lt;key&gt; &lt;value&gt;")
                    return
                value = self.settings.set(args[0], args[1])
                await self._reply(f"✅ {escape(args[0])} = {escape(json.dumps(value))}")
            elif cmd in ("pause", "resume"):
                self.settings.set("discovery.paused", "on" if cmd == "pause" else "off")
                await self._reply("⏸ X polling paused." if cmd == "pause" else "▶️ X polling resumed.")
            else:
                await self._reply("Unknown command. /help")
        except (IndexError, ValueError) as exc:
            await self._reply(f"⚠️ {escape(str(exc) or 'missing argument')} — see /help")

    async def _details(self, pid: int) -> None:
        p = self.db.get_project(pid)
        if not p:
            await self._reply(f"Project #{pid} not found")
            return
        text = messages.details(p, self.db.evidence_for(pid), self.db.status_history(pid), self.db.reminders_for(pid),
                                self.settings.display_tz(), self.clock.now(), self.fee_note(p["chain"]),
                                self.chain_label(p["chain"]))
        await self._reply(text, project_keyboard(pid))

    async def _callback(self, cq: dict) -> None:
        chat = str(((cq.get("message") or {}).get("chat") or {}).get("id"))
        cid = cq.get("id", "")
        if chat != self.chat_id:
            await self.telegram.answer_callback(cid, "Not authorized")
            return
        data = cq.get("data") or ""
        try:
            _, act, pid_s = data.split(":")
            pid = int(pid_s)
        except ValueError:
            await self.telegram.answer_callback(cid, "Bad button")
            return
        mid = (cq.get("message") or {}).get("message_id")
        if act == "s":
            await self.telegram.edit_buttons(self.chat_id, mid, snooze_keyboard(pid))
            await self.telegram.answer_callback(cid, "Snooze for how long?")
            return
        if act == "b":
            await self.telegram.edit_buttons(self.chat_id, mid, project_keyboard(pid))
            await self.telegram.answer_callback(cid)
            return
        if act == "v":
            await self.telegram.answer_callback(cid)
            await self._details(pid)
            return
        mapping = {"m": ("minted", None), "x": ("missed", None), "d": ("dismissed", None),
                   "s1": ("snooze", 1), "s4": ("snooze", 4), "s24": ("snooze", 24)}
        if act not in mapping:
            await self.telegram.answer_callback(cid, "Unknown action")
            return
        action, hours = mapping[act]
        try:
            result = apply_action(self.db, self.engine, self.clock, pid, action, hours)
        except ValueError as exc:
            result = f"Not allowed: {exc}"
        await self.telegram.answer_callback(cid, result)
        if action != "snooze":
            await self.telegram.edit_buttons(self.chat_id, mid, None)
        else:
            await self.telegram.edit_buttons(self.chat_id, mid, project_keyboard(pid))
        await self._reply(escape(result))

    def _status_text(self) -> str:
        h = self.health()
        counts = self.db.count_by_status()
        lines = [f"<b>Status:</b> {'✅ healthy' if h['healthy'] else '⚠️ degraded'}",
                 f"Mode: X={escape(h['x_source'])}, Telegram={escape(h['telegram_mode'])}"
                 + (" · ⏸ polling paused" if h.get("paused") else ""),
                 f"Last discovery: {escape(str(h.get('last_discovery_ok_at') or 'never'))}",
                 f"Last scheduler tick: {escape(str(h.get('last_scheduler_tick_at') or 'never'))}"]
        if h.get("last_discovery_error"):
            lines.append(f"Last error: {escape(h['last_discovery_error'][:200])}")
        u = h.get("usage", {})
        lines.append(f"X reads today: {u.get('reads_today', 0)}/{u.get('daily_budget')} · "
                     f"month: {u.get('reads_month', 0)}/{u.get('monthly_budget')} "
                     f"(≈${u.get('est_cost_month', 0):.2f} est.)")
        lines.append("Projects: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"))
        lines.append(f"Pending reminders: {h.get('pending_reminders', 0)} · unsent alerts: {h.get('unsent_alerts', 0)}")
        return "\n".join(lines)

    def _settings_text(self) -> str:
        lines = ["<b>Runtime settings</b> (change with /set)"]
        for key in RUNTIME_KEYS:
            lines.append(f"<code>{key}</code> = {escape(json.dumps(self.settings.get(key)))}")
        lines.append("<b>Networks</b> (<code>/set network.&lt;name&gt; on|off</code>)")
        for name in self.settings.networks():
            lines.append(f"{'🟢' if self.settings.network_enabled(name) else '⚪️'} {name}")
        lines.append(f"Display time zone: {escape(self.settings.display_tz())}")
        return "\n".join(lines)
