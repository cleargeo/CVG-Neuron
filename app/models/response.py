"""
CVG Neuron AI Orchestration System — Standard Response Models
Version: 2.0.0 | Clearview Geographic LLC

Consistent envelope for all API responses.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class NeuronResponse(BaseModel, Generic[T]):
    """
    Standard CVG Neuron API response envelope.

    All endpoints return this structure so clients can
    uniformly handle success, errors, and metadata.
    """

    success: bool = True
    data: Optional[T] = None
    message: Optional[str] = None
    neuron_id: str = "CVG-NEURON-001"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    request_id: Optional[str] = None
    duration_ms: Optional[int] = None
    version: str = "2.0.0"

    @classmethod
    def ok(
        cls,
        data: Any = None,
        message: str = "Success",
        request_id: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> "NeuronResponse":
        return cls(
            success=True,
            data=data,
            message=message,
            request_id=request_id,
            duration_ms=duration_ms,
        )

    @classmethod
    def fail(
        cls,
        message: str = "An error occurred",
        data: Any = None,
        request_id: Optional[str] = None,
    ) -> "NeuronResponse":
        return cls(
            success=False,
            data=data,
            message=message,
            request_id=request_id,
        )


class ErrorDetail(BaseModel):
    """Structured error detail."""

    code: str
    message: str
    field: Optional[str] = None
    context: Optional[Dict[str, Any]] = None


class ErrorResponse(BaseModel):
    """Standard error response body."""

    success: bool = False
    error: str
    detail: Optional[str] = None
    errors: List[ErrorDetail] = Field(default_factory=list)
    neuron_id: str = "CVG-NEURON-001"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    request_id: Optional[str] = None


class PaginatedResponse(BaseModel, Generic[T]):
    """Paginated list response."""

    success: bool = True
    items: List[T] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    pages: int = 1
    neuron_id: str = "CVG-NEURON-001"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def of(
        cls,
        items: List[Any],
        total: int,
        page: int = 1,
        page_size: int = 20,
    ) -> "PaginatedResponse":
        import math
        return cls(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            pages=max(1, math.ceil(total / page_size)),
        )


class HealthStatus(BaseModel):
    """System health check response."""

    status: str = "healthy"           # healthy | degraded | unhealthy
    neuron_id: str = "CVG-NEURON-001"
    version: str = "2.0.0"
    environment: str = "development"
    uptime_seconds: Optional[float] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Component health
    database: str = "unknown"
    redis: str = "unknown"
    hive: str = "unknown"
    comb: str = "unknown"
    observability: str = "unknown"

    # Stats
    active_tasks: int = 0
    total_agents: int = 0
    cache_size: int = 0
    memory_mb: Optional[float] = None
    cpu_percent: Optional[float] = None


class DashboardStats(BaseModel):
    """Real-time dashboard statistics."""

    # Task metrics
    tasks_total: int = 0
    tasks_active: int = 0
    tasks_completed_today: int = 0
    tasks_failed_today: int = 0
    task_success_rate: float = 1.0
    avg_task_duration_ms: float = 0.0

    # Agent metrics
    agents_total: int = 0
    agents_idle: int = 0
    agents_busy: int = 0
    agents_offline: int = 0

    # Performance
    tokens_used_today: int = 0
    avg_cognitive_level: str = "advanced"

    # System
    uptime_seconds: float = 0.0
    cache_hit_rate: float = 0.0
    hive_nodes_online: int = 0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
