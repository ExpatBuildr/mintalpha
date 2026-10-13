# Free Mint Radar + MintAlpha

Watches X for free NFT mint announcements, tracks each project in a database, alerts you on Telegram, and keeps reminding you until you tap **Minted**, **Missed** or **Dismiss**.

It never touches wallets, keys or transactions. It does not mint for you.

**MintAlpha**, the Android app, lives in `mobile/` and `radar/webapp/` — see **[MOBILE.md](MOBILE.md)** for building it in Android Studio, getting an APK from GitHub Actions, and connecting it to this server.

```
X API (recent search) ──► extract ──► dedupe/merge ──► verify ──► SQLite ◄──► dashboard (aiohttp)
   or mock source          rules (+opt. LLM)  keys      official site      │
                                                                           ├─► alert outbox ──► Telegram
                                                    reminder engine (DB rows, 30s tick) ─┘      ▲  │
                                                                                Telegram buttons/commands
```

## What works today (verified)

- 83 automated tests pass (`pytest`), covering the mobile API, duplicate detection, date/time-zone/DST parsing, reminder scheduling, status transitions, restart recovery, Telegram buttons/commands, X/Telegram API failures, budget enforcement, dashboard, LLM fallback and site verification.
- Full offline demo with a simulated clock: `python -m radar demo`.
- The real service starts in mock mode, serves the dashboard, writes backups, and shuts down cleanly.

**Not yet verified:** live X API calls and live Telegram delivery — no credentials were available when this was built. The code paths are tested against recorded-style HTTP responses. Run `python -m radar check-x` and `python -m radar test-telegram` (below) to confirm on your side.

