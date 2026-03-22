"""
CVG Neuron AI Orchestration System — NeuronOrchestrator
Version: 2.0.0 | Clearview Geographic LLC

The NeuronOrchestrator is the central coordination engine.
It receives tasks, routes them, selects agents, dispatches to the
CognitiveProcessor, records results, and triggers learning.

Exposes:
  execute_task(request)  → TaskResult
  get_status()           → system status dict
  learn_from_execution() → update internal knowledge
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.logger import get_logger
from app.memory.neuro_cache import get_neuro_cache
from app.models.response import DashboardStats, HealthStatus
from app.models.task import (
    CognitiveLevel, Task, TaskRequest, TaskResult, TaskStatus,
)
from app.orchestrator.agent_registry import AgentRegistry
from app.orchestrator.cognitive_processor import CognitiveProcessor
from app.orchestrator.task_router import TaskRouter
from app.services.hive_service import HiveService
from app.services.comb_service import CombService
from app.services.observability_service import ObservabilityService

log = get_logger("neuron-orchestrator")


class NeuronOrchestrator:
    """
    Central AI task coordinator for the CVG Neuron system.

    Lifecycle:
        orchestrator = NeuronOrchestrator()
        await orchestrator.startup()
        result = await orchestrator.execute_task(request)
        await orchestrator.shutdown()
    """

    # Identity constants
    NEURON_ID = settings.neuron_id
    EMPLOYEE_ID = settings.neuron_employee_id
    PRIMARY_HIVE = settings.neuron_primary_hive
    PRIMARY_QUEEN = settings.neuron_primary_queen

    def __init__(self) -> None:
        self.registry = AgentRegistry()
        self.processor = CognitiveProcessor()
        self.router = TaskRouter()
        self.cache = get_neuro_cache()
        self.hive = HiveService()
        self.comb = CombService()
        self.observability = ObservabilityService()

        # Runtime state
        self._active_tasks: Dict[str, Task] = {}
        self._task_history: Dict[str, Task] = {}
        self._started_at: Optional[datetime] = None
        self._tasks_completed: int = 0
        self._tasks_failed: int = 0
        self._total_tokens: int = 0
        self._lock = asyncio.Lock()

        log.info(
            "NeuronOrchestrator created",
            neuron_id=self.NEURON_ID,
            employee_id=self.EMPLOYEE_ID,
            primary_hive=self.PRIMARY_HIVE,
            primary_queen=self.PRIMARY_QUEEN,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        """Initialize all subsystems."""
        log.info("NeuronOrchestrator starting up...")
        self._started_at = datetime.now(timezone.utc)

        # Start agent pool
        await self.registry.startup()

        # Warm up connections (non-blocking)
        asyncio.create_task(self._warm_up_connections())

        log.info(
            "NeuronOrchestrator online",
            neuron_id=self.NEURON_ID,
            agents=self.registry.stats()["total"],
        )

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        log.info("NeuronOrchestrator shutting down...")

        # Wait for active tasks (with timeout)
        if self._active_tasks:
            log.warning("Active tasks during shutdown", count=len(self._active_tasks))
            try:
                await asyncio.wait_for(
                    self._wait_for_active_tasks(),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                log.warning("Shutdown timeout — forcing close")

        await self.registry.shutdown()
        await self.cache.clear()
        log.info("NeuronOrchestrator offline")

    # ── Core: Execute Task ────────────────────────────────────────────────────

    async def execute_task(self, request: TaskRequest) -> TaskResult:
        """
        Main entry point. Accepts a TaskRequest and returns a TaskResult.

        Pipeline:
          1. Route (determine cognitive level + priority)
          2. Analyze requirements
          3. Select agents
          4. Dispatch to CognitiveProcessor
          5. Record result + learn
        """
        t0 = time.monotonic()

        # 1. Route
        routed = self.router.route(request)

        # 2. Create task record
        task = Task(
            task_id=routed.task_id,
            input=routed.input,
            context=routed.context,
            cognitive_level=routed.cognitive_level,
            priority=routed.priority,
            input_method=routed.input_method,
            status=TaskStatus.PROCESSING,
            started_at=datetime.now(timezone.utc),
            session_id=routed.session_id,
            user_id=routed.user_id,
            tags=routed.tags,
        )

        async with self._lock:
            self._active_tasks[task.task_id] = task

        log.info(
            "Task execution started",
            task_id=task.task_id,
            level=task.cognitive_level,
            priority=task.priority,
            method=task.input_method,
        )

        try:
            # 3. Select agents
            agents = await self.registry.select_for_task(
                cognitive_level=task.cognitive_level,
                input_method=task.input_method,
                context=task.context,
            )

            if not agents:
                raise RuntimeError("No agents available to handle this task")

            # 4. Execute
            result, cognitive_trace = await asyncio.wait_for(
                self.processor.process(routed, agents, self.registry),
                timeout=routed.timeout or settings.task_timeout_seconds,
            )

            # 5. Finalize task record
            duration_ms = int((time.monotonic() - t0) * 1000)
            task.status = TaskStatus.COMPLETED
            task.completed_at = datetime.now(timezone.utc)
            task.duration_ms = duration_ms
            task.result = result
            task.cognitive_trace = cognitive_trace

            async with self._lock:
                self._tasks_completed += 1
                self._total_tokens += result.total_tokens or 0

            log.info(
                "Task completed",
                task_id=task.task_id,
                duration_ms=duration_ms,
                tokens=result.total_tokens,
                cognitive_level=task.cognitive_level,
            )

            # 6. Async: persist to COMB, push observability metrics
            asyncio.create_task(self._post_execution(task, result))

            return result

        except asyncio.TimeoutError:
            task.status = TaskStatus.TIMEOUT
            task.error = f"Task timed out after {routed.timeout or settings.task_timeout_seconds}s"
            async with self._lock:
                self._tasks_failed += 1
            log.error("Task timed out", task_id=task.task_id)
            raise

        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.error = str(exc)
            async with self._lock:
                self._tasks_failed += 1
            log.exception("Task failed", task_id=task.task_id, error=str(exc))
            raise

        finally:
            async with self._lock:
                self._active_tasks.pop(task.task_id, None)
                self._task_history[task.task_id] = task

    # ── Task retrieval ────────────────────────────────────────────────────────

    async def get_task(self, task_id: str) -> Optional[Task]:
        return (
            self._active_tasks.get(task_id)
            or self._task_history.get(task_id)
        )

    async def get_active_tasks(self) -> List[Task]:
        return list(self._active_tasks.values())

    async def get_history(self, limit: int = 100) -> List[Task]:
        tasks = list(self._task_history.values())
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)[:limit]

    # ── Status & Dashboard ────────────────────────────────────────────────────

    async def get_health(self) -> HealthStatus:
        """Return system health check."""
        import psutil
        import os

        try:
            mem = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
            cpu = psutil.cpu_percent(interval=0.1)
        except Exception:
            mem = None
            cpu = None

        agent_stats = self.registry.stats()
        cache_stats = self.cache.stats()
        uptime = (
            (datetime.now(timezone.utc) - self._started_at).total_seconds()
            if self._started_at
            else 0.0
        )

        # Check CVG subsystem connectivity
        hive_ok = await self.hive.health_check()
        comb_ok = await self.comb.health_check()
        obs_ok = await self.observability.health_check()

        status = "healthy"
        if not hive_ok or not comb_ok:
            status = "degraded"

        return HealthStatus(
            status=status,
            neuron_id=self.NEURON_ID,
            version=settings.app_version,
            environment=settings.environment,
            uptime_seconds=round(uptime, 1),
            hive="healthy" if hive_ok else "unreachable",
            comb="healthy" if comb_ok else "unreachable",
            observability="healthy" if obs_ok else "unreachable",
            active_tasks=len(self._active_tasks),
            total_agents=agent_stats["total"],
            cache_size=cache_stats["current_size"],
            memory_mb=round(mem, 1) if mem else None,
            cpu_percent=round(cpu, 1) if cpu else None,
        )

    async def get_dashboard(self) -> DashboardStats:
        """Return dashboard statistics."""
        agent_stats = self.registry.stats()
        cache_stats = self.cache.stats()
        uptime = (
            (datetime.now(timezone.utc) - self._started_at).total_seconds()
            if self._started_at
            else 0.0
        )

        hive_nodes = await self.hive.get_online_nodes()

        total = self._tasks_completed + self._tasks_failed
        success_rate = self._tasks_completed / total if total > 0 else 1.0

        return DashboardStats(
            tasks_total=total,
            tasks_active=len(self._active_tasks),
            tasks_completed_today=self._tasks_completed,
            tasks_failed_today=self._tasks_failed,
            task_success_rate=round(success_rate, 4),
            agents_total=agent_stats["total"],
            agents_idle=agent_stats["idle"],
            agents_busy=agent_stats["busy"],
            agents_offline=agent_stats["offline"],
            tokens_used_today=self._total_tokens,
            uptime_seconds=round(uptime, 1),
            cache_hit_rate=round(cache_stats["hit_rate"], 4),
            hive_nodes_online=len(hive_nodes),
        )

    async def get_info(self) -> Dict[str, Any]:
        """Return system identity and configuration info."""
        return {
            "neuron_id": self.NEURON_ID,
            "employee_id": self.EMPLOYEE_ID,
            "primary_hive": self.PRIMARY_HIVE,
            "primary_queen": self.PRIMARY_QUEEN,
            "version": settings.app_version,
            "environment": settings.environment,
            "default_cognitive_level": settings.default_cognitive_level,
            "default_ai_provider": settings.default_ai_provider,
            "hive_nodes": settings.hive_nodes,
            "capabilities": [
                "task_orchestration",
                "multi_agent_processing",
                "cognitive_processing",
                "memory_caching",
                "hive_distribution",
                "comb_persistence",
                "observability_reporting",
            ],
        }

    # ── Learning ──────────────────────────────────────────────────────────────

    async def learn_from_execution(self, task: Task) -> None:
        """
        Extract learning signals from completed tasks and update
        NeuroCache / COMB knowledge stores.
        """
        if task.status != TaskStatus.COMPLETED or not task.result:
            return

        if task.cognitive_trace:
            trace = task.cognitive_trace
            log.info(
                "Learning from execution",
                task_id=task.task_id,
                agents=len(trace.agents_used),
                tokens=trace.total_tokens,
                level=trace.level,
            )

        # Store in COMB BitHive for hot recall
        try:
            await self.comb.store(
                key=f"task_result:{task.task_id}",
                value={
                    "input": task.input,
                    "output": task.result.output if task.result else None,
                    "cognitive_level": task.cognitive_level,
                    "duration_ms": task.duration_ms,
                },
                tier="bithive",
            )
        except Exception as exc:
            log.warning("COMB store failed during learning", error=str(exc))

    async def generate_mcp(self, task_id: str) -> Dict[str, Any]:
        """
        Generate a Machine-Checkable Proof (MCP) for a completed task.
        Returns a signed audit record suitable for WaxCell immutable storage.
        """
        task = await self.get_task(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")

        mcp = {
            "mcp_id": f"mcp-{uuid.uuid4().hex[:12]}",
            "task_id": task_id,
            "neuron_id": self.NEURON_ID,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": task.status,
            "cognitive_level": task.cognitive_level,
            "agents_used": task.cognitive_trace.agents_used if task.cognitive_trace else [],
            "duration_ms": task.duration_ms,
            "verified": task.status == TaskStatus.COMPLETED,
        }

        # Store in WaxCell immutable audit tier
        try:
            await self.comb.store(
                key=f"mcp:{mcp['mcp_id']}",
                value=mcp,
                tier="waxcell",  # Immutable audit storage
            )
        except Exception as exc:
            log.warning("MCP WaxCell store failed", error=str(exc))

        return mcp

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _post_execution(self, task: Task, result: TaskResult) -> None:
        """Fire-and-forget post-execution tasks."""
        try:
            await self.learn_from_execution(task)
        except Exception as exc:
            log.warning("Post-execution learning failed", error=str(exc))

        try:
            await self.observability.push_task_metric(task, result)
        except Exception as exc:
            log.warning("Observability push failed", error=str(exc))

    async def _warm_up_connections(self) -> None:
        """Non-blocking connection warmup at startup."""
        try:
            await self.hive.health_check()
            await self.comb.health_check()
            await self.observability.health_check()
            log.info("CVG subsystem connections warmed up")
        except Exception as exc:
            log.warning("Subsystem warmup failed (will retry on demand)", error=str(exc))

    async def _wait_for_active_tasks(self) -> None:
        while self._active_tasks:
            await asyncio.sleep(0.5)
