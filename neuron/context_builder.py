"""
CVG Neuron — Context Builder
(c) Clearview Geographic, LLC — Proprietary

Pulls live telemetry from all four CVG support engines and builds
structured context strings for LLM analysis.

Engines:
  - Container Engine  : http://cvg-container-v1:8091
  - Git Engine        : http://cvg-gitengine-v1:8092
  - DNS Engine        : http://cvg-dns-v1:8094
  - Audit Engine      : http://cvg-audit-results-v1:8096
"""

from __future__ import annotations

import os
import json
import asyncio
import logging
from datetime import datetime
from typing import Any, Optional

import httpx

logger = logging.getLogger("cvg.neuron.context")

# Engine URLs — using actual container names from each engine's docker-compose.yml
# Git Engine:       container=cvg-git-engine,        port=8092
# DNS Engine:       container=cvg-dns-engine,         port=8094
# Support Engine:   container=cvg-support-engine,     port=8091
# Audit Results:    container=cvg-audit-results-api,  port=8001 (separate network — use host IP)
#
# Hive-0 Queen Nodes (per CVG_NETWORK_STANDARD.md 2026-03-17):
#   QUEEN-11 Proxmox : 10.10.10.56:8006  QUEEN-11 iDRAC:  10.10.10.50:443
#   QUEEN-12 DS1823+ : 10.10.10.53:5000  QUEEN-20 DS3622: 10.10.10.67:5000
#   QUEEN-30 DS418   : 10.10.10.71:5000  QUEEN-21 Terra:  10.10.10.57:8181
#   QUEEN-10 ESXi    : 10.10.10.61:443   QUEEN-10 iLO5:   10.10.10.58:443
#   QUEEN-10 TrueNAS : 10.10.10.100:80   FortiGate:       10.10.10.1:443
GIT_ENGINE_URL       = os.getenv("GIT_ENGINE_URL",
                           "http://cvg-git-engine:8092")
DNS_ENGINE_URL       = os.getenv("DNS_ENGINE_URL",
                           "http://cvg-dns-engine:8094")
CONTAINER_ENGINE_URL = os.getenv("CONTAINERIZATION_ENGINE_URL",
                           os.getenv("CONTAINER_ENGINE_URL",
                               "http://cvg-support-engine:8091"))
# Audit VM is on a separate Docker network (cvg_audit_net), access via host IP or URL override
AUDIT_ENGINE_URL     = os.getenv("AUDIT_VM_URL",
                           os.getenv("AUDIT_ENGINE_URL",
                               "http://10.10.10.220:8001"))
CVG_INTERNAL_KEY     = os.getenv("CVG_INTERNAL_KEY", "cvg-internal-2026")

FETCH_TIMEOUT = httpx.Timeout(connect=4.0, read=15.0, write=5.0, pool=4.0)

INTERNAL_HEADERS = {
    "X-CVG-Key": CVG_INTERNAL_KEY,
    "User-Agent": "cvg-neuron/1.0",
}


