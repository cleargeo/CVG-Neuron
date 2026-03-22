"""
CVG Neuron AI Orchestration System — Cognitive Processor
Version: 2.0.0 | Clearview Geographic LLC

The CognitiveProcessor implements 4 levels of AI task execution:

  Level 1 — basic:      Single-agent direct response
  Level 2 — advanced:   Multi-agent with synthesis
  Level 3 — neural:     Deep processing with memory reads/writes
  Level 4 — autonomous: Self-directed execution with learning loop

Each level builds on the previous, adding sophistication at the cost
of latency and resource usage.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.logger import get_logger
from app.memory.neuro_cache import get_neuro_cache
from app.models.agent import Agent
from app.models.task import (
    AgentExecution, CognitiveLevel, CognitiveTrace, TaskRequest, TaskResult,
)

log = get_logger("cognitive-processor")


class CognitiveProcessor:
    """
    Executes AI tasks through the appropriate cognitive processing pipeline.

    This class is stateless per-call — all state is carried in the
    task request and returned in the CognitiveTrace.
    """

    def __init__(self) -> None:
        self._cache = get_neuro_cache()
        log.info("CognitiveProcessor initialized")

    # ── Main entry point ──────────────────────────────────────────────────────

    async def process(
        self,
        request: TaskRequest,
        agents: List[Agent],
        registry,     # AgentRegistry — avoid circular import with type hint
    ) -> Tuple[TaskResult, CognitiveTrace]:
        """
        Execute a task at the specified cognitive level.

        Returns:
            (TaskResult, CognitiveTrace) — result and full audit trail
        """
        level = request.cognitive_level
        trace = CognitiveTrace(
            level=level,
            agents_used=[a.agent_id for a in agents],
        )

        log.info(
            "Starting cognitive processing",
            task_id=request.task_id,
            level=level,
            agent_count=len(agents),
        )

        try:
            if level == CognitiveLevel.BASIC:
                result = await self._process_basic(request, agents, trace, registry)
            elif level == CognitiveLevel.ADVANCED:
                result = await self._process_advanced(request, agents, trace, registry)
            elif level == CognitiveLevel.NEURAL:
                result = await self._process_neural(request, agents, trace, registry)
            elif level == CognitiveLevel.AUTONOMOUS:
                result = await self._process_autonomous(request, agents, trace, registry)
            else:
                result = await self._process_advanced(request, agents, trace, registry)
        except Exception as exc:
            log.exception("Cognitive processing failed", task_id=request.task_id, error=str(exc))
            result = TaskResult(
                task_id=request.task_id,
                output=f"Processing failed: {exc}",
                output_type="error",
            )

        return result, trace

    # ── Level 1: Basic ────────────────────────────────────────────────────────

    async def _process_basic(
        self,
        request: TaskRequest,
        agents: List[Agent],
        trace: CognitiveTrace,
        registry,
    ) -> TaskResult:
        """Single-agent direct response. Fastest, lowest resource usage."""
        if not agents:
            raise ValueError("No agents available for basic processing")

        agent = agents[0]
        execution, output = await self._run_agent(agent, request, registry)
        trace.executions.append(execution)
        trace.total_tokens = execution.tokens_used or 0

        return TaskResult(
            task_id=request.task_id,
            output=output,
            output_type="text",
            model_used=agent.model,
            provider_used=agent.provider,
            tokens_output=execution.tokens_used,
        )

    # ── Level 2: Advanced ─────────────────────────────────────────────────────

    async def _process_advanced(
        self,
        request: TaskRequest,
        agents: List[Agent],
        trace: CognitiveTrace,
        registry,
    ) -> TaskResult:
        """
        Multi-agent parallel execution with synthesis.
        Each agent processes the request independently, then outputs are combined.
        """
        if not agents:
            raise ValueError("No agents available for advanced processing")

        # Run all agents in parallel
        tasks = [self._run_agent(a, request, registry) for a in agents]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        executions: List[AgentExecution] = []
        outputs: List[str] = []

        for i, res in enumerate(results):
            if isinstance(res, Exception):
                log.warning(
                    "Agent execution failed",
                    agent_id=agents[i].agent_id,
                    error=str(res),
                )
                continue
            execution, output = res
            executions.append(execution)
            outputs.append(output)

        trace.executions.extend(executions)
        trace.total_tokens = sum(e.tokens_used or 0 for e in executions)

        if len(outputs) == 1:
            final_output = outputs[0]
        elif len(outputs) > 1:
            final_output = await self._synthesize(outputs, request, trace)
            trace.synthesis_applied = True
        else:
            final_output = "No agent outputs produced."

        total_tokens = sum(e.tokens_used or 0 for e in executions)
        primary_agent = agents[0] if agents else None

        return TaskResult(
            task_id=request.task_id,
            output=final_output,
            output_type="text",
            model_used=primary_agent.model if primary_agent else None,
            provider_used=primary_agent.provider if primary_agent else None,
            total_tokens=total_tokens,
        )

    # ── Level 3: Neural ───────────────────────────────────────────────────────

    async def _process_neural(
        self,
        request: TaskRequest,
        agents: List[Agent],
        trace: CognitiveTrace,
        registry,
    ) -> TaskResult:
        """
        Deep processing with NeuroCache memory reads/writes.
        Retrieves relevant context from cache before running agents,
        and stores results back for future use.
        """
        # 1. Check cache for similar recent results
        cache_key = f"neural:{request.session_id or 'anon'}:{hash(request.input) % 100000}"
        cached = await self._cache.get(cache_key)
        if cached:
            trace.memory_reads += 1
            log.info("Neural cache hit", task_id=request.task_id, key=cache_key)
            return TaskResult(
                task_id=request.task_id,
                output=cached,
                output_type="text",
                metadata={"cache_hit": True},
            )

        # 2. Retrieve session context
        context_key = f"session:{request.session_id}" if request.session_id else None
        session_context: Optional[str] = None
        if context_key:
            session_context = await self._cache.get(context_key)
            if session_context:
                trace.memory_reads += 1

        # 3. Build enriched request with context
        enriched_request = request
        if session_context:
            ctx = dict(request.context or {})
            ctx["session_history"] = session_context
            enriched_request = request.model_copy(update={"context": ctx})

        # 4. Run advanced multi-agent processing
        result = await self._process_advanced(enriched_request, agents, trace, registry)

        # 5. Store result and update session cache
        await self._cache.set(
            cache_key,
            result.output,
            ttl=1800,
            tags=[f"task:{request.task_id}"],
            task_id=request.task_id,
        )
        trace.memory_writes += 1

        if request.session_id:
            history = f"Q: {request.input}\nA: {result.output}"
            await self._cache.set(
                f"session:{request.session_id}",
                history,
                ttl=3600,
                tags=[f"session:{request.session_id}"],
            )
            trace.memory_writes += 1

        return result

    # ── Level 4: Autonomous ───────────────────────────────────────────────────

    async def _process_autonomous(
        self,
        request: TaskRequest,
        agents: List[Agent],
        trace: CognitiveTrace,
        registry,
    ) -> TaskResult:
        """
        Self-directed execution with learning loop.
        Runs neural processing, then evaluates result quality and
        optionally re-runs with refined prompts if confidence is low.
        """
        # Phase 1: Neural processing
        result = await self._process_neural(request, agents, trace, registry)

        # Phase 2: Self-evaluation
        confidence = await self._evaluate_confidence(result.output, request)
        result = result.model_copy(update={"confidence": confidence})

        # Phase 3: Refinement loop if confidence < threshold
        refinement_threshold = 0.65
        max_refinements = 2

        for attempt in range(max_refinements):
            if confidence >= refinement_threshold:
                break

            log.info(
                "Autonomous refinement triggered",
                task_id=request.task_id,
                confidence=confidence,
                attempt=attempt + 1,
            )

            refined_prompt = await self._build_refinement_prompt(
                original_input=request.input,
                current_output=result.output,
                confidence=confidence,
            )
            refined_request = request.model_copy(update={"input": refined_prompt})

            # Re-run with neural level
            result = await self._process_neural(refined_request, agents, trace, registry)
            confidence = await self._evaluate_confidence(result.output, request)
            result = result.model_copy(update={"confidence": confidence})

        # Phase 4: Mark learned
        result = result.model_copy(update={"learned": True})

        # Store high-confidence results in long-term cache
        if confidence >= refinement_threshold:
            await self._cache.set(
                f"learned:{hash(request.input) % 1000000}",
                {"input": request.input, "output": result.output, "confidence": confidence},
                ttl=86400,  # 24 hours
                tags=["learned", "autonomous"],
            )
            trace.memory_writes += 1

        return result

    # ── Agent execution ───────────────────────────────────────────────────────

    async def _run_agent(
        self,
        agent: Agent,
        request: TaskRequest,
        registry,
    ) -> Tuple[AgentExecution, str]:
        """
        Execute a single agent on a task.
        Calls the configured AI provider (Ollama/OpenAI/Anthropic).
        """
        started_at = datetime.now(timezone.utc)
        t0 = time.monotonic()

        execution = AgentExecution(
            agent_id=agent.agent_id,
            agent_type=agent.agent_type,
            started_at=started_at,
        )

        await registry.mark_busy(agent.agent_id)

        try:
            output = await self._call_ai_provider(agent, request)
            latency_ms = int((time.monotonic() - t0) * 1000)

            execution.completed_at = datetime.now(timezone.utc)
            execution.duration_ms = latency_ms
            execution.output = output
            execution.tokens_used = len(output.split()) * 2  # rough estimate

            await registry.mark_idle(
                agent.agent_id,
                success=True,
                latency_ms=latency_ms,
                tokens=execution.tokens_used,
            )

            return execution, output

        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            execution.completed_at = datetime.now(timezone.utc)
            execution.duration_ms = latency_ms
            execution.error = str(exc)

            await registry.mark_idle(agent.agent_id, success=False, latency_ms=latency_ms)
            raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    async def _call_ai_provider(self, agent: Agent, request: TaskRequest) -> str:
        """
        Call the configured AI provider for this agent.
        Supports: Ollama (local), OpenAI, Anthropic.
        """
        provider = agent.provider
        model = agent.model

        # Build messages
        messages = []
        if agent.system_prompt:
            messages.append({"role": "system", "content": agent.system_prompt})

        # Add session context if provided
        if request.context and request.context.get("session_history"):
            messages.append({
                "role": "system",
                "content": f"Previous conversation:\n{request.context['session_history']}",
            })

        messages.append({"role": "user", "content": request.input})

        params: Dict[str, Any] = {
            "max_tokens": request.max_tokens or 4096,
            "temperature": request.temperature or 0.7,
        }

        async with httpx.AsyncClient(timeout=settings.task_timeout_seconds) as client:
            if provider == "ollama":
                return await self._call_ollama(client, model, messages, params)
            elif provider == "openai":
                return await self._call_openai(client, model, messages, params)
            elif provider == "anthropic":
                return await self._call_anthropic(client, model, messages, params)
            else:
                raise ValueError(f"Unknown AI provider: {provider}")

    async def _call_ollama(
        self,
        client: httpx.AsyncClient,
        model: str,
        messages: List[Dict],
        params: Dict,
    ) -> str:
        response = await client.post(
            f"{settings.ollama_base_url}/api/chat",
            json={
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": params.get("temperature", 0.7),
                    "num_predict": params.get("max_tokens", 4096),
                },
            },
        )
        response.raise_for_status()
        data = response.json()
        return data.get("message", {}).get("content", "")

    async def _call_openai(
        self,
        client: httpx.AsyncClient,
        model: str,
        messages: List[Dict],
        params: Dict,
    ) -> str:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json={
                "model": model,
                "messages": messages,
                "max_tokens": params.get("max_tokens", 4096),
                "temperature": params.get("temperature", 0.7),
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

    async def _call_anthropic(
        self,
        client: httpx.AsyncClient,
        model: str,
        messages: List[Dict],
        params: Dict,
    ) -> str:
        # Anthropic uses separate system / messages format
        system_msg = ""
        user_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_msg += msg["content"] + "\n"
            else:
                user_messages.append(msg)

        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": params.get("max_tokens", 4096),
            "messages": user_messages,
        }
        if system_msg:
            payload["system"] = system_msg.strip()

        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
            },
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        return data["content"][0]["text"]

    # ── Synthesis & evaluation ────────────────────────────────────────────────

    async def _synthesize(
        self,
        outputs: List[str],
        request: TaskRequest,
        trace: CognitiveTrace,
    ) -> str:
        """
        Combine multiple agent outputs into a single coherent response.
        Uses a simple weighted synthesis for now; can be replaced with
        a dedicated synthesis agent call.
        """
        if len(outputs) == 1:
            return outputs[0]

        # Build synthesis prompt
        numbered = "\n\n".join(
            f"[Agent {i + 1} Response]\n{o}" for i, o in enumerate(outputs)
        )
        synthesis_prompt = (
            f"You are synthesizing multiple expert responses into one unified answer.\n\n"
            f"Original question: {request.input}\n\n"
            f"Expert responses:\n{numbered}\n\n"
            f"Provide a single, comprehensive, well-structured response that "
            f"incorporates the best insights from all agents. Resolve any conflicts "
            f"and present a unified, authoritative answer."
        )

        # Use a simple httpx call to the default Ollama instance
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    f"{settings.ollama_base_url}/api/chat",
                    json={
                        "model": settings.ollama_default_model,
                        "messages": [{"role": "user", "content": synthesis_prompt}],
                        "stream": False,
                    },
                )
                response.raise_for_status()
                data = response.json()
                synthesized = data.get("message", {}).get("content", "")
                if synthesized:
                    return synthesized
        except Exception as exc:
            log.warning("Synthesis call failed, using best output", error=str(exc))

        # Fallback: return the longest output (heuristic)
        return max(outputs, key=len)

    async def _evaluate_confidence(self, output: str, request: TaskRequest) -> float:
        """
        Heuristic confidence evaluation.
        In production, replace with a dedicated evaluation model call.
        """
        if not output or len(output.strip()) < 20:
            return 0.3

        # Simple heuristics
        score = 0.5
        if len(output) > 200:
            score += 0.1
        if len(output) > 500:
            score += 0.1
        if "I don't know" in output or "I'm not sure" in output:
            score -= 0.2
        if any(kw in output.lower() for kw in ["therefore", "analysis", "recommend", "conclusion"]):
            score += 0.1
        if "error" in output.lower() and "failed" in output.lower():
            score -= 0.3

        return max(0.0, min(1.0, score))

    async def _build_refinement_prompt(
        self,
        original_input: str,
        current_output: str,
        confidence: float,
    ) -> str:
        """Build a refined prompt for a second-pass autonomous refinement."""
        return (
            f"The previous response to the following question had low confidence "
            f"(score: {confidence:.2f}). Please provide a more thorough, accurate, "
            f"and comprehensive answer.\n\n"
            f"Original question: {original_input}\n\n"
            f"Previous answer (to improve upon):\n{current_output}\n\n"
            f"Please provide an improved, more confident response with clear "
            f"reasoning and specific details."
        )
