# MintAlpha — the Android app

MintAlpha is the phone app for Free Mint Radar. It shows the mints the radar tracks, counts down to launches, sends phone notifications (24h before, 1h before, when live, then every 30 minutes until you act), and gives you **Minted / Missed / Dismiss / Snooze** buttons that update the server.

It runs in two modes:

| Mode | Data | Needs |
|---|---|---|
| **Demo** (default on first open) | Sample projects, a demo clock you can skip ahead | Nothing |
| **Live** | Your radar server, over HTTPS | Server address + `MOBILE_API_TOKEN` |

The same app code (`radar/webapp/`) runs in three places: the Android app, the installable web app the radar server serves at `/app/`, and any browser.

```
Phone (MintAlpha) ──HTTPS + Bearer token──► Caddy/Tailscale ──► radar server :8080
   │  /api/v1/projects, /actions, /feed, /settings                 │
   └─ local notifications scheduled on the phone from the          ├─ X API discovery
      server's launch times (work with the app closed)             └─ Telegram alerts (unchanged)
```

---

## Path A — build the APK in Android Studio

### Prerequisites (one time)

- **Android Studio** (current stable). It bundles the JDK 21 that Capacitor 8 needs.
- **Node.js 20+** (`node -v`).
- An Android phone with **Developer options → USB debugging** on (Settings → About phone → tap *Build number* 7 times), or an emulator.

### Steps

```bash
cd mobile
npm install            # REQUIRED before opening in Android Studio: the Gradle project references node_modules
npm run sync           # copies radar/webapp -> mobile/www -> android assets
npm run open           # opens mobile/android in Android Studio (or File → Open → mobile/android)
```

In Android Studio:

1. Wait for **Gradle sync** to finish (first time downloads the Android SDK pieces; a few minutes).
2. Plug in your phone, pick it in the device dropdown, press **Run ▶**.
3. MintAlpha installs and opens: system splash → animated intro → demo data.
4. Android asks to allow notifications → **Allow**.

To get a file you can send to your phone instead: **Build → Build App Bundle(s) / APK(s) → Build APK(s)**. The APK lands in `mobile/android/app/build/outputs/apk/debug/app-debug.apk`. Copy it to the phone and open it (allow "install unknown apps" for your file manager once).

Command-line equivalent: `npm run build:debug`.

### Every time you change the app UI

Edit files in **`radar/webapp/`** (never in `mobile/www` or `android/.../assets` — those are generated), then:

```bash
cd mobile && npm run sync
```

and press Run again. To iterate fast in a browser first: `cd radar/webapp && python3 -m http.server 9000` and open http://localhost:9000.

## Path B — let GitHub build the APK (no Android Studio)

1. Push this repository to GitHub.
2. The **build** workflow (`.github/workflows/build.yml`) runs the server tests and builds a debug APK.
3. Open the workflow run → **Artifacts** → download `mintalpha-debug-apk` → unzip → install `app-debug.apk` on the phone.

## Path C — no build at all (installable web app)

Once the server is reachable over HTTPS (below), open `https://<your-radar-domain>/app/` in Chrome on Android → ⋮ → **Install app**. You get the MintAlpha icon, splash and full-screen app, with live data. The only difference from the APK: no phone notifications when the app is closed (Telegram still alerts you).

---

## Connect the app to your radar server

### 1. Turn on the mobile API (server)

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # copy the output
```

Put it in the server's `.env`:

```
MOBILE_API_TOKEN=<the value you just generated>
```

Restart the radar (`docker compose up -d` or `sudo systemctl restart freemint-radar`). Check: `curl -s 127.0.0.1:8080/api/v1/ping` shows `"auth_configured": true`.

### 2. Make the server reachable from your phone over HTTPS

The app refuses plain `http://` to the internet (Android blocks it, and your token would travel unencrypted). Pick one:

**Option 1 — Tailscale (simplest, private, no domain).** Install Tailscale on the VPS and on your phone, same account. On the VPS:

```bash
sudo tailscale serve --bg 8080
tailscale serve status        # shows https://<vps-name>.<tailnet>.ts.net
```

