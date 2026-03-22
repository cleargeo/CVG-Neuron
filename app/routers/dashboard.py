"""
CVG Neuron AI Orchestration System — /api/dashboard Router
Version: 2.0.0 | Clearview Geographic LLC

Real-time dashboard statistics and agent management.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.logger import get_logger
from app.core.security import optional_auth, require_admin
from app.models.agent import AgentCreate, AgentUpdate
from app.models.response import NeuronResponse

router = APIRouter(prefix="/api/dashboard", tags=["Dashboard"])
log = get_logger("router.dashboard")


@router.get(
    "",
    response_model=NeuronResponse,
    summary="Real-time dashboard statistics",
)
async def get_dashboard(request: Request) -> NeuronResponse:
    """
    Returns comprehensive real-time statistics for the CVG Neuron dashboard:
    - Task counts (active, completed, failed, success rate)
    - Agent pool status (idle, busy, offline)
    - Token usage
    - Cache hit rate
    - Hive nodes online
    - System uptime
    """
    orchestrator = request.app.state.orchestrator
    stats = await orchestrator.get_dashboard()
    return NeuronResponse.ok(
        data=stats.model_dump(),
        message="Dashboard data retrieved",
    )


# ── Agent management ──────────────────────────────────────────────────────────

@router.get(
    "/agents",
    response_model=NeuronResponse,
    summary="List all agents with status",
)
async def list_agents(request: Request) -> NeuronResponse:
    """Return all agents in the registry with their current status and metrics."""
    orchestrator = request.app.state.orchestrator
    summaries = await orchestrator.registry.list_summaries()
    return NeuronResponse.ok(
        data=[s.model_dump() for s in summaries],
        message=f"{len(summaries)} agent(s) registered",
    )


@router.get(
    "/agents/{agent_id}",
    response_model=NeuronResponse,
    summary="Get agent details",
)
async def get_agent(agent_id: str, request: Request) -> NeuronResponse:
    """Return full details for a specific agent including performance metrics."""
    orchestrator = request.app.state.orchestrator
    agent = await orchestrator.registry.get(agent_id)
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id} not found",
        )
    return NeuronResponse.ok(data=agent.model_dump(), message="Agent found")


@router.post(
    "/agents",
    response_model=NeuronResponse,
    summary="Register a new agent",
    status_code=status.HTTP_201_CREATED,
)
async def register_agent(
    agent_data: AgentCreate,
    request: Request,
    auth: Dict[str, Any] = Depends(require_admin),
) -> NeuronResponse:
    """Register a new autonomous agent in the CVG Neuron pool."""
    orchestrator = request.app.state.orchestrator
    agent = await orchestrator.registry.register(agent_data)
    log.info("Agent registered via API", agent_id=agent.agent_id, name=agent.name)
    return NeuronResponse.ok(
        data=agent.model_dump(),
        message=f"Agent {agent.name} registered successfully",
    )


@router.patch(
    "/agents/{agent_id}",
    response_model=NeuronResponse,
    summary="Update agent configuration",
)
async def update_agent(
    agent_id: str,
    update: AgentUpdate,
    request: Request,
    auth: Dict[str, Any] = Depends(require_admin),
) -> NeuronResponse:
    """Update an agent's configuration (status, system prompt, limits, etc.)."""
    orchestrator = request.app.state.orchestrator
    agent = await orchestrator.registry.get(agent_id)
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id} not found",
        )

    # Apply updates
    if update.status is not None:
        await orchestrator.registry.set_status(agent_id, update.status)
    if update.system_prompt is not None:
        agent.system_prompt = update.system_prompt
    if update.max_concurrent is not None:
        agent.max_concurrent = update.max_concurrent
    if update.priority_weight is not None:
        agent.priority_weight = update.priority_weight
    if update.config is not None:
        agent.config.update(update.config)
    if update.tags is not None:
        agent.tags = update.tags

    return NeuronResponse.ok(data=agent.model_dump(), message="Agent updated")


@router.delete(
    "/agents/{agent_id}",
    response_model=NeuronResponse,
    summary="Deregister an agent",
)
async def deregister_agent(
    agent_id: str,
    request: Request,
    auth: Dict[str, Any] = Depends(require_admin),
) -> NeuronResponse:
    """Remove an agent from the registry."""
    orchestrator = request.app.state.orchestrator
    removed = await orchestrator.registry.deregister(agent_id)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id} not found",
        )
    return NeuronResponse.ok(message=f"Agent {agent_id} deregistered")


# ── Cache management ──────────────────────────────────────────────────────────

@router.get(
    "/cache",
    response_model=NeuronResponse,
    summary="NeuroCache statistics",
)
async def get_cache_stats(request: Request) -> NeuronResponse:
    """Return NeuroCache performance statistics."""
    orchestrator = request.app.state.orchestrator
    stats = orchestrator.cache.stats()
    return NeuronResponse.ok(data=stats, message="Cache statistics")


@router.delete(
    "/cache",
    response_model=NeuronResponse,
    summary="Flush NeuroCache",
)
async def flush_cache(
    request: Request,
    auth: Dict[str, Any] = Depends(require_admin),
) -> NeuronResponse:
    """Flush all entries from NeuroCache (volatile memory only)."""
    orchestrator = request.app.state.orchestrator
    await orchestrator.cache.clear()
    return NeuronResponse.ok(message="NeuroCache flushed")


# ── Hive overview ─────────────────────────────────────────────────────────────

@router.get(
    "/hive",
    response_model=NeuronResponse,
    summary="CVG Hive node status",
)
async def get_hive_status(request: Request) -> NeuronResponse:
    """Return status and metrics for all CVG Hive nodes."""
    orchestrator = request.app.state.orchestrator
    online = await orchestrator.hive.get_online_nodes()
    metrics = await orchestrator.hive.get_node_metrics()
    return NeuronResponse.ok(
        data={"online_nodes": online, "node_metrics": metrics},
        message=f"{len(online)} of 3 Hive nodes online",
    )
