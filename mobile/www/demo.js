/* MintAlpha demo data store.
 * Sample projects, a local reminder engine (24h / 1h / live / repeat, same rules as the
 * radar server) and a demo clock you can skip ahead. Nothing here touches the network.
 * Exposes window.MintDemo.create(): a store with the same interface as the live store in app.js.
 */
(function () {
  "use strict";
  const M = 60e3, H = 60 * M, D = 24 * H, GRACE = 10 * M;
  const KEY = "mintalpha-demo-v2";
  const RESOLVED = ["minted", "missed", "dismissed", "ended"];
  const CHAINS = {
    ethereum: { label: "Ethereum", fee: "Free = 0 ETH mint price. You still pay L1 gas, which can be several dollars when the network is busy." },
    base: { label: "Base", fee: "Free = 0 ETH on Base. Gas is usually cents, but some platforms add a small fixed fee per mint." },
    polygon: { label: "Polygon", fee: "Free = 0 POL. Gas is normally a fraction of a cent; check for platform fees." },
    solana: { label: "Solana", fee: "Free = 0 SOL, but you still pay network fees and account rent (about 0.01–0.02 SOL for a standard NFT)." },
    bitcoin_ordinals: { label: "Bitcoin Ordinals", fee: "Free usually means no creator price. You still pay inscription and Bitcoin network fees, which rise with mempool congestion." }
  };
  const PRICE = {
    claimed: "Free (third-party claim, unverified)",
    project: "Free (stated by the project's own account)",
    confirmed: "Free (shown on the official mint page)"
  };
  const VERIF = {
    claimed: "Unverified — only third-party posts so far",
    project: "Stated by the project's own X account",
    confirmed: "Official mint page shows it as free"
  };
  const floorMin = t => Math.floor(t / M) * M;
  const ev = (f, v, kind, who) => ({ f, v, kind, who });

  function decorate(p) {
    const c = p.chain ? CHAINS[p.chain] : null;
    p.chainLabel = c ? c.label : null;
    p.feeNote = c ? c.fee : null;
    p.priceLabel = PRICE[p.price] || "Unknown";
    p.verifLabel = VERIF[p.price] || "Unverified";
    p.highRisk = (p.flags || []).some(f => f.sev === "high");
    p.resolved = RESOLVED.includes(p.status);
    p.confLabel = p.conf >= .7 ? "high" : p.conf >= .45 ? "medium" : "low";
    return p;
  }

  function seed() {
    const t = floorMin(Date.now());
    const day3 = new Date(t + 3 * D).toISOString().slice(0, 10);
    return {
      v: 2, offset: 0, nextId: 7, queueIdx: 0, lastSeen: 0,
      settings: { repeat: 30, chains: { ethereum: true, base: true, polygon: true, solana: true, bitcoin_ordinals: true } },
      sent: { "3:live": 1, "4:live": 1 },
      events: [],
      projects: [
        { id: 1, name: "Pixel Pond", handle: "pixelpond_xyz", chain: "base", status: "upcoming", launchAt: t + 26 * H, precision: "exact", stated: "UTC",
          price: "project", supply: 3333, elig: ["Public", "First-come-first-served"], website: "https://pixelpond.xyz", mint: "https://pixelpond.xyz/mint",
          conf: .95, flags: [], postCount: 1,
          evidence: [ev("launch", "26h from first post, stated in UTC", "project account", "@pixelpond_xyz"), ev("price", "“0 ETH”", "project account", "@pixelpond_xyz"), ev("supply", "3,333", "project account", "@pixelpond_xyz")] },
        { id: 2, name: "Sol Sprouts", handle: "solsprouts", chain: "solana", status: "upcoming", launchAt: null, launchDate: day3, precision: "day",
          price: "project", elig: ["Allowlist"], conf: .75, flags: [], postCount: 2,
          notes: ["Date posted without a time — no timed reminders until an exact time and zone appear."],
          evidence: [ev("name", "Sol Sprouts", "third party", "@nftdropsdaily"), ev("launch", day3 + " (no time)", "project account", "@solsprouts"), ev("eligibility", "Allowlist closes tonight", "project account", "@solsprouts")] },
        { id: 3, name: "Satoshi Shards", handle: "satoshishards", chain: "bitcoin_ordinals", status: "live", launchAt: t - 20 * M, liveSince: t - 20 * M, lastNag: t - 20 * M, precision: "exact", stated: "UTC",
          price: "project", elig: [], mint: "https://magiceden.io/ordinals/launchpad/satoshi-shards", platform: "Magic Eden",
          conf: .95, flags: [], postCount: 1,
          evidence: [ev("launch", "Exact time in UTC", "project account", "@satoshishards"), ev("mint link", "Magic Eden launchpad", "project account", "@satoshishards")] },
        { id: 4, name: "0pensea_claims", handle: "0pensea_claims", chain: null, status: "live", liveSince: t - 2 * H, lastNag: t - 2 * H, launchAt: null, precision: "unknown",
          price: "claimed", elig: [], website: "https://opensea-claim.top/free", conf: .2, postCount: 1,
          flags: [
            { sev: "high", t: "opensea-claim.top uses the OpenSea name but isn't OpenSea's domain" },
            { sev: "high", t: "“Connect wallet to claim” wording is common in wallet-drainer scams" },
            { sev: "warn", t: ".top domains are frequently used by scam sites" },
            { sev: "warn", t: "Urgency pressure: “only 10 minutes left”" },
            { sev: "warn", t: "@0pensea_claims has only 12 followers" }],
          evidence: [ev("claim", "“Free NFT claim is live!”", "unknown account", "@0pensea_claims")] },
        { id: 5, name: "Glow Frogs", handle: "glowfrogs", chain: "polygon", status: "upcoming", launchAt: t + 55 * M, precision: "exact", stated: "ET",
          price: "confirmed", supply: 10000, elig: ["Open edition", "1 per wallet"], website: "https://glowfrogs.art", mint: "https://glowfrogs.art/mint",
          conf: .9, flags: [], postCount: 3,
          evidence: [ev("launch", "Stated in ET (converted, DST-aware)", "project account", "@glowfrogs"), ev("price", "Mint page shows “Free”", "official site", "glowfrogs.art/mint")] },
        { id: 6, name: "Quiet Lines", handle: "quietlines_art", chain: "ethereum", status: "date_unknown", launchAt: null, precision: "unknown",
          price: "claimed", supply: 512, elig: ["Allowlist"], conf: .55, postCount: 1,
          flags: [{ sev: "warn", t: "Post also mentions a 0.01 ETH public price — “free” may apply to the allowlist only" }],
          notes: ["Source says date TBA. It stays in the digest until a date is posted."],
          evidence: [ev("price", "“Free mint for WL, public 0.01 ETH”", "third party", "@alphacaller")] }
      ]
    };
  }

  const QUEUE = [
    { kind: "new", by: "@moontoads", make: (n, id) => ({ id, name: "Moon Toads", handle: "moontoads", chain: "base", status: "upcoming", launchAt: floorMin(n) + 6 * H, precision: "exact", stated: "UTC",
        price: "project", supply: 5555, elig: ["Allowlist", "Public"], website: "https://moontoads.xyz", mint: "https://moontoads.xyz/mint", conf: .9, flags: [], postCount: 1,
        evidence: [ev("launch", "6h from post, stated in UTC", "project account", "@moontoads")] }) },
    { kind: "dup", target: 1, by: "@alphahunter", text: "Repost about Pixel Pond merged into the existing project. No new alert." },
    { kind: "new", by: "@emberkeys", make: (n, id) => ({ id, name: "Ember Keys", handle: "emberkeys", chain: "solana", status: "live", launchAt: floorMin(n), liveSince: floorMin(n), lastNag: floorMin(n), precision: "exact", stated: "post time",
        price: "project", supply: 2222, elig: ["Public"], website: "https://emberkeys.io", mint: "https://emberkeys.io/mint", conf: .85, flags: [], postCount: 1,
        evidence: [ev("status", "“Mint is live now”", "project account", "@emberkeys")] }) },
    { kind: "move", target: 1, hours: 4, by: "@pixelpond_xyz" },
    { kind: "exact", target: 2, by: "@solsprouts" }
  ];

  function create(hooks) {
    // hooks: { fmt(ms, withUtc), toast(msg, pid), onEvent(event) }
    let S;
    try { const raw = localStorage.getItem(KEY); const s = raw && JSON.parse(raw); if (s && s.v === 2) S = s; } catch (e) {}
    if (!S) S = seed();
    const save = () => { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) {} };
    const now = () => Date.now() + S.offset;
    const P = id => S.projects.find(p => p.id === id);
    const chainOn = p => !p.chain || S.settings.chains[p.chain] !== false;

    function push(kind, p, title, body) {
      const e = { id: "d" + now() + Math.random().toString(36).slice(2, 6), kind, pid: p ? p.id : null, at: now(), title, body };
      S.events.unshift(e);
      S.events = S.events.slice(0, 100);
      if (hooks && hooks.onEvent) hooks.onEvent(e);
      return e;
    }

    function tick(t) {
      t = t || now();
      const out = [];
      for (const p of S.projects) {
        if (RESOLVED.includes(p.status) || !chainOn(p)) continue;
        const snoozed = p.snoozedUntil && p.snoozedUntil > t;
        if (p.status === "upcoming" && p.precision === "exact" && p.launchAt) {
          for (const mins of [1440, 60]) {
            const due = p.launchAt - mins * M, key = `${p.id}:pre${mins}:${p.launchAt}`;
            if (S.sent[key] || t < due) continue;
            if (t - due > GRACE || t >= p.launchAt) { S.sent[key] = 1; continue; }
            if (snoozed) continue;
            S.sent[key] = 1;
            out.push(push("pre_" + mins, p, `Mint in ~${mins === 60 ? "1 hour" : "24 hours"}: ${p.name}`, hooks.fmt(p.launchAt, true)));
          }
          if (t >= p.launchAt) { p.status = "live"; p.liveSince = p.launchAt; }
        }
        if (p.status === "live") {
          const lk = `${p.id}:live`;
          if (!S.sent[lk]) {
            if (snoozed) continue;
            S.sent[lk] = 1; p.lastNag = t;
            out.push(push("live", p, `MINT IS LIVE: ${p.name}`, p.mint ? "Mint link found — open Details" : "No mint link yet — check the source post"));
          } else if (!snoozed && !p.highRisk && t - (p.lastNag || p.liveSince || t) >= S.settings.repeat * M) {
            p.lastNag = t;
            out.push(push("repeat", p, `Still live, not resolved: ${p.name}`, "Tap Minted, Missed or Dismiss to stop these"));
          }
        }
      }
      if (out.length) save();
      return out;
    }

    function pendingReminders(p) {
      const list = [];
      if (p.resolved) return list;
      if (p.precision === "exact" && p.launchAt && p.status === "upcoming") {
        for (const m of [1440, 60]) {
          const due = p.launchAt - m * M;
          if (due > now() && !S.sent[`${p.id}:pre${m}:${p.launchAt}`]) list.push({ kind: "pre_" + m, due });
        }
        list.push({ kind: "live", due: p.launchAt });
      }
      return list;
    }

    const store = {
      mode: "demo",
      now,
      list() { return S.projects.filter(chainOn).map(p => decorate(p)); },
      get(id) { const p = P(id); return p ? decorate(p) : null; },
      async detail(id) { const p = store.get(id); if (p) { p.reminders = pendingReminders(p); p.repeatMinutes = S.settings.repeat; } return p; },
      async refresh() { return true; },   // reminders advance in tick(), driven by the app loop
      tick,
      events() { return S.events; },
      unread() { return S.events.filter(e => e.at > S.lastSeen).length; },
      markRead() { S.lastSeen = now(); save(); },
      settings() {
        const chains = {};
        for (const [k, c] of Object.entries(CHAINS)) chains[k] = { label: c.label, enabled: S.settings.chains[k] !== false };
        return { repeat: S.settings.repeat, chains, paused: false };
      },
      async setSetting(key, value) {
        if (key === "repeat") S.settings.repeat = +value;
        else if (key.startsWith("network.")) S.settings.chains[key.slice(8)] = !!value;
        save();
      },
      async act(id, action, hours) {
        const p = P(id); if (!p) throw new Error("Project not found");
        let msg;
        if (["minted", "missed", "dismissed"].includes(action)) {
          if (RESOLVED.includes(p.status)) throw new Error(`Already ${p.status}. Reopen it first.`);
          p.status = action; p.snoozedUntil = null; p.resolvedAt = now();
          msg = `${p.name}: marked ${action}. Reminders stopped.`;
        } else if (action === "snooze") {
          p.snoozedUntil = now() + hours * H; msg = `${p.name}: snoozed for ${hours}h.`;
        } else if (action === "unsnooze") {
          p.snoozedUntil = null; msg = `${p.name}: snooze cleared.`;
        } else if (action === "reopen") {
          p.resolvedAt = null;
          if (p.precision === "exact" && p.launchAt) p.status = p.launchAt <= now() ? "live" : "upcoming";
          else p.status = p.precision === "day" ? "upcoming" : "date_unknown";
          if (p.status === "live") p.lastNag = now();
          msg = `${p.name}: reopened.`;
        }
        save();
        return { message: msg };
      },
      simulate() {
        if (S.queueIdx >= QUEUE.length) return { message: "No new posts left in the sample feed. Reset the demo in Settings to replay." };
        const q = QUEUE[S.queueIdx++];
        const n = now();
        let res;
        if (q.kind === "new") {
          const p = q.make(n, S.nextId++);
          if (p.chain && S.settings.chains[p.chain] === false) { save(); return { message: `Post from ${q.by} filtered: ${CHAINS[p.chain].label} is turned off.` }; }
          decorate(p);
          S.projects.push(p);
          if (p.status === "live") S.sent[`${p.id}:live`] = 1;
          const when = p.status === "live" ? "live now" : hooks.fmt(p.launchAt, false);
          const e = push("discovery", p, `Free mint found: ${p.name}`, `${p.chainLabel || "Unknown chain"} · ${when} · via ${q.by}`);
          if (p.status === "live") push("live", p, `MINT IS LIVE: ${p.name}`, "Mint link found — open Details");
          res = { message: e.title, pid: p.id };
        } else if (q.kind === "dup") {
          const p = P(q.target); p.postCount = (p.postCount || 1) + 1;
          p.evidence.push(ev("mention", "Repost of the announcement", "third party", q.by));
          res = { message: q.text, pid: p.id };
        } else if (q.kind === "move") {
          const p = P(q.target);
          if (RESOLVED.includes(p.status) || p.status === "live") res = { message: `${q.by} posted a date change, but ${p.name} is already ${p.status}.`, pid: p.id };
          else {
            const old = p.launchAt; p.launchAt = old + q.hours * H;
            p.evidence.push(ev("launch", `Moved +${q.hours}h by the project`, "project account", q.by));
            const e = push("update", p, `Update: ${p.name} launch moved`, `${hooks.fmt(old, false)} → ${hooks.fmt(p.launchAt, false)}. Reminders rescheduled.`);
            res = { message: e.title, pid: p.id };
          }
        } else if (q.kind === "exact") {
          const p = P(q.target);
          if (RESOLVED.includes(p.status)) res = { message: `${q.by} posted an exact time, but ${p.name} is already ${p.status}.`, pid: p.id };
          else {
            p.launchAt = Date.parse(p.launchDate + "T15:00:00Z"); p.precision = "exact"; p.stated = "UTC"; p.notes = [];
            p.evidence.push(ev("launch", p.launchDate + " 15:00 UTC", "project account", q.by));
            const e = push("update", p, `Update: ${p.name} exact time found`, `${hooks.fmt(p.launchAt, true)}. 24h and 1h reminders scheduled.`);
            res = { message: e.title, pid: p.id };
          }
        }
        save();
        return res;
      },
      fastForward(mins) {
        let fired = [];
        for (let step = 0; step < mins; step += 5) { S.offset += 5 * M; fired = fired.concat(tick(now())); }
        save();
        return fired;
      },
      clockOffsetHours() { return Math.round(S.offset / H * 10) / 10; },
      reset() { S = seed(); save(); },
      // Phone notifications in demo mode fire as events happen (see app.js), so no plan here.
      notificationPlan() { return []; }
    };
    return store;
  }

  window.MintDemo = { create, CHAINS };
})();
