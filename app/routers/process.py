"""
CVG Neuron AI Orchestration System — /api/process Router
Version: 2.0.0 | Clearview Geographic LLC

Primary AI task submission and execution endpoint.
"""

from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.logger import get_logger
from app.core.security import optional_auth
from app.models.response import ErrorResponse, NeuronResponse
from app.models.task import TaskRequest, TaskResult

router = APIRouter(prefix="/api/process", tags=["Process"])
log = get_logger("router.process")


@router.post(
    "",
    response_model=NeuronResponse,
    summary="Submit and execute an AI task",
    description=(
        "Submit a task to the CVG Neuron orchestration engine. "
        "The request is routed to the appropriate cognitive level, "
        "agents are selected, processing occurs, and results are returned. "
        "Cognitive levels: basic (1 agent) → advanced (multi-agent) → "
        "neural (with memory) → autonomous (self-directed + learning)."
    ),
    responses={
        200: {"description": "Task completed successfully"},
        422: {"description": "Invalid request payload"},
        500: {"description": "Processing error"},
        504: {"description": "Task timeout"},
    },
)
async def process_task(
    request_body: TaskRequest,
    request: Request,
    auth: Dict[str, Any] = Depends(optional_auth),
) -> NeuronResponse:
    """
    Execute an AI task through the CVG Neuron cognitive processing pipeline.

    **Examples:**

    Basic chat:
    ```json
    {"input": "What is sea-level rise?", "cognitive_level": "basic"}
    ```

    GIS analysis with neural processing:
    ```json
    {
      "input": "Analyze flood risk for polygon coordinates [...]",
      "cognitive_level": "neural",
      "input_method": "map_input",
      "context": {"coordinates": [...]}
    }
    ```

    Multi-turn conversation:
    ```json
    {
      "input": "Follow-up: what mitigation options exist?",
      "session_id": "session-abc123",
      "cognitive_level": "advanced"
    }
    ```
    """
    orchestrator = request.app.state.orchestrator
    t0 = time.monotonic()

    # Attach user context if authenticated
    if auth and auth.get("sub") and not request_body.user_id:
        request_body = request_body.model_copy(update={"user_id": auth["sub"]})

    try:
        result: TaskResult = await orchestrator.execute_task(request_body)
        duration_ms = int((time.monotonic() - t0) * 1000)

        log.info(
            "Process request complete",
            task_id=result.task_id,
            duration_ms=duration_ms,
        )

        return NeuronResponse.ok(
            data=result.model_dump(),
            message="Task completed successfully",
            request_id=request_body.task_id,
            duration_ms=duration_ms,
        )

    except TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Task timed out: {exc}",
        )
    except Exception as exc:
        log.exception("Process request failed", task_id=request_body.task_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.get(
    "/{task_id}",
    response_model=NeuronResponse,
    summary="Get task status and result",
)
async def get_task(task_id: str, request: Request) -> NeuronResponse:
    """Retrieve the current status and result of a previously submitted task."""
    orchestrator = request.app.state.orchestrator
    task = await orchestrator.get_task(task_id)

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )

    return NeuronResponse.ok(
        data=task.model_dump(),
        message=f"Task status: {task.status}",
        request_id=task_id,
    )


@router.get(
    "",
    response_model=NeuronResponse,
    summary="List active tasks",
)
async def list_active_tasks(request: Request) -> NeuronResponse:
    """Return all currently active (in-progress) tasks."""
    orchestrator = request.app.state.orchestrator
    tasks = await orchestrator.get_active_tasks()
    return NeuronResponse.ok(
        data=[t.model_dump() for t in tasks],
        message=f"{len(tasks)} active task(s)",
    )
