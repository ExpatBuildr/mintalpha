"""SQLite storage. Single connection in WAL mode; all writes are short and
happen on the event-loop thread, which is fine at this scale and keeps the
code simple. Swap for Postgres later by reimplementing this class."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .clock import to_iso
from .models import Status, check_transition

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.migrate()

    # -- infrastructure ---------------------------------------------------
    def migrate(self) -> list[str]:
        self.conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT)")
        done = {r["name"] for r in self.conn.execute("SELECT name FROM schema_migrations")}
        applied = []
        for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if f.name in done:
                continue
            with self.tx():
                # executescript() would auto-commit; run statements one by one inside the tx
                for stmt in _split_sql(f.read_text()):
                    self.conn.execute(stmt)
                self.conn.execute("INSERT INTO schema_migrations VALUES (?, datetime('now'))", (f.name,))
            applied.append(f.name)
        return applied

    def tx(self):
        return _Tx(self.conn)

    def close(self) -> None:
        self.conn.close()

    def backup(self, dest: str | Path) -> None:
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(dest))
        try:
            self.conn.backup(target)
        finally:
            target.close()

    def ping(self) -> bool:
        return self.conn.execute("SELECT 1").fetchone()[0] == 1

    # -- kv / settings -------------------------------------------------------
    def kv_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, None if value is None else str(value)))

    def get_setting(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, value))

    def all_settings(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM settings")}

    # -- posts ---------------------------------------------------------------
    def post_exists(self, post_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM posts WHERE id=?", (post_id,)).fetchone() is not None

    def insert_post(self, post, project_id: int | None, relevant: bool, now: datetime, raw: dict | None = None) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO posts(id, project_id, author_username, text, url, created_at, fetched_at, relevant, raw)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (post.id, project_id, post.author_username, post.text, post.url, to_iso(post.created_at),
             to_iso(now), int(relevant), json.dumps(raw) if raw else None))
        return cur.rowcount == 1

    def posts_for_project(self, project_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM posts WHERE project_id=? ORDER BY created_at", (project_id,)).fetchall()

    # -- projects ------------------------------------------------------------
    def find_project_by_keys(self, keys: Iterable[str]) -> int | None:
        keys = list(keys)
        if not keys:
            return None
        q = f"SELECT project_id FROM project_keys WHERE key IN ({','.join('?' * len(keys))})"
        ids = {r["project_id"] for r in self.conn.execute(q, keys)}
        if not ids:
            return None
        return min(ids)  # oldest project wins if keys point at several

    def add_keys(self, project_id: int, keys: Iterable[str]) -> None:
        for key in keys:
            self.conn.execute("INSERT OR IGNORE INTO project_keys(key, project_id) VALUES(?,?)", (key, project_id))

    def keys_for(self, project_id: int) -> list[str]:
        return [r["key"] for r in self.conn.execute("SELECT key FROM project_keys WHERE project_id=?", (project_id,))]

    def create_project(self, fields: dict, now: datetime) -> int:
        fields = {**fields, "created_at": to_iso(now), "updated_at": to_iso(now)}
        fields.setdefault("status", Status.DISCOVERED.value)
        cols = ", ".join(fields)
        cur = self.conn.execute(f"INSERT INTO projects({cols}) VALUES ({','.join('?' * len(fields))})",
                                [_encode(v) for v in fields.values()])
        pid = cur.lastrowid
        self.conn.execute("INSERT INTO status_history(project_id, old_status, new_status, reason, at) VALUES (?,?,?,?,?)",
                          (pid, None, fields["status"], "created", to_iso(now)))
        return pid

    def get_project(self, project_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return _decode_project(row) if row else None

    def update_project(self, project_id: int, fields: dict, now: datetime) -> None:
        if not fields:
            return
        fields = {**fields, "updated_at": to_iso(now)}
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE projects SET {sets} WHERE id=?", [_encode(v) for v in fields.values()] + [project_id])

    def set_status(self, project_id: int, new: Status, reason: str, now: datetime, *, force: bool = False) -> bool:
        p = self.get_project(project_id)
        old = Status(p["status"])
        if old == new:
            return False
        if not force:
            check_transition(old, new)
        fields: dict[str, Any] = {"status": new.value}
        if new == Status.LIVE and not p["live_since"]:
            fields["live_since"] = to_iso(now)
        if new in (Status.MINTED, Status.MISSED, Status.DISMISSED):
            fields["resolved_at"] = to_iso(now)
        if old in (Status.MINTED, Status.MISSED, Status.DISMISSED):
            fields["resolved_at"] = None
        self.update_project(project_id, fields, now)
        self.conn.execute("INSERT INTO status_history(project_id, old_status, new_status, reason, at) VALUES (?,?,?,?,?)",
                          (project_id, old.value, new.value, reason, to_iso(now)))
        return True

    def status_history(self, project_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM status_history WHERE project_id=? ORDER BY id", (project_id,)).fetchall()

    def list_projects(self, statuses: Iterable[str] | None = None, q: str | None = None,
                      chain: str | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM projects WHERE 1=1"
        args: list[Any] = []
        if statuses:
            statuses = list(statuses)
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += statuses
        if chain:
            sql += " AND chain=?"
            args.append(chain)
        if q:
            sql += " AND (name LIKE ? OR x_handle LIKE ? OR website_url LIKE ? OR mint_url LIKE ?)"
            args += [f"%{q}%"] * 4
        sql += " ORDER BY COALESCE(launch_at, launch_date || 'T23:59:59+00:00', '9999') ASC, id DESC LIMIT ?"
        args.append(limit)
        return [_decode_project(r) for r in self.conn.execute(sql, args)]

    def count_by_status(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self.conn.execute("SELECT status, COUNT(*) n FROM projects GROUP BY status")}

    # -- evidence ------------------------------------------------------------
    def add_evidence(self, project_id: int, field: str, value: Any, source_url: str | None,
                     source_kind: str, snippet: str | None, now: datetime) -> None:
        if value is None or value == "" or value == []:
            return
        val = value if isinstance(value, str) else json.dumps(value, default=str)
        exists = self.conn.execute(
            "SELECT 1 FROM evidence WHERE project_id=? AND field=? AND value=? AND COALESCE(source_url,'')=COALESCE(?, '')",
            (project_id, field, val, source_url)).fetchone()
        if exists:
            return
        self.conn.execute(
            "INSERT INTO evidence(project_id, field, value, source_url, source_kind, snippet, observed_at) VALUES (?,?,?,?,?,?,?)",
            (project_id, field, val, source_url, source_kind, (snippet or "")[:500], to_iso(now)))

    def evidence_for(self, project_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM evidence WHERE project_id=? ORDER BY id", (project_id,)).fetchall()

    # -- alerts outbox ---------------------------------------------------------
    def enqueue_alert(self, project_id: int | None, kind: str, dedupe_key: str, message: str,
                      now: datetime, with_buttons: bool = True) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO alerts(project_id, kind, dedupe_key, message, with_buttons, created_at) VALUES (?,?,?,?,?,?)",
            (project_id, kind, dedupe_key, message, int(with_buttons), to_iso(now)))
        return cur.rowcount == 1

    def pending_alerts(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM alerts WHERE sent_at IS NULL ORDER BY id LIMIT ?", (limit,)).fetchall()

    def mark_alert_sent(self, alert_id: int, message_id: int | None, now: datetime) -> None:
        self.conn.execute("UPDATE alerts SET sent_at=?, telegram_message_id=?, attempts=attempts+1 WHERE id=?",
                          (to_iso(now), message_id, alert_id))

    def mark_alert_failed(self, alert_id: int, error: str) -> None:
        self.conn.execute("UPDATE alerts SET attempts=attempts+1, last_error=? WHERE id=?", (error[:500], alert_id))

    def alerts_for(self, project_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM alerts WHERE project_id=? ORDER BY id", (project_id,)).fetchall()

    # -- reminders -------------------------------------------------------------
    def insert_reminder(self, project_id: int | None, kind: str, due_at: datetime, dedupe_key: str, now: datetime) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO reminders(project_id, kind, due_at, dedupe_key, created_at) VALUES (?,?,?,?,?)",
            (project_id, kind, to_iso(due_at), dedupe_key, to_iso(now)))
        return cur.rowcount == 1

    def reschedule_reminder(self, dedupe_key: str, due_at: datetime) -> None:
        self.conn.execute("UPDATE reminders SET due_at=? WHERE dedupe_key=? AND sent_at IS NULL AND cancelled_at IS NULL",
                          (to_iso(due_at), dedupe_key))

    def pending_reminders_for(self, project_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reminders WHERE project_id=? AND sent_at IS NULL AND cancelled_at IS NULL ORDER BY due_at",
            (project_id,)).fetchall()

    def due_reminders(self, now: datetime, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reminders WHERE sent_at IS NULL AND cancelled_at IS NULL AND due_at <= ? ORDER BY due_at LIMIT ?",
            (to_iso(now), limit)).fetchall()

    def cancel_reminder(self, reminder_id: int, reason: str, now: datetime) -> None:
        self.conn.execute("UPDATE reminders SET cancelled_at=?, cancel_reason=? WHERE id=? AND sent_at IS NULL",
                          (to_iso(now), reason, reminder_id))

    def mark_reminder_sent(self, reminder_id: int, now: datetime) -> None:
        self.conn.execute("UPDATE reminders SET sent_at=?, attempts=attempts+1 WHERE id=?", (to_iso(now), reminder_id))

    def mark_reminder_failed(self, reminder_id: int, error: str) -> None:
        self.conn.execute("UPDATE reminders SET attempts=attempts+1, last_error=? WHERE id=?", (error[:500], reminder_id))

    def reminders_for(self, project_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM reminders WHERE project_id=? ORDER BY due_at, id", (project_id,)).fetchall()

    # -- query state & API usage -------------------------------------------------
    def get_query_state(self, query: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM query_state WHERE query=?", (query,)).fetchone()

    def set_query_state(self, query: str, since_id: str | None, now: datetime) -> None:
        self.conn.execute(
            "INSERT INTO query_state(query, since_id, last_polled_at) VALUES(?,?,?) "
            "ON CONFLICT(query) DO UPDATE SET since_id=COALESCE(excluded.since_id, query_state.since_id), last_polled_at=excluded.last_polled_at",
            (query, since_id, to_iso(now)))

    def record_usage(self, provider: str, day: str, reads: int = 0, requests: int = 0, errors: int = 0) -> None:
        self.conn.execute(
            "INSERT INTO api_usage(day, provider, reads, requests, errors) VALUES(?,?,?,?,?) "
            "ON CONFLICT(day, provider) DO UPDATE SET reads=reads+excluded.reads, requests=requests+excluded.requests, "
            "errors=errors+excluded.errors", (day, provider, reads, requests, errors))

    def usage(self, provider: str, day_prefix: str) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(reads),0) reads, COALESCE(SUM(requests),0) requests, COALESCE(SUM(errors),0) errors "
            "FROM api_usage WHERE provider=? AND day LIKE ?", (provider, f"{day_prefix}%")).fetchone()
        return dict(row)


class _Tx:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def _split_sql(script: str) -> list[str]:
    lines = [ln for ln in script.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def _encode(v: Any) -> Any:
    if isinstance(v, (list, dict)):
        return json.dumps(v)
    if isinstance(v, datetime):
        return to_iso(v)
    if isinstance(v, Status):
        return v.value
    return v


def _decode_project(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("eligibility", "risk_flags", "notes"):
        try:
            d[key] = json.loads(d.get(key) or "[]")
        except json.JSONDecodeError:
            d[key] = []
    return d
