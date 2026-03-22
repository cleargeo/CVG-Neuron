"""
CVG Neuron AI Orchestration System — Agent Registry
Version: 2.0.0 | Clearview Geographic LLC

Manages the pool of autonomous agents: registration, discovery,
health-checking, and optimal agent selection for tasks.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.logger import get_logger
from app.models.agent import (
    Agent, AgentCreate, AgentStatus, AgentSummary, AgentType, AgentCapability,
    AgentPerformanceMetrics,
)

log = get_logger("agent-registry")


# ── Default agent pool definition ────────────────────────────────────────────

DEFAULT_AGENTS: List[Dict[str, Any]] = [
    {
        "name": "CVG-NLP-Prime",
        "agent_type": AgentType.NLP,
        "capabilities": [
            AgentCapability.TEXT_GENERATION,
            AgentCapability.SUMMARIZATION,
            AgentCapability.QUESTION_ANSWERING,
            AgentCapability.SENTIMENT_ANALYSIS,
        ],
        "provider": settings.default_ai_provider,
        "model": settings.ollama_default_model,
        "system_prompt": (
            "You are CVG-NLP-Prime, a natural language specialist for "
            "Clearview Geographic. You excel at understanding, summarizing, "
            "and answering questions about geographic and engineering projects."
        ),
        "max_concurrent": 10,
        "priority_weight": 1.5,
        "hive_node": settings.hive_node_0,
    },
    {
        "name": "CVG-Reasoning-001",
        "agent_type": AgentType.REASONING,
        "capabilities": [
            AgentCapability.DATA_ANALYSIS,
            AgentCapability.CODE_REVIEW,
        ],
        "provider": settings.default_ai_provider,
        "model": settings.ollama_default_model,
        "system_prompt": (
            "You are CVG-Reasoning-001, a logical reasoning and planning agent. "
            "You analyze complex multi-step problems, evaluate evidence, and "
            "produce structured plans and decisions."
        ),
        "max_concurrent": 5,
        "priority_weight": 1.2,
        "hive_node": settings.hive_node_0,
    },
    {
        "name": "CVG-GIS-Analyst",
        "agent_type": AgentType.GIS,
        "capabilities": [
            AgentCapability.SPATIAL_ANALYSIS,
            AgentCapability.DATA_ANALYSIS,
        ],
        "provider": settings.default_ai_provider,
        "model": settings.ollama_default_model,
        "system_prompt": (
            "You are CVG-GIS-Analyst, a geographic information systems specialist. "
            "You interpret spatial data, perform coordinate analysis, evaluate "
            "flood risks, and guide GIS workflows for Clearview Geographic projects."
        ),
        "max_concurrent": 5,
        "priority_weight": 1.3,
        "hive_node": settings.hive_node_1,
        "tags": ["gis", "spatial", "flood"],
    },
    {
        "name": "CVG-Hydro-Expert",
        "agent_type": AgentType.HYDROLOGY,
        "capabilities": [
            AgentCapability.DATA_ANALYSIS,
            AgentCapability.QUESTION_ANSWERING,
        ],
        "provider": settings.default_ai_provider,
        "model": settings.ollama_default_model,
        "system_prompt": (
            "You are CVG-Hydro-Expert, a hydrology and water resources specialist. "
            "You analyze rainfall data, storm surge models, flood risk assessments, "
            "and sea-level rise projections for engineering and planning projects."
        ),
        "max_concurrent": 5,
        "priority_weight": 1.2,
        "hive_node": settings.hive_node_1,
        "tags": ["hydrology", "flood", "rainfall"],
    },
    {
        "name": "CVG-Infra-Monitor",
        "agent_type": AgentType.INFRASTRUCTURE,
        "capabilities": [
            AgentCapability.MONITORING,
            AgentCapability.API_CALL,
            AgentCapability.DATABASE_QUERY,
        ],
        "provider": settings.default_ai_provider,
        "model": settings.ollama_default_model,
        "system_prompt": (
            "You are CVG-Infra-Monitor, an infrastructure management agent. "
            "You monitor server health, interpret container status, diagnose "
            "system issues, and recommend remediation actions across CVG Queens."
        ),
        "max_concurrent": 8,
        "priority_weight": 1.0,
        "hive_node": settings.hive_node_0,
        "tags": ["infrastructure", "monitoring", "queens"],
    },
    {
        "name": "CVG-Synthesis-Core",
        "agent_type": AgentType.SYNTHESIS,
        "capabilities": [
            AgentCapability.TEXT_GENERATION,
            AgentCapability.SUMMARIZATION,
        ],
        "provider": settings.default_ai_provider,
        "model": settings.ollama_default_model,
        "system_prompt": (
            "You are CVG-Synthesis-Core, a multi-source synthesis specialist. "
            "You combine outputs from multiple agents into coherent, unified "
            "responses. You resolve conflicts, identify consensus, and produce "
            "well-structured summaries."
        ),
        "max_concurrent": 5,
        "priority_weight": 1.4,
        "hive_node": settings.hive_node_0,
    },
    {
        "name": "CVG-Code-Gen",
        "agent_type": AgentType.CODE,
        "capabilities": [
            AgentCapability.CODE_GENERATION,
            AgentCapability.CODE_REVIEW,
        ],
        "provider": settings.default_ai_provider,
        "model": settings.ollama_default_model,
        "system_prompt": (
            "You are CVG-Code-Gen, a software engineering agent specializing in "
            "Python, PHP, PowerShell, and GIS scripting. You write clean, "
            "well-documented code following CVG conventions."
        ),
        "max_concurrent": 5,
        "priority_weight": 1.0,
        "hive_node": settings.hive_node_2,
        "tags": ["code", "python", "php"],
    },
    {
        "name": "CVG-Network-Ops",
        "agent_type": AgentType.NETWORK,
        "capabilities": [
            AgentCapability.MONITORING,
            AgentCapability.API_CALL,
        ],
        "provider": settings.default_ai_provider,
        "model": settings.ollama_default_model,
        "system_prompt": (
            "You are CVG-Network-Ops, a network infrastructure specialist. "
            "You manage DNS, VPN tunnels, routing, firewall policies, and "
            "inter-Hive mesh networking for the CVG ecosystem."
        ),
        "max_concurrent": 5,
        "priority_weight": 1.0,
        "hive_node": settings.hive_node_0,
        "tags": ["network", "dns", "vpn"],
    },
]


class AgentRegistry:
    """
    Central registry for all CVG Neuron autonomous agents.

    Responsibilities:
    - Register / deregister agents
    - Track status and performance metrics
    - Select optimal agent(s) for a given task
    - Health check agents
    - Provide agent pool statistics
    """

    def __init__(self) -> None:
        self._agents: Dict[str, Agent] = {}
        self._lock = asyncio.Lock()
        log.info("AgentRegistry initialized")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        """Register the default agent pool at startup."""
        log.info("Registering default agent pool", count=len(DEFAULT_AGENTS))
        for agent_def in DEFAULT_AGENTS:
            await self.register(AgentCreate(**agent_def))
        log.info("Default agent pool ready", total_agents=len(self._agents))

    async def shutdown(self) -> None:
        """Mark all agents offline during graceful shutdown."""
        async with self._lock:
            for agent in self._agents.values():
                agent.status = AgentStatus.OFFLINE
        log.info("AgentRegistry shut down")

    # ── Registration ──────────────────────────────────────────────────────────

    async def register(self, create: AgentCreate) -> Agent:
        """Register a new agent. Returns the created Agent."""
        agent_id = f"agent-{uuid.uuid4().hex[:8]}"
        agent = Agent(
            agent_id=agent_id,
            name=create.name,
            agent_type=create.agent_type,
            status=AgentStatus.IDLE,
            capabilities=create.capabilities,
            provider=create.provider,
            model=create.model,
            system_prompt=create.system_prompt,
            max_concurrent=create.max_concurrent,
            priority_weight=create.priority_weight,
            hive_node=create.hive_node,
            tags=create.tags,
            config=create.config,
        )

        async with self._lock:
            self._agents[agent_id] = agent

        log.info(
            "Agent registered",
            agent_id=agent_id,
            name=create.name,
            type=create.agent_type,
            hive_node=create.hive_node,
        )
        return agent

    async def deregister(self, agent_id: str) -> bool:
        """Remove an agent from the registry. Returns True if found."""
        async with self._lock:
            if agent_id in self._agents:
                agent = self._agents.pop(agent_id)
                log.info("Agent deregistered", agent_id=agent_id, name=agent.name)
                return True
        return False

    # ── Retrieval ─────────────────────────────────────────────────────────────

    async def get(self, agent_id: str) -> Optional[Agent]:
        return self._agents.get(agent_id)

    async def list_all(self) -> List[Agent]:
        return list(self._agents.values())

    async def list_summaries(self) -> List[AgentSummary]:
        return [
            AgentSummary(
                agent_id=a.agent_id,
                name=a.name,
                agent_type=a.agent_type,
                status=a.status,
                efficiency_score=round(a.get_efficiency_score(), 4),
                current_tasks=a.current_tasks,
                max_concurrent=a.max_concurrent,
                provider=a.provider,
                model=a.model,
                hive_node=a.hive_node,
            )
            for a in self._agents.values()
        ]

    async def get_by_type(self, agent_type: AgentType) -> List[Agent]:
        return [a for a in self._agents.values() if a.agent_type == agent_type]

    async def get_by_capability(self, capability: AgentCapability) -> List[Agent]:
        return [a for a in self._agents.values() if capability in a.capabilities]

    # ── Selection ─────────────────────────────────────────────────────────────

    async def select_best(
        self,
        agent_type: Optional[AgentType] = None,
        capabilities: Optional[List[AgentCapability]] = None,
        count: int = 1,
        exclude: Optional[List[str]] = None,
    ) -> List[Agent]:
        """
        Select the best available agent(s) by efficiency score.

        Args:
            agent_type: Filter by agent type
            capabilities: Filter by required capabilities (ANY match)
            count: How many agents to return
            exclude: Agent IDs to exclude

        Returns:
            Sorted list of best-matching available agents (may be < count if insufficient)
        """
        exclude_set = set(exclude or [])

        candidates = [
            a for a in self._agents.values()
            if a.is_available
            and a.agent_id not in exclude_set
            and (agent_type is None or a.agent_type == agent_type)
            and (
                capabilities is None
                or any(cap in a.capabilities for cap in capabilities)
            )
        ]

        # Sort by efficiency score (desc)
        candidates.sort(key=lambda a: a.get_efficiency_score(), reverse=True)
        return candidates[:count]

    async def select_for_task(
        self,
        cognitive_level: str,
        input_method: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> List[Agent]:
        """
        Intelligently select agents based on cognitive level and input method.

        Maps:
          basic      → 1 NLP agent
          advanced   → NLP + Reasoning
          neural     → NLP + Reasoning + Synthesis + domain specialist
          autonomous → full pool including Memory + Learning
        """
        selections: List[Agent] = []

        if cognitive_level == "basic":
            nlp_agents = await self.select_best(agent_type=AgentType.NLP, count=1)
            selections.extend(nlp_agents)

        elif cognitive_level == "advanced":
            nlp = await self.select_best(agent_type=AgentType.NLP, count=1)
            reasoning = await self.select_best(agent_type=AgentType.REASONING, count=1)
            selections.extend(nlp + reasoning)

        elif cognitive_level == "neural":
            nlp = await self.select_best(agent_type=AgentType.NLP, count=1)
            reasoning = await self.select_best(agent_type=AgentType.REASONING, count=1)
            synthesis = await self.select_best(agent_type=AgentType.SYNTHESIS, count=1)
            # Domain specialist
            domain = await self._select_domain_agent(input_method, context)
            selections.extend(nlp + reasoning + synthesis + ([domain] if domain else []))

        elif cognitive_level == "autonomous":
            # Full multi-agent ensemble
            nlp = await self.select_best(agent_type=AgentType.NLP, count=1)
            reasoning = await self.select_best(agent_type=AgentType.REASONING, count=1)
            synthesis = await self.select_best(agent_type=AgentType.SYNTHESIS, count=1)
            memory = await self.select_best(agent_type=AgentType.MEMORY, count=1)
            domain = await self._select_domain_agent(input_method, context)
            selections.extend(nlp + reasoning + synthesis + memory + ([domain] if domain else []))

        # Fallback: any available agent
        if not selections:
            fallback = await self.select_best(count=1)
            selections.extend(fallback)

        return selections

    async def _select_domain_agent(
        self,
        input_method: str,
        context: Optional[Dict[str, Any]],
    ) -> Optional[Agent]:
        """Pick a domain specialist based on task context."""
        domain_map: Dict[str, AgentType] = {
            "map_input": AgentType.GIS,
            "calculators": AgentType.HYDROLOGY,
            "wizards": AgentType.HYDROLOGY,
            "api_integrations": AgentType.API,
        }

        agent_type = domain_map.get(input_method)
        if agent_type:
            agents = await self.select_best(agent_type=agent_type, count=1)
            return agents[0] if agents else None

        # Infer from context tags
        if context:
            tags = context.get("tags", [])
            if any(t in tags for t in ["gis", "spatial", "map"]):
                agents = await self.select_best(agent_type=AgentType.GIS, count=1)
                return agents[0] if agents else None
            if any(t in tags for t in ["flood", "hydrology", "rainfall"]):
                agents = await self.select_best(agent_type=AgentType.HYDROLOGY, count=1)
                return agents[0] if agents else None

        return None

    # ── State Management ──────────────────────────────────────────────────────

    async def mark_busy(self, agent_id: str) -> None:
        agent = self._agents.get(agent_id)
        if agent:
            agent.current_tasks += 1
            if agent.current_tasks >= agent.max_concurrent:
                agent.status = AgentStatus.BUSY
            agent.last_active = datetime.now(timezone.utc)

    async def mark_idle(self, agent_id: str, success: bool, latency_ms: int, tokens: int = 0) -> None:
        agent = self._agents.get(agent_id)
        if agent:
            agent.current_tasks = max(0, agent.current_tasks - 1)
            if agent.current_tasks < agent.max_concurrent:
                agent.status = AgentStatus.IDLE
            agent.metrics.update(success=success, latency_ms=latency_ms, tokens=tokens)

    async def set_status(self, agent_id: str, status: AgentStatus) -> None:
        agent = self._agents.get(agent_id)
        if agent:
            agent.status = status

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        agents = list(self._agents.values())
        return {
            "total": len(agents),
            "idle": sum(1 for a in agents if a.status == AgentStatus.IDLE),
            "busy": sum(1 for a in agents if a.status == AgentStatus.BUSY),
            "offline": sum(1 for a in agents if a.status == AgentStatus.OFFLINE),
            "error": sum(1 for a in agents if a.status == AgentStatus.ERROR),
            "types": list({a.agent_type for a in agents}),
        }
