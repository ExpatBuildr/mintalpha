"""Ingestion: post -> extraction -> dedupe/merge -> verification -> alert outbox -> reminder sync."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime

import httpx

from . import messages, security
from .classifier import LLMClassifier, merge_llm
from .clock import Clock, from_iso, to_iso
from .config import Settings
from .db import Database
from .extract import extract, normalize_name
from .models import PRICE_RANK, RESOLVED, Extraction, Post, PriceStatus, Status
from .verify import fetch_and_check

log = logging.getLogger("radar.pipeline")
PRECISION_RANK = {"unknown": 0, "day": 1, "exact": 2}


@dataclass
class IngestResult:
    outcome: str  # duplicate | irrelevant | filtered | created | updated | unchanged
    project_id: int | None = None
    changes: list[str] | None = None


def derive_status(current: Status, launch_at: datetime | None, precision: str, live_signal: bool,
                  ended_signal: bool, now: datetime) -> Status:
    if current in RESOLVED:
        return current
    if ended_signal:
        return Status.ENDED
    if live_signal:
        return Status.LIVE
    if current == Status.ENDED:
        return Status.ENDED
    if precision == "exact" and launch_at:
        if launch_at <= now:
            return Status.LIVE
        return Status.UPCOMING
    if current == Status.LIVE:
        return Status.LIVE
    if precision == "day":
        return Status.UPCOMING
    return Status.DATE_UNKNOWN


def project_keys(ex: Extraction, cfg_security: dict) -> list[str]:
    keys = []
    if ex.project_handle:
        keys.append(f"handle:{ex.project_handle.lower()}")
    known = {k.lower() for k in (cfg_security.get("known_platforms") or {})}
    if ex.website_url:
        host = security.domain_of(ex.website_url)
        if host and security.registrable(host) not in known and host not in known:
            keys.append(f"domain:{security.registrable(host)}")
    if ex.mint_url:
        keys.append("mint:" + ex.mint_url.split("?")[0].rstrip("/").lower().replace("http://", "https://"))
    norm = normalize_name(ex.name)
    if norm and len(norm) >= 4 and ex.name_source not in ("author_name",):
        keys.append(f"name:{norm}")
    return keys


class Pipeline:
    def __init__(self, db: Database, settings: Settings, clock: Clock, *,
                 classifier: LLMClassifier | None = None, http: httpx.AsyncClient | None = None,
                 on_project_changed=None):
        self.db = db
        self.settings = settings
        self.clock = clock
        self.classifier = classifier
        self.http = http
        self.on_project_changed = on_project_changed  # callback(project_id) -> reminders sync
        cfg = settings.config
        self.cfg_security = cfg.get("security") or {}
        self.aggregators = {a.lower() for a in cfg.get("discovery.aggregator_accounts") or []}
        self.platforms = {a.lower() for a in cfg.get("discovery.platform_handles") or []}

    # -- helpers ---------------------------------------------------------------
    def _enabled_networks(self) -> dict:
        return {k: v for k, v in self.settings.networks().items() if self.settings.network_enabled(k)}

    def fee_note(self, chain: str | None) -> str | None:
        return (self.settings.networks().get(chain) or {}).get("fee_note") if chain else None

    def chain_label(self, chain: str | None) -> str | None:
        return (self.settings.networks().get(chain) or {}).get("label") if chain else None

    # -- main entry ------------------------------------------------------------
    async def ingest(self, post: Post, raw: dict | None = None) -> IngestResult:
        now = self.clock.now()
        if self.db.post_exists(post.id):
            return IngestResult("duplicate")
        all_networks = self.settings.networks()
        ex = extract(post, all_networks, self.cfg_security, self.aggregators, self.platforms,
                     int(self.settings.get("security.min_author_followers", 100)))
        if self.classifier is not None:
            result = await self.classifier.classify(post)
            ex = merge_llm(ex, result, list(all_networks))
        if not ex.relevant:
            self.db.insert_post(post, None, False, now, raw)
            return IngestResult("irrelevant")
        if ex.chain and not self.settings.network_enabled(ex.chain):
            self.db.insert_post(post, None, False, now, raw)
            log.info("filtered_disabled_network", extra={"post_id": post.id, "chain": ex.chain})
            return IngestResult("filtered")

        keys = project_keys(ex, self.cfg_security)
        with self.db.tx():
            if self.db.post_exists(post.id):  # re-check inside the transaction
                return IngestResult("duplicate")
            pid = self.db.find_project_by_keys(keys)
            if pid is None:
                pid = self._create(ex, post, keys, now)
                self.db.insert_post(post, pid, True, now, raw)
                outcome, changes = "created", []
            else:
                changes = self._merge(pid, ex, post, keys, now)
                self.db.insert_post(post, pid, True, now, raw)
                outcome = "updated" if changes else "unchanged"

        await self._maybe_verify(pid)

        with self.db.tx():
            p = self.db.get_project(pid)
            if outcome == "created":
                self.db.enqueue_alert(pid, "discovery", f"discovery:{pid}",
                                      messages.discovery(p, self.settings.display_tz(), now,
                                                         self.fee_note(p["chain"]), self.chain_label(p["chain"])), now)
            elif changes and Status(p["status"]) not in RESOLVED:
                digest = hashlib.sha1(json.dumps(sorted(changes)).encode()).hexdigest()[:12]
                self.db.enqueue_alert(pid, "update", f"update:{pid}:{digest}",
                                      messages.update(p, changes, self.settings.display_tz(), now), now)
        if self.on_project_changed:
            self.on_project_changed(pid)
        log.info("ingested", extra={"post_id": post.id, "outcome": outcome, "project_id": pid, "changes": changes})
        return IngestResult(outcome, pid, changes)

    # -- create / merge ---------------------------------------------------------
    def _source_kind(self, ex: Extraction, project_handle: str | None) -> str:
        if ex.author_is_project and (project_handle is None or (ex.project_handle or "").lower() == project_handle.lower()):
            return "project_account"
        return "third_party"

    def _create(self, ex: Extraction, post: Post, keys: list[str], now: datetime) -> int:
        kind = self._source_kind(ex, None)
        s = ex.schedule
        status = derive_status(Status.DISCOVERED, s.launch_at, s.precision, s.live_signal, s.ended_signal, now)
        notes = list(ex.notes) + [f"schedule: {a}" for a in s.assumptions]
        if s.tba:
            notes.append("source says date TBA/soon")
        fields = {
            "name": ex.name or f"Unnamed project via @{post.author_username}",
            "norm_name": normalize_name(ex.name),
            "x_handle": ex.project_handle,
            "chain": ex.chain,
            "status": Status.DISCOVERED.value,
            "verification": "project_account" if kind == "project_account" else "unverified",
            "price_text": ex.price_text,
            "price_status": (PriceStatus.PROJECT_CLAIMED_FREE if kind == "project_account" else PriceStatus.CLAIMED_FREE).value,
            "launch_at": to_iso(s.launch_at), "launch_date": s.launch_date, "launch_precision": s.precision,
            "launch_text": s.raw, "launch_tz": s.tz_name,
            "supply": ex.supply, "eligibility": ex.eligibility,
            "website_url": ex.website_url, "mint_url": ex.mint_url, "mint_platform": ex.mint_platform,
            "first_source_url": post.url, "confidence": ex.confidence, "risk_flags": ex.risk_flags, "notes": notes,
        }
        pid = self.db.create_project(fields, now)
        self.db.add_keys(pid, keys)
        if status != Status.DISCOVERED:
            self.db.set_status(pid, status, "initial classification", now)
        self._evidence(pid, ex, post, kind, now)
        return pid

    def _evidence(self, pid: int, ex: Extraction, post: Post, kind: str, now: datetime) -> None:
        snip = post.text[:300]
        add = lambda f, v: self.db.add_evidence(pid, f, v, post.url, kind, snip, now)  # noqa: E731
        add("price", ex.price_text)
        add("name", ex.name)
        add("chain", ex.chain)
        add("launch", to_iso(ex.schedule.launch_at) or ex.schedule.launch_date)
        add("launch_text", ex.schedule.raw)
        add("mint_url", ex.mint_url)
        add("website", ex.website_url)
        add("supply", ex.supply)
        add("eligibility", ex.eligibility)
        if ex.schedule.live_signal:
            add("status", "live (post says live)")
        if ex.schedule.ended_signal:
            add("status", "ended (post says ended)")
        if ex.paid_price:
            add("paid_price", ex.paid_price)

    def _merge(self, pid: int, ex: Extraction, post: Post, keys: list[str], now: datetime) -> list[str]:
        p = self.db.get_project(pid)
        kind = self._source_kind(ex, p["x_handle"])
        auth = kind == "project_account"
        upd: dict = {}
        changes: list[str] = []
        notes = list(p["notes"])

        if not p["x_handle"] and ex.project_handle:
            upd["x_handle"] = ex.project_handle
        if p["name"].startswith("Unnamed project") or (p["name"] == p["x_handle"] and ex.name_source in ("introducing", "quoted", "before_mint", "after_mint")):
            if ex.name and ex.name != p["name"]:
                upd["name"], upd["norm_name"] = ex.name, normalize_name(ex.name)
        if ex.chain and ex.chain != p["chain"]:
            if not p["chain"] or auth:
                upd["chain"] = ex.chain
                changes.append(f"chain: {p['chain'] or 'unknown'} → {ex.chain}")
            else:
                notes.append(f"conflicting chain claim '{ex.chain}' from {post.url}")

        new_ps = PriceStatus.PROJECT_CLAIMED_FREE if auth else PriceStatus.CLAIMED_FREE
        old_ps = PriceStatus(p["price_status"])
        if old_ps != PriceStatus.PAID and PRICE_RANK[new_ps] > PRICE_RANK[old_ps]:
            upd["price_status"] = new_ps.value
            changes.append(f"price evidence: {new_ps.value.replace('_', ' ')}")
        if auth and p["verification"] == "unverified":
            upd["verification"] = "project_account"

        s = ex.schedule
        old_rank, new_rank = PRECISION_RANK[p["launch_precision"]], PRECISION_RANK[s.precision]
        old_launch = from_iso(p["launch_at"])
        take = new_rank > old_rank or (auth and new_rank == old_rank and new_rank > 0 and
                                        (to_iso(s.launch_at) != p["launch_at"] or s.launch_date != p["launch_date"]))
        if take:
            upd.update({"launch_at": to_iso(s.launch_at), "launch_date": s.launch_date, "launch_precision": s.precision,
                        "launch_text": s.raw, "launch_tz": s.tz_name})
            if old_launch and s.launch_at and old_launch != s.launch_at:
                changes.append(f"launch time changed: {old_launch:%b %d %H:%M} UTC → {s.launch_at:%b %d %H:%M} UTC")
            elif s.precision == "exact":
                changes.append(f"exact launch time found: {s.launch_at:%b %d %H:%M} UTC")
            else:
                changes.append(f"launch date: {s.launch_date}")
            notes.extend(f"schedule: {a}" for a in s.assumptions)
        elif new_rank > 0 and (to_iso(s.launch_at) != p["launch_at"] or s.launch_date != p["launch_date"]):
            notes.append(f"different schedule claimed by third party ({s.raw}) at {post.url} — kept project-stated time")

        for field, value in (("website_url", ex.website_url), ("mint_url", ex.mint_url)):
            if value and value != p[field] and (not p[field] or auth):
                upd[field] = value
                if field == "mint_url":
                    upd["mint_platform"] = ex.mint_platform
                changes.append(f"{'mint link' if field == 'mint_url' else 'website'}: {value}")
        if ex.supply and not p["supply"]:
            upd["supply"] = ex.supply
        new_elig = [e for e in ex.eligibility if e not in p["eligibility"]]
        if new_elig:
            upd["eligibility"] = p["eligibility"] + new_elig
            if any(e in ("allowlist", "whitelist", "guaranteed (GTD)") for e in new_elig):
                changes.append(f"eligibility: {', '.join(new_elig)}")
        new_flags = [f for f in ex.risk_flags if f not in p["risk_flags"]]
        if new_flags:
            upd["risk_flags"] = p["risk_flags"] + new_flags
            if any(security.is_high_risk(f) for f in new_flags):
                changes.append("new security warning: " + new_flags[0])
        if ex.confidence > (p["confidence"] or 0):
            upd["confidence"] = ex.confidence
        if notes != p["notes"]:
            upd["notes"] = notes[-20:]
        self.db.update_project(pid, upd, now)
        self.db.add_keys(pid, keys)
        self._evidence(pid, ex, post, kind, now)

        p2 = self.db.get_project(pid)
        cur = Status(p2["status"])
        new = derive_status(cur, from_iso(p2["launch_at"]), p2["launch_precision"], s.live_signal, s.ended_signal, now)
        if new != cur:
            try:
                self.db.set_status(pid, new, f"evidence from {post.url}", now)
                if new == Status.ENDED:
                    changes.append("mint reported ended / minted out")
                elif new == Status.UPCOMING and cur == Status.DATE_UNKNOWN:
                    pass  # covered by the launch change line
            except ValueError as exc:
                log.warning("status_transition_rejected", extra={"project_id": pid, "error": str(exc)})
        return changes

    # -- verification ---------------------------------------------------------------
    async def _maybe_verify(self, pid: int) -> None:
        if not self.http or not self.settings.get("verification.fetch_official_sites", False):
            return
        p = self.db.get_project(pid)
        if any(security.is_high_risk(f) for f in p["risk_flags"]):
            return
        if p["verification"] == "site_confirms_free":
            return
        targets = [u for u in (p["website_url"], p["mint_url"]) if u]
        if not targets:
            return
        now = self.clock.now()
        upd: dict = {}
        flags = list(p["risk_flags"])
        for url in dict.fromkeys(targets):
            res = await fetch_and_check(self.http, url, p["x_handle"],
                                        max_bytes=int(self.settings.get("verification.max_bytes", 500000)),
                                        timeout=float(self.settings.get("verification.timeout_seconds", 10)))
            if not res.ok:
                continue
            flags += [f for f in res.flags if f not in flags]
            if res.confirms_free and not res.paid_price:
                upd["price_status"] = PriceStatus.CONFIRMED_FREE.value
                upd["verification"] = "site_confirms_free"
                self.db.add_evidence(pid, "price", "free (official page)", url, "official_site", None, now)
            elif res.paid_price and not res.confirms_free:
                upd["price_status"] = PriceStatus.PAID.value
                flags.append(f"official page shows a non-zero price ({res.paid_price})")
                self.db.add_evidence(pid, "paid_price", res.paid_price, url, "official_site", None, now)
            if res.links_back_to_handle and upd.get("verification") != "site_confirms_free":
                upd.setdefault("verification", "site_crosslinked")
            if res.note:
                self.db.add_evidence(pid, "verification_note", res.note, url, "official_site", None, now)
        if flags != p["risk_flags"]:
            upd["risk_flags"] = flags
        with self.db.tx():
            self.db.update_project(pid, upd, now)
