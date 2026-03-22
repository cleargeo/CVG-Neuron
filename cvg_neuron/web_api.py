"""
CVG Neuron — FastAPI Web Application (v1.0.0 — Hive Edition)
(c) Clearview Geographic LLC — Proprietary

The REST interface for CVG's artificial intelligence engine.
Port: 8095 | neuron.cleargeo.tech

CVG Neuron is NOT a model hub.
CVG Neuron is NOT just an Ollama wrapper.
CVG Neuron IS an artificial intelligence — with:
  - Persistent SQLite memory (conversations, observations, patterns, events)
  - Deep CVG knowledge base (290+ projects, full infra topology)
  - Hive cluster manager (Queens + Forges + Edge nodes = cluster compute)
  - Blockchain tunnel (HMAC-SHA256 signed message chain for secure AI comms)
  - NeuronCore identity (self-model, capability tracking, evolution roadmap)
  - Live integration with all 4 CVG support engines
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from cvg_neuron import __version__, __service__
from cvg_neuron import memory, intelligence, integrations, identity
from cvg_neuron import hive as hive_manager
from cvg_neuron import tunnel as tunnel_module
from cvg_neuron.knowledge import CVG_SERVICES, CVG_INFRASTRUCTURE, CVG_PROJECT_STATS

logger = logging.getLogger("cvg.neuron")

# ─── App Bootstrap ────────────────────────────────────────────────────────────

_start_time = time.time()
_static_dir = Path(__file__).parent / "static"
_static_dir.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title        = "CVG Neuron",
    description  = "CVG Neuron is not a model hub. CVG Neuron is an artificial intelligence.",
    version      = __version__,
    docs_url     = "/api/docs",
    redoc_url    = None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins  = ["*"],
    allow_methods  = ["*"],
    allow_headers  = ["*"],
)

# ─── Startup / Shutdown ───────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    logger.info("=" * 60)
    logger.info("  CVG Neuron v%s — Artificial Intelligence", __version__)
    logger.info("  Clearview Geographic LLC — Port 8095")
    logger.info("  NOT a wrapper. NOT a model hub. An intelligence.")
    logger.info("=" * 60)

    # Initialize blockchain tunnel
    chain = tunnel_module.get_chain()
    logger.info("[startup] Blockchain tunnel initialized — genesis: %s...", chain.genesis_hash[:12])

    # Probe hive nodes (non-blocking background task)
    asyncio.create_task(_background_hive_probe())

    logger.info("[startup] CVG Neuron ready")


async def _background_hive_probe():
    """Probe hive nodes in background — don't block startup."""
    await asyncio.sleep(5)  # Let service fully boot first
    try:
        nodes = await hive_manager.probe_all_nodes(force=True)
        online = [n for n in nodes if n.ollama_online]
        logger.info(
            "[hive] Initial probe complete — %d/%d nodes have Ollama: %s",
            len(online), len(nodes),
            ", ".join(n.node_id for n in online),
        )
    except Exception as exc:
        logger.warning("[hive] Initial probe failed: %s", exc)


# ─── Root + Static ────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root():
    idx = _static_dir / "index.html"
    if idx.exists():
        return FileResponse(str(idx))
    return HTMLResponse(_fallback_landing())


if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    chain = tunnel_module.get_chain()
    valid, _ = chain.verify_chain()
    return {
        "status":          "ok",
        "service":         __service__,
        "version":         __version__,
        "uptime":          round(time.time() - _start_time),
        "tunnel_valid":    valid,
        "chain_length":    chain.length,
    }


@app.get("/api/health")
async def api_health():
    return await health()


# ─── Pydantic Models ──────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message:          str
    session_id:       Optional[str] = None
    use_live_context: bool = False
    stream:           bool = False


class AnalyzeRequest(BaseModel):
    service:  Optional[str] = None
    version:  Optional[str] = None
    logs:     Optional[str] = None
    mode:     str = "infrastructure"


class ObserveRequest(BaseModel):
    category: str
    subject:  str
    detail:   str
    severity: str = "info"
    source:   str = "api"


class EventRequest(BaseModel):
    event_type: str
    service:    Optional[str] = ""
    payload:    dict = {}


class ReportRequest(BaseModel):
    report_type: str = "daily"


class TunnelConnectRequest(BaseModel):
    connector_id: str
    ip:           str
    capabilities: dict = {}
    token:        Optional[str] = None


