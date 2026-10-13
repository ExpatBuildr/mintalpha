"""Database-driven reminder engine.

Every reminder is a row with a unique dedupe_key, so:
  * restarts lose nothing (rows persist; the next tick picks up anything due),
  * repeated ticks / repeated posts never create duplicates,
  * after downtime, stale one-shot reminders are cancelled instead of sent late in a burst,
    and live nags resume with ONE reminder rather than a catch-up storm.
Delivery is at-least-once: a crash between Telegram accepting a message and the DB
recording it can produce a single duplicate. That is preferred over silently losing a reminder.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from . import messages
from .clock import Clock, from_iso, to_iso
from .config import Settings
from .db import Database
from .models import RESOLVED, Status
from .security import is_high_risk

log = logging.getLogger("radar.reminders")
Sender = Callable[[str, "int | None", bool], Awaitable["int | None"]]


class ReminderEngine:
    def __init__(self, db: Database, settings: Settings, clock: Clock, sender: Sender):
        self.db = db
        self.settings = settings
        self.clock = clock
        self.sender = sender

    # -- config shortcuts --------------------------------------------------------
    @property
    def grace(self) -> timedelta:
        return timedelta(minutes=int(self.settings.get("reminders.catchup_grace_minutes", 10)))

    def _before(self) -> list[int]:
        return [int(m) for m in self.settings.get("reminders.before_launch_minutes", [1440, 60])]

    # -- scheduling ----------------------------------------------------------------
    def sync_project(self, pid: int) -> None:
        now = self.clock.now()
        with self.db.tx():
            p = self.db.get_project(pid)
            if p is None:
                return
            status = Status(p["status"])
            pending = self.db.pending_reminders_for(pid)
            if status in RESOLVED or status == Status.ENDED:
                for r in pending:
                    self.db.cancel_reminder(r["id"], f"project {status.value}", now)
                return
            wanted: dict[str, tuple[str, datetime]] = {}
            live_key = f"{pid}:live"
            launch = from_iso(p["launch_at"])
            if p["launch_precision"] == "exact" and launch:
                for minutes in self._before():
                    due = launch - timedelta(minutes=minutes)
                    if due >= now - self.grace:
                        wanted[f"{pid}:pre_{minutes}:{to_iso(launch)}"] = (f"pre_{minutes}", due)
                wanted[live_key] = ("live", now if status == Status.LIVE else launch)
            elif status == Status.LIVE:
                wanted[live_key] = ("live", now)
            for key, (kind, due) in wanted.items():
                if not self.db.insert_reminder(pid, kind, due, key, now) and key == live_key:
                    self.db.reschedule_reminder(key, due)
            for r in pending:
                if r["kind"] != "repeat" and r["dedupe_key"] not in wanted:
                    self.db.cancel_reminder(r["id"], "schedule changed", now)

    def advance_statuses(self) -> list[int]:
        """Move upcoming->live when the exact launch time passes; optional auto-end."""
        now = self.clock.now()
        changed = []
        rows = self.db.conn.execute(
            "SELECT id FROM projects WHERE status IN ('upcoming','discovered') AND launch_precision='exact' "
            "AND launch_at <= ?", (to_iso(now),)).fetchall()
        for r in rows:
            with self.db.tx():
                if self.db.set_status(r["id"], Status.LIVE, "launch time reached", now):
                    changed.append(r["id"])
        hours = self.settings.get("reminders.auto_end_live_after_hours")
        if hours:
            cutoff = to_iso(now - timedelta(hours=float(hours)))
            for r in self.db.conn.execute("SELECT id FROM projects WHERE status='live' AND live_since <= ?", (cutoff,)):
                with self.db.tx():
                    if self.db.set_status(r["id"], Status.ENDED, f"auto-ended after {hours}h live", now):
                        changed.append(r["id"])
        return changed

    def plan_recurring(self) -> None:
        now = self.clock.now()
        interval = timedelta(minutes=int(self.settings.get("reminders.live_repeat_minutes", 30)))
        rows = self.db.conn.execute(
            "SELECT id, live_since, last_nag_at, snoozed_until, risk_flags FROM projects WHERE status='live'").fetchall()
        quiet_risky = bool(self.settings.get("reminders.no_repeat_for_high_risk", True))
        for r in rows:
            if quiet_risky and any(is_high_risk(f) for f in json.loads(r["risk_flags"] or "[]")):
                continue  # one live alert is enough for something that looks like a scam
            snoozed = from_iso(r["snoozed_until"])
            if snoozed and snoozed > now:
                continue
            anchors = [d for d in (from_iso(r["live_since"]), from_iso(r["last_nag_at"])) if d]
            anchor = max(anchors) if anchors else now
            if now - anchor >= interval:
                with self.db.tx():
                    if self.db.insert_reminder(r["id"], "repeat", now, f"{r['id']}:repeat:{to_iso(now)}", now):
                        self.db.update_project(r["id"], {"last_nag_at": to_iso(now)}, now)
        hours = int(self.settings.get("reminders.date_unknown_digest_hours", 12))
        last = from_iso(self.db.kv_get("last_digest_at"))
        if last is None:
            self.db.kv_set("last_digest_at", to_iso(now))  # first digest one interval after first start
            return
        if now - last >= timedelta(hours=hours) and self._digest_projects(now):
            with self.db.tx():
                self.db.insert_reminder(None, "digest", now, f"digest:{to_iso(now)}", now)
                self.db.kv_set("last_digest_at", to_iso(now))

    def _digest_projects(self, now: datetime) -> list[dict]:
        rows = self.db.list_projects(statuses=["date_unknown", "upcoming", "discovered"], limit=500)
        out = []
        for p in rows:
            if p["status"] == "upcoming" and p["launch_precision"] == "exact":
                continue
            snoozed = from_iso(p["snoozed_until"])
            if snoozed and snoozed > now:
                continue
            out.append(p)
        return out

    # -- delivery -------------------------------------------------------------------
    async def dispatch(self) -> int:
        now = self.clock.now()
        sent = 0
        tz = self.settings.display_tz()
        for r in self.db.due_reminders(now):
            if r["kind"] == "digest":
                projects = self._digest_projects(now)
                if not projects:
                    self.db.cancel_reminder(r["id"], "nothing to report", now)
                    continue
                if await self._send(r, messages.digest(projects, tz, now), None, False):
                    sent += 1
                continue
            p = self.db.get_project(r["project_id"])
            if p is None:
                self.db.cancel_reminder(r["id"], "project deleted", now)
                continue
            status = Status(p["status"])
            if status in RESOLVED or status == Status.ENDED:
                self.db.cancel_reminder(r["id"], f"project {status.value}", now)
                continue
            snoozed = from_iso(p["snoozed_until"])
            if snoozed and snoozed > now:
                if r["kind"] == "repeat":
                    self.db.cancel_reminder(r["id"], "snoozed", now)
                continue  # one-shot reminders wait until the snooze ends
            due = from_iso(r["due_at"])
            if r["kind"].startswith("pre_"):
                launch = from_iso(p["launch_at"])
                if launch is None or launch <= now or now - due > self.grace:
                    self.db.cancel_reminder(r["id"], "stale (launch passed or delivery window missed)", now)
                    continue
            if r["kind"] == "repeat" and status != Status.LIVE:
                self.db.cancel_reminder(r["id"], "no longer live", now)
                continue
            if r["kind"] == "live" and status != Status.LIVE:
                continue  # wait for advance_statuses (or a reschedule) to confirm live
            if await self._send(r, messages.reminder(r["kind"], p, tz, now), p["id"], True):
                sent += 1
                if r["kind"] == "live":
                    self.db.update_project(p["id"], {"last_nag_at": to_iso(now)}, now)
        return sent

    async def _send(self, r, text: str, pid: int | None, buttons: bool) -> bool:
        try:
            await self.sender(text, pid, buttons)
        except Exception as exc:  # delivery failure keeps the reminder pending for the next tick
            self.db.mark_reminder_failed(r["id"], str(exc))
            log.warning("reminder_send_failed", extra={"reminder_id": r["id"], "error": str(exc)})
            return False
        self.db.mark_reminder_sent(r["id"], self.clock.now())
        log.info("reminder_sent", extra={"reminder_id": r["id"], "kind": r["kind"], "project_id": pid})
        return True

    async def tick(self) -> int:
        changed = self.advance_statuses()
        open_ids = [r["id"] for r in self.db.conn.execute(
            "SELECT id FROM projects WHERE status NOT IN ('minted','missed','dismissed','ended') "
            "OR id IN (SELECT DISTINCT project_id FROM reminders WHERE sent_at IS NULL AND cancelled_at IS NULL)")]
        for pid in set(open_ids) | set(changed):
            self.sync_project(pid)
        self.plan_recurring()
        sent = await self.dispatch()
        self.db.kv_set("last_scheduler_tick_at", to_iso(self.clock.now()))
        return sent
