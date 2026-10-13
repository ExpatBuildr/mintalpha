# Project guide for AI coding assistants

Free Mint Radar watches X for free NFT mint announcements, tracks each project in SQLite, alerts on
Telegram, and keeps reminding until the user resolves it. **MintAlpha** is the Android app for it.

## Layout

- `radar/` — Python 3.11+ asyncio service (`python -m radar run`). Entry: `radar/app.py`.
  - `sources.py` X API v2 recent search (paid per read; budget caps in config) + mock source
  - `extract.py`, `dates.py`, `security.py` rule-based extraction, schedule parsing, risk flags
  - `pipeline.py` ingest → dedupe/merge → evidence → alert outbox; `reminders.py` DB-driven reminders
  - `bot.py`/`telegram.py` Telegram; `dashboard.py` aiohttp dashboard; `api.py` **mobile API `/api/v1`** + serves `/app/`
  - `db.py` + `migrations/*.sql` (add a new numbered file for schema changes; never edit 001)
- `radar/webapp/` — **the MintAlpha app UI** (plain HTML/CSS/JS, no bundler). `app.js` = UI + live store + phone
  notifications; `demo.js` = sample data store with the same interface.
- `mobile/` — Capacitor 8 wrapper. `mobile/www` and `mobile/android/app/src/main/assets/public` are
  **generated** by `npm run sync` from `radar/webapp`; don't edit them.
- `tests/` — pytest (asyncio auto mode). `sample_data/mock_posts.json` — mock X posts.

## Commands

```bash
pip install -r requirements-dev.txt
python -m pytest -q                 # all tests must pass
python -m radar demo                # offline simulated run
python -m radar run                 # service (mock mode by default; dashboard 127.0.0.1:8080, app at /app/)
cd radar/webapp && python3 -m http.server 9000    # iterate on the app UI in a browser (demo mode)
cd mobile && npm install && npm run sync && npm run open   # Android Studio
cd mobile && npm run build:debug    # APK via Gradle (needs Android SDK + JDK 21)
```

Modes come from `.env` (`X_SOURCE=mock|x_api`, `TELEGRAM_MODE=mock|live`, `MOBILE_API_TOKEN`). Never
commit `.env`, keystores or tokens.

## Rules that matter

- Never add anything that signs transactions, connects wallets, handles seed phrases, or auto-mints.
- Never invent mint URLs or launch times; only exact date+time+zone gets timed reminders.
- Keep "free" claims graded (claimed → project-claimed → confirmed); gas/fees are shown separately.
- X API reads cost money: don't increase default polling or `max_results` without updating budget docs.
- App UI: every color is a CSS token in `styles.css` (light + dark); keep the phone layout working at 360px.
- API changes: update `radar/api.py`, `tests/test_api.py`, and the mapping in `app.js` (`norm()`).
- After changing `radar/webapp`, run `cd mobile && npm run sync` before building Android.
