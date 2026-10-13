"""Rule-based extraction from a post. Deterministic, explainable, testable.
An optional LLM pass (classifier.py) can fill gaps afterwards — never schedules."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from . import security
from .dates import parse_schedule
from .models import Extraction, Post

FREE_PATTERNS = [
    r"\bfree\s*[- ]?mint", r"\bfree\s+nfts?\b", r"\bfree\s+claim", r"\bfree\s+to\s+mint\b",
    r"\bmint\s+(?:is\s+|for\s+)?free\b", r"\bprice\s*[:\-–]?\s*free\b", r"\bcost\s*[:\-–]?\s*free\b",
    r"(?<![\d.])0(?:\.0+)?\s*(?:eth|sol|btc|pol|matic|ape)\b", r"\bzero\s+cost\s+mint\b", r"\bfree\s+(?:drop|edition|open\s+edition)\b",
]
NFT_CONTEXT = re.compile(r"\b(nfts?|mint(?:ing|ed)?|ordinals?|inscriptions?|inscrib\w*|collection|pfp|allow\s*list|"
                         r"white\s*list|wl|open\s+edition|claim|drop|launchpad|cnfts?)\b", re.I)
PAID_RE = re.compile(r"(?<![\d.])(\d*\.\d+|[1-9]\d*(?:\.\d+)?)\s*(eth|sol|btc|pol|matic|ape|usdc)\b", re.I)
FEE_CONTEXT = re.compile(r"(gas|fees?|rent|network|tip|giveaway|won|volume|floor)\W{0,3}\w*\W{0,3}$", re.I)


def find_paid_price(text: str):
    """First non-zero price that isn't described as gas/fees/rent/floor. Returns (amount, unit) or None."""
    for m in PAID_RE.finditer(text):
        if float(m.group(1)) <= 0:
            continue
        if FEE_CONTEXT.search(text[max(0, m.start() - 20):m.start()]):
            continue
        return m.group(1), m.group(2).upper()
    return None
SUPPLY_RES = [
    re.compile(r"\b(?:supply|max\s+supply|total\s+supply)\s*[:\-–]?\s*(\d{1,3}(?:[,\s]\d{3})+|\d+)\b", re.I),
    re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d{2,6})\s+(?:supply|pieces|nfts|items|editions|total)\b", re.I),
]
ELIGIBILITY = [
    (r"\ballow\s*list\b", "allowlist"), (r"\b(white\s*list|wl)\b", "whitelist"), (r"\bgtd\b", "guaranteed (GTD)"),
    (r"\bfcfs\b", "first-come-first-served"), (r"\bpublic\s+(mint|sale|phase)\b", "public"),
    (r"\bholders?\s+only\b|\bfor\s+holders\b", "holders only"), (r"\bopen\s+edition\b", "open edition"),
    (r"\b(\d+)\s+per\s+wallet\b", "per-wallet limit"),
]
SELF_CUES = re.compile(r"\b(our|we|we're|we are|us|my collection|join us|i'm launching)\b", re.I)
URL_RE = re.compile(r"https?://[^\s)\]>\"'”]+", re.I)
MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_]{1,15})\b")
HASHTAG_RE = re.compile(r"#([A-Za-z][A-Za-z0-9_]{2,30})")
SOCIAL_DOMAINS = {"x.com", "twitter.com", "t.me", "discord.gg", "discord.com", "instagram.com", "youtube.com",
                  "youtu.be", "tiktok.com", "linktr.ee", "warpcast.com", "medium.com"}
MINT_PATH = re.compile(r"/(mint|claim|launchpad|drop|collect)(/|$|\?)", re.I)
GENERIC_TAGS = {"nft", "nfts", "freemint", "freemints", "nftdrop", "nftdrops", "nftcommunity", "nftgiveaway", "web3",
                "crypto", "solana", "solananft", "solananfts", "ethereum", "eth", "base", "basechain", "onbase",
                "polygon", "ordinals", "bitcoin", "btc", "nftart", "nftcollector", "mint", "airdrop", "giveaway",
                "freenft", "nftmint", "nftalpha", "alpha", "gm", "pfp", "art", "nftartist", "cnft", "cnfts",
                "bitcoinordinals", "inscriptions", "nftproject", "upcomingnft", "nftlaunch"}
NAME_STOPWORDS = {"free", "nft", "nfts", "the", "mint", "minting", "our", "gm", "huge", "today", "tomorrow", "new",
                  "upcoming", "alert", "alpha", "drop", "big", "announcing", "introducing", "official", "this", "a",
                  "an", "is", "now", "live", "public", "claim", "ordinals", "solana", "base", "ethereum", "polygon",
                  "breaking", "update", "reminder", "don't", "dont", "miss", "just", "hey", "fam", "wl", "allowlist",
                  "we", "it", "its", "mint:", "date", "it's", "check", "join", "get", "your", "my", "time", "here",
                  "rt", "all", "and", "for", "on", "at", "in", "of", "with", "to", "by"}


