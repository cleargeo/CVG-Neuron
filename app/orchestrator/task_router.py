"""
CVG Neuron AI Orchestration System — Task Router
Version: 2.0.0 | Clearview Geographic LLC

Routes incoming tasks to the correct cognitive level, priority,
and CVG Hive compute strategy based on input method and context.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from app.core.config import settings
from app.core.logger import get_logger
from app.models.task import CognitiveLevel, InputMethod, TaskPriority, TaskRequest

log = get_logger("task-router")


# ── Routing Tables ────────────────────────────────────────────────────────────

# Default cognitive level per input method
INPUT_METHOD_COGNITIVE_MAP: Dict[str, CognitiveLevel] = {
    InputMethod.CHAT: CognitiveLevel.ADVANCED,
    InputMethod.MAP_INPUT: CognitiveLevel.NEURAL,
    InputMethod.FILE_UPLOAD: CognitiveLevel.ADVANCED,
    InputMethod.CALCULATORS: CognitiveLevel.NEURAL,
    InputMethod.WIZARDS: CognitiveLevel.ADVANCED,
    InputMethod.VIDEO_CONSULTATION: CognitiveLevel.NEURAL,
    InputMethod.API_INTEGRATIONS: CognitiveLevel.ADVANCED,
    InputMethod.EMAIL_PARSER: CognitiveLevel.ADVANCED,
    InputMethod.SMS_SERVICE: CognitiveLevel.BASIC,
    InputMethod.QR_CODE_SYSTEM: CognitiveLevel.BASIC,
    InputMethod.MOBILE_FIELD: CognitiveLevel.ADVANCED,
    InputMethod.DIRECT_API: CognitiveLevel.ADVANCED,
}

# CVG Hive priority per input method (from cvg-hive-compute-rules.json)
INPUT_METHOD_PRIORITY_MAP: Dict[str, TaskPriority] = {
    InputMethod.CHAT: TaskPriority.CRITICAL,
    InputMethod.MAP_INPUT: TaskPriority.HIGH,
    InputMethod.FILE_UPLOAD: TaskPriority.NORMAL,
    InputMethod.CALCULATORS: TaskPriority.HIGH,
    InputMethod.WIZARDS: TaskPriority.NORMAL,
    InputMethod.VIDEO_CONSULTATION: TaskPriority.NORMAL,
    InputMethod.API_INTEGRATIONS: TaskPriority.NORMAL,
    InputMethod.EMAIL_PARSER: TaskPriority.NORMAL,
    InputMethod.SMS_SERVICE: TaskPriority.HIGH,
    InputMethod.QR_CODE_SYSTEM: TaskPriority.LOW,
    InputMethod.MOBILE_FIELD: TaskPriority.HIGH,
    InputMethod.DIRECT_API: TaskPriority.NORMAL,
}

# Hive distribution strategy per input method
INPUT_METHOD_HIVE_STRATEGY: Dict[str, str] = {
    InputMethod.MAP_INPUT: "parallel",
    InputMethod.FILE_UPLOAD: "queue_based",
    InputMethod.CALCULATORS: "gpu_priority",
    InputMethod.VIDEO_CONSULTATION: "sequential",
    InputMethod.CHAT: "round_robin",
    InputMethod.EMAIL_PARSER: "round_robin",
    InputMethod.SMS_SERVICE: "round_robin",
    InputMethod.API_INTEGRATIONS: "least_loaded",
}

# Keywords that elevate cognitive level
AUTONOMOUS_KEYWORDS = [
    "autonomous", "self-direct", "learn from", "optimize yourself",
    "continuously improve", "analyze and adapt",
]

NEURAL_KEYWORDS = [
    "remember", "recall", "session", "history", "context",
    "spatial analysis", "flood model", "terrain", "multi-step",
]


class TaskRouter:
    """
    Routes a TaskRequest to the appropriate cognitive level, priority,
    and Hive distribution strategy.

    The router can override defaults in the request when the automatic
    routing produces a better fit (e.g., upgrading a basic request to
    neural if keywords suggest complex processing is needed).
    """

    def route(self, request: TaskRequest) -> TaskRequest:
        """
        Analyze the request and return an (optionally upgraded) copy
        with routing decisions applied.

        Returns a new TaskRequest — does not mutate the original.
        """
        # Determine cognitive level
        cognitive_level = self._determine_cognitive_level(request)

        # Determine priority
        priority = self._determine_priority(request)

        # Log routing decision
        log.info(
            "Task routed",
            task_id=request.task_id,
            original_level=request.cognitive_level,
            routed_level=cognitive_level,
            original_priority=request.priority,
            routed_priority=priority,
            input_method=request.input_method,
        )

        # Return updated request
        return request.model_copy(
            update={
                "cognitive_level": cognitive_level,
                "priority": priority,
            }
        )

    def get_hive_strategy(self, input_method: str) -> str:
        """Return the CVG Hive distribution strategy for an input method."""
        return INPUT_METHOD_HIVE_STRATEGY.get(input_method, "round_robin")

    # ── Private routing logic ─────────────────────────────────────────────────

    def _determine_cognitive_level(self, request: TaskRequest) -> CognitiveLevel:
        """
        Determine the optimal cognitive level.

        Priority order:
        1. Explicit request (never downgrade from explicit)
        2. Keyword-based elevation
        3. Input method default
        4. Settings default
        """
        input_lower = request.input.lower()

        # Check for autonomous keywords — always elevate
        if any(kw in input_lower for kw in AUTONOMOUS_KEYWORDS):
            return CognitiveLevel.AUTONOMOUS

        # Check for neural keywords
        if any(kw in input_lower for kw in NEURAL_KEYWORDS):
            # Only elevate if not already above neural
            if request.cognitive_level in (CognitiveLevel.BASIC, CognitiveLevel.ADVANCED):
                return CognitiveLevel.NEURAL

        # Use input method default if request uses the system default
        system_default = CognitiveLevel(settings.default_cognitive_level)
        if request.cognitive_level == system_default:
            method_default = INPUT_METHOD_COGNITIVE_MAP.get(
                request.input_method, system_default
            )
            return method_default

        # Respect explicit request level
        return request.cognitive_level

    def _determine_priority(self, request: TaskRequest) -> TaskPriority:
        """
        Determine task priority.

        Use input method mapping unless an explicit (non-default) priority was set.
        """
        default_priority = TaskPriority.NORMAL
        if request.priority != default_priority:
            # Explicit priority — respect it
            return request.priority

        # Use input method priority
        return INPUT_METHOD_PRIORITY_MAP.get(request.input_method, TaskPriority.NORMAL)

    def analyze_requirements(self, request: TaskRequest) -> Dict[str, Any]:
        """
        Return a full analysis of task requirements for routing and
        Hive resource allocation (used by NeuronOrchestrator).
        """
        return {
            "task_id": request.task_id,
            "cognitive_level": request.cognitive_level,
            "priority": request.priority,
            "input_method": request.input_method,
            "hive_strategy": self.get_hive_strategy(request.input_method),
            "estimated_complexity": self._estimate_complexity(request),
            "requires_gpu": request.input_method in (
                InputMethod.CALCULATORS, InputMethod.VIDEO_CONSULTATION
            ),
            "requires_memory": request.cognitive_level in (
                CognitiveLevel.NEURAL, CognitiveLevel.AUTONOMOUS
            ),
            "session_aware": request.session_id is not None,
        }

    def _estimate_complexity(self, request: TaskRequest) -> str:
        """Rough complexity estimate: low | medium | high | very_high"""
        level = request.cognitive_level
        if level == CognitiveLevel.BASIC:
            return "low"
        elif level == CognitiveLevel.ADVANCED:
            length = len(request.input)
            return "medium" if length < 500 else "high"
        elif level == CognitiveLevel.NEURAL:
            return "high"
        else:
            return "very_high"
