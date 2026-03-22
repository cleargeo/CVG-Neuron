"""
neuron/routes_extended.py -- CVG Neuron Extended API Routes

Supplementary routes mounted at /api/ext:
- Platform context diagnostics
- Neuron info + capabilities
- Prompt testing
- Context summary
- New: context/raw, cluster/nodes, memory/export, identity/card, edge/log
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

logger = logging.getLogger("neuron.routes_ext")

CVG_INTERNAL_KEY = os.getenv("CVG_INTERNAL_KEY", "cvg-internal-2026")


def _require_key(request: Request) -> None:
    provided = request.headers.get("X-CVG-Key", "")
    if provided != CVG_INTERNAL_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing CVG internal key",
        )


router = APIRouter(prefix="/api/ext", tags=["extended"])

# -- Request models --

class PromptTestRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    context_type: str = Field(default="general")
    include_system_prompt: bool = Field(default=False)


class ContextRefreshRequest(BaseModel):
    force: bool = Field(default=True)


# -- Neuron info --


@router.get("/info", dependencies=[Depends(_require_key)])
async def neuron_info():
    """Full Neuron service info -- capabilities, version, cluster summary, memory stats."""
    from .identity import get_identity_card, NEURON_NAME, NEURON_VERSION
    from .memory import get_memory
    from .cluster import get_cluster
    from .mind import get_mind
    from .edge_connector import get_edge_network

    mem_stats = get_memory().stats()
    cluster = get_cluster()
    edge_stats = get_edge_network().stats()
    mind = get_mind()

    return {
        "name": NEURON_NAME,
        "version": NEURON_VERSION,
        "identity": get_identity_card(),
        "cognitive_engine": {
            "protocol": "RECALL -> ASSESS -> REASON -> VERIFY -> RESPOND",
            "interaction_count": mind._interaction_count,
            "boot_time": mind._boot_time,
        },
        "memory": mem_stats,
        "cluster": {
            "known_nodes": len(cluster._nodes),
            "last_scan": cluster._last_scan,
        },
        "edge_network": edge_stats,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# -- Context diagnostics --


@router.get("/context/diagnostic", dependencies=[Depends(_require_key)])
async def context_diagnostic():
    """Diagnostic check of all CVG engine connections."""
    from .context_builder import fetch_all_context

    ctx = await fetch_all_context()
    return {
        "context_snapshot": ctx,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "engines_checked": list(ctx.keys()),
        "engines_online": [k for k, v in ctx.items() if v and not str(v).startswith("Error")],
    }


@router.get("/context/summary", dependencies=[Depends(_require_key)])
async def context_summary():
    """Return a human-readable summary of current platform context."""
    from .context_builder import get_cached_context, build_context_string

    ctx = await get_cached_context()
    summary = build_context_string(ctx, engine="all")
    return {
        "summary": summary,
        "context_keys": list(ctx.keys()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/context/refresh", dependencies=[Depends(_require_key)])
async def context_refresh(req: ContextRefreshRequest):
    """Force a context refresh from all CVG engines."""
    from .context_builder import get_cached_context

    ctx = await get_cached_context(force_refresh=req.force)
    return {
        "status": "refreshed" if req.force else "cached",
        "engines": list(ctx.keys()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# -- NEW: /api/ext/context/raw --


@router.get("/context/raw", dependencies=[Depends(_require_key)])
async def context_raw():
    """Return full raw context JSON from all CVG engines.
    Useful for debugging context quality and engine connectivity.
    Use /context/summary for human-readable version."""
    from .context_builder import fetch_all_context

    ctx = await fetch_all_context()
    online = [k for k, v in ctx.items() if v and not str(v).startswith("Error")]
    offline = [k for k in ctx if k not in online]
    return {
        "status": "ok",
        "engines_online": online,
        "engines_offline": offline,
        "engine_count": len(ctx),
        "context": ctx,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# -- NEW: /api/ext/cluster/nodes --


@router.get("/cluster/nodes", dependencies=[Depends(_require_key)])
async def cluster_nodes():
    """Return detailed node list for all registered Hive-0 cluster nodes.
    Includes hardware info, role, IP, last seen status, and latency."""
    from .cluster import get_cluster

    cluster = get_cluster()
    summary = cluster.get_hive0_summary()
    nodes = summary.get("nodes", [])

    # Enrich with manifest data if available
    try:
        from .hive0_telemetry import get_hive0_node_manifest
        manifest = get_hive0_node_manifest()
        manifest_by_ip = {n.get("ip"): n for n in manifest}
    except Exception:
        manifest_by_ip = {}

    enriched = []
    for node in nodes:
        ip = node.get("ip", "")
        enriched_node = {**node, **manifest_by_ip.get(ip, {})}
        enriched.append(enriched_node)

    return {
        "cluster": "CVG Hive-0",
        "network": "10.10.10.0/24",
        "total_nodes": len(enriched),
        "nodes_online": sum(1 for n in enriched if n.get("status") == "online"),
        "nodes": enriched,
        "last_scan": cluster._last_scan,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# -- NEW: /api/ext/memory/export --


@router.get("/memory/export", dependencies=[Depends(_require_key)])
async def memory_export():
    """Export all memory tiers as a downloadable JSON file.
    Returns semantic facts, episodic entries, and procedural patterns."""
    from .memory import get_memory

    mem = get_memory()
    export_data = {
        "export_timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "1.0",
        "semantic_facts": mem.semantic.all_facts() if hasattr(mem.semantic, "all_facts") else [],
        "episodic_entries": mem.episodic.recent(500) if hasattr(mem.episodic, "recent") else [],
        "procedural_patterns": mem.procedural.all_patterns() if hasattr(mem.procedural, "all_patterns") else [],
        "stats": mem.stats(),
    }
    json_bytes = json.dumps(export_data, indent=2, default=str).encode("utf-8")
    return Response(
        content=json_bytes,
        media_type="application/json",
        headers={
            "Content-Disposition": f"attachment; filename=cvg_neuron_memory_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        },
    )

# -- /api/ext/prompt/test (improved) --


@router.post("/prompt/test", dependencies=[Depends(_require_key)])
async def prompt_test(req: PromptTestRequest):
    """Test a custom prompt through Neuron cognition pipeline.
    Optionally returns the assembled system prompt for debugging."""
    from .mind import get_mind
    from .identity import build_neuron_system_prompt
    from .memory import get_memory
    from .cluster import get_cluster

    mind = get_mind()
    result = await mind.think(message=req.message, context_type=req.context_type)

    response: Dict[str, Any] = {
        "cognitive_result": result,
        "context_type": req.context_type,
        "test_timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if req.include_system_prompt:
        try:
            recalled = mind._recall(req.message)
            memory_summary = mind._build_memory_summary(recalled)
            knowledge_snippet = "\n".join(
                f"- {f.get('key', '')}: {f.get('value', '')}"
                for f in recalled.get("semantic", [])[:5]
            )
            cluster_state = get_cluster().get_cluster_state_for_neuron()
            system_prompt = build_neuron_system_prompt(
                memory_summary=memory_summary,
                knowledge_snippet=knowledge_snippet,
                cluster_state=cluster_state,
            )
            response["system_prompt_preview"] = system_prompt[:2000]
        except Exception as e:
            response["system_prompt_error"] = str(e)

    return response

# -- NEW: /api/ext/identity/card --


@router.get("/identity/card", dependencies=[Depends(_require_key)])
async def identity_card():
    """Return CVG Neuron identity JSON.
    Includes name, version, role, capabilities, and cluster affiliation."""
    try:
        from .identity import get_identity_card, NEURON_NAME, NEURON_VERSION
        card = get_identity_card()
    except Exception as e:
        card = {"error": str(e)}
        NEURON_NAME = "CVG Neuron"
        NEURON_VERSION = "unknown"

    return {
        "name": NEURON_NAME,
        "version": NEURON_VERSION,
        "role": "AI Intelligence Engine",
        "operator": "Clearview Geographic, LLC",
        "cluster": "CVG Hive-0",
        "primary_host": "cvg-stormsurge-01 (10.10.10.200)",
        "substrate": "Ollama (local LLM inference)",
        "capabilities": [
            "infrastructure-analysis",
            "git-version-tracking",
            "dns-health-analysis",
            "security-audit",
            "full-platform-synthesis",
            "multi-source-synthesis",
            "anomaly-detection",
            "code-review",
            "conversation-memory",
        ],
        "identity_card": card,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# -- NEW: /api/ext/edge/log --


@router.get("/edge/log", dependencies=[Depends(_require_key)])
async def edge_log(limit: int = 50):
    """Return recent edge payloads received by the edge connector.
    Shows last N payloads (default 50) for debugging and monitoring."""
    from .edge_connector import get_edge_network

    edge = get_edge_network()
    limit = min(limit, 200)

    try:
        recent_payloads = edge.get_recent_payloads(limit=limit)
    except AttributeError:
        recent_payloads = []
        logger.warning("EdgeNetwork.get_recent_payloads not implemented")

    stats = edge.stats()
    return {
        "status": "ok",
        "limit": limit,
        "payload_count": len(recent_payloads),
        "edge_stats": stats,
        "recent_payloads": recent_payloads,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# -- Memory search --


@router.get("/memory/search", dependencies=[Depends(_require_key)])
async def memory_search(query: str, limit: int = 20):
    """Full-text search across semantic memory facts."""
    if not query:
        raise HTTPException(status_code=400, detail="query parameter required")
    from .memory import get_memory
    mem = get_memory()
    results = mem.semantic.search(query, limit=min(limit, 100))
    return {"query": query, "results": results, "count": len(results)}


@router.get("/memory/episodes", dependencies=[Depends(_require_key)])
async def memory_episodes(limit: int = 20):
    """Return recent episodic memory entries."""
    from .memory import get_memory
    episodes = get_memory().episodic.recent(min(limit, 100))
    return {"episodes": episodes, "count": len(episodes)}


@router.get("/memory/procedures", dependencies=[Depends(_require_key)])
async def memory_procedures():
    """Return all procedural memory patterns."""
    from .memory import get_memory
    patterns = get_memory().procedural.all_patterns()
    return {"patterns": patterns, "count": len(patterns)}

# -- Edge sign helper --


@router.post("/edge/sign", dependencies=[Depends(_require_key)])
async def edge_sign(
    edge_id: str,
    payload_type: str,
    timestamp: Optional[float] = None,
):
    """Generate a valid HMAC signature for an edge payload."""
    from .edge_connector import get_edge_network
    import time

    ts = timestamp or time.time()
    sig = get_edge_network().generate_signature(
        edge_id=edge_id,
        payload_type=payload_type,
        timestamp=ts,
    )
    return {
        "edge_id": edge_id,
        "payload_type": payload_type,
        "timestamp": ts,
        "signature": sig,
        "note": "signature valid for 5 minutes from timestamp",
    }

# -- Substrate health --


@router.get("/substrate/health", dependencies=[Depends(_require_key)])
async def substrate_health():
    """Check Ollama substrate health."""
    from .ollama_client import get_ollama_client

    client = get_ollama_client()
    ok = await client.health()
    models = []
    if ok:
        try:
            models = await client.list_models()
        except Exception:
            pass

    return {
        "substrate": "ollama",
        "substrate_healthy": ok,
        "loaded_models": models,
        "note": "Ollama is Neuron inference substrate, not Neuron identity",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# =========================================================
# Hive-0 Cluster Router
# =========================================================

import asyncio as _asyncio

hive0_router = APIRouter(prefix="/api/hive0", tags=["hive0"])


@hive0_router.get("/status", dependencies=[Depends(_require_key)])
async def hive0_status(force: bool = False):
    """Full Hive-0 cluster telemetry. Cached 2 min. Use ?force=true to bypass."""
    from .hive0_telemetry import get_hive0_telemetry
    from .cluster import get_cluster

    telemetry = await get_hive0_telemetry(force=force)
    cluster_summary = get_cluster().get_hive0_summary()
    return {
        "status": "ok",
        "cluster": cluster_summary,
        "queen_telemetry": telemetry,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@hive0_router.post("/sweep", dependencies=[Depends(_require_key)])
async def hive0_sweep():
    """Force full Hive-0 queen telemetry sweep and queue cluster scan."""
    from .hive0_telemetry import get_hive0_telemetry
    from .cluster import get_cluster

    telemetry = await get_hive0_telemetry(force=True)
    cluster = get_cluster()
    _asyncio.create_task(cluster.scan_cluster())
    return {
        **telemetry,
        "cluster_scan": "queued (background)",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@hive0_router.get("/manifest", dependencies=[Depends(_require_key)])
async def hive0_manifest():
    """Return static Hive-0 node manifest."""
    from .hive0_telemetry import get_hive0_node_manifest

    manifest = get_hive0_node_manifest()
    return {
        "cluster": "CVG Hive-0",
        "location": "New Smyrna Beach, FL",
        "network": "10.10.10.0/24 (VLAN 10) + 10.10.20.0/24 (VLAN 20)",
        "domain": "hive0.cleargeo.tech",
        "total_nodes": len(manifest),
        "nodes": manifest,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@hive0_router.get("/nodes", dependencies=[Depends(_require_key)])
async def hive0_nodes():
    """Return cluster node status from last scan."""
    from .cluster import get_cluster
    return get_cluster().get_hive0_summary()


@hive0_router.post("/cluster/scan", dependencies=[Depends(_require_key)])
async def hive0_cluster_scan():
    """Trigger a full cluster scan and return results."""
    from .cluster import get_cluster
    cluster = get_cluster()
    result = await cluster.scan_cluster()
    return {
        **result,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
