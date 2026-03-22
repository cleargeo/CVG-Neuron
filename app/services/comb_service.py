"""
CVG Neuron AI Orchestration System — CVG COMB Service
Version: 2.0.0 | Clearview Geographic LLC

Client for the CVG COMB tiered memory management system.

COMB Memory Tiers:
  pollenstore   — Cold storage (long-term archive)
  bithive       — Hot storage (rapid recall)
  waxcell       — Immutable audit log
  entangle      — Distributed sync across Queens
  quantumcell   — Predictive prefetch
  neurocache    — Local volatile (managed by NeuroCache directly)
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.logger import get_logger

log = get_logger("comb-service")


# Memory tier → endpoint mapping
TIER_ENDPOINTS: Dict[str, str] = {
    "pollenstore": settings.comb_pollen_store,
    "bithive":     settings.comb_bit_hive,
    "waxcell":     settings.comb_wax_cell,
    "default":     settings.comb_endpoint,
}


class CombService:
    """
    CVG COMB tiered memory integration.

    Provides read/write/delete access to the 6-tier COMB memory system.
    NeuroCache (Level 6) is managed directly by the orchestrator.
    This service handles Levels 1–5 via the remote COMB API.
    """

    def __init__(self) -> None:
        self._timeout = 15

    # ── Health ────────────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Ping COMB endpoint. Returns True if reachable."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    settings.comb_endpoint.replace("/store", "/health")
                )
                return resp.status_code < 500
        except Exception as exc:
            log.debug("COMB health check failed", error=str(exc))
            return False

    # ── Write ─────────────────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=5))
    async def store(
        self,
        key: str,
        value: Any,
        tier: str = "bithive",
        ttl: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Store a value in the specified COMB memory tier.

        Args:
            key: Storage key
            value: Value to store (must be JSON-serializable)
            tier: Memory tier (pollenstore | bithive | waxcell | entangle)
            ttl: Optional TTL in seconds (0 = permanent)
            metadata: Optional metadata to store alongside the value

        Returns:
            COMB store response
        """
        endpoint = TIER_ENDPOINTS.get(tier, TIER_ENDPOINTS["default"])
        payload = {
            "key": key,
            "value": value,
            "tier": tier,
            "ttl": ttl or 0,
            "metadata": metadata or {},
            "neuron_id": settings.neuron_id,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    endpoint,
                    json=payload,
                    headers={"X-Neuron-ID": settings.neuron_id},
                )
                resp.raise_for_status()
                log.debug("COMB store successful", key=key, tier=tier)
                return resp.json()
        except httpx.HTTPError as exc:
            log.warning("COMB store failed", key=key, tier=tier, error=str(exc))
            return {"success": False, "error": str(exc), "key": key}

    # ── Read ──────────────────────────────────────────────────────────────────

    async def retrieve(
        self,
        key: str,
        tier: str = "bithive",
    ) -> Optional[Any]:
        """
        Retrieve a value from the specified COMB memory tier.

        Returns:
            Stored value, or None if not found
        """
        endpoint = TIER_ENDPOINTS.get(tier, TIER_ENDPOINTS["default"])
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    endpoint.replace("/store", f"/retrieve/{key}"),
                    headers={"X-Neuron-ID": settings.neuron_id},
                )
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                data = resp.json()
                log.debug("COMB retrieve successful", key=key, tier=tier)
                return data.get("value")
        except httpx.HTTPError as exc:
            log.warning("COMB retrieve failed", key=key, tier=tier, error=str(exc))
            return None

    async def retrieve_multi(
        self,
        keys: list[str],
        tier: str = "bithive",
    ) -> Dict[str, Any]:
        """Batch retrieve multiple keys from COMB."""
        endpoint = TIER_ENDPOINTS.get(tier, TIER_ENDPOINTS["default"])
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    endpoint.replace("/store", "/retrieve/batch"),
                    json={"keys": keys, "tier": tier},
                    headers={"X-Neuron-ID": settings.neuron_id},
                )
                resp.raise_for_status()
                return resp.json().get("results", {})
        except Exception as exc:
            log.warning("COMB batch retrieve failed", error=str(exc))
            return {}

    # ── Delete ────────────────────────────────────────────────────────────────

    async def delete(self, key: str, tier: str = "bithive") -> bool:
        """Delete a key from COMB. Returns True on success."""
        endpoint = TIER_ENDPOINTS.get(tier, TIER_ENDPOINTS["default"])
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.delete(
                    endpoint.replace("/store", f"/{key}"),
                    headers={"X-Neuron-ID": settings.neuron_id},
                )
                return resp.status_code in (200, 204)
        except Exception as exc:
            log.warning("COMB delete failed", key=key, error=str(exc))
            return False

    # ── Search ────────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        tier: str = "bithive",
        limit: int = 10,
    ) -> list[Dict[str, Any]]:
        """
        Search COMB memory by query string (semantic or key-based).
        Returns matched records.
        """
        base = settings.comb_endpoint.rsplit("/store", 1)[0]
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{base}/search",
                    json={"query": query, "tier": tier, "limit": limit},
                    headers={"X-Neuron-ID": settings.neuron_id},
                )
                resp.raise_for_status()
                return resp.json().get("results", [])
        except Exception as exc:
            log.warning("COMB search failed", query=query, error=str(exc))
            return []

    # ── Tier info ─────────────────────────────────────────────────────────────

    async def get_tier_stats(self) -> Dict[str, Any]:
        """Fetch usage statistics for all COMB tiers."""
        base = settings.comb_endpoint.rsplit("/store", 1)[0]
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    f"{base}/stats",
                    headers={"X-Neuron-ID": settings.neuron_id},
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            log.debug("COMB stats unavailable", error=str(exc))
            return {
                "tiers": ["pollenstore", "bithive", "waxcell", "entangle"],
                "status": "unreachable",
            }