def normalize_name(name: str | None) -> str | None:
    if not name:
        return None
    n = name.lower()
    n = re.sub(r"\b(nfts?|official|collection|the|mint|free|genesis|club|project|xyz)\b", " ", n)
    n = re.sub(r"[^a-z0-9]", "", n)
    return n or None


def _clean_name(raw: str) -> str | None:
    words = [w for w in re.split(r"\s+", raw.strip(" -–—:|,.!\"'“”")) if w]
    while words and words[0].lower().strip(":!,") in NAME_STOPWORDS:
        words.pop(0)
    while words and words[-1].lower().strip(":!,") in NAME_STOPWORDS:
        words.pop()
    name = " ".join(words).strip(" -–—:|,.!")
    if not name or len(name) < 2 or len(name) > 40 or not re.search(r"[A-Za-z]", name):
        return None
    return name


def extract_name(text: str) -> tuple[str | None, str | None]:
    patterns = [
        (r"(?:introducing|announcing|meet|presenting)\s+[\"“]?([A-Z][\w'&.\- ]{1,40}?)[\"”]?(?=\s*[.!,:\n—–\-(]|\s+(?:is|are|a|an|—|on|free)\b|$)", "introducing"),
        (r"[\"“]([^\"”\n]{2,40})[\"”]", "quoted"),
        (r"free\s*[- ]?mint\s*[:—–\-]\s*((?:[A-Z][\w'’\-]*|[A-Z0-9]{2,})(?:\s+(?:[A-Z][\w'’\-]*|[A-Z0-9]{2,})){0,3})", "after_mint"),
        (r"((?:[A-Z][\w'’\-]*|[A-Z0-9]{2,})(?:\s+(?:[A-Z][\w'’\-]*|[A-Z0-9]{2,})){0,3})\s*(?:[—–:\-]\s*)?(?:free\s*[- ]?mint|is\s+(?:now\s+)?(?:live|minting)|mint\s+(?:is|date|starts|opens|goes|moved))", "before_mint"),
    ]
    for pat, source in patterns:
        for m in re.finditer(pat, text, re.I if source not in ("before_mint", "after_mint") else 0):
            name = _clean_name(m.group(1))
            if name and name.lower() not in NAME_STOPWORDS:
                return name, source
    for tag in HASHTAG_RE.findall(text):
        if tag.lower() not in GENERIC_TAGS:
            return tag, "hashtag"
    return None, None


def extract_links(post: Post, cfg_security: dict) -> dict:
    urls = list(dict.fromkeys(post.urls + [u.rstrip(".,!?") for u in URL_RE.findall(post.text)]))
    known = {k.lower(): v for k, v in (cfg_security.get("known_platforms") or {}).items()}
    out = {"website": None, "mint": None, "platform": None, "socials": [], "other": [], "all": urls}
    for url in urls:
        host = security.domain_of(url)
        if not host:
            continue
        if host in SOCIAL_DOMAINS or security.registrable(host) in SOCIAL_DOMAINS:
            out["socials"].append(url)
            continue
        platform = known.get(host) or known.get(security.registrable(host))
        path = urlparse(url).path
        if platform and out["mint"] is None and (MINT_PATH.search(path) or len(path) > 1):
            out["mint"], out["platform"] = url, platform
        elif not platform and MINT_PATH.search(path) and out["mint"] is None:
            out["mint"] = url
            if out["website"] is None:
                out["website"] = f"https://{host}"
        elif not platform and out["website"] is None:
            out["website"] = url
        else:
            out["other"].append(url)
    return out


def detect_chain(text: str, networks: dict, mint_platform: str | None) -> tuple[str | None, list[str]]:
    scores: dict[str, int] = {}
    for name, net in networks.items():
        score = 0
        for pat in net.get("strong_keywords", []) or []:
            if re.search(pat, text, re.I):
                score += 3
        for pat in net.get("keywords", []) or []:
            if re.search(pat, text, re.I):
                score += 1
        if score:
            scores[name] = score
    if mint_platform in ("Gamma", "UniSat", "ordinals.com") or re.search(r"magiceden\.\w+/ordinals", text, re.I):
        scores["bitcoin_ordinals"] = scores.get("bitcoin_ordinals", 0) + 2
    if mint_platform == "Tensor":
        scores["solana"] = scores.get("solana", 0) + 2
    if not scores:
        return None, []
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    # Base mints are priced in ETH, so "eth" alone shouldn't beat an explicit Base mention.
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None, [r[0] for r in ranked]
    return ranked[0][0], [r[0] for r in ranked]


