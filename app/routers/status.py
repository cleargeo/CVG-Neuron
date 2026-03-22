"""
CVG Neuron AI Orchestration System — /api/status Router
Version: 2.0.0 | Clearview Geographic LLC

System health and status endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.core.logger import get_logger
from app.models.response import NeuronResponse

router = APIRouter(prefix="/api/status", tags=["Status"])
log = get_logger("router.status")


@router.get(
    "",
    response_model=NeuronResponse,
    summary="System health status",
    description="Returns full health status of CVG Neuron and all connected subsystems.",
)
async def get_status(request: Request) -> NeuronResponse:
    """
    Returns the health status of CVG Neuron and all connected CVG subsystems:
    - CVG Hive (distributed compute)
    - CVG COMB (tiered memory)
    - CVG Observability (metrics)
    - NeuroCache
    - Agent pool
    """
    orchestrator = request.app.state.orchestrator
    health = await orchestrator.get_health()

    log.debug("Health status checked", status=health.status)

    return NeuronResponse.ok(
        data=health.model_dump(),
        message=f"System status: {health.status}",
    )


@router.get(
    "/ping",
    summary="Quick liveness check",
    response_model=dict,
)
async def ping() -> dict:
    """Simple liveness probe — returns immediately without heavy computation."""
    return {
        "status": "ok",
        "neuron_id": "CVG-NEURON-001",
        "message": "CVG Neuron is alive",
    }


@router.get(
    "/metrics",
    summary="Prometheus metrics",
    include_in_schema=False,
)
async def prometheus_metrics(request: Request) -> Response:
    """Prometheus metrics endpoint for scraping."""
    orchestrator = request.app.state.orchestrator
    metrics_bytes = orchestrator.observability.get_prometheus_metrics()
    return Response(
        content=metrics_bytes,
        media_type="text/plain; version=0.0.4",
    )


@router.get(
    "/history",
    response_model=NeuronResponse,
    summary="Task execution history",
)
async def get_history(request: Request, limit: int = 50) -> NeuronResponse:
    """Return recent task execution history."""
    orchestrator = request.app.state.orchestrator
    tasks = await orchestrator.get_history(limit=min(limit, 500))
    return NeuronResponse.ok(
        data=[t.model_dump() for t in tasks],
        message=f"{len(tasks)} task(s) in history",
    )
