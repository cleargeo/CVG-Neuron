"""
CVG Neuron AI Orchestration System — Agent Models
Version: 2.0.0 | Clearview Geographic LLC

Schemas for the CVG Neuron autonomous agent pool.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Enumerations ──────────────────────────────────────────────────────────────

class AgentType(str, Enum):
    """Specialization types for autonomous agents."""
    # Core AI
    NLP = "nlp"                         # Natural language processing
    REASONING = "reasoning"             # Logical reasoning & planning
    SYNTHESIS = "synthesis"             # Multi-source synthesis
    MEMORY = "memory"                   # Memory retrieval & storage
    LEARNING = "learning"               # Continuous learning

    # Domain specialists
    GIS = "gis"                         # Geographic / spatial analysis
    HYDROLOGY = "hydrology"             # Flood, rainfall, water systems
    NETWORK = "network"                 # Infrastructure network management
    SECURITY = "security"               # StratoVault / security ops
    INFRASTRUCTURE = "infrastructure"   # Server / container management

    # Integration agents
    HIVE = "hive"                       # CVG Hive coordinator
    COMB = "comb"                       # CVG COMB memory access
    OBSERVABILITY = "observability"     # Monitoring & metrics
    API = "api"                         # External API orchestration

    # Utility agents
    CODE = "code"                       # Code generation
    DATA = "data"                       # Data processing & transformation
    FILE = "file"                       # File processing (OCR, extract)
    SCHEDULER = "scheduler"             # Task scheduling


class AgentStatus(str, Enum):
    """Operational state of an agent."""
    IDLE = "idle"
    BUSY = "busy"
    OFFLINE = "offline"
    ERROR = "error"
    INITIALIZING = "initializing"
    RETIRING = "retiring"


class AgentCapability(str, Enum):
    """Discrete capabilities an agent advertises."""
    TEXT_GENERATION = "text_generation"
    TEXT_CLASSIFICATION = "text_classification"
    ENTITY_EXTRACTION = "entity_extraction"
    SENTIMENT_ANALYSIS = "sentiment_analysis"
    SUMMARIZATION = "summarization"
    TRANSLATION = "translation"
    QUESTION_ANSWERING = "question_answering"
    CODE_GENERATION = "code_generation"
    CODE_REVIEW = "code_review"
    DATA_ANALYSIS = "data_analysis"
    IMAGE_ANALYSIS = "image_analysis"
    SPATIAL_ANALYSIS = "spatial_analysis"
    VECTOR_SEARCH = "vector_search"
    WEB_SEARCH = "web_search"
    API_CALL = "api_call"
    DATABASE_QUERY = "database_query"
    FILE_PROCESSING = "file_processing"
    MONITORING = "monitoring"


# ── Agent Models ──────────────────────────────────────────────────────────────

class AgentPerformanceMetrics(BaseModel):
    """Real-time performance tracking for an agent."""

    tasks_completed: int = 0
    tasks_failed: int = 0
    total_tokens_used: int = 0
    average_latency_ms: float = 0.0
    success_rate: float = 1.0
    efficiency_score: float = 1.0     # 0.0–1.0; drives agent selection weight
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def update(self, success: bool, latency_ms: int, tokens: int = 0) -> None:
        """Incrementally update metrics after a task execution."""
        total = self.tasks_completed + self.tasks_failed
        if success:
            self.tasks_completed += 1
        else:
            self.tasks_failed += 1

        new_total = total + 1
        # Running average for latency
        self.average_latency_ms = (
            (self.average_latency_ms * total + latency_ms) / new_total
        )
        self.total_tokens_used += tokens
        self.success_rate = self.tasks_completed / new_total if new_total > 0 else 1.0

        # Efficiency score: weighted combination
        latency_score = max(0.0, 1.0 - (self.average_latency_ms / 30000))  # Penalize > 30s
        self.efficiency_score = (self.success_rate * 0.7) + (latency_score * 0.3)
        self.last_updated = datetime.now(timezone.utc)


class Agent(BaseModel):
    """An autonomous CVG Neuron agent instance."""

    agent_id: str
    name: str
    agent_type: AgentType
    status: AgentStatus = AgentStatus.IDLE
    capabilities: List[AgentCapability] = Field(default_factory=list)

    # AI backend
    provider: str = "ollama"                # openai | anthropic | ollama
    model: str = "llama3.2"
    system_prompt: Optional[str] = None

    # Resource limits
    max_concurrent: int = 5
    current_tasks: int = 0
    priority_weight: float = Field(default=1.0, ge=0.1, le=10.0)

    # Performance
    metrics: AgentPerformanceMetrics = Field(default_factory=AgentPerformanceMetrics)

    # Lifecycle
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_active: Optional[datetime] = None
    hive_node: Optional[str] = None       # Which Hive node runs this agent
    queen: str = "CVG-QUEEN-13"

    # Metadata
    tags: List[str] = Field(default_factory=list)
    config: Dict[str, Any] = Field(default_factory=dict)

    @property
    def is_available(self) -> bool:
        return self.status == AgentStatus.IDLE and self.current_tasks < self.max_concurrent

    @property
    def load_factor(self) -> float:
        """0.0 = completely free, 1.0 = at capacity."""
        if self.max_concurrent == 0:
            return 1.0
        return self.current_tasks / self.max_concurrent

    def get_efficiency_score(self) -> float:
        """Primary signal used by AgentRegistry for selection."""
        availability_factor = 1.0 - self.load_factor
        return self.metrics.efficiency_score * availability_factor * self.priority_weight


class AgentCreate(BaseModel):
    """Payload to register a new agent."""

    name: str
    agent_type: AgentType
    capabilities: List[AgentCapability] = Field(default_factory=list)
    provider: str = "ollama"
    model: str = "llama3.2"
    system_prompt: Optional[str] = None
    max_concurrent: int = 5
    priority_weight: float = 1.0
    hive_node: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    config: Dict[str, Any] = Field(default_factory=dict)


class AgentUpdate(BaseModel):
    """Partial agent update."""

    status: Optional[AgentStatus] = None
    system_prompt: Optional[str] = None
    max_concurrent: Optional[int] = None
    priority_weight: Optional[float] = None
    config: Optional[Dict[str, Any]] = None
    tags: Optional[List[str]] = None


class AgentSummary(BaseModel):
    """Compact agent representation for list endpoints."""

    agent_id: str
    name: str
    agent_type: AgentType
    status: AgentStatus
    efficiency_score: float
    current_tasks: int
    max_concurrent: int
    provider: str
    model: str
    hive_node: Optional[str] = None