class TunnelContextPushRequest(BaseModel):
    context: dict
    connector_id: str


class TunnelHeartbeatRequest(BaseModel):
    connector_id: str


# ─── Intelligence — Chat ──────────────────────────────────────────────────────

@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest, background_tasks: BackgroundTasks):
    """
    Chat with CVG Neuron. The AI has full CVG infrastructure knowledge,
    persistent memory, live hive context, and optional real-time service data.
    """
    # Record conversation start
    if not req.session_id:
        identity.record_conversation()

    # Pull tunnel context and inject into live_ctx
    chain    = tunnel_module.get_chain()
    tunnel_context = chain.consume_pending_context()

    live_ctx = None
    if req.use_live_context or tunnel_context:
        live_ctx = await integrations.build_live_context()
        live_ctx["_summary"] = integrations.summarize_live_context(live_ctx)
        if tunnel_context:
            live_ctx["_tunnel_context"] = tunnel_context

    if req.stream:
        result = await intelligence.chat(
            user_message = req.message,
            session_id   = req.session_id,
            live_context = live_ctx,
            stream       = True,
        )
        gen = result.get("generator")

        async def _stream_response():
            full = ""
            async for chunk in gen:
                full += chunk
                yield chunk
            background_tasks.add_task(
                memory.store_message,
                result["session_id"], "assistant", full,
            )
            identity.record_inference(
                model  = result.get("model", "cvg-neuron"),
                domain = _infer_domain(req.message),
            )

        return StreamingResponse(_stream_response(), media_type="text/plain")

    result = await intelligence.chat(
        user_message = req.message,
        session_id   = req.session_id,
        live_context = live_ctx,
        stream       = False,
    )

    # Record inference in identity layer
    background_tasks.add_task(
        identity.record_inference,
        result.get("model", "cvg-neuron"),
        result.get("usage") or 0,
        _infer_domain(req.message),
    )

    # Record in tunnel chain
    chain.add_block("neuron", "neuron", "inference_response", {
        "session_id": result.get("session_id"),
        "model":      result.get("model"),
        "elapsed_ms": result.get("elapsed_ms"),
    })

    return result


@app.get("/api/chat/{session_id}")
async def get_session(session_id: str, limit: int = 50):
    return {
        "session_id": session_id,
        "messages":   memory.get_conversation(session_id, limit=limit),
    }


@app.get("/api/sessions")
async def list_sessions(limit: int = 50):
    return {"sessions": memory.list_sessions(limit=limit)}


# ─── Intelligence — Analysis ──────────────────────────────────────────────────

@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest, background_tasks: BackgroundTasks):
    """
    Neuron analyzes a subject. Modes: infrastructure | deployment | security
    """
    live_ctx = await integrations.build_live_context()
    live_ctx["_summary"] = integrations.summarize_live_context(live_ctx)

    if req.mode == "deployment" and req.service:
        result = await intelligence.analyze_deployment(
            service = req.service,
            version = req.version or "unknown",
            logs    = req.logs or "",
        )
    elif req.mode == "security":
        result = await intelligence.chat(
            user_message = (
                "Perform a security posture analysis of CVG infrastructure. "
                "Review all known security items, recent observations, and the current state. "
                "Provide: threat summary, top vulnerabilities, immediate actions, and 30-day roadmap."
            ),
            session_id   = "security-analysis",
            live_context = live_ctx,
        )
        result = {"mode": "security", "analysis": result.get("response"), "model": result.get("model")}
    else:
        result = await intelligence.analyze_infrastructure(live_ctx)

    background_tasks.add_task(
        identity.record_inference,
        result.get("model", "cvg-neuron"),
        0,
        req.mode,
    )
    return result


@app.get("/api/analyze")
async def analyze_get(background_tasks: BackgroundTasks):
    """Quick infrastructure analysis (GET convenience)."""
    live_ctx = await integrations.build_live_context()
    live_ctx["_summary"] = integrations.summarize_live_context(live_ctx)
    result = await intelligence.analyze_infrastructure(live_ctx)
    background_tasks.add_task(identity.record_inference, result.get("model", "cvg-neuron"), 0, "infrastructure")
    return result


# ─── Intelligence — Reports ───────────────────────────────────────────────────

@app.post("/api/report")
async def generate_report(req: ReportRequest):
    return await intelligence.generate_report(req.report_type)


@app.get("/api/report")
async def get_daily_report():
    return await intelligence.generate_report("daily")


