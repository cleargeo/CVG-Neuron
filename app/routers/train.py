"""
CVG Neuron AI Orchestration System — /api/train Router
Version: 2.0.0 | Clearview Geographic LLC

Knowledge ingestion and training endpoints.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.logger import get_logger
from app.core.security import require_admin
from app.models.response import NeuronResponse
from app.models.task import TrainRequest, TrainResult

router = APIRouter(prefix="/api/train", tags=["Train"])
log = get_logger("router.train")


@router.post(
    "",
    response_model=NeuronResponse,
    summary="Submit knowledge / training data",
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_training(
    request_body: TrainRequest,
    request: Request,
    auth: Dict[str, Any] = Depends(require_admin),
) -> NeuronResponse:
    """
    Feed knowledge or training examples into CVG Neuron.

    Supported methods:
    - `memory`: Store examples in COMB BitHive for immediate recall
    - `few-shot`: Register few-shot examples for prompt injection
    - `fine-tune`: Trigger model fine-tuning (GPU-required, async)
    - `rag`: Index documents for retrieval-augmented generation

    **Example: Add GIS knowledge records**
    ```json
    {
      "domain": "gis",
      "method": "memory",
      "data": [
        {"concept": "floodplain", "definition": "Area prone to inundation..."},
        {"concept": "FEMA Zone A", "definition": "Special flood hazard area..."}
      ]
    }
    ```
    """
    orchestrator = request.app.state.orchestrator

    # Store records in COMB memory
    stored = 0
    errors = []

    for i, record in enumerate(request_body.data):
        try:
            key = f"train:{request_body.domain}:{request_body.method}:{i}"
            await orchestrator.comb.store(
                key=key,
                value=record,
                tier="bithive",
                metadata={
                    "domain": request_body.domain,
                    "method": request_body.method,
                    "source": "training_api",
                },
            )
            stored += 1
        except Exception as exc:
            errors.append({"index": i, "error": str(exc)})

    result = TrainResult(
        status="completed" if not errors else "partial",
        records_processed=stored,
        domain=request_body.domain,
        method=request_body.method,
        message=f"Stored {stored}/{len(request_body.data)} records in {request_body.domain} domain",
    )

    if errors:
        result = result.model_copy(update={"status": "partial"})

    log.info(
        "Training data submitted",
        domain=request_body.domain,
        method=request_body.method,
        stored=stored,
        errors=len(errors),
    )

    return NeuronResponse.ok(
        data=result.model_dump(),
        message=result.message,
    )


@router.get(
    "/domains",
    response_model=NeuronResponse,
    summary="List knowledge domains",
)
async def list_domains() -> NeuronResponse:
    """Return all available knowledge domains."""
    domains = [
        {"id": "gis", "name": "Geographic Information Systems", "description": "Spatial analysis, coordinates, projections"},
        {"id": "hydrology", "name": "Hydrology & Water Resources", "description": "Flood risk, rainfall, storm surge, SLR"},
        {"id": "network", "name": "Network Infrastructure", "description": "DNS, VPN, routing, firewalls"},
        {"id": "infrastructure", "name": "Server Infrastructure", "description": "Containers, Queens, monitoring"},
        {"id": "security", "name": "Security & Authentication", "description": "StratoVault, RBAC, audit trails"},
        {"id": "general", "name": "General Knowledge", "description": "Cross-domain general purpose"},
        {"id": "code", "name": "Code & Engineering", "description": "Python, PHP, PowerShell, APIs"},
    ]
    return NeuronResponse.ok(data=domains, message=f"{len(domains)} domains available")


@router.get(
    "/status/{job_id}",
    response_model=NeuronResponse,
    summary="Check training job status",
)
async def get_training_status(job_id: str, request: Request) -> NeuronResponse:
    """Check the status of an async training job."""
    orchestrator = request.app.state.orchestrator
    cached = await orchestrator.cache.get(f"train_job:{job_id}")
    if not cached:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Training job {job_id} not found (may have completed)",
        )
    return NeuronResponse.ok(data=cached, message=f"Training job {job_id}")