async def _fetch(client: httpx.AsyncClient, url: str, label: str) -> dict:
    """Fetch JSON from an engine endpoint, returning a result dict."""
    try:
        resp = await client.get(url, headers=INTERNAL_HEADERS, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
        return {"source": label, "status": "ok", "data": resp.json()}
    except httpx.ConnectError:
        logger.warning("[context] %s unreachable at %s", label, url)
        return {"source": label, "status": "unreachable", "data": None}
    except httpx.TimeoutException:
        logger.warning("[context] %s timed out at %s", label, url)
        return {"source": label, "status": "timeout", "data": None}
    except Exception as exc:
        logger.warning("[context] %s error: %s", label, exc)
        return {"source": label, "status": "error", "data": None, "error": str(exc)}


async def fetch_git_context(client: httpx.AsyncClient) -> dict:
    """Pull summary data from the Git Engine (GET /api/summary)."""
    return await _fetch(client, f"{GIT_ENGINE_URL}/api/summary", "git_engine")


async def fetch_dns_context(client: httpx.AsyncClient) -> dict:
    """Pull status from the DNS Engine (GET /api/status — richer than /api/health)."""
    return await _fetch(client, f"{DNS_ENGINE_URL}/api/status", "dns_engine")


async def fetch_container_context(client: httpx.AsyncClient) -> dict:
    """
    Pull deep container visibility from the Containerization Engine.
    Fetches both the static summary (/api/summary) and live container state
    (/api/containers/live — real-time SSH poll of all Docker hosts).
    """
    summary_res = await _fetch(client, f"{CONTAINER_ENGINE_URL}/api/summary", "container_engine")
    live_res    = await _fetch(client, f"{CONTAINER_ENGINE_URL}/api/containers/live", "container_live")
    telemetry_res = await _fetch(client, f"{CONTAINER_ENGINE_URL}/api/telemetry", "container_telemetry")

    # Merge all container data into one rich result
    merged: dict = {"source": "container_engine", "status": summary_res["status"]}
    data: dict = {}
    if summary_res.get("data"):
        data["summary"] = summary_res["data"]
    if live_res.get("data"):
        live = live_res["data"]
        data["live"] = {
            "summary":   live.get("summary", {}),
            "hosts":     live.get("hosts", []),
            "containers": [
                {k: v for k, v in c.items() if k not in ("id", "command")}
                for c in live.get("containers", [])
            ],
        }
    if telemetry_res.get("data"):
        tel = telemetry_res["data"]
        data["telemetry_summary"] = tel.get("summary", {})
        data["telemetry_nodes"]   = [
            {k: v for k, v in n.items() if k in (
                "node_id", "hostname", "ssh_reachable",
                "containers_running", "containers_total", "health_checks",
            )}
            for n in tel.get("nodes", [])
        ]
    merged["data"] = data
    return merged


async def fetch_audit_context(client: httpx.AsyncClient) -> dict:
    """Pull audit summary from the Audit VM Results API (GET /api/summary)."""
    return await _fetch(client, f"{AUDIT_ENGINE_URL}/api/summary", "audit_engine")


async def fetch_all_context() -> dict[str, Any]:
    """
    Concurrently pull data from all four CVG engines.
    Returns a structured dict with per-engine results + metadata.
    """
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            fetch_git_context(client),
            fetch_dns_context(client),
            fetch_container_context(client),
            fetch_audit_context(client),
            return_exceptions=False,
        )

    git_result, dns_result, container_result, audit_result = results

    online = sum(1 for r in results if r["status"] == "ok")
    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "engines_online": online,
        "engines_total": 4,
        "git": git_result,
        "dns": dns_result,
        "container": container_result,
        "audit": audit_result,
    }


# Mapping from cognitive context_type → which engines to include
_CONTEXT_TYPE_TO_ENGINES = {
    "git":            ["git"],
    "dns":            ["dns"],
    "container":      ["container"],
    "audit":          ["audit"],
    "infrastructure": ["container", "audit"],
    "security":       ["audit", "container"],
    # All-engine contexts
    "general":        ["git", "dns", "container", "audit"],
    "synthesis":      ["git", "dns", "container", "audit"],
    "all":            ["git", "dns", "container", "audit"],
}


def build_context_string(context_data: dict, engine: Optional[str] = None) -> str:
    """
    Convert fetched context data into a plain-text string
    suitable for injection into LLM prompts.

    engine: any context_type string (e.g. 'git', 'dns', 'infrastructure',
            'general', 'synthesis', 'all', None) — mapped to relevant engines.
    """
    ts = context_data.get("timestamp", "unknown")
    parts = [
        f"[CVG Live Context — {ts}]",
        f"Engines online: {context_data.get('engines_online')}/{context_data.get('engines_total')}",
        "",
    ]

    # Resolve which engines to include
    engines_to_include = _CONTEXT_TYPE_TO_ENGINES.get(
        engine or "all", ["git", "dns", "container", "audit"]
    )

    for eng in engines_to_include:
        result = context_data.get(eng, {})
        label = result.get("source", eng)
        status = result.get("status", "unknown")
        data = result.get("data")

        parts.append(f"=== {label.upper()} (status: {status}) ===")
        if data:
            parts.append(json.dumps(data, indent=2, default=str))
        else:
            parts.append(f"[No data — engine {status}]")
        parts.append("")

    return "\n".join(parts)


# ─── Cached context for background refresh ───────────────────────────────────

_cached_context: Optional[dict] = None
_cache_timestamp: Optional[datetime] = None
CACHE_TTL_SECONDS = 300  # 5 minutes


async def get_cached_context(force_refresh: bool = False) -> dict:
    """Return cached context, refreshing if stale or forced."""
    global _cached_context, _cache_timestamp

    now = datetime.utcnow()
    is_stale = (
        _cached_context is None
        or _cache_timestamp is None
        or (now - _cache_timestamp).total_seconds() > CACHE_TTL_SECONDS
    )

    if is_stale or force_refresh:
        logger.info("[context] Refreshing live context from all engines...")
        _cached_context = await fetch_all_context()
        _cache_timestamp = now
        logger.info(
            "[context] Context refreshed — %d/4 engines online",
            _cached_context.get("engines_online", 0),
        )

    return _cached_context


async def refresh_context() -> None:
    """
    Refresh the cached context — called by APScheduler every 5 minutes.
    Fire-and-forget signature (no return value needed by scheduler).
    """
    await get_cached_context(force_refresh=True)