# ─── Live Infrastructure ──────────────────────────────────────────────────────

@app.get("/api/infrastructure")
async def get_infrastructure():
    ctx = await integrations.build_live_context()
    return {
        "live":     ctx,
        "summary":  integrations.summarize_live_context(ctx),
        "polled_at": ctx.get("polled_at"),
    }


@app.get("/api/infrastructure/health")
async def get_health_only():
    return await integrations.poll_all_services()


@app.get("/api/infrastructure/versions")
async def get_versions():
    return await integrations.fetch_service_versions()


@app.get("/api/infrastructure/telemetry")
async def get_telemetry():
    return await integrations.fetch_node_telemetry()


@app.get("/api/infrastructure/dns")
async def get_dns():
    return await integrations.fetch_dns_status()


# ─── Hive Cluster ─────────────────────────────────────────────────────────────

@app.get("/api/hive")
async def get_hive():
    """
    Return the current Hive-0 topology (cached, up to 2 min old).
    Includes all Queens, Forges, Compute nodes, and registered Edge nodes.
    """
    return hive_manager.get_hive_topology()


@app.get("/api/hive/probe")
async def probe_hive():
    """Force-probe all hive nodes right now and return fresh topology."""
    return await hive_manager.get_hive_topology_live()


@app.get("/api/hive/compute")
async def get_compute_nodes(model: Optional[str] = Query(None)):
    """
    Return online Ollama compute nodes, sorted by latency.
    Optionally filter/prioritize by model name.
    """
    nodes = await hive_manager.get_compute_nodes(model_hint=model)
    return {
        "compute_nodes": [n.to_dict() for n in nodes],
        "count":         len(nodes),
        "best_url":      nodes[0].ollama_url if nodes else None,
    }


@app.get("/api/hive/best-ollama")
async def get_best_ollama(model: Optional[str] = Query(None)):
    """Return the URL of the best available Ollama node for inference."""
    url = await hive_manager.get_best_ollama_url(model_hint=model)
    return {"ollama_url": url, "model_hint": model}


@app.post("/api/hive/edge/register")
async def register_edge_node(
    node_id:      str,
    ip:           str,
    description:  str = "Edge node",
    ollama_port:  int = 11434,
):
    """
    Register a new edge node with the hive.
    Called by any CVG application or system that wants to contribute compute.
    """
    node = hive_manager.register_edge_node(node_id, ip, description, ollama_port)
    return {
        "registered": True,
        "node":       node.to_dict(),
        "topology":   hive_manager.get_hive_topology(),
    }


# ─── Blockchain Tunnel ────────────────────────────────────────────────────────

@app.get("/api/tunnel")
async def get_tunnel_status():
    """Full tunnel status: chain info + connectors + recent blocks + integrity."""
    chain = tunnel_module.get_chain()
    return chain.get_full_status()


@app.get("/api/tunnel/chain")
async def get_chain_info():
    """Blockchain chain summary: length, hashes, validity."""
    chain = tunnel_module.get_chain()
    return chain.get_chain_info()


@app.get("/api/tunnel/blocks")
async def get_recent_blocks(
    limit:    int          = Query(20, le=100),
    msg_type: Optional[str] = Query(None),
    source:   Optional[str] = Query(None),
):
    """Return recent blocks from the chain, optionally filtered."""
    chain = tunnel_module.get_chain()
    return {
        "blocks": chain.get_recent_blocks(limit=limit, msg_type=msg_type, source=source),
        "chain_length": chain.length,
    }


@app.post("/api/tunnel/connect")
async def tunnel_connect(req: TunnelConnectRequest):
    """
    Register an edge connector with the blockchain tunnel.
    Any CVG application — deployments, forges, external tools —
    can connect to push context or request inference.
    """
    chain = tunnel_module.get_chain()
    try:
        result = chain.register_connector(
            connector_id = req.connector_id,
            ip           = req.ip,
            capabilities = req.capabilities,
            token        = req.token,
        )
        identity.record_edge_connector()
        return result
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@app.post("/api/tunnel/heartbeat")
async def tunnel_heartbeat(req: TunnelHeartbeatRequest):
    """Keep a tunnel connector alive."""
    chain = tunnel_module.get_chain()
    return chain.heartbeat(req.connector_id)