def detect_project_handle(post: Post, name: str | None, aggregators: set[str], platforms: set[str]) -> tuple[str | None, bool]:
    author = post.author_username.lower()
    mentions = [m for m in MENTION_RE.findall(post.text) if m.lower() not in platforms and m.lower() != author]
    if author in aggregators:
        return (mentions[0] if mentions else None), False
    norm = normalize_name(name)
    if norm:
        for m in mentions:
            nm = normalize_name(m) or ""
            if nm and (nm in norm or norm in nm):
                if (normalize_name(post.author_username) or "") == nm:
                    break
                return m, False
        an = normalize_name(post.author_username) or ""
        if an and (an in norm or norm in an):
            return post.author_username, True
    if not mentions or SELF_CUES.search(post.text):
        return post.author_username, True
    return mentions[0], False


def extract(post: Post, networks: dict, cfg_security: dict, aggregators: set[str], platforms: set[str],
            min_followers: int = 100) -> Extraction:
    text = post.text
    reported_free = any(re.search(p, text, re.I) for p in FREE_PATTERNS)
    nft = bool(NFT_CONTEXT.search(text))
    paid = find_paid_price(text)
    relevant = reported_free and nft
    ex = Extraction(relevant=relevant)
    if not relevant:
        return ex
    ex.reported_free = True
    m = re.search(r"(?:free\s*[- ]?mint|free\s+claim|free\s+to\s+mint|(?<![\d.])0(?:\.0+)?\s*(?:eth|sol|btc|pol|matic|ape))", text, re.I)
    ex.price_text = m.group(0) if m else "free"
    if paid:
        ex.paid_price = f"{paid[0]} {paid[1]}"
        ex.risk_flags.append(f"post also mentions a non-zero price ({ex.paid_price}) — 'free' may be phase-limited or bait")

    ex.name, ex.name_source = extract_name(text)
    links = extract_links(post, cfg_security)
    ex.website_url, ex.mint_url, ex.mint_platform = links["website"], links["mint"], links["platform"]
    ex.other_links = links["socials"] + links["other"]
    ex.project_handle, ex.author_is_project = detect_project_handle(post, ex.name, aggregators, platforms)
    if not ex.name:
        if ex.project_handle:
            ex.name, ex.name_source = ex.project_handle, "handle"
        elif post.author_name:
            ex.name, ex.name_source = post.author_name, "author_name"

    enabled_networks = networks
    ex.chain, ex.chain_candidates = detect_chain(text, enabled_networks, ex.mint_platform)
    if ex.chain is None and len(ex.chain_candidates) > 1:
        ex.notes.append(f"chain ambiguous: {', '.join(ex.chain_candidates)}")

    ex.schedule = parse_schedule(text, post.created_at)
    for pat in SUPPLY_RES:
        sm = pat.search(text)
        if sm:
            try:
                ex.supply = int(re.sub(r"[,\s]", "", sm.group(1)))
            except ValueError:
                pass
            break
    for pat, label in ELIGIBILITY:
        if re.search(pat, text, re.I) and label not in ex.eligibility:
            ex.eligibility.append(label)

    for url in links["all"]:
        ex.risk_flags.extend(security.url_flags(url, cfg_security))
    ex.risk_flags.extend(security.text_flags(text))
    ex.risk_flags.extend(security.handle_flags(post.author_username, ex.project_handle, post.author_followers,
                                               min_followers, ex.author_is_project))
    ex.risk_flags = list(dict.fromkeys(ex.risk_flags))
    ex.confidence = score(ex, nft, post)
    return ex


def score(ex: Extraction, nft_context: bool, post: Post) -> float:
    s = 0.2
    s += 0.15 if ex.reported_free else 0
    s += 0.05 if nft_context else 0
    s += 0.15 if ex.chain else 0
    s += 0.15 if ex.schedule.precision == "exact" or ex.schedule.live_signal else (0.05 if ex.schedule.precision == "day" else 0)
    s += 0.1 if ex.author_is_project else 0
    s += 0.1 if ex.mint_platform else (0.05 if ex.mint_url else 0)
    s += 0.05 if ex.website_url else 0
    s += 0.05 if ex.name_source in ("introducing", "quoted", "before_mint", "after_mint") else 0
    high = sum(1 for f in ex.risk_flags if security.is_high_risk(f))
    s -= 0.2 * min(high, 2)
    if post.author_followers is not None and post.author_followers < 100:
        s -= 0.1
    return round(max(0.0, min(1.0, s)), 2)


def confidence_label(value: float) -> str:
    return "high" if value >= 0.7 else "medium" if value >= 0.45 else "low"
