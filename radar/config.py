"""Configuration: config.yaml (non-secret settings) + environment variables (secrets/modes)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no extra dependency). Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


@dataclass
class Env:
    x_source: str = "mock"            # mock | x_api
    telegram_mode: str = "mock"       # mock | live
    x_bearer_token: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    anthropic_api_key: str | None = None
    dashboard_token: str | None = None
    mobile_api_token: str | None = None
    db_path: str | None = None
    mock_data_path: str | None = None

    @classmethod
    def from_environ(cls, environ: dict | None = None) -> "Env":
        e = os.environ if environ is None else environ
        return cls(
            x_source=(e.get("X_SOURCE") or "mock").lower(),
            telegram_mode=(e.get("TELEGRAM_MODE") or "mock").lower(),
            x_bearer_token=e.get("X_BEARER_TOKEN") or None,
            telegram_bot_token=e.get("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=e.get("TELEGRAM_CHAT_ID") or None,
            anthropic_api_key=e.get("ANTHROPIC_API_KEY") or None,
            dashboard_token=e.get("DASHBOARD_TOKEN") or None,
            mobile_api_token=e.get("MOBILE_API_TOKEN") or None,
            db_path=e.get("RADAR_DB_PATH") or None,
            mock_data_path=e.get("MOCK_DATA_PATH") or None,
        )

    def problems(self) -> list[str]:
        """Missing credentials for the selected modes (empty list = OK)."""
        issues = []
        if self.x_source not in ("mock", "x_api"):
            issues.append("X_SOURCE must be 'mock' or 'x_api'")
        if self.telegram_mode not in ("mock", "live"):
            issues.append("TELEGRAM_MODE must be 'mock' or 'live'")
        if self.x_source == "x_api" and not self.x_bearer_token:
            issues.append("X_SOURCE=x_api requires X_BEARER_TOKEN (X developer app bearer token)")
        if self.telegram_mode == "live":
            if not self.telegram_bot_token:
                issues.append("TELEGRAM_MODE=live requires TELEGRAM_BOT_TOKEN (from @BotFather)")
            if not self.telegram_chat_id:
                issues.append("TELEGRAM_MODE=live requires TELEGRAM_CHAT_ID (your numeric chat id)")
        return issues


@dataclass
class Config:
    data: dict[str, Any]
    env: Env = field(default_factory=Env)
    root: Path = ROOT

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, dotted: str) -> Path:
        p = Path(self.get(dotted))
        return p if p.is_absolute() else self.root / p

    @property
    def db_path(self) -> Path:
        if self.env.db_path:
            return Path(self.env.db_path)
        return self.path("app.db_path")


def load_config(path: str | Path | None = None, environ: dict | None = None,
                load_env_file: bool = True) -> Config:
    if load_env_file and environ is None:
        _load_dotenv(ROOT / ".env")
    cfg_path = Path(path or os.environ.get("RADAR_CONFIG") or ROOT / "config.yaml")
    data = yaml.safe_load(cfg_path.read_text()) or {}
    env = Env.from_environ(environ)
    src = os.environ if environ is None else environ
    if src.get("DISPLAY_TIMEZONE"):
        data.setdefault("app", {})["display_timezone"] = src["DISPLAY_TIMEZONE"]
    if src.get("DASHBOARD_HOST"):
        data.setdefault("dashboard", {})["host"] = src["DASHBOARD_HOST"]
    if src.get("DASHBOARD_PORT"):
        data.setdefault("dashboard", {})["port"] = int(src["DASHBOARD_PORT"])
    return Config(data=data, env=env, root=cfg_path.resolve().parent)


# --- runtime settings (overridable from Telegram /set, persisted in DB) ------

RUNTIME_KEYS: dict[str, type] = {
    "discovery.poll_interval_seconds": int,
    "discovery.followup_interval_seconds": int,
    "discovery.daily_read_budget": int,
    "discovery.monthly_read_budget": int,
    "reminders.live_repeat_minutes": int,
    "reminders.date_unknown_digest_hours": int,
    "discovery.paused": bool,
}
MINIMUMS = {
    "discovery.poll_interval_seconds": 60,
    "discovery.followup_interval_seconds": 300,
    "reminders.live_repeat_minutes": 5,
    "reminders.date_unknown_digest_hours": 1,
    "discovery.daily_read_budget": 0,
    "discovery.monthly_read_budget": 0,
}


class Settings:
    """Config values with DB overrides for runtime-tunable keys and network toggles."""

    def __init__(self, config: Config, db):
        self.config = config
        self.db = db

    def get(self, dotted: str, default: Any = None) -> Any:
        raw = self.db.get_setting(dotted)
        if raw is not None:
            return json.loads(raw)
        if dotted == "discovery.paused":
            return False
        return self.config.get(dotted, default)

    def set(self, key: str, value: str) -> Any:
        key = key.strip()
        if key.startswith("network."):
            name = key.split(".", 1)[1]
            if name not in (self.config.get("networks") or {}):
                raise ValueError(f"unknown network '{name}'")
            parsed = _parse_bool(value)
            self.db.set_setting(f"networks.{name}.enabled", json.dumps(parsed))
            return parsed
        if key not in RUNTIME_KEYS:
            raise ValueError(f"'{key}' is not a runtime setting")
        typ = RUNTIME_KEYS[key]
        parsed = _parse_bool(value) if typ is bool else typ(value)
        if typ is int and parsed < MINIMUMS.get(key, 0):
            raise ValueError(f"{key} must be >= {MINIMUMS[key]}")
        self.db.set_setting(key, json.dumps(parsed))
        return parsed

    def network_enabled(self, name: str) -> bool:
        return bool(self.get(f"networks.{name}.enabled", False))

    def networks(self) -> dict[str, dict]:
        return self.config.get("networks") or {}

    def display_tz(self) -> str:
        return self.config.get("app.display_timezone", "UTC")


def _parse_bool(value: str) -> bool:
    v = str(value).strip().lower()
    if v in ("1", "true", "on", "yes", "enable", "enabled"):
        return True
    if v in ("0", "false", "off", "no", "disable", "disabled"):
        return False
    raise ValueError(f"expected on/off, got '{value}'")