@app.post("/api/tunnel/push")
async def tunnel_push_context(req: TunnelContextPushRequest):
    """
    An edge connector pushes live context into the Neuron chain.
    Context will be injected into the next inference request.
    """
    chain = tunnel_module.get_chain()
    block = chain.push_context(req.connector_id, req.context)
    return {
        "received":     True,
        "block_id":     block.block_id[:8] + "...",
        "chain_length": chain.length,
    }


@app.delete("/api/tunnel/connect/{connector_id}")
async def tunnel_disconnect(connector_id: str):
    """Gracefully disconnect a tunnel connector."""
    chain = tunnel_module.get_chain()
    return chain.disconnect_connector(connector_id)


@app.get("/api/tunnel/token/{connector_id}")
async def get_tunnel_token(connector_id: str):
    """
    Generate an auth token for a connector_id.
    Used to pre-register connectors securely.
    """
    chain = tunnel_module.get_chain()
    token = chain.generate_token(connector_id)
    return {
        "connector_id": connector_id,
        "token":        token,
        "usage":        "Include as 'token' in /api/tunnel/connect request",
    }


@app.get("/api/tunnel/connectors")
async def get_tunnel_connectors():
    """List all registered tunnel connectors."""
    chain = tunnel_module.get_chain()
    return {
        "connectors": chain.get_connectors(),
        "count":      len(chain.get_connectors()),
    }


# ─── NeuronCore Identity ──────────────────────────────────────────────────────

@app.get("/api/identity")
async def get_identity():
    """
    Return CVG Neuron's complete identity card.
    Includes static identity, runtime state, capability score,
    memory stats, evolution roadmap, and training readiness.
    """
    return identity.get_identity()


