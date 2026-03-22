"""
CVG Neuron — Intelligence Engine
Core reasoning engine. Uses Ollama as the LLM backend with CVG-specific
system prompts, live infrastructure context injection, and memory-augmented
generation. This is what makes Neuron an AI, not just a chatbot wrapper.
"""
from __future__ import annotations

import os
import time
import uuid
from typing import AsyncIterator

import httpx

from cvg_neuron import memory
from cvg_neuron.knowledge import build_system_prompt, CVG_SERVICES, CVG_OPERATIONAL_KNOWLEDGE

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OLLAMA_URL   = os.environ.get("OLLAMA_URL",    "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL",   "cvg-neuron")
OLLAMA_ALT   = os.environ.get("OLLAMA_ALT_MODEL", "llama3.1:8b")
MAX_CTX_MSGS = int(os.environ.get("NEURON_MAX_CTX_MSGS", "12"))
TIMEOUT      = float(os.environ.get("NEURON_TIMEOUT", "120"))


# ---------------------------------------------------------------------------
# Hive routing — use best available Ollama node across Hive-0
# ---------------------------------------------------------------------------

async def _get_ollama_url(model_hint: str = "") -> str:
    """
    Get the best Ollama URL from the Hive cluster manager.
    Falls back to OLLAMA_URL env var if hive probe fails or no nodes found.
    This is what makes CVG Neuron use the ENTIRE HIVE as compute,
    not just one node.
    """
    try:
        from cvg_neuron.hive import get_best_ollama_url
        url = await get_best_ollama_url(model_hint or None)
        return url
    except Exception:
        return OLLAMA_URL


# ---------------------------------------------------------------------------
# Ollama connectivity
# ---------------------------------------------------------------------------

async def check_ollama() -> dict:
    """Check Ollama availability and list available models (via best hive node)."""
    url = await _get_ollama_url()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{url}/api/tags", timeout=5)
            if r.status_code == 200:
                models = [m["name"] for m in r.json().get("models", [])]
                return {"available": True, "models": models, "url": url}
            return {"available": False, "error": f"HTTP {r.status_code}", "url": url}
    except Exception as e:
        return {"available": False, "error": str(e), "url": url}


async def resolve_model() -> str:
    """Return the best available model name from the hive's best Ollama node."""
    status = await check_ollama()
    if not status.get("available"):
        return OLLAMA_MODEL  # return configured, let it fail naturally
    available = status.get("models", [])
    # Prefer: cvg-neuron (our identity model) → OLLAMA_ALT → any llama → first available
    for candidate in [OLLAMA_MODEL, OLLAMA_ALT]:
        if candidate in available:
            return candidate
        base = candidate.split(":")[0]
        match = next((m for m in available if m.startswith(base)), None)
        if match:
            return match
    return available[0] if available else OLLAMA_MODEL


# ---------------------------------------------------------------------------
# Core chat completion
# ---------------------------------------------------------------------------

async def _ollama_chat(
    messages: list[dict],
    model: str,
    stream: bool = False,
) -> dict | AsyncIterator[str]:
    """
    Raw Ollama /api/chat call — dynamically routed to the best hive node.
    This is the Hive-distributed inference path.
    """
    # Get best available Ollama URL for this model from hive cluster
    ollama_url = await _get_ollama_url(model)

    payload = {
        "model":    model,
        "messages": messages,
        "stream":   stream,
        "options": {
            "temperature":  0.3,      # Low temp: precise, factual
            "top_p":        0.9,
            "num_ctx":      8192,
            "num_predict":  2048,
        },
    }

    if stream:
        async def _gen():
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "POST", f"{ollama_url}/api/chat",
                    json=payload, timeout=TIMEOUT,
                ) as response:
                    async for line in response.aiter_lines():
                        if line:
                            import json
                            try:
                                chunk = json.loads(line)
                                if content := chunk.get("message", {}).get("content"):
                                    yield content
                                if chunk.get("done"):
                                    break
                            except Exception:
                                continue
        return _gen()
    else:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{ollama_url}/api/chat", json=payload, timeout=TIMEOUT
            )
            r.raise_for_status()
            return r.json()


