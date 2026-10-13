"""Official-source verification: fetch the project's website / mint page (no JS, no wallet,
size-capped) and look for price evidence and a link back to the project's X account.

Limitations (stated in alerts): many mint pages render client-side, so a page that shows
nothing useful is "inconclusive", not "not free". Verification lowers risk; it does not prove safety."""
from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from .extract import FREE_PATTERNS, find_paid_price

log = logging.getLogger("radar.verify")


@dataclass
class VerifyResult:
    url: str
    ok: bool
    confirms_free: bool = False
    paid_price: str | None = None
    links_back_to_handle: bool = False
    note: str = ""
    flags: list[str] = field(default_factory=list)


def _is_public_host(host: str) -> bool:
    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        return True
    except (socket.gaierror, ValueError):
        return False


async def fetch_and_check(http: httpx.AsyncClient, url: str, handle: str | None, *, max_bytes: int,
                          timeout: float, check_host: bool = True) -> VerifyResult:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return VerifyResult(url, False, note="unsupported URL")
    if check_host and not await asyncio.to_thread(_is_public_host, parsed.hostname):
        return VerifyResult(url, False, note="host does not resolve to a public address")
    try:
        async with http.stream("GET", url, timeout=timeout, follow_redirects=True,
                               headers={"User-Agent": "FreeMintRadar/1.0 (+verification; no wallet)"}) as resp:
            if resp.status_code >= 400:
                return VerifyResult(url, False, note=f"HTTP {resp.status_code}")
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= max_bytes:
                    break
            final_host = resp.url.host
    except httpx.HTTPError as exc:
        return VerifyResult(url, False, note=f"fetch failed: {exc.__class__.__name__}")
    raw = b"".join(chunks).decode("utf-8", errors="replace")
    text = html.unescape(re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", raw, flags=re.S | re.I))
    text = re.sub(r"\s+", " ", text)
    result = VerifyResult(url, True)
    if final_host and parsed.hostname and final_host.lower() != parsed.hostname.lower():
        result.flags.append(f"{parsed.hostname} redirects to {final_host}")
    result.confirms_free = any(re.search(p, text, re.I) for p in FREE_PATTERNS)
    paid = find_paid_price(text)
    if paid:
        result.paid_price = f"{paid[0]} {paid[1]}"
    if handle:
        result.links_back_to_handle = bool(re.search(rf"(?:x|twitter)\.com/{re.escape(handle)}\b", raw, re.I))
    if not result.confirms_free and not result.paid_price:
        result.note = "page loaded but showed no price (often rendered by JavaScript) — inconclusive"
    return result