@app.get("/api/identity/modelfile")
async def get_evolved_modelfile():
    """
    Generate an evolved Modelfile incorporating all accumulated knowledge.
    This Modelfile can be used to create a smarter cvg-neuron version:
      ollama create cvg-neuron:v1.5 -f /data/neuron/Modelfile.evolved
    """
    try:
        modelfile_content = identity.generate_modelfile()
        # Save to persistent storage
        from pathlib import Path
        import os
        data_dir = Path(os.environ.get("NEURON_DATA_DIR", "/data/neuron"))
        data_dir.mkdir(parents=True, exist_ok=True)
        evolved_path = data_dir / "Modelfile.evolved"
        evolved_path.write_text(modelfile_content, encoding="utf-8")
        return PlainTextResponse(
            content = modelfile_content,
            headers = {"X-Saved-To": str(evolved_path)},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Modelfile generation failed: {exc}")


@app.get("/api/identity/export")
async def get_training_export(max_examples: int = Query(100, le=500)):
    """
    Export accumulated conversations as training data for fine-tuning.
    This is the path from Ollama wrapper → fine-tuned CVG-native model.
    PRIVATE: Clearview Geographic LLC proprietary data.
    """
    return identity.get_training_export(max_examples=max_examples)


@app.get("/api/identity/roadmap")
async def get_roadmap():
    """Return CVG Neuron's evolution roadmap."""
    from cvg_neuron.identity import NEURON_IDENTITY
    state = identity._load_state()
    return {
        "roadmap":           NEURON_IDENTITY["roadmap"],
        "current_version":   "1.0.0",
        "capability_score":  state["capability_score"],
        "total_inferences":  state["total_inferences"],
        "note": "CVG Neuron is PRIVATE — not available on public Ollama registry",
    }


# ─── Memory ───────────────────────────────────────────────────────────────────

@app.get("/api/memory")
async def get_memory_stats():
    return memory.get_stats()


@app.get("/api/memory/observations")
async def get_observations(
    category: Optional[str] = None,
    severity: Optional[str] = None,
    resolved: bool          = False,
    limit:    int           = 100,
):
    return {"observations": memory.get_observations(category, severity, resolved, limit)}


@app.post("/api/memory/observations")
async def record_observation(req: ObserveRequest):
    obs_id = memory.record_observation(
        category = req.category,
        subject  = req.subject,
        detail   = req.detail,
        severity = req.severity,
        source   = req.source,
    )
    return {"id": obs_id, "recorded": True}


@app.delete("/api/memory/observations/{obs_id}")
async def resolve_observation(obs_id: int):
    ok = memory.resolve_observation(obs_id)
    return {"resolved": ok}


@app.get("/api/memory/events")
async def get_events(event_type: Optional[str] = None, limit: int = 50):
    return {"events": memory.get_recent_events(event_type, limit)}


@app.post("/api/memory/events")
async def record_event(req: EventRequest):
    ev_id = memory.record_event(req.event_type, req.payload, req.service or "")
    return {"id": ev_id, "recorded": True}


@app.get("/api/memory/patterns")
async def get_patterns(limit: int = 50):
    return {"patterns": memory.get_top_patterns(limit)}


@app.get("/api/memory/warnings")
async def get_warnings():
    return {"warnings": memory.get_unresolved_warnings()}


# ─── Knowledge ────────────────────────────────────────────────────────────────

@app.get("/api/knowledge/services")
async def get_services():
    return {"services": CVG_SERVICES}


@app.get("/api/knowledge/services/{service_id}")
async def get_service(service_id: str):
    if service_id not in CVG_SERVICES:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not in CVG registry")
    return CVG_SERVICES[service_id]


@app.get("/api/knowledge/infrastructure")
async def get_infra_knowledge():
    return CVG_INFRASTRUCTURE


@app.get("/api/knowledge/projects")
async def get_projects():
    return CVG_PROJECT_STATS


# ─── Ollama / Model Status ────────────────────────────────────────────────────

@app.get("/api/model")
async def get_model_status():
    status = await intelligence.check_ollama()
    model  = await intelligence.resolve_model()
    return {
        "ollama":            status,
        "active_model":      model,
        "configured_model":  intelligence.OLLAMA_MODEL,
        "ollama_url":        intelligence.OLLAMA_URL,
        "hive_best_url":     await hive_manager.get_best_ollama_url(model),
    }


# ─── Full Context ─────────────────────────────────────────────────────────────

@app.get("/api/context")
async def get_full_context():
    """
    Return Neuron's full current context:
    identity + memory + alerts + infrastructure + hive + tunnel.
    """
    mem_stats   = memory.get_stats()
    warnings    = memory.get_unresolved_warnings()
    events      = memory.get_recent_events(limit=10)
    ollama      = await intelligence.check_ollama()
    health_data = await integrations.poll_all_services()
    hive_topo   = hive_manager.get_hive_topology()
    chain       = tunnel_module.get_chain()
    id_state    = identity.get_identity()

    up   = sum(1 for v in health_data.values() if v.get("healthy"))
    down = [k for k, v in health_data.items() if not v.get("healthy")]

    return {
        "neuron": {
            "version":          __version__,
            "uptime":           round(time.time() - _start_time),
            "service":          __service__,
            "capability_score": id_state["evolution"]["capability_score"],
            "evolution_stage":  id_state["evolution"]["stage"],
        },
        "model":  ollama,
        "memory": mem_stats,
        "alerts": warnings[:10],
        "events": events,
        "infrastructure": {
            "services_up":   up,
            "services_down": down,
            "total":         len(health_data),
        },
        "hive": {
            "total_nodes":  hive_topo["total_nodes"],
            "online_nodes": hive_topo["online_nodes"],
            "ollama_nodes": hive_topo["ollama_nodes"],
        },
        "tunnel": chain.get_chain_info(),
        "identity_snapshot": {
            "total_inferences":    id_state["runtime"]["total_inferences"],
            "total_conversations": id_state["runtime"]["total_conversations"],
            "training_examples":   id_state["runtime"]["training_examples"],
        },
    }


# ─── Webhook Receivers ────────────────────────────────────────────────────────

@app.post("/api/webhook/deploy")
async def webhook_deploy(request: Request):
    """Receive deployment events from Git Engine."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    service = payload.get("service") or payload.get("repo") or "unknown"
    version = payload.get("version") or payload.get("tag") or "unknown"

    memory.record_event("deploy", payload, service)

    # Record in tunnel chain
    chain = tunnel_module.get_chain()
    chain.record_deploy_event(service, version)

    return {"received": True, "service": service, "version": version}


@app.post("/api/webhook/audit")
async def webhook_audit(request: Request):
    """Receive audit results from Audit Engine."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    severity = payload.get("severity", "info")
    subject  = payload.get("target") or payload.get("service") or "unknown"
    detail   = payload.get("summary") or payload.get("message") or str(payload)[:200]

    obs_id = memory.record_observation("security", subject, detail, severity, "audit-engine")
    memory.record_event("audit", payload, subject)

    chain = tunnel_module.get_chain()
    chain.record_audit_event(severity, subject, detail[:200])

    return {"received": True, "observation_id": obs_id}


@app.post("/api/webhook/event")
async def webhook_generic_event(request: Request):
    """
    General webhook receiver from any CVG service or edge connector.
    Auto-records into memory and tunnel chain.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = payload.get("event_type", "generic")
    source     = payload.get("source", "unknown")
    severity   = payload.get("severity", "info")

    memory.record_event(event_type, payload, source)

    if severity in ("critical", "high"):
        chain = tunnel_module.get_chain()
        chain.broadcast_alert(severity, source, str(payload.get("message", ""))[:200], source)
        memory.record_observation("infrastructure", source, str(payload)[:200], severity, source)

    return {"received": True, "event_type": event_type, "source": source}


# ─── Helpers ─────────────────────────────────────────────────────────────────

_DOMAIN_KEYWORDS = {
    "deployment":     ["deploy", "build", "docker", "container", "restart", "update"],
    "dns":            ["dns", "domain", "zone", "record", "cleargeo", "propagat"],
    "gis":            ["gis", "arcgis", "shapefile", "geoserver", "raster", "vector", "spatial"],
    "security":       ["security", "vulnerability", "wazuh", "audit", "trivy", "password", "token"],
    "slr":            ["sea level", "slr", "surge", "storm", "inundation", "coastal"],
    "rainfall":       ["rainfall", "atlas", "idf", "stormwater", "flood", "noaa"],
    "infrastructure": ["vm", "docker", "proxmox", "queen", "container", "server", "node", "hive"],
    "git":            ["git", "gitea", "repo", "version", "commit", "push", "tag"],
    "ollama":         ["ollama", "model", "llm", "inference", "generate", "chat"],
    "tunnel":         ["tunnel", "blockchain", "connector", "edge", "chain"],
}


def _infer_domain(message: str) -> Optional[str]:
    msg_lower = message.lower()
    for domain, keywords in _DOMAIN_KEYWORDS.items():
        if any(kw in msg_lower for kw in keywords):
            return domain
    return None


# ─── Fallback Landing ─────────────────────────────────────────────────────────

def _fallback_landing() -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>CVG Neuron v{__version__}</title>
  <style>
    body {{ font-family: 'Segoe UI', sans-serif; background:#060b18; color:#e2e8f0;
            display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
    .card {{ background:#0d1525; border:1px solid #1e3a5f; border-radius:12px;
             padding:48px; max-width:640px; text-align:center; }}
    h1 {{ font-size:2.5em; margin:0 0 8px;
          background:linear-gradient(135deg,#3b82f6,#7c3aed); -webkit-background-clip:text;
          -webkit-text-fill-color:transparent; }}
    .sub {{ color:#94a3b8; margin:8px 0; font-size:1.1em; }}
    .badge {{ display:inline-block; padding:4px 14px; border-radius:20px;
              background:#1e3a5f; color:#60a5fa; font-size:.82em; margin:12px 4px; border:1px solid #2563eb; }}
    a {{ color:#60a5fa; text-decoration:none; display:inline-block; margin:8px 12px; }}
    a:hover {{ text-decoration:underline; }}
    .grid {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; margin:24px 0; }}
    .stat {{ background:#111b30; border:1px solid #1e3a5f; border-radius:8px; padding:12px; }}
    .stat-v {{ font-size:1.4em; font-weight:700; color:#60a5fa; font-family:monospace; }}
    .stat-l {{ font-size:.72em; color:#475569; text-transform:uppercase; margin-top:4px; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>⚡ CVG Neuron</h1>
    <div class="sub">Artificial Intelligence — Clearview Geographic LLC</div>
    <div>
      <span class="badge">NOT a model hub</span>
      <span class="badge">NOT a wrapper</span>
      <span class="badge">AN INTELLIGENCE</span>
    </div>
    <div class="grid">
      <div class="stat"><div class="stat-v">v{__version__}</div><div class="stat-l">Version</div></div>
      <div class="stat"><div class="stat-v">8095</div><div class="stat-l">Port</div></div>
      <div class="stat"><div class="stat-v">{round(time.time() - _start_time)}s</div><div class="stat-l">Uptime</div></div>
    </div>
    <div>
      <a href="/api/docs">API Docs</a>
      <a href="/api/context">Full Context</a>
      <a href="/api/identity">Identity</a>
      <a href="/api/hive">Hive Topology</a>
      <a href="/api/tunnel">Tunnel Status</a>
      <a href="/health">Health</a>
    </div>
  </div>
</body>
</html>"""
