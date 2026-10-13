"""CLI: python -m radar <command>

  run             start the bot (modes from .env: X_SOURCE, TELEGRAM_MODE)
  demo            offline end-to-end demo with a simulated clock
  check           validate configuration and print what is missing
  test-telegram   send one test message to TELEGRAM_CHAT_ID (needs TELEGRAM_BOT_TOKEN)
  check-x         one small X API request to confirm access (costs up to ~25 reads)
  backup          write a database backup now
"""
from __future__ import annotations

import asyncio
import signal
import sys

from .config import load_config
from .logs import setup_logging


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "run"
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if cmd == "demo":
        from .demo import main as demo_main
        demo_main()
        return 0

    config = load_config()
    setup_logging(config.get("app.log_level", "INFO"), json_output=cmd == "run")
    if cmd == "check":
        problems = config.env.problems()
        print(f"X_SOURCE={config.env.x_source}  TELEGRAM_MODE={config.env.telegram_mode}  DB={config.db_path}")
        print("OK" if not problems else "Problems:\n- " + "\n- ".join(problems))
        return 0 if not problems else 1

    from .app import RadarApp
    app = RadarApp(config, echo_mock=True)

    if cmd == "test-telegram":
        if config.env.telegram_mode != "live":
            print("Set TELEGRAM_MODE=live plus TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.")
            return 1
        async def go():
            me = await app.telegram.get_me()
            await app.telegram.send_message(app.chat_id, "✅ Free Mint Radar test message — alerts will arrive here.")
            print(f"Sent via @{me.get('username')}")
        asyncio.run(go())
        return 0
    if cmd == "check-x":
        if config.env.x_source != "x_api":
            print("Set X_SOURCE=x_api and X_BEARER_TOKEN first.")
            return 1
        print(asyncio.run(app.source.check_access()))
        return 0
    if cmd == "backup":
        print(app.backup_once())
        return 0
    if cmd == "run":
        async def go():
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, app.stop)
                except NotImplementedError:
                    pass
            await app.run()
        asyncio.run(go())
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