# ---------------------------------------------------------------------------
# Session-aware chat
# ---------------------------------------------------------------------------

async def chat(
    user_message: str,
    session_id:   str  | None = None,
    live_context: dict | None = None,
    stream:       bool        = False,
) -> dict:
    """
    Core intelligence entry point.
    1. Get or create session
    2. Build system prompt with live CVG context
    3. Retrieve conversation history from memory
    4. Inject memory context (alerts, patterns, events)
    5. Call Ollama
    6. Store exchange
    7. Return response
    """
    if not session_id:
        session_id = str(uuid.uuid4())

    t0 = time.time()

    # Build system prompt
    live_for_prompt = {}
    if live_context and "health" in live_context:
        live_for_prompt = {
            k: {"healthy": v.get("healthy"), "response_ms": v.get("response_ms")}
            for k, v in live_context.get("health", {}).items()
        }
    system_prompt = build_system_prompt(live_for_prompt if live_for_prompt else None)

    # Append memory context to system prompt
    mem_ctx = memory.build_memory_context(session_id)
    if mem_ctx:
        system_prompt += f"\n\n## NEURON MEMORY CONTEXT\n{mem_ctx}"

    # If live_context has a summary string, append it
    if live_context and live_context.get("_summary"):
        system_prompt += f"\n\n{live_context['_summary']}"

    # Retrieve conversation history
    history = memory.get_conversation(session_id, limit=MAX_CTX_MSGS)

    # Build messages array
    messages = [{"role": "system", "content": system_prompt}]
    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})

    # Add current user message
    messages.append({"role": "user", "content": user_message})

    # Store user message
    memory.store_message(session_id, "user", user_message)

    # Resolve model
    model = await resolve_model()

    # Call Ollama
    try:
        if stream:
            gen = await _ollama_chat(messages, model, stream=True)
            # For streaming we return immediately — caller handles the iterator
            return {
                "session_id": session_id,
                "model":      model,
                "stream":     True,
                "generator":  gen,
            }

        result = await _ollama_chat(messages, model, stream=False)
        response_text = result.get("message", {}).get("content", "")
        elapsed_ms = round((time.time() - t0) * 1000)

        # Store response
        memory.store_message(session_id, "assistant", response_text)

        # Learn pattern from exchange
        topic = _infer_topic(user_message)
        if topic:
            memory.learn_pattern(
                key=f"chat:topic:{topic}",
                description=f"User asked about {topic}",
                metadata={"session": session_id, "model": model},
            )

        return {
            "session_id":  session_id,
            "model":       model,
            "response":    response_text,
            "elapsed_ms":  elapsed_ms,
            "stream":      False,
            "usage":       result.get("eval_count"),
        }

    except httpx.ConnectError:
        err = f"Ollama not reachable at {OLLAMA_URL}. CVG Neuron requires Ollama running with {model}."
        memory.record_observation("infrastructure", "ollama", err, "critical", "intelligence")
        return {
            "session_id": session_id,
            "model":      model,
            "response":   err,
            "error":      "ollama_unavailable",
            "elapsed_ms": round((time.time() - t0) * 1000),
        }
    except Exception as e:
        err = f"Intelligence error: {type(e).__name__}: {e}"
        return {
            "session_id": session_id,
            "model":      model,
            "response":   err,
            "error":      str(e),
            "elapsed_ms": round((time.time() - t0) * 1000),
        }


# ---------------------------------------------------------------------------
# Autonomous analysis — Neuron generates insights without user prompt
# ---------------------------------------------------------------------------

