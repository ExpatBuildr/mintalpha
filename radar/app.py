"""Wires everything together and runs the always-on loops."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

import httpx

from .bot import COMMANDS, BotHandler, Notifier
from .classifier import LLMClassifier
from .clock import Clock, from_iso, to_iso
from .config import Config, Settings
from .db import Database
from .pipeline import Pipeline
from .reminders import ReminderEngine
from .retry import FatalError
from .sources import BudgetExhausted, MockXSource, XApiSource
from .telegram import MockTelegram, TelegramClient

log = logging.getLogger("radar.app")


class RadarApp:
    def __init__(self, config: Config, *, clock: Clock | None = None, db: Database | None = None,
                 source=None, telegram=None, http: httpx.AsyncClient | None = None, echo_mock: bool = False):
        self.config = config
        self.env = config.env
        self.clock = clock or Clock()
        self.db = db or Database(config.db_path)
        self.settings = Settings(config, self.db)
        self.http = http or httpx.AsyncClient(timeout=30)
        self.chat_id = self.env.telegram_chat_id or "mock-chat"

        if telegram is not None:
            self.telegram = telegram
        elif self.env.telegram_mode == "live":
            self.telegram = TelegramClient(self.env.telegram_bot_token, http=self.http)
        else:
            self.telegram = MockTelegram(echo=echo_mock)

        if source is not None:
            self.source = source
        elif self.env.x_source == "x_api":
            self.source = XApiSource(self.env.x_bearer_token, self.db, self.settings, self.clock, http=self.http)
        else:
            path = self.env.mock_data_path or (config.root / "sample_data" / "mock_posts.json")
            self.source = MockXSource(path, self.clock)

        classifier = None
        if config.get("classifier.use_llm") and self.env.anthropic_api_key:
            classifier = LLMClassifier(self.env.anthropic_api_key, config.get("classifier.model"),
                                       list(self.settings.networks()), int(config.get("classifier.max_tokens", 400)),
                                       http=self.http)
        self.notifier = Notifier(self.db, self.telegram, self.chat_id, self.clock)
        self.engine = ReminderEngine(self.db, self.settings, self.clock, self.notifier.send)
        verify_http = self.http if self.env.x_source == "x_api" else None  # demo/mock mode stays offline
        self.pipeline = Pipeline(self.db, self.settings, self.clock, classifier=classifier, http=verify_http,
                                 on_project_changed=self.engine.sync_project)
        self.bot = BotHandler(self.db, self.settings, self.clock, self.telegram, self.chat_id, self.engine,
                              self.health, self.pipeline.fee_note, self.pipeline.chain_label)
        self._stop = asyncio.Event()
        self._fatal_x: str | None = None

    # -- single steps (also used by tests and the demo) ---------------------------
    async def discovery_once(self) -> dict:
        counts: dict[str, int] = {}
        if self.settings.get("discovery.paused"):
            return {"paused": 1}
        try:
            posts = await self.source.poll()
        except BudgetExhausted as exc:
            self.db.kv_set("last_discovery_error", str(exc))
            log.warning("budget_exhausted", extra={"error": str(exc)})
            return {"budget_exhausted": 1}
        for post, raw in posts:
            try:
                res = await self.pipeline.ingest(post, raw)
                counts[res.outcome] = counts.get(res.outcome, 0) + 1
            except Exception:
                log.exception("ingest_failed", extra={"post_id": post.id})
                counts["error"] = counts.get("error", 0) + 1
        self.db.kv_set("last_discovery_ok_at", to_iso(self.clock.now()))
        self.db.kv_set("last_discovery_error", None)
        await self.notifier.flush_outbox()
        return counts

    async def followup_once(self) -> dict:
        if not self.config.get("discovery.follow_project_accounts", True) or self.settings.get("discovery.paused"):
            return {}
        handles = [p["x_handle"] for p in self.db.list_projects(statuses=["upcoming", "live", "date_unknown", "discovered"], limit=500)
                   if p["x_handle"]]
        if not handles:
            return {}
        since = from_iso(self.db.kv_get("last_followup_at")) or self.clock.now() - timedelta(hours=1)
        try:
            posts = await self.source.poll_followups(handles, since)
        except BudgetExhausted:
            return {"budget_exhausted": 1}
        counts: dict[str, int] = {}
        for post, raw in posts:
            res = await self.pipeline.ingest(post, raw)
            counts[res.outcome] = counts.get(res.outcome, 0) + 1
        self.db.kv_set("last_followup_at", to_iso(self.clock.now()))
        await self.notifier.flush_outbox()
        return counts

    async def scheduler_once(self) -> int:
        sent = await self.engine.tick()
        await self.notifier.flush_outbox()
        return sent

    def backup_once(self) -> Path | None:
        if self.db.path == ":memory:":
            return None
        backup_dir = self.config.path("app.backup_dir")
        stamp = self.clock.now().strftime("%Y%m%d-%H%M%S")
        dest = backup_dir / f"radar-{stamp}.db"
        self.db.backup(dest)
        keep = int(self.config.get("app.backup_keep", 7))
        for old in sorted(backup_dir.glob("radar-*.db"))[:-keep]:
            old.unlink(missing_ok=True)
        self.db.kv_set("last_backup_at", to_iso(self.clock.now()))
        log.info("backup_written", extra={"path": str(dest)})
        return dest

    def health(self) -> dict:
        now = self.clock.now()
        poll = int(self.settings.get("discovery.poll_interval_seconds", 300))
        last_disc = from_iso(self.db.kv_get("last_discovery_ok_at"))
        last_tick = from_iso(self.db.kv_get("last_scheduler_tick_at"))
        paused = bool(self.settings.get("discovery.paused"))
        disc_ok = paused or (last_disc is not None and now - last_disc < timedelta(seconds=poll * 3 + 120))
        tick_ok = last_tick is not None and now - last_tick < timedelta(minutes=5)
        reads_today = self.db.usage(self.source.name, now.strftime("%Y-%m-%d"))["reads"]
        reads_month = self.db.usage(self.source.name, now.strftime("%Y-%m"))["reads"]
        cost = float(self.config.get("discovery.cost_per_read_usd", 0.005))
        pending = self.db.conn.execute("SELECT COUNT(*) FROM reminders WHERE sent_at IS NULL AND cancelled_at IS NULL").fetchone()[0]
        unsent = self.db.conn.execute("SELECT COUNT(*) FROM alerts WHERE sent_at IS NULL").fetchone()[0]
        return {
            "healthy": bool(self.db.ping() and disc_ok and tick_ok and not self._fatal_x),
            "db": "ok", "x_source": self.env.x_source, "telegram_mode": self.env.telegram_mode, "paused": paused,
            "last_discovery_ok_at": to_iso(last_disc), "last_discovery_error": self._fatal_x or self.db.kv_get("last_discovery_error"),
            "last_scheduler_tick_at": to_iso(last_tick), "last_backup_at": self.db.kv_get("last_backup_at"),
            "pending_reminders": pending, "unsent_alerts": unsent,
            "usage": {"reads_today": reads_today, "reads_month": reads_month,
                      "daily_budget": self.settings.get("discovery.daily_read_budget"),
                      "monthly_budget": self.settings.get("discovery.monthly_read_budget"),
                      "est_cost_month": round(reads_month * cost, 2) if self.env.x_source == "x_api" else 0.0},
        }

    # -- loops -----------------------------------------------------------------------
    async def _loop(self, name: str, fn, interval_fn) -> None:
        while not self._stop.is_set():
            try:
                await fn()
            except FatalError as exc:
                self._fatal_x = str(exc)
                log.error("fatal_error", extra={"loop": name, "error": str(exc)})
                key = f"fatal:{name}:{str(exc)[:80]}"
                self.db.enqueue_alert(None, "system", key, f"🛑 {name} stopped retrying: {exc}\n"
                                      "Fix credentials/credits, then restart the service.", self.clock.now(), False)
                await self.notifier.flush_outbox()
                await self._sleep(3600)
                continue
            except Exception as exc:
                self.db.kv_set("last_discovery_error" if name != "scheduler" else "last_scheduler_error", str(exc))
                log.exception("loop_error", extra={"loop": name})
            await self._sleep(interval_fn())

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _telegram_loop(self) -> None:
        offset = int(self.db.kv_get("telegram_offset") or 0) or None
        timeout = int(self.config.get("telegram.poll_timeout_seconds", 30))
        while not self._stop.is_set():
            try:
                updates = await self.telegram.get_updates(offset, timeout)
                for upd in updates:
                    offset = upd.get("update_id", 0) + 1 if "update_id" in upd else offset
                    try:
                        await self.bot.handle_update(upd)
                    except Exception:
                        log.exception("update_failed")
                    if offset:
                        self.db.kv_set("telegram_offset", offset)
                self.db.kv_set("last_telegram_poll_at", to_iso(self.clock.now()))
            except FatalError as exc:
                log.error("telegram_fatal", extra={"error": str(exc)})
                await self._sleep(300)
            except Exception:
                log.exception("telegram_poll_error")
                await self._sleep(5)

    async def run(self) -> None:
        problems = self.env.problems()
        if problems:
            raise SystemExit("Configuration problems:\n- " + "\n- ".join(problems))
        log.info("starting", extra={"x_source": self.env.x_source, "telegram_mode": self.env.telegram_mode,
                                    "db": str(self.db.path)})
        try:
            await self.telegram.set_commands(COMMANDS)
        except Exception as exc:
            log.warning("set_commands_failed", extra={"error": str(exc)})
        await self.notifier.flush_outbox()  # recover alerts left unsent before a restart
        tasks = [
            asyncio.create_task(self._loop("discovery", self.discovery_once,
                                           lambda: int(self.settings.get("discovery.poll_interval_seconds", 300)))),
            asyncio.create_task(self._loop("followup", self.followup_once,
                                           lambda: int(self.settings.get("discovery.followup_interval_seconds", 1800)))),
            asyncio.create_task(self._loop("scheduler", self.scheduler_once,
                                           lambda: int(self.config.get("reminders.tick_seconds", 30)))),
            asyncio.create_task(self._loop("backup", self._backup_async,
                                           lambda: float(self.config.get("app.backup_interval_hours", 24)) * 3600)),
            asyncio.create_task(self._telegram_loop()),
        ]
        runner = None
        if self.config.get("dashboard.enabled", True):
            from aiohttp import web
            from .dashboard import make_app
            runner = web.AppRunner(make_app(self))
            await runner.setup()
            host, port = self.config.get("dashboard.host", "127.0.0.1"), int(self.config.get("dashboard.port", 8080))
            await web.TCPSite(runner, host, port).start()
            log.info("dashboard_listening", extra={"url": f"http://{host}:{port}"})
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if runner:
                await runner.cleanup()
            await self.http.aclose()
            self.db.close()
            log.info("stopped")

    async def _backup_async(self) -> None:
        # SQLite's online backup is fast for a DB this size; run it on the loop thread
        # so the single connection is never used from two threads at once.
        self.backup_once()

    def stop(self) -> None:
        self._stop.set()
