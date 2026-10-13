"""Heuristic risk flags. These reduce risk; they never prove a link is safe.
The radar itself never touches wallets, keys, or transactions."""
from __future__ import annotations

import re
from urllib.parse import urlparse

DRAINER_PHRASES = [
    (r"\bseed\s*phrase\b|\bsecret\s+recovery\s+phrase\b|\bprivate\s+key\b|\bmnemonic\b", "asks about seed phrase / private key — never share these"),
    (r"\b(validate|verify|sync|rectify|restore)\s+(your\s+)?wallet\b", "'validate/sync your wallet' wording is a classic drainer pattern"),
    (r"\bsetapprovalforall\b|\bincrease\s+allowance\b|\bpermit2?\s+signature\b|\bsign\s+(the\s+)?permit\b", "mentions approval/permit signatures — these can hand over your assets"),
    (r"\bsend\s+\d*\.?\d+\s*(eth|sol|btc|usdc|usdt)\b.{0,40}\b(receive|get|claim)\b", "asks you to send funds to receive something"),
    (r"\bconnect\s+(your\s+)?wallet\s+to\s+(claim|receive|verify)\b", "'connect wallet to claim' — common in fake airdrops; verify the domain first"),
    (r"\bonly\s+\d+\s*(min|mins|minutes|hours?)\s+left\b|\bact\s+fast\b|\bhurry\b", "urgency pressure language"),
    (r"\bairdrop\s+(is\s+)?(live|claim)\b.{0,60}\bfree\s+(nft|mint)\b|\bclaim\s+your\s+(free\s+)?airdrop\b", "generic airdrop-claim wording"),
]


def domain_of(url: str) -> str | None:
    try:
        host = urlparse(url if "://" in url else f"https://{url}").hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def registrable(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _lev(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


_LEET = str.maketrans({"0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t", "$": "s"})


def url_flags(url: str, cfg_security: dict) -> list[str]:
    flags: list[str] = []
    host = domain_of(url)
    if not host:
        return ["unparseable link"]
    if url.lower().startswith("http://"):
        flags.append(f"{host}: not HTTPS")
    shorteners = set(cfg_security.get("shortener_domains", []))
    if host in shorteners or registrable(host) in shorteners:
        flags.append(f"{host}: shortened URL hides the real destination")
    if host == "t.co":
        flags.append("t.co link could not be expanded")
    if "xn--" in host:
        flags.append(f"{host}: punycode domain (possible look-alike characters)")
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host):
        flags.append(f"{host}: raw IP address")
    tld = host.rsplit(".", 1)[-1]
    if tld in set(cfg_security.get("risky_tlds", [])):
        flags.append(f"{host}: TLD .{tld} is frequently used by scam sites")
    known = {d.lower() for d in (cfg_security.get("known_platforms") or {})}
    reg = registrable(host)
    if reg not in known and host not in known:
        label = reg.split(".")[0]
        norm = label.translate(_LEET).replace("-", "")
        for brand in cfg_security.get("protected_brands", []):
            if brand in norm and norm != brand:
                flags.append(f"{host}: uses brand name '{brand}' but is not the official {brand} domain")
                break
            if norm != label and norm == brand:
                flags.append(f"{host}: look-alike of '{brand}' using substituted characters")
                break
            if 0 < _lev(norm, brand) <= (1 if len(brand) <= 5 else 2) and len(norm) >= 4:
                flags.append(f"{host}: looks similar to '{brand}' (possible typosquat)")
                break
    return flags


def text_flags(text: str) -> list[str]:
    flags = []
    for pattern, message in DRAINER_PHRASES:
        if re.search(pattern, text, re.I):
            flags.append(message)
    return flags


def handle_flags(author: str, project_handle: str | None, author_followers: int | None,
                 min_followers: int, author_is_project: bool) -> list[str]:
    flags = []
    if author_followers is not None and author_followers < min_followers:
        flags.append(f"@{author} has only {author_followers} followers")
    if project_handle and not author_is_project and author.lower() != project_handle.lower():
        a, p = author.lower(), project_handle.lower()
        if (p in a or a in p or _lev(a, p) <= 2) and a != p:
            flags.append(f"@{author} resembles @{project_handle} — possible impersonation")
    return flags


HIGH_RISK_MARKERS = ("seed phrase", "drainer", "approval", "send funds", "look-alike", "typosquat",
                     "impersonation", "punycode", "not the official", "scam", "non-zero price")


def is_high_risk(flag: str) -> bool:
    f = flag.lower()
    return any(m in f for m in HIGH_RISK_MARKERS)