If Tailscale says HTTPS isn't enabled, turn on MagicDNS and HTTPS certificates in the Tailscale admin console (DNS page) and run the command again. Use that `https://…ts.net` address in the app, with Tailscale switched on on the phone. Only your devices can reach it.

**Option 2 — your own domain + Caddy (public HTTPS).** Point `radar.yourdomain.com` at the VPS, install Caddy, use `deploy/Caddyfile`. Set `DASHBOARD_TOKEN` too if the dashboard becomes public. Use `https://radar.yourdomain.com` in the app.

### 3. Connect in the app

Settings → **Connection** → server address + access token → **Connect**. The app checks the address is a radar server and that the token works, then switches to live data. "Use demo data instead" switches back; your connection details stay saved.

---

## What the app does in live mode

- Pulls projects and alerts every 30 seconds while open, and on every return to the app.
- **Minted / Missed / Dismiss / Snooze / Reopen** call the server, which stops or changes the Telegram reminders too.
- Settings for reminder interval, chains on/off and pausing X polling are saved **on the server** (same as Telegram `/set`).
- **Phone notifications**: after each sync the app schedules the next 24h, 1h, live and repeat reminders on the phone itself (up to ~6 hours of repeats ahead), so they arrive even if the app is closed. Resolving or snoozing re-plans them. High-risk projects never get repeats. Turn this off in Settings if Telegram is enough.
- Links on projects with security warnings are not clickable (copy only).

### Notification timing on Android 14+

New installs on Android 14+ don't get the "exact alarm" permission by default, so a reminder may arrive a few minutes late. For exact timing: Android Settings → Apps → MintAlpha → **Alarms & reminders** → allow. Also exclude MintAlpha from battery optimisation on phones that aggressively kill background apps (Xiaomi, Oppo, Samsung "Deep sleep").

## Troubleshooting

| Symptom | Fix |
|---|---|
| Gradle sync: `Could not find :capacitor-android` | You skipped `npm install` in `mobile/`. |
| Gradle: "requires Java 21" | Android Studio → Settings → Build Tools → Gradle → Gradle JDK → the bundled JBR 21. |
| App: "No radar server found at that address" | Check the address in the phone's browser: `https://…/api/v1/ping` must show JSON. |
| App: "Mobile API is off" | `MOBILE_API_TOKEN` missing on the server, or the server wasn't restarted. |
| App: "Wrong or missing access token" | Re-paste the token; no spaces or quotes. |
| Testing on home Wi-Fi with `http://192.168.x.x:8080` | Blocked by design. Use Tailscale (Option 1). For a throwaway test only: in `capacitor.config.json` add `"server": {"androidScheme": "http", "cleartext": true}`, `npm run sync`, rebuild, and remove it afterwards. |
| No notifications | Android Settings → Apps → MintAlpha → Notifications → allow; check the app's Settings → Phone notifications is on. |

## Release build (optional, for keeping the app long-term)

Debug APKs are fine for your own phone. For a signed release (e.g. Play Store internal testing): Android Studio → **Build → Generate Signed App Bundle / APK** → create a keystore (store it and its passwords somewhere safe; losing it means you can't update the app). Never commit the keystore — `*.jks` is git-ignored. Store icon: `mobile/store-icon-512.png`.

## Files

```
radar/webapp/          the app (HTML/CSS/JS) — edit here
  index.html, styles.css, app.js (UI, live store, notifications), demo.js (sample data + demo engine)
  manifest.webmanifest, sw.js (installable web app), icons/, fonts/ (bundled, works offline)
radar/api.py           /api/v1 for the app + serves /app/
mobile/
  package.json         Capacitor 8 + plugins (app, local-notifications), npm scripts
  capacitor.config.json  appId com.mintalpha.app
  scripts/copy-web.mjs  radar/webapp -> www
  android/             Android Studio project (generated by Capacitor, then branded:
                       adaptive + themed icons, Android 12 splash, notification icon)
.github/workflows/build.yml   tests + debug APK
deploy/Caddyfile       HTTPS reverse proxy
```