async def analyze_infrastructure(live_context: dict) -> dict:
    """
    Neuron autonomously analyzes the current infrastructure state and
    generates prioritized observations and recommendations.
    """
    from cvg_neuron.integrations import summarize_live_context

    ctx_summary = summarize_live_context(live_context)

    prompt = f"""Analyze the current CVG infrastructure state below and provide:
1. A brief health summary (1-2 sentences)
2. Any issues or anomalies detected (be specific — service names, IPs, ports)
3. Top 3 prioritized action items
4. Any security or operational risks observed

{ctx_summary}

Be concise and actionable. Format with clear headers."""

    result = await chat(
        user_message=prompt,
        session_id="autonomous-analysis",
        live_context=live_context,
    )

    analysis_text = result.get("response", "")

    # Record as observation
    memory.record_observation(
        category="infrastructure",
        subject="autonomous-analysis",
        detail=analysis_text[:500],
        severity="info",
        source="neuron-autonomous",
    )
    memory.record_event("analysis", {"summary": analysis_text[:200]}, "neuron")

    return {
        "analysis":    analysis_text,
        "session_id":  result.get("session_id"),
        "model":       result.get("model"),
        "analyzed_at": time.time(),
        "context":     ctx_summary,
    }


async def analyze_deployment(service: str, version: str, logs: str = "") -> dict:
    """Analyze a deployment event and surface issues."""
    prompt = f"""A CVG service just deployed. Analyze for issues:

Service: {service}
Version: {version}
Deploy logs (last portion):
{logs[-2000:] if logs else "No logs provided"}

Check for:
1. Build errors or warnings
2. Startup failures
3. Version regressions
4. Missing dependencies
5. Port conflicts

Be specific. If no issues, say so briefly."""

    result = await chat(user_message=prompt, session_id=f"deploy-{service}")

    memory.record_event(
        "deployment_analysis",
        {"service": service, "version": version, "analysis": result.get("response", "")[:300]},
        service,
    )

    return {
        "service":   service,
        "version":   version,
        "analysis":  result.get("response"),
        "model":     result.get("model"),
        "elapsed_ms": result.get("elapsed_ms"),
    }


async def generate_report(report_type: str = "daily") -> dict:
    """Generate a structured intelligence report."""
    mem_stats = memory.get_stats()
    warnings  = memory.get_unresolved_warnings()
    events    = memory.get_recent_events(limit=20)
    patterns  = memory.get_top_patterns(limit=10)

    context_text = f"""
Memory stats: {mem_stats}
Unresolved warnings ({len(warnings)}):
{chr(10).join(f"  [{w['severity']}] {w['subject']}: {w['detail'][:80]}" for w in warnings[:5])}
Recent events:
{chr(10).join(f"  [{e['event_type']}] {e.get('service','')}: {str(e.get('payload',''))[:80]}" for e in events[:10])}
Top patterns:
{chr(10).join(f"  {p['pattern_key']} (x{p['occurrences']})" for p in patterns[:5])}
"""

    prompt = f"""Generate a {report_type} CVG infrastructure intelligence report.
Based on Neuron's memory and observations:

{context_text}

Include:
1. Executive summary
2. Service health overview
3. Active issues requiring attention
4. Security items
5. Recommendations

Format as a clear, structured report."""

    result = await chat(user_message=prompt, session_id="report-engine")

    return {
        "report_type": report_type,
        "report":      result.get("response"),
        "generated_at": time.time(),
        "model":       result.get("model"),
        "memory_stats": mem_stats,
    }


# ---------------------------------------------------------------------------
# Topic inference (for pattern learning)
# ---------------------------------------------------------------------------

_TOPIC_KEYWORDS = {
    "deployment": ["deploy", "build", "docker", "container", "restart", "update"],
    "dns":        ["dns", "domain", "zone", "record", "cleargeo", "propagat"],
    "gis":        ["gis", "arcgis", "shapefile", "geoserver", "raster", "vector", "spatial", "layer"],
    "security":   ["security", "vulnerability", "wazuh", "audit", "trivy", "password", "token", "key"],
    "slr":        ["sea level", "slr", "surge", "storm", "inundation", "coastal"],
    "rainfall":   ["rainfall", "atlas 14", "idf", "stormwater", "flood", "noaa"],
    "infrastructure": ["vm", "docker", "proxmox", "queen", "container", "server", "node"],
    "git":        ["git", "gitea", "repo", "version", "commit", "push", "tag"],
}


def _infer_topic(message: str) -> str | None:
    msg_lower = message.lower()
    for topic, keywords in _TOPIC_KEYWORDS.items():
        if any(kw in msg_lower for kw in keywords):
            return topic
    return None
