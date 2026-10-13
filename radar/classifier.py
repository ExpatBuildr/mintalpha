"""Optional LLM gap-filler (Anthropic Messages API).

Design rules:
  * Rules decide relevance first; the LLM can ADD relevance (catch posts rules missed)
    and fill missing name/chain/supply/eligibility. It cannot remove a rule-detected project.
  * The LLM never supplies dates, mint URLs or websites — those must come from source text/links,
    so nothing is invented. Everything it contributes is stored as 'llm' evidence.
  * Any failure (no key, timeout, bad JSON) silently falls back to rules only.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from .models import Extraction, Post

log = logging.getLogger("radar.classifier")

PROMPT = """You label X posts about NFT mints. Reply with ONLY a JSON object, no prose:
{{"is_free_mint_announcement": true|false, "project_name": string|null, "chain": one of {chains} or null,
 "supply": integer|null, "eligibility": [strings], "is_scam_likely": true|false, "reason": short string}}
Use null when the post does not say. Do not guess dates or links.

Post by @{author}:
\"\"\"{text}\"\"\""""


class LLMClassifier:
    def __init__(self, api_key: str, model: str, networks: list[str], max_tokens: int = 400,
                 http: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self.model = model
        self.networks = networks
        self.max_tokens = max_tokens
        self.http = http or httpx.AsyncClient(timeout=20)

    async def classify(self, post: Post) -> dict | None:
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": PROMPT.format(
                chains=json.dumps(self.networks), author=post.author_username, text=post.text[:1500])}],
        }
        try:
            resp = await self.http.post("https://api.anthropic.com/v1/messages", json=body, headers={
                "x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
            if resp.status_code != 200:
                log.warning("llm_http_error", extra={"status": resp.status_code, "body": resp.text[:200]})
                return None
            text = "".join(b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text")
            m = re.search(r"\{.*\}", text, re.S)
            return json.loads(m.group(0)) if m else None
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            log.warning("llm_failed", extra={"error": str(exc)})
            return None


def merge_llm(ex: Extraction, result: dict | None, networks: list[str]) -> Extraction:
    if not result:
        return ex
    if result.get("is_free_mint_announcement") and not ex.relevant:
        ex.relevant = True
        ex.reported_free = True
        ex.price_text = ex.price_text or "free (LLM-classified)"
        ex.notes.append("relevance added by LLM classifier")
    if not ex.relevant:
        return ex
    name = result.get("project_name")
    if isinstance(name, str) and name.strip() and ex.name_source in (None, "handle", "author_name", "hashtag"):
        ex.name, ex.name_source = name.strip()[:60], "llm"
    chain = result.get("chain")
    if not ex.chain and chain in networks:
        ex.chain = chain
        ex.notes.append(f"chain '{chain}' inferred by LLM")
    if ex.supply is None and isinstance(result.get("supply"), int) and result["supply"] > 0:
        ex.supply = result["supply"]
    for item in result.get("eligibility") or []:
        if isinstance(item, str) and item not in ex.eligibility:
            ex.eligibility.append(item[:40])
    if result.get("is_scam_likely"):
        ex.risk_flags.append(f"LLM classifier thinks this looks like a scam: {str(result.get('reason'))[:120]}")
    return ex
