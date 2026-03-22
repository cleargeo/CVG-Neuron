"""
CVG Neuron AI Orchestration System — Task Models
Version: 2.0.0 | Clearview Geographic LLC

Pydantic schemas for AI task lifecycle: request → execution → result.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator


# ── Enumerations ──────────────────────────────────────────────────────────────

class CognitiveLevel(str, Enum):
    """Processing depth level for the CognitiveProcessor."""
    BASIC = "basic"           # Level 1: Single-agent direct response
    ADVANCED = "advanced"     # Level 2: Multi-agent synthesis
    NEURAL = "neural"         # Level 3: Deep processing with memory
    AUTONOMOUS = "autonomous" # Level 4: Self-directed with learning


class TaskStatus(str, Enum):
    """Lifecycle states for a task."""
    PENDING = "pending"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class TaskPriority(str, Enum):
    """Scheduling priority for CVG Hive distribution."""
    CRITICAL = "critical"   # weight=100, max_wait=5s
    HIGH = "high"           # weight=75,  max_wait=30s
    NORMAL = "normal"       # weight=50,  max_wait=120s
    LOW = "low"             # weight=25,  max_wait=600s


class InputMethod(str, Enum):
    """Source input method — maps to CVG Hive compute task types."""
    CHAT = "chat"
    MAP_INPUT = "map_input"
    FILE_UPLOAD = "file_upload"
    CALCULATORS = "calculators"
    WIZARDS = "wizards"
    VIDEO_CONSULTATION = "video_consultation"
    API_INTEGRATIONS = "api_integrations"
    EMAIL_PARSER = "email_parser"
    SMS_SERVICE = "sms_service"
    QR_CODE_SYSTEM = "qr_code_system"
    MOBILE_FIELD = "mobile_field_collection"
    DIRECT_API = "direct_api"


# ── Request Models ────────────────────────────────────────────────────────────

class TaskRequest(BaseModel):
    """Incoming task submission payload."""

    task_id: str = Field(
        default_factory=lambda: f"task-{uuid.uuid4().hex[:12]}",
        description="Unique task identifier (auto-generated if not provided)",
    )
    input: str = Field(..., description="Primary task input / prompt / instruction")
    context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Additional context, metadata, or parameters",
    )
    cognitive_level: CognitiveLevel = Field(
        default=CognitiveLevel.ADVANCED,
        description="Processing depth level",
    )
    priority: TaskPriority = Field(
        default=TaskPriority.NORMAL,
        description="Task scheduling priority",
    )
    input_method: InputMethod = Field(
        default=InputMethod.DIRECT_API,
        description="Source system that originated this task",
    )
    agents: Optional[List[str]] = Field(
        default=None,
        description="Specific agent IDs to use (auto-selected if None)",
    )
    max_tokens: Optional[int] = Field(
        default=None,
        ge=1,
        le=128000,
        description="Max tokens for AI response",
    )
    temperature: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description="AI sampling temperature",
    )
    stream: bool = Field(default=False, description="Stream response tokens")
    timeout: Optional[int] = Field(
        default=None,
        ge=1,
        le=3600,
        description="Task timeout in seconds",
    )
    session_id: Optional[str] = Field(
        default=None,
        description="Session ID for multi-turn conversations",
    )
    user_id: Optional[str] = Field(
        default=None,
        description="User ID for attribution and personalization",
    )
    tags: List[str] = Field(default_factory=list, description="Arbitrary tags for filtering")

    @field_validator("input")
    @classmethod
    def input_not_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Task input cannot be empty")
        return stripped


class TaskUpdate(BaseModel):
    """Partial update for an existing task (e.g., cancel)."""

    status: Optional[TaskStatus] = None
    context: Optional[Dict[str, Any]] = None
    tags: Optional[List[str]] = None


# ── Domain Models ─────────────────────────────────────────────────────────────

class AgentExecution(BaseModel):
    """Record of a single agent's contribution to a task."""

    agent_id: str
    agent_type: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    output: Optional[Any] = None
    tokens_used: Optional[int] = None
    error: Optional[str] = None
    efficiency_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class CognitiveTrace(BaseModel):
    """Full cognitive processing audit trail for a task."""

    level: CognitiveLevel
    agents_used: List[str] = Field(default_factory=list)
    executions: List[AgentExecution] = Field(default_factory=list)
    synthesis_applied: bool = False
    memory_reads: int = 0
    memory_writes: int = 0
    hive_nodes_used: List[str] = Field(default_factory=list)
    total_tokens: int = 0


class Task(BaseModel):
    """Full task entity including status and results."""

    task_id: str
    input: str
    context: Optional[Dict[str, Any]] = None
    cognitive_level: CognitiveLevel = CognitiveLevel.ADVANCED
    priority: TaskPriority = TaskPriority.NORMAL
    input_method: InputMethod = InputMethod.DIRECT_API
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    result: Optional["TaskResult"] = None
    cognitive_trace: Optional[CognitiveTrace] = None
    error: Optional[str] = None
    retry_count: int = 0


class TaskResult(BaseModel):
    """The output/result of a completed task."""

    task_id: str
    output: Any = Field(..., description="Primary output — text, data, or structured object")
    output_type: str = Field(default="text", description="text | json | binary | stream")
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    model_used: Optional[str] = None
    provider_used: Optional[str] = None
    tokens_input: Optional[int] = None
    tokens_output: Optional[int] = None
    total_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    learned: bool = Field(default=False, description="Whether Neuron learned from this result")


# ── Prediction Models ─────────────────────────────────────────────────────────

class PredictRequest(BaseModel):
    """Request for AI inference/prediction."""

    input: str = Field(..., description="Input text or data to run inference on")
    model: Optional[str] = None
    provider: Optional[str] = None
    task_type: str = Field(default="completion", description="completion | classification | embedding | extraction")
    parameters: Dict[str, Any] = Field(default_factory=dict)
    session_id: Optional[str] = None


class PredictResult(BaseModel):
    """Prediction/inference output."""

    output: Any
    model: str
    provider: str
    tokens_used: Optional[int] = None
    latency_ms: Optional[int] = None
    confidence: Optional[float] = None


# ── Training Models ───────────────────────────────────────────────────────────

class TrainRequest(BaseModel):
    """Request to train/fine-tune or feed knowledge to Neuron."""

    data: List[Dict[str, Any]] = Field(..., description="Training examples or knowledge records")
    domain: str = Field(default="general", description="Knowledge domain (e.g. gis, hydrology, network)")
    method: str = Field(default="memory", description="memory | fine-tune | few-shot | rag")
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TrainResult(BaseModel):
    """Training job result."""

    job_id: str = Field(default_factory=lambda: f"train-{uuid.uuid4().hex[:8]}")
    status: str = "accepted"
    records_processed: int = 0
    domain: str
    method: str
    message: str = "Training job submitted successfully"


# Rebuild forward refs
Task.model_rebuild()