## Quick start (mock mode — free, no accounts)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest            # run the test suite
python -m radar demo        # 2 simulated days in a few seconds: alerts, reminders, buttons, restart
python -m radar run         # real service with sample data; dashboard at http://127.0.0.1:8080
```

In mock mode "Telegram" messages print to the console, and sample posts come from `sample_data/mock_posts.json` (one wave per poll).

## Accounts you need for live mode

| What | Why | Cost |
|---|---|---|
| Telegram bot (via @BotFather) | Alerts, reminders, buttons | Free |
| X developer account + app with API credits | Search posts (official API v2 `GET /2/tweets/search/recent`) | Pay per post read — see costs |
| A small Linux VPS (1 vCPU, 1 GB RAM is plenty) | Always-on process | Hosting |
| Optional: Anthropic API key | LLM fills missing name/chain/supply | Small per-call cost |

## Operating cost (realistic)

Hosting and data are separate:

- **Hosting:** a small VPS is typically about $4–6/month. The bot uses ~60 MB RAM.
- **X API:** as of a March 2026 summary of X's pricing, new developers are on **pay-per-use at about $0.005 per post read** (the older Basic/Pro plans are closed to new signups). Every post a search returns counts, even ones the radar discards as irrelevant. Check your developer console — X's billing is the source of truth, and the radar's counter is an estimate.

| Daily read budget | ≈ per month | What you get |
|---|---|---|
| 100 | ~$15 | ~10 newest matching posts every ~2.4 h |
| **300 (default)** | **~$45** | ~10 newest posts every ~48 min, plus follow-ups |
| 1,000 | ~$150 | ~10 posts every ~15 min |

The default config enforces hard daily and monthly caps (`/status` shows usage), spreads the daily budget across the day, uses `since_id` so posts are never paid for twice, and never backfills more than 30 minutes on a new query. **"Free mint" is a high-volume, spam-heavy phrase: at any affordable budget you get a sample of the newest posts, not complete coverage.** Following known project accounts (`from:` queries) is cheap and high-signal; that runs every 30 minutes for tracked projects.

- **LLM (optional, off by default):** one short call per relevant post. At default volumes this is low single-digit dollars per month; check current model pricing.

Third-party X data resellers can be cheaper, but they're outside X's terms in many cases. Only the official API is implemented. The source is a small class (`radar/sources.py`), so adding a compliant provider is contained.

## Setup for live mode

### 1. Telegram

1. In Telegram, open **@BotFather** → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Find your chat id: set `TELEGRAM_MODE=live`, `TELEGRAM_CHAT_ID=0`, run `python -m radar run`, and send `/start` to your bot. It replies with your chat id because you're not yet authorized. Put that number in `TELEGRAM_CHAT_ID` and restart.
   (Alternative: send the bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `message.chat.id`.)
3. Confirm: `python -m radar test-telegram` sends one test message.

Only `TELEGRAM_CHAT_ID` can use the bot; everyone else is ignored.

### 2. X API access

1. Sign up at the X developer portal (developer.x.com), create a Project and App.
2. Copy the app's **Bearer Token** into `X_BEARER_TOKEN`; set `X_SOURCE=x_api`.
3. Add credits / a payment method in the developer console (pay-per-use). Without credits, requests fail with 402/403 and the bot tells you on Telegram.
4. Confirm: `python -m radar check-x` makes one small search (at most ~10 reads, about $0.05).

The X developer console flow changes often. If a step doesn't match, follow X's current docs; the radar only needs a bearer token with recent-search access.

### 3. Configure

```bash
cp .env.example .env    # secrets + modes
nano config.yaml        # phrases, chains, budgets, reminder timing, display time zone
python -m radar check   # lists anything missing
```

Key settings (most can also be changed live from Telegram with `/set`):

| Setting | Default | Notes |
|---|---|---|
| `discovery.poll_interval_seconds` | 900 | Discovery search cadence |
| `discovery.daily_read_budget` / `monthly_read_budget` | 300 / 9000 | Hard caps on paid reads |
| `discovery.search_phrases` | free mint, 0 eth mint, … | Packed into ≤512-char OR queries |
| `discovery.aggregator_accounts` | [] | Accounts that post about many projects |
| `networks.<chain>.enabled` | ETH, Base, Polygon, Solana, Ordinals on | `/set network.solana off` |
| `reminders.before_launch_minutes` | [1440, 60] | 24h and 1h before an exact launch |
| `reminders.live_repeat_minutes` | 30 | Nag interval while live and unresolved |
| `reminders.date_unknown_digest_hours` | 12 | One digest for all projects without an exact time |
| `app.display_timezone` | Asia/Manila | Alerts show UTC plus this zone |

## Deploy to a VPS

### Option A — Docker Compose (recommended)

```bash
git clone <your repo> /opt/freemint-radar && cd /opt/freemint-radar   # or scp the folder
cp .env.example .env && nano .env
mkdir -p data && sudo chown 10001:10001 data     # container runs as uid 10001
docker compose up -d --build
docker compose logs -f                           # JSON logs
curl -s 127.0.0.1:8080/healthz
```

`restart: unless-stopped` restarts it after crashes and reboots. The database and backups live in `./data`.

### Option B — systemd (no Docker)

```bash
sudo useradd --system --create-home radar
sudo mkdir -p /opt/freemint-radar && sudo cp -r . /opt/freemint-radar && cd /opt/freemint-radar
sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env
sudo mkdir -p data && sudo chown -R radar:radar /opt/freemint-radar
sudo cp deploy/freemint-radar.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now freemint-radar
journalctl -u freemint-radar -f
```

### Reaching the dashboard

It listens on `127.0.0.1:8080` only. From your laptop: `ssh -L 8080:127.0.0.1:8080 you@your-vps`, then open http://localhost:8080. If you expose it through a reverse proxy, set `DASHBOARD_TOKEN` (HTTP Basic auth; any username, token as password) and use HTTPS.

### Verify alerts end to end

1. `python -m radar test-telegram` → you get a test message.
2. Before paying for X: run with `X_SOURCE=mock` and `TELEGRAM_MODE=live`. You get real Telegram alerts from the sample data and can tap the buttons.
3. Switch to `X_SOURCE=x_api`, run `python -m radar check-x`, start the service, then send `/status` in Telegram. "Last discovery" and "X reads today" should move within one poll interval.

## Using it

Each alert has buttons: **✅ Minted · ❌ Missed · 🙈 Dismiss · 😴 Snooze (1h/4h/24h) · 🔎 Details**.

Commands: `/start /help /active /upcoming /live /details <id> /status /settings /set <key> <value> /minted <id> /missed <id> /dismiss <id> /snooze <id> <hours> /reopen <id> /pause /resume`.

Reminder rules:
- **Exact launch known** (date + time + stated zone, or "in 2 hours"): reminders at T-24h and T-1h, plus one when it goes live.
- **Live and unresolved:** a reminder every 30 minutes (configurable) until you resolve or snooze it. Projects with high-risk warnings get the live alert once, then no repeats.
- **No exact time:** included in a digest every 12 hours, not separate messages per project.
- **After downtime:** stale "24h/1h before" reminders are cancelled instead of sent late; live nags resume with one message, not a backlog.

## How decisions are made

**Dedupe and merge.** Each project gets identity keys: `handle:`, `domain:`, `mint:` (link) and `name:` (normalized). A post that matches any key merges into the existing project. Duplicate post IDs are ignored. Alerts and reminders have unique dedupe keys, so repeats can't cause message storms.

**Facts vs claims.** Every value keeps evidence (source URL, snippet, source kind). Price status moves through levels:
`claimed_free` (third party) → `project_claimed_free` (project's own account) → `confirmed_free` (official site or mint page shows it). A non-zero price on the official page sets `paid` and adds a warning. Gas and fees are always shown separately with per-chain notes. "Free" on Ethereum, Solana and Ordinals means different total costs.

**Schedules.** Only date + time + an explicit zone counts as exact. A time without a zone is never guessed and stays "day" precision with no timed reminders. Abbreviations map to IANA zones, so DST is handled correctly (e.g., "3pm ET" on Oct 30 vs Nov 2, 2026). "EST" used in summer is read as US Eastern local time, and that assumption is recorded. The project's own account can move a date; third parties can't override it, but their claims are noted.

**Security flags** (risk reduction, not a guarantee): shortened links, unexpanded t.co, punycode, raw IPs, scam-heavy TLDs, brand look-alikes (`opensea-claim.top`, `0pensea.io`), drainer wording (seed phrase, "validate wallet", approval/permit signatures, "send X to receive"), urgency language, low-follower and impersonating accounts, and a non-zero price next to a "free" claim.

**LLM (optional).** It can mark a post relevant and fill a missing name, chain, supply or eligibility. It never supplies dates, mint links or websites, so nothing is invented. Any failure falls back to rules.

## Operations

- **Logs:** structured JSON to stdout (journald or Docker).
- **Health:** `GET /healthz` (503 when degraded) and `/status` in Telegram. Health covers last discovery, scheduler heartbeat, X usage and cost estimate, and pending or unsent counts.
- **Retries:** exponential backoff with jitter; X `429` waits for the rate-limit reset; Telegram `retry_after` is honored. Bad credentials (401/403/402) stop retrying and alert you once.
- **Backups:** SQLite online backup every 24h (and at start) to `data/backups/`, keeping 7. Manual: `python -m radar backup`. Restore by stopping the service and copying a backup over `data/radar.db`.
- **Restart safety:** projects, reminders, alert outbox, Telegram update offset and X `since_id` all live in SQLite. Unsent alerts are re-sent on start.
- **Delivery semantics:** at-least-once. A crash in the instant between Telegram accepting a message and the DB recording it can repeat that one message. This was chosen over risking a lost reminder.

## Tradeoffs and known limits

- **Coverage vs cost** (above) is the main constraint. Tune `search_phrases`. Adding `has:links` to `query_suffix` cuts noise but drops link-less announcements.
- **Statuses:** verification is a separate field (`unverified`, `project_account`, `site_crosslinked`, `site_confirms_free`) rather than an "unverified" lifecycle status, so a project can be both "upcoming" and "unverified".
- **Name and handle extraction is heuristic.** Odd phrasing can produce a handle as the name; later posts upgrade it. Two unrelated projects with the same normalized name will merge. Rare, and visible in Details.
- **"Mint is live" from a third party** marks the project live, since speed matters more than certainty there. The warning badges still apply.
- **Site verification** can't run JavaScript. Many mint pages render client-side, so "inconclusive" is common and is reported as such.
- **SQLite** is right for one user on one VPS. The `Database` class isolates SQL if you ever move to Postgres.
- **Docker build** wasn't run in the build environment (no Docker daemon there). The Dockerfile is standard; run `docker compose up --build` once to confirm.

## Project layout

```
radar/
  app.py          loops, health, backups, wiring
  sources.py      X API v2 recent search (budget, since_id, retries) + mock source
  extract.py      rule-based extraction, link classification, chain detection, confidence
  dates.py        schedule parsing (zones, DST, relative dates)
  security.py     risk flags
  classifier.py   optional LLM gap-filler
  verify.py       official-site fetch (size-capped, no JS, private IPs blocked)
  pipeline.py     ingest → dedupe/merge → evidence → alerts
  reminders.py    DB-driven reminder engine
  bot.py          Telegram commands/buttons, alert outbox
  telegram.py     Bot API client + mock
  dashboard.py    search, filters, calendar, actions, /healthz, /api/projects
  api.py          MintAlpha mobile API (/api/v1, bearer token) + installable app at /app/
  webapp/         MintAlpha app UI (shared by the Android app and /app/)
  db.py, migrations/001_init.sql
tests/            83 tests
sample_data/      mock posts
deploy/           systemd unit, Caddyfile (HTTPS for the phone app)
mobile/           MintAlpha Android project (Capacitor 8)
```
