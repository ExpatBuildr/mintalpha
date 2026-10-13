/* MintAlpha — companion app for Free Mint Radar.
 *
 * Runs in three places with the same code:
 *   1. Android app (Capacitor WebView)  -> Capacitor global present, phone notifications on
 *   2. Installed web app from the radar server at /app/
 *   3. Plain browser
 * Two data sources share one interface:
 *   - demo store (demo.js): sample data, local reminder engine, demo clock
 *   - live store (below): your radar server's /api/v1 with a bearer token
 */
(function () {
  "use strict";
  const M = 60e3, H = 60 * M;
  const PREFS_KEY = "mintalpha-prefs-v1";
  const CACHE_KEY = "mintalpha-live-cache-v1";
  const REFRESH_MS = 30e3;
  const TZS = { "Asia/Manila": "Manila", "UTC": "UTC", "America/New_York": "New York", "Europe/London": "London", "Asia/Singapore": "Singapore" };
  const TICKS = { ethereum: "ETH", base: "BASE", polygon: "POL", solana: "SOL", bitcoin_ordinals: "BTC", abstract: "ABS", apechain: "APE" };
  const PRICE_MAP = { claimed_free: "claimed", project_claimed_free: "project", confirmed_free: "confirmed", paid: "paid", unknown: "unknown" };
  const KIND_ICON = { discovery: "🆕", update: "🔄", pre_1440: "⏰", pre_60: "⏰", live: "🟢", repeat: "🔁", digest: "📋", system: "🛑" };

  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const iso = v => (v ? Date.parse(v) : null);

  // ---------- platform ----------
  const Cap = window.Capacitor;
  const isNative = !!(Cap && Cap.isNativePlatform && Cap.isNativePlatform());
  const plugin = name => (Cap && ((Cap.Plugins && Cap.Plugins[name]) || (Cap.registerPlugin && Cap.registerPlugin(name)))) || null;
  const servedByRadar = !isNative && /^https?:$/.test(location.protocol) && location.pathname.startsWith("/app");

  // ---------- prefs ----------
  function loadPrefs() {
    let p = {};
    try { p = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}") || {}; } catch (e) {}
    return Object.assign({ mode: null, serverUrl: servedByRadar ? location.origin : "", token: "", tz: "Asia/Manila", tab: "radar", phoneNotifs: true, lastSeen: 0 }, p);
  }
  let prefs = loadPrefs();
  const savePrefs = () => { try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch (e) {} };

  // ---------- formatting ----------
  let store; // set in boot
  const now = () => (store ? store.now() : Date.now());
  function fmtTime(ms, withUtc) {
    const tz = prefs.tz;
    const d = new Date(ms);
    const local = new Intl.DateTimeFormat("en-US", { timeZone: tz, weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }).format(d);
    if (!withUtc || tz === "UTC") return `${local} ${TZS[tz] || tz}`;
    const utc = new Intl.DateTimeFormat("en-US", { timeZone: "UTC", hour: "2-digit", minute: "2-digit", hour12: false }).format(d);
    return `${local} ${TZS[tz] || tz} · ${utc} UTC`;
  }
  function rel(ms) {
    let s = Math.round((ms - now()) / 1000); const fut = s >= 0; s = Math.abs(s);
    const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    const parts = []; if (d) parts.push(d + "d"); if (h) parts.push(h + "h"); if (!d && m) parts.push(m + "m");
    if (!parts.length) return "now";
    return fut ? "in " + parts.join(" ") : parts.join(" ") + " ago";
  }
  function whenText(p) {
    if (p.status === "live") return p.liveSince ? "Live · started " + rel(p.liveSince) : "Live now";
    if (p.precision === "exact" && p.launchAt) return rel(p.launchAt);
    if (p.precision === "day" && p.launchDate) return p.launchDate + " · time TBA";
    if (p.status === "ended") return "Ended";
    return "Date unknown";
  }
  const kindLabel = k => ({ pre_1440: "24h before", pre_60: "1h before", live: "When live", repeat: "Repeat", digest: "Digest" }[k] || k);

  // ---------- live store (radar server API) ----------
  function createLiveStore(cfg) {
    let offset = 0, projects = [], events = [], settings = null, lastOk = null, lastError = null;
    try {
      const c = JSON.parse(localStorage.getItem(CACHE_KEY) || "null");
      if (c && c.url === cfg.serverUrl) { projects = c.projects || []; events = c.events || []; settings = c.settings || null; lastOk = c.lastOk || null; }
    } catch (e) {}
    const base = cfg.serverUrl.replace(/\/+$/, "") + "/api/v1";

    async function req(path, opts) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 15000);
      let resp;
      try {
        resp = await fetch(base + path, Object.assign({ signal: ctrl.signal, headers: { "Authorization": "Bearer " + cfg.token, "Content-Type": "application/json" } }, opts || {}));
      } catch (e) {
        throw new Error(e.name === "AbortError" ? "The server took too long to answer." : "Can't reach the server. Check the address and your connection.");
      } finally { clearTimeout(timer); }
      let data = null;
      try { data = await resp.json(); } catch (e) {}
      if (!resp.ok) throw new Error((data && data.error) || `Server answered HTTP ${resp.status}.`);
      if (data && data.server_time) offset = Date.parse(data.server_time) - Date.now();
      return data;
    }

    function norm(a) {
      const price = PRICE_MAP[a.price_status] || "unknown";
      return {
        id: a.id, name: a.name, handle: a.handle, chain: a.chain, chainLabel: a.chain_label, feeNote: a.fee_note,
        status: a.status, resolved: a.resolved,
        launchAt: a.launch_precision === "exact" ? iso(a.launch_at) : null, launchDate: a.launch_date,
        precision: a.launch_precision, stated: a.launch_tz,
        price, priceLabel: a.price_label, priceText: a.price_text, supply: a.supply, elig: a.eligibility || [],
        website: a.website_url, mint: a.mint_url, platform: a.mint_platform, source: a.source_url,
        conf: a.confidence, confLabel: a.confidence_label, verifLabel: a.verification_label,
        flags: (a.flags || []).map(f => ({ sev: f.severity, t: f.text })), highRisk: a.high_risk,
        notes: a.notes || [], snoozedUntil: iso(a.snoozed_until), liveSince: iso(a.live_since), resolvedAt: iso(a.resolved_at),
        postCount: a.post_count || (a.posts ? a.posts.length : 1),
        evidence: (a.evidence || []).map(e => ({ f: e.field, v: e.value, kind: e.source_kind.replace("_", " "), who: e.source_url ? e.source_url.replace(/^https?:\/\/(www\.)?/, "") : "", url: e.source_url })),
        reminders: (a.reminders || []).map(r => ({ kind: r.kind, due: iso(r.due_at) })),
        repeatMinutes: a.live_repeat_minutes
      };
    }
    function persist() {
      try { localStorage.setItem(CACHE_KEY, JSON.stringify({ url: cfg.serverUrl, projects, events, settings, lastOk })); } catch (e) {}
    }
    function mapSettings(s) {
      const chains = {};
      for (const [k, v] of Object.entries(s.networks || {})) chains[k] = { label: v.label, enabled: v.enabled };
      return { repeat: s.runtime["reminders.live_repeat_minutes"], chains, paused: !!s.runtime["discovery.paused"],
               dailyBudget: s.runtime["discovery.daily_read_budget"], serverTz: s.display_timezone };
    }

    const st = {
      mode: "live",
      now: () => Date.now() + offset,
      status: () => ({ lastOk, lastError }),
      list: () => projects,
      get: id => projects.find(p => p.id === id) || null,
      async detail(id) {
        const d = await req(`/projects/${id}`);
        const p = norm(d.project);
        const i = projects.findIndex(x => x.id === id);
        if (i >= 0) projects[i] = Object.assign({}, projects[i], p, { postCount: projects[i].postCount });
        return p;
      },
      async refresh() {
        try {
          const [pl, fd] = await Promise.all([req("/projects?status=all"), req("/feed?limit=60")]);
          projects = pl.projects.map(norm);
          events = fd.events.map(e => ({ id: e.id, kind: e.kind, pid: e.project_id, at: iso(e.at), title: e.title, body: e.body }));
          if (!settings) settings = mapSettings(await req("/settings"));
          lastOk = Date.now(); lastError = null; persist();
          return true;
        } catch (e) { lastError = e.message; return false; }
      },
      events: () => events,
      unread: () => events.filter(e => e.at > (prefs.lastSeen || 0)).length,
      markRead() { prefs.lastSeen = st.now(); savePrefs(); },
      settings: () => settings || { repeat: 30, chains: {}, paused: false },
      async setSetting(key, value) {
        const body = {};
        if (key === "repeat") body["reminders.live_repeat_minutes"] = String(value);
        else if (key === "paused") body["discovery.paused"] = !!value;
        else if (key.startsWith("network.")) body[key] = !!value;
        const r = await req("/settings", { method: "PATCH", body: JSON.stringify(body) });
        settings = mapSettings(r.settings); persist();
      },
      async act(id, action, hours) {
        const r = await req(`/projects/${id}/actions`, { method: "POST", body: JSON.stringify({ action, hours }) });
        const p = norm(r.project);
        const i = projects.findIndex(x => x.id === id);
        if (i >= 0) projects[i] = Object.assign({}, projects[i], p, { postCount: projects[i].postCount });
        persist();
        return { message: r.message };
      },
      async test() { return req("/me"); }
    };
    return st;
  }

  async function testConnection(url, token) {
    url = url.trim().replace(/\/+$/, "");
    if (!/^https?:\/\//i.test(url)) throw new Error("Start the address with https:// (or http:// on your own Wi-Fi).");
    let ping;
    try {
      const r = await fetch(url + "/api/v1/ping", { cache: "no-store" });
      ping = await r.json();
    } catch (e) { throw new Error("No radar server found at that address."); }
    if (!ping || ping.app !== "free-mint-radar") throw new Error("That address answered, but it isn't a Free Mint Radar server.");
    if (!ping.auth_configured) throw new Error("The server's mobile API is off. Set MOBILE_API_TOKEN on the server and restart it.");
    const live = createLiveStore({ serverUrl: url, token: token.trim() });
    await live.test();
    return url;
  }

  // ---------- phone notifications (Android app only) ----------
  const Notify = (function () {
    const LN = isNative ? plugin("LocalNotifications") : null;
    let ready = false, granted = false, seq = 0;
    const CH = "mint-reminders";
    async function init() {
      if (!LN || ready) return;
      ready = true;
      try {
        await LN.createChannel({ id: CH, name: "Mint reminders", description: "Launch countdowns and live-mint reminders", importance: 5, vibration: true, visibility: 1 });
      } catch (e) {}
      try {
        let perm = await LN.checkPermissions();
        if (perm.display !== "granted") perm = await LN.requestPermissions();
        granted = perm.display === "granted";
      } catch (e) { granted = false; }
      try {
        LN.addListener("localNotificationActionPerformed", ev => {
          const pid = ev && ev.notification && ev.notification.extra && ev.notification.extra.pid;
          if (pid) { closeSheet(); openDetail(pid); }
        });
      } catch (e) {}
    }
    const enabled = () => !!(LN && granted && prefs.phoneNotifs);
    async function clearPlanned() {
      if (!LN) return;
      try {
        const pending = await LN.getPending();
        const ours = (pending.notifications || []).filter(n => n.id < 2000000000 && n.id >= 1000);
        if (ours.length) await LN.cancel({ notifications: ours.map(n => ({ id: n.id })) });
      } catch (e) {}
    }
    // Live mode: mirror the server's reminder schedule on the phone so it alerts even when the app is closed.
    async function plan(projects, repeatMinutes) {
      if (!enabled()) { await clearPlanned(); return 0; }
      await clearPlanned();
      const t = Date.now(), out = [];
      const add = (id, at, title, body, pid) => { if (at > t + 5000) out.push({ id, title, body, channelId: CH, smallIcon: "ic_stat_mintalpha", extra: { pid }, schedule: { at: new Date(at), allowWhileIdle: true } }); };
      for (const p of projects) {
        if (p.resolved || !p.id || p.id > 1999999) continue;
        const snoozeEnd = p.snoozedUntil && p.snoozedUntil > t ? p.snoozedUntil : 0;
        const base = p.id * 1000;
        if (p.status === "upcoming" && p.launchAt) {
          [[1440, 1], [60, 2]].forEach(([m, slot]) => {
            const at = p.launchAt - m * M;
            if (at > snoozeEnd) add(base + slot, at, `Mint in ~${m === 60 ? "1 hour" : "24 hours"}: ${p.name}`, fmtTime(p.launchAt, true), p.id);
          });
          add(base + 3, Math.max(p.launchAt, snoozeEnd), `MINT IS LIVE: ${p.name}`, p.mint ? "Mint link found — tap for details" : "Check the source post", p.id);
        }
        if ((p.status === "live" || (p.status === "upcoming" && p.launchAt)) && !p.highRisk) {
          const start = Math.max(p.status === "live" ? t : p.launchAt, snoozeEnd);
          const every = (repeatMinutes || 30) * M;
          for (let i = 1; i <= 12; i++) add(base + 10 + i, start + i * every, `Still live, not resolved: ${p.name}`, "Tap to mark Minted, Missed or Dismiss", p.id);
        }
      }
      if (!out.length) return 0;
      try { await LN.schedule({ notifications: out.slice(0, 400) }); } catch (e) { console.warn("schedule failed", e); return 0; }
      return out.length;
    }
    // Demo mode: show an event on the phone right away.
    async function now_(title, body, pid) {
      if (!enabled()) return;
      try { await LN.schedule({ notifications: [{ id: 2000000000 + (seq++ % 100000), title, body, channelId: CH, smallIcon: "ic_stat_mintalpha", extra: { pid } }] }); } catch (e) {}
    }
    return { available: !!LN, init, plan, now: now_, clearPlanned, isGranted: () => granted };
  })();

  // ---------- reminder sync after data changes ----------
  let planTimer;
  function replanNotifications() {
    clearTimeout(planTimer);
    planTimer = setTimeout(() => {
      if (store && store.mode === "live") Notify.plan(store.list(), store.settings().repeat);
      else Notify.clearPlanned();
    }, 400);
  }

  // ---------- UI ----------
  const ICONS = {
    radar: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><path d="M12 12l6-5"/>',
    live: '<circle cx="12" cy="12" r="3"/><path d="M6.3 6.3a8 8 0 0 0 0 11.4M17.7 6.3a8 8 0 0 1 0 11.4"/>',
    upcoming: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
    done: '<path d="M20 6L9 17l-5-5"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2V21a2 2 0 1 1-4 0v-.1A1.7 1.7 0 0 0 7 19.4a1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 3.6 14H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 4.6 7a1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 9 3.6V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>'
  };
  const TABS = [["radar", "Radar"], ["live", "Live"], ["upcoming", "Upcoming"], ["done", "Done"], ["settings", "Settings"]];

  let markN = 0;
  function mark() {
    const n = ++markN;
    return `<svg class="mark" viewBox="0 0 64 64" aria-hidden="true">
      <defs><clipPath id="mc${n}"><rect width="64" height="64" rx="15"/></clipPath></defs>
      <rect width="64" height="64" rx="15" fill="var(--brand)"/>
      <g clip-path="url(#mc${n})">
        <circle cx="32" cy="34" r="21" fill="none" stroke="#fff" stroke-opacity=".28" stroke-width="2"/>
        <circle cx="32" cy="34" r="13" fill="none" stroke="#fff" stroke-opacity=".45" stroke-width="2"/>
        <g class="sw"><path d="M32 34 L32 13 A21 21 0 0 1 50.2 23.5 Z" fill="#fff" fill-opacity=".30"/>
        <path d="M32 34 L50.2 23.5" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/></g>
        <g class="bp"><rect x="37" y="16" width="7" height="7" rx="1.2" transform="rotate(45 40.5 19.5)" fill="var(--blip)"/></g>
        <circle cx="32" cy="34" r="3" fill="#fff"/>
      </g></svg>`;
  }

  function sortOpen(a, b) {
    const rank = p => p.status === "live" ? 0 : p.status === "upcoming" ? 1 : 2;
    if (rank(a) !== rank(b)) return rank(a) - rank(b);
    const ta = a.launchAt || (a.launchDate ? Date.parse(a.launchDate) : Infinity);
    const tb = b.launchAt || (b.launchDate ? Date.parse(b.launchDate) : Infinity);
    return ta - tb;
  }
  function card(p) {
    const tick = p.chain ? (TICKS[p.chain] || p.chain.toUpperCase()) : "chain ?";
    const soon = p.status === "upcoming" && p.launchAt && p.launchAt - now() < 2 * H;
    const flags = p.flags || [];
    const warn = p.highRisk ? `<span class="warnline">⚠ Likely scam · ${flags.length} warning${flags.length > 1 ? "s" : ""}</span>`
      : flags.length ? `<span class="warnline soft">⚠ ${flags.length} warning${flags.length > 1 ? "s" : ""}</span>` : "";
    const snz = p.snoozedUntil && p.snoozedUntil > now() ? `<span class="snz">Snoozed until ${esc(fmtTime(p.snoozedUntil, false))}</span>` : "";
    return `<button class="card ${esc(p.status)}${p.highRisk ? " risk" : ""}" data-open="${p.id}">
      <span class="rail"></span>
      <span class="body">
        <span class="row"><span class="name">${esc(p.name)}</span><span class="pill ${esc(p.status)}">${esc(p.status.replace("_", " "))}</span></span>
        <span class="row"><span class="chip">${esc(tick)}</span><span class="when${soon ? " soon" : ""}">${esc(whenText(p))}</span></span>
        <span class="meta">${esc(p.priceLabel)} · confidence ${(p.conf || 0).toFixed(2)}</span>
        ${warn}${snz}
      </span></button>`;
  }
  function listView(title, sub, items, emptyMsg) {
    return `<div class="view-title"><h1>${esc(title)}</h1><small>${esc(sub)}</small></div>
      <div class="list">${items.length ? items.map(card).join("") : `<div class="empty">${esc(emptyMsg)}</div>`}</div>`;
  }

  function renderBanner() {
    let html = "";
    if (store.mode === "demo") {
      html = `<div class="banner demo" role="note"><p>Demo data: sample projects, not real mints.</p><button data-goto="settings">Connect radar</button></div>`;
    } else {
      const s = store.status();
      if (s.lastError) html = `<div class="banner error" role="alert"><p>${esc(s.lastError)}${s.lastOk ? " Showing data from " + esc(fmtTime(s.lastOk, false)) + "." : ""}</p><button id="retry">Retry</button></div>`;
      else if (store.settings().paused) html = `<div class="banner demo" role="note"><p>X polling is paused on the server. No new mints will be found.</p><button data-resume>Resume</button></div>`;
    }
    $("banner").innerHTML = html;
  }

  function render() {
    if (!store) return;
    const all = store.list();
    const open = all.filter(p => !p.resolved).sort(sortOpen);
    const live = open.filter(p => p.status === "live");
    const tab = prefs.tab;
    let html = "";
    if (tab === "radar") {
      const next = open.filter(p => p.status === "upcoming" && p.launchAt).sort((a, b) => a.launchAt - b.launchAt)[0];
      html = `<div class="strip">
          <div class="stat"><b>${live.length}</b><span>live now</span></div>
          <div class="stat"><b>${next ? esc(rel(next.launchAt).replace("in ", "")) : "—"}</b><span>${next ? "until " + esc(next.name) : "no timed launch"}</span></div>
          <div class="stat"><b>${open.length}</b><span>being tracked</span></div>
        </div>` + listView("Radar", `${open.length} open`, open, store.mode === "demo" ? "Nothing tracked. Tap Simulate post." : "Nothing tracked yet. New free mints show up here as the radar finds them.");
    } else if (tab === "live") {
      html = listView("Live now", "reminders every " + store.settings().repeat + " min", live, "No mints are live right now.");
    } else if (tab === "upcoming") {
      html = listView("Upcoming", "24h and 1h reminders", open.filter(p => p.status !== "live"), "No upcoming mints.");
    } else if (tab === "done") {
      const done = all.filter(p => p.resolved).sort((a, b) => (b.resolvedAt || 0) - (a.resolvedAt || 0));
      html = listView("Resolved", "minted, missed, dismissed, ended", done, "Projects you mark Minted, Missed or Dismiss land here.");
    } else {
      html = settingsView();
    }
    $("main").innerHTML = html;
    $("tabs").innerHTML = TABS.map(([k, label]) => `<button data-tab="${k}" ${tab === k ? 'aria-current="page"' : ""}>
        <span class="ind"><svg viewBox="0 0 24 24">${ICONS[k]}</svg></span>${label}
        ${k === "live" && live.length ? `<span class="dot">${live.length}</span>` : ""}</button>`).join("");
    $("fab").hidden = store.mode !== "demo" || tab === "settings";
    $("refresh").hidden = store.mode !== "live";
    const u = $("unread"), n = store.unread(); u.hidden = !n; u.textContent = n > 99 ? "99+" : n;
    if (store.mode === "live") {
      const s = store.status();
      const hm = t => new Intl.DateTimeFormat("en-US", { timeZone: prefs.tz, hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(t));
      $("clockline").textContent = !s.lastOk ? "Connecting…" : (s.lastError ? "Offline · last sync " : "Synced ") + hm(s.lastOk);
    } else {
      const off = store.clockOffsetHours();
      $("clockline").textContent = (off ? "Demo clock " : "") + fmtTime(now(), false);
    }
    renderBanner();
  }

  function settingsView() {
    const s = store.settings();
    const chains = Object.entries(s.chains || {}).map(([k, c]) => `<div class="setrow"><label for="ch-${k}">${esc(c.label)}<small>${esc(TICKS[k] || k)}</small></label>
        <input type="checkbox" class="switch" id="ch-${k}" data-chain="${k}" ${c.enabled ? "checked" : ""}></div>`).join("");
    const repeatOpts = Array.from(new Set([15, 30, 60, s.repeat])).sort((a, b) => a - b);
    const live = store.mode === "live";
    const conn = live
      ? `<div class="setrow"><label>Connected<small>${esc(prefs.serverUrl)}</small></label></div>
         <div class="btnrow"><button class="btn" id="disconnect">Use demo data instead</button></div>`
      : `<div class="field"><label for="srv-url">Radar server address</label>
           <input id="srv-url" type="url" inputmode="url" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="https://radar.yourdomain.com" value="${esc(prefs.serverUrl)}"></div>
         <div class="field"><label for="srv-token">Access token (MOBILE_API_TOKEN)</label>
           <input id="srv-token" type="password" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Paste the token from your server's .env" value="${esc(prefs.token)}"></div>
         <div class="btnrow"><button class="btn accent" id="connect">Connect</button></div>
         <div class="formmsg" id="connmsg" role="status"></div>`;
    const notif = Notify.available
      ? `<div class="setrow"><label for="set-notifs">Phone notifications<small>${Notify.isGranted() ? "24h, 1h, live and repeat reminders, even when the app is closed" : "Allow notifications for MintAlpha in Android settings"}</small></label>
           <input type="checkbox" class="switch" id="set-notifs" ${prefs.phoneNotifs ? "checked" : ""}></div>` : "";
    const demo = live ? "" : `<div class="view-title"><h1 style="font-size:18px">Demo clock</h1><small>test reminders fast</small></div>
      <div class="setgroup">
        <div class="setrow"><label>Skip ahead<small>${store.clockOffsetHours() ? `Demo clock is ${store.clockOffsetHours()}h ahead of real time.` : "Demo clock matches real time."} Reminders due along the way fire.</small></label></div>
        <div class="btnrow"><button class="btn" data-ff="30">+30 min</button><button class="btn" data-ff="60">+1 hour</button>
          <button class="btn" data-ff="360">+6 hours</button><button class="btn" data-ff="1440">+24 hours</button></div>
        <div class="btnrow"><button class="btn" id="reset">Reset demo data</button></div>
      </div>`;
    const paused = live ? `<div class="setrow"><label for="set-paused">Pause X polling<small>Stops paid API reads on the server</small></label>
        <input type="checkbox" class="switch" id="set-paused" ${s.paused ? "checked" : ""}></div>` : "";
    return `<div class="view-title"><h1>Settings</h1><small>${live ? "live server" : "demo data"}</small></div>
      <div class="view-title"><h1 style="font-size:18px">Connection</h1><small>${live ? "connected" : "not connected"}</small></div>
      <div class="setgroup">${conn}</div>
      <div class="view-title"><h1 style="font-size:18px">Reminders</h1><small>${live ? "saved on the server" : "this phone"}</small></div>
      <div class="setgroup">
        <div class="setrow"><label for="set-repeat">Live reminder interval<small>Repeats until you resolve or snooze</small></label>
          <select id="set-repeat">${repeatOpts.map(v => `<option value="${v}" ${s.repeat === v ? "selected" : ""}>${v} min</option>`).join("")}</select></div>
        <div class="setrow"><label for="set-tz">Show times in<small>UTC is always shown too</small></label>
          <select id="set-tz">${Object.entries(TZS).map(([v, l]) => `<option value="${v}" ${prefs.tz === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>
        ${notif}${paused}
      </div>
      <div class="view-title"><h1 style="font-size:18px">Chains</h1><small>off = not tracked</small></div>
      <div class="setgroup">${chains || '<div class="setrow"><label>Loading…</label></div>'}</div>
      ${demo}
      <div class="view-title"><h1 style="font-size:18px">About</h1><small>v1.0</small></div>
      <div class="setgroup"><div class="about"><span class="am">${mark()}</span><div><b>Mint<span class="a">Alpha</span></b><span class="meta">Companion app for Free Mint Radar. It never asks for a seed phrase or connects a wallet.</span></div></div>
        <div class="btnrow"><button class="btn" id="replay">Replay intro</button></div></div>`;
  }

  // ---------- sheets ----------
  function closeSheet() { $("overlay").innerHTML = ""; }
  function sheet(inner) {
    $("overlay").innerHTML = `<div class="scrim" data-close></div><div class="sheet" role="dialog" aria-modal="true"><div class="grab"></div><button class="sheetclose" data-close aria-label="Close">✕</button>${inner}</div>`;
  }
  function linkHtml(url, platform, p) {
    if (!url) return "Not found yet";
    const label = platform ? `<div class="note">${esc(platform)}</div>` : "";
    if (store.mode === "demo") return `<span class="linktxt">${esc(url)}</span><div class="note">${platform ? esc(platform) + " · " : ""}sample link</div>`;
    if (p.highRisk || !/^https:\/\//i.test(url)) {
      return `<span class="linktxt">${esc(url)}</span><button class="copy" data-copy="${esc(url)}">Copy</button><div class="note">Not clickable: this project has security warnings.</div>`;
    }
    return `<a class="extlink" href="${esc(url)}" ${isNative ? "" : 'target="_blank"'} rel="noopener noreferrer nofollow">${esc(url)}</a>${label}`;
  }
  let detailSeq = 0;
  async function openDetail(id, snoozeMenu) {
    const cached = store.get(id);
    if (!cached) { toast("That project isn't in the list anymore."); return; }
    drawDetail(cached, snoozeMenu, true);
    const seq = ++detailSeq;
    try {
      const full = await store.detail(id);
      if (seq === detailSeq && $("overlay").innerHTML) drawDetail(full, snoozeMenu, false);
    } catch (e) {
      if (seq === detailSeq && $("overlay").innerHTML) drawDetail(cached, snoozeMenu, false, e.message);
    }
  }
  function drawDetail(p, snoozeMenu, loading, err) {
    let launch;
    if (p.precision === "exact" && p.launchAt) launch = `${esc(fmtTime(p.launchAt, true))}<div class="note">${esc(rel(p.launchAt))}${p.stated && !["UTC", "relative"].includes(p.stated) ? " · stated as " + esc(p.stated) : ""}</div>`;
    else if (p.precision === "day") launch = `${esc(p.launchDate)}<div class="note">No time or zone posted yet, so no timed reminders.</div>`;
    else launch = p.status === "live" ? "Reported live" : "Unknown — tracking for updates";
    const snoozed = p.snoozedUntil && p.snoozedUntil > now();
    const rem = [];
    for (const r of p.reminders || []) rem.push(`${kindLabel(r.kind)} · ${fmtTime(r.due, false)}`);
    if (!p.resolved && p.status === "live") rem.push(p.highRisk ? "No repeats: high-risk projects get one live alert" : `Repeats every ${p.repeatMinutes || store.settings().repeat} min until resolved`);
    let actions;
    if (snoozeMenu) actions = `<div class="actions">
        <button class="btn" data-act="snooze" data-h="1" data-id="${p.id}">1 hour</button><button class="btn" data-act="snooze" data-h="4" data-id="${p.id}">4 hours</button>
        <button class="btn" data-act="snooze" data-h="24" data-id="${p.id}">24 hours</button><button class="btn" data-back="${p.id}">Back</button></div>`;
    else if (p.resolved) actions = `<div class="actions"><button class="btn accent wide" data-act="reopen" data-id="${p.id}">Reopen</button></div>`;
    else actions = `<div class="actions">
        <button class="btn primary" data-act="minted" data-id="${p.id}">✓ Minted</button><button class="btn" data-act="missed" data-id="${p.id}">Missed</button>
        <button class="btn" data-act="dismissed" data-id="${p.id}">Dismiss</button>
        ${snoozed ? `<button class="btn" data-act="unsnooze" data-id="${p.id}">Unsnooze</button>` : `<button class="btn" data-snooze="${p.id}">Snooze</button>`}</div>`;
    const src = p.source && store.mode === "live" ? `<dt>Source post</dt><dd>${linkHtml(p.source, null, {})}</dd>` : "";
    const evidence = (p.evidence || []).map(e => `<li><span class="k">${esc(e.f)} · ${esc(e.kind)}</span><br>${esc(e.v)}${e.who ? ` <span class="meta">— ${esc(e.who)}</span>` : ""}</li>`).join("");
    sheet(`<div class="row"><h2>${esc(p.name)}</h2><span class="pill ${esc(p.status)}">${esc(p.status.replace("_", " "))}</span></div>
      <div class="meta">${p.handle ? "@" + esc(p.handle) + " · " : ""}<span class="mono">#${p.id}</span> · ${p.postCount || 1} source post${(p.postCount || 1) > 1 ? "s" : ""}${snoozed ? " · snoozed until " + esc(fmtTime(p.snoozedUntil, false)) : ""}</div>
      ${err ? `<div class="banner error" style="margin:12px 0 0"><p>${esc(err)}</p></div>` : ""}
      ${actions}
      <div class="sec"><h3>Mint</h3><dl class="kv">
        <dt>Launch</dt><dd>${launch}</dd>
        <dt>Chain</dt><dd>${esc(p.chainLabel || "Unknown")}</dd>
        <dt>Price</dt><dd>${esc(p.priceLabel)}${p.feeNote ? `<div class="note">${esc(p.feeNote)}</div>` : ""}</dd>
        <dt>Supply</dt><dd class="mono">${p.supply ? Number(p.supply).toLocaleString("en-US") : "—"}</dd>
        <dt>Eligibility</dt><dd>${esc((p.elig || []).join(", ") || "—")}</dd>
        <dt>Mint link</dt><dd>${linkHtml(p.mint, p.platform, p)}</dd>
        <dt>Website</dt><dd>${linkHtml(p.website, null, p)}</dd>
        ${src}
        <dt>Confidence</dt><dd><span class="mono">${(p.conf || 0).toFixed(2)}</span> · ${esc(p.confLabel)}</dd>
        <dt>Verified</dt><dd>${esc(p.verifLabel)}</dd>
      </dl></div>
      ${(p.flags || []).length ? `<div class="sec"><h3>Warnings</h3><ul class="flags">${p.flags.map(f => `<li class="${f.sev === "high" ? "high" : "warn"}">${esc(f.t)}</li>`).join("")}</ul></div>` : ""}
      ${(p.notes || []).length ? `<div class="sec"><h3>Notes</h3>${p.notes.map(n => `<div class="note">${esc(n)}</div>`).join("")}</div>` : ""}
      ${rem.length ? `<div class="sec"><h3>Reminders</h3>${rem.map(x => `<div class="note mono">${esc(x)}</div>`).join("")}</div>` : ""}
      <div class="sec"><h3>Evidence</h3>${loading && !evidence ? '<div class="note">Loading…</div>' : `<ul class="ev">${evidence || '<li>None recorded.</li>'}</ul>`}</div>
      <div class="safety">Never share a seed phrase. Check the domain yourself, use a burner wallet, and read what you sign. These checks lower risk but don't guarantee a link is safe.</div>`);
  }
  function openInbox() {
    store.markRead(); render();
    const ev = store.events();
    sheet(`<div class="row"><h2>Alerts</h2><span class="meta">${ev.length} recent</span></div>
      ${ev.length ? `<ul class="inbox">${ev.map(a => `<li><button ${a.pid ? `data-open="${a.pid}"` : ""}>
        <span class="ic" aria-hidden="true">${KIND_ICON[a.kind] || "•"}</span>
        <span><span class="t">${esc(a.title)}</span><br><span class="s">${esc(a.body)}</span><br><span class="s mono">${esc(fmtTime(a.at, false))}</span></span>
      </button></li>`).join("")}</ul>` : `<div class="empty" style="margin-top:12px">${store.mode === "demo" ? "No alerts yet. Tap Simulate post, or skip the demo clock ahead in Settings." : "No alerts yet. Discovery alerts and reminders the server sends show up here."}</div>`}`);
  }

  let toastTimer;
  function toast(msg, pid) {
    clearTimeout(toastTimer);
    $("toastwrap").innerHTML = `<div class="toast" role="status"><p>${esc(msg)}</p>${pid ? `<button data-open="${pid}">View</button>` : ""}</div>`;
    toastTimer = setTimeout(() => { $("toastwrap").innerHTML = ""; }, 4500);
  }

  // ---------- data refresh loop ----------
  let refreshing = false;
  async function refresh(manual) {
    if (refreshing || !store) return;
    refreshing = true;
    const btn = $("refresh").querySelector("svg"); if (manual) btn.classList.add("spin");
    const before = store.mode === "live" ? new Set(store.events().map(e => e.id)) : null;
    const ok = await store.refresh();
    refreshing = false; btn.classList.remove("spin");
    if (store.mode === "live" && ok && before && before.size) {
      const fresh = store.events().filter(e => !before.has(e.id));
      if (fresh.length) toast(fresh.length === 1 ? fresh[0].title : `${fresh.length} new alerts`, fresh[0].pid);
    }
    if (manual && store.mode === "live") toast(ok ? "Up to date." : store.status().lastError);
    render();
    replanNotifications();
  }

  function useStore(mode) {
    if (mode === "live") {
      store = createLiveStore({ serverUrl: prefs.serverUrl, token: prefs.token });
    } else {
      store = window.MintDemo.create({
        fmt: fmtTime,
        toast,
        onEvent: e => { if (booted) Notify.now(e.title, e.body, e.pid); }
      });
    }
    prefs.mode = mode; savePrefs();
  }

  // ---------- events ----------
  async function runAction(id, action, hours) {
    try {
      const r = await store.act(id, action, hours);
      toast(r.message);
      render(); replanNotifications();
      openDetail(id);
    } catch (e) { toast(e.message); }
  }

  document.addEventListener("click", async e => {
    const t = e.target.closest("[data-open],[data-tab],[data-act],[data-snooze],[data-back],[data-close],[data-ff],[data-goto],[data-copy],[data-resume],#bell,#fab,#reset,#replay,#connect,#disconnect,#refresh,#retry");
    if (!t) return;
    if (t.dataset.close !== undefined) return closeSheet();
    if (t.dataset.open) { $("toastwrap").innerHTML = ""; return openDetail(+t.dataset.open); }
    if (t.dataset.tab || t.dataset.goto) { prefs.tab = t.dataset.tab || t.dataset.goto; savePrefs(); render(); window.scrollTo(0, 0); return; }
    if (t.dataset.act) return runAction(+t.dataset.id, t.dataset.act, +t.dataset.h || undefined);
    if (t.dataset.snooze) return openDetail(+t.dataset.snooze, true);
    if (t.dataset.back) return openDetail(+t.dataset.back);
    if (t.dataset.copy) {
      try { await navigator.clipboard.writeText(t.dataset.copy); toast("Copied. Check the domain carefully before opening it."); }
      catch (err) { toast("Couldn't copy. Long-press the address to select it."); }
      return;
    }
    if (t.dataset.resume !== undefined) { try { await store.setSetting("paused", false); toast("X polling resumed."); } catch (err) { toast(err.message); } render(); return; }
    if (t.dataset.ff) {
      const fired = store.fastForward(+t.dataset.ff);
      render();
      const m = +t.dataset.ff, lbl = m >= 60 ? m / 60 + "h" : m + "m";
      toast(fired.length ? `Skipped ahead ${lbl} · ${fired.length} reminder${fired.length > 1 ? "s" : ""} fired` : `Skipped ahead ${lbl} · no reminders due`, fired[0] && fired[0].pid);
      return;
    }
    switch (t.id) {
      case "bell": return openInbox();
      case "refresh": case "retry": return refresh(true);
      case "fab": { const r = store.simulate(); render(); toast(r.message, r.pid); return; }
      case "replay": return runSplash(() => {});
      case "reset": store.reset(); closeSheet(); store.tick(); render(); toast("Demo reset to the sample projects."); return;
      case "disconnect":
        useStore("demo"); await Notify.clearPlanned(); render(); toast("Switched to demo data. Your server settings are saved.");
        return;
      case "connect": {
        const msg = $("connmsg"), url = $("srv-url").value, token = $("srv-token").value;
        msg.className = "formmsg"; msg.textContent = "Checking…";
        if (!token.trim()) { msg.className = "formmsg bad"; msg.textContent = "Paste the access token first."; return; }
        try {
          const clean = await testConnection(url, token);
          prefs.serverUrl = clean; prefs.token = token.trim(); prefs.lastSeen = 0;
          useStore("live");
          msg.className = "formmsg good"; msg.textContent = "Connected.";
          await refresh(false);
          prefs.tab = "radar"; savePrefs(); render();
          toast("Connected to your radar.");
        } catch (err) { msg.className = "formmsg bad"; msg.textContent = err.message; }
        return;
      }
    }
  });
  document.addEventListener("change", async e => {
    const t = e.target;
    try {
      if (t.id === "set-repeat") { await store.setSetting("repeat", +t.value); replanNotifications(); }
      else if (t.id === "set-tz") { prefs.tz = t.value; savePrefs(); }
      else if (t.id === "set-notifs") { prefs.phoneNotifs = t.checked; savePrefs(); if (t.checked) await Notify.init(); replanNotifications(); }
      else if (t.id === "set-paused") { await store.setSetting("paused", t.checked); toast(t.checked ? "X polling paused." : "X polling resumed."); }
      else if (t.dataset.chain) { await store.setSetting("network." + t.dataset.chain, t.checked); if (store.mode === "live") refresh(false); }
      else return;
    } catch (err) { toast(err.message); }
    render();
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeSheet(); });

  // ---------- splash ----------
  function runSplash(done) {
    const el = $("splash"), fill = $("splashfill"), step = $("splashstep");
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const list = store.list();
    const open = list.filter(p => !p.resolved).length;
    const liveN = list.filter(p => p.status === "live").length;
    const steps = store.mode === "live"
      ? ["Connecting to your radar", `Loading ${open} tracked projects`, `Checking reminders · ${liveN} live`, "Ready"]
      : ["Starting radar", `Loading ${open} sample projects`, `Checking reminders · ${liveN} live`, "Ready"];
    $("splashfoot").textContent = store.mode === "live" ? "Tap to skip" : "Tap to skip · Demo data";
    const gap = reduce ? 160 : 520;
    let i = 0, finished = false, timer;
    el.hidden = false; el.classList.remove("out"); fill.style.width = "0";
    const finish = () => {
      if (finished) return; finished = true; clearTimeout(timer);
      el.classList.add("out");
      setTimeout(() => { el.hidden = true; done(); }, reduce ? 0 : 350);
    };
    const next = () => {
      step.textContent = steps[i]; fill.style.width = ((i + 1) / steps.length * 100) + "%";
      i++; timer = setTimeout(i < steps.length ? next : finish, i < steps.length ? gap : gap * .8);
    };
    el.onclick = finish;
    $("splashmark").innerHTML = mark();
    next();
  }

  // ---------- Android back button & resume ----------
  function wireNative() {
    if (!isNative) return;
    const App = plugin("App");
    if (!App) return;
    try {
      App.addListener("backButton", () => {
        if ($("overlay").innerHTML) return closeSheet();
        if (prefs.tab !== "radar") { prefs.tab = "radar"; savePrefs(); render(); return; }
        App.minimizeApp ? App.minimizeApp() : App.exitApp();
      });
      App.addListener("appStateChange", st => { if (st.isActive) refresh(false); });
    } catch (e) {}
  }

  // ---------- boot ----------
  let booted = false;
  async function boot() {
    $("hdrmark").innerHTML = mark();
    const mode = prefs.mode || (prefs.serverUrl && prefs.token ? "live" : "demo");
    useStore(mode === "live" && prefs.serverUrl && prefs.token ? "live" : "demo");
    if (servedByRadar && !prefs.token && prefs.tab !== "settings") prefs.tab = "settings";
    render();
    const first = store.refresh();
    runSplash(async () => {
      await first;
      await Notify.init();   // ask for notification permission before the first reminders fire
      booted = true;
      if (store.mode === "demo") {
        const fired = store.tick();
        if (fired.length) toast(fired.length === 1 ? fired[0].title : `${fired.length} new reminders`, fired[0].pid);
      }
      render();
      replanNotifications();
    });
    wireNative();
    setInterval(() => {
      if (!booted) return;
      if (store.mode === "demo") {
        const fired = store.tick();
        if (fired.length) toast(fired.length === 1 ? fired[0].title : `${fired.length} new reminders`, fired[0].pid);
        render();
      } else if (Date.now() - (store.status().lastOk || 0) >= REFRESH_MS) refresh(false);
      else render();
    }, 15e3);
    document.addEventListener("visibilitychange", () => { if (!document.hidden && booted && store.mode === "live") refresh(false); });
    if ("serviceWorker" in navigator && servedByRadar) {   // only the /app/ install path uses the offline cache
      navigator.serviceWorker.register("sw.js").catch(() => {});
    }
  }

  window.MintAlpha = { get store() { return store; }, prefs: () => prefs, render, refresh };
  boot();
})();
